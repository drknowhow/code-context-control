"""c3_artifacts — agent-config tracking: inventory, history, diff, restore."""

from pathlib import Path

from cli.tools import _grants
from services import access_guard

READ_ACTIONS = {"scan", "list", "history", "show", "diff", "status"}

_ACTIONS = "scan, list, history, show, diff, restore, status"
_SHOW_MAX_LINES = 200


def _fmt_artifact(a) -> str:
    marker = "" if a.get("exists", True) else " [deleted]"
    roles = f" +{','.join(a['roles'])}" if a.get("roles") else ""
    when = (a.get("last_changed") or "")[:10]
    return (f"  {a['id']} v{a['version']} {a['unit_hash'] or '-'} {when} "
            f"({a['files']} file{'s' if a['files'] != 1 else ''}){roles}{marker}")


def _fmt_event(e, with_id=False) -> str:
    summ = f" — {e['summary']}" if e.get("summary") else ""
    aid = f"{e.get('artifact_id')} " if with_id else ""
    return (f"  {aid}v{e.get('version')} {e.get('event'):<8} "
            f"src:{e.get('source'):<11} {e.get('ts', '')}{summ}")


def handle_artifacts(action, svc, finalize, *, artifact="", cls="", provider="",
                     version=0, against=0, limit=50) -> str:
    action = (action or "").strip().lower()

    def done_resp(resp, summ="ok"):
        return finalize("c3_artifacts", {"action": action, "artifact": artifact},
                        resp, summ)

    store = getattr(svc, "artifact_store", None)
    if store is None:
        return done_resp("[artifacts:error] artifact store unavailable on this runtime",
                         "error")

    cfg = (getattr(svc, "hybrid_config", None) or {}).get("agent_artifacts") or {}
    if not cfg.get("enabled", True):
        return done_resp("[artifacts:disabled] artifact tracking is off "
                         "(hybrid.agent_artifacts.enabled=false)", "disabled")

    if not action:
        return done_resp(f"[artifacts:error] action required. Actions: {_ACTIONS}.",
                         "error")
    try:
        version = int(version)
        against = int(against)
        limit = int(limit)
    except (TypeError, ValueError):
        return done_resp("[artifacts:error] version/against/limit must be integers",
                         "error")

    if action == "scan":
        store.consume_pending()
        res = store.scan()
        tracked = len(store.list_artifacts())
        head = (f"[artifacts:scan] {tracked} tracked — {len(res['added'])} added, "
                f"{len(res['modified'])} modified, {len(res['deleted'])} deleted")
        lines = [head]
        for ev in res["events"]:
            lines.append(_fmt_event(ev, with_id=True))
        return done_resp("\n".join(lines),
                         f"{len(res['added'])}a/{len(res['modified'])}m/{len(res['deleted'])}d")

    if action == "list":
        rows = store.list_artifacts(cls=cls, provider=provider)
        if not rows:
            return done_resp("[artifacts:list] none tracked yet — run "
                             "c3_artifacts(action='scan') first", "0")
        lines = [f"[artifacts:list] {len(rows)} artifact(s)"]
        lines += [_fmt_artifact(a) for a in rows]
        return done_resp("\n".join(lines), f"{len(rows)}")

    if action == "status":
        st = store.status()
        by_cls = ", ".join(f"{k}:{v}" for k, v in sorted(st["by_class"].items()))
        missing = f", {st['missing']} deleted" if st.get("missing") else ""
        pend = (f", {st['pending_signals']} pending signal(s)"
                if st.get("pending_signals") else "")
        return done_resp(
            f"[artifacts:status] {st['tracked']} tracked ({by_cls}){missing} — "
            f"{st['out_of_band_recent']} recent out-of-band change(s), "
            f"last scan {st['last_scan'] or 'never'}{pend}",
            f"{st['tracked']}t")

    if action == "history":
        events = store.get_history(artifact=artifact, limit=limit)
        if not events:
            scopef = f" for {artifact}" if artifact else ""
            return done_resp(f"[artifacts:history] no events{scopef}", "0")
        scopef = f" {artifact} —" if artifact else ""
        lines = [f"[artifacts:history]{scopef} {len(events)} event(s)"]
        lines += [_fmt_event(e, with_id=not artifact) for e in events]
        return done_resp("\n".join(lines), f"{len(events)}e")

    # Remaining actions operate on one artifact.
    if action not in ("show", "diff", "restore"):
        return done_resp(f"[artifacts:error] unknown action '{action}'. "
                         f"Actions: {_ACTIONS}.", "error")
    if not artifact:
        return done_resp(f"[artifacts:error] {action} requires artifact "
                         "(id, unique prefix, or path).", "error")

    if action == "show":
        res = store.get_version(artifact, version)
        if "error" in res:
            return done_resp(f"[artifacts:error] {res['error']}", "error")
        total = sum(m["size"] for m in res["members"])
        lines = [f"[artifacts:show] {res['id']} {res['version']} "
                 f"({len(res['members'])} file(s), {total}B)"]
        for m in res["members"]:
            lines.append(f"--- {m['path']} ---")
            if m.get("binary"):
                lines.append(f"[binary, {m['size']}B]")
            elif m.get("text") is None:
                lines.append("[not stored — exceeded size cap at capture]")
            else:
                text = m["text"].splitlines()
                lines += text[:_SHOW_MAX_LINES]
                if len(text) > _SHOW_MAX_LINES:
                    lines.append(f"... (+{len(text) - _SHOW_MAX_LINES} more lines)")
        return done_resp("\n".join(lines), res["version"])

    if action == "diff":
        if not version:
            return done_resp("[artifacts:error] diff requires version (the "
                             "older side; against=0 diffs vs live).", "error")
        res = store.diff(artifact, version, against or None)
        if "error" in res:
            return done_resp(f"[artifacts:error] {res['error']}", "error")
        return done_resp(
            f"[artifacts:diff] {res['id']} {res['from']} -> {res['to']} "
            f"(+{res['plus']} -{res['minus']})\n{res['diff']}",
            f"+{res['plus']}-{res['minus']}")

    if action == "restore":
        if not version:
            return done_resp("[artifacts:error] restore requires version.", "error")
        session = getattr(getattr(svc, "session_mgr", None), "current_session", None) or {}
        sid = session.get("id", "")
        granted = ""
        try:
            try:
                res = store.restore(artifact, version, session_id=sid,
                                    confirm="hold")
            except access_guard.AccessDenied as exc:
                if exc.denial.kind != "confirm":
                    raise
                # An agent-issued restore of a held file pauses like any
                # other agent write to it (v2.102.0): the hold is filed
                # against the artifact's ROOT so one approval covers a
                # multi-file unit (a skill directory), and the retry — this
                # same call — consumes that grant and restores under it.
                entry = store.resolve(artifact) or {}
                root = str(Path(str(svc.project_path))
                           / str(entry.get("root") or artifact))
                granted = _grants.allow(svc, exc.denial, tool="c3_artifacts",
                                        op="write", path=root) or ""
                if not granted:
                    rid, note = _grants.confirm_request(
                        svc, exc.denial, tool="c3_artifacts", op="write",
                        path=root)
                    return done_resp(
                        access_guard.refusal(exc.denial, root, "write",
                                             request_id=rid, request_note=note),
                        "access-confirm")
                res = store.restore(artifact, version, session_id=sid,
                                    confirm="approved")
        except access_guard.AccessDenied as exc:
            # Service layer raises; the tool boundary converts to the S1/S2
            # refusal string (docs/access-guard.md §3).
            return done_resp(exc.message, "access-denied")
        if "error" in res:
            return done_resp(f"[artifacts:error] {res['error']}", "error")
        ledger = getattr(svc, "edit_ledger", None)
        if ledger is not None:
            try:
                for path in res["files_written"]:
                    ledger.log_edit(path, "restored",
                                    f"restored {res['id']} to v{version} via c3_artifacts",
                                    tags=["artifact_restore"],
                                    session_id=session.get("id", ""))
            except Exception:
                pass
        lines = ([granted] if granted else []) + [
                 f"[artifacts:restored] {res['id']} v{version} -> live as "
                 f"v{res['new_version']} — {len(res['files_written'])} file(s) written"
                 + (f", {len(res['files_removed'])} removed" if res["files_removed"] else "")]
        for w in res["warnings"]:
            lines.append(f"  warning: {w}")
        return done_resp("\n".join(lines), f"v{res['new_version']}")

    raise AssertionError("unreachable")  # all actions handled above
