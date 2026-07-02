"""PostToolUse hook: signal agent-artifact writes for attributed capture.

Fires alongside the edit ledger on Edit/Write/NotebookEdit. When the touched
file classifies as an agent-affecting artifact (instruction docs, settings/
hooks, MCP configs, .claude skills/agents/commands — see
services/artifact_defs), appends a one-line pending signal to
.c3/agent_artifacts/pending.jsonl.

Performance: no hashing, no manifest lock — the ArtifactScanAgent consumes
signals asynchronously and attributes the resulting history events to
source='hook'. Silent hook: never emits user-visible output.
"""

import json
import sys
from pathlib import Path

# Add project root to path for imports
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from cli._hook_utils import get_tool_input_path, log_hook_error, normalize_tool_name  # noqa: E402
from services.artifact_defs import classify_path, note_pending_write  # noqa: E402


def run(payload: dict, project_path: Path | None = None) -> None:
    """Core logic — importable by the dispatcher and tests. Always None."""
    tool_name = normalize_tool_name(payload.get("tool_name", ""))
    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        return None

    file_path = get_tool_input_path(payload)
    if not file_path:
        return None

    if project_path is None:
        project_path = Path.cwd()
    if not (project_path / ".c3").exists():
        return None  # Not a C3 project

    try:
        rel = str(Path(file_path).resolve().relative_to(project_path.resolve()))
    except (ValueError, OSError):
        return None  # outside the project → not a project artifact

    if classify_path(rel) is None:
        return None

    note_pending_write(project_path, rel, "hook",
                       session_id=payload.get("session_id", "") or "",
                       tool=tool_name)
    return None


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        run(json.loads(raw))
    except Exception as _e:
        log_hook_error("hook_artifact", _e)


if __name__ == "__main__":
    main()
