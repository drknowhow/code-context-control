"""C3Bridge — gives Oracle full read-only access to C3 tool handlers.

Runtimes come from a private ``ProjectRuntimeCache`` (the shared LRU that was
originally lifted from this module) with a vector-warm ``on_build`` hook; each
handler is wrapped with project_path dispatch + read-only enforcement.
"""

import asyncio
import logging
import threading
from pathlib import Path

from services.project_runtime import ProjectRuntimeCache
from services.runtime import C3Runtime

log = logging.getLogger("oracle.c3_bridge")

# Actions that are blocked in Oracle context (read-only).
_BLOCKED_EDITS_ACTIONS = {"log"}
_BLOCKED_MEMORY_ACTIONS = {"add", "update", "delete", "consolidate", "consolidate_deep", "ground"}
_BLOCKED_STATUS_VIEWS = {"ghost_files"}


def _noop_finalize(_name: str, _args: dict, resp: str, _summ: str = "", **_kw) -> str:
    """No-op finalize — Oracle doesn't track MCP budgets."""
    return resp


def _noop_facts(*_a, **_kw) -> str:
    """Stub for maybe_facts callback (currently disabled in C3 too)."""
    return ""


def validate_project_path(scanner, project_path: str) -> str:
    """Resolve ``project_path`` and confirm it is a discovered C3 project.

    Returns the resolved absolute path. Raises ``ValueError`` if the path is
    empty or is not a member of ``scanner.discover()`` — preventing any caller
    (chat tools, Discovery API) from reading an arbitrary ``.c3`` project on the
    machine simply by supplying its path.
    """
    if not project_path:
        raise ValueError("project_path is required")
    resolved = str(Path(project_path).resolve())
    try:
        discovered = scanner.discover() if scanner else []
    except Exception:
        discovered = []
    known = {str(Path(p.get("path", "")).resolve()) for p in discovered if p.get("path")}
    if resolved not in known:
        raise ValueError(
            f"Unknown project: {project_path}. Not a registered C3 project."
        )
    return resolved


