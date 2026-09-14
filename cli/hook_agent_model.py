"""PreToolUse sub-hook: a native subagent with no ``model`` gets one tier below its parent.

Routed by cli/hook_dispatch.py for ``Agent`` / ``Task`` on Claude Code only;
the policy lives in services/agent_downshift.py. Returns
``hookSpecificOutput.updatedInput`` (the whole tool_input with ``model`` set),
or None to leave the call as it is. Every decision on a blank call — applied
or not, and why — is one ``agent_downshift`` telemetry row, so coverage is
measurable (how many calls fell through as ``parent_unknown``).

Opt out per project with ``delegate.agent_downshift: "off"``, or for a shell
with ``C3_AGENT_DOWNSHIFT=0``. Never raises into the tool call.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import find_project, log_hook_error  # noqa: E402


def run(payload: dict, project_path: Path | None = None):
    try:
        return _run(payload, project_path)
    except Exception as exc:  # a model choice is never worth a failed tool call
        log_hook_error("hook_agent_model", exc)
        return None


def _run(payload: dict, project_path):
    from services import agent_downshift as ad

    if str(payload.get("tool_name") or "") not in ad.SUBAGENT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if str(tool_input.get("model") or "").strip():
        return None  # the caller chose
    if os.environ.get("C3_AGENT_DOWNSHIFT", "").strip().lower() in ("0", "off", "false", "no"):
        return None
    project = find_project(payload, project_path)
    if project is None:
        return None
    from core.config import load_delegate_config

    cfg = load_delegate_config(str(project))
    stype = str(tool_input.get("subagent_type") or "general-purpose")
    if ":" in stype:
        return None  # plugin agent: its definition is not ours to second-guess
    if ad.definition_sets_model(stype, project):
        return None
    parent, source = ad.parent_model(payload, project)
    target, reason = ad.resolve(stype, parent, cfg)
    try:
        from services.telemetry import append_telemetry_record
        append_telemetry_record(project, {"tool": "agent_downshift" if target else "agent_downshift_skip",
                                          "detail": {
            "subagent_type": stype, "parent_model": parent, "parent_source": source,
            "model": target, "reason": reason, "applied": bool(target)}})
    except Exception:
        pass
    if not target:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "updatedInput": {**tool_input, "model": target}}}
