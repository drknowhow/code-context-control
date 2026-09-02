#!/usr/bin/env python3
"""
C3 MCP Server - Claude Code Companion as a native MCP tool server.

Exposes 18 C3 tools as MCP endpoints. Tool logic lives in cli/tools/.

Usage:
    python cli/mcp_server.py --project <path>
"""
import argparse
import asyncio
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp import Context, FastMCP

from core.ide import get_profile, load_ide_config
from services.auto_memory import AutoMemory
from services.context_snapshot import ContextSnapshot
from services.runtime import C3Runtime, build_runtime, start_runtime, stop_runtime
from services.transcript_index import TranscriptIndex


# Read version without importing cli.c3 (heavy side effects)
def _read_version() -> str:
    try:
        _c3_py = Path(__file__).parent / "c3.py"
        for line in _c3_py.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split('"')[1]
    except Exception:
        pass
    return "?"

C3_VERSION = _read_version()

# Tool handlers
from cli.tools._helpers import maybe_related_facts, validate_file_path
from cli.tools.agent import handle_agent
from cli.tools.compress import handle_compress
from cli.tools.delegate import handle_delegate
from cli.tools.edit import handle_edit
from cli.tools.filter import handle_filter
from cli.tools.memory import handle_memory
from cli.tools.read import handle_read
from cli.tools.search import handle_search
from cli.tools.session import handle_session
from cli.tools.shell import handle_shell
from cli.tools.status import handle_status
from cli.tools.validate import handle_validate