class C3Bridge:
    """Bridge between Oracle and C3 tool handlers with per-project runtime cache."""

    def __init__(self, scanner):
        self.scanner = scanner
        # Private instance, NOT shared_cache(): Oracle owns its runtimes'
        # lifecycle via the atexit-registered shutdown(), and the vector-warm
        # on_build hook shouldn't leak to other consumers. Size defaults to 8
        # (env-tunable C3_RUNTIME_CACHE_SIZE) — the old hand-rolled LRU held 3,
        # which thrashed cross-project search over more than 3 projects.
        self._cache = ProjectRuntimeCache(
            ide_name="claude-code", on_build=self._warm_runtime
        )

    # ── Runtime cache ──────────────────────────────────────────────

    def get_runtime(self, project_path: str) -> C3Runtime:
        """Return a cached runtime or build one (LRU, shared implementation).

        ``project_path`` is validated against discovered projects first, so a
        runtime can never be built for an arbitrary path on the machine.
        """
        project_path = validate_project_path(self.scanner, project_path)
        return self._cache.get(project_path)

    def _warm_runtime(self, runtime: C3Runtime) -> None:
        """Warm heavy vector backends off the request thread.

        Mirrors the MCP server's lifespan warm (embedding build + SLTM warm)
        so the first c3_search/c3_memory_query on a project doesn't pay the
        chromadb/embedding init inline.
        """
        def _warm():
            if getattr(runtime, "embedding_index", None):
                try:
                    runtime.embedding_index.build(runtime.indexer)
                except Exception as e:
                    log.debug("embedding warm failed: %s", e)
            if getattr(runtime, "vector_store", None):
                try:
                    runtime.vector_store.warm()
                except Exception as e:
                    log.debug("vector warm failed: %s", e)

        threading.Thread(target=_warm, daemon=True, name="oracle-vector-warm").start()

    def shutdown(self):
        """Stop all cached runtimes."""
        self._cache.shutdown()

    # ── Helpers ────────────────────────────────────────────────────

    def _discover_c3_projects(self) -> list[dict]:
        """Return projects that have a .c3 directory."""
        projects = self.scanner.discover()
        return [p for p in projects if p.get("has_c3")]

    # ── Per-project tool wrappers ─────────────────────────────────

    def c3_search(self, project_path: str, query: str, action: str = "code",
                  top_k: int = 3, max_tokens: int = 1200) -> dict:
        from cli.tools.search import handle_search
        svc = self.get_runtime(project_path)
        result = handle_search(query, action, top_k, max_tokens,
                               svc, _noop_finalize, _noop_facts)
        return {"project": project_path, "result": result}

    def c3_read(self, project_path: str, file_path: str,
                symbols=None, lines=None) -> dict:
        from cli.tools.read import handle_read
        svc = self.get_runtime(project_path)
        result = handle_read(file_path, symbols=symbols, lines=lines, svc=svc,
                             finalize=_noop_finalize)
        return {"project": project_path, "result": result}

    def c3_edits(self, project_path: str, action: str = "history",
                 file: str = "", change_type: str = "", summary: str = "",
                 lines_changed: str = "", tags: str = "", limit: int = 50,
                 since: str = "", edit_id: str = "", tag: str = "") -> dict:
        if action in _BLOCKED_EDITS_ACTIONS:
            return {"error": f"Action '{action}' is write-only and blocked in Oracle. "
                             "Use suggest_action for write operations."}
        from cli.tools.edits import handle_edits
        svc = self.get_runtime(project_path)
        result = handle_edits(action, file, change_type, summary,
                              lines_changed, tags, limit, since,
                              edit_id, tag, svc, _noop_finalize)
        return {"project": project_path, "result": result}

    def c3_memory(self, project_path: str, action: str = "query",
                  query: str = "", fact: str = "", category: str = "",
                  top_k: int = 10, fact_id: str = "") -> dict:
        if action in _BLOCKED_MEMORY_ACTIONS:
            return {"error": f"Action '{action}' is blocked in Oracle (read-only). "
                             "Use suggest_action to propose memory changes."}
        from cli.tools.memory import handle_memory
        svc = self.get_runtime(project_path)
        result = handle_memory(action, query, fact, category, top_k,
                               svc, _noop_finalize, fact_id=fact_id)
        return {"project": project_path, "result": result}

    def c3_compress(self, project_path: str, file_path: str,
                    mode: str = "map") -> dict:
        from cli.tools.compress import handle_compress
        svc = self.get_runtime(project_path)
        result = handle_compress(file_path, mode, svc, _noop_finalize, _noop_facts)
        return {"project": project_path, "result": result}

    def c3_validate(self, project_path: str, file_path: str) -> dict:
        from cli.tools.validate import handle_validate
        svc = self.get_runtime(project_path)
        result = asyncio.run(handle_validate(file_path, svc, _noop_finalize))
        return {"project": project_path, "result": result}

    def c3_status(self, project_path: str, view: str = "health",
                  detailed: bool = False) -> dict:
        if view in _BLOCKED_STATUS_VIEWS:
            return {"error": f"View '{view}' has side effects and is blocked in Oracle."}
        from cli.tools.status import handle_status
        svc = self.get_runtime(project_path)
        result = handle_status(view, detailed, svc, _noop_finalize)
        return {"project": project_path, "result": result}

    def c3_filter(self, project_path: str, file_path: str = "", text: str = "",
                  pattern: str = "", max_lines: int = 100,
                  depth: str = "smart") -> dict:
        from cli.tools.filter import handle_filter
        svc = self.get_runtime(project_path)
        result = handle_filter(file_path, text, pattern, max_lines,
                               depth, False, svc, _noop_finalize)
        return {"project": project_path, "result": result}

    # ── Cross-project aggregation ─────────────────────────────────

    def c3_search_cross(self, query: str, action: str = "code",
                        top_k: int = 3) -> dict:
        """Search code across ALL registered projects."""
        projects = self._discover_c3_projects()
        results = []
        for proj in projects:
            path = proj.get("path", "")
            if not path:
                continue
            try:
                r = self.c3_search(path, query, action, top_k)
                results.append({"project": path, "result": r.get("result", "")})
            except Exception as e:
                results.append({"project": path, "error": str(e)})
        return {"projects_queried": len(results), "results": results}

    def c3_edits_cross(self, action: str = "history", tag: str = "",
                       limit: int = 20) -> dict:
        """Query edit ledgers across ALL registered projects."""
        if action in _BLOCKED_EDITS_ACTIONS:
            return {"error": f"Action '{action}' is write-only and blocked in Oracle."}
        projects = self._discover_c3_projects()
        results = []
        for proj in projects:
            path = proj.get("path", "")
            if not path:
                continue
            try:
                r = self.c3_edits(path, action=action, tag=tag, limit=limit)
                results.append({"project": path, "result": r.get("result", "")})
            except Exception as e:
                results.append({"project": path, "error": str(e)})
        return {"projects_queried": len(results), "results": results}
