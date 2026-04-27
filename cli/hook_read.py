#!/usr/bin/env python3
"""PostToolUse/AfterTool hook for Read/read_file/SearchText/FindFiles.

Checks if the model used required C3 tools before standard discovery or reads:
- Code/docs files: c3_search / c3_compress(mode='map') / c3_read
- Log/data files (.log/.txt/.jsonl): c3_filter(file_path='...')

If not, injects strong additionalContext guidance. Also queues the file
for async file memory indexing.

Supports both Claude Code (PostToolUse/Read) and Gemini CLI (AfterTool/read_file).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import emit_additional_context, get_tool_output, log_hook_error, normalize_tool_name  # noqa: E402

CODE_PRE_READ_TOOLS = {"c3_search", "c3_compress", "c3_read"}
DATA_PRE_READ_TOOLS = {"c3_extract", "c3_filter"}
C3_TOOLS = CODE_PRE_READ_TOOLS | DATA_PRE_READ_TOOLS
LOOKBACK = 30


def _check_c3_used(project_path: Path, rel_path: str, allowed_tools=None) -> bool:
    """Check activity log for recent C3 tool calls targeting this file."""
    log_file = project_path / ".c3" / "activity_log.jsonl"
    if not log_file.exists():
        return False
    allowed = set(allowed_tools or C3_TOOLS)

    try:
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return False

    for line in reversed(lines[-LOOKBACK:]):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if entry.get("type") != "tool_call":
            continue

        tool = entry.get("tool", "")
        if tool not in allowed:
            continue

        args = entry.get("args", {})
        tool_path = args.get("file_path", "") or args.get("query", "")
        tool_path_norm = tool_path.replace("\\", "/").strip("/")
        rel_norm = rel_path.replace("\\", "/").strip("/")

        if rel_norm in tool_path_norm or tool_path_norm in rel_norm:
            return True

        if tool == "c3_search":
            filename = Path(rel_path).name
            if filename and filename in tool_path:
                return True

    return False


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)

        # Normalize Gemini tool names to Claude equivalents
        tool_name = normalize_tool_name(data.get("tool_name", ""))
        if tool_name not in ("Read", "FindFiles", "SearchText"):
            return

        result_text, is_gemini = get_tool_output(data)
        if not result_text or not isinstance(result_text, str):
            return

        project_path = Path.cwd()

        if tool_name == "FindFiles":
            if not _check_c3_used(project_path, "", allowed_tools=["c3_search"]):
                emit_additional_context(
                    "\u26a0\ufe0f [c3:enforce] Standard file discovery is fallback-only in this project.\n\n"
                    "Before `FindFiles`, use a core C3 discovery tool:\n"
                    "  c3_search(query=\"<your pattern>\", action=\"files\")\n\n"
                    "Use `FindFiles` only after a C3 result narrows the target.",
                    is_gemini,
                )
            return

        if tool_name == "SearchText":
            if not _check_c3_used(project_path, "", allowed_tools=["c3_search", "c3_compress", "c3_read"]):
                emit_additional_context(
                    "\u26a0\ufe0f [c3:enforce] Standard text search is fallback-only in this project.\n\n"
                    "Before `SearchText`, use a core C3 tool first:\n"
                    "  c3_search(query=\"<symbol or concept>\", action=\"code\")\n"
                    "  c3_compress(file_path=\"<candidate file>\", mode=\"map\")\n\n"
                    "Use `SearchText` only after C3 narrows the scope.",
                    is_gemini,
                )
            return

        # Read tool: extract file_path from tool_input (Claude: file_path, Gemini: path)
        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
        if not file_path:
            return

        line_count = result_text.count("\n") + 1

        try:
            rel_path = str(Path(file_path).resolve().relative_to(project_path.resolve()))
        except ValueError:
            rel_path = file_path
        rel_path = rel_path.replace("\\", "/")

        queue_path = project_path / ".c3" / "file_memory" / "_queue.txt"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(queue_path, "a", encoding="utf-8") as handle:
                handle.write(rel_path + "\n")
        except Exception:
            pass

        # Clear this file from edit-unlock pending list (Edit prerequisite satisfied)
        pending_path = project_path / ".c3" / "edit_unlock_pending.txt"
        try:
            if pending_path.exists():
                pending = set(
                    line.strip() for line in
                    pending_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
                # Match against both the raw file_path and the rel_path
                to_remove = set()
                fp_norm = file_path.replace("\\", "/").strip("/")
                rel_norm = rel_path.replace("\\", "/").strip("/")
                for p in pending:
                    p_norm = p.replace("\\", "/").strip("/")
                    if p_norm == fp_norm or p_norm == rel_norm or fp_norm.endswith(p_norm) or rel_norm.endswith(p_norm):
                        to_remove.add(p)
                if to_remove:
                    pending -= to_remove
                    pending_path.write_text(
                        "\n".join(sorted(pending)) + "\n" if pending else "",
                        encoding="utf-8",
                    )
        except Exception:
            pass

        ext = Path(rel_path).suffix.lower()
        code_and_doc_exts = {
            ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
            ".rb", ".c", ".cpp", ".h", ".cs", ".r", ".R",
            ".html", ".css", ".json", ".yaml", ".yml", ".sql", ".md",
        }
        data_exts = {".log", ".txt", ".jsonl"}

        if ext not in code_and_doc_exts and ext not in data_exts:
            return

        is_data_file = ext in data_exts
        required_tools = DATA_PRE_READ_TOOLS if is_data_file else CODE_PRE_READ_TOOLS
        if _check_c3_used(project_path, rel_path, allowed_tools=required_tools):
            return

        if is_data_file:
            emit_additional_context(
                f"\u26a0\ufe0f [c3:enforce] STOP. You read `{rel_path}` without running a core C3 data tool first.\n\n"
                "STRICT PREREQUISITE for `.log`/`.txt`/`.jsonl`:\n"
                f"  1. c3_filter(file_path=\"{rel_path}\", pattern=\"<optional pattern>\")\n"
                "  2. Read only the extracted signal if needed\n\n"
                "Use standard `Read` only after `c3_filter` narrows the result.",
                is_gemini,
            )
            return

        emit_additional_context(
            f"\u26a0\ufe0f [c3:enforce] STOP. You read `{rel_path}` ({line_count} lines) without using a core C3 tool first.\n\n"
            "Required workflow before standard `Read`:\n"
            f"  1. c3_search(query=\"{Path(rel_path).name}\", action=\"code\") or c3_compress(file_path=\"{rel_path}\", mode=\"map\")\n"
            f"  2. c3_read(file_path=\"{rel_path}\", symbols=['ClassName', 'func_name']) or c3_read(file_path=\"{rel_path}\", lines=[[start, end]])\n"
            "  3. Use standard `Read` only for a narrow follow-up if C3 output is insufficient\n\n"
            "Core C3 tools are mandatory here: `c3_search`, `c3_compress`, `c3_read`.",
            is_gemini,
        )
    except Exception as _e:
        log_hook_error("hook_read", _e)


if __name__ == "__main__":
    main()
