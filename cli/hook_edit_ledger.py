"""PostToolUse hook: auto-log Edit/Write tool calls to the Edit Ledger.

Intercepts Edit and Write tool calls, extracts the file path, and appends
an entry to .c3/edit_ledger.jsonl — same format as EditLedger.log_edit().

Performance: logs immediately with git_pending=True (no subprocess).
Git info and syntax validation are enriched asynchronously by
EditLedgerEnricherAgent running in the MCP server background.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path for imports
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from cli._hook_utils import get_tool_input_path, log_hook_error, normalize_tool_name  # noqa: E402
from core.config import load_hybrid_config  # noqa: E402

EDITABLE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".c", ".cpp", ".h", ".cs", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".sql", ".md", ".txt",
    ".sh", ".bat", ".ps1", ".r",
}

# How many tail lines to scan for version/seq (avoids full-file parse)
_TAIL_SCAN = 50


def _tail_lines(ledger_file: Path, n: int = _TAIL_SCAN) -> list[str]:
    """Read the last n non-empty lines from the ledger file efficiently."""
    try:
        raw = ledger_file.read_bytes()
    except Exception:
        return []
    lines = []
    pos = len(raw)
    while pos > 0 and len(lines) < n:
        nl = raw.rfind(b"\n", 0, pos - 1)
        chunk = raw[nl + 1:pos]
        if chunk.strip():
            lines.append(chunk.decode("utf-8", errors="replace"))
        pos = nl if nl >= 0 else 0
    return list(reversed(lines))


def _next_version(ledger_file: Path, rel_path: str) -> str:
    """Get next version for a file by scanning only the tail of the ledger."""
    max_v = 0
    for line in _tail_lines(ledger_file, _TAIL_SCAN):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("file") == rel_path:
            v_str = entry.get("version", "v0")
            try:
                max_v = max(max_v, int(v_str.lstrip("v")))
            except (ValueError, AttributeError):
                pass
    # If nothing found in tail, file might have older entries — do a quick check
    if max_v == 0 and ledger_file.exists():
        try:
            for line in ledger_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("file") == rel_path:
                    v_str = entry.get("version", "v0")
                    try:
                        max_v = max(max_v, int(v_str.lstrip("v")))
                    except (ValueError, AttributeError):
                        pass
        except Exception:
            pass
    return f"v{max_v + 1}"


def _next_seq(ledger_file: Path, now: datetime) -> int:
    """Sequence number — scan only tail lines for same-second collisions."""
    prefix = f"edit_{now.strftime('%Y%m%d_%H%M%S')}_"
    max_seq = 0
    for line in _tail_lines(ledger_file, 10):  # Same-second entries are always recent
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        eid = entry.get("id", "")
        if eid.startswith(prefix):
            try:
                max_seq = max(max_seq, int(eid[len(prefix):]))
            except ValueError:
                pass
    return max_seq + 1



def _extract_summary(tool_name: str, tool_input: dict) -> str:
    """Build a short summary from tool input."""
    if tool_name == "Edit":
        old = (tool_input.get("old_string") or "")[:60]
        new = (tool_input.get("new_string") or "")[:60]
        if old and new:
            return f"Replaced: {old!r} → {new!r}"
        return "Edit"
    elif tool_name == "Write":
        return "File written"
    return "Modified"


def _detect_change_type(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Write":
        return "created" if tool_input.get("_is_new") else "modified"
    return "modified"


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        tool_name = normalize_tool_name(data.get("tool_name", ""))

        if tool_name not in ("Edit", "Write", "NotebookEdit"):
            return

        file_path = get_tool_input_path(data)
        if not file_path:
            return

        # Filter to editable extensions
        if Path(file_path).suffix.lower() not in EDITABLE_EXTS:
            return

        project_path = Path.cwd()
        c3_dir = project_path / ".c3"
        if not c3_dir.exists():
            return  # Not a C3 project

        # Load config and check if edit ledger is enabled
        config = load_hybrid_config(str(project_path))
        ledger_cfg = config.get("edit_ledger", {})
        if not ledger_cfg.get("enabled", True):
            return
        tracking_level = ledger_cfg.get("tracking_level", "standard")

        ledger_file = c3_dir / "edit_ledger.jsonl"

        # Make file path relative
        try:
            rel = str(Path(file_path).resolve().relative_to(project_path.resolve()))
        except ValueError:
            rel = file_path
        rel = rel.replace("\\", "/")

        now = datetime.now(timezone.utc)
        tool_input = data.get("tool_input", {})
        change_type = _detect_change_type(tool_name, tool_input)

        # Git info is enriched asynchronously by EditLedgerEnricherAgent.
        # Mark as pending so the enricher knows to process this entry.
        git_pending = tracking_level != "minimal"

        entry = {
            "id": f"edit_{now.strftime('%Y%m%d_%H%M%S')}_{_next_seq(ledger_file, now):03d}",
            "timestamp": now.isoformat(),
            "session_id": "",
            "file": rel,
            "change_type": change_type,
            "summary": change_type if tracking_level == "minimal" else _extract_summary(tool_name, tool_input),
            "lines_changed": None,
            "version": _next_version(ledger_file, rel),
            "git": {},
            "diff_summary": "",
            "git_pending": git_pending,
            "tags": ["auto"] if ledger_cfg.get("auto_tag", True) else [],
        }

        # Detailed mode: include code snippets for richer diffs
        if tracking_level == "detailed":
            detail = {}
            old_str = tool_input.get("old_string")
            new_str = tool_input.get("new_string")
            if old_str is not None:
                detail["old_string"] = old_str[:200]
            if new_str is not None:
                detail["new_string"] = new_str[:200]
            if detail:
                entry["detail"] = detail

        with open(ledger_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        print(f"[c3:ledger] {rel} {entry['version']} auto-logged. "
              f"Call c3_edits(action='log', file='{rel}', summary='...', tags='...') "
              f"to add a semantic summary.")

    except Exception as _e:
        log_hook_error("hook_edit_ledger", _e)


if __name__ == "__main__":
    main()
