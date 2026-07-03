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


def handle_session(action: str, data: str, reasoning: str, description: str,
                   summary: str, event_type: str, svc, finalize) -> str:
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