def _get_project_path() -> str:
    """Parse --project from sys.argv (before FastMCP takes over)."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project", default=".")
    args, _ = parser.parse_known_args()
    return str(Path(args.project).resolve())


PROJECT_PATH = _get_project_path()
_IDE_NAME = load_ide_config(PROJECT_PATH)
_IDE_PROFILE = get_profile(_IDE_NAME)


def _build_instructions(ide_name: str) -> str:
    """Build compact MCP instructions. Optimized for minimal token overhead.

    Structure: intent-keyed one-liners so Claude can route by goal, not
    by tool-name memorization. Each line = one intent → one tool.
    """
    return (
        "C3 — local code intelligence. Route by goal:\n"
        "  FIND candidates → c3_search\n"
        "  MAP file shape → c3_compress(mode='map') then READ content → c3_read(symbols=…)\n"
        "  EDIT code → c3_edit (always; it logs to the ledger automatically)\n"
        "  VALIDATE after every edit → c3_validate\n"
        "  BLAST RADIUS before shared-symbol edits → c3_impact\n"
        "  DISTILL terminal/log output >10 lines → c3_filter\n"
        "  EXECUTE shell (tests/git/build) → c3_shell\n"
        "  RECALL cross-session knowledge → c3_memory(action='recall') (index+fetch for large stores)\n"
        "  TRACK durable tasks/milestones/decisions + time → c3_task\n"
        "  AGENT CONFIG inventory/history/diff/restore → c3_artifacts\n"
        "  SNAPSHOT before /clear → c3_session(action='snapshot')\n"
        "  HEALTH/budget checks → c3_status\n"
        "  OFFLOAD to another model → c3_delegate\n"
        "PLAN MODE: all read-only actions above are safe."
    )


@asynccontextmanager
async def lifespan(server):
    """Initialize all services, auto-start session, start file watcher."""
    project = PROJECT_PATH
    services = build_runtime(project, ide_name=_IDE_NAME)
    if getattr(services, "time_tracker", None) is not None:
        try:
            # The IDE loading the c3 MCP server marks the start of a work
            # session for this project.
            services.time_tracker.ping("startup")
        except Exception:
            pass
    transcript_index = TranscriptIndex(project)
    services.transcript_index = transcript_index
    snapshots = services.snapshots or ContextSnapshot(project)
    services.snapshots = snapshots

    if _IDE_PROFILE.supports_transcripts:
        if not (Path(project) / ".c3" / "transcript_index" / "index.json").exists():
            transcript_index.build_index()
        else:
            transcript_index._load_index()
            transcript_index._load_manifest()

    if not (Path(project) / ".c3" / "index" / "index.json").exists():
        import threading

        def _bg_build():
            try:
                services.indexer.build_index()
            except Exception:
                pass
            # After code index is built, build embedding index. build() lazily
            # inits its chromadb/ollama backends, kept off the handshake path.
            if services.embedding_index:
                try:
                    services.embedding_index.build(services.indexer)
                except Exception:
                    pass
            # Warm SLTM vector store so the first memory call isn't slow.
            if services.vector_store:
                try:
                    services.vector_store.warm()
                except Exception:
                    pass
            # Build doc index for Local RAG Pipeline
            if services.doc_index:
                try:
                    services.doc_index.build()
                except Exception:
                    pass

        threading.Thread(target=_bg_build, daemon=True, name="c3-initial-index").start()
    else:
        services.indexer._load_index()
        # Build/update embedding index + warm SLTM in background. Deferred off
        # the handshake path: build()/warm() lazily init the heavy backends, so
        # this must NOT gate on .ready synchronously here.
        if services.embedding_index or services.vector_store:
            import threading

            def _bg_embed():
                if services.embedding_index:
                    try:
                        services.embedding_index.build(services.indexer)
                    except Exception:
                        pass
                if services.vector_store:
                    try:
                        services.vector_store.warm()
                    except Exception:
                        pass

            threading.Thread(target=_bg_embed, daemon=True, name="c3-embed-index").start()

        # Build/update doc index in background for Local RAG Pipeline
        if services.doc_index:
            import threading

            def _bg_doc_index():
                try:
                    services.doc_index.build()
                except Exception:
                    pass

            threading.Thread(target=_bg_doc_index, daemon=True, name="c3-doc-index").start()

    started_session = services.session_mgr.start_session("MCP server session", source_system=_IDE_NAME)
    start_runtime(services)

    convo_store = services.convo_store
    services.convo_store = convo_store
    if _IDE_PROFILE.supports_transcripts:
        try:
            convo_store.sync(source="claude")
            if services.retrieval:
                services.retrieval.mark_sessions_dirty()
        except Exception:
            pass

    import threading
    _convo_sync_stop = threading.Event()
    if _IDE_PROFILE.supports_transcripts:
        def _bg_convo_sync():
            while not _convo_sync_stop.wait(timeout=60):
                try:
                    convo_store.sync(source="claude")
                    if services.retrieval:
                        services.retrieval.mark_sessions_dirty()
                except Exception:
                    pass
        threading.Thread(target=_bg_convo_sync, daemon=True, name="c3-convo-sync").start()

    # Auto-memory: background learning from tool calls.
    auto_mem_cfg = (services.hybrid_config or {}).get("auto_memory", {})
    services.auto_memory = AutoMemory(services.memory, services.session_mgr, auto_mem_cfg)

    if services.session_mgr.current_session:
        services.activity_log.log("session_start", {
            "session_id": services.session_mgr.current_session["id"],
            "source_system": started_session.get("source_system", ""),
        })

    # Auto-restore latest snapshot if recent (< 30 min).
    # Deferred to background thread so first tool call isn't blocked.
    # Skipped in benchmark mode to prevent snapshot budget from carrying over between tasks.
    if not os.environ.get("C3_BENCHMARK_MODE"):
        import threading as _restore_t

        def _bg_auto_restore():
            try:
                latest = snapshots._load_snapshot("latest")
                if "error" not in latest and "created" in latest:
                    created_dt = datetime.fromisoformat(latest["created"])
                    age_sec = (datetime.now(timezone.utc) - created_dt).total_seconds()
                    if age_sec < 1800:
                        res = snapshots.restore("latest", memory_store=services.memory, level=1)
                        if "error" not in res:
                            services.session_mgr.reset_budget(initial_tokens=res.get("tokens", 0))
                            services.notifications.add(
                                agent="c3",
                                severity="info",
                                title="Session Auto-Restored",
                                message=f"Restored latest context from {round(age_sec/60)}m ago: {res['briefing']}"
                            )
                            services.activity_log.log("auto_restore", {
                                "snapshot_id": res["snapshot_id"], "age_min": round(age_sec/60)})
            except Exception:
                pass

        _restore_t.Thread(target=_bg_auto_restore, daemon=True, name="c3-auto-restore").start()

    # Pre-warm delegate health checks so first c3_agent call skips 3-4s preflight.
    import threading as _t

    def _bg_delegate_prewarm():
        try:
            from cli.tools.delegate import check_codex, check_gemini
            check_gemini()
            check_codex()
        except Exception:
            pass

    _t.Thread(target=_bg_delegate_prewarm, daemon=True, name="c3-delegate-prewarm").start()

    # Background validation sweep: check recently-errored files and notify.
    if services.validation_cache:
        import threading as _t

        def _bg_validation_sweep():
            import time
            time.sleep(8)  # Let watcher populate cache from initial file events.
            errors = services.validation_cache.get_errors()
            if errors:
                names = ", ".join(e["path"] for e in errors[:5])
                more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
                services.notifications.add(
                    agent="c3", severity="warning",
                    title="Syntax Errors Detected",
                    message=f"{len(errors)} file(s) have syntax errors: {names}{more}",
                )
        _t.Thread(target=_bg_validation_sweep, daemon=True, name="c3-validate-sweep").start()

    try:
        yield services
    finally:
        _convo_sync_stop.set()
        # Auto-memory: extract remaining learnings and generate session summary.
        if hasattr(services, "auto_memory"):
            try:
                services.auto_memory.on_session_end()
            except Exception:
                pass
        # Memory distillation: durably enqueue a digest job while the session
        # is still current. Processing happens at the END of this finally
        # block (after save_session + convo sync) so the inline pass sees the
        # richest material; if the process dies first, the job file survives
        # and the next session's MemoryDistillerAgent drains it.
        _distill_job = None
        if getattr(services, "memory_distiller", None):
            try:
                _distill_job = services.memory_distiller.enqueue_session(
                    services.session_mgr.current_session)
            except Exception:
                _distill_job = None
        # Memory consolidation: triage + prune at session end (lightweight).
        if services.memory_consolidator:
            try:
                session = services.session_mgr.current_session
                services.memory_consolidator.phase_triage(session)
                services.memory_consolidator.phase_prune()
            except Exception:
                pass
        stop_runtime(services)
        services.session_mgr._persist_budget()
        services.session_mgr.save_session()
        if _IDE_PROFILE.supports_transcripts:
            try:
                convo_store.sync(source="claude", force=True)
                if services.retrieval:
                    services.retrieval.mark_sessions_dirty()
            except Exception:
                pass
        # Best-effort inline distillation. A daemon thread dies with the
        # interpreter without blocking shutdown — the queued job is the
        # durable fallback either way.
        if _distill_job and getattr(services, "memory_distiller", None):
            try:
                threading.Thread(
                    target=services.memory_distiller.process_job_safe,
                    args=(_distill_job,), daemon=True,
                    name="c3-memory-distill").start()
            except Exception:
                pass


mcp = FastMCP(f"C3 v{C3_VERSION}", instructions=_build_instructions(_IDE_NAME), lifespan=lifespan)

# ─── Helper Functions ─────────────────────────────────────────────

_repo_map_ensured = False


def _ensure_repo_map_once(rt) -> None:
    """First tool call of this server process kicks a background repo-map
    freshness pass. In-process single-flight here; cross-process single-flight
    via the lock file inside RepoMapService.ensure(). Never blocks the tool
    call that triggered it."""
    global _repo_map_ensured
    if _repo_map_ensured:
        return
    _repo_map_ensured = True

    def _bg():
        try:
            from services.repo_map import RepoMapService
            RepoMapService(rt.project_path,
                           session_mgr=getattr(rt, "session_mgr", None)).ensure()
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True, name="repo-map-ensure").start()


def _svc(ctx: Context) -> C3Runtime:
    rt = ctx.request_context.lifespan_context
    _ensure_repo_map_once(rt)
    return rt


_last_tool_call_time: float = 0.0
_last_badge_count: int = 0  # delta-based: only show badge when count increases
_finalize_lock = threading.Lock()


def _finalize_response(ctx: Context, tool_name: str, args: dict,
                       response: str, summary: str = "",
                       response_tokens: int = 0) -> str:
    global _last_tool_call_time, _last_badge_count

    deferred_snapshot = False
    snap_pct = 0
    svc = _svc(ctx)

    # Credential hygiene: scrub any decoded vault value from every PERSISTED
    # copy (session store, activity log, auto-memory). The response returned
    # to the model is deliberately untouched — reveal is gated upstream by
    # the per-entry agent_readable flag, and injected values only appear in
    # output when a subprocess echoed them (already scrubbed in c3_shell;
    # this is the belt-and-braces layer for every other tool).
    persisted_response = response
    try:
        from services import credential_store as _creds
        if _creds._ACTIVE_SECRETS:
            args = _creds.redact_obj(args)
            summary = _creds.redact_text(summary)
            persisted_response = _creds.redact_text(response)
    except Exception:
        pass

    # Minimal critical section: only the module-global timing state needs
    # exclusive access. Disk I/O happens outside the lock so concurrent tool
    # calls don't serialize on append-only JSONL writes.
    gap_reset_seconds = 0
    with _finalize_lock:
        now = time.time()
        if _last_tool_call_time > 0 and (now - _last_tool_call_time) > 30:
            gap_reset_seconds = round(now - _last_tool_call_time)
            _last_badge_count = 0  # surface any pending alerts after /clear
        _last_tool_call_time = now

    # Outside lock: append-only logs + budget track. These are independent
    # across tool calls; holding the lock was serializing them needlessly.
    if gap_reset_seconds:
        svc.session_mgr.reset_budget()
        svc.activity_log.log("budget_auto_reset", {"gap_seconds": gap_reset_seconds})

    svc.session_mgr.log_tool_call(tool_name, args, summary)
    # ok=False lets hook_pretool_enforce's activity scan skip a failed call
    # (ISSUE-3: "Error: File not found" used to count as "c3 was used").
    try:
        from cli._hook_utils import response_text_failed as _failed
        call_ok = not _failed(response)
    except Exception:
        call_ok = True
    svc.activity_log.log("tool_call", {"tool": tool_name, "args": args,
                                       "result_summary": summary, "ok": call_ok})
    tracker = getattr(svc, "time_tracker", None)
    if tracker is not None:
        try:
            tracker.ping("tool")  # throttled heartbeat; idle gaps close sessions
        except Exception:
            pass
    svc.session_mgr.track_response(tool_name, persisted_response, response_tokens=response_tokens)

    hybrid_cfg = svc.hybrid_config or {}

    # Auto-snapshot check-then-set — short lock to ensure single-fire under
    # concurrent tool calls.
    with _finalize_lock:
        snap = svc.session_mgr.get_budget_snapshot()
        if "error" not in snap:
            threshold = snap.get("threshold", 35000)
            pct = round(snap["response_tokens"] / threshold * 100) if threshold > 0 else 0
            if pct >= 80 and not snap.get("auto_snapshot_fired", False):
                svc.session_mgr.mark_auto_snapshot_fired()
                deferred_snapshot = True
                snap_pct = pct

    # --- Outside lock: auto-memory extraction (may do file I/O / Ollama) ---
    if hasattr(svc, "auto_memory"):
        try:
            svc.auto_memory.on_tool_complete(tool_name, args, summary, persisted_response)
        except Exception:
            pass

    # --- Slow path OUTSIDE lock: auto-snapshot (fires once per session) ---
    if deferred_snapshot:
        try:
            from cli.tools.session import handle_session
            handle_session("snapshot", data="auto_budget_snapshot",
                           reasoning=f"Budget at {snap_pct}% — auto-snapshot before potential /clear",
                           description="", summary="", event_type="auto",
                           svc=svc, finalize=lambda *a, **kw: "")
            response += (
                f"\n\n[c3:auto_snapshot] Budget {snap_pct}%. Snapshot saved. "
                f"Tell user to /clear, then restore."
            )
            svc.activity_log.log("auto_snapshot", {"budget_pct": snap_pct})
        except Exception:
            pass

    return response


# ─── TOOL REGISTRATIONS (19 tools) ────────────────────────────────
# Each tool's first docstring line should state WHEN to reach for it —
# that's what Claude reads when selecting between tools.

@mcp.tool()
async def c3_search(query: str, action: str = "code", top_k: int = 3,
              max_tokens: int = 1200, prefetch: bool = False,
              scope: str = "", ctx: Context = None) -> str:
    """FIND candidates. Use to discover which files/symbols are relevant (read-only, plan-mode safe).
    action: 'code' (TF-IDF content), 'exact' (regex), 'files' (by name), 'transcript', 'semantic'.
    scope: '' current project | 'all' also searches linked sub-projects | '<name>' one sub-project.
    prefetch: auto-compress top results. Next step: c3_compress/c3_read on hits."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_search, query, action, top_k, max_tokens, svc,
                                   finalize, maybe_related_facts, prefetch=prefetch,
                                   scope=scope)


