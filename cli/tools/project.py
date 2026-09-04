"""c3_project tool -- run C3 against OTHER c3-installed projects.

Discovery and read ops run freely against any registered/.c3 project. Write ops
(``edit``, ``shell``, and memory mutations) require ``allow_write=True`` and are
recorded on the *target* project (its edit ledger + activity log), so a foreign
mutation leaves an audit trail in the project it touched.

The heavy lifting reuses the existing per-tool handlers unchanged -- only the
``svc`` (a ``C3Runtime``) differs, supplied by the shared foreign-runtime cache.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from services.project_runtime import (
    discover_projects,
    resolve_project,
    shared_cache,
)

# Memory sub-actions that mutate the target project's fact store.
_MEMORY_WRITE = {"add", "update", "delete", "consolidate", "consolidate_deep", "ground"}
# Dispatch verbs that mutate the target project.
_WRITE_OPS = {"edit", "shell"}
_DISCOVERY_OPS = {"list", "scan", "info", "register", "unregister", "subprojects",
                  "sub_tree", "sub_inspect"}
_READ_OPS = {
    "search", "read", "compress", "status", "memory",
    "impact", "edits", "validate", "filter",
}
# Sub-project governance verbs (project = the PARENT). sub_add/sub_link/
# sub_remove and cascade update/reindex mutate the target tree -> allow_write
# required. sub_tree and sub_inspect are reads and mutate nothing.
_SUB_OPS = {"subprojects", "sub_tree", "sub_inspect",
            "sub_add", "sub_link", "sub_remove", "sub_cascade"}


def _foreign_finalize(_name, _args, resp, _summ="", **_kw):
    """No-op finalize for proxied calls.

    The home session's finalize wraps the whole ``c3_project`` response, so the
    inner handlers must not also charge budget / log against either session.
    """
    return resp


def _foreign_facts(*_a, **_kw):
    return ""


def _runtime_for(path: str):
    """Indirection point (monkeypatched in tests) -> foreign ``C3Runtime``."""
    return shared_cache().get(path)


def _is_registered(path: str) -> bool:
    """True when *path* is in the cross-project registry. Fails closed."""
    try:
        from services import project_runtime as _pr
        resolved = _pr._resolved(path)
        return any(_pr._resolved(p.get("path", "")) == resolved
                   for p in _pr._read_registry())
    except Exception:
        return False


def _registration_error(action: str, resolved: dict) -> str:
    return (
        f"[c3_project:error] '{resolved['name']}' ({resolved['path']}) is not "
        f"a registered project — '{action}' requires registration; a bare "
        "directory containing .c3/ is not enough. Fix: "
        f"c3_project(action='register', project='{resolved['path']}')."
    )


def _proxy_guard(action: str, resolved: dict, caller_path: str, *,
                 file_path: str = "") -> str:
    """Access Guard pre-checks for proxied ops (docs/access-guard.md §3, S5).

    Effective rules for a proxied path = global ∪ caller-project ∪ the
    containing realm of the RESOLVED absolute path. Realm entry is checked
    for every proxied op (a caller/global rule can fence off a whole foreign
    tree); file-level verdicts run for read/compress/edit. Inner handlers
    enforce again through the target runtime — this layer exists so a
    foreign realm can never be MORE permissive than the caller's own policy.
    Returns a refusal string, or '' when permitted. Fails closed.
    """
    try:
        from services import access_guard as ag
    except Exception:
        return ("[c3_project:error] Access Guard evaluator unavailable — "
                "failing closed for proxied access.")
    target_root = resolved["path"]
    name = resolved["name"]

    def _deny(path: str, op: str) -> str:
        try:
            # caller realm (global ∪ caller rules; abs/basename globs) …
            d = ag.check(path, op, caller_path or ".")
            if d is None:
                # … then the containing realm of the RESOLVED path.
                d = ag.check(path, op, target_root)
        except Exception as exc:  # evaluator error → fail closed
            d = ag.Denial("<evaluator-error>", "deny", "builtin",
                          f"evaluator error: {type(exc).__name__}")
        if d is None:
            return ""
        return ag.refusal(d, path, op, surface="proxy", project=name)

    msg = _deny(target_root, "read")  # realm entry — is the target reachable?
    if msg:
        return msg
    if action in ("read", "compress", "edit") and (file_path or "").strip():
        op = "write" if action == "edit" else "read"
        for fp in str(file_path).split(","):
            fp = fp.strip()
            if not fp:
                continue
            p = fp if Path(fp).is_absolute() else str(Path(target_root) / fp)
            msg = _deny(p, op)
            if msg:
                return msg
    return ""


# ── Discovery renderers ────────────────────────────────────────────────────


def _render_discovery(scan_roots_csv: str, do_scan: bool) -> str:
    roots = [r.strip() for r in (scan_roots_csv or "").split(",") if r.strip()] or None
    data = discover_projects(scan_roots=roots, scan=do_scan)
    reg = data["registered"]
    unreg = data["unregistered"]

    out = [f"Registered C3 projects ({len(reg)}):"]
    if not reg:
        out.append("  (none -- c3_project(action='register', project='<path>') to add one)")
    for p in reg:
        flag = "" if p["accessible"] else "  [MISSING]"
        out.append(f"  - {p['name']:<28} {p['ide']:<12} {p['path']}{flag}")

    if do_scan:
        out.append("")
        out.append(f"Unregistered .c3 projects found nearby ({len(unreg)}):")
        if not unreg:
            out.append("  (none found near registered projects)")
        for p in unreg:
            out.append(f"  - {p['name']:<28} {'':<12} {p['path']}")
        if unreg:
            out.append("")
            out.append("Register one: c3_project(action='register', project='<path>')")
    return "\n".join(out)


def _render_info(project: str) -> str:
    try:
        resolved = resolve_project(project)
    except ValueError as e:
        return f"[c3_project:error] {e}"
    p = Path(resolved["path"])
    out = [
        f"Project: {resolved['name']}",
        f"  path        : {resolved['path']}",
        f"  .c3 present : {(p / '.c3').is_dir()}",
        f"  accessible  : {p.is_dir()}",
    ]
    try:
        from services.project_manager import ProjectManager

        details = ProjectManager().get_project_details(resolved["path"]) or {}
        for key in ("ide", "c3_version", "facts_count", "last_session", "active"):
            if key in details and details[key] not in (None, ""):
                out.append(f"  {key:<11} : {details[key]}")
    except Exception:
        pass
    return "\n".join(out)


def _do_register(project: str) -> str:
    if not (project or "").strip():
        return "[c3_project:error] register requires project='<path>'."
    path = Path(project).expanduser()
    if not path.exists():
        return f"[c3_project:error] Path does not exist: {project}"
    if not (path / ".c3").is_dir():
        return (
            f"[c3_project:error] No .c3 directory in {path}. "
            "Run 'c3 init' there first."
        )
    from services.project_manager import ProjectManager

    entry = ProjectManager().add_project(str(path.resolve()))
    return f"Registered: {entry['name']}  ({entry['path']})"


def _do_subprojects(action: str, project: str, *, target: str = "", tag: str = "",
                    mode: str = "", allow_write: bool = False,
                    from_project: str = "") -> str:
    """Sub-project governance on a parent project (tree/add/remove/cascade)."""
    try:
        resolved = resolve_project(project)
    except ValueError as e:
        return f"[c3_project:error] {e}"
    if not _is_registered(resolved["path"]):
        return _registration_error(action, resolved)
    from services.subprojects import (
        MAX_DEPTH,
        VALID_CASCADE_OPS,
        VALID_REMOVE_MODES,
        SubprojectManager,
        inspect_path,
    )

    sm = SubprojectManager(resolved["path"])

    def _blocked(label):
        return (f"[c3_project:blocked] '{label}' would modify project "
                f"'{resolved['name']}'. Re-run with allow_write=true to proceed.")

    def _audit(label):
        try:
            from services.activity_log import ActivityLog
            ActivityLog(resolved["path"]).log("cross_project_write", {
                "action": label, "from_project": from_project,
            })
        except Exception:
            pass

    if action in ("subprojects", "sub_tree"):
        # 'subprojects' keeps the direct-children view it has always had;
        # 'sub_tree' walks the whole hierarchy.
        levels = MAX_DEPTH if action == "sub_tree" else 1
        tree = sm.tree(depth=levels)
        if not tree["children"]:
            return (f"No sub-projects designated in {tree['parent']['name']}. "
                    "Link one: c3_project(action='sub_link', project='<parent>', "
                    "target='<path>', allow_write=true)")
        out = [f"Sub-projects of {tree['parent']['name']} ({tree['rollup']['children']}):"]

        def _rows(children, indent="  "):
            for c in children:
                loc = c["rel_path"] or c["path"]
                out.append(f"{indent}- {c['name']:<24} {c['status']:<16} "
                           f"{c['link_kind']:<9} alerts:{c['notification_count']:<3} {loc}")
                _rows(c.get("children") or [], indent + "    ")

        _rows(tree["children"])
        r = tree["rollup"]
        out.append(f"  rollup: {r['direct_children']} direct, {r['children']} total, "
                   f"{r['notifications']} alert(s), {r['issues']} issue(s)")
        return "\n".join(out)

    if action == "sub_inspect":
        # Read-only: reports what is at a path and how it is already linked.
        # Never registers, never links.
        probe = target or resolved["path"]
        rep = inspect_path(probe)
        out = [f"Inspect: {rep['path']}"]
        if not rep["is_dir"]:
            return out[0] + "\n  not a folder"
        p = rep.get("project")
        if p:
            out.append(f"  C3 project '{p['name']}' v{p.get('c3_version') or '?'} "
                       f"({'registered' if rep['registered'] else 'NOT registered'})")
            out.append(f"  {p['facts_count']} facts, {p['sessions']} sessions, "
                       f"{p['edit_ledger_entries']} ledger entries")
        else:
            out.append("  no .c3 — not a C3 project yet")
        out.append("  parent: " + (" < ".join(a["name"] for a in rep["ancestors"])
                                   if rep["ancestors"] else "(top-level)"))
        for c in rep["children"]:
            out.append(f"    child: {c['name']:<24} {c['link_kind']}/{c['status']}  {c['path']}")
        for d in rep["detected"]:
            out.append(f"    detected (unlinked): {d['name']:<20} {d['path']}")
        for w in rep["warnings"]:
            out.append(f"  warning: {w}")
        return "\n".join(out)

    if action in ("sub_add", "sub_link"):
        if not allow_write:
            return _blocked(action)
        if not (target or "").strip():
            return (f"[c3_project:error] {action} requires target='<folder|path>' "
                    "(relative to the parent, or absolute anywhere on disk). "
                    "Optional: tag='<display name>'.")
        # sub_link means "this is already a project" — it refuses to create one.
        if action == "sub_link":
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = Path(resolved["path"]) / target
            probe = inspect_path(candidate, detect=False)
            if probe["is_dir"] and not probe["has_c3"]:
                return (f"[c3_project:error] not a C3 project: {probe['path']}. "
                        "Use action='sub_add' to initialize it as one.")
        res = sm.add(target, name=(tag or None), run_init=(action == "sub_add"))
        if not res.get("added"):
            return f"[c3_project:error] {res.get('error')}"
        _audit(action)
        if res["link_kind"] == "external":
            return (f"Linked by path: {res['name']}  ({res['path']}) at depth "
                    f"{res['depth']} — outside the parent tree, so the parent's "
                    "index is unchanged.")
        verb = "Adopted (existing .c3 kept)" if res.get("adopted") else "Initialized"
        return (f"{verb}: {res['name']}  ({res['path']}) at depth {res['depth']} "
                "— parent index now excludes it.")

    if action == "sub_remove":
        if not allow_write:
            return _blocked("sub_remove")
        if not (target or "").strip():
            return "[c3_project:error] sub_remove requires target='<name|path>'."
        m = mode if mode in VALID_REMOVE_MODES else "unlink"
        res = sm.remove(target, mode=m)
        if not res.get("removed"):
            return f"[c3_project:error] {res.get('error')}"
        _audit(f"sub_remove/{m}")
        verb = "Cleared (.c3 wiped, unregistered)" if m == "clear" else "Unlinked (now top-level)"
        return f"{verb}: {res.get('name')}  ({res.get('path')})"

    if action == "sub_cascade":
        op = (mode or "").strip().lower()
        if op not in VALID_CASCADE_OPS:
            return ("[c3_project:error] sub_cascade requires "
                    f"mode='{'|'.join(VALID_CASCADE_OPS)}'.")
        if op != "health" and not allow_write:
            return _blocked(f"sub_cascade/{op}")
        res = sm.cascade(op)
        if op != "health":
            _audit(f"sub_cascade/{op}")
        rows = []
        for r in res["results"]:
            mark = "OK" if r["ok"] else "FAIL"
            extra = f" -- {r.get('error')}" if r.get("error") else ""
            rows.append(f"  [{mark:<4}] {r['name']:<24} {r['elapsed_ms']:>6}ms{extra}")
        s = res["summary"]
        return (f"cascade {op} on {resolved['name']}: {s['ok']}/{s['total']} ok, "
                f"{s['failed']} failed\n" + "\n".join(rows))

    return f"[c3_project:error] Unhandled sub-project op '{action}'."


def _do_unregister(project: str) -> str:
    try:
        resolved = resolve_project(project)
    except ValueError as e:
        return f"[c3_project:error] {e}"
    from services.project_manager import ProjectManager

    removed = ProjectManager().remove_project(resolved["path"])
    return (
        f"Unregistered: {resolved['name']}"
        if removed
        else f"Not in registry: {resolved['name']}"
    )


# ── Proxied op dispatch ────────────────────────────────────────────────────


def _proxy(action, fsvc, *, query, file_path, symbols, lines, mode, view, top_k,
           max_tokens, search_action, mem_action, fact, category, fact_id,
           edits_action, file, tag, limit, target, old_string, new_string,
           summary, edits, replace_all, tags, cmd, timeout, project_path):
    if action == "search":
        from cli.tools.search import handle_search

        return handle_search(query, search_action, top_k, max_tokens,
                             fsvc, _foreign_finalize, _foreign_facts)
    if action == "read":
        from cli.tools.read import handle_read

        return handle_read(file_path, symbols=symbols, lines=lines,
                           svc=fsvc, finalize=_foreign_finalize)
    if action == "compress":
        from cli.tools.compress import handle_compress

        return handle_compress(file_path, mode, fsvc, _foreign_finalize, _foreign_facts)
    if action == "status":
        from cli.tools.status import handle_status

        return handle_status(view, False, fsvc, _foreign_finalize)
    if action == "memory":
        from cli.tools.memory import handle_memory

        return handle_memory(mem_action, query, fact, category, top_k,
                            fsvc, _foreign_finalize, fact_id=fact_id)
    if action == "impact":
        from cli.tools.impact import handle_impact

        imode = mode if mode in ("symbol", "unstaged") else "symbol"
        return handle_impact(target, file_path, imode, fsvc, _foreign_finalize)
    if action == "edits":
        from cli.tools.edits import handle_edits

        # old_string/new_string/edits are threaded through for edits_action=
        # 'verify'. A cross-project edit that times out is the same problem as a
        # local one, and the proxy carries those params already (#74).
        return handle_edits(edits_action, file, "", "", "", tags, limit, "", "",
                           tag, fsvc, _foreign_finalize, "",
                           old_string, new_string, edits)
    if action == "validate":
        from cli.tools.validate import handle_validate

        return asyncio.run(handle_validate(file_path, fsvc, _foreign_finalize))
    if action == "filter":
        from cli.tools.filter import handle_filter

        return handle_filter(file_path, "", query, 100, "smart", False,
                           fsvc, _foreign_finalize)
    if action == "edit":
        from cli.tools.edit import handle_edit

        return handle_edit(file_path, old_string, new_string, summary, tags,
                          replace_all, fsvc, _foreign_finalize, edits)
    if action == "shell":
        from cli.tools.shell import handle_shell

        # enable_creds=False: a proxied shell must never expand or inject the
        # TARGET project's credentials — its vault is reachable only from its
        # own runtime.
        return asyncio.run(handle_shell(cmd, project_path, timeout, True, True,
                                       fsvc, _foreign_finalize,
                                       enable_creds=False))
    return f"[c3_project:error] Unhandled op '{action}'."


# ── Entry point ────────────────────────────────────────────────────────────


def handle_project(action, svc, finalize, *, project="", query="", file_path="",
                   symbols=None, lines=None, mode="map", view="health", top_k=5,
                   max_tokens=1200, search_action="code", mem_action="recall",
                   fact="", category="", fact_id="", edits_action="history",
                   file="", tag="", limit=50, target="", old_string="",
                   new_string="", summary="", edits="", replace_all=False,
                   tags="", cmd="", timeout=60, scan_roots="", allow_write=False):
    action = (action or "").strip().lower()

    def done(resp, summ="ok"):
        return finalize("c3_project", {"action": action, "project": project},
                        resp, summ)

    if not action:
        return done(
            "[c3_project:error] action required. "
            f"Discovery: {', '.join(sorted(_DISCOVERY_OPS))}. "
            f"Read: {', '.join(sorted(_READ_OPS))}. "
            f"Write (allow_write=true): {', '.join(sorted(_WRITE_OPS))}. "
            "Sub-projects: sub_tree, sub_inspect (reads); "
            "sub_add, sub_link, sub_remove, sub_cascade (allow_write=true).",
            "error")

    # ── Discovery (no foreign runtime needed) ──────────────────────────
    if action in ("list", "scan"):
        return done(_render_discovery(scan_roots, action == "scan"), f"{action} projects")
    if action == "info":
        return done(_render_info(project), "project info")
    if action == "register":
        return done(_do_register(project), "register project")
    if action == "unregister":
        return done(_do_unregister(project), "unregister project")
    if action in _SUB_OPS:
        return done(
            _do_subprojects(action, project, target=target, tag=tag, mode=mode,
                            allow_write=allow_write,
                            from_project=str(getattr(svc, "project_path", ""))),
            action)

    if action in ("shell_job", "job", "jobs"):
        # S3: a background job is bound to the project AND session that
        # started it (services/shell_jobs.py); proxying one would either run
        # it under the caller's identity or hand another project's session a
        # handle it must not have. Not offered cross-project.
        return done(
            "[c3_project:error] c3_shell_job is not proxied across projects — "
            "start, poll and cancel background jobs from the target project's "
            "own session.",
            "error")

    if action not in _READ_OPS and action not in _WRITE_OPS:
        return done(
            f"[c3_project:error] Unknown action '{action}'. "
            f"Discovery: {', '.join(sorted(_DISCOVERY_OPS))}. "
            f"Read: {', '.join(sorted(_READ_OPS))}. "
            f"Write (allow_write=true): {', '.join(sorted(_WRITE_OPS))}.",
            "error")

    # ── Write guard ────────────────────────────────────────────────────
    is_write = action in _WRITE_OPS or (
        action == "memory" and (mem_action or "").lower() in _MEMORY_WRITE
    )
    if is_write and not allow_write:
        label = action + (f"/{mem_action}" if action == "memory" else "")
        return done(
            f"[c3_project:blocked] '{label}' would modify project '{project}'. "
            "Re-run with allow_write=true to proceed.",
            "blocked")

    # ── Resolve + borrow the foreign runtime ───────────────────────────
    try:
        resolved = resolve_project(project)
    except ValueError as e:
        return done(f"[c3_project:error] {e}", "error")

    # Registration gate (Access Guard T2c): a bare directory that merely
    # contains .c3/ is NOT a valid proxy target — only list/scan/register/
    # info accept unregistered paths. This closes the mint-a-rule-free-
    # project pivot (create a fresh .c3 dir, proxy through its empty realm).
    if not _is_registered(resolved["path"]):
        return done(_registration_error(action, resolved), "error")

    # Access Guard proxy verdicts (S5) — before the foreign runtime is even
    # built, so a denied realm never spins up an indexer.
    guard_msg = _proxy_guard(
        action, resolved, str(getattr(svc, "project_path", "") or "."),
        file_path=file_path)
    if guard_msg:
        return done(guard_msg, "denied")

    try:
        fsvc = _runtime_for(resolved["path"])
    except Exception as e:
        return done(
            f"[c3_project:error] Could not load '{resolved['name']}': {e}", "error")

    banner = f"[c3_project:{resolved['name']}] {action}\n"
    try:
        body = _proxy(
            action, fsvc, query=query, file_path=file_path, symbols=symbols,
            lines=lines, mode=mode, view=view, top_k=top_k, max_tokens=max_tokens,
            search_action=search_action, mem_action=mem_action, fact=fact,
            category=category, fact_id=fact_id, edits_action=edits_action,
            file=file, tag=tag, limit=limit, target=target, old_string=old_string,
            new_string=new_string, summary=summary, edits=edits,
            replace_all=replace_all, tags=tags, cmd=cmd, timeout=timeout,
            project_path=resolved["path"],
        )
    except Exception as e:
        return done(f"{banner}[error] {type(e).__name__}: {e}", "error")

    # Audit foreign mutations on the target project itself.
    if is_write and getattr(fsvc, "activity_log", None):
        try:
            fsvc.activity_log.log("cross_project_write", {
                "action": action,
                "from_project": getattr(svc, "project_path", ""),
            })
        except Exception:
            pass

    return done(banner + (body or ""), f"{action} on {resolved['name']}")
