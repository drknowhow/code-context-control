"""Grok Build installation. Never edits ~/.grok/config.toml or Grok's trust store.

Grok Build (xAI's `grok` CLI) reads project MCP servers from
``.grok/config.toml`` and project hooks from ``.grok/hooks/*.json``, but only
after the folder is trusted (``grok --trust`` or ``/hooks-trust``). C3 writes
the project files and reports trust; granting it stays the user's decision.
The hook contract below was captured from grok 1.0.30
(tests/fixtures/grok/hook_payloads_1.0.30.json).
"""
import base64
import json
import os
import subprocess
from pathlib import Path

import tomlkit  # tomllib is 3.11+; C3 supports 3.10
from tomlkit.exceptions import TOMLKitError

CONTRACT_VERSION = "1.0.30"

GROK_WORKFLOW_NOTE = """## Grok Build

C3 tools are MCP tools. In Grok they sit behind `search_tool` / `use_tool`;
their qualified names are `c3__c3_search`, `c3__c3_read`, `c3__c3_edit`, and so
on. Discover one with search_tool, then call it with use_tool.

Project MCP servers, hooks and this file load only after the folder is trusted:
run `grok --trust` once in the project (or `/hooks-trust` inside Grok).
C3's hooks live in `.grok/hooks/c3.json`; `c3 doctor --ide grok` reports
whether the CLI, hooks, MCP entry and folder trust are in place.
Agent-config writes (.grok/config.toml, .grok/hooks, AGENTS.md) must go through c3_edit.
"""

# Grok event -> hook_dispatch route. UserPromptSubmit is deliberately absent:
# Grok discards an allowing prompt hook's stdout, so recall context could never
# reach the model and the hook would only add latency to every prompt.
HOOK_EVENTS = {
    "PreToolUse": "pretool",
    "PostToolUse": "posttool",
    "Stop": "stop",
    "SessionStart": "start",
    "PreCompact": "compact",
    "SessionEnd": "end",
}

HOOKS_FILE = Path(".grok") / "hooks" / "c3.json"


def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")


def hook_command(interpreter: str, dispatcher: Path, route: str, target: Path,
                 windows: bool | None = None) -> str:
    from core.hook_command import posix_command, powershell_encoded_command
    argv = [interpreter, str(dispatcher), route, "--host", "grok", "--project", str(target)]
    if windows is None:
        windows = os.name == "nt"
    return powershell_encoded_command(argv) if windows else posix_command(argv)


def _is_c3_command(command: str) -> bool:
    if "hook_dispatch.py" in command:
        return True
    marker = "-EncodedCommand "
    if marker not in command:
        return False
    try:
        script = base64.b64decode(command.split(marker, 1)[1].strip()).decode("utf-16-le")
    except (ValueError, UnicodeDecodeError):
        return False
    return "hook_dispatch.py" in script


def install_hooks(target: Path, interpreter: str, dispatcher: Path) -> dict:
    """Write .grok/hooks/c3.json. C3 owns the whole file, so it is replaced, not merged."""
    path = target / HOOKS_FILE
    hooks = {}
    for event, route in HOOK_EVENTS.items():
        hooks[event] = [{"matcher": "*", "hooks": [{
            "type": "command",
            "command": hook_command(interpreter, dispatcher, route, target),
            "timeout": 10 if event == "SessionEnd" else 30,
        }]}]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    trust = trust_state(target)
    return {"supported": True, "installed": True, "trusted": trust["trusted"],
            "activation": activation_hint(trust)}


def remove_hooks(target: Path) -> bool:
    path = target / HOOKS_FILE
    if not path.exists():
        return False
    path.unlink()
    hooks_dir = path.parent
    try:
        if not any(hooks_dir.iterdir()):
            hooks_dir.rmdir()
    except OSError:
        pass
    return True