@mcp.tool()
async def c3_session(action: str, data: str = "", reasoning: str = "",
               description: str = "", summary: str = "",
               event_type: str = "auto", ctx: Context = None) -> str:
    """Session management: start, save, log, plan, snapshot, restore, compact, convo_log (log/snapshot are safe in plan mode).
    log: data + reasoning. snapshot: data=task, reasoning=next steps, summary=key files.
    restore: data=snapshot_id. convo_log: data=text, event_type=role.
    plan logs an ephemeral session plan — durable tracked TODOs belong in c3_task."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_session, action, data, reasoning, description, summary,
                                   event_type, svc, finalize)


@mcp.tool()
async def c3_memory(action: str, query: str = "", fact: str = "",
              category: str = "", top_k: int = 3,
              fact_id: str = "", include_scores: bool = False,
              scope: str = "", ctx: Context = None) -> str:
    """Durable facts — cross-session knowledge. Read-only actions safe in plan mode.
    Retrieve: recall (search; include_scores=True adds per-fact salience), index (compact IDs+snippets, then fetch), fetch (full text by fact_id="id1,id2"), query (multi-source: facts+sessions+files).
    Write:    add (fact+category, empty category→'general'), update (fact_id+fact), delete (fact_id).
    Browse:   list (category='' shows all; 'foo' filters), export (markdown).
    Audit:    review (health), ground (verify against code), score (salience), graph (edges), trends, lifespan, consolidate, consolidate_deep.
    scope (recall): '' config default | 'all' union linked sub-project facts | '<name>' one sub-project | 'project' this project only."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    call = asyncio.to_thread(
        handle_memory, action, query, fact, category, top_k, svc, finalize,
        fact_id=fact_id, include_scores=include_scores, scope=scope,
    )
    if action not in {"recall", "index", "query"}:
        return await call
    try:
        configured = float((svc.hybrid_config or {}).get(
            "memory_retrieval_timeout_seconds", 15))
    except (TypeError, ValueError):
        configured = 15.0
    timeout = max(0.1, min(configured, 60.0))
    try:
        return await asyncio.wait_for(call, timeout=timeout)
    except asyncio.TimeoutError:
        return (
            f"[memory:timeout] {action} exceeded {timeout:g}s; "
            "semantic backends may still be warming, so other tools remain available"
        )


