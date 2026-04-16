"""c3_status — Budget, health, notifications, and ghost-file sweep (4+ views).

Removed views (available via REST API/CLI):
  'why', 'raw', 'optimize' — use `c3 status <view>` CLI command instead.
  'tokens', 'memory' — merged into 'budget' (detailed=True) and 'health' respectively.
"""

import json
import time
from pathlib import Path

from core import count_tokens, format_token_count
from cli.hook_ghost_files import scan_ghost_files, cleanup_ghost_files


def handle_status(view: str, detailed: bool, svc, finalize) -> str:
    if view == "budget":
        return _budget_view(svc, detailed, finalize)

    if view == "health":
        return _health_view(svc, finalize)

    if view == "notifications":
        return _notifications_view(svc, finalize)

    if view == "sessions":
        return _sessions_view(svc, finalize)

    if view == "ghost_files":
        return _ghost_files_view(svc, finalize)

    # Graceful migration for removed views
    removed = {
        "tokens": "Merged into 'budget'. Use c3_status(view='budget', detailed=True).",
        "memory": "Merged into 'health'. Use c3_status(view='health').",
        "why": "Available via CLI: `c3 status why`",
        "raw": "Available via CLI: `c3 status raw`",
        "optimize": "Available via CLI: `c3 status optimize`",
    }
    if view in removed:
        return finalize("c3_status", {"view": view},
                        f"[status:moved] '{view}' view removed from MCP. {removed[view]}", "moved")

    return f"[status:error] Unknown view: {view}. Available: budget, health, notifications, sessions, ghost_files"


def _budget_view(svc, detailed, finalize):
    snap = svc.session_mgr.get_budget_snapshot()
    if "error" in snap:
        return f"[ctx_status] {snap['error']}"

    tokens = snap["response_tokens"]
    threshold = snap["threshold"]
    pct = round(tokens / threshold * 100) if threshold > 0 else 0

    # Session age
    age_str = ""
    sess = svc.session_mgr.current_session
    if sess:
        started = sess.get("started", "")
        try:
            start_ts = time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%S"))
            age_str = f" age:{round((time.time() - start_ts) / 60)}min"
        except Exception:
            pass

    content = snap.get("content_tokens", tokens)
    infra = snap.get("infra_tokens", 0)
    infra_note = f" (content:{content} infra:{infra})" if infra > 0 else ""
    lines = [
        f"[ctx_status] {tokens}tok/{snap['call_count']}calls "
        f"avg:{snap['avg_tokens_per_call']} ({pct}% of {threshold}tok threshold){age_str}{infra_note}"
    ]

    # File memory coverage
    try:
        tracked = svc.file_memory.list_tracked()
        idx_stats = svc.indexer.get_stats()
        total_files = idx_stats.get("files_indexed", 0)
        lines.append(f"[file_memory] {len(tracked)}/{total_files} files indexed")
    except Exception as e:
        lines.append(f"[file_memory] error: {e}")

    # C3 adoption ratio
    c3_calls = snap.get("c3_calls", 0)
    native_calls = snap.get("native_calls", 0)
    adoption = snap.get("c3_adoption_pct", 100)
    if c3_calls + native_calls > 0:
        lines.append(f"[c3_adoption] {adoption}% ({c3_calls}c3/{native_calls}native)")

    # Per-tool token breakdown
    by_tool = snap.get("by_tool", {})
    if by_tool:
        sorted_tools = sorted(by_tool.items(), key=lambda x: -x[1])
        shown = sorted_tools[:6]
        breakdown = " | ".join(f"{n}:{t}tok" for n, t in shown)
        if len(sorted_tools) > 6:
            breakdown += f" (+{len(sorted_tools) - 6} more)"
        lines.append(f"[breakdown] {breakdown}")

    if detailed:
        stats = svc.indexer.get_stats()
        lines.append(f"[index] files:{stats['files_indexed']} "
                      f"tok:{format_token_count(stats['total_tokens_in_codebase'])}")

    # Single warning when over threshold
    if pct >= 100:
        lines.append(f"[warn] Budget exceeded ({pct}%). Consider compact + /clear.")

    return finalize("c3_status", {"view": "budget"}, "\n".join(lines), f"{pct}%")


