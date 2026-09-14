"""c3_delegate — LLM task offload via Ollama (local) or Codex CLI (cloud).

Absorbs former c3_intelligence routing logic internally.
Supports task_type='available' for zero-cost Ollama status check.
Supports backend='codex' for OpenAI Codex CLI delegation.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from core import count_tokens
from services import access_guard
from services.circuit_breaker import CircuitBreaker
from services.win_subprocess import harden_win_argv

log = logging.getLogger(__name__)


def _log_progress(svc, message):
    """Emit progress notification if callback is set."""
    cb = getattr(svc, "_agent_progress_cb", None)
    if cb:
        try:
            cb(message)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _kill_proc_tree(proc):
    """Kill a subprocess and its entire process tree."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _communicate_with_heartbeat(proc, timeout=45, idle_timeout=15, stdin_text=None):
    """communicate() replacement with idle-activity watchdog.

    Monitors both stdout and stderr for activity. If neither stream produces
    output for idle_timeout seconds, kills the process early (catches MCP startup
    hangs) without killing a backend that streams its answer only on stdout.
    Also enforces total timeout.

    Returns (stdout, stderr, status) where status is 'ok', 'timeout', or 'idle_timeout'.
    """
    import threading

    stdout_parts = []
    stderr_parts = []
    last_activity = [time.time()]

    def _read_stream(stream, parts, track_activity=False):
        try:
            for line in stream:
                parts.append(line)
                if track_activity:
                    last_activity[0] = time.time()
        except (ValueError, OSError):
            pass

    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_parts, True), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(proc.stderr, stderr_parts, True), daemon=True)
    t_out.start()
    t_err.start()
    if stdin_text is not None:
        def _feed():
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        threading.Thread(target=_feed, daemon=True).start()

    deadline = time.time() + timeout
    status = "ok"
    while proc.poll() is None:
        now = time.time()
        if now >= deadline:
            _kill_proc_tree(proc)
            status = "timeout"
            break
        if idle_timeout and (now - last_activity[0]) > idle_timeout:
            _kill_proc_tree(proc)
            status = "idle_timeout"
            break
        time.sleep(0.5)

    t_out.join(timeout=3)
    t_err.join(timeout=3)
    return "".join(stdout_parts), "".join(stderr_parts), status


def _popen_kwargs():
    """Platform-specific Popen kwargs for clean subprocess management."""
    kwargs = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    return kwargs


