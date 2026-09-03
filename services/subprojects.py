"""Sub-project designation and governance for C3 parent projects.

A *sub-project* is a c3-initialized project that carries its own full ``.c3``
branch (index, memory, ledger, config) and is declared a child of a parent:

- parent ``.c3/config.json`` lists children under ``subprojects``
- child ``.c3/config.json`` back-links via ``parent``
- the global registry entry (``~/.c3/projects.json``) carries ``parent_path``

Two link kinds, distinguished by how the entry addresses the child:

``nested``
    The child folder lives inside the parent tree. The entry stores a
    parent-relative ``rel_path``, and the parent's own index / doc-index /
    dictionary / watcher exclude that subtree (see ``exclusion_prefixes``)
    so nothing is indexed twice.

``external``
    The child lives anywhere on disk — a sibling folder, another drive. The
    entry stores an absolute ``path`` and no ``rel_path``. There is nothing to
    exclude, because the child was never inside the parent's scan tree.

Hierarchy is a **strict tree**: one parent per project (the registry's single
``parent_path``), many children, nested to ``MAX_DEPTH``. Containment used to
make cycles structurally impossible; once children can live anywhere it does
not, so ``validate()`` walks the ancestor chain explicitly.

Cross-scope visibility is restored by search/memory federation
(``cli/tools/federate.py``).
"""

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

from services.project_manager import ProjectManager

VALID_CASCADE_OPS = ("update", "reindex", "health")
VALID_REMOVE_MODES = ("unlink", "clear")

LINK_NESTED = "nested"
LINK_EXTERNAL = "external"

#: Hard ceiling on hierarchy depth. Guards a malformed or looping chain of
#: ``parent`` back-links as much as it caps legitimate nesting — every walk
#: over the chain is bounded by this *and* a visited-set.
MAX_DEPTH = 8


# ── Path helpers ───────────────────────────────────────────────────────────

def _norm(p) -> str:
    """Case-normalized resolved path string for comparisons (Windows-safe)."""
    return os.path.normcase(str(Path(p).resolve()))


def _same_path(a, b) -> bool:
    return _norm(a) == _norm(b)


def is_within(child, parent) -> bool:
    """True when ``child`` is strictly inside ``parent`` (not equal)."""
    child_n, parent_n = _norm(child), _norm(parent)
    if child_n == parent_n:
        return False
    return child_n.startswith(parent_n.rstrip("\\/") + os.sep)


def _utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ── Config I/O (tolerant, atomic) ──────────────────────────────────────────

def _read_config(project_path) -> dict:
    config_path = Path(project_path) / ".c3" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _write_config(project_path, config: dict) -> None:
    cfg_dir = Path(project_path) / ".c3"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg_dir / "config.json.tmp"
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_dir / "config.json")