def _health_view(svc, finalize):
    parts = []
    ollama_ok = svc.ollama_client and svc.ollama_client.is_available()
    models = svc.ollama_client.list_models() if ollama_ok else []
    parts.append(f"[ollama] {'up (' + str(len(models)) + ' models)' if ollama_ok else 'unavailable'}")
    stats = svc.indexer.get_stats()
    parts.append(f"[index] {stats.get('files_indexed', 0)} files indexed")
    sess = svc.session_mgr.current_session
    if sess:
        started = sess.get("started", "")
        try:
            start_ts = time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%S"))
            age_min = round((time.time() - start_ts) / 60)
            parts.append(f"[session] {sess.get('id', '?')[:12]} age:{age_min}min "
                          f"calls:{len(sess.get('tool_calls', []))}")
        except Exception:
            parts.append(f"[session] {sess.get('id', '?')[:12]}")
    else:
        parts.append("[session] none active")
    pending = svc.notifications.get_unacknowledged(limit=5)  # actionable only
    info_n = svc.notifications.get_suppressed_info_count()
    info_tail = f" (+{info_n} info)" if info_n else ""
    parts.append(f"[notifications] {len(pending)} actionable{info_tail}")
    if svc.vector_store:
        try:
            vs = svc.vector_store.get_stats()
            parts.append(f"[sltm] {vs.get('total_records', 0)} records "
                          f"ollama={vs.get('ollama_available', False)}")
        except Exception as e:
            parts.append(f"[sltm] error: {e}")
    else:
        parts.append("[sltm] disabled")
    all_facts = getattr(svc.memory, 'facts', []) or []
    active_facts = [f for f in all_facts if f.get("lifecycle") != "archived"]
    parts.append(f"[memory] {len(active_facts)} active / {len(all_facts)} total")
    # Doc index (Local RAG Pipeline)
    if hasattr(svc, "doc_index") and svc.doc_index:
        di_stats = svc.doc_index.get_stats()
        parts.append(f"[doc_index] {di_stats['total_chunks']} chunks "
                      f"({di_stats['files_tracked']} files)")
    else:
        parts.append("[doc_index] disabled")
    # Validation cache stats
    vcache = getattr(svc, "validation_cache", None)
    if vcache:
        vs = vcache.summary()
        err_note = f" ({vs['errors']} errors)" if vs["errors"] else ""
        parts.append(f"[validation] {vs['cached_files']} cached, {vs['clean']} clean{err_note}")
    else:
        parts.append("[validation] disabled")
    # Agent workflows
    wf_cfg = (svc.hybrid_config or {}).get("agent_workflows", {})
    wf_enabled = wf_cfg.get("enabled", True)
    if wf_enabled:
        parts.append(f"[workflows] enabled prefetch_max={wf_cfg.get('prefetch_max_files', 3)} "
                      f"batch_max={wf_cfg.get('batch_validate_max_files', 10)} "
                      f"compound_max={wf_cfg.get('compound_max_compress', 5)} "
                      f"delegate={'on' if wf_cfg.get('delegate_in_workflows', True) else 'off'}")
    else:
        parts.append("[workflows] disabled")
    # .c3/ directory disk usage
    try:
        c3_dir = Path(svc.project_path) / ".c3"
        if c3_dir.exists():
            total_bytes = sum(f.stat().st_size for f in c3_dir.rglob("*") if f.is_file())
            if total_bytes < 1024 * 1024:
                size_str = f"{total_bytes / 1024:.0f}KB"
            else:
                size_str = f"{total_bytes / (1024 * 1024):.1f}MB"
            parts.append(f"[storage] .c3/ {size_str}")
    except Exception as e:
        parts.append(f"[storage] error: {e}")
    # Ghost-file scan
    try:
        ghosts = scan_ghost_files(Path(svc.project_path))
        if ghosts:
            names = ", ".join(g["name"] for g in ghosts)
            parts.append(f"[ghost_files] {len(ghosts)} found: {names}  "
                         f"(run c3_status(view='ghost_files') to clean)")
        else:
            parts.append("[ghost_files] clean")
    except Exception:
        pass
    return finalize("c3_status", {"view": "health"}, "\n".join(parts), "ok")