def _probe_cli_version(exe: str, timeout: int = 10, args: list[str] | None = None):
    """Run ``<exe> --version`` without subprocess.run's timeout footgun.

    ``subprocess.run(cmd, capture_output=True, timeout=N)`` reads as safe and
    is not. When the timeout fires on Windows, CPython's own handler kills the
    direct child and then calls ``process.communicate()`` a *second* time with
    **no timeout** (the ``_mswindows`` branch of ``run()`` in Lib/subprocess.py).
    That second call joins the stdout/stderr reader threads, which never see
    EOF while any surviving grandchild still holds the pipe write-ends — so
    ``run()`` blocks forever inside its own timeout handler. Observed wedging
    the c3-delegate-prewarm thread for 10h and leaking its two reader threads,
    so delegate health checks never completed and every first c3_agent call
    paid full preflight.

    Popen + a process-*tree* kill + a bounded communicate() in ``finally``
    closes the write-ends no matter which way we leave. ``_kill_proc_tree``
    uses ``taskkill /T`` on Windows, so grandchildren die too — the exact case
    CPython's bare ``process.kill()`` misses.

    Returns (stdout, stderr, returncode), or None if it timed out.
    """
    timed_out = False
    proc = subprocess.Popen(
        harden_win_argv([exe, *(args if args is not None else ["--version"])]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
        **_popen_kwargs(),
    )
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            return None
        return (out or "").strip(), (err or "").strip(), proc.returncode
    finally:
        if proc.poll() is None:
            _kill_proc_tree(proc)
        if timed_out:
            # Reap the reader threads with a bound. This is the call CPython
            # makes with no timeout at all; the bound is the whole fix.
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Codex CLI backend
# ---------------------------------------------------------------------------

# No "model" keys here on purpose: a pinned model name goes stale and breaks
# accounts that don't support it (ChatGPT-plan logins reject retired models
# outright). Resolution order: .c3 config codex_default_model → the user's own
# Codex CLI default (~/.codex/config.toml). Empty model = omit -m entirely.
CODEX_MODELS = {
    "review":   {"sandbox": "read-only",       "reasoning": "high"},
    "explain":  {"sandbox": "read-only",       "reasoning": "medium"},
    "improve":  {"sandbox": "read-only",       "reasoning": "high"},
    "diagnose": {"sandbox": "read-only",       "reasoning": "high"},
    "test":     {"sandbox": "workspace-write", "reasoning": "medium"},
    "summarize":{"sandbox": "read-only",       "reasoning": "low"},
    "docstring":{"sandbox": "read-only",       "reasoning": "low"},
    "ask":      {"sandbox": "read-only",       "reasoning": "medium"},
}

_codex_available: bool | None = None  # cached after first check

# ---------------------------------------------------------------------------
# Gemini CLI backend
# ---------------------------------------------------------------------------

GEMINI_MODELS = {
    "review":   {"model": "gemini-2.5-pro"},
    "explain":  {"model": "gemini-2.5-flash"},
    "improve":  {"model": "gemini-2.5-pro"},
    "diagnose": {"model": "gemini-2.5-pro"},
    "test":     {"model": "gemini-2.5-flash"},
    "summarize":{"model": "gemini-2.5-flash"},
    "docstring":{"model": "gemini-2.5-flash"},
    "ask":      {"model": "gemini-2.5-flash"},
}

_gemini_available: bool | None = None  # cached after first check


def _npm_global_bin() -> str:
    """Return the npm global bin directory (Windows: AppData/Roaming/npm)."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return os.path.join(appdata, "npm")
    return ""


def _ensure_npm_on_path() -> None:
    """Ensure npm global bin is on PATH so shutil.which() finds npm-installed CLIs."""
    npm_bin = _npm_global_bin()
    if npm_bin and npm_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = npm_bin + os.pathsep + os.environ.get("PATH", "")


def _which(name: str) -> str | None:
    """Resolve full path for a CLI name, ensuring npm global bin is on PATH."""
    _ensure_npm_on_path()
    return shutil.which(name)


def _is_gemini_on_path() -> bool:
    """Check if gemini CLI binary is on PATH."""
    return _which("gemini") is not None


# ---------------------------------------------------------------------------
# Claude Code CLI backend
# ---------------------------------------------------------------------------

_claude_available = None  # None=unknown, True=up, False=down


def _is_claude_on_path() -> bool:
    """Check if claude CLI binary is on PATH."""
    return _which("claude") is not None


def check_claude() -> dict:
    """Zero-cost health check for Claude CLI. Returns status dict."""
    global _claude_available
    exe = _which("claude")
    if not exe:
        _claude_available = False
        return {"status": "not_installed", "detail": "claude CLI not found on PATH"}
    try:
        probed = _probe_cli_version(exe, timeout=10)
        if probed is None:
            _claude_available = False
            return {"status": "timeout", "detail": "claude --version timed out (10s)"}
        out, err, code = probed
        if code == 0:
            _claude_available = True
            return {"status": "ok", "version": out}
        _claude_available = False
        return {"status": "error", "detail": err or f"exit {code}"}
    except Exception as e:
        _claude_available = False
        return {"status": "error", "detail": str(e)}


# Tiers, not model ids: a pinned id goes stale, an alias follows the CLI to
# the current model of that size. "default" omits --model, so the user's own
# Claude Code default answers. Overridable per project via
# delegate.claude_tier_models.
DELEGATE_TIERS = ("small", "medium", "large", "default")
CLAUDE_TIERS = DELEGATE_TIERS
CLAUDE_TIER_MODELS = {"small": "haiku", "medium": "sonnet", "large": "opus", "default": ""}
_TIER_ALIASES = {"parent": "default", "haiku": "small", "sonnet": "medium", "opus": "large"}

# The same-provider step down on the other hosts. Codex and Grok keep the
# account's own model (a pinned id goes stale — 2.132.0 measured the old
# codex default rejected outright by ChatGPT logins) and step reasoning
# effort instead; a project can still name a model per tier. Gemini steps
# model size. "default" changes nothing.
CODEX_TIER_REASONING = {"small": "low", "medium": "medium", "large": "high"}
GROK_TIER_EFFORT = {"small": "low", "medium": "medium", "large": "high"}
GEMINI_TIER_MODELS = {"small": "gemini-2.5-flash-lite", "medium": "gemini-2.5-flash",
                      "large": "gemini-2.5-pro"}

# backend='host': the backend of the provider the calling agent runs on.
HOST_BACKENDS = {"claude-code": "claude", "codex": "codex", "grok": "grok",
                 "antigravity": "gemini"}


def normalize_tier(tier: str, dcfg: dict, default_key: str = "") -> str:
    """A tier name (aliases folded), the configured default when empty, or ''
    when the name is not a tier."""
    raw = (tier or (dcfg.get(default_key) if default_key else "")
           or dcfg.get("default_tier") or "small")
    resolved = _TIER_ALIASES.get(str(raw).strip().lower(), str(raw).strip().lower())
    return resolved if resolved in DELEGATE_TIERS else ""


def host_provider(svc) -> str:
    """The provider the calling agent runs on.

    The runtime's ``ide_name`` first: the MCP server gets it from its own
    ``--host`` argument, which is the only reliable answer — a project's
    ``.c3/config.json`` names whichever IDE last ran ``install-mcp`` (this
    repository's says codex while Claude Code is connected). The environment
    and that config are the fallback for callers without a runtime.
    """
    provider = str(getattr(svc, "ide_name", "") or "").strip()
    if provider:
        try:
            from core.ide import normalize_ide_name
            return normalize_ide_name(provider)
        except Exception:
            return provider
    try:
        from core.host import resolve_host
        return resolve_host(str(getattr(svc, "project_path", "") or "")).provider
    except Exception:
        return ""


def host_backend(svc) -> tuple[str, str]:
    """(host provider, same-provider backend or '')."""
    provider = host_provider(svc)
    return provider, HOST_BACKENDS.get(provider, "")

# A model name reaches argv, so it must not look like a flag.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}$")

# Variables Claude Code sets in its own children; a delegate started from a
# Claude Code session must not look nested.
_CLAUDE_NESTING_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

_CLAUDE_DELEGATE_RULES = (
    "You are a delegate answering one bounded request for another agent. You "
    "have no tools and cannot open files, run commands or browse: everything "
    "you know about the code is in the text below. If that text is not enough "
    "to answer, say exactly what is missing instead of guessing."
)

_CLAUDE_SCOUT_RULES = (
    "You are a read-only scout answering one bounded request for another agent. "
    "You can use Read, Grep and Glob inside this repository and nothing else: no "
    "edits, no commands, no network. Look up only what the request needs, then "
    "answer concisely with file:line references. A file you are refused is off "
    "limits; do not try to reach it another way. If you cannot find the answer, "
    "say so instead of guessing."
)

SCOUT_TOOLS = "Read,Grep,Glob"

_CLAUDE_WORKER_RULES = (
    "You are a worker making one code change that a lead agent has already "
    "decided. You have Read, Grep, Glob, Edit and Write in this repository and "
    "nothing else: no commands, no tests, no network. Do exactly the change the "
    "task describes: no refactors, renames, reformatting or extras it did not "
    "ask for. Edit or create only paths in the write set; a refused path is off "
    "limits, do not reach it another way. Read a file before editing it and "
    "match its style. If the task is ambiguous, contradicts the code, or needs a "
    "file outside the write set, do the part you can do safely and report the "
    "rest instead of guessing. Make the edits first, then finish with a short "
    "report: each file changed and what changed, anything left undone and why, "
    "and any assumption the lead should check. The lead reviews your diff and "
    "runs the tests."
)


def _permission_pattern(glob: str, root: str) -> str:
    """An Access Guard glob as a Claude Code ``Read()`` permission pattern.

    Measured on Claude Code 2.1.270 (Windows): ``Read(**/x)`` and
    ``Read(./x)`` deny Grep hits too, an absolute ``Read(//C:/...)`` does
    not. So an absolute glob inside the project becomes project-relative, and
    one outside it is dropped — ``--restricted`` confines the file tools to
    the project anyway. ``root`` is the casefolded POSIX project path.
    """
    g = str(glob or "").strip()
    if not g:
        return ""
    if re.match(r"^[a-z]:/", g, re.IGNORECASE) or g.startswith("/"):
        prefix = root.rstrip("/") + "/"
        if g.casefold().startswith(prefix):
            return "./" + g[len(prefix):]
        return ""
    if "/" not in g:
        return "**/" + g
    if g.startswith("**/"):
        return g
    return "./" + (g[2:] if g.startswith("./") else g)


def scout_read_denies(project_path) -> list[str]:
    """``Read()`` deny patterns for every path a scout must not read.

    A deny rule, a confirm rule that holds reads, and every mask rule (a
    scout cannot be served a masked view). A PreToolUse hook sees a Grep's
    arguments, never its hits, so only these rules keep a repository-wide
    Grep out of a denied file: measured, a hook alone leaked a .env value.
    Raises ValueError when a rule scope is corrupt (fail closed).
    """
    rules, mask_rules, corrupt = access_guard.load_all(str(project_path))
    if corrupt:
        raise ValueError(f"Access Guard config is unreadable in scope(s) {', '.join(corrupt)}")
    globs = [r.glob for r in rules
             if r.kind == "deny" or (r.kind == "confirm" and getattr(r, "confirm_ops", "") == "all")]
    globs += [m.glob for m in mask_rules]
    root = Path(project_path).resolve().as_posix().casefold()
    patterns = []
    for glob in dict.fromkeys(globs):
        pattern = _permission_pattern(glob, root)
        if pattern:
            patterns.append(f"Read({pattern})")
    return patterns


def scout_settings(project_path) -> dict:
    """``--settings`` for a scout: guard-derived read denies plus C3's access
    guard as the PreToolUse hook (canonicalization, 8.3 names, symlinks on
    explicit paths). ``--restricted`` ignores every settings file, so these
    are the only hooks and permissions that run."""
    from cli._hook_utils import hook_command_arg

    hook = Path(__file__).resolve().parents[1] / "hook_access_guard.py"
    project = str(Path(project_path).resolve())
    command = (f"{hook_command_arg(sys.executable)} {hook_command_arg(str(hook))} "
               f"--project {hook_command_arg(project)}")
    return {
        "permissions": {"deny": scout_read_denies(project)},
        "hooks": {"PreToolUse": [{"matcher": "Read|Grep|Glob",
                                  "hooks": [{"type": "command", "command": command}]}]},
    }


def worker_edit_denies(project_path) -> list[str]:
    """``Edit()`` deny patterns: every deny, read_only, confirm and mask glob.

    A worker never gets a grant or files a request, so a confirm hold is a
    refusal for it. Deny beats allow in Claude Code, so a broad write set
    cannot reopen any of these. Raises ValueError on a corrupt rule scope.
    """
    rules, mask_rules, corrupt = access_guard.load_all(str(project_path))
    if corrupt:
        raise ValueError(f"Access Guard config is unreadable in scope(s) {', '.join(corrupt)}")
    globs = [r.glob for r in rules if r.kind in ("deny", "read_only", "confirm")]
    globs += [m.glob for m in mask_rules]
    root = Path(project_path).resolve().as_posix().casefold()
    patterns = []
    for glob in dict.fromkeys(globs):
        pattern = _permission_pattern(glob, root)
        if pattern:
            patterns.append(f"Edit({pattern})")
    return patterns


def worker_settings(project_path, write_globs, state_dir) -> dict:
    """``--settings`` for a write-mode worker (services/delegate_write).

    Edit() allow rules are the write set, rooted at the project (a bare name
    is that file at the root, never a basename match anywhere). Read and
    Edit denies come from Access Guard. The guard hook runs in worker mode on
    every file tool: it re-checks all of that on the canonical path and saves
    pre-images into ``state_dir``.
    """
    from cli._hook_utils import hook_command_arg

    hook = Path(__file__).resolve().parents[1] / "hook_access_guard.py"
    project = str(Path(project_path).resolve())
    command = (f"{hook_command_arg(sys.executable)} {hook_command_arg(str(hook))} "
               f"--project {hook_command_arg(project)} "
               f"--worker-state {hook_command_arg(str(state_dir))}")
    return {
        "permissions": {
            "allow": [f"Edit(./{g})" for g in write_globs],
            "deny": scout_read_denies(project) + worker_edit_denies(project),
        },
        "hooks": {"PreToolUse": [{"matcher": "Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit",
                                  "hooks": [{"type": "command", "command": command}]}]},
    }


_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def claude_effort_for(tier: str, dcfg: dict) -> str:
    """``--effort`` for a resolved tier: ``claude_effort`` overrides every
    tier, else ``claude_tier_effort[tier]``, else none (the CLI's default).
    An unknown level is ignored rather than passed to argv."""
    value = str(dcfg.get("claude_effort") or "").strip().lower()
    if not value:
        table = dcfg.get("claude_tier_effort") or {}
        value = str(table.get(tier) or "").strip().lower() if isinstance(table, dict) else ""
    return value if value in _EFFORT_LEVELS else ""


def claude_thinking_cap_for(tier: str, dcfg: dict) -> int | None:
    """``MAX_THINKING_TOKENS`` for a resolved tier, or None (no cap).

    ``claude_thinking_tokens`` overrides every tier, else
    ``claude_tier_thinking_tokens[tier]``. 0 turns thinking off. Unlike
    ``--effort`` (no measurable effect on Haiku 4.5), this is what moves a
    delegate's cost: one Haiku review went from 4,831 output tokens and 54 s
    uncapped to 1,186 and 12 s at 1024. A negative or non-integer value is
    ignored.
    """
    value = dcfg.get("claude_thinking_tokens")
    if value is None or value == "":
        table = dcfg.get("claude_tier_thinking_tokens") or {}
        value = table.get(tier) if isinstance(table, dict) else None
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return None
    return cap if cap >= 0 else None


def claude_default_tier_key(scout: bool, write: bool = False) -> str:
    """Which config key names the default Claude tier.

    A scout defaults to its own key (medium): measured 2026-09-14, a Haiku
    scout took 28 turns and $0.24 to find one function that Sonnet found in
    8 turns for $0.046; the eval's canary case showed the same (15 turns
    against 2). A write-mode worker has its own key too (see
    docs/delegate-write.md for the measurement behind its default).
    """
    if write:
        return "claude_write_default_tier"
    return "claude_scout_default_tier" if scout else "claude_default_tier"


def resolve_claude_tier(tier: str, model: str, dcfg: dict,
                        scout: bool = False, write: bool = False) -> tuple[str, str, str]:
    """(tier, model_arg, error). An explicit ``model`` wins over the tier.

    ``model_arg`` empty means omit --model. ``error`` is non-empty when the
    tier or model name is unusable; nothing is spawned then.
    """
    if model:
        if not _MODEL_NAME_RE.match(model):
            return "", "", f"model name {model!r} is not a Claude model alias or id"
        return "custom", model, ""
    resolved = normalize_tier(tier, dcfg, claude_default_tier_key(scout, write))
    if not resolved:
        return "", "", f"unknown tier {tier!r} (use one of {', '.join(CLAUDE_TIERS)})"
    table = {**CLAUDE_TIER_MODELS, **(dcfg.get("claude_tier_models") or {})}
    model_arg = str(table.get(resolved) or "")
    if model_arg and not _MODEL_NAME_RE.match(model_arg):
        return "", "", (f"delegate.claude_tier_models[{resolved!r}] = {model_arg!r} "
                         "is not a Claude model alias or id")
    return resolved, model_arg, ""


def _claude_cmd(exe: str, model: str, system_prompt: str, *, effort: str = "",
                max_budget_usd: float = 0.0, settings: dict | None = None,
                tools: str = SCOUT_TOOLS) -> list:
    """The locked-down headless argv. The prompt goes on stdin.

    --safe-mode: no CLAUDE.md, skills, plugins, hooks or MCP servers — but
    OAuth still works, so delegation stays on the user's subscription
    (--bare would read ANTHROPIC_API_KEY only). --strict-mcp-config with no
    --mcp-config: no MCP servers even from managed config. --tools "": no
    built-in tools, so the delegate cannot read, write or run anything.

    With ``settings`` (a scout) the run is --restricted instead: Read, Grep
    and Glob only, confined to the project, no user/project/local settings
    files, no CLAUDE.md, and the given --settings as the only permissions and
    hooks. --permission-mode dontAsk refuses anything not already allowed.
    A write-mode worker is the same run with ``tools`` adding Edit and Write,
    allowed only by the settings' Edit() rules.
    """
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence"]
    if settings is None:
        cmd += ["--safe-mode", "--strict-mcp-config", "--tools", ""]
    else:
        cmd += ["--restricted", "--strict-mcp-config", "--tools", tools,
                "--permission-mode", "dontAsk", "--settings", json.dumps(settings)]
    cmd += ["--system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if max_budget_usd and max_budget_usd > 0:
        cmd += ["--max-budget-usd", f"{float(max_budget_usd):g}"]
    return cmd


def _claude_env(thinking_cap: int | None = None) -> dict:
    env = _child_host_env("claude-code")
    for name in _CLAUDE_NESTING_VARS:
        env.pop(name, None)
    # The caller's own MAX_THINKING_TOKENS must not leak into a delegate: the
    # cap is decided per tier here, or not set at all.
    env.pop("MAX_THINKING_TOKENS", None)
    if thinking_cap is not None:
        env["MAX_THINKING_TOKENS"] = str(int(thinking_cap))
    return env


def _primary_model(model_usage: dict, requested: str) -> str:
    """Which modelUsage entry answered. Claude Code also spends a few hundred
    tokens of Haiku on housekeeping, so the requested alias wins when it
    appears in a key; otherwise the most expensive entry."""
    if not isinstance(model_usage, dict) or not model_usage:
        return ""
    names = list(model_usage)
    if requested:
        hits = [n for n in names if requested.lower() in n.lower()]
        if hits:
            return hits[0]

    def cost(name):
        entry = model_usage.get(name) or {}
        try:
            return float(entry.get("costUSD") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return max(names, key=cost)


def parse_claude_json(stdout: str, requested_model: str = "") -> tuple[str, bool, dict]:
    """(text, ok, stats) from ``claude -p --output-format json``."""
    raw = (stdout or "").strip()
    start = raw.find("{")
    try:
        data = json.loads(raw[start:]) if start >= 0 else None
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return "[claude:error] no JSON result on stdout", False, {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    model_usage = data.get("modelUsage") if isinstance(data.get("modelUsage"), dict) else {}
    stats: dict = {}
    for src, dst in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                     ("cache_read_input_tokens", "cache_read_tokens"),
                     ("cache_creation_input_tokens", "cache_write_tokens")):
        if usage.get(src) is not None:
            try:
                stats[dst] = int(usage[src])
            except (TypeError, ValueError):
                pass
    if data.get("total_cost_usd") is not None:
        try:
            stats["cost_usd"] = float(data["total_cost_usd"])
        except (TypeError, ValueError):
            pass
    if data.get("num_turns") is not None:
        try:
            stats["turns"] = int(data["num_turns"])
        except (TypeError, ValueError):
            pass
    primary = _primary_model(model_usage, requested_model)
    if primary:
        stats["model"] = primary
    if len(model_usage) > 1:
        stats["models_used"] = sorted(model_usage)
    if isinstance(data.get("permission_denials"), list) and data["permission_denials"]:
        stats["denials"] = data["permission_denials"]
    text = str(data.get("result") or "").strip()
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        reason = text or str(data.get("terminal_reason") or data.get("subtype") or "error")
        return f"[claude:error] {reason}", False, stats
    if not text:
        return "[claude:error] empty result", False, stats
    return text, True, stats


def _run_claude(prompt: str, system_prompt: str, model: str = "", *,
                timeout: int = 120, effort: str = "",
                max_budget_usd: float = 0.0, scout_project: str | None = None,
                settings: dict | None = None,
                thinking_cap: int | None = None,
                tools: str = SCOUT_TOOLS) -> tuple[str, bool, dict]:
    """Run the locked-down ``claude -p``.

    Tool-less (default): in a throwaway directory. Scout (``scout_project``
    and ``settings`` given): in the project, read-only, guarded.
    Returns (text, ok, stats). No idle watchdog: JSON output arrives in one
    piece at the end, so a healthy long answer is silent until it is done.
    """
    exe = _which("claude")
    if not exe:
        return "[claude:error] claude CLI not found on PATH", False, {}
    scout = scout_project is not None
    workdir = str(scout_project) if scout else tempfile.mkdtemp(prefix="c3-claude-")
    try:
        cmd = _claude_cmd(exe, model, system_prompt, effort=effort,
                          max_budget_usd=max_budget_usd,
                          settings=settings if scout else None, tools=tools)
        proc = subprocess.Popen(
            harden_win_argv(cmd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=workdir,
            env=_claude_env(thinking_cap), **_popen_kwargs(),
        )
        stdout, stderr, status = _communicate_with_heartbeat(
            proc, timeout=timeout, idle_timeout=0, stdin_text=prompt)
        if status == "timeout":
            return f"[claude:timeout] No response after {timeout}s", False, {}
        text, ok, stats = parse_claude_json(stdout, model)
        if ok and proc.returncode != 0:
            ok, text = False, f"[claude:error] exit code {proc.returncode}"
        err = (stderr or "").strip()
        if not ok and err:
            text = f"{text} — {err[-500:]}"
        return text, ok, stats
    except Exception as e:
        return f"[claude:error] {e}", False, {}
    finally:
        if not scout:
            shutil.rmtree(workdir, ignore_errors=True)


def _pack_file_context(file_path: str, svc, *, file_max_tokens: int) -> str:
    """Inline ``file_path`` entries for a delegate that cannot read files.

    Each entry passes the Access Guard read verdict first: a denial raises
    AccessDenied (the policy refusal surfaces, spec §3) and a masked path
    refuses, because a delegate answer cannot carry the mask's disclosure.
    A file within ``file_max_tokens`` is inlined whole; a bigger one as its
    file map, with a note saying so.
    """
    parts: list[str] = []
    project = Path(svc.project_path)
    for rel in [p.strip() for p in (file_path or "").split(",") if p.strip()]:
        full = Path(rel) if Path(rel).is_absolute() else project / rel
        v = access_guard.verdict(str(full), "read", str(project))
        if v.denial:
            raise access_guard.AccessDenied(
                v.denial, access_guard.refusal(v.denial, rel, "read"))
        if v.masked:
            raise access_guard.AccessDenied(
                access_guard.Denial(rule=v.mask_rule.glob, kind="mask",
                                    scope=v.mask_rule.scope, reason="masked path"),
                f"{access_guard.TAG_MASK_UNSUPPORTED} {rel} is masked; c3_delegate does not "
                "send masked content to another model. Read it with c3_read instead.")
        compressor = getattr(svc, "compressor", None)
        if compressor is not None and compressor.is_protected_file(full):
            parts.append(f"--- file: {rel} ---\n[not included: protected file]")
            continue
        if not full.is_file():
            parts.append(f"--- file: {rel} ---\n[not included: file not found]")
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        n_lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        if count_tokens(text) <= file_max_tokens:
            parts.append(f"--- file: {rel} ({n_lines} lines) ---\n{text.rstrip()}")
            continue
        body = ""
        if compressor is not None:
            try:
                res = compressor.compress_file(str(full), "map")
                body = res.get("compressed", "") if isinstance(res, dict) else ""
            except access_guard.AccessDenied:
                raise
            except Exception:
                body = ""
        note = (f"[file map only: the file is over {file_max_tokens} tokens; "
                "pass a smaller excerpt in context for line-level questions]")
        parts.append(f"--- file: {rel} ({n_lines} lines) ---\n{note}\n{body.rstrip()}")
    return "\n\n".join(parts)


def _handle_claude_delegate(task: str, task_type: str, context: str,
                            file_path: str, svc, dcfg: dict, finalize,
                            tier: str = "", model: str = "", scout: bool = False) -> str:
    """Delegate to a Claude model tier through a locked-down ``claude -p``.

    No tools, no project directory, no MCP servers, no hooks: C3 packs the
    context (guarded file reads) and the delegate answers from that text
    alone. That is what keeps a one-line answer at a few thousand tokens
    instead of a full 46k-token project session.

    ``scout``: the delegate may also Read/Grep/Glob the project itself, under
    permission denies derived from Access Guard and the guard as its hook.
    Scout answers are never cached — they depend on the files as they are.
    """
    base = {"task_type": task_type, "backend": "claude"}
    if scout:
        base["mode"] = "scout"
    if not dcfg.get("claude_enabled", True):
        return finalize("c3_delegate", base,
                        "[delegate:error] Claude not enabled. Set delegate.claude_enabled=true "
                        "in .c3/config.json", "disabled")
    resolved_tier, model_arg, problem = resolve_claude_tier(tier, model, dcfg, scout=scout)
    if problem:
        return finalize("c3_delegate", base, f"[delegate:error] {problem}", "error")
    base["tier"] = resolved_tier
    breaker = _backend_breaker("claude", dcfg)
    if not breaker.allow():
        return finalize("c3_delegate", base,
                        "[delegate:degraded] Claude skipped after repeated failures; retrying in "
                        f"~{breaker.cooldown_remaining()}s. Run 'claude --version' to diagnose.",
                        "degraded")

    if task_type == "auto":
        task_type = infer_task_type(task, context)
        base["task_type"] = task_type
    tdef = DELEGATE_TASKS.get(task_type)
    if not tdef:
        return finalize("c3_delegate", base, f"[delegate:error] Unknown type: {task_type}", "error")

    file_max = max(200, int(dcfg.get("claude_file_max_tokens", 8000) or 8000))
    enriched = context or ""
    packed = _pack_file_context(file_path, svc, file_max_tokens=file_max) if file_path else ""
    if packed:
        enriched = f"{enriched}\n\n{packed}" if enriched else packed
    max_ctx = max(200, int(dcfg.get("claude_max_context_tokens", 24000) or 24000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4] + f"\n[context truncated at ~{max_ctx} tokens]"

    settings = None
    if scout:
        try:
            settings = scout_settings(svc.project_path)
        except ValueError as exc:
            return finalize("c3_delegate", base,
                            f"[delegate:blocked] scout refused: {exc}. Fix the config "
                            "(c3 access list) or delegate without scout.", "blocked")
    rules = _CLAUDE_SCOUT_RULES if scout else _CLAUDE_DELEGATE_RULES
    system_prompt = f"{tdef['system']} {rules}"
    prompt = tdef["prompt_template"].format(context=enriched or "(none)", task=task)
    effort = claude_effort_for(resolved_tier, dcfg)
    if effort:
        base["effort"] = effort
    thinking_cap = claude_thinking_cap_for(resolved_tier, dcfg)
    if thinking_cap is not None:
        base["thinking_cap"] = thinking_cap
    try:
        budget = float(dcfg.get("claude_max_budget_usd") or 0.0)
    except (TypeError, ValueError):
        budget = 0.0

    ckey = hashlib.md5(
        f"claude|{resolved_tier}|{model_arg}|{effort}|{thinking_cap}|{system_prompt}|{prompt}".encode()
    ).hexdigest()
    if not scout and ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {**base, "model": model_arg or "cli-default", "cached": True},
                        cached_resp, "cached")

    if scout:
        timeout = int(dcfg.get("claude_scout_timeout", 240) or 240)
    else:
        timeout = int(dcfg.get("claude_timeout", 120) or 120)
    _log_progress(svc, f"[delegate] Claude {resolved_tier} ({model_arg or 'cli-default'})"
                       f"{' scout' if scout else ''}...")
    t0 = time.monotonic()
    output, ok, stats = _run_claude(prompt, system_prompt, model_arg, timeout=timeout,
                                    effort=effort, max_budget_usd=budget,
                                    scout_project=str(svc.project_path) if scout else None,
                                    settings=settings, thinking_cap=thinking_cap)
    elapsed = round(time.monotonic() - t0, 1)
    meta = {**base, "model": stats.pop("model", "") or model_arg or "cli-default",
            "elapsed": f"{elapsed}s", **stats}
    if not ok:
        if breaker.record_failure():
            _notify_backend_degraded(svc, "claude", breaker)
        return finalize("c3_delegate", meta, output, "error")
    breaker.record_success()
    _delegate_metrics["total_calls"] += 1
    if not scout:
        _delegate_cache[ckey] = (output, count_tokens(output))
    return finalize("c3_delegate", meta, output, "ok")


def _worker_timeout(dcfg: dict) -> tuple[int, str]:
    """(seconds, note). Inside the MCP client's own ceiling when it is known:
    a worker the client abandons keeps writing with nobody to report to, so
    C3's deadline must fire first and kill it."""
    want = int(dcfg.get("claude_write_timeout", 600) or 600)
    try:
        from cli.tools.shell import _transport_ceiling_s
        ceiling = _transport_ceiling_s()
    except Exception:
        ceiling = None
    if ceiling and want > ceiling - 15:
        return max(30, ceiling - 15), f"capped at {max(30, ceiling - 15)}s by the MCP client's {ceiling}s limit"
    return want, ""


def _handle_claude_write(task: str, task_type: str, context: str, file_path: str,
                         svc, dcfg: dict, finalize, *, tier: str = "", model: str = "",
                         write_paths="") -> str:
    """Write mode: a Claude worker makes a change the caller specified.

    The caller names the write set; the worker runs ``claude -p --restricted``
    with Read/Grep/Glob/Edit/Write under Access Guard (services/delegate_write
    has the four fences). C3 then diffs every file the worker touched against
    its pre-image, logs each change to the edit ledger, and returns the diff.
    Never cached. A worker killed at its deadline still reports what it wrote.
    """
    from services import delegate_write as dw

    base = {"task_type": task_type, "backend": "claude", "mode": "write"}
    if not dcfg.get("claude_enabled", True):
        return finalize("c3_delegate", base,
                        "[delegate:error] Claude not enabled. Set delegate.claude_enabled=true "
                        "in .c3/config.json", "disabled")
    globs, problem = dw.parse_write_paths(write_paths)
    if problem:
        return finalize("c3_delegate", base, f"[delegate:error] {problem}", "error")
    # A literal path the guard already refuses would only cost the worker
    # turns and come back as a bare permission error: drop it now and say why.
    guarded: list[str] = []
    for g in list(globs):
        if any(ch in g for ch in "*?["):
            continue
        full = Path(svc.project_path) / g
        denial = access_guard.check(str(full), "write" if full.exists() else "create",
                                    str(svc.project_path))
        if denial:
            globs.remove(g)
            guarded.append(f"{g} ({denial.kind} rule '{denial.rule}')")
    if not globs:
        return finalize("c3_delegate", base,
                        "[delegate:blocked] Access Guard refuses every write path: "
                        + "; ".join(guarded) + ". Make those edits yourself.", "blocked")
    resolved_tier, model_arg, problem = resolve_claude_tier(tier, model, dcfg, write=True)
    if problem:
        return finalize("c3_delegate", base, f"[delegate:error] {problem}", "error")
    base["tier"] = resolved_tier
    breaker = _backend_breaker("claude", dcfg)
    if not breaker.allow():
        return finalize("c3_delegate", base,
                        "[delegate:degraded] Claude skipped after repeated failures; retrying in "
                        f"~{breaker.cooldown_remaining()}s. Run 'claude --version' to diagnose.",
                        "degraded")

    file_max = max(200, int(dcfg.get("claude_file_max_tokens", 8000) or 8000))
    enriched = context or ""
    packed = _pack_file_context(file_path, svc, file_max_tokens=file_max) if file_path else ""
    if packed:
        enriched = f"{enriched}\n\n{packed}" if enriched else packed
    max_ctx = max(200, int(dcfg.get("claude_max_context_tokens", 24000) or 24000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4] + f"\n[context truncated at ~{max_ctx} tokens]"

    try:
        from cli.tools import _grants
        session_id = _grants.session_id(svc)
    except Exception:
        session_id = ""
    project = str(svc.project_path)
    state = dw.new_state_dir(project, globs, session_id)
    try:
        try:
            settings = worker_settings(project, globs, state)
        except ValueError as exc:
            return finalize("c3_delegate", base,
                            f"[delegate:blocked] write mode refused: {exc}. Fix the config "
                            "(c3 access list) or make the change yourself.", "blocked")
        timeout, cap_note = _worker_timeout(dcfg)
        effort = claude_effort_for(resolved_tier, dcfg)
        if effort:
            base["effort"] = effort
        thinking_cap = claude_thinking_cap_for(resolved_tier, dcfg)
        if thinking_cap is not None:
            base["thinking_cap"] = thinking_cap
        try:
            budget = float(dcfg.get("claude_max_budget_usd") or 0.0)
        except (TypeError, ValueError):
            budget = 0.0
        prompt = ("WRITE SET (the only paths you may edit or create, relative to the "
                  "repository root):\n" + "\n".join(f"- {g}" for g in globs)
                  + f"\n\nTIME: you are stopped after {timeout}s."
                  + f"\n\nTASK FROM THE LEAD AGENT:\n{task}"
                  + f"\n\nCONTEXT:\n{enriched or '(none)'}")
        label = f"claude {resolved_tier} ({model_arg or 'cli-default'})"
        _log_progress(svc, f"[delegate] {label} write mode, {len(globs)} write path(s)...")
        t0 = time.monotonic()
        output, ok, stats = _run_claude(prompt, _CLAUDE_WORKER_RULES, model_arg, timeout=timeout,
                                        effort=effort, max_budget_usd=budget,
                                        scout_project=project, settings=settings,
                                        thinking_cap=thinking_cap, tools=dw.WORKER_TOOLS)
        elapsed = round(time.monotonic() - t0, 1)
        refused = dw.denial_lines(stats.pop("denials", None), project)
        changes = dw.collect_changes(state, project)
    finally:
        shutil.rmtree(state, ignore_errors=True)

    model_name = stats.pop("model", "") or model_arg or "cli-default"
    added = sum(c["added"] for c in changes)
    removed = sum(c["removed"] for c in changes)
    meta = {**base, "model": model_name, "elapsed": f"{elapsed}s", **stats,
            "files_changed": len(changes), "lines_added": added, "lines_removed": removed,
            "denied": len(refused)}
    if changes and getattr(svc, "edit_ledger", None):
        from cli.tools.edit import _log_to_ledger
        first_line = (task.strip().splitlines() or [""])[0][:100]
        for c in changes:
            _log_to_ledger(
                c["rel"], f"c3_delegate write ({model_name}): {first_line}",
                ["c3_delegate", "claude", resolved_tier], svc,
                detail={"delegate": {"backend": "claude", "tier": resolved_tier,
                                     "model": model_name, "added": c["added"],
                                     "removed": c["removed"]},
                        "created": c["change"] == "created"})

    n = len(changes)
    if ok:
        breaker.record_success()
        _delegate_metrics["total_calls"] += 1
        header = (f"[delegate:write] {label} changed {n} file(s) in {elapsed}s. Review the diff "
                  "and run the tests before relying on it." if n else
                  f"[delegate:write] {label} changed nothing in {elapsed}s.")
        status, report = "ok", output
    else:
        if breaker.record_failure():
            _notify_backend_degraded(svc, "claude", breaker)
        header = f"[delegate:write-failed] {output}"
        if n:
            header += f"\n{n} file(s) were changed before it stopped; review them:"
        status, report = "error", ""
    if cap_note and not ok:
        header += f" (deadline {cap_note})"
    if guarded:
        header += ("\nNot attempted, Access Guard refuses a delegate: " + "; ".join(guarded)
                   + ". Make those edits yourself.")
    max_diff = max(2000, int(dcfg.get("claude_write_diff_max_chars", 24000) or 24000))
    text = dw.render(changes, header=header, report=report, refused=refused,
                     max_diff_chars=max_diff)
    return finalize("c3_delegate", meta, text, status)


def check_gemini() -> dict:
    """Zero-cost health check for Gemini CLI. Returns status dict."""
    global _gemini_available
    exe = _which("gemini")
    if not exe:
        _gemini_available = False
        return {"status": "not_installed", "detail": "gemini CLI not found on PATH"}
    try:
        probed = _probe_cli_version(exe, timeout=10)
        if probed is None:
            _gemini_available = False
            return {"status": "timeout", "detail": "gemini --version timed out (10s)"}
        out, err, code = probed
        if code == 0:
            _gemini_available = True
            return {"status": "ok", "version": out}
        else:
            _gemini_available = False
            return {"status": "error", "detail": err or f"exit code {code}"}
    except Exception as e:
        _gemini_available = False
        return {"status": "error", "detail": str(e)}


def _start_gemini_early(model: str, timeout: int = 45, idle_timeout: int = 15,
                        cwd: str | None = None):
    """Start Gemini subprocess with stdin=PIPE so the prompt can be fed later.

    Call this before the compress step so Gemini's ~9s MCP startup overlaps
    with other work. Then call _finish_gemini_early() to send the prompt and
    collect the result.

    Returns the Popen object, or None if Gemini is not available.
    """
    gem_exe = _which("gemini") or "gemini"
    if not gem_exe or gem_exe == "gemini":
        exe = _which("gemini")
        if not exe:
            return None
    cmd = [
        gem_exe,
        "--output-format", "json",
        "--approval-mode", "yolo",
        "--allowed-mcp-server-names", "__none__",
    ]
    if model:
        cmd += ["-m", model]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=cwd,
            **_popen_kwargs(),
        )
        return proc
    except Exception:
        return None


def _finish_gemini_early(proc, task: str, context: str,
                         timeout: int = 45, idle_timeout: int = 15):
    """Feed the prompt to an early-started Gemini process and collect result.

    Returns (output, success, token_stats).
    """
    import json as _json

    empty_stats = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    if proc is None:
        return "[gemini:error] process not started", False, empty_stats

    prompt = f"{task}\n\nContext:\n{context}" if context else task

    import threading
    stdout_parts = []
    stderr_parts = []
    last_activity = [time.time()]

    def _read_stream(stream, parts, track_activity=False):
        try:
            for line in stream:
                parts.append(line)
                if track_activity:
                    last_activity[0] = time.time()
        except (ValueError, OSError):
            pass

    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_parts, True), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(proc.stderr, stderr_parts, True), daemon=True)
    t_out.start()
    t_err.start()

    # Write prompt to stdin in a daemon thread — avoids blocking the caller if
    # the pipe buffer fills up before Gemini reads (it reads only after MCP startup).
    def _write_stdin():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass
    threading.Thread(target=_write_stdin, daemon=True).start()

    deadline = time.time() + timeout
    status = "ok"
    while proc.poll() is None:
        now = time.time()
        if now >= deadline:
            _kill_proc_tree(proc)
            status = "timeout"
            break
        if idle_timeout and (now - last_activity[0]) > idle_timeout:
            _kill_proc_tree(proc)
            status = "idle_timeout"
            break
        time.sleep(0.5)

    t_out.join(timeout=3)
    t_err.join(timeout=3)
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)

    if status == "idle_timeout":
        return (f"[gemini:idle_timeout] No stderr activity for {idle_timeout}s "
                f"(likely MCP startup hang)"), False, empty_stats
    if status == "timeout":
        return f"[gemini:timeout] No response after {timeout}s", False, empty_stats
    if proc.returncode != 0:
        err = stderr.strip() if stderr else f"exit code {proc.returncode}"
        return f"[gemini:error] {err}", False, empty_stats

    # Parse JSON output
    raw = stdout.strip()
    json_start = raw.find("{")
    if json_start > 0:
        raw = raw[json_start:]
    try:
        data = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        return raw, True, empty_stats

    if isinstance(data, dict):
        text = data.get("response", data.get("text", data.get("result", raw)))
    elif isinstance(data, list):
        texts = [msg.get("text", msg.get("content", ""))
                 for msg in data if isinstance(msg, dict)]
        text = "\n".join(t for t in texts if t)
    else:
        text = str(data)

    token_stats = dict(empty_stats)
    if isinstance(data, dict):
        stats = data.get("stats", {})
        models = stats.get("models", {})
        for _model_id, mdata in models.items():
            tok = mdata.get("tokens", {})
            token_stats["input_tokens"] += tok.get("input", 0) or 0
            token_stats["output_tokens"] += tok.get("candidates", 0) or 0
            token_stats["cached_tokens"] += tok.get("cached", 0) or 0

    return text, True, token_stats


