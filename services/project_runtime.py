"""Shared cross-project runtime cache, resolution, and discovery.

Lets C3 tools operate on *other* c3-installed projects without launching a
separate MCP server per project. A full ``C3Runtime`` is built and cached per
project path (LRU) via ``services.runtime.build_runtime`` -- the exact builder
the MCP server and web server already use.

This is the shared home for the per-project runtime cache; the Oracle's
``C3Bridge`` predates it and keeps its own cache for now.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from services.runtime import C3Runtime, build_runtime, stop_runtime

# Global registry written by ProjectManager (~/.c3/projects.json).
_GLOBAL_C3_DIR = Path.home() / ".c3"
_PROJECTS_FILE = _GLOBAL_C3_DIR / "projects.json"

# Filesystem scan tuning -- bounded so a sweep over large roots stays cheap.
_SCAN_MAX_DEPTH = 4
_SCAN_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".c3", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox",
    "dist", "build", "site-packages", ".idea", ".vscode", ".next",
    "target", ".gradle", ".cargo", "vendor",
}


# ── Runtime cache ──────────────────────────────────────────────────────────


class ProjectRuntimeCache:
    """Thread-safe LRU cache of ``C3Runtime`` objects keyed by resolved path.

    Lifted from ``oracle.services.c3_bridge.C3Bridge.get_runtime`` so the MCP
    server and any other caller share one implementation.
    """

    def __init__(self, ide_name: str = "claude-code", max_cached: int = 4):
        self._ide_name = ide_name
        self._max = max(1, int(max_cached))
        self._runtimes: dict[str, C3Runtime] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def get(self, project_path: str) -> C3Runtime:
        """Return a cached runtime or build one. Raises ValueError if invalid."""
        project_path = str(Path(project_path).resolve())
        with self._lock:
            rt = self._runtimes.get(project_path)
            if rt is not None:
                self._touch_locked(project_path)
                return rt

        # Validate outside the lock (don't hold it across disk checks / build).
        p = Path(project_path)
        if not p.exists():
            raise ValueError(f"Project path does not exist: {project_path}")
        if not (p / ".c3").is_dir():
            raise ValueError(
                f"No .c3 directory in {project_path}. Run 'c3 init' there first."
            )

        runtime = build_runtime(project_path, ide_name=self._ide_name)

        with self._lock:
            # Another thread may have built it while we were unlocked.
            existing = self._runtimes.get(project_path)
            if existing is not None:
                stop_runtime(runtime)
                self._touch_locked(project_path)
                return existing
            self._runtimes[project_path] = runtime
            self._order.append(project_path)
            while len(self._order) > self._max:
                evict = self._order.pop(0)
                old = self._runtimes.pop(evict, None)
                if old is not None:
                    stop_runtime(old)
        return runtime

    def _touch_locked(self, path: str) -> None:
        if path in self._order:
            self._order.remove(path)
        self._order.append(path)

    def shutdown(self) -> None:
        with self._lock:
            for rt in self._runtimes.values():
                stop_runtime(rt)
            self._runtimes.clear()
            self._order.clear()


_shared_cache: Optional[ProjectRuntimeCache] = None
_shared_lock = threading.Lock()


def shared_cache() -> ProjectRuntimeCache:
    """Return the process-wide foreign-runtime cache (lazily created)."""
    global _shared_cache
    if _shared_cache is None:
        with _shared_lock:
            if _shared_cache is None:
                _shared_cache = ProjectRuntimeCache()
    return _shared_cache


# ── Registry access ────────────────────────────────────────────────────────


def _read_registry() -> list[dict]:
    try:
        if _PROJECTS_FILE.exists():
            with open(_PROJECTS_FILE, encoding="utf-8") as f:
                return json.load(f).get("projects", [])
    except Exception:
        pass
    return []


def _norm(name: str) -> str:
    """Case/space/punctuation-insensitive key for fuzzy name matching."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _resolved(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except Exception:
        return path or ""


# ── Resolution ─────────────────────────────────────────────────────────────


