"""Translate Grok Build hook payloads into the shape C3's sub-hooks read.

Grok sends snake_case aliases for its top-level keys (tool_name, tool_input,
tool_response, session_id, transcript_path), but the VALUES are Grok's own:
native tool names, native input keys, and tagged result objects. Pinned by
tests/fixtures/grok/hook_payloads_1.0.30.json:

    read_file            {"target_file"}                      -> ReadFile.FileContent.content
    grep                 {"pattern", ...}                     -> GrepSearch.stdout (byte list)
    list_dir             {"target_directory"}                 -> ListDir.Content.content
    write                {"file_path", "content"}             -> SearchReplace.EditsApplied
    search_replace       {"file_path", "old_string", "new_string"}
    run_terminal_command {"command", "description"}           -> Bash.output_for_prompt
    <server>__<tool>     {"tool_name", "tool_input": {...}}   -> MCP.output.{OkayOutput|...}

Sub-hooks key on Claude names (Read/Edit/Write/Bash/Grep/Glob) and on
``mcp__c3__c3_*``, so the translation happens once, before any sub-hook runs.
"""
import json
from pathlib import Path

GROK_TOOL_NAMES = {
    "read_file": "Read",
    "hashline_read": "Read",
    "write": "Write",
    "search_replace": "Edit",
    "hashline_edit": "Edit",
    "run_terminal_command": "Bash",
    "grep": "Grep",
    "hashline_grep": "Grep",
    "list_dir": "Glob",
}

_INPUT_ALIASES = {"target_file": "file_path", "target_directory": "path"}


def is_grok_payload(data) -> bool:
    """Grok's envelope carries camelCase hookEventName plus workspaceRoot."""
    return isinstance(data, dict) and "hookEventName" in data and "workspaceRoot" in data


def _absolute(value: str, cwd) -> str:
    if not value or not cwd:
        return value
    path = Path(value)
    return value if path.is_absolute() else str(Path(str(cwd)) / path)


def _decode_bytes(value) -> str:
    if isinstance(value, list) and all(isinstance(b, int) for b in value):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except ValueError:
            return ""
    return value if isinstance(value, str) else ""


def response_text(response) -> str:
    """Flatten a Grok tagged tool result into the text the model read."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response  # an oversized result reaches hooks as a plain string
    if not isinstance(response, dict):
        return json.dumps(response)
    kind = response.get("type")
    if kind == "MCP":
        output = response.get("output")
        if isinstance(output, dict) and len(output) == 1:
            (variant, value), = output.items()
            text = value if isinstance(value, str) else json.dumps(value)
            # Grok names the success arm OkayOutput; anything else is a failure
            # and must read as one (hook_c3_signal refuses failed c3 calls).
            return text if variant == "OkayOutput" else f"Error: {text}"
        return output if isinstance(output, str) else json.dumps(output)
    if kind == "Bash":
        return response.get("output_for_prompt") or _decode_bytes(response.get("output"))
    if kind == "GrepSearch":
        return _decode_bytes(response.get("stdout"))
    for value in response.values():
        if isinstance(value, dict):
            for key in ("content", "tool_output_for_prompt", "output_for_prompt"):
                if isinstance(value.get(key), str):
                    return value[key]
    for key in ("content", "output_for_prompt", "tool_output_for_prompt"):
        if isinstance(response.get(key), str):
            return response[key]
    return json.dumps(response)


def translate(payload: dict) -> dict:
    """Return a Claude-shaped copy of a Grok hook payload (the original is kept intact)."""
    raw_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    raw_input = payload.get("tool_input", payload.get("toolInput"))
    tool_input = dict(raw_input) if isinstance(raw_input, dict) else {}
    cwd = payload.get("cwd") or payload.get("workspaceRoot")
    out = {**payload, "_c3_host": "grok"}
    if not raw_name:
        return out

    if "__" in raw_name and not raw_name.startswith("mcp__"):
        name = "mcp__" + raw_name
        inner = tool_input.get("tool_input")
        if tool_input.get("tool_name") == raw_name and isinstance(inner, dict):
            tool_input = dict(inner)  # use_tool wraps the real arguments
    else:
        name = GROK_TOOL_NAMES.get(raw_name, raw_name)
        for grok_key, claude_key in _INPUT_ALIASES.items():
            if grok_key in tool_input and claude_key not in tool_input:
                tool_input[claude_key] = tool_input[grok_key]
        for key in ("file_path", "path"):
            if isinstance(tool_input.get(key), str):
                tool_input[key] = _absolute(tool_input[key], cwd)
        if raw_name in ("search_replace", "hashline_edit") and tool_input.get("old_string") == "":
            name = "Write"  # an empty old_string creates the file

    out.update({"tool_name": name, "tool_input": tool_input, "_c3_original_tool": raw_name})
    response = payload.get("tool_response", payload.get("toolResult"))
    if response is not None:
        out["tool_response"] = response_text(response)
    return out
