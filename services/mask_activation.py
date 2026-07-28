"""Mask Guard activation — the transactional purge/rebuild.

Implements docs/mask-guard.md §6. Adding a mask rule is not a config edit; it
is a transaction, because every derived artifact C3 already built from the raw
file is a channel that outlives the rule change::

    pending -> purge derived artifacts -> build + validate views
            -> rebuild indexes -> active

A rule is NOT active until the purge completes. ``status()`` reports the truth
so the UI and ``c3_status`` can say "masking pending" rather than implying a
protection that is not yet real.

The hardest channel is auto-memory: facts outlive every file cache. Facts
carry ``source_paths`` provenance from v2.63.0 onward; anything older has
provenance ``None`` and is purged wholesale on first activation, because a
fact that cannot be proven clean is not clean.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from services import access_guard, mask_mirror

_STATE_NAME = "state.json"
_SKIP_DIRS = {".git", ".c3", "node_modules", "__pycache__", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".idea",
              ".vscode", ".tox", ".ruff_cache"}
_MAX_SCAN_FILES = 200_000


# ── State ───────────────────────────────────────────────────────────────────

def _state_path(project_path) -> Path:
    return mask_mirror.mirror_root(project_path) / _STATE_NAME


def rules_digest(project_path) -> str:
    """Stable digest of the active mask policy — changes force re-activation."""
    rules, _corrupt = access_guard.load_mask_rules(project_path)
    payload = sorted(
        (r.scope, r.glob, r.preset,
         json.dumps(r.params_dict, sort_keys=True, separators=(",", ":")))
        for r in rules
    )
    blob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=12).hexdigest()


def _read_state(project_path) -> dict:
    path = _state_path(project_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(project_path, state: dict) -> None:
    path = _state_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(_STATE_NAME + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def status(project_path) -> dict:
    """Current activation state vs. the config on disk.

    ``stale`` True means the config has mask rules that have not been through
    a purge — protection is INCOMPLETE and callers must say so.
    """
    state = _read_state(project_path)
    current = rules_digest(project_path)
    rules, corrupt = access_guard.load_mask_rules(project_path)
    activated = state.get("rules_digest")
    return {
        "status": state.get("status", "none" if not rules else "pending"),
        "rules_digest": current,
        "activated_digest": activated,
        "stale": bool(rules) and activated != current,
        "rule_count": len(rules),
        "corrupt_scopes": corrupt,
        "activated_at": state.get("activated_at"),
        "last_report": state.get("last_report"),
    }


# ── Enumeration ─────────────────────────────────────────────────────────────

def masked_files(project_path) -> list:
    """Repo-relative POSIX paths that currently resolve to a mask verdict."""
    project = Path(project_path).resolve()
    rules, _corrupt = access_guard.load_mask_rules(project)
    if not rules:
        return []
    out, seen = [], 0
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            seen += 1
            if seen > _MAX_SCAN_FILES:
                return out
            full = Path(root) / name
            try:
                if access_guard.verdict(str(full), "read", str(project)).masked:
                    out.append(str(full.relative_to(project))
                               .replace("\\", "/"))
            except Exception:
                continue
    return sorted(out)


# ── Purge steps ─────────────────────────────────────────────────────────────

def _purge_compression_cache(project: Path) -> int:
    """Clear the whole cache: entries are keyed on content hash alone, so a
    rule change cannot invalidate them selectively (§6, row 1)."""
    cache = project / ".c3" / "cache"
    removed = 0
    if cache.is_dir():
        for entry in cache.iterdir():
            if entry.is_file():
                try:
                    entry.unlink()
                    removed += 1
                except Exception:
                    pass
    return removed


def _purge_index(project: Path) -> bool:
    """Drop the TF-IDF/chunk index so masked content cannot survive in it."""
    index = project / ".c3" / "index"
    if not index.is_dir():
        return False
    try:
        shutil.rmtree(index)
        return True
    except Exception:
        return False


def _purge_repo_map(project: Path) -> bool:
    """Delete MAP.md and mark it dirty — module one-liners are file-derived."""
    removed = False
    for name in ("MAP.md", "map.meta.json"):
        path = project / ".c3" / name
        if path.is_file():
            try:
                path.unlink()
                removed = True
            except Exception:
                pass
    try:
        from services.repo_map import mark_map_dirty
        mark_map_dirty(str(project), "mask activation")
    except Exception:
        pass
    return removed


def _purge_file_memory(project: Path, rel_paths: list) -> int:
    try:
        from services.file_memory import FileMemoryStore
    except Exception:
        return 0
    try:
        store = FileMemoryStore(str(project))
    except Exception:
        return 0
    dropped = 0
    for rel in rel_paths:
        try:
            if store.drop(rel):
                dropped += 1
        except Exception:
            pass
    return dropped


def _purge_facts(project: Path, rel_paths: list, memory_store,
                 include_unknown: bool) -> dict:
    if memory_store is None:
        try:
            from services.memory import MemoryStore
            memory_store = MemoryStore(str(project))
        except Exception:
            return {"purged": 0, "unknown_purged": 0, "error": "unavailable"}
    try:
        return memory_store.purge_by_source(
            rel_paths, include_unknown=include_unknown)
    except Exception as exc:
        return {"purged": 0, "unknown_purged": 0, "error": str(exc)}


# ── The transaction ─────────────────────────────────────────────────────────

def activate(project_path, *, memory_store=None, rebuild_index=False) -> dict:
    """Run the full activation transaction. Returns a report.

    ``ok`` False means the rules are on disk but protection is INCOMPLETE —
    at least one view failed to render, so those paths currently refuse reads
    (fail-closed) instead of serving a view. Callers must surface that.
    """
    project = Path(project_path).resolve()
    started = int(time.time())
    digest = rules_digest(project)
    prior = _read_state(project)
    first_time = not prior.get("activated_at")

    _write_state(project, {**prior, "status": "pending",
                           "pending_digest": digest,
                           "pending_since": started})

    targets = masked_files(project)

    report = {
        "files": len(targets),
        "cache_entries_removed": _purge_compression_cache(project),
        "index_dropped": _purge_index(project),
        "repo_map_dropped": _purge_repo_map(project),
        "file_memory_dropped": _purge_file_memory(project, targets),
        "mirror_cleared": mask_mirror.clear(project),
        "first_activation": first_time,
    }
    report["facts"] = _purge_facts(project, targets, memory_store,
                                   include_unknown=first_time)

    built, failures = 0, []
    for rel in targets:
        try:
            v = access_guard.verdict(str(project / rel), "read", str(project))
            if not v.masked:
                continue
            mask_mirror.build_view(rel, v.mask_rule, project)
            built += 1
        except mask_mirror.MaskUnavailable as exc:
            failures.append({"path": rel, "reason": exc.reason,
                             "detail": exc.message})
        except Exception as exc:
            failures.append({"path": rel, "reason": "error",
                             "detail": f"{type(exc).__name__}: {exc}"})
    report["views_built"] = built
    report["failures"] = failures

    if rebuild_index:
        try:
            from services.indexer import CodeIndex
            report["reindexed"] = bool(CodeIndex(str(project)).build_index())
        except Exception as exc:
            report["reindexed"] = False
            report["reindex_error"] = f"{type(exc).__name__}: {exc}"

    ok = not failures
    _write_state(project, {
        "status": "active" if ok else "incomplete",
        "rules_digest": digest,
        "activated_at": int(time.time()),
        "last_report": report,
    })
    report["ok"] = ok
    report["status"] = "active" if ok else "incomplete"
    return report


def summary_line(project_path) -> str:
    """One line for c3_status / the UI banner. '' when no mask rules exist."""
    st = status(project_path)
    if not st["rule_count"] and st["status"] == "none":
        return ""
    if st["corrupt_scopes"]:
        return (f"{access_guard.TAG_MASK_UNSUPPORTED} mask config is invalid "
                f"in scope(s) {', '.join(st['corrupt_scopes'])} — those paths "
                "fail closed until repaired.")
    if st["stale"] or st["status"] == "pending":
        return (f"{access_guard.TAG_MASK_LIMITED} {st['rule_count']} mask "
                "rule(s) are configured but NOT activated — derived artifacts "
                "(index, caches, memory) may still hold pre-mask content. "
                "Run `c3 access mask activate`.")
    if st["status"] == "incomplete":
        failed = len((st.get("last_report") or {}).get("failures") or [])
        return (f"{access_guard.TAG_MASK_UNSUPPORTED} masking is INCOMPLETE: "
                f"{failed} path(s) could not be rendered and now refuse reads. "
                "See `c3 access mask status`.")
    return (f"{access_guard.TAG_MASK_LIMITED} {st['rule_count']} mask rule(s) "
            "active — some content is served as policy-transformed views.")