def _run_gemini(task: str, context: str, model: str,
                timeout: int = 45, idle_timeout: int = 15,
                cwd: str | None = None) -> tuple[str, bool, dict]:
    """Run gemini CLI as subprocess. Returns (output, success, token_stats).

    Uses heartbeat monitor: kills process if no stderr activity for idle_timeout
    seconds (catches MCP startup hangs). Also enforces total timeout (default 45s).
    Parses structured JSON output for response text and token metrics.
    """
    import json as _json

    prompt = f"{task}\n\nContext:\n{context}" if context else task
    gem_exe = _which("gemini") or "gemini"
    cmd = [
        gem_exe, "-p", prompt,
        "--output-format", "json",
        "--approval-mode", "yolo",
        "--allowed-mcp-server-names", "__none__",
    ]
    if model:
        cmd += ["-m", model]

    empty_stats = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    try:
        proc = subprocess.Popen(
            harden_win_argv(cmd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=cwd,
            **_popen_kwargs(),
        )
        stdout, stderr, status = _communicate_with_heartbeat(
            proc, timeout=timeout, idle_timeout=idle_timeout,
        )
        if status == "idle_timeout":
            return (f"[gemini:idle_timeout] No stderr activity for {idle_timeout}s "
                    f"(likely MCP startup hang)"), False, empty_stats
        if status == "timeout":
            return f"[gemini:timeout] No response after {timeout}s", False, empty_stats

        if proc.returncode != 0:
            err = stderr.strip() if stderr else f"exit code {proc.returncode}"
            return f"[gemini:error] {err}", False, empty_stats

        # Parse JSON output — strip non-JSON prefix lines (MCP startup messages)
        raw = stdout.strip()
        json_start = raw.find("{")
        if json_start > 0:
            raw = raw[json_start:]

        try:
            data = _json.loads(raw)
        except (_json.JSONDecodeError, TypeError):
            # Fallback: treat entire stdout as plain text
            return raw, True, empty_stats

        # Extract response text
        if isinstance(data, dict):
            text = data.get("response", data.get("text", data.get("result", raw)))
        elif isinstance(data, list):
            texts = [msg.get("text", msg.get("content", ""))
                     for msg in data if isinstance(msg, dict)]
            text = "\n".join(t for t in texts if t)
        else:
            text = str(data)

        # Extract token stats from stats.models.<id>.tokens
        token_stats = dict(empty_stats)
        if isinstance(data, dict):
            stats = data.get("stats", {})
            models = stats.get("models", {})
            for _model_id, mdata in models.items():
                tok = mdata.get("tokens", {})
                token_stats["input_tokens"] += tok.get("input", 0) or 0
                token_stats["output_tokens"] += tok.get("candidates", 0) or 0
                token_stats["cached_tokens"] += tok.get("cached", 0) or 0

        return text, True, token_stats
    except FileNotFoundError:
        return "[gemini:error] gemini CLI not found on PATH", False, empty_stats
    except Exception as e:
        return f"[gemini:error] {e}", False, empty_stats


def _is_codex_on_path() -> bool:
    """Check if codex CLI binary is on PATH."""
    return _which("codex") is not None


def check_codex() -> dict:
    """Zero-cost health check for Codex CLI. Returns status dict."""
    global _codex_available
    exe = _which("codex")
    if not exe:
        _codex_available = False
        return {"status": "not_installed", "detail": "codex CLI not found on PATH"}
    try:
        probed = _probe_cli_version(exe, timeout=10)
        if probed is None:
            _codex_available = False
            return {"status": "timeout", "detail": "codex --version timed out (10s)"}
        out, err, code = probed
        if code == 0:
            _codex_available = True
            return {"status": "ok", "version": out}
        else:
            _codex_available = False
            return {"status": "error", "detail": err or f"exit code {code}"}
    except Exception as e:
        _codex_available = False
        return {"status": "error", "detail": str(e)}


def _codex_cmd(prompt: str, model: str, sandbox: str, reasoning: str) -> list:
    """Build the codex exec argv.

    An empty/falsy model omits ``-m`` so the user's own Codex CLI default
    (~/.codex/config.toml) applies — never pin a fallback model name here.
    """
    codex_exe = _which("codex") or "codex"
    cmd = [codex_exe, "exec"]
    if model:
        cmd += ["-m", model]
    cmd += [
        "--config", f"model_reasoning_effort={reasoning}",
        "--sandbox", sandbox,
        "--config", 'approval_policy="never"',
        "--json",
        "--skip-git-repo-check",
        prompt,
    ]
    return cmd


_CHILD_IDENTITY_VARS = (
    "CODEX_THREAD_ID", "CODEX_MANAGED_BY_NPM", "CLAUDE_CODE_SESSION_ID",
    # A delegate launched from inside a Grok session/hook must not inherit it.
    "GROK_SESSION_ID", "GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_WORKSPACE_ROOT",
)


def _child_host_env(provider: str) -> dict:
    env = os.environ.copy()
    for name in _CHILD_IDENTITY_VARS:
        env.pop(name, None)
    env["C3_HOST"] = provider
    return env


def _delegate_binding(cwd, origin_id=""):
    from core.host import resolve_host
    host = resolve_host(str(cwd or Path.cwd()))
    project = str(Path(cwd or Path.cwd()).resolve())
    origin = origin_id or host.host_session_id or f"pid-{os.getpid()}"
    key = hashlib.sha256((project + "\0" + origin).encode()).hexdigest()
    return Path.home() / ".c3" / "delegate_sessions" / (key + ".json"), project, origin


def _codex_usage(stdout: str) -> dict:
    """Token counts from the last ``turn.completed`` event, when present."""
    stats: dict = {}
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        for src, dst in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                         ("cached_input_tokens", "cached_tokens"),
                         ("reasoning_output_tokens", "reasoning_tokens")):
            if usage.get(src) is not None:
                try:
                    stats[dst] = int(usage[src])
                except (TypeError, ValueError):
                    pass
    return stats