def _sessions_view(svc, finalize):
    """Show recent session token/cost stats from .c3/session_stats.jsonl."""
    stats_path = Path(svc.project_path) / ".c3" / "session_stats.jsonl"
    if not stats_path.exists():
        return finalize(
            "c3_status", {"view": "sessions"},
            "[sessions] No data yet. Run c3 install-mcp to enable the Stop hook that captures stats.",
            "empty",
        )

    entries = []
    try:
        with open(stats_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception as exc:
        return f"[sessions:error] Could not read {stats_path}: {exc}"

    if not entries:
        return finalize("c3_status", {"view": "sessions"}, "[sessions] No sessions recorded yet.", "empty")

    recent = entries[-20:]  # last 20 sessions
    total_cost = sum(e.get("cost_usd") or 0 for e in recent)
    total_in = sum(e.get("input_tokens") or 0 for e in recent)
    total_out = sum(e.get("output_tokens") or 0 for e in recent)
    total_cache_read = sum(e.get("cache_read_tokens") or 0 for e in recent)

    lines = [f"# Session Stats (last {len(recent)} of {len(entries)} sessions)"]
    lines.append(f"Total cost: ${total_cost:.4f}  |  In: {total_in:,}  Out: {total_out:,}  Cache-read: {total_cache_read:,}")
    lines.append("")
    lines.append(f"{'Date':<22} {'Cost':>8} {'In':>8} {'Out':>6} {'Cache-rd':>9} Stop")
    lines.append("-" * 64)
    for e in reversed(recent):
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        cost = f"${e.get('cost_usd') or 0:.4f}"
        inp = f"{e.get('input_tokens') or 0:,}"
        out = f"{e.get('output_tokens') or 0:,}"
        cr = f"{e.get('cache_read_tokens') or 0:,}"
        reason = e.get("stop_reason") or ""
        lines.append(f"{ts:<22} {cost:>8} {inp:>8} {out:>6} {cr:>9} {reason}")

    resp = "\n".join(lines)
    return finalize("c3_status", {"view": "sessions"}, resp, f"{len(recent)}sess")


def _notifications_view(svc, finalize):
    """Actionable warnings + critical only. Info events are archived,
    not surfaced here — 'File maps updated' is not news worth paging on.
    Use the web UI activity log or the REST API with severities='info'
    to inspect them.
    """
    pending = svc.notifications.get_unacknowledged(limit=20)  # actionable by default
    info_count = svc.notifications.get_suppressed_info_count()
    if not pending:
        tail = f" ({info_count} info events archived)" if info_count else ""
        return f"No actionable notifications.{tail}"
    lines = [f"# Actionable ({len(pending)})"]
    for n in pending:
        lines.append(f"[{n['severity']}] {n['agent']}: {n['title']}")
    if info_count:
        lines.append(f"\n(+{info_count} info events archived — not shown)")
    return finalize("c3_status", {"view": "notifications"},
                    "\n".join(lines), f"{len(pending)}p")


def _ghost_files_view(svc, finalize):
    """Scan project root for ghost files, report details, and auto-clean them."""
    project_root = Path(svc.project_path)
    ghosts = scan_ghost_files(project_root)

    if not ghosts:
        return finalize("c3_status", {"view": "ghost_files"},
                        "[ghost_files] Project root is clean — no ghost files detected.", "clean")

    lines = [f"# Ghost Files ({len(ghosts)} found)"]
    for g in ghosts:
        lines.append(f"  {g['name']} ({g['size']}B) — {g['reason']}")

    # Auto-clean
    deleted = cleanup_ghost_files(ghosts)
    if deleted:
        lines.append(f"\nDeleted {len(deleted)}: {', '.join(deleted)}")
    else:
        lines.append("\nCould not delete any ghost files (permission error?).")

    resp = "\n".join(lines)
    return finalize("c3_status", {"view": "ghost_files"}, resp, f"{len(deleted)} cleaned")