def hook_state(target: Path) -> dict:
    path = target / HOOKS_FILE
    state = {"supported": True, "installed": False, "path": str(path)}
    if not path.exists():
        return state
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        state["installed"] = all(
            any(_is_c3_command(h.get("command", ""))
                for group in hooks.get(event, []) for h in group.get("hooks", []))
            for event in ("PreToolUse", "PostToolUse"))
        state["events"] = sorted(hooks)
    except (OSError, ValueError, TypeError, AttributeError):
        state["error"] = "Unreadable or invalid hook configuration"
    return state


def _canonical(path) -> str:
    return os.path.normcase(os.path.realpath(str(path))).rstrip("\\/")


def trust_state(target: Path) -> dict:
    """Read Grok's folder-trust store. Read-only: C3 never grants trust."""
    store = grok_home() / "trusted_folders.toml"
    result = {"trusted": "unknown", "store": str(store)}
    if os.environ.get("GROK_FOLDER_TRUST", "").strip() == "0":
        return {**result, "trusted": True, "reason": "GROK_FOLDER_TRUST=0 disables the trust gate"}
    if not store.exists():
        return {**result, "trusted": False, "reason": "no trust store yet"}
    try:
        folders = tomlkit.parse(store.read_text(encoding="utf-8")).unwrap().get("folders", {})
    except (OSError, ValueError, TOMLKitError) as exc:
        return {**result, "reason": type(exc).__name__}
    project = _canonical(target)
    for folder, entry in folders.items():
        if not isinstance(entry, dict):
            continue
        root = _canonical(folder)
        if project == root or project.startswith(root + os.sep):
            return {**result, "trusted": bool(entry.get("trusted")), "folder": folder}
    return {**result, "trusted": False, "reason": "folder not in trust store"}


def activation_hint(trust: dict) -> str:
    if trust.get("trusted") is True:
        return "Folder is trusted; restart Grok in this project to load C3."
    return ("Run `grok --trust` once in this folder (or /hooks-trust inside Grok): "
            "project MCP servers, hooks and AGENTS.md stay inactive until then.")


def probe_cli() -> dict:
    """Version probe at zero model cost; never infer health from PATH alone."""
    import shutil

    from cli.tools.delegate import _probe_cli_version
    executable = shutil.which("grok")
    if not executable:
        return {"available": False, "reason": "Grok CLI not found"}
    try:
        probed = _probe_cli_version(executable, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": type(exc).__name__}
    if probed is None:
        return {"available": False, "reason": "CLI probe timed out"}
    out, err, code = probed
    version = (out or err or "").strip()[:120]
    result = {"available": code == 0, "version": version, "contract_version": CONTRACT_VERSION}
    number = version.split()[1] if version.startswith("grok ") and len(version.split()) > 1 else ""
    if number and number.split(".")[0] != CONTRACT_VERSION.split(".")[0]:
        result["warning"] = (f"Grok {number} differs in major version from the hook contract "
                             f"C3 was built against ({CONTRACT_VERSION}); re-check hooks.")
    return result


def diagnose(target: Path) -> dict:
    target = target.resolve()
    trust = trust_state(target)
    result = {"project": str(target), "cli": probe_cli(), "hooks": hook_state(target), "trust": trust}
    config_path = target / ".grok" / "config.toml"
    try:
        config = tomlkit.parse(config_path.read_text(encoding="utf-8")).unwrap()
        server = config.get("mcp_servers", {}).get("c3", {})
        result["mcp"] = {"configured": bool(server), "enabled": server.get("enabled", True),
                         "startup_timeout_sec": server.get("startup_timeout_sec", 30),
                         "explicit_host": "--host" in server.get("args", []),
                         "check": "grok mcp doctor c3 --json"}
    except (OSError, ValueError, TypeError, AttributeError, TOMLKitError) as exc:
        result["mcp"] = {"configured": False, "reason": type(exc).__name__}
    result["activation"] = activation_hint(trust)
    return result
