"""Codex-specific installation. Never edits another host's configuration."""
import json
import os
import subprocess
from pathlib import Path

CODEX_WORKFLOW = """# C3 — Codex workflow

Use C3 for repository intelligence and audited changes:
1. Recall relevant project facts with c3_memory; inspect .c3/MAP.md.
2. Find candidates with c3_search; map with c3_compress(mode='map'), then
   read exact symbols or lines with c3_read. Check shared changes with c3_impact.
3. Edit with c3_edit, validate with c3_validate, and record decisions with
   c3_session(action='log'). Preserve unrelated work.
4. Use c3_shell for commands and c3_shell_job for long work; outputs already
   have a safety budget. Use c3_filter for other long logs.
5. Snapshot with c3_session before clearing or handing off unfinished work.

If a C3 tool fails or its scope is insufficient, explain the limitation and
use a targeted native fallback. Never bypass an access denial or masked path.
A [c3-access:confirm] response is a hold: follow its S8 instructions, wait on
the existing request with c3_override, and retry only after approval. A pending
request is not a denial. Agent-config writes must go through c3_edit.

Codex lifecycle hooks require a supported client, installed .codex/hooks.json,
and user trust in Codex. Installation does not establish that hooks are active.
Native apply_patch and Bash hooks cover supported local tool paths only;
C3's server-side guards remain authoritative for C3 calls.

MCP configuration uses [mcp_servers.c3] in project-scoped .codex/config.toml. Project configuration
and hooks must be trusted by Codex. Use `c3 install-mcp --ide codex` to update;
add `--global-fallback` only when a machine-wide fallback is wanted.
Detailed C3 actions are documented in MCP tool descriptions and `c3 --help`.
"""


def probe_cli() -> dict:
    """Inspect CLI flags at zero model cost; never infer health from PATH alone."""
    import shutil

    from cli.tools.delegate import _probe_cli_version
    executable = shutil.which("codex")
    if not executable:
        return {"available": False, "hooks_supported": "unknown", "reason": "Codex CLI not found"}
    try:
        version = _probe_cli_version(executable, timeout=10)
        help_result = _probe_cli_version(executable, timeout=10, args=["exec", "--help"])
        if version is None or help_result is None:
            return {"available": False, "hooks_supported": "unknown", "reason": "CLI probe timed out"}
        help_text, _, help_code = help_result
        supported = help_code == 0 and all(flag in help_text for flag in ("--json", "--sandbox"))
        return {"available": version[2] == 0 and supported,
                "version": version[0][:120], "delegate_arguments_supported": supported,
                "hooks_supported": True if "--dangerously-bypass-hook-trust" in help_text else "unknown"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "hooks_supported": "unknown", "reason": type(exc).__name__}


def hook_state(target: Path, host_session_id: str = "") -> dict:
    import hashlib
    path = target / ".codex/hooks.json"
    state = {"supported": True, "installed": False, "active": "unknown"}
    if not path.exists():
        return state
    try:
        raw = path.read_bytes()
        hooks = json.loads(raw).get("hooks", {})
        state["installed"] = all(any("hook_dispatch.py" in h.get("command", "")
            for group in hooks.get(event, []) for h in group.get("hooks", []))
            for event in ("PreToolUse", "PostToolUse"))
        if host_session_id:
            key = hashlib.sha256(host_session_id.encode()).hexdigest()
            observed = target / ".c3/host_sessions/codex" / (key + ".events.json")
            data = json.loads(observed.read_text(encoding="utf-8")) if observed.exists() else {}
            if data.get("config_hash") == hashlib.sha256(raw).hexdigest() and data.get("host_session_id") == host_session_id:
                state["active"] = "observed_this_session"
                state["last_event"] = data.get("event")
    except (OSError, ValueError, TypeError, AttributeError):
        state["error"] = "Unreadable or invalid hook configuration/state"
    return state


def diagnose(target: Path) -> dict:
    import tomlkit

    from core.host import resolve_host
    result = {"project": str(target.resolve()), "cli": probe_cli(),
              "hooks": hook_state(target, resolve_host(str(target), "codex").host_session_id)}
    config_path = target / ".codex/config.toml"
    try:
        config = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        server = config.get("mcp_servers", {}).get("c3", {})
        result["mcp"] = {"configured": bool(server), "enabled": server.get("enabled", True),
                         "startup_timeout_sec": server.get("startup_timeout_sec", 10),
                         "tool_timeout_sec": server.get("tool_timeout_sec", 60),
                         "explicit_host": "--host" in server.get("args", []),
                         "connection": "not tested; restart the client if its transport is closed"}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result["mcp"] = {"configured": False, "reason": type(exc).__name__}
    result["activation"] = "Trust project configuration and review hooks in Codex; installation never bypasses trust."
    return result


def install_hooks(target: Path, interpreter: str, dispatcher: Path) -> dict:
    path = target / ".codex" / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    # One dispatcher per event preserves ordering inside C3's guard chain.
    events = {"PreToolUse": "pretool", "PostToolUse": "posttool",
              "UserPromptSubmit": "prompt", "Stop": "stop",
              "SessionStart": "start", "PreCompact": "compact",
              "SessionEnd": "end"}
    for event, route in events.items():
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get("hooks", [])
                    if h.get("statusMessage") != "C3 lifecycle"
                    and "hook_dispatch.py" not in h.get("command", "")]
            if kept:
                groups.append({**group, "hooks": kept})
        # Encode a PowerShell invocation so cmd.exe cannot expand metacharacters
        # or %variables% in project/interpreter paths. stdin remains the payload.
        argv = [interpreter, str(dispatcher), route, "--host", "codex", "--project", str(target)]
        import base64
        import shlex
        command = shlex.join(argv)
        script = "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv) + "; exit $LASTEXITCODE"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command_windows = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded
        groups.append({"matcher": ".*", "hooks": [{
            "type": "command", "command": command,
            "commandWindows": command_windows, "timeout": 3 if event == "SessionEnd" else 30,
            "statusMessage": "C3 lifecycle"}]})
        hooks[event] = groups
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"supported": True, "installed": True, "active": "unknown",
            "activation": "Review and trust the hooks in Codex, then restart the session."}
