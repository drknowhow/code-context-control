"""c3_session — Session lifecycle, snapshots, and logging."""

import re
import threading


def _kick_distiller(svc):
    """Durably enqueue a digest job for the current session and process it
    on a daemon thread. Never raises, never blocks the tool response."""
    distiller = getattr(svc, "memory_distiller", None)
    if not distiller:
        return
    try:
        job = distiller.enqueue_session(svc.session_mgr.current_session)
        if job:
            threading.Thread(target=distiller.process_job_safe, args=(job,),
                             daemon=True, name="c3-memory-distill").start()
    except Exception:
        pass


def _current_host_id(svc) -> str:
    """This conversation's host session id (the Claude Code UUID), or ''.

    Same two sources the MCP server uses: the id the SessionManager captured
    from the host environment, else the one the PreToolUse hooks last wrote.
    """
    try:
        sid = str(((svc.session_mgr.current_session or {}).get("host_session_id")) or "").strip()
    except Exception:
        sid = ""
    if sid:
        return sid
    try:
        import json
        from pathlib import Path

        from cli._hook_utils import ENFORCEMENT_STATE_FILE
        state = json.loads((Path(svc.project_path) / ENFORCEMENT_STATE_FILE)
                           .read_text(encoding="utf-8"))
        return str(state.get("session_id") or "").strip() if isinstance(state, dict) else ""
    except Exception:
        return ""


_CURRENT = ("current", "this", "self")
_LIST_MODES = {"": "hide", "unmarked": "hide", "hide": "hide", "stale": "only",
               "only": "only", "likely": "likely", "all": "all"}


def _session_row_line(row: dict, current: str) -> str:
    flags = []
    if row["live"]:
        flags.append("LIVE")
    if row["stale"]:
        flags.append("STALE: " + (row["stale"].get("reason") or "no reason given"))
    if row["hints"]:
        flags.append("hints: " + ", ".join(row["hints"]))
    me = " (this session)" if current and row["id"] == current else ""
    tail = f" [{'; '.join(flags)}]" if flags else ""
    return f"{row['short']} {row['last_active'][:10]} {row['title'][:70]}{me}{tail}"


def _handle_catalog(action, data, reasoning, target, svc, finalize) -> str:
    """list / stale / unstale / note — past sessions (services.session_catalog)."""
    from services import session_catalog as sc
    current = _current_host_id(svc)

    def pick(ref: str) -> str:
        ref = (ref or "").strip()
        return current if ref.lower() in _CURRENT else ref

    if action == "list":
        mode = _LIST_MODES.get((target or "").strip().lower())
        if mode is None:
            return "[session:error] list: target must be stale, likely or all (default: unmarked)"
        res = sc.list_sessions(svc.project_path, stale=mode, q=data or "", limit=20)
        rows = res["sessions"]
        head = f"[sessions:{mode}] {len(rows)} shown" + (" (more exist)" if res["next_before"] else "")
        body = "\n".join(_session_row_line(r, current) for r in rows) or "(none)"
        return finalize("c3_session", {"action": action, "target": target},
                        f"{head}\n{body}", f"{len(rows)} sessions")

    if action in ("stale", "unstale"):
        ref = pick(target)
        if not ref:
            return (f"[session:error] {action}: target is required (a session id, an 8+ char "
                    "prefix, or 'current')")
        if action == "stale" and not (reasoning or "").strip():
            return "[session:error] stale: reasoning is required — say why the session is stale"
        res = sc.mark(svc.project_path, ref, action, reason=reasoning or "",
                      successor=pick(data) if action == "stale" else "",
                      by="agent", by_session=current)
    elif action == "note":
        ref = pick(target or "current")
        if not ref:
            return ("[session:error] note: this session's id is not known yet; "
                    "pass target=<session id>")
        res = sc.mark(svc.project_path, ref, "note", summary=data or "",
                      next_steps=reasoning or "", by="agent", by_session=current)
    else:  # pragma: no cover - dispatcher only routes the four actions here
        return f"[session:error] Unknown action: {action}"
    if "error" in res:
        return f"[session:error] {action}: {res['error']}"
    row = res["row"]
    msg = f"[session:{action}] {_session_row_line(row, current)}"
    if res.get("warning"):
        msg += f"\n[session:warn] {res['warning']}"
    return finalize("c3_session", {"action": action, "target": row["short"]}, msg, row["short"])