def resolve_project(ref: str) -> dict:
    """Resolve a project *name or path* to ``{"name", "path"}``.

    Resolution order: a real path containing ``.c3`` > exact registry path >
    exact registry name > unique substring name match. Raises ``ValueError``
    with an actionable message when the reference is unknown or ambiguous.
    """
    if not ref or not ref.strip():
        raise ValueError("No project specified -- pass a registered name or a path.")
    ref = ref.strip()
    registry = _read_registry()

    # 1. A path on disk that is itself a C3 project (registered or not).
    candidate = Path(ref).expanduser()
    if candidate.exists() and (candidate / ".c3").is_dir():
        resolved = str(candidate.resolve())
        match = next(
            (p for p in registry if _resolved(p.get("path", "")) == resolved), None
        )
        return {
            "name": (match or {}).get("name") or candidate.name,
            "path": resolved,
        }

    # 2. Exact path match in the registry (path may be stale on disk).
    ref_resolved = _resolved(ref)
    for p in registry:
        if _resolved(p.get("path", "")) == ref_resolved:
            return {
                "name": p.get("name") or Path(p["path"]).name,
                "path": _resolved(p["path"]),
            }

    # 3. Name match: exact (normalized) first, then unique substring.
    nref = _norm(ref)
    exact = [p for p in registry if _norm(p.get("name", "")) == nref]
    if len(exact) == 1:
        return {"name": exact[0]["name"], "path": _resolved(exact[0]["path"])}
    if len(exact) > 1:
        raise ValueError(
            f"Ambiguous project name '{ref}' -- {len(exact)} registered projects "
            "share it. Pass an absolute path instead."
        )

    partial = [p for p in registry if nref and nref in _norm(p.get("name", ""))]
    if len(partial) == 1:
        return {"name": partial[0]["name"], "path": _resolved(partial[0]["path"])}
    if len(partial) > 1:
        names = ", ".join(p.get("name", "?") for p in partial[:8])
        raise ValueError(
            f"Ambiguous project '{ref}' -- matches: {names}. "
            "Be more specific or pass a path."
        )

    known = ", ".join(p.get("name", "?") for p in registry[:12]) or "(none registered)"
    raise ValueError(
        f"Unknown project '{ref}'. Registered: {known}. "
        "Or pass an absolute path to a folder that contains a .c3 directory."
    )


# ── Discovery (registry + filesystem scan) ─────────────────────────────────


def scan_for_c3(roots: list[str], max_depth: int = _SCAN_MAX_DEPTH) -> list[str]:
    """Walk ``roots`` (bounded depth) and return folders containing a ``.c3`` dir.

    Once a project is found its subtree is not descended -- nested ``.c3`` dirs
    inside a project are ignored.
    """
    found: list[str] = []
    seen: set[str] = set()
    for root in roots or []:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        base_parts = len(base.resolve().parts)
        for dirpath, dirnames, _files in os.walk(base):
            here = Path(dirpath)
            depth = len(here.resolve().parts) - base_parts
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS]
            if (here / ".c3").is_dir():
                rp = str(here.resolve())
                if rp not in seen:
                    seen.add(rp)
                    found.append(rp)
                dirnames[:] = []  # don't descend into a project's children
    return found


def default_scan_roots() -> list[str]:
    """Parent directories of registered projects -- a cheap, useful default.

    Scanning the parents of known projects surfaces sibling projects without
    walking the whole filesystem.
    """
    roots: list[str] = []
    seen: set[str] = set()
    for p in _read_registry():
        path = p.get("path", "")
        if not path:
            continue
        parent = str(Path(path).expanduser().resolve().parent)
        if parent and parent not in seen:
            seen.add(parent)
            roots.append(parent)
    return roots


def discover_projects(
    scan_roots: Optional[list[str]] = None, scan: bool = True
) -> dict:
    """Return ``{"registered": [...], "unregistered": [...]}``.

    ``registered`` comes from the global registry; ``unregistered`` are ``.c3``
    projects found by scanning that are not in the registry yet.
    """
    registry = _read_registry()
    reg_paths = {_resolved(p.get("path", "")) for p in registry if p.get("path")}

    registered = [
        {
            "name": p.get("name") or Path(p.get("path", "")).name,
            "path": _resolved(p.get("path", "")),
            "ide": p.get("ide", "unknown"),
            "registered": True,
            "accessible": Path(p.get("path", "")).is_dir() if p.get("path") else False,
        }
        for p in registry
    ]

    unregistered: list[dict] = []
    if scan:
        roots = scan_roots if scan_roots is not None else default_scan_roots()
        for path in scan_for_c3(roots):
            if path not in reg_paths:
                unregistered.append(
                    {
                        "name": Path(path).name,
                        "path": path,
                        "ide": "unknown",
                        "registered": False,
                        "accessible": True,
                    }
                )
    return {"registered": registered, "unregistered": unregistered}