def _codex_result(stdout: str) -> tuple[str, str, bool]:
    thread_id, messages, completed, error = "", [], False, ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            import uuid
            try:
                thread_id = str(uuid.UUID(event.get("thread_id", "")))
            except (ValueError, TypeError, AttributeError):
                pass
        elif kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                messages.append(str(item.get("text") or ""))
        elif kind == "turn.completed":
            completed = True
        elif kind in ("turn.failed", "error"):
            raw = event.get("error") or event.get("message") or "Codex turn failed"
            if isinstance(raw, dict):
                raw = raw.get("message") or json.dumps(raw)
            error = error or str(raw)  # the first failure names the cause
    if error or not completed:
        return "[codex:error] " + (error or "No completed turn in Codex event stream"), thread_id, False
    return "\n\n".join(messages).strip(), thread_id, True


def _execute_codex(cmd, prompt, timeout, idle_timeout, cwd, origin_id="", resume_id="",
                   stats: dict | None = None):
    """Run a codex exec argv. ``stats``, when given, receives token usage."""
    try:
        proc = subprocess.Popen(harden_win_argv(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                cwd=cwd, env=_child_host_env("codex"), **_popen_kwargs())
        stdout, stderr, status = _communicate_with_heartbeat(
            proc, timeout=timeout, idle_timeout=idle_timeout, stdin_text=prompt)
        if status != "ok":
            return f"[codex:{status}] Delegation exceeded its execution budget", False
        if stats is not None:
            stats.update(_codex_usage(stdout))
        if proc.returncode != 0:
            # The reason is in the JSON event stream (turn.failed / error), not
            # on stderr: 2.132.0's eval saw 25 of 25 calls come back as a bare
            # "exit code 1" for a model a ChatGPT login rejects.
            detail = stderr.strip()[-4000:]
            if not detail:
                reason, _tid, _ok = _codex_result(stdout)
                if reason.startswith("[codex:error] ") and "No completed turn" not in reason:
                    detail = reason[len("[codex:error] "):][-4000:]
            return "[codex:error] " + (detail or f"exit code {proc.returncode}"), False
        answer, thread_id, ok = _codex_result(stdout)
        if resume_id and thread_id and resume_id != thread_id:
            return "[codex:error] Resumed thread identity did not match", False
        if ok and (thread_id or resume_id):
            path, project, origin = _delegate_binding(cwd, origin_id)
            from cli._hook_utils import _atomic_write_json
            _atomic_write_json(path, {"project": project, "origin": origin, "thread_id": thread_id or resume_id})
        return answer, ok
    except Exception as exc:
        return f"[codex:error] {exc}", False


def _run_codex(task: str, context: str, model: str, sandbox: str,
               reasoning: str = "high", timeout: int = 120,
               idle_timeout: int = 0, cwd: str | None = None,
               origin_id: str = "", stats: dict | None = None) -> tuple[str, bool]:
    prompt = f"{task}\n\nContext:\n{context}" if context else task
    return _execute_codex(_codex_cmd("-", model, sandbox, reasoning), prompt,
                          timeout, idle_timeout, cwd, origin_id, stats=stats)


def _run_codex_resume(follow_up: str, timeout: int = 120,
                      cwd: str | None = None, origin_id: str = "") -> tuple[str, bool]:
    path, project, origin = _delegate_binding(cwd, origin_id)
    try:
        import uuid
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("project") != project or saved.get("origin") != origin:
            raise ValueError("delegate binding mismatch")
        thread_id = str(uuid.UUID(saved["thread_id"]))
    except (OSError, ValueError, KeyError, TypeError):
        return "[codex:error] No Codex delegate thread bound to this project and caller; start a delegation first", False
    cmd = [_which("codex") or "codex", "exec", "resume", "--skip-git-repo-check",
           "--config", 'approval_policy="never"', "--json", thread_id, "-"]
    return _execute_codex(cmd, follow_up, timeout, 0, cwd, origin_id, resume_id=thread_id)


# ---------------------------------------------------------------------------
# Grok Build CLI backend (xAI)
# ---------------------------------------------------------------------------

# Read-only tools are auto-approved without --yolo, so headless runs with this
# allowlist never stop for an approval.
GROK_READONLY_TOOLS = "read_file,grep,list_dir"

# Grok also imports Claude Code / Cursor agent config (agents, hooks, MCP
# servers, rules, skills). A delegate must not pick any of that up.
_GROK_COMPAT_OFF = {
    f"GROK_{vendor}_{kind}_ENABLED": "0"
    for vendor in ("CLAUDE", "CURSOR")
    for kind in ("AGENTS", "HOOKS", "MCPS", "RULES", "SKILLS")
}

_grok_available: bool | None = None  # cached after first check


def _is_grok_on_path() -> bool:
    """Check if grok CLI binary is on PATH."""
    return _which("grok") is not None


def check_grok() -> dict:
    """Zero-cost health check for Grok Build CLI. Returns status dict."""
    global _grok_available
    exe = _which("grok")
    if not exe:
        _grok_available = False
        return {"status": "not_installed", "detail": "grok CLI not found on PATH"}
    try:
        probed = _probe_cli_version(exe, timeout=10)
        if probed is None:
            _grok_available = False
            return {"status": "timeout", "detail": "grok --version timed out (10s)"}
        out, err, code = probed
        if code == 0:
            _grok_available = True
            return {"status": "ok", "version": out}
        _grok_available = False
        return {"status": "error", "detail": err or f"exit code {code}"}
    except Exception as e:
        _grok_available = False
        return {"status": "error", "detail": str(e)}


def _grok_cmd(prompt_file: str, model: str, max_turns: int, cwd: str,
              allow_write: bool = False, reasoning_effort: str = "") -> list:
    """Build the headless grok argv.

    An empty model omits ``-m`` so the account's own Grok default applies —
    never pin a model name here. Read-only (the default) passes an explicit
    ``--tools`` allowlist; ``allow_write`` drops it and adds ``--yolo``, which
    auto-approves every tool call.
    """
    cmd = [_which("grok") or "grok", "--prompt-file", prompt_file,
           "--output-format", "json"]
    if allow_write:
        cmd.append("--yolo")
    else:
        cmd += ["--tools", GROK_READONLY_TOOLS]
    cmd += ["--max-turns", str(max(1, int(max_turns or 1)))]
    if model:
        cmd += ["-m", model]
    if reasoning_effort:
        cmd += ["--reasoning-effort", reasoning_effort]
    cmd += ["--cwd", cwd]
    return cmd


def _grok_env() -> dict:
    """Child env for grok: no inherited session identity, no auto-update,
    no Claude Code / Cursor config imports."""
    env = _child_host_env("grok")
    env["GROK_DISABLE_AUTOUPDATER"] = "1"
    env.update(_GROK_COMPAT_OFF)
    return env


def _grok_json(stdout: str):
    """Return the first JSON object in grok's stdout, tolerating leading
    non-JSON lines (update notices, warnings). None when there is none."""
    raw = (stdout or "").strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    starts = [0] if raw.startswith("{") else []
    offset = 0
    for line in raw.splitlines(keepends=True):
        if line.lstrip().startswith("{"):
            starts.append(offset + (len(line) - len(line.lstrip())))
        offset += len(line)
    for start in starts:
        try:
            data, _end = decoder.raw_decode(raw[start:])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _grok_token_stats(data) -> dict:
    stats = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    if not isinstance(data, dict):
        return stats
    usage = data.get("usage") or {}
    if isinstance(usage, dict):
        stats["input_tokens"] = int(usage.get("input_tokens") or 0)
        stats["output_tokens"] = int(usage.get("output_tokens") or 0)
        stats["cached_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
        if usage.get("reasoning_tokens") is not None:
            stats["reasoning_tokens"] = int(usage.get("reasoning_tokens") or 0)
    if data.get("total_cost_usd") is not None:
        try:
            stats["cost_usd"] = float(data["total_cost_usd"])
        except (TypeError, ValueError):
            pass
    return stats


def _run_grok(task: str, context: str, model: str = "", timeout: int = 120,
              idle_timeout: int = 0, max_turns: int = 8,
              allow_write: bool = False, reasoning_effort: str = "",
              cwd: str | None = None) -> tuple[str, bool, dict]:
    """Run ``grok --prompt-file`` headless. Returns (output, success, token_stats).

    Read-only (default): cwd is a fresh temp directory, removed afterwards.
    ``--tools`` does NOT stop a trusted project's ``.grok/config.toml`` MCP
    servers or ``.grok/hooks`` from loading, so a read-only delegate must not
    run inside the project; file context is inlined into the prompt instead.

    Write mode (``allow_write=True``): cwd is ``cwd`` (the project), ``--tools``
    is dropped and ``--yolo`` added. That run DOES load the project's trusted
    ``.grok`` MCP servers and hooks — writing into the project is the point of
    the mode, and handle_delegate gates it behind Access Guard.

    ``idle_timeout`` defaults to 0 (off): ``--output-format json`` prints one
    object at the very end, so a healthy multi-turn run is silent throughout.
    The total ``timeout`` still applies.
    """
    empty_stats = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    if not _which("grok"):
        return "[grok:error] grok CLI not found on PATH", False, empty_stats
    if allow_write and not cwd:
        return "[grok:error] write mode needs the project directory as cwd", False, empty_stats
    prompt = f"Context:\n{context}\n\nTask:\n{task}" if context else task
    workdir = tempfile.mkdtemp(prefix="c3-grok-")
    try:
        prompt_file = os.path.join(workdir, "prompt.md")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        run_cwd = str(cwd) if allow_write else workdir
        cmd = _grok_cmd(prompt_file, model, max_turns, run_cwd, allow_write,
                        reasoning_effort=reasoning_effort)
        proc = subprocess.Popen(
            harden_win_argv(cmd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=run_cwd, env=_grok_env(),
            **_popen_kwargs(),
        )
        stdout, stderr, status = _communicate_with_heartbeat(
            proc, timeout=timeout, idle_timeout=idle_timeout,
        )
        if status == "idle_timeout":
            return f"[grok:idle_timeout] No output for {idle_timeout}s", False, empty_stats
        if status == "timeout":
            return f"[grok:timeout] No response after {timeout}s", False, empty_stats
        data = _grok_json(stdout)
        if proc.returncode != 0:
            detail = (stderr or "").strip()[-4000:]
            if not detail and isinstance(data, dict):
                detail = str(data.get("error") or data.get("text") or "").strip()
            return (f"[grok:error] {detail or f'exit code {proc.returncode}'}",
                    False, empty_stats)
        if data is None:
            raw = (stdout or "").strip()
            if raw:
                return raw, True, empty_stats
            return "[grok:error] no output", False, empty_stats
        text = str(data.get("text") or "").strip()
        if not text:
            reason = data.get("error") or data.get("stopReason") or "no text"
            return f"[grok:error] empty response ({reason})", False, _grok_token_stats(data)
        return text, True, _grok_token_stats(data)
    except Exception as e:
        return f"[grok:error] {e}", False, empty_stats
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Delegate task definitions
DELEGATE_TASKS = {
    "summarize": {
        "default_model": "gemma3n:latest",
        "system": "You are a concise technical summarizer. Keep the answer compact and concrete.",
        "prompt_template": "Context:\n{context}\n\nTask:\n{task}\n\nReturn a compact summary with only the key points.",
        "temperature": 0.2,
    },
    "explain": {
        "default_model": "llama3.2:3b",
        "system": "You explain code precisely and concisely. Prefer short bullet points and specific references.",
        "prompt_template": "Context:\n{context}\n\nQuestion:\n{task}\n\nExplain only what is needed to answer the question.",
        "temperature": 0.2,
    },
    "docstring": {
        "default_model": "gemma3n:latest",
        "system": "Write terse, accurate code documentation.",
        "prompt_template": "Context:\n{context}\n\nTask:\n{task}\n\nProduce a concise docstring or documentation snippet.",
        "temperature": 0.2,
    },
    "review": {
        "default_model": "llama3.2:3b",
        "system": "You are a pragmatic code reviewer. Prioritize bugs, regressions, and missing tests.",
        "prompt_template": "Context:\n{context}\n\nReview task:\n{task}\n\nReturn the most important findings first.",
        "temperature": 0.2,
    },
    "ask": {
        "default_model": "deepseek-r1:1.5b",
        "system": "Answer narrowly and directly from the provided context.",
        "prompt_template": "Context:\n{context}\n\nQuestion:\n{task}\n\nAnswer concisely.",
        "temperature": 0.2,
    },
    "test": {
        "default_model": "llama3.2:3b",
        "system": "Design targeted tests that maximize defect coverage with minimal redundancy.",
        "prompt_template": "Context:\n{context}\n\nTask:\n{task}\n\nProduce focused test ideas or test code.",
        "temperature": 0.2,
    },
    "diagnose": {
        "default_model": "llama3.2:3b",
        "system": "You diagnose failures from logs and execution context. Focus on root cause and next step.",
        "prompt_template": "Context:\n{context}\n\nProblem:\n{task}\n\nIdentify the most likely cause and the next debugging step.",
        "temperature": 0.1,
    },
    "improve": {
        "default_model": "llama3.2:3b",
        "system": "You improve code with minimal, high-value changes.",
        "prompt_template": "Context:\n{context}\n\nTask:\n{task}\n\nSuggest the smallest useful improvement plan.",
        "temperature": 0.2,
    },
}

# Module-level cache and metrics
_delegate_cache: dict[str, tuple[str, int]] = {}
_delegate_metrics = {"total_calls": 0, "tokens_saved": 0}

# Per-backend runtime circuit breakers. Distinct from the install-status flags
# (_gemini_available etc., which only answer "is the CLI on PATH"): these track
# *runtime* health so a broken-but-installed backend (expired auth, repeated
# timeouts) stops re-spawning a 90-120s subprocess on every call. Keyed by
# backend name and intentionally process-global — backend health (auth, CLI
# version) is a property of the host, not of any single project.
_backend_breakers: dict[str, CircuitBreaker] = {}
_backend_breakers_lock = threading.Lock()


def _backend_breaker(name: str, dcfg: dict | None = None) -> CircuitBreaker:
    """Return (creating on first use) the runtime circuit breaker for a backend."""
    with _backend_breakers_lock:
        breaker = _backend_breakers.get(name)
        if breaker is None:
            cfg = dcfg or {}
            breaker = CircuitBreaker(
                name,
                failure_threshold=int(cfg.get("breaker_failure_threshold", 3) or 3),
                cooldown_seconds=float(cfg.get("breaker_cooldown_seconds", 60) or 60),
            )
            _backend_breakers[name] = breaker
        return breaker


def _notify_backend_degraded(svc, name: str, breaker: CircuitBreaker) -> None:
    """Surface a backend trip via the NotificationStore (best-effort, never raises)."""
    notifications = getattr(svc, "notifications", None)
    if notifications is None:
        return
    try:
        notifications.add(
            agent="c3",
            severity="warning",
            title=f"Delegate backend degraded: {name}",
            message=(
                f"{name} failed {breaker.failure_threshold}x consecutively; c3_delegate "
                f"will skip it for ~{int(breaker.cooldown_seconds)}s instead of re-spawning "
                f"the CLI. Run '{name} --version' to diagnose."
            ),
            replace_if_unacked=True,
        )
    except Exception:
        pass


def _cascade_order(task_type: str, dcfg: dict, host: str = "") -> list[str]:
    """Ordered backend preference for backend='auto' routing.

    The host's own backend (``host``, from HOST_BACKENDS) goes first when
    known. Then heavy tasks (review/diagnose/improve/test by default) prefer
    the cloud CLIs and degrade gracefully: codex -> gemini -> grok -> ollama.
    Light tasks stay local-first and only fall over to a cloud CLI when
    Ollama itself is down: ollama -> codex -> gemini -> grok.
    """
    heavy_default = ["review", "diagnose", "improve", "test"]
    order: list[str] = []
    for name in ("codex", "gemini", "grok"):
        if task_type in set(dcfg.get(f"{name}_task_types", heavy_default)):
            order.append(name)
    if order:
        order.append("ollama")
    else:
        order = ["ollama", "codex", "gemini", "grok"]
    if host:
        order = [host] + [name for name in order if name != host]
    return order


def _write_capable(name: str, dcfg: dict) -> bool:
    """Backends that may write outside C3's control (Access Guard gate).

    gemini (--approval-mode yolo) always; grok only in write mode — read-only
    grok runs a read-only tool allowlist in a throwaway temp directory.
    claude is not: since 2.133.0 it runs with no tools in a temp directory,
    and C3 packs its context through guarded reads.
    """
    if name == "gemini":
        return True
    return name == "grok" and bool(dcfg.get("grok_allow_write", False))


def _cascade_skip_reason(name: str, dcfg: dict, svc) -> str | None:
    """Why backend ``name`` should be skipped during auto cascade (None = usable).

    Non-mutating and cheap: consults the breaker's allow() peek (which does not
    consume the half-open probe), cached install state, PATH presence, and the
    ~30s-cached Ollama availability check. No delegate subprocess is spawned.
    """
    breaker = _backend_breaker(name, dcfg)
    if not breaker.allow():
        return f"breaker open, retry ~{breaker.cooldown_remaining()}s"
    if name == "ollama":
        ollama = getattr(svc, "ollama_client", None)
        if ollama is None:
            return "no client"
        try:
            if not ollama.is_available():
                return "unreachable"
        except Exception:
            return "unreachable"
        return None
    if name == "claude":
        if not dcfg.get("claude_enabled", True):
            return "disabled"
        if _claude_available is False:
            return "unavailable"
        if _claude_available is None and not _is_claude_on_path():
            return "not on PATH"
        return None
    if name in ("codex", "gemini", "grok"):
        if not dcfg.get(f"{name}_enabled", False):
            return "disabled"
        known = {"codex": _codex_available, "gemini": _gemini_available,
                 "grok": _grok_available}[name]
        if known is False:
            return "unavailable"
        if known is None:
            on_path = {"codex": _is_codex_on_path, "gemini": _is_gemini_on_path,
                       "grok": _is_grok_on_path}[name]()
            if not on_path:
                return "not on PATH"
        return None
    return "unknown backend"


def _with_cascade_note(finalize, note: str):
    """Wrap finalize so the auto-cascade decision lands in metadata + response."""
    def wrapped(tool, meta, output, status, **kw):
        meta = dict(meta or {})
        meta["cascade"] = note
        if isinstance(output, str) and note not in output:
            output = f"{note}\n{output}"
        return finalize(tool, meta, output, status, **kw)
    return wrapped


def get_delegate_metrics() -> dict:
    return dict(_delegate_metrics)


def infer_task_type(task: str, context: str = "") -> str:
    text = f"{task}\n{context}".lower()
    if any(tok in text for tok in ("traceback", "exception", "stack trace", "exit code", "failed", "error")):
        return "diagnose"
    if any(tok in text for tok in ("review", "regression", "bug risk", "audit")):
        return "review"
    if any(tok in text for tok in ("test", "pytest", "unit test", "integration test")):
        return "test"
    if any(tok in text for tok in ("docstring", "document", "documentation")):
        return "docstring"
    if any(tok in text for tok in ("summarize", "summary", "tl;dr")):
        return "summarize"
    if any(tok in text for tok in ("improve", "refactor", "clean up", "optimize")):
        return "improve"
    return "explain"


_CLOUD_TAG_RE = re.compile(r"(?:^|[:\-])cloud$", re.IGNORECASE)


def is_cloud_tag(name: str) -> bool:
    """An Ollama Cloud tag (``deepseek-v4-pro:cloud``, ``gpt-oss:20b-cloud``):
    the local daemon proxies it to a remote service."""
    return bool(_CLOUD_TAG_RE.search(str(name or "").strip()))


def resolve_model_name(candidate: str, available: list[str]) -> str:
    if not candidate:
        return ""
    normalized = candidate.strip().lower()
    if not normalized:
        return ""
    for model in available:
        if model.lower() == normalized:
            return model
    base = normalized.split(":", 1)[0]
    for model in available:
        lower = model.lower()
        if lower == base or lower.startswith(base + ":"):
            return model
    for model in available:
        if base in model.lower():
            return model
    return ""


def _fallback_model_order(task_type: str) -> list[str]:
    if task_type in {"ask", "diagnose", "explain"}:
        return ["llama3.2:latest", "llama3.2:3b", "qwen3-coder-next:latest", "llama3.1:latest", "gemma3n:latest"]
    return ["llama3.2:latest", "llama3.2:3b", "qwen3-coder-next:latest", "gemma3n:latest"]


def _estimate_confidence(task_type: str, response: str, response_tokens: int) -> str:
    hedging = [
        "i'm not sure", "i don't know", "it's unclear", "might be",
        "possibly", "i cannot determine", "hard to say", "not enough context",
    ]
    hedge_count = sum(1 for phrase in hedging if phrase in (response or "").lower())
    min_tokens = {"summarize": 15, "explain": 30, "docstring": 10, "review": 20,
                  "ask": 10, "test": 30, "diagnose": 20, "improve": 10}
    too_short = response_tokens < min_tokens.get(task_type, 10)
    if too_short or hedge_count >= 2:
        return "low"
    if hedge_count == 1 or response_tokens < min_tokens.get(task_type, 10) * 2:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Codex delegate handler
# ---------------------------------------------------------------------------

def _tier_overrides(backend: str, tier: str, model: str, dcfg: dict) -> tuple[dict, str]:
    """Per-backend settings for a requested tier/model: ({model, effort, tier}, error).

    Only called when the caller asked for a tier or a model (or routed via
    backend='host'), so an explicit backend with neither keeps its configured
    behaviour byte for byte.
    """
    out: dict = {}
    if model:
        if not _MODEL_NAME_RE.match(model):
            return {}, f"model name {model!r} is not a valid model id"
        out["model"] = model
    if tier or not model:
        resolved = normalize_tier(tier, dcfg)
        if not resolved:
            return {}, f"unknown tier {tier!r} (use one of {', '.join(DELEGATE_TIERS)})"
        out["tier"] = resolved
        if resolved != "default":
            tier_models = dcfg.get(f"{backend}_tier_models") or {}
            if backend == "gemini":
                tier_models = {**GEMINI_TIER_MODELS, **tier_models}
            if "model" not in out and tier_models.get(resolved):
                out["model"] = str(tier_models[resolved])
            if backend == "codex":
                out["effort"] = (dcfg.get("codex_tier_reasoning") or CODEX_TIER_REASONING).get(resolved, "")
            elif backend == "grok":
                out["effort"] = (dcfg.get("grok_tier_effort") or GROK_TIER_EFFORT).get(resolved, "")
    else:
        out["tier"] = "custom"
    if out.get("model") and not _MODEL_NAME_RE.match(out["model"]):
        return {}, f"delegate.{backend}_tier_models = {out['model']!r} is not a valid model id"
    return out, ""


def _pack_for(file_path: str, context: str, svc, dcfg: dict) -> str:
    """context + guarded file_path packing, shared by the cloud CLIs and Ollama.

    ``auto_compress`` stays the switch (false = file_path is ignored, as before).
    """
    enriched = context or ""
    if file_path and dcfg.get("auto_compress", True):
        file_max = max(200, int(dcfg.get("file_max_tokens", 8000) or 8000))
        packed = _pack_file_context(file_path, svc, file_max_tokens=file_max)
        if packed:
            enriched = f"{enriched}\n\n{packed}" if enriched else packed
    return enriched


def _handle_codex_delegate(task: str, task_type: str, context: str,
                           file_path: str, svc, dcfg: dict, finalize,
                           tier: str = "", model: str = "") -> str:
    """Handle delegation via Codex CLI."""
    if not dcfg.get("codex_enabled", False):
        return finalize("c3_delegate", {"task_type": task_type, "backend": "codex"},
                        "[delegate:error] Codex not enabled. Set delegate.codex_enabled=true in .c3/config.json",
                        "disabled")

    global _codex_available
    if _codex_available is None:
        check_codex()  # populates _codex_available
    if not _codex_available:
        return finalize("c3_delegate", {"task_type": task_type, "backend": "codex"},
                        "[delegate:error] Codex CLI not available. Run 'codex --version' to diagnose.",
                        "unavailable")

    breaker = _backend_breaker("codex", dcfg)
    if not breaker.allow():
        return finalize("c3_delegate", {"task_type": task_type, "backend": "codex"},
                        "[delegate:degraded] Codex skipped after repeated failures; retrying in "
                        f"~{breaker.cooldown_remaining()}s. Run 'codex --version' to diagnose.",
                        "degraded")

    # Resolve model/sandbox/reasoning from config or defaults
    cdef = CODEX_MODELS.get(task_type, CODEX_MODELS.get("ask", {}))
    requested_model = model
    model = dcfg.get("codex_default_model") or cdef.get("model", "")
    tier_meta: dict = {}
    if tier or requested_model:
        over, problem = _tier_overrides("codex", tier, requested_model, dcfg)
        if problem:
            return finalize("c3_delegate", {"task_type": task_type, "backend": "codex"},
                            f"[delegate:error] {problem}", "error")
        model = over.get("model") or model
        tier_meta = {"tier": over["tier"]}
    sandbox = dcfg.get("codex_default_sandbox") or cdef.get("sandbox", "read-only")
    try:
        _pin = access_guard.has_active_rules(str(svc.project_path))
    except Exception:
        _pin = True  # evaluator failure → fail closed
    if _pin:
        # Access Guard rules are active → pin the sandbox to read-only no
        # matter what config asked for. Codex is the one delegated backend
        # whose sandbox C3 can pin; the guard's file rules cannot be pushed
        # into a foreign CLI, so delegation runs read-only instead. With
        # ZERO user rules this branch never fires (byte-identical behavior).
        sandbox = "read-only"
    reasoning = dcfg.get("codex_reasoning_effort") or cdef.get("reasoning", "high")
    if tier_meta and tier_meta["tier"] not in ("default", "custom"):
        reasoning = over.get("effort") or reasoning
    timeout = int(dcfg.get("codex_timeout", 120))

    enriched = _pack_for(file_path, context, svc, dcfg)

    # Truncate context to avoid blowing Codex's input
    max_ctx = max(200, int(dcfg.get("codex_max_context_tokens", 4000) or 4000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4]

    # Cache check
    from cli.tools._grants import session_id
    origin = session_id(svc)
    ckey = hashlib.md5(
        f"codex|{svc.project_path}|{origin}|{task_type}|{model}|{reasoning}|{enriched}|{task}".encode()
    ).hexdigest()
    if ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {"task_type": task_type, "backend": "codex", **tier_meta,
                                        "cached": True},
                        cached_resp, "cached")

    # Run Codex
    _log_progress(svc, f"[delegate] Codex {model or 'cli-default'} ({sandbox}, reasoning={reasoning})...")
    t0 = time.monotonic()
    usage: dict = {}
    output, ok = _run_codex(
        task=task, context=enriched,
        model=model, sandbox=sandbox,
        reasoning=reasoning, timeout=timeout,
        cwd=str(svc.project_path), origin_id=origin, stats=usage,
    )
    elapsed = round(time.monotonic() - t0, 1)
    meta = {"task_type": task_type, "backend": "codex", **tier_meta,
            "model": model or "cli-default", "effort": reasoning, "elapsed": f"{elapsed}s", **usage}

    if not ok:
        if breaker.record_failure():
            _notify_backend_degraded(svc, "codex", breaker)
        return finalize("c3_delegate", meta, output, "error")

    breaker.record_success()
    _delegate_metrics["total_calls"] += 1
    _delegate_cache[ckey] = (output, count_tokens(output))

    # Memory bridge — auto-extract key findings from substantial Codex responses
    _codex_memory_bridge(output, task_type, task, svc)

    return finalize("c3_delegate", meta, output, "ok")


def _codex_memory_bridge(output: str, task_type: str, task: str, svc):
    """Auto-extract key findings from Codex responses into c3_memory.

    Only stores when the response is substantial and actionable.
    """
    try:
        memory = getattr(svc, "memory", None)
        if not memory:
            return
        dcfg = svc.delegate_config or {}
        if not dcfg.get("codex_memory_bridge", True):
            return

        # Only bridge substantial responses (not trivial or error)
        tokens = count_tokens(output)
        if tokens < 50 or tokens > 3000:
            return  # too short = trivial, too long = dump

        # Skip benign responses
        lower = output.lower()
        benign = ("no issues", "looks good", "no problems", "lgtm", "all good",
                  "no regressions", "no bugs")
        if any(b in lower for b in benign):
            return

        # Build a concise fact from the Codex output
        # Truncate to keep facts digestible
        summary = output[:400].strip()
        if len(output) > 400:
            summary += "..."

        fact = f"[codex:{task_type}] {task[:80]} — {summary}"
        memory.remember(fact, category=f"codex_{task_type}")
        log.debug("codex_memory_bridge: stored fact for task_type=%s", task_type)
    except Exception:
        pass  # never break delegation for memory


# ---------------------------------------------------------------------------
# Gemini delegate handler
# ---------------------------------------------------------------------------

def _handle_gemini_delegate(task: str, task_type: str, context: str,
                            file_path: str, svc, dcfg: dict, finalize,
                            tier: str = "", model: str = "") -> str:
    """Handle delegation via Gemini CLI."""
    if not dcfg.get("gemini_enabled", False):
        return finalize("c3_delegate", {"task_type": task_type, "backend": "gemini"},
                        "[delegate:error] Gemini not enabled. Set delegate.gemini_enabled=true in .c3/config.json",
                        "disabled")

    global _gemini_available
    if _gemini_available is None:
        check_gemini()
    if not _gemini_available:
        return finalize("c3_delegate", {"task_type": task_type, "backend": "gemini"},
                        "[delegate:error] Gemini CLI not available. Run 'gemini --version' to diagnose.",
                        "unavailable")

    breaker = _backend_breaker("gemini", dcfg)
    if not breaker.allow():
        return finalize("c3_delegate", {"task_type": task_type, "backend": "gemini"},
                        "[delegate:degraded] Gemini skipped after repeated failures; retrying in "
                        f"~{breaker.cooldown_remaining()}s. Run 'gemini --version' to diagnose.",
                        "degraded")

    # Resolve model from config or defaults
    gdef = GEMINI_MODELS.get(task_type, GEMINI_MODELS.get("ask", {}))
    requested_model = model
    model = dcfg.get("gemini_default_model") or gdef.get("model", "gemini-2.5-flash")
    tier_meta: dict = {}
    if tier or requested_model:
        over, problem = _tier_overrides("gemini", tier, requested_model, dcfg)
        if problem:
            return finalize("c3_delegate", {"task_type": task_type, "backend": "gemini"},
                            f"[delegate:error] {problem}", "error")
        model = over.get("model") or model
        tier_meta = {"tier": over["tier"]}
    timeout = int(dcfg.get("gemini_timeout", 120))

    enriched = _pack_for(file_path, context, svc, dcfg)

    # Truncate context
    max_ctx = max(200, int(dcfg.get("gemini_max_context_tokens", 8000) or 8000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4]

    # Cache check
    ckey = hashlib.md5(f"gemini|{task_type}|{model}|{enriched}|{task}".encode()).hexdigest()
    if ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {"task_type": task_type, "backend": "gemini", **tier_meta,
                                        "model": model, "cached": True},
                        cached_resp, "cached")

    # Run Gemini
    _log_progress(svc, f"[delegate] Gemini {model}...")
    t0 = time.monotonic()
    output, ok, token_stats = _run_gemini(
        task=task, context=enriched,
        model=model, timeout=timeout,
        cwd=str(svc.project_path),
    )
    elapsed = round(time.monotonic() - t0, 1)

    if not ok:
        if breaker.record_failure():
            _notify_backend_degraded(svc, "gemini", breaker)
        return finalize("c3_delegate",
                        {"task_type": task_type, "backend": "gemini", **tier_meta, "model": model,
                         "elapsed": f"{elapsed}s"},
                        output, "error")

    breaker.record_success()
    _delegate_metrics["total_calls"] += 1
    _delegate_cache[ckey] = (output, count_tokens(output))

    # Memory bridge
    _gemini_memory_bridge(output, task_type, task, svc)

    return finalize("c3_delegate",
                    {"task_type": task_type, "backend": "gemini", **tier_meta, "model": model,
                     "elapsed": f"{elapsed}s", **token_stats},
                    output, "ok")


def _gemini_memory_bridge(output: str, task_type: str, task: str, svc):
    """Auto-extract key findings from Gemini responses into c3_memory."""
    try:
        memory = getattr(svc, "memory", None)
        if not memory:
            return
        dcfg = svc.delegate_config or {}
        if not dcfg.get("gemini_memory_bridge", True):
            return

        tokens = count_tokens(output)
        if tokens < 50 or tokens > 3000:
            return

        lower = output.lower()
        benign = ("no issues", "looks good", "no problems", "lgtm", "all good",
                  "no regressions", "no bugs")
        if any(b in lower for b in benign):
            return

        summary = output[:400].strip()
        if len(output) > 400:
            summary += "..."

        fact = f"[gemini:{task_type}] {task[:80]} -- {summary}"
        memory.remember(fact, category=f"gemini_{task_type}")
        log.debug("gemini_memory_bridge: stored fact for task_type=%s", task_type)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Grok delegate handler
# ---------------------------------------------------------------------------

def _handle_grok_delegate(task: str, task_type: str, context: str,
                          file_path: str, svc, dcfg: dict, finalize,
                          tier: str = "", model: str = "") -> str:
    """Handle delegation via xAI's Grok Build CLI.

    Read-only by default (temp cwd, read-only tool allowlist, file context
    inlined). ``grok_allow_write`` runs --yolo in the project instead, which
    loads the project's trusted .grok MCP servers and hooks.
    """
    base = {"task_type": task_type, "backend": "grok"}
    if not dcfg.get("grok_enabled", False):
        return finalize("c3_delegate", base,
                        "[delegate:error] Grok not enabled. Set delegate.grok_enabled=true in .c3/config.json",
                        "disabled")

    global _grok_available
    if _grok_available is None:
        check_grok()
    if not _grok_available:
        return finalize("c3_delegate", base,
                        "[delegate:error] Grok CLI not available. Run 'grok --version' to diagnose.",
                        "unavailable")

    breaker = _backend_breaker("grok", dcfg)
    if not breaker.allow():
        return finalize("c3_delegate", base,
                        "[delegate:degraded] Grok skipped after repeated failures; retrying in "
                        f"~{breaker.cooldown_remaining()}s. Run 'grok --version' to diagnose.",
                        "degraded")

    requested_model = model
    model = str(dcfg.get("grok_model") or "")
    effort = ""
    if tier or requested_model:
        over, problem = _tier_overrides("grok", tier, requested_model, dcfg)
        if problem:
            return finalize("c3_delegate", base, f"[delegate:error] {problem}", "error")
        model = over.get("model") or model
        effort = over.get("effort", "")
        base = {**base, "tier": over["tier"]}
    timeout = int(dcfg.get("grok_timeout", 120) or 120)
    max_turns = int(dcfg.get("grok_max_turns", 8) or 8)
    allow_write = bool(dcfg.get("grok_allow_write", False))

    enriched = _pack_for(file_path, context, svc, dcfg)

    max_ctx = max(200, int(dcfg.get("grok_max_context_tokens", 8000) or 8000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4]

    # Only read-only answers are cacheable; a write run has side effects.
    ckey = hashlib.md5(
        f"grok|{svc.project_path}|{task_type}|{model}|{effort}|{max_turns}|{enriched}|{task}".encode()
    ).hexdigest()
    if not allow_write and ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {**base, "cached": True}, cached_resp, "cached")

    mode = "write" if allow_write else "read-only"
    _log_progress(svc, f"[delegate] Grok {model or 'cli-default'} ({mode}, max_turns={max_turns})...")
    t0 = time.monotonic()
    output, ok, token_stats = _run_grok(
        task=task, context=enriched, model=model, timeout=timeout,
        max_turns=max_turns, allow_write=allow_write, reasoning_effort=effort,
        cwd=str(svc.project_path) if allow_write else None,
    )
    elapsed = round(time.monotonic() - t0, 1)
    meta = {**base, "model": model or "cli-default", "mode": mode, "elapsed": f"{elapsed}s"}
    if effort:
        meta["effort"] = effort

    if not ok:
        if breaker.record_failure():
            _notify_backend_degraded(svc, "grok", breaker)
        return finalize("c3_delegate", meta, output, "error")

    breaker.record_success()
    _delegate_metrics["total_calls"] += 1
    if not allow_write:
        _delegate_cache[ckey] = (output, count_tokens(output))
    return finalize("c3_delegate", {**meta, **token_stats}, output, "ok")


# ---------------------------------------------------------------------------
# Telemetry (D0 of the delegate remediation, docs/delegate-eval.md)
# ---------------------------------------------------------------------------

# Measured 2026-09-14 over 65 projects: 19 c3_delegate calls since July, and
# the telemetry could not say which backend, model or outcome any of them had
# — the args sat in the activity log, the cost nowhere. Every response now
# lands one flat `detail` on its .c3/tool_telemetry.jsonl record.

_PROBE_TASK_TYPES = frozenset({"available", "codex_check", "gemini_check", "grok_check", "ping"})
_OUTCOMES = frozenset({"ok", "cached", "error", "timeout", "blocked", "disabled",
                       "unavailable", "degraded"})
_CONFIDENCE = frozenset({"high", "medium", "low"})
_USAGE_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "cache_read_tokens",
               "cache_write_tokens", "reasoning_tokens", "cost_usd", "turns",
               "files_changed", "lines_added", "lines_removed", "denied")


def _delegate_outcome(status) -> str:
    s = str(status or "").strip().lower()
    if s in _CONFIDENCE:
        return "ok"
    if s in _OUTCOMES:
        return s
    return s[:40] or "unknown"


def delegate_telemetry_detail(meta: dict | None, status, *, requested_backend: str,
                              task_type: str, host: str, wall_ms: float) -> dict:
    """The flat ``detail`` one c3_delegate call writes to telemetry.

    Pure: built from the meta dict the handler passed to ``finalize`` plus
    what handle_delegate knew before routing. Usage keys are copied only when
    the backend reported them, so an absent count stays absent rather than
    reading as zero.
    """
    meta = meta if isinstance(meta, dict) else {}
    raw_status = str(status or "").strip().lower()
    resolved_type = str(meta.get("task_type") or meta.get("task") or task_type or "")
    backend = str(meta.get("backend") or "")
    if not backend:
        backend = "probe" if resolved_type in _PROBE_TASK_TYPES else "ollama"
    detail: dict = {
        "host": host or "",
        "backend": backend,
        "backend_requested": requested_backend or "",
        "task_type": resolved_type,
        "outcome": _delegate_outcome(raw_status),
        "wall_ms": round(float(wall_ms), 1),
    }
    if resolved_type in _PROBE_TASK_TYPES:
        detail["probe"] = True
    for key in ("tier", "model", "mode", "effort"):
        if meta.get(key):
            detail[key] = str(meta[key])
    if meta.get("thinking_cap") is not None and not isinstance(meta.get("thinking_cap"), bool):
        try:
            detail["thinking_cap"] = int(meta["thinking_cap"])
        except (TypeError, ValueError):
            pass
    if raw_status in _CONFIDENCE:
        detail["confidence"] = raw_status
    if meta.get("cached"):
        detail["outcome"] = "cached"
    if meta.get("cascade"):
        detail["cascade"] = True
    for key in _USAGE_KEYS:
        value = meta.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            detail[key] = float(value) if key == "cost_usd" else int(value)
        except (TypeError, ValueError):
            continue
    return detail


def _telemetry_finalize(finalize, svc, *, requested_backend: str, task_type: str):
    """Wrap ``finalize`` so every exit records a delegate ``detail``.

    The clock starts here, at the top of handle_delegate, so wall_ms covers
    routing, context packing and the subprocess. Failure-safe: accounting
    never changes or breaks the response.
    """
    t0 = time.monotonic()
    host = host_provider(svc)

    def wrapped(tool, meta, output, status, **kw):
        try:
            session_mgr = getattr(svc, "session_mgr", None)
            if session_mgr is not None:
                wall_ms = (time.monotonic() - t0) * 1000
                session_mgr.record_tool_tokens(
                    "c3_delegate", duration_ms=round(wall_ms, 1),
                    detail=delegate_telemetry_detail(
                        meta, status, requested_backend=requested_backend,
                        task_type=task_type, host=host, wall_ms=wall_ms))
        except Exception:
            pass
        return finalize(tool, meta, output, status, **kw)
    return wrapped


PING_TASK = "Reply with exactly: OK"


def _ping_finalize(finalize):
    """task_type='ping': a one-line live call that proves auth, not just --version."""
    def wrapped(tool, meta, output, status, **kw):
        meta = {**(meta or {}), "task_type": "ping"}
        facts = [f"{k}={meta[k]}" for k in ("backend", "tier", "model", "elapsed") if meta.get(k)]
        if meta.get("cost_usd") is not None:
            facts.append(f"cost=${float(meta['cost_usd']):.4f}")
        outcome = "ok" if _delegate_outcome(status) in ("ok", "cached") else _delegate_outcome(status)
        first = (output or "").strip().splitlines()[0][:300] if (output or "").strip() else ""
        text = f"[delegate:ping] {outcome} {' '.join(facts)}\n{first}".rstrip()
        return finalize(tool, meta, text, status, **kw)
    return wrapped


# Backends that can look things up in the project: claude as a guarded scout,
# codex because its read-only sandbox already runs in the project.
_SCOUT_BACKENDS = ("claude", "codex")
_NOT_WRITE_TASKS = ("ping", "available", "codex_check", "gemini_check", "grok_check",
                    "codex_resume")


def _route_write(task: str, task_type: str, context: str, file_path: str, svc, finalize,
                 backend: str, tier: str, model: str, route: dict, write_paths) -> str:
    """``write_paths`` given: write mode, which only the claude backend has.

    It never cascades — a write lands on the backend the caller chose or
    nowhere — and it does not need allow_write_delegation: the worker runs
    under Access Guard in-band, unlike gemini or grok in write mode.
    """
    dcfg = svc.delegate_config or {}
    meta = {"task_type": task_type, "backend": backend, "mode": "write"}
    if not dcfg.get("enabled", True):
        return finalize("c3_delegate", meta, "[delegate:disabled]", "disabled")
    if task_type in _NOT_WRITE_TASKS:
        return finalize("c3_delegate", meta,
                        f"[delegate:error] write_paths does not apply to task_type '{task_type}'.",
                        "error")
    if backend == "host":
        provider, mapped = host_backend(svc)
        if mapped != "claude":
            return finalize("c3_delegate", meta,
                            f"[delegate:error] write mode runs on the claude backend only; host "
                            f"{provider or 'unknown'} maps to {mapped or 'nothing'}. "
                            "Pass backend='claude' or make the change yourself.", "error")
        backend = "claude"
    if backend != "claude":
        return finalize("c3_delegate", {**meta, "backend": backend},
                        f"[delegate:error] write mode runs on the claude backend only, not "
                        f"'{backend}'.", "error")
    route["backend"] = "claude"
    if not model:
        route["tier"] = normalize_tier(tier, dcfg, claude_default_tier_key(False, write=True))
    return _handle_claude_write(task, task_type, context, file_path, svc, dcfg, finalize,
                                tier=tier, model=model, write_paths=write_paths)


def handle_delegate(task: str, task_type: str, context: str, file_path: str,
                    svc, finalize, backend: str = "ollama",
                    allow_write_delegation: bool = False,
                    tier: str = "", model: str = "", scout: bool = False,
                    write_paths: str = "") -> str:
    finalize = _telemetry_finalize(finalize, svc, requested_backend=backend,
                                   task_type=task_type)
    route = {"backend": backend}
    try:
        return _route_delegate(task, task_type, context, file_path, svc, finalize, backend,
                               allow_write_delegation, tier, model, scout, route,
                               write_paths=write_paths)
    except access_guard.AccessDenied as exc:
        # A guard refusal while packing file_path is the answer, not a crash:
        # the refusal text goes back as the response (the agent reads the
        # same S1 line c3_read would give) and telemetry counts it as blocked,
        # under the backend routing had chosen (not 'host' or 'auto').
        meta = {"task_type": task_type, "backend": route["backend"]}
        if route.get("tier"):
            meta["tier"] = route["tier"]
        return finalize("c3_delegate", meta, exc.message, "blocked")


def _route_delegate(task: str, task_type: str, context: str, file_path: str,
                    svc, finalize, backend: str, allow_write_delegation: bool,
                    tier: str, model: str, scout: bool, route: dict | None = None,
                    write_paths: str = "") -> str:
    route = route if route is not None else {}
    if str(write_paths or "").strip():
        return _route_write(task, task_type, context, file_path, svc, finalize, backend,
                            tier, model, route, write_paths)
    if task_type == "ping":
        finalize = _ping_finalize(finalize)
        task, context, file_path, task_type = PING_TASK, "", "", "ask"
    dcfg = svc.delegate_config or {}
    if not dcfg.get("enabled", True):
        return finalize("c3_delegate", {"task_type": task_type, "backend": backend},
                        "[delegate:disabled]", "disabled")

    # ── Access Guard (T2c) delegation posture ──────────────────────────────
    # With ZERO user rules `_guard_active` is False and every branch below is
    # inert — behavior is byte-identical to the pre-guard tool. When rules
    # exist: codex is pinned to --sandbox read-only; backends that run
    # autonomously with potential write access (gemini --approval-mode yolo,
    # grok with grok_allow_write running --yolo in the project, codex_resume
    # reusing an unpinnable prior-session sandbox) require the explicit
    # allow_write_delegation=true user opt-in. Evaluator errors fail closed.
    try:
        _guard_active = access_guard.has_active_rules(str(svc.project_path))
    except Exception:
        _guard_active = True

    # --- Health checks -----------------------------------------------------
    if task_type == "available":
        # Parallel health check across all backends
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}

        def _check_ollama():
            ollama = svc.ollama_client
            if not ollama:
                return "ollama", "down", "client=None", []
            up = ollama.is_available()
            models = ollama.list_models() if up else []
            return "ollama", "up" if up else "down", "", models or []

        def _check_codex():
            info = check_codex()
            s = info.get("status", "unknown")
            d = info.get("version") or info.get("detail", "")
            return "codex", s, d, []

        def _check_gemini():
            info = check_gemini()
            s = info.get("status", "unknown")
            d = info.get("version") or info.get("detail", "")
            return "gemini", s, d, []

        def _check_claude():
            info = check_claude()
            s = info.get("status", "unknown")
            d = info.get("version") or info.get("detail", "")
            return "claude", s, d, []

        def _check_grok():
            info = check_grok()
            s = info.get("status", "unknown")
            d = info.get("version") or info.get("detail", "")
            return "grok", s, d, []

        checkers = [("ollama", _check_ollama), ("codex", _check_codex),
                    ("gemini", _check_gemini), ("claude", _check_claude),
                    ("grok", _check_grok)]
        names = [name for name, _fn in checkers]
        total = len(checkers)
        with ThreadPoolExecutor(max_workers=total) as pool:
            futs = [pool.submit(fn) for _name, fn in checkers]
            for fut in as_completed(futs):
                name, status, detail, models = fut.result()
                results[name] = (status, detail, models)

        lines = []
        for name in names:
            status, detail, models = results.get(name, ("unknown", "", []))
            line = f"  {name}={status}"
            if detail:
                line += f" {detail}"
            if models:
                line += f" models={len(models)} [{', '.join(models[:5])}]"
            lines.append(line)

        provider, mapped = host_backend(svc)
        if mapped:
            host_tier = normalize_tier("", dcfg, "claude_default_tier" if mapped == "claude" else "")
            if mapped == "claude":
                host_tier += f", scout {normalize_tier('', dcfg, claude_default_tier_key(True))}"
            lines.append(f"  host={provider} -> {mapped} (default tier {host_tier}); "
                         "task_type='ping' makes a live call")
        else:
            lines.append(f"  host={provider or 'unknown'} -> no same-provider backend (auto)")

        summary_statuses = [results.get(n, ("unknown",))[0] for n in names]
        up_count = sum(1 for s in summary_statuses if s in ("up", "ok"))
        return finalize("c3_delegate", {"task_type": "available"},
                        f"[delegate:available] {up_count}/{total} backends up (--version only)\n"
                        + "\n".join(lines),
                        f"{up_count}/{total} up")

    if task_type == "codex_check":
        info = check_codex()
        status = info.get("status", "unknown")
        detail = info.get("version") or info.get("detail", "")
        return finalize("c3_delegate", {"task_type": "codex_check"},
                        f"[delegate:codex_check] status={status} {detail}".strip(),
                        status)

    if task_type == "codex_resume":
        if not dcfg.get("codex_enabled", False):
            return finalize("c3_delegate", {"task_type": "codex_resume"},
                            "[delegate:error] Codex not enabled in config", "disabled")
        if _guard_active and not allow_write_delegation:
            return finalize(
                "c3_delegate", {"task_type": "codex_resume"},
                "[delegate:blocked] Access Guard rules are active and "
                "codex_resume reuses the previous session's sandbox, which C3 "
                "cannot pin to read-only. Re-run with "
                "allow_write_delegation=true (explicit user opt-in) or run "
                "codex directly.",
                "blocked")
        timeout = int(dcfg.get("codex_timeout", 120))
        from cli.tools._grants import session_id
        output, ok = _run_codex_resume(task, timeout=timeout,
                                        cwd=str(svc.project_path), origin_id=session_id(svc))
        return finalize("c3_delegate", {"task_type": "codex_resume"},
                        output, "ok" if ok else "error")

    if task_type == "gemini_check":
        info = check_gemini()
        status = info.get("status", "unknown")
        detail = info.get("version") or info.get("detail", "")
        return finalize("c3_delegate", {"task_type": "gemini_check"},
                        f"[delegate:gemini_check] status={status} {detail}".strip(),
                        status)

    if task_type == "grok_check":
        info = check_grok()
        status = info.get("status", "unknown")
        detail = info.get("version") or info.get("detail", "")
        return finalize("c3_delegate", {"task_type": "grok_check"},
                        f"[delegate:grok_check] status={status} {detail}".strip(),
                        status)

    # --- Backend routing ---------------------------------------------------
    route_tier = tier
    if backend == "host":
        # The same provider the calling agent runs on, one tier down by
        # default (delegate.default_tier). A host with no backend of its own,
        # or whose backend the guard would block, falls back to auto.
        provider, mapped = host_backend(svc)
        blocked = bool(mapped) and (_guard_active and not allow_write_delegation
                                    and _write_capable(mapped, dcfg))
        if scout and mapped and mapped not in _SCOUT_BACKENDS:
            return finalize("c3_delegate", {"task_type": task_type, "backend": mapped},
                            f"[delegate:error] scout needs a backend that can read the project "
                            f"({' or '.join(_SCOUT_BACKENDS)}); host {provider} maps to {mapped}. "
                            "Pass backend='claude' or backend='codex'.", "error")
        if mapped and not blocked:
            backend = mapped
            if not tier and not model:
                route_tier = normalize_tier(
                    "", dcfg, claude_default_tier_key(scout) if mapped == "claude" else "")
        else:
            why = (f"host {provider} -> {mapped} blocked by Access Guard (write-capable)" if blocked
                   else f"host {provider or 'unknown'} has no same-provider backend")
            note = f"[delegate] {why} -> auto"
            _log_progress(svc, note)
            finalize = _with_cascade_note(finalize, note)
            backend = "auto"

    if backend == "auto":
        # Cascade: walk the ordered preference list for this task type and use
        # the first backend that is enabled, installed, and whose breaker is
        # closed. Heavy tasks: codex -> gemini -> grok -> ollama. Light tasks:
        # ollama first, cloud CLIs only when Ollama itself is down.
        skips: list[str] = []
        chosen = ""
        for cand in _cascade_order(task_type, dcfg, host=host_backend(svc)[1]):
            if scout and cand not in _SCOUT_BACKENDS:
                continue
            if (_guard_active and not allow_write_delegation
                    and _write_capable(cand, dcfg)):
                skips.append(f"{cand} blocked by Access Guard (write-capable; "
                             "allow_write_delegation=false)")
                continue
            reason = _cascade_skip_reason(cand, dcfg, svc)
            if reason is None:
                chosen = cand
                break
            skips.append(f"{cand} {reason}")
        if not chosen:
            detail = "; ".join(skips) or "no backends configured"
            return finalize("c3_delegate",
                            {"task_type": task_type, "backend": "auto", "cascade": detail},
                            f"[delegate:error] No healthy backend for auto routing ({detail}). "
                            "Run c3_delegate task_type='available' to diagnose.",
                            "unavailable")
        backend = chosen
        if skips:
            cascade_note = f"[delegate] {'; '.join(skips)} -> routed to {chosen}"
            _log_progress(svc, cascade_note)
            finalize = _with_cascade_note(finalize, cascade_note)

    if _write_capable(backend, dcfg) and _guard_active and not allow_write_delegation:
        detail = {
            "gemini": "--approval-mode yolo auto-approves writes",
            "grok": ("grok_allow_write runs --yolo in the project and loads its "
                     "trusted .grok MCP servers and hooks"),
        }[backend]
        return finalize(
            "c3_delegate", {"task_type": task_type, "backend": backend},
            f"[delegate:blocked] Access Guard rules are active and the "
            f"'{backend}' backend runs autonomously with potential write "
            f"access ({detail}) that C3 cannot fence to the guard's rules. "
            "Re-run with allow_write_delegation=true (explicit user opt-in) "
            f"or run {backend} directly.",
            "blocked")

    route["backend"] = backend
    if backend == "claude" and not model:
        route["tier"] = normalize_tier(route_tier, dcfg, claude_default_tier_key(scout))
    elif route_tier:
        route["tier"] = route_tier

    # Tier/model reach a handler only when asked for (or set by host routing),
    # so an explicit backend with neither keeps its configured behaviour.
    tier_kw = {k: v for k, v in (("tier", route_tier), ("model", model)) if v}

    if scout and backend not in _SCOUT_BACKENDS:
        return finalize("c3_delegate", {"task_type": task_type, "backend": backend},
                        f"[delegate:error] scout is not available on backend '{backend}' "
                        f"(use {' or '.join(_SCOUT_BACKENDS)}).", "error")

    if backend == "codex":
        _log_progress(svc, f"[delegate] Routing {task_type} → Codex...")
        return _handle_codex_delegate(task, task_type, context, file_path, svc, dcfg, finalize,
                                      **tier_kw)

    if backend == "gemini":
        _log_progress(svc, f"[delegate] Routing {task_type} → Gemini...")
        return _handle_gemini_delegate(task, task_type, context, file_path, svc, dcfg, finalize,
                                       **tier_kw)

    if backend == "claude":
        return _handle_claude_delegate(task, task_type, context, file_path, svc, dcfg, finalize,
                                       tier=route_tier, model=model, scout=scout)

    if backend == "grok":
        _log_progress(svc, f"[delegate] Routing {task_type} → Grok...")
        return _handle_grok_delegate(task, task_type, context, file_path, svc, dcfg, finalize,
                                     **tier_kw)

    if backend != "ollama":
        return finalize("c3_delegate", {"task_type": task_type, "backend": backend},
                        f"[delegate:error] Unknown backend: {backend} "
                        "(use host, claude, codex, gemini, grok, ollama or auto)", "error")

    # --- Original Ollama path (backend="ollama") ---------------------------

    if task_type == "auto":
        task_type = infer_task_type(task, context)

    tdef = DELEGATE_TASKS.get(task_type)
    if not tdef:
        return finalize("c3_delegate", {"task_type": task_type, "backend": "ollama"},
                        f"[delegate:error] Unknown type: {task_type}", "error")
    ollama = svc.ollama_client
    if not ollama or not ollama.is_available():
        return finalize("c3_delegate", {"task_type": task_type, "backend": "ollama"},
                        "[delegate:error] Ollama unavailable. Requires Ollama for local LLM tasks.",
                        "unavailable")

    # Context enrichment
    enriched = _pack_for(file_path, context, svc, dcfg)

    if task_type == "diagnose" and dcfg.get("auto_activity_log", True):
        recent = svc.activity_log.get_recent(limit=8)
        if recent:
            enriched += "\nRecent Activity:\n" + "\n".join(
                [f"[{e.get('timestamp','').split('T')[-1][:8]}] {e.get('tool','')}..."
                 for e in reversed(recent)])

    max_context_tokens = max(200, int(dcfg.get("max_context_tokens", 1400) or 1400))
    if count_tokens(enriched) > max_context_tokens:
        enriched = enriched[:max_context_tokens * 4]

    # Model resolution. Only an exact configured name may select an Ollama
    # Cloud tag: prefix/substring matching and the fallback walk see local
    # tags only, so a prompt never leaves the machine by accident (#182).
    req_model = (model or dcfg.get(f"{task_type}_model") or dcfg.get("preferred_model")
                 or tdef["default_model"])
    avail = ollama.list_models() or []
    local = [m for m in avail if not is_cloud_tag(m)]
    model = next((m for m in avail if m.lower() == req_model.strip().lower()), "")
    if not model:
        model = resolve_model_name(req_model, local)
    if not model:
        for cand in _fallback_model_order(task_type) + local:
            model = resolve_model_name(cand, local)
            if model:
                break
    if not model:
        return finalize("c3_delegate", {"task_type": task_type, "backend": "ollama"},
                        "[delegate:error] No compatible local model found", "unavailable")

    # Cache check
    ckey = hashlib.md5(f"{task_type}|{model}|{enriched}|{task}".encode()).hexdigest()
    if ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {"task_type": task_type, "backend": "ollama",
                                        "model": model, "cached": True},
                        cached_resp, "cached")

    # Generate
    _log_progress(svc, f"[delegate] Running Ollama ({model})...")
    timeout_s = int(dcfg.get("timeout", 90) or 90)
    _t0 = time.monotonic()
    resp = ollama.generate(
        prompt=tdef["prompt_template"].format(context=enriched, task=task),
        model=model, system=tdef["system"],
        temperature=tdef.get("temperature", 0.3),
        max_tokens=int(dcfg.get("max_tokens", 512) or 512),
        timeout=timeout_s)
    _elapsed = round(time.monotonic() - _t0, 1)
    if resp is None:
        return finalize("c3_delegate", {"task_type": task_type, "backend": "ollama", "model": model},
                        f"[delegate:timeout] No response from {model} after {_elapsed}s "
                        f"(limit {timeout_s}s)", "timeout")

    # Self-correction: retry with fallback model on low confidence
    conf = _estimate_confidence(task_type, resp, count_tokens(resp))
    if conf == "low" and dcfg.get("allow_model_fallback", True):
        tried = {model}
        for fallback_cand in _fallback_model_order(task_type) + local:
            fallback = resolve_model_name(fallback_cand, local)
            if not fallback or fallback in tried:
                continue
            tried.add(fallback)
            retry_resp = ollama.generate(
                prompt=tdef["prompt_template"].format(context=enriched, task=task),
                model=fallback, system=tdef["system"],
                temperature=tdef.get("temperature", 0.3),
                max_tokens=int(dcfg.get("max_tokens", 512) or 512),
                timeout=timeout_s)
            if retry_resp is None:
                # Timeout/failure on the fallback — not a valid empty answer.
                continue
            retry_conf = _estimate_confidence(task_type, retry_resp, count_tokens(retry_resp))
            if retry_conf != "low":
                resp = retry_resp
                conf = retry_conf
                model = fallback
                break
            if retry_conf == "low" and count_tokens(retry_resp) > count_tokens(resp):
                resp = retry_resp
                model = fallback
                conf = "medium"

    _delegate_metrics["total_calls"] += 1
    _delegate_cache[ckey] = (resp, count_tokens(resp))
    return finalize("c3_delegate", {"task": task_type, "backend": "ollama", "model": model,
                                    "elapsed": f"{_elapsed}s"},
                    resp, conf)
