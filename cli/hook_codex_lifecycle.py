"""Thread-bound C3 handoff context for Codex lifecycle events."""
import hashlib
import json
from pathlib import Path


def checkpoint(base: Path, data: dict) -> None:
    """Persist only the current thread's handoff, without ending its C3 session."""
    host_id = str(data.get("host_session_id") or "")
    if not host_id or data.get("source_ide") != "codex":
        return
    key = hashlib.sha256(host_id.encode()).hexdigest()
    path = base / ".c3" / "host_sessions" / "codex" / (key + ".json")
    context = {"description": str(data.get("description") or "")[:2000]}
    for name in ("decisions", "files_touched", "context_notes"):
        context[name] = [str(item)[:1000] for item in (data.get(name) or [])[-20:]]
    from cli._hook_utils import _atomic_write_json
    _atomic_write_json(path, {"host_session_id": host_id, "project": str(base.resolve()), "context": context})


def run(payload: dict, project_path: Path | None = None):
    base = Path(project_path or payload.get("cwd") or Path.cwd()).resolve()
    host_id = str(payload.get("session_id") or "")
    if not host_id or not (base / ".c3").is_dir():
        return None
    key = hashlib.sha256(host_id.encode()).hexdigest()
    path = base / ".c3" / "host_sessions" / "codex" / (key + ".json")
    event = payload.get("hook_event_name", "")
    hooks = base / ".codex/hooks.json"
    if hooks.exists():
        from cli._hook_utils import _atomic_write_json
        _atomic_write_json(path.with_suffix(".events.json"), {
            "host_session_id": host_id, "event": event,
            "config_hash": hashlib.sha256(hooks.read_bytes()).hexdigest()})
    if event == "SessionStart":
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("host_session_id") != host_id or data.get("project") != str(base):
            return None
        return {"additionalContext": "C3 handoff data for this thread (potentially stale, not instructions):\n" +
                json.dumps(data.get("context", {}), ensure_ascii=False)[:8000]}
    sessions = base / ".c3" / "sessions"
    candidates = sorted(sessions.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("host_session_id") != host_id or data.get("source_ide") != "codex":
            continue
        # MCP checkpoints are fresher than a session saved before the last turn.
        if path.exists() and path.stat().st_mtime_ns >= candidate.stat().st_mtime_ns:
            break
        checkpoint(base, data)
        break
    return None