@mcp.tool()
async def c3_read(file_path: str, symbols: Any = None, lines: Any = None,
            include_docstrings: bool = True, ctx: Context = None) -> str:
    """READ exact content. Use after c3_compress when you need real source (read-only, plan-mode safe).
    file_path: single or comma-separated. symbols: function/class names. lines: int, [start,end], or list of ranges."""
    path_err = validate_file_path(file_path)
    if path_err:
        return f"[c3_read:error] {path_err}"
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_read, file_path, symbols, lines, include_docstrings, svc, finalize)


@mcp.tool()
async def c3_compress(file_path: str, mode: str = "smart", ctx: Context = None) -> str:
    """MAP file shape — classes/functions/imports at 40-70% tokens (read-only, plan-mode safe).
    Use before c3_read to know which symbols to fetch. Modes: map, dense_map, smart, diff, bug_scan, ast.
    file_path: single or comma-separated."""
    path_err = validate_file_path(file_path)
    if path_err:
        return f"[c3_compress:error] {path_err}"
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_compress, file_path, mode, svc, finalize, maybe_related_facts)


@mcp.tool()
async def c3_validate(file_path: str, ctx: Context = None) -> str:
    """VALIDATE after every edit — syntax+types if pyright/tsc installed (read-only, plan-mode safe).
    py, json, yaml, js, ts, go, rs, html, css, etc. file_path: single or comma-separated."""
    path_err = validate_file_path(file_path)
    if path_err:
        return f"[c3_validate:error] {path_err}"
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await handle_validate(file_path, svc, finalize)


@mcp.tool()
async def c3_filter(file_path: str = "", text: str = "", pattern: str = "",
              max_lines: int = 50, depth: str = "smart",
              use_llm: bool = True, ctx: Context = None) -> str:
    """DISTILL long output — use when terminal/log output exceeds ~10 lines (read-only, plan-mode safe).
    text: inline output. file_path: log file. pattern: regex. depth: fast|smart|deep."""
    if file_path:
        path_err = validate_file_path(file_path)
        if path_err:
            return f"[c3_filter:error] {path_err}"
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_filter, file_path, text, pattern, max_lines, depth, use_llm,
                                   svc, finalize)


@mcp.tool()
async def c3_status(view: str = "budget", detailed: bool = False,
              ctx: Context = None) -> str:
    """PROJECT health and budget — run at session start or when context feels stale (read-only, plan-mode safe).
    views: budget (tokens/ratio), health (memory/index/notifications), notifications (actionable only), sessions, ghost_files, access (Access Guard rules — read-only)."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_status, view, detailed, svc, finalize)


@mcp.tool()
async def c3_delegate(task: str, task_type: str = "ask", context: str = "",
                file_path: str = "", backend: str = "ollama",
                allow_write_delegation: bool = False,
                ctx: Context = None) -> str:
    """OFFLOAD to another model — use when the subtask is local-model-sized or needs a different perspective.
    backend: ollama|codex|gemini|claude|auto. task_type: auto, summarize, explain, review, ask, test, diagnose, available, codex_check, gemini_check, codex_resume.
    allow_write_delegation: explicit user opt-in for write-capable backends (gemini/claude/codex_resume) while Access Guard rules are active; codex is pinned read-only instead."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    # Wire progress notifications (same direct-stdout approach as c3_agent)
    import json as _json
    import threading as _threading
    _stdout_lock = _threading.Lock()

    def _progress_cb(message: str):
        try:
            line = _json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": message},
            }, separators=(",", ":")) + "\n"
            with _stdout_lock:
                sys.stdout.buffer.write(line.encode("utf-8"))
                sys.stdout.buffer.flush()
        except Exception:
            pass
    svc._agent_progress_cb = _progress_cb
    try:
        return await asyncio.to_thread(handle_delegate, task, task_type, context,
                                       file_path, svc, finalize, backend,
                                       allow_write_delegation)
    finally:
        svc._agent_progress_cb = None


@mcp.tool()
async def c3_agent(workflow: str, scope: str = "", context: str = "",
             ctx: Context = None) -> str:
    """ORCHESTRATE a multi-step pipeline. Use for compound investigations that'd be 5+ tool calls otherwise.
    workflow: available, review_changes, prepare_context, investigate, preflight, validate_compress.
    scope: file paths, query, or git range. context: extra hints."""
    svc = _svc(ctx)
    loop = asyncio.get_running_loop()

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    # Wire live progress notifications: _log_progress calls this from the worker thread.
    # We write raw JSON-RPC notifications directly to stdout instead of going through
    # the async transport (session.send_log_message / ctx.info). The async approach fails
    # because asyncio.run_coroutine_threadsafe coroutines never complete in time — the
    # event loop is technically free (awaiting to_thread) but the transport write stalls.
    # Direct stdout writes are safe here because the event loop is idle during to_thread
    # (no concurrent transport writes until the tool response is sent after to_thread returns).
    import json as _json
    import threading as _threading
    _stdout_lock = _threading.Lock()

    def _progress_cb(message: str):
        try:
            line = _json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": message},
            }, separators=(",", ":")) + "\n"
            with _stdout_lock:
                sys.stdout.buffer.write(line.encode("utf-8"))
                sys.stdout.buffer.flush()
        except Exception:
            pass
    svc._agent_progress_cb = _progress_cb
    try:
        return await asyncio.to_thread(handle_agent, workflow, scope, context, svc, finalize)
    finally:
        svc._agent_progress_cb = None


