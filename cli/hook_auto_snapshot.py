"""Stop hook: auto-snapshot + auto-memory on session end.

Triggered by the Claude Code 'Stop' event. Fires after hook_session_stats.
Looks up the running C3 UI server via ~/.c3/registry.json and calls
POST /api/auto-snapshot to capture context before it is lost.

Falls back to a lightweight file-based snapshot if the server is unreachable.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import log_hook_error  # noqa: E402

_REGISTRY_FILE = Path.home() / ".c3" / "registry.json"
_TIMEOUT_SECS = 4


def _find_server_port(project_path: str) -> int | None:
    """Look up the C3 UI server port for this project from the global registry."""
    try:
        if not _REGISTRY_FILE.exists():
            return None
        with open(_REGISTRY_FILE, encoding="utf-8") as f:
            entries = json.load(f)
        resolved = str(Path(project_path).resolve())
        for entry in entries:
            if str(Path(entry.get("project_path", "")).resolve()) == resolved:
                return entry.get("port")
    except Exception:
        pass
    return None


def _call_server(port: int, stop_hook_data: dict) -> bool:
    """POST to the running C3 server's auto-snapshot endpoint."""
    try:
        url = f"http://127.0.0.1:{port}/api/auto-snapshot"
        body = json.dumps({
            "session_id": stop_hook_data.get("session_id", ""),
            "stop_reason": stop_hook_data.get("stop_reason", ""),
        }).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=_TIMEOUT_SECS)
        return resp.status == 200
    except (URLError, OSError):
        return False


def _fallback_snapshot(stop_hook_data: dict) -> None:
    """Lightweight file-based snapshot when the UI server is not running."""
    c3_dir = Path(".c3")
    if not c3_dir.exists():
        return

    snap_dir = c3_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Read the latest session file for context
    sessions_dir = c3_dir / "sessions"
    session_data = {}
    if sessions_dir.exists():
        session_files = sorted(sessions_dir.glob("session_*.json"), reverse=True)
        if session_files:
            try:
                with open(session_files[0], encoding="utf-8") as f:
                    session_data = json.load(f)
            except Exception:
                pass

    # Read budget if available
    budget = {}
    budget_file = c3_dir / "context_budget.json"
    if budget_file.exists():
        try:
            with open(budget_file, encoding="utf-8") as f:
                budget = json.load(f)
        except Exception:
            pass

    snap_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot = {
        "schema_version": 3,
        "snapshot_id": snap_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "session_id": stop_hook_data.get("session_id", session_data.get("id", "")),
        "task_description": session_data.get("description", "auto-snapshot on stop"),
        "trigger": "stop_hook",
        "stop_reason": stop_hook_data.get("stop_reason", ""),
        "working_files": [ft["file"] for ft in session_data.get("files_touched", [])[:8]
                          if isinstance(ft, dict) and ft.get("file")],
        "decisions": session_data.get("decisions", []),
        "files_touched": session_data.get("files_touched", []),
        "context_notes": session_data.get("context_notes", []),
        "context_budget": {
            "response_tokens": budget.get("response_tokens", 0),
            "call_count": budget.get("call_count", 0),
        },
        "state": {
            "task_description": session_data.get("description", ""),
            "working_files": [ft["file"] for ft in session_data.get("files_touched", [])[:8]
                              if isinstance(ft, dict) and ft.get("file")],
            "decisions": session_data.get("decisions", []),
            "files_touched": session_data.get("files_touched", []),
            "context_notes": session_data.get("context_notes", []),
        },
    }

    snap_path = snap_dir / f"snap_{snap_id}.json"
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        log_hook_error("hook_auto_snapshot", exc)
        sys.exit(0)

    try:
        project_path = str(Path.cwd())
        port = _find_server_port(project_path)

        if port and _call_server(port, data):
            sys.exit(0)

        # Server not running or unreachable — fallback
        _fallback_snapshot(data)
    except Exception as exc:
        log_hook_error("hook_auto_snapshot", exc)

    sys.exit(0)


if __name__ == "__main__":
    main()
