"""Sub-project designation and governance for C3 parent projects.

A *sub-project* is a sub-folder of a c3-initialized parent that carries its
own full ``.c3`` branch (index, memory, ledger, config) linked to the parent:

- parent ``.c3/config.json`` lists children under ``subprojects``
- child ``.c3/config.json`` back-links via ``parent``
- the global registry entry (``~/.c3/projects.json``) carries ``parent_path``

The parent's own index/doc-index/dictionary/watcher exclude designated
sub-project folders (see ``exclusion_prefixes``); cross-scope visibility is
restored by search/memory federation (``cli/tools/federate.py``).
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


# ── Index-exclusion helpers (hot path for indexer/doc_index/watcher) ──────

def exclusion_prefixes(project_path) -> list:
    """Normcased path-part tuples for designated sub-project folders.

    Fast path: ``[]`` when the project has no sub-projects, so scan loops
    pay nothing in the common case.
    """
    subs = get_subprojects(project_path)
    if not subs:
        return []
    prefixes = []
    for entry in subs:
        rel = entry.get("rel_path") or ""
        parts = tuple(os.path.normcase(p) for p in PurePosixPath(rel).parts)
        if parts:
            prefixes.append(parts)
    return prefixes


def is_excluded(rel_parts, prefixes) -> bool:
    """True when a project-relative path (as parts) sits under any prefix."""
    if not prefixes:
        return False
    normed = tuple(os.path.normcase(p) for p in rel_parts)
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
        try:
            return is_excluded(Path(fpath).relative_to(root).parts, prefixes)
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

    def _abs_child(self, rel_path: str) -> Path:
        return (Path(self.parent_path) / PurePosixPath(rel_path)).resolve()

    def _resolve_ref(self, ref: str) -> dict | None:
        """Find a parent-config entry by name, rel_path, or path."""
        entries = get_subprojects(self.parent_path)
        ref_l = (ref or "").strip().lower()
        for e in entries:
            if (e.get("name") or "").lower() == ref_l:
                return e
        for e in entries:
            if PurePosixPath(e.get("rel_path", "")).as_posix().lower() == PurePosixPath(ref.replace("\\", "/")).as_posix().lower():
                return e
        try:
            ref_abs = Path(self.parent_path, ref) if not Path(ref).is_absolute() else Path(ref)
            for e in entries:
                if _same_path(self._abs_child(e.get("rel_path", "")), ref_abs):
                    return e
        except OSError:
            pass
        return None

    def _entry_status(self, entry: dict, registry: list = None) -> str:
        abs_path = self._abs_child(entry.get("rel_path", ""))
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
        """Parent-config entries enriched with status and live counts."""
        out = []
        registry = self.pm._read_projects()
        for entry in get_subprojects(self.parent_path):
            abs_path = self._abs_child(entry.get("rel_path", ""))
            status = self._entry_status(entry, registry)
            out.append({
                "name": entry.get("name") or abs_path.name,
                "rel_path": entry.get("rel_path", ""),
                "path": str(abs_path),
                "added_at": entry.get("added_at"),
                "status": status,
                "facts_count": _facts_count(abs_path) if status not in ("missing_folder", "missing_c3") else 0,
                "notification_count": _notification_count(abs_path) if status != "missing_folder" else 0,
            })
        return out

    def tree(self) -> dict:
        """Hub-facing parent + children + rollup summary."""
        children = self.list()
        return {
            "parent": {"name": self._parent_name(), "path": self.parent_path},
            "children": children,
            "rollup": {
                "children": len(children),
                "notifications": sum(c["notification_count"] for c in children),
                "issues": sum(1 for c in children if c["status"] != "ok"),
            },
        }

    # ── Designation ────────────────────────────────────────────────

    def validate(self, folder: str) -> dict:
        """Pre-flight the add() validation chain without mutating anything."""
        out = {
            "ok": False, "exists": False, "is_dir": False, "has_c3": False,
            "registered": False, "already_child_of": None, "is_ancestor": False,
            "already_linked": False, "path": "", "warnings": [],
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

        if _same_path(target, self.parent_path) or is_within(self.parent_path, target):
            out["is_ancestor"] = True
            out["warnings"].append("folder is the parent itself or an ancestor of it")
        if not is_within(target, self.parent_path):
            if not out["is_ancestor"]:
                out["warnings"].append("folder is outside the parent project")
            return out

        if _read_config(self.parent_path).get("parent"):
            out["warnings"].append(
                "parent is itself a sub-project (nesting depth > 1 is not supported)"
            )
            return out

        out["has_c3"] = (target / ".c3").is_dir()
        for e in get_subprojects(self.parent_path):
            if _same_path(self._abs_child(e.get("rel_path", "")), target):
                out["already_linked"] = True
                if out["has_c3"]:
                    out["warnings"].append("already designated as a sub-project")
                    return out
                out["warnings"].append("already designated but .c3 is missing; re-add will re-init")
                break

        if out["has_c3"]:
            back = _read_config(target).get("parent") or {}
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
        rel_posix = PurePosixPath(*target.relative_to(Path(self.parent_path).resolve()).parts).as_posix()
        name = name or target.name
        adopted = v["has_c3"]
        warnings = []

        if run_init and not adopted:
            # Lazy import: services -> cli only at call time (precedent:
            # ProjectManager.merge_projects cleanup block).
            from cli.c3 import _do_init
            _do_init(str(target), ide_name=ide)

        # Child back-link.
        child_cfg = _read_config(target)
        child_cfg["parent"] = {
            "name": self._parent_name(),
            "path": self.parent_path,
            "rel_path": Path(os.path.relpath(self.parent_path, target)).as_posix(),
        }
        _write_config(target, child_cfg)

        # Parent config entry (replace stale duplicate if re-adding).
        parent_cfg = _read_config(self.parent_path)
        entries = [e for e in parent_cfg.get("subprojects", []) if isinstance(e, dict)]
        entries = [e for e in entries
                   if not _same_path(self._abs_child(e.get("rel_path", "")), target)]
        entries.append({"name": name, "rel_path": rel_posix, "added_at": _utcnow()})
        parent_cfg["subprojects"] = entries
        _write_config(self.parent_path, parent_cfg)

        # Global registry.
        self.pm.add_project(str(target), name=name, parent_path=self.parent_path)

        reindex = {}
        if reindex_parent:
            reindex = self._reindex_parent()

        result = {"added": True, "adopted": adopted, "name": name,
                  "rel_path": rel_posix, "path": str(target)}
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

        abs_path = self._abs_child(entry.get("rel_path", ""))
        warnings = []

        # Strip from parent config.
        parent_cfg = _read_config(self.parent_path)
        parent_cfg["subprojects"] = [
            e for e in parent_cfg.get("subprojects", [])
            if not (isinstance(e, dict)
                    and _same_path(self._abs_child(e.get("rel_path", "")), abs_path))
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
                    _uninstall_mcp_all(str(abs_path))
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

        reindex = {}
        if reindex_parent:
            reindex = self._reindex_parent()

        result = {"removed": True, "mode": mode, "path": str(abs_path),
                  "name": entry.get("name")}
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
            abs_path = self._abs_child(entry.get("rel_path", ""))
            status = self._entry_status(entry, registry)
            rec = {"name": entry.get("name"), "rel_path": entry.get("rel_path"),
                   "path": str(abs_path), "status": status}
            if status == "missing_folder" and prune and fix:
                pruned.append(rec)
                continue
            if fix and status == "backlink_broken":
                child_cfg = _read_config(abs_path)
                child_cfg["parent"] = {
                    "name": self._parent_name(),
                    "path": self.parent_path,
                    "rel_path": Path(os.path.relpath(self.parent_path, abs_path)).as_posix(),
                }
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
        config_paths = {_norm(self._abs_child(e.get("rel_path", ""))) for e in keep}
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

    def cascade(self, op: str, include_parent: bool = False, mcp: bool = False) -> dict:
        """Run ``op`` across all sub-projects (sequential), aggregating results."""
        if op not in VALID_CASCADE_OPS:
            return {"op": op, "error": f"invalid op: {op} (use {'|'.join(VALID_CASCADE_OPS)})",
                    "results": [], "summary": {"total": 0, "ok": 0, "failed": 0}}

        targets = [(c["name"], c["path"], c["status"]) for c in self.list()]
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
                if ei.ready:
                    detail["embeddings"] = ei.build(indexer, force=True)
            except Exception:
                pass  # embeddings are best-effort; index/doc exclusion is the contract
        return detail