@mcp.tool()
async def c3_edit(file_path: str, old_string: str = "", new_string: str = "",
                  summary: str = "", tags: str = "", replace_all: bool = False,
                  edits: str = "",
                  ctx: Context = None) -> str:
    """EDIT — read+patch+write+log in one step. Primary code-change tool; always prefer over native Edit.
    old_string: text to replace. new_string: replacement. summary: ledger description.
    edits: JSON list of {old_string, new_string, summary?} for multi-hunk batch on one file.
    Parallel across files. Create new file: non-existent file_path + old_string='' + new_string=<content>.
    If this call ERRORS OR TIMES OUT, do not retry blind — a failed c3_edit may still have
    written the file. Re-send the same args to c3_edits(action='verify') for a verdict."""
    path_err = validate_file_path(file_path)
    if path_err:
        return f"[c3_edit:error] {path_err}"
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await asyncio.to_thread(handle_edit, file_path, old_string, new_string,
                                   summary, tags, replace_all, svc, finalize, edits)


@mcp.tool()
async def c3_edits(action: str, file: str = "", change_type: str = "modified",
             summary: str = "", lines_changed: str = "", tags: str = "",
             limit: int = 50, since: str = "", edit_id: str = "",
             tag: str = "", branch: str = "", old_string: str = "",
             new_string: str = "", edits: str = "", ctx: Context = None) -> str:
    """EDIT HISTORY — inspect the ledger. Different from c3_edit (which writes); this one reads.
    actions: log (append entry), history (recent edits), versions (per-file), stats, tag (mark edit_id),
    verify (did an edit land?).
    branch: filter history to edits stamped with a given git branch.
    verify: pass the SAME file/old_string/new_string (or edits) you gave the c3_edit that errored or
    timed out. Answers APPLIED (do not retry) / NOT_APPLIED (safe to retry) / INCONCLUSIVE (read the
    file) — a failed c3_edit may still have written, so retrying blind can apply an edit twice."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    from cli.tools.edits import handle_edits
    return await asyncio.to_thread(handle_edits, action, file, change_type, summary,
                                   lines_changed, tags, limit, since, edit_id, tag,
                                   svc, finalize, branch, old_string, new_string,
                                   edits)


@mcp.tool()
async def c3_locks(action: str = "list", paths: str = "", intent: str = "",
                   ttl_s: int = 0, ctx: Context = None) -> str:
    """AGENT LEASES — who is working on which file, so two agents don't collide.
    actions: list (default), acquire, release, renew, sweep.
    paths: comma-separated, project-relative. Acquisition is ALL-OR-NOTHING.
    intent: short note other agents see when they are blocked ("refactor retry backoff").
    c3_edit already leases what it edits; use acquire to claim a multi-file refactor up front."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    from cli.tools.locks import handle_locks
    return await asyncio.to_thread(handle_locks, action, paths, intent, ttl_s,
                                   svc, finalize)


@mcp.tool()
async def c3_ci(action: str = "inspect", job: str = "", run_id: str = "",
                allow_foreign: bool = False, workflow: str = "",
                tail: int = 200, timeout: int = 0, event: str = "",
                engine: str = "auto", allow_side_effects: bool = False,
                network: str = "", mode: str = "full", base: str = "",
                allow_host_mutation: bool = False,
                no_cache: bool = False, ctx: Context = None) -> str:
    """RUN THIS REPO'S REAL CI LOCALLY — before pushing, not after.
    Reads .github/workflows/*.yml as the source of truth (no second CI config).
    actions: inspect | plan | run | rerun | status | failures | logs | runs |
      cache | history | publish | doctor. publish posts the last run as a
      GitHub COMMIT STATUS via your gh auth; it refuses a dirty tree, an
      unpushed commit, and (unless forced) a PARTIAL result. history reports per-job fail rates and
      flags FLAKY jobs — ones that both passed and failed on identical
      inputs, so the code cannot be the cause. A job whose inputs are unchanged since it last passed
      is reused (status `cached`, nothing executed); no_cache forces it.
    mode: full (default) | required. required runs only the jobs a change
      could have broken; it is CONSERVATIVE — anything unmapped runs. Use
      action='plan' to see the decision and reason for every job first.
    allow_host_mutation: the NATIVE engine refuses steps that would
      reconfigure this machine (pip/npm -g/apt/brew install...). It has no
      isolation; running this repo's own CI natively once uninstalled C3.
      Prefer engine='act'.
    inspect: workflows, the job DAG, and which jobs are runnable on THIS host.
    run: execute in dependency order; job='lint' or 'test (ubuntu-latest, 3.12)'
      selects one (bare job name = all its matrix cells). A job whose dependency
      failed is SKIPPED, never passed.
    rerun: re-run only the jobs that failed in the last run — the fix loop.
    failures: structured {file,line,message} instead of raw logs.
    VERDICTS: FULL_CI_PASS means every job ran HERE and passed — the only one
    that means "safe to push". PARTIAL_PASS means something did not run
    (different OS, unsupported action, or you selected a subset); it is NOT a
    green light. Jobs targeting another OS are refused unless allow_foreign=true,
    which runs them and labels the result cross-OS.
    event: declare which GitHub event you are simulating ('push',
      'pull_request'). Needed only when an `if:` reads github.event_name —
      there is no event locally, so C3 refuses to guess one."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    from cli.tools.ci import handle_ci
    return await asyncio.to_thread(handle_ci, action, job, run_id,
                                   allow_foreign, workflow, tail, timeout,
                                   svc, finalize, event, engine,
                                   allow_side_effects, network,
                                   mode, base, allow_host_mutation,
                                   no_cache)


@mcp.tool()
async def c3_override(action: str = "list", path: str = "", tool: str = "",
                      op: str = "read", why: str = "", request_id: str = "",
                      layer: str = "", timeout_s: int = 60,
                      ctx: Context = None) -> str:
    """ASK A HUMAN to allow ONE blocked call — never a policy change.
    actions: request, status, wait (blocks timeout_s: default 60, max 180), list, withdraw.
    request: path = the exact blocked path; why = one concrete sentence for the human.
    A yes mints a single-use, session-bound, path-exact grant with a short TTL; the
    rule that blocked you stays in force. There is NO approve action here — only the
    user can decide, from their phone or `c3 override approve`.
    Most denials are NOT escalatable (credential vault, Tier-0, catastrophic shell
    commands); those are refused here and never shown to anyone. If a refusal did not
    invite you to ask, do not ask — mark the step blocked and tell the user."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    from cli.tools.override import handle_override
    return await asyncio.to_thread(handle_override, action, path, tool, op, why,
                                   request_id, layer, timeout_s, svc, finalize)