def get_subprojects(parent_path) -> list:
    """Raw ``subprojects`` entries from the parent config (may be [])."""
    subs = _read_config(parent_path).get("subprojects")
    return [s for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []


# ── Entry addressing ───────────────────────────────────────────────────────
#
# An entry addresses its child either relatively (``rel_path``, nested) or
# absolutely (``path``, external). Everything downstream — status, listing,
# reconcile, removal, the hub's link annotations — resolves through
# ``entry_abs_path``, so this pair is the only place the two forms are known
# apart.

def entry_link_kind(entry: dict) -> str:
    """``external`` when the entry carries an absolute ``path``, else ``nested``."""
    return LINK_EXTERNAL if (entry or {}).get("path") else LINK_NESTED


def entry_abs_path(parent_path, entry) -> Path:
    """Resolve a sub-project entry to an absolute path.

    Accepts a full entry dict (preferred) or a bare ``rel_path`` string, so
    pre-2.96 call sites that passed ``entry["rel_path"]`` keep working.
    """
    if isinstance(entry, dict):
        if entry.get("path"):
            return Path(entry["path"]).resolve()
        rel = entry.get("rel_path") or ""
    else:
        rel = entry or ""
    return (Path(parent_path) / PurePosixPath(rel)).resolve()


def _rel_or_none(target, base) -> str | None:
    """POSIX relpath from ``base`` to ``target``, or None when impossible.

    ``os.path.relpath`` raises on Windows when the two sides sit on different
    drives (``U:\\`` vs ``W:\\``) — which is precisely the external-link case,
    so the back-link's ``rel_path`` has to be allowed to be absent.
    """
    try:
        return Path(os.path.relpath(str(target), str(base))).as_posix()
    except (ValueError, OSError):
        return None


def parent_link(project_path) -> dict:
    """The child's ``parent`` back-link record, or ``{}`` when top-level."""
    link = _read_config(project_path).get("parent")
    return link if isinstance(link, dict) and link.get("path") else {}


def ancestors(project_path, max_depth: int = MAX_DEPTH) -> list:
    """Walk ``parent`` back-links upward: nearest parent first, root last.

    Bounded by ``max_depth`` and a visited-set so a corrupt or self-referential
    config chain terminates instead of spinning.
    """
    out, seen = [], {_norm(project_path)}
    current = project_path
    for _ in range(max_depth):
        link = parent_link(current)
        if not link:
            break
        nxt = link.get("path")
        if not nxt or _norm(nxt) in seen:
            break
        seen.add(_norm(nxt))
        out.append({"name": link.get("name") or Path(nxt).name, "path": str(nxt)})
        current = nxt
    return out


def depth_of(project_path) -> int:
    """0 for a top-level project, 1 for its child, and so on."""
    return len(ancestors(project_path))


def subtree_depth(project_path, limit: int = MAX_DEPTH) -> int:
    """How many levels of descendants hang below ``project_path`` (0 = none).

    Breadth-first with a visited-set, so linking a project that already has a
    deep subtree can be checked against ``MAX_DEPTH`` before it is attached.
    """
    seen = {_norm(project_path)}
    frontier, levels = [str(project_path)], 0
    while frontier and levels < limit:
        nxt = []
        for path in frontier:
            for entry in get_subprojects(path):
                child = entry_abs_path(path, entry)
                key = _norm(child)
                if key in seen:
                    continue
                seen.add(key)
                nxt.append(str(child))
        if not nxt:
            break
        levels += 1
        frontier = nxt
    return levels


# ── Index-exclusion helpers (hot path for indexer/doc_index/watcher) ──────

def exclusion_prefixes(project_path) -> list:
    """Lowercased path-part tuples for designated sub-project folders.

    Fast path: ``[]`` when the project has no sub-projects, so scan loops
    pay nothing in the common case.

    **External children contribute nothing.** They carry no ``rel_path``
    because they were never inside the parent's tree, so there is no subtree
    to carve out — the empty-parts test below is what skips them.
    """
    subs = get_subprojects(project_path)
    if not subs:
        return []
    prefixes = []
    for entry in subs:
        rel = entry.get("rel_path") or ""
        parts = tuple(p.lower() for p in PurePosixPath(rel).parts)
        if parts:
            prefixes.append(parts)
    return prefixes


def is_excluded(rel_parts, prefixes) -> bool:
    """True when a project-relative path (as parts) sits under any prefix."""
    if not prefixes:
        return False
    normed = tuple(p.lower() for p in rel_parts)
    return any(normed[: len(pre)] == pre for pre in prefixes)


def make_excluder(project_path):
    """Return a predicate ``path -> bool`` (True = inside a sub-project).

    Cheap no-op lambda when the project has no sub-projects.
    """
    prefixes = exclusion_prefixes(project_path)
    if not prefixes:
        return lambda fpath: False
    root = Path(project_path).resolve()

    def _excluded(fpath) -> bool:
        # Resolve fpath too: callers may pass paths through a symlink
        # (macOS /var/folders) or an 8.3 short name (Windows), which would
        # never be relative to the resolved root.
        try:
            return is_excluded(Path(fpath).resolve().relative_to(root).parts, prefixes)
        except (ValueError, OSError):
            return False

    return _excluded


def _notification_count(project_path) -> int:
    """Unacknowledged notification count (same format as the hub reads)."""
    nf = Path(project_path) / ".c3" / "notifications.jsonl"
    if not nf.exists():
        return 0
    count = 0
    try:
        for line in nf.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                if not json.loads(line).get("acknowledged"):
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return count


def _facts_count(project_path) -> int:
    facts_file = Path(project_path) / ".c3" / "facts" / "facts.json"
    if not facts_file.exists():
        return 0
    try:
        data = json.loads(facts_file.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


class SubprojectManager:
    """Designate, link, inspect, and govern sub-projects of one parent."""

    def __init__(self, parent_path: str):
        self.parent_path = str(Path(parent_path).resolve())
        self.pm = ProjectManager()

    # ── Introspection ──────────────────────────────────────────────

    def _parent_name(self) -> str:
        cfg = _read_config(self.parent_path)
        meta = cfg.get("meta") or {}
        return meta.get("name") or Path(self.parent_path).name

    def _abs_child(self, entry) -> Path:
        """Resolve one entry (dict, or a bare ``rel_path`` string) to a path."""
        return entry_abs_path(self.parent_path, entry)

    def _resolve_ref(self, ref: str) -> dict | None:
        """Find a parent-config entry by name, rel_path, or path."""
        entries = get_subprojects(self.parent_path)
        ref_l = (ref or "").strip().lower()
        for e in entries:
            if (e.get("name") or "").lower() == ref_l:
                return e
        for e in entries:
            rel = e.get("rel_path")
            if not rel:
                continue  # external entry — matched by path below
            if PurePosixPath(rel).as_posix().lower() == PurePosixPath(ref.replace("\\", "/")).as_posix().lower():
                return e
        try:
            ref_abs = Path(self.parent_path, ref) if not Path(ref).is_absolute() else Path(ref)
            for e in entries:
                if _same_path(self._abs_child(e), ref_abs):
                    return e
        except OSError:
            pass
        return None

    def _entry_status(self, entry: dict, registry: list = None) -> str:
        abs_path = self._abs_child(entry)
        if not abs_path.is_dir():
            return "missing_folder"
        if not (abs_path / ".c3").is_dir():
            return "missing_c3"
        back = _read_config(abs_path).get("parent") or {}
        if not back.get("path") or not _same_path(back.get("path", ""), self.parent_path):
            return "backlink_broken"
        # Raw registry read — list_projects() probes ports, far too heavy here.
        if registry is None:
            registry = self.pm._read_projects()
        registered = any(
            _same_path(p.get("path", ""), abs_path) and p.get("parent_path")
            and _same_path(p.get("parent_path"), self.parent_path)
            for p in registry
        )
        if not registered:
            return "unregistered"
        return "ok"

    def list(self) -> list:
        """Parent-config entries enriched with status and live counts.

        Direct children only — see ``tree()`` for the recursive view.
        """
        out = []
        registry = self.pm._read_projects()
        for entry in get_subprojects(self.parent_path):
            abs_path = self._abs_child(entry)
            status = self._entry_status(entry, registry)
            out.append({
                "name": entry.get("name") or abs_path.name,
                "rel_path": entry.get("rel_path", ""),
                "path": str(abs_path),
                "link_kind": entry_link_kind(entry),
                "added_at": entry.get("added_at"),
                "status": status,
                "facts_count": _facts_count(abs_path) if status not in ("missing_folder", "missing_c3") else 0,
                "notification_count": _notification_count(abs_path) if status != "missing_folder" else 0,
            })
        return out

    def tree(self, depth: int = MAX_DEPTH) -> dict:
        """Hub-facing parent + descendants + transitive rollup.

        ``depth=1`` reproduces the pre-2.96 single-level shape. Deeper levels
        hang off each child as its own ``children`` list, and the rollup counts
        the whole subtree, not just the first hop.
        """
        seen = {_norm(self.parent_path)}

        def _walk(path: str, remaining: int) -> list:
            if remaining <= 0:
                return []
            rows = SubprojectManager(path).list() if not _same_path(path, self.parent_path) else self.list()
            for row in rows:
                key = _norm(row["path"])
                if key in seen:
                    # Already visited on this walk: a cycle, or a project
                    # reachable twice. Render it, but do not descend again.
                    row["children"] = []
                    row["rollup"] = {"children": 0, "notifications": 0, "issues": 0}
                    continue
                seen.add(key)
                kids = _walk(row["path"], remaining - 1) if row["status"] != "missing_folder" else []
                row["children"] = kids
                row["rollup"] = {
                    "children": len(kids) + sum(k["rollup"]["children"] for k in kids),
                    "notifications": sum(k["notification_count"] + k["rollup"]["notifications"] for k in kids),
                    "issues": sum((1 if k["status"] != "ok" else 0) + k["rollup"]["issues"] for k in kids),
                }
            return rows

        children = _walk(self.parent_path, max(1, int(depth or 1)))
        return {
            "parent": {"name": self._parent_name(), "path": self.parent_path,
                       "depth": depth_of(self.parent_path)},
            "children": children,
            "rollup": {
                "children": len(children) + sum(c["rollup"]["children"] for c in children),
                "notifications": sum(c["notification_count"] + c["rollup"]["notifications"]
                                     for c in children),
                "issues": sum((1 if c["status"] != "ok" else 0) + c["rollup"]["issues"]
                              for c in children),
                "direct_children": len(children),
            },
        }

    def descendants(self, depth: int = MAX_DEPTH) -> list:
        """Flat, de-duplicated list of every descendant, nearest level first."""
        out, seen = [], {_norm(self.parent_path)}
        frontier = [self.parent_path]
        for _ in range(max(1, int(depth or 1))):
            nxt = []
            for path in frontier:
                for row in SubprojectManager(path).list():
                    key = _norm(row["path"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(row)
                    if row["status"] != "missing_folder":
                        nxt.append(row["path"])
            if not nxt:
                break
            frontier = nxt
        return out

    # ── Designation ────────────────────────────────────────────────

    def validate(self, folder: str) -> dict:
        """Pre-flight the add() validation chain without mutating anything."""
        out = {
            "ok": False, "exists": False, "is_dir": False, "has_c3": False,
            "registered": False, "already_child_of": None, "is_ancestor": False,
            "already_linked": False, "path": "", "warnings": [],
            "link_kind": LINK_NESTED, "rel_path": None,
            "parent_depth": 0, "depth": 0, "subtree_depth": 0,
            "would_create_cycle": False, "ancestors": [],
        }
        try:
            target = Path(folder) if Path(folder).is_absolute() else Path(self.parent_path) / folder
            target = target.resolve()
        except OSError as e:
            out["warnings"].append(f"unresolvable path: {e}")
            return out
        out["path"] = str(target)
        out["exists"] = target.exists()
        out["is_dir"] = target.is_dir()
        if not out["is_dir"]:
            out["warnings"].append("folder does not exist")
            return out

        # ── Cycle safety ───────────────────────────────────────────
        # Containment used to make cycles impossible by construction. Once a
        # child can live anywhere, the ancestor chain has to be walked.
        out["ancestors"] = ancestors(self.parent_path)
        out["parent_depth"] = len(out["ancestors"])
        out["depth"] = out["parent_depth"] + 1

        if _same_path(target, self.parent_path):
            out["is_ancestor"] = True
            out["would_create_cycle"] = True
            out["warnings"].append("folder is the parent itself")
            return out
        if is_within(self.parent_path, target):
            out["is_ancestor"] = True
            out["would_create_cycle"] = True
            out["warnings"].append(
                "folder contains the parent — linking it would create a cycle")
            return out
        if any(_same_path(a["path"], target) for a in out["ancestors"]):
            out["is_ancestor"] = True
            out["would_create_cycle"] = True
            out["warnings"].append(
                "folder is already an ancestor of the parent — linking it would create a cycle")
            return out

        # ── Depth ──────────────────────────────────────────────────
        out["subtree_depth"] = subtree_depth(target)
        total_depth = out["depth"] + out["subtree_depth"]
        if total_depth > MAX_DEPTH:
            out["warnings"].append(
                f"hierarchy would be {total_depth} levels deep (max {MAX_DEPTH})")
            return out

        # ── Link kind ──────────────────────────────────────────────
        # Nested children carry a rel_path and are carved out of the parent's
        # index; external children are addressed absolutely and change nothing
        # about what the parent indexes.
        if is_within(target, self.parent_path):
            out["link_kind"] = LINK_NESTED
            out["rel_path"] = PurePosixPath(
                *target.relative_to(Path(self.parent_path).resolve()).parts).as_posix()
        else:
            out["link_kind"] = LINK_EXTERNAL
            out["warnings"].append(
                "folder is outside the parent — it will be linked by path, "
                "and the parent's index is unaffected")

        out["has_c3"] = (target / ".c3").is_dir()
        for e in get_subprojects(self.parent_path):
            if _same_path(self._abs_child(e), target):
                out["already_linked"] = True
                if out["has_c3"]:
                    out["warnings"].append("already designated as a sub-project")
                    return out
                out["warnings"].append("already designated but .c3 is missing; re-add will re-init")
                break

        if out["has_c3"]:
            back = parent_link(target)
            if back.get("path") and not _same_path(back["path"], self.parent_path):
                out["already_child_of"] = back["path"]
                out["warnings"].append(f"already a sub-project of {back['path']}")
                return out

        out["registered"] = any(
            _same_path(p.get("path", ""), target) for p in self.pm.list_projects()
        )
        if out["has_c3"]:
            out["warnings"].append("folder already has .c3 — it will be adopted (existing state kept)")
        out["ok"] = True
        return out

    def add(self, folder: str, name: str = None, ide: str = None,
            run_init: bool = True, reindex_parent: bool = True) -> dict:
        """Designate ``folder`` as a sub-project (full .c3 branch, linked)."""
        v = self.validate(folder)
        if not v["ok"]:
            return {"added": False, "error": "; ".join(v["warnings"]) or "validation failed",
                    "validation": v}

        target = Path(v["path"])
        link_kind = v["link_kind"]
        rel_posix = v["rel_path"]  # None for an external link
        name = name or target.name
        adopted = v["has_c3"]
        warnings = []

        if run_init and not adopted:
            # Lazy import: services -> cli only at call time (precedent:
            # ProjectManager.merge_projects cleanup block).
            from cli.c3 import _do_init
            _do_init(str(target), ide_name=ide)

        # Child back-link. rel_path is best-effort and omitted entirely when
        # the two sides sit on different Windows drives, where relpath raises.
        child_cfg = _read_config(target)
        back = {"name": self._parent_name(), "path": self.parent_path}
        back_rel = _rel_or_none(self.parent_path, target)
        if back_rel is not None:
            back["rel_path"] = back_rel
        child_cfg["parent"] = back
        _write_config(target, child_cfg)

        # Parent config entry (replace stale duplicate if re-adding).
        parent_cfg = _read_config(self.parent_path)
        entries = [e for e in parent_cfg.get("subprojects", []) if isinstance(e, dict)]
        entries = [e for e in entries if not _same_path(self._abs_child(e), target)]
        entry = {"name": name, "added_at": _utcnow()}
        if link_kind == LINK_EXTERNAL:
            entry["path"] = str(target)
            entry["link"] = LINK_EXTERNAL
        else:
            entry["rel_path"] = rel_posix
        entries.append(entry)
        parent_cfg["subprojects"] = entries
        _write_config(self.parent_path, parent_cfg)

        # Global registry.
        self.pm.add_project(str(target), name=name, parent_path=self.parent_path)

        # Only a nested child changes what the parent indexes; an external one
        # was never in the parent's scan tree, so there is nothing to rebuild.
        reindex = {}
        if reindex_parent and link_kind == LINK_NESTED:
            reindex = self._reindex_parent()

        result = {"added": True, "adopted": adopted, "name": name,
                  "rel_path": rel_posix, "path": str(target),
                  "link_kind": link_kind, "depth": v["depth"]}
        if reindex:
            result["parent_reindex"] = reindex
        if warnings:
            result["warnings"] = warnings
        return result

    def remove(self, ref: str, mode: str = "unlink", reindex_parent: bool = True) -> dict:
        """Unlink a sub-project (keep its .c3) or clear it entirely."""
        if mode not in VALID_REMOVE_MODES:
            return {"removed": False, "error": f"invalid mode: {mode} (use unlink|clear)"}
        entry = self._resolve_ref(ref)
        if entry is None:
            return {"removed": False, "error": f"no sub-project matches: {ref}"}

        abs_path = self._abs_child(entry)
        was_nested = entry_link_kind(entry) == LINK_NESTED
        warnings = []

        # Strip from parent config.
        parent_cfg = _read_config(self.parent_path)
        parent_cfg["subprojects"] = [
            e for e in parent_cfg.get("subprojects", [])
            if not (isinstance(e, dict)
                    and _same_path(self._abs_child(e), abs_path))
        ]
        if not parent_cfg["subprojects"]:
            parent_cfg.pop("subprojects", None)
        _write_config(self.parent_path, parent_cfg)

        if mode == "unlink":
            # Child keeps .c3 and stays registered as a top-level project.
            if (abs_path / ".c3").is_dir():
                child_cfg = _read_config(abs_path)
                if child_cfg.pop("parent", None) is not None:
                    _write_config(abs_path, child_cfg)
            self.pm.set_parent(str(abs_path), None)
        else:  # clear
            try:
                # Same cleanup helpers merge_projects uses (lazy cli import).
                from cli.c3 import _instruction_documents_for_project, _uninstall_mcp_all
                try:
                    # Project cleanup only. ~/.codex and Antigravity's
                    # mcp_config.json are machine-wide and serve every other
                    # C3 project, so removing ONE sub-project must not
                    # deregister C3 from the box.
                    _uninstall_mcp_all(str(abs_path), include_global=False)
                except Exception as e:
                    warnings.append(f"uninstall_mcp failed: {e}")
                c3_dir = abs_path / ".c3"
                if c3_dir.exists():
                    try:
                        shutil.rmtree(c3_dir)
                    except Exception as e:
                        warnings.append(f"rmtree .c3 failed: {e}")
                for filename, _ in _instruction_documents_for_project():
                    doc = abs_path / filename
                    if doc.exists():
                        try:
                            doc.unlink()
                        except Exception as e:
                            warnings.append(f"delete {filename} failed: {e}")
            except Exception as e:
                warnings.append(f"cleanup helpers unavailable: {e}")
            self.pm.remove_project(str(abs_path))

        # Un-nesting restores a subtree to the parent's index; unlinking an
        # external child leaves the parent's scan tree exactly as it was.
        reindex = {}
        if reindex_parent and was_nested:
            reindex = self._reindex_parent()

        result = {"removed": True, "mode": mode, "path": str(abs_path),
                  "name": entry.get("name"),
                  "link_kind": LINK_NESTED if was_nested else LINK_EXTERNAL}
        if reindex:
            result["parent_reindex"] = reindex
        if warnings:
            result["warnings"] = warnings
        return result

    # ── Consistency ────────────────────────────────────────────────

    def reconcile(self, fix: bool = False, prune: bool = False) -> dict:
        """Cross-check parent config / child back-links / registry.

        Parent config wins: with ``fix=True`` back-links and registry entries
        are repaired from it. Entries whose folder is gone are only dropped
        with ``prune=True``. Registry entries claiming this parent without a
        matching config entry get their ``parent_path`` cleared.
        """
        children = []
        fixed = []
        pruned = []
        parent_cfg = _read_config(self.parent_path)
        entries = [e for e in parent_cfg.get("subprojects", []) if isinstance(e, dict)]
        keep = []

        registry = self.pm._read_projects()
        for entry in entries:
            abs_path = self._abs_child(entry)
            status = self._entry_status(entry, registry)
            rec = {"name": entry.get("name"), "rel_path": entry.get("rel_path"),
                   "path": str(abs_path), "status": status,
                   "link_kind": entry_link_kind(entry)}
            if status == "missing_folder" and prune and fix:
                pruned.append(rec)
                continue
            if fix and status == "backlink_broken":
                child_cfg = _read_config(abs_path)
                back = {"name": self._parent_name(), "path": self.parent_path}
                back_rel = _rel_or_none(self.parent_path, abs_path)
                if back_rel is not None:
                    back["rel_path"] = back_rel
                child_cfg["parent"] = back
                _write_config(abs_path, child_cfg)
                fixed.append({**rec, "action": "backlink_rewritten"})
                status = self._entry_status(entry)
                rec["status"] = status
            if fix and status == "unregistered":
                self.pm.add_project(str(abs_path), name=entry.get("name"),
                                    parent_path=self.parent_path)
                fixed.append({**rec, "action": "registered"})
                rec["status"] = self._entry_status(entry)  # fresh read: registry just changed
            keep.append(entry)
            children.append(rec)

        if pruned and fix:
            parent_cfg["subprojects"] = keep
            if not keep:
                parent_cfg.pop("subprojects", None)
            _write_config(self.parent_path, parent_cfg)

        # Registry orphans: claim this parent but aren't in the config.
        orphans = []
        config_paths = {_norm(self._abs_child(e)) for e in keep}
        for p in self.pm._read_projects():
            if p.get("parent_path") and _same_path(p["parent_path"], self.parent_path):
                if _norm(p.get("path", "")) not in config_paths:
                    orphans.append(p.get("path"))
                    if fix:
                        self.pm.set_parent(p.get("path", ""), None)
                        fixed.append({"path": p.get("path"), "status": "orphan_registry",
                                      "action": "parent_cleared"})

        return {
            "parent": self.parent_path,
            "children": children,
            "orphans": orphans,
            "fixed": fixed,
            "pruned": pruned if fix else [],
            "ok": all(c["status"] == "ok" for c in children) and not orphans,
        }

    # ── Governance ─────────────────────────────────────────────────

    def cascade(self, op: str, include_parent: bool = False, mcp: bool = False,
                depth: int = MAX_DEPTH) -> dict:
        """Run ``op`` across the whole subtree (sequential), aggregating results.

        Descends to ``depth`` levels — ``depth=1`` is the pre-2.96 direct-children
        behaviour. ``descendants()`` de-duplicates, so a project reachable twice
        is still operated on once.
        """
        if op not in VALID_CASCADE_OPS:
            return {"op": op, "error": f"invalid op: {op} (use {'|'.join(VALID_CASCADE_OPS)})",
                    "results": [], "summary": {"total": 0, "ok": 0, "failed": 0}}

        targets = [(c["name"], c["path"], c["status"]) for c in self.descendants(depth)]
        if include_parent:
            targets.append((self._parent_name(), self.parent_path, "ok"))

        results = []
        for name, path, status in targets:
            t0 = time.time()
            row = {"name": name, "path": path, "ok": False}
            try:
                if status == "missing_folder":
                    row["error"] = "missing_folder"
                elif status == "missing_c3" and op != "update":
                    row["error"] = "missing_c3"
                elif op == "update":
                    from cli.c3 import _do_init
                    _do_init(path)
                    if mcp:
                        try:
                            from cli.c3 import _run_install_mcp
                            _run_install_mcp(path)
                        except Exception as e:
                            row["mcp_warning"] = str(e)
                    row["ok"] = True
                elif op == "reindex":
                    from services.doc_index import DocIndex
                    from services.indexer import CodeIndex
                    detail = {"code": CodeIndex(path).build_index()}
                    try:
                        detail["docs"] = DocIndex(path).build()
                    except Exception as e:
                        detail["doc_error"] = str(e)
                    row["detail"] = detail
                    row["ok"] = True
                elif op == "health":
                    from cli.c3 import _check_c3_health
                    info = _check_c3_health(path)
                    row["detail"] = info
                    row["ok"] = bool(info.get("healthy"))
                    if not row["ok"]:
                        row["error"] = "; ".join(info.get("issues", [])) or "unhealthy"
            except Exception as e:
                row["error"] = str(e)
            row["elapsed_ms"] = int((time.time() - t0) * 1000)
            results.append(row)

        ok_count = sum(1 for r in results if r["ok"])
        return {
            "op": op,
            "parent": self.parent_path,
            "include_parent": include_parent,
            "depth": depth,
            "results": results,
            "summary": {"total": len(results), "ok": ok_count,
                        "failed": len(results) - ok_count},
        }

    # ── Internal ───────────────────────────────────────────────────

    def _reindex_parent(self) -> dict:
        """Rebuild the parent's indexes so exclusions take effect."""
        detail = {}
        indexer = None
        try:
            from services.indexer import CodeIndex
            indexer = CodeIndex(self.parent_path)
            detail["code"] = indexer.build_index()
        except Exception as e:
            detail["code_error"] = str(e)
        try:
            from services.doc_index import DocIndex
            detail["docs"] = DocIndex(self.parent_path).build(force=True)
        except Exception as e:
            detail["doc_error"] = str(e)
        if indexer is not None:
            try:
                from core.config import load_hybrid_config
                from services.embedding_index import EmbeddingIndex
                from services.ollama_client import OllamaClient
                cfg = load_hybrid_config(self.parent_path)
                ollama = OllamaClient(cfg.get("ollama_base_url", "http://localhost:11434"))
                ei = EmbeddingIndex(self.parent_path, ollama,
                                    embed_model=cfg.get("embed_model", "nomic-embed-text"))
                # probe() initializes lazy backends; a fresh instance's
                # .ready is always False and skipped this unconditionally.
                if ei.probe()["ready"]:
                    detail["embeddings"] = ei.build(indexer, force=True)
            except Exception:
                pass  # embeddings are best-effort; index/doc exclusion is the contract
        return detail


# ── Path inspection ────────────────────────────────────────────────────────

def _count_lines(path: Path, cap: int = 100_000) -> int:
    """Bounded line count — big ledgers are common and reading one is enough."""
    if not path.exists():
        return 0
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, _ in enumerate(f, 1):
                if n >= cap:
                    break
    except OSError:
        return 0
    return n


def _project_summary(path: Path) -> dict:
    """Cheap identity + volume read of an initialized .c3 branch."""
    cfg = _read_config(path)
    meta = cfg.get("meta") or {}
    c3 = path / ".c3"
    try:
        sessions = sum(1 for _ in (c3 / "sessions").glob("*.json"))
    except OSError:
        sessions = 0
    return {
        "name": meta.get("name") or path.name,
        "description": meta.get("description") or "",
        "c3_version": cfg.get("version"),
        "ide": cfg.get("ide"),
        "facts_count": _facts_count(path),
        "notification_count": _notification_count(path),
        "sessions": sessions,
        "edit_ledger_entries": _count_lines(c3 / "edit_ledger.jsonl"),
        "has_index": _has_index(c3 / "index"),
    }


def _has_index(index_dir: Path) -> bool:
    """The SQLite store (2.107.0+) or a legacy index.json still to be migrated."""
    try:
        from services.index_store import IndexStore
        if IndexStore(index_dir).exists():
            return True
    except Exception:
        pass
    return (index_dir / "index.json").exists()


def _detect_nested(path: Path, linked: set) -> list:
    """Nested .c3 projects under ``path`` that are not linked to it.

    Suggestions only — nothing here is ever applied automatically. Explicit
    links are the source of truth; this is the prompt to create one.
    """
    from services.project_runtime import scan_for_c3

    # scan_for_c3 stops at the first .c3 it finds, so scanning ``path`` itself
    # would return only ``path`` when it is already a project. Scan one level in.
    if (path / ".c3").is_dir():
        try:
            roots = [str(d) for d in path.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            roots = []
    else:
        roots = [str(path)]
    out = []
    for found in scan_for_c3(roots):
        if _same_path(found, path) or _norm(found) in linked:
            continue
        out.append({"name": Path(found).name, "path": found, "has_c3": True, "linked": False})
    return out


def inspect_path(path, detect: bool = True) -> dict:
    """Read-only report on any path: is it a C3 project, and how is it linked?

    Mutates nothing. This is what the Hub calls before offering to link, so
    inspecting an unregistered folder must never register it.
    """
    out = {
        "path": "", "exists": False, "is_dir": False, "has_c3": False,
        "registered": False, "project": None, "parent": None, "ancestors": [],
        "depth": 0, "children": [], "detected": [], "linkable": False,
        "warnings": [],
    }
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        out["warnings"].append(f"unresolvable path: {e}")
        return out

    out["path"] = str(target)
    out["exists"] = target.exists()
    out["is_dir"] = target.is_dir()
    if not out["is_dir"]:
        out["warnings"].append("folder does not exist")
        return out

    out["has_c3"] = (target / ".c3").is_dir()
    if not out["has_c3"]:
        out["warnings"].append("no .c3 here — it will be initialized when linked")
        if detect:
            out["detected"] = _detect_nested(target, set())
        out["linkable"] = True
        return out

    out["project"] = _project_summary(target)
    out["registered"] = any(
        _same_path(p.get("path", ""), target)
        for p in ProjectManager()._read_projects()
    )
    if not out["registered"]:
        out["warnings"].append("has .c3 but is not in the registry — linking will register it")

    back = parent_link(target)
    if back:
        out["parent"] = {"name": back.get("name") or Path(back["path"]).name,
                         "path": back["path"]}
        out["warnings"].append(f"already a sub-project of {back['path']}")
    out["ancestors"] = ancestors(target)
    out["depth"] = len(out["ancestors"])

    children = SubprojectManager(str(target)).list()
    out["children"] = children
    if detect:
        out["detected"] = _detect_nested(target, {_norm(c["path"]) for c in children})

    # Linkable means "this is a thing a link can point at" — whether a specific
    # parent may claim it is validate()'s question, not this one.
    out["linkable"] = out["parent"] is None
    return out