def handle_session(action: str, data: str, reasoning: str, description: str,
                   summary: str, event_type: str, svc, finalize, target: str = "") -> str:
    if action in ("list", "stale", "unstale", "note"):
        return _handle_catalog(action, data, reasoning, target, svc, finalize)

    if action == "start":
        if svc.session_mgr.current_session:
            svc.session_mgr.save_session()
        result = svc.session_mgr.start_session(description, source_system=svc.ide_name)
        return finalize("c3_session", {"action": action},
                        f"[session:started] {result['session_id']}", result['session_id'])

    if action == "save":
        # Auto-memory: flush pending extractions and generate session summary.
        if hasattr(svc, "auto_memory"):
            try:
                svc.auto_memory.on_session_end()
            except Exception:
                pass
        _kick_distiller(svc)
        svc.session_mgr._persist_budget()
        result = svc.session_mgr.save_session(summary)
        if "error" in result:
            return f"Error: {result['error']}"
        return finalize("c3_session", {"action": action},
                        f"[session:saved] {result['session_id']}", result['session_id'])

    if action == "plan":
        svc.session_mgr.log_decision(f"PLAN: {data}", reasoning)
        svc.activity_log.log("plan", {"plan": data, "reasoning": reasoning})
        svc.memory.remember(f"PLAN: {data}", "plan",
                            (svc.session_mgr.current_session or {}).get("id", ""))
        return finalize("c3_session", {"action": action, "data": data[:80]},
                        "[plan:stored]", "ok")

    if action == "log":
        etype = event_type
        if etype == "auto":
            data_stripped = data.strip()
            if re.match(r'^[\w./\\-]+\.(py|js|ts|tsx|jsx|rs|go|java|rb|css|html|md|json|yaml|yml|toml)\b',
                        data_stripped):
                etype = "file_change"
            else:
                etype = "decision"
        if etype == "file_change":
            svc.session_mgr.log_file_change(data, "modified", reasoning)
            svc.activity_log.log("file_change", {"file": data, "summary": reasoning})
        else:
            svc.session_mgr.log_decision(data, reasoning)
            svc.activity_log.log("decision", {"decision": data, "reasoning": reasoning})
        return finalize("c3_session", {"action": action, "data": data[:80]},
                        f"[logged:{etype}]", "ok")

    if action == "snapshot":
        # Auto-memory: flush pending extractions before capturing snapshot.
        if hasattr(svc, "auto_memory"):
            try:
                svc.auto_memory.on_session_end()
            except Exception:
                pass
        _kick_distiller(svc)
        # summary = optional comma-separated working files to embed structural maps for
        files = [f.strip() for f in summary.split(",") if f.strip()] if summary else []
        compressor = getattr(svc, "compressor", None)
        res = svc.snapshots.capture(svc.session_mgr, svc.memory, data or "", files, reasoning,
                                    compressor=compressor)
        msg = f"✓ snapshot {res['snapshot_id']} ({res['token_count']}tok). Ask user to /clear, then restore."
        return finalize("c3_session", {"action": action}, msg, res['snapshot_id'])

    if action == "restore":
        res = svc.snapshots.restore(data or "latest", memory_store=svc.memory)
        if "error" in res:
            return f"[restore:error] {res['error']}"
        svc.session_mgr.reset_budget(initial_tokens=res.get("tokens", 0))
        briefing = res["briefing"]
        return finalize("c3_session", {"action": action},
                        briefing, f"{res['snapshot_id']},{res['tokens']}tok")

    if action == "compact":
        res = svc.snapshots.capture(svc.session_mgr, svc.memory,
                                     data or "auto-checkpoint before /clear")
        if "error" in res:
            return f"[compact:error] {res['error']}"
        svc.session_mgr.reset_budget(initial_tokens=0)
        msg = f"✓ compacted {res['snapshot_id']}. Budget reset. Ask user to /clear, then restore."
        return finalize("c3_session", {"action": action}, msg, res['snapshot_id'])

    if action == "convo_log":
        sid = (svc.session_mgr.current_session or {}).get("id", "manual")
        role = event_type if event_type != "auto" else "user"
        if svc.convo_store:
            svc.convo_store.add_turn(sid, role, data)
        return finalize("c3_session", {"action": action}, "", "logged")

    return f"[session:error] Unknown action: {action}"