@mcp.tool()
async def c3_impact(target: str, file_path: str = "", mode: str = "symbol",
                    ctx: Context = None) -> str:
    """BLAST RADIUS before editing a shared symbol — all call sites, imports, references.
    target: symbol/function/class name. file_path: source file to exclude. mode: symbol | unstaged (affected files with uncommitted changes)."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    from cli.tools.impact import handle_impact
    return await asyncio.to_thread(handle_impact, target, file_path, mode, svc, finalize)


@mcp.tool()
async def c3_shell(cmd: str, cwd: str = "", timeout: int = 60,
                   filter_output: bool = True, log: bool = True,
                   env_creds: str = "",
                   ctx: Context = None) -> str:
    """EXECUTE shell command — structured returns, auto-filter, ledger-aware.
    Use for tests, git, build, scripts. Returns exit_code/stdout/stderr/duration_ms.
    Auto-filters stdout >30 lines; auto-logs git mutations to the edit ledger.
    Credentials: env_creds='NAME1,NAME2' injects vault entries as env vars, and
    {{cred:NAME}} inside cmd expands server-side — decoded values never enter
    model context (see c3_credentials; echoed values are auto-redacted).
    Best-effort block of catastrophic commands (rm -rf of /, a top-level system dir, or
    $HOME/~; fork bombs; whole-drive wipes) — a guard, NOT a sandbox. Soft-warns on
    --force, --no-verify, reset --hard.
    Git Bash may not include optional tools such as jq; use `python -m json.tool`
    for portable JSON formatting. Native Bash remains the fallback for interactive/TTY commands."""
    svc = _svc(ctx)

    def finalize(name, args, resp, summ, **kw):
        return _finalize_response(ctx, name, args, resp, summ, **kw)

    return await handle_shell(cmd, cwd, timeout, filter_output, log, svc, finalize,
                              env_creds=env_creds)


@mcp.tool()
async def c3_bitbucket(
    action: str,
    project: str = "",
    repo: str = "",
    pr_id: int = 0,
    branch: str = "",
    state: str = "OPEN",
    title: str = "",
    body: str = "",
    from_branch: str = "",
    to_branch: str = "",
    description: str = "",
    reviewers: str = "",
    name: str = "",
    url: str = "",
    events: str = "",
    start_point: str = "",
    commit: str = "",
    settings: str = "",
    webhook_id: int = 0,
    comment_id: int = 0,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """BITBUCKET (Data Center / Server) — see and act on PRs, branches, builds, repo admin.
    actions: status, whoami, list_projects, list_repos, get_repo,
    list_prs, get_pr, get_pr_diff, get_pr_activities, get_pr_commits,
    create_pr, update_pr (pr_id + any of title/description/reviewers/to_branch —
    unchanged fields are preserved; reviewers is the FULL comma-separated
    replacement set), comment_pr,
    update_pr_comment / delete_pr_comment (pr_id, comment_id [body] — the
    comment's current version is fetched automatically),
    approve_pr, unapprove_pr,
    needs_work_pr (mark 'needs work' as the authenticated reviewer),
    decline_pr, merge_pr,
    list_pr_tasks (state filters OPEN/RESOLVED), create_pr_task (pr_id, body —
    a blocker task on the PR), resolve_pr_task (pr_id, comment_id from
    list_pr_tasks),
    list_branches, create_branch, delete_branch,
    list_commits, list_activity, build_status,
    repo_settings, update_repo_settings, list_webhooks, create_webhook, delete_webhook,
    list_permissions.
    project/repo fall back to bitbucket.default_project/default_repo from .c3/config.json.
    Tokens live in the OS keyring — `c3 bitbucket login` to set them up first."""
    svc = _svc(ctx)

    def finalize(fname, fargs, fresp, fsumm, **kw):
        return _finalize_response(ctx, fname, fargs, fresp, fsumm, **kw)

    from cli.tools.bitbucket import handle_bitbucket
    return await asyncio.to_thread(
        handle_bitbucket, action, svc, finalize,
        project=project, repo=repo, pr_id=pr_id, branch=branch, state=state,
        title=title, body=body, from_branch=from_branch, to_branch=to_branch,
        description=description, reviewers=reviewers, name=name, url=url,
        events=events, start_point=start_point, commit=commit,
        settings=settings, webhook_id=webhook_id, comment_id=comment_id,
        limit=limit,
    )


@mcp.tool()
async def c3_jira(
    action: str,
    issue: str = "",
    jql: str = "",
    project: str = "",
    issue_type: str = "",
    summary: str = "",
    description: str = "",
    body: str = "",
    body_format: str = "text",
    transition: str = "",
    user: str = "",
    query: str = "",
    fields: str = "",
    status_category: str = "",
    account: str = "",
    cursor: str = "",
    target: str = "",
    link_type: str = "",
    parent: str = "",
    link_id: str = "",
    board_id: int = 0,
    sprint_id: int = 0,
    sprint_state: str = "",
    time_spent: str = "",
    file_path: str = "",
    delete_subtasks: bool = False,
    limit: int = 25,
    ctx: Context = None,
) -> str:
    """JIRA (Cloud + Data Center) — search, read, create, update, link, sprint, comment, transition, assign issues.
    actions: status, whoami, search (jql), get_issue (issue — shows parent, links, attachments),
    my_issues, list_projects, list_transitions (issue), get_create_metadata (project, issue_type),
    search_users (query), list_link_types,
    list_boards ([project]), list_sprints (board_id [sprint_state=active|future|closed]),
    list_worklogs (issue),
    create_issue (project, issue_type, summary [description] [parent=epic/parent key] [fields=JSON]),
    update_issue (issue [summary] [description] [parent=epic/parent key, 'none' clears]
    [fields=JSON: field ids -> values]),
    link_issues (issue, link_type, target — reads '<issue> <link_type> <target>', e.g.
    issue=PROJ-1 link_type=blocks target=PROJ-2; link_type takes a type name or either
    directional phrasing from list_link_types),
    unlink_issues (link_id — from get_issue's link lines),
    move_to_sprint (issue [comma-list], sprint_id), move_to_backlog (issue [comma-list]),
    add_worklog (issue, time_spent='2h 30m' [body=comment]),
    attach_file (issue, file_path — uploads one local file, 20MB local cap),
    comment (issue, body), transition (issue, transition=id|name [body]), assign (issue, user),
    delete_issue (issue [delete_subtasks] — PERMANENT; Jira refuses an issue with
    subtasks unless delete_subtasks=true).
    To put an issue under an epic, pass parent=<EPIC-KEY> to create_issue/update_issue —
    C3 maps it per deployment (Cloud parent field vs Data Center Epic Link custom field).
    get_create_metadata reflects Jira's field configuration, not the create screen — if
    create_issue rejects a listed field ('not on the appropriate screen'), create without
    it and set it via update_issue (the edit screen is configured separately).
    project falls back to the account's default_project; account to jira.default_account.
    Mutating actions are ledger-logged. Tokens live in the OS keyring — `c3 jira login` first."""
    svc = _svc(ctx)

    def finalize(fname, fargs, fresp, fsumm, **kw):
        return _finalize_response(ctx, fname, fargs, fresp, fsumm, **kw)

    from cli.tools.jira import handle_jira
    return await asyncio.to_thread(
        handle_jira, action, svc, finalize,
        issue=issue, jql=jql, project=project, issue_type=issue_type,
        summary=summary, description=description, body=body,
        body_format=body_format, transition=transition, user=user,
        query=query, fields=fields, status_category=status_category,
        account=account, cursor=cursor, target=target, link_type=link_type,
        parent=parent, link_id=link_id, board_id=board_id,
        sprint_id=sprint_id, sprint_state=sprint_state,
        time_spent=time_spent, file_path=file_path,
        delete_subtasks=delete_subtasks, limit=limit,
    )


@mcp.tool()
async def c3_credentials(
    action: str,
    name: str = "",
    value: str = "",
    scope: str = "",
    description: str = "",
    ctype: str = "",
    env_var: str = "",
    inject: bool = False,
    agent_readable: bool = False,
    file_path: str = "",
    dry_run: bool = True,
    only: str = "",
    ctx: Context = None,
) -> str:
    """CREDENTIAL VAULT — named secrets + sensitive personal data the user manages
    (global + per-project), injection-first.
    actions: list, describe (name), check (name), usage ([name] — when/where/how
    often it was used: counts by surface, recent events for THIS project, other
    projects reduced to counts), reveal (name — only entries the user marked
    agent_readable), set (name, value [scope=project|global]
    [ctype=token|env|multiline|address|identity|card|login] [description] [env_var]
    [inject]), delete (name [scope]), import_env (file_path [only='A,B']
    [dry_run=false] — bulk-import a .env into the vault).
    import_env NEVER shows you a value: the server reads the file, you get names,
    lengths, fingerprints and per-row reasons. It is project scope only, never
    overwrites, refuses a path outside the project, and DEFAULTS TO dry_run=true
    — call it bare to preview, then re-run with dry_run=false once the user has
    seen the list. Importing to the global vault or replacing an existing secret
    stays a user action (`c3 creds import` / the Credentials UI).
    To USE a credential, do NOT reveal it — pass env_creds='NAME1,NAME2' to c3_shell
    (injected as env vars) or write {{cred:NAME}} inside the cmd (expanded server-side);
    the decoded value never enters model context.
    STRUCTURED kinds (address/identity/card/login) hold named fields: set takes value
    as a JSON object (card: cardholder/number/expiry[/cvc/billing_zip]; address:
    street1/city/state/zip[/recipient/street2/country/phone]; identity:
    full_name[/dob/ssn/phone/email]; login: site_id/canonical_origin/username/password
    [/totp_secret]). `login` is STORAGE ONLY — C3 has no browser and never types a
    password anywhere. canonical_origin is https-only and stored normalized so an
    external runner can bind the credential to one origin; do NOT build a browser
    login runner in this package. Address a FIELD at the boundary —
    env_creds='CARD.number' (env $CARD_NUMBER) or {{cred:CARD.number}} in cmd. Reveal
    is permanently disabled for them and they never auto-inject; have the user enter
    the values via the Credentials UI or `c3 creds set` so they never enter the chat.
    Values live in the OS keyring / an encrypted sidecar, never in config files.
    Mutations and reveals are ledger-logged."""
    svc = _svc(ctx)

    def finalize(fname, fargs, fresp, fsumm, **kw):
        return _finalize_response(ctx, fname, fargs, fresp, fsumm, **kw)

    from cli.tools.credentials import handle_credentials
    return await asyncio.to_thread(
        handle_credentials, action, svc, finalize,
        name=name, value=value, scope=scope, description=description,
        ctype=ctype, env_var=env_var, inject=inject, agent_readable=agent_readable,
        file_path=file_path, dry_run=dry_run, only=only,
    )


@mcp.tool()
async def c3_project(
    action: str,
    project: str = "",
    query: str = "",
    file_path: str = "",
    symbols: Any = None,
    lines: Any = None,
    mode: str = "map",
    view: str = "health",
    top_k: int = 5,
    max_tokens: int = 1200,
    search_action: str = "code",
    mem_action: str = "recall",
    fact: str = "",
    category: str = "",
    fact_id: str = "",
    edits_action: str = "history",
    file: str = "",
    tag: str = "",
    limit: int = 50,
    target: str = "",
    old_string: str = "",
    new_string: str = "",
    summary: str = "",
    edits: str = "",
    replace_all: bool = False,
    tags: str = "",
    cmd: str = "",
    timeout: int = 60,
    scan_roots: str = "",
    allow_write: bool = False,
    ctx: Context = None,
) -> str:
    """CROSS-PROJECT — run C3 against OTHER c3-installed projects (read-only safe in plan mode).
    Discover: list (registry), scan (registry+filesystem), info, register, unregister.
    Read    : search, read, compress, status, memory, impact, edits, validate, filter.
    Write   : edit, shell, memory(add/update/delete) — require allow_write=true; logged to that project's ledger.
    Sub-projects (project = the PARENT). Hierarchy is a strict tree: one parent, many children, nested up to 8 deep.
      Reads : subprojects (direct children + rollup), sub_tree (the whole hierarchy),
              sub_inspect (target=path — is there a C3 project there, what is in it, who already claims it,
              what it claims, and which nested projects under it are NOT linked yet; mutates nothing).
      Writes: sub_add (target=folder|path, tag=name — initializes if needed; allow_write),
              sub_link (target=path to an EXISTING project anywhere on disk, incl. another drive; allow_write),
              sub_remove (target=name|path, mode=unlink|clear; allow_write),
              sub_cascade (mode=update|reindex|health — walks the whole subtree; health is read-only).
      A child need NOT live inside the parent folder. Nested children are excluded from the parent's index;
      externally-linked ones are not in it to begin with, so the parent's index is untouched.
    project = registered name OR absolute path (.c3 required). list/scan need no project.
    search_action/mem_action/edits_action pick the sub-op for those verbs."""
    svc = _svc(ctx)

    def finalize(fname, fargs, fresp, fsumm, **kw):
        return _finalize_response(ctx, fname, fargs, fresp, fsumm, **kw)

    from cli.tools.project import handle_project
    return await asyncio.to_thread(
        handle_project, action, svc, finalize,
        project=project, query=query, file_path=file_path, symbols=symbols,
        lines=lines, mode=mode, view=view, top_k=top_k, max_tokens=max_tokens,
        search_action=search_action, mem_action=mem_action, fact=fact,
        category=category, fact_id=fact_id, edits_action=edits_action, file=file,
        tag=tag, limit=limit, target=target, old_string=old_string,
        new_string=new_string, summary=summary, edits=edits,
        replace_all=replace_all, tags=tags, cmd=cmd, timeout=timeout,
        scan_roots=scan_roots, allow_write=allow_write,
    )


@mcp.tool()
async def c3_task(
    action: str,
    title: str = "",
    task_id: str = "",
    status: str = "",
    priority: str = "",
    due_date: str = "",
    tags: str = "",
    description: str = "",
    milestone: str = "",
    note: str = "",
    kind: str = "",
    link_type: str = "",
    ref: str = "",
    label: str = "",
    name: str = "",
    target_date: str = "",
    query: str = "",
    limit: int = 50,
    parent: str = "",
    minutes: int = 0,
    ctx: Context = None,
) -> str:
    """TRACK WORK — durable per-project tasks, milestones, and decision notes; use when asked to create/update/complete tasks (reads safe in plan mode).
    Tasks: add (title [+description/priority p0-p3/due_date YYYY-MM-DD/tags CSV/milestone/parent]),
      update (task_id + changed fields incl. status backlog|in_progress|blocked|done), done (task_id),
      list (filters: status/priority/tags/milestone/query), get, board (kanban columns + milestone progress), archive,
      link/unlink (task_id + link_type file|commit|edit + ref — ties tasks to code),
      block/unblock (task_id + ref=blocking task id; cycle-safe; completing the last open blocker auto-releases dependents to backlog),
      report (overdue, blocked chains + aging, ready-to-unblock, milestone health/at-risk, throughput).
    Subtasks: one level via parent (add/update; update parent='none' clears).
    Time: time_add (minutes 1-1440 [+note/due_date=date/task_id]), time_update / time_delete (ref=entry id),
      time_list (manual entries + recent auto sessions), time_summary (today/7d/30d, auto vs manual).
      Auto-tracking: server startup + tool calls ping .c3/time; idle gaps >15min close a session.
    Milestones: milestone_add (name [+target_date]), milestone_update, milestone_list (with progress %),
      milestone_complete (close a shipped milestone; refuses while open tasks remain; tasks KEEP their link),
      milestone_reopen (undo complete), milestone_archive (removal: detaches tasks).
    Notes: note_add (note [+kind=decision] [+task_id]), note_list.
    History: history ([+task_id] [+limit]) — append-only event log (who/what/when, before->after), newest first.
    task_id accepts any unique id prefix (>=4 chars); milestone accepts id or unique name.
    Ephemeral session plans stay in c3_session(action='plan')."""
    svc = _svc(ctx)

    def finalize(fname, fargs, fresp, fsumm, **kw):
        return _finalize_response(ctx, fname, fargs, fresp, fsumm, **kw)

    from cli.tools.tasks import handle_task
    return await asyncio.to_thread(
        handle_task, action, svc, finalize,
        title=title, task_id=task_id, status=status, priority=priority,
        due_date=due_date, tags=tags, description=description, milestone=milestone,
        note=note, kind=kind, link_type=link_type, ref=ref, label=label,
        name=name, target_date=target_date, query=query, limit=limit,
        parent=parent, minutes=minutes)


@mcp.tool()
async def c3_artifacts(
    action: str,
    artifact: str = "",
    cls: str = "",
    provider: str = "",
    version: int = 0,
    against: int = 0,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """AGENT-CONFIG TRACKING — inventory + version history + diff + restore for the files that shape the agent itself: instruction docs (CLAUDE.md/AGENTS.md/GEMINI.md/.cursorrules/copilot-instructions), settings/hooks, MCP configs, and .claude skills/agents/commands/plugins, across every IDE (reads safe in plan mode).
    scan (refresh inventory; captures out-of-band edits), list (filters: cls=instructions|settings|mcp|skill|agent|command|plugin, provider),
    history (artifact optional — all events when omitted), show (artifact [+version; 0=live]),
    diff (artifact + version [+against; 0=live]), restore (artifact + version — writes prior bytes back, forward-only),
    status (counts + out-of-band changes since last scan).
    artifact accepts an id ('skill:browcontrol'), unique prefix, or path ('CLAUDE.md').
    Changes made through c3_edit are attributed automatically; a background agent catches manual edits."""
    svc = _svc(ctx)

    def finalize(fname, fargs, fresp, fsumm, **kw):
        return _finalize_response(ctx, fname, fargs, fresp, fsumm, **kw)

    from cli.tools.artifacts import handle_artifacts
    return await asyncio.to_thread(
        handle_artifacts, action, svc, finalize,
        artifact=artifact, cls=cls, provider=provider,
        version=version, against=against, limit=limit)


def main() -> None:
    """Entry-point for the ``c3-mcp`` console script."""
    from services import error_reporting
    error_reporting.init(component="c3-mcp", version=C3_VERSION)
    mcp.run(transport="stdio", show_banner=False, log_level="ERROR")


if __name__ == "__main__":
    main()
