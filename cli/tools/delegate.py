"""c3_delegate — LLM task offload via Ollama (local) or Codex CLI (cloud).

Absorbs former c3_intelligence routing logic internally.
Supports task_type='available' for zero-cost Ollama status check.
Supports backend='codex' for OpenAI Codex CLI delegation.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import count_tokens

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
            )
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _communicate_with_heartbeat(proc, timeout=45, idle_timeout=15):
    """communicate() replacement with idle-activity watchdog.

    Monitors stderr for activity. If no stderr output for idle_timeout seconds,
    kills the process early (catches MCP startup hangs). Also enforces total timeout.

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

    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_parts), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(proc.stderr, stderr_parts, True), daemon=True)
    t_out.start()
    t_err.start()

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


# ---------------------------------------------------------------------------
# Codex CLI backend
# ---------------------------------------------------------------------------

CODEX_MODELS = {
    "review":   {"model": "gpt-5.3-codex-spark", "sandbox": "read-only",      "reasoning": "high"},
    "explain":  {"model": "gpt-5.3-codex-spark", "sandbox": "read-only",      "reasoning": "medium"},
    "improve":  {"model": "gpt-5.4",             "sandbox": "read-only",      "reasoning": "high"},
    "diagnose": {"model": "gpt-5.3-codex",       "sandbox": "read-only",      "reasoning": "high"},
    "test":     {"model": "gpt-5.3-codex-spark", "sandbox": "workspace-write", "reasoning": "medium"},
    "summarize":{"model": "gpt-5.3-codex-spark", "sandbox": "read-only",      "reasoning": "low"},
    "docstring":{"model": "gpt-5.3-codex-spark", "sandbox": "read-only",      "reasoning": "low"},
    "ask":      {"model": "gpt-5.3-codex-spark", "sandbox": "read-only",      "reasoning": "medium"},
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
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            _claude_available = True
            return {"status": "ok", "version": proc.stdout.strip()}
        _claude_available = False
        return {"status": "error", "detail": proc.stderr.strip() or f"exit {proc.returncode}"}
    except subprocess.TimeoutExpired:
        _claude_available = False
        return {"status": "timeout", "detail": "claude --version timed out (10s)"}
    except Exception as e:
        _claude_available = False
        return {"status": "error", "detail": str(e)}


def _run_claude(task: str, context: str, cwd: str | None = None,
                timeout: int = 90, idle_timeout: int = 30) -> tuple:
    """Run claude -p in non-interactive print mode. Returns (output, success)."""
    exe = _which("claude")
    if not exe:
        return "[claude:error] claude CLI not on PATH", False
    prompt = f"Context:\n{context}\n\nTask:\n{task}" if context else task
    cmd = [exe, "-p", prompt, "--output-format", "text"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, cwd=cwd,
            **_popen_kwargs(),
        )
        output, err = _communicate_with_heartbeat(proc, timeout=timeout, idle_timeout=idle_timeout)
        if proc.returncode == 0 and output.strip():
            return output.strip(), True
        return f"[claude:error] {(err or '').strip() or 'no output'}", False
    except Exception as e:
        return f"[claude:error] {e}", False


def _claude_memory_bridge(output: str, task_type: str, task: str, svc) -> None:
    """Auto-extract key findings from Claude responses into c3_memory."""
    try:
        from services.auto_memory import _save_or_merge_standalone
        _save_or_merge_standalone(output[:400], f"auto:claude:{task_type}", svc)
    except Exception:
        pass


def _handle_claude_delegate(task: str, task_type: str, context: str,
                             file_path: str, svc, dcfg: dict, finalize) -> str:
    """Handle delegation via Claude Code CLI."""
    timeout = int(dcfg.get("claude_timeout", 90))
    _log_progress(svc, f"[delegate] Routing {task_type} → Claude CLI...")
    output, ok = _run_claude(task, context, cwd=str(svc.project_path), timeout=timeout)
    if not ok:
        return finalize("c3_delegate", {"task_type": task_type, "backend": "claude"},
                        output, "error")
    return finalize("c3_delegate", {"task_type": task_type, "backend": "claude"},
                    output, "ok")


def check_gemini() -> dict:
    """Zero-cost health check for Gemini CLI. Returns status dict."""
    global _gemini_available
    exe = _which("gemini")
    if not exe:
        _gemini_available = False
        return {"status": "not_installed", "detail": "gemini CLI not found on PATH"}
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            version = proc.stdout.strip()
            _gemini_available = True
            return {"status": "ok", "version": version}
        else:
            _gemini_available = False
            return {"status": "error", "detail": proc.stderr.strip() or f"exit code {proc.returncode}"}
    except subprocess.TimeoutExpired:
        _gemini_available = False
        return {"status": "timeout", "detail": "gemini --version timed out (10s)"}
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
            text=True,
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

    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_parts), daemon=True)
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
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
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
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            version = proc.stdout.strip()
            _codex_available = True
            return {"status": "ok", "version": version}
        else:
            _codex_available = False
            return {"status": "error", "detail": proc.stderr.strip() or f"exit code {proc.returncode}"}
    except subprocess.TimeoutExpired:
        _codex_available = False
        return {"status": "timeout", "detail": "codex --version timed out (10s)"}
    except Exception as e:
        _codex_available = False
        return {"status": "error", "detail": str(e)}


def _run_codex(task: str, context: str, model: str, sandbox: str,
               reasoning: str = "high", timeout: int = 120,
               idle_timeout: int = 20,
               cwd: str | None = None) -> tuple[str, bool]:
    """Run codex exec as a subprocess. Returns (output, success).

    Uses heartbeat monitor: kills process if no stderr activity for idle_timeout
    seconds (catches MCP startup hangs). Also enforces total timeout.
    """
    prompt = f"{task}\n\nContext:\n{context}" if context else task
    codex_exe = _which("codex") or "codex"
    cmd = [
        codex_exe, "exec",
        "-m", model,
        "--config", f"model_reasoning_effort={reasoning}",
        "--sandbox", sandbox,
        "--full-auto",
        "--skip-git-repo-check",
        prompt,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=cwd,
            **_popen_kwargs(),
        )
        stdout, stderr, status = _communicate_with_heartbeat(
            proc, timeout=timeout, idle_timeout=idle_timeout,
        )
        if status == "idle_timeout":
            return (f"[codex:idle_timeout] No stderr activity for {idle_timeout}s "
                    f"(likely MCP startup hang)"), False
        if status == "timeout":
            return f"[codex:timeout] No response after {timeout}s", False

        if proc.returncode != 0:
            err = stderr.strip() if stderr else f"exit code {proc.returncode}"
            return f"[codex:error] {err}", False

        return stdout.strip(), True
    except FileNotFoundError:
        return "[codex:error] codex CLI not found on PATH", False
    except Exception as e:
        return f"[codex:error] {e}", False


def _run_codex_resume(follow_up: str, timeout: int = 120,
                      cwd: str | None = None) -> tuple[str, bool]:
    """Resume last Codex session with a follow-up prompt."""
    cmd = ["codex", "exec", "--skip-git-repo-check", "resume", "--last"]
    try:
        import sys
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        try:
            stdout, stderr = proc.communicate(input=follow_up, timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, stdin=subprocess.DEVNULL,
                )
            else:
                proc.kill()
            proc.wait(timeout=5)
            return f"[codex:timeout] Resume timed out after {timeout}s", False

        if proc.returncode != 0:
            err = stderr.strip() if stderr else f"exit code {proc.returncode}"
            return f"[codex:error] {err}", False

        return stdout.strip(), True
    except Exception as e:
        return f"[codex:error] {e}", False


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

def _handle_codex_delegate(task: str, task_type: str, context: str,
                           file_path: str, svc, dcfg: dict, finalize) -> str:
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

    # Resolve model/sandbox/reasoning from config or defaults
    cdef = CODEX_MODELS.get(task_type, CODEX_MODELS.get("ask", {}))
    model = dcfg.get("codex_default_model") or cdef.get("model", "gpt-5.3-codex-spark")
    sandbox = dcfg.get("codex_default_sandbox") or cdef.get("sandbox", "read-only")
    reasoning = dcfg.get("codex_reasoning_effort") or cdef.get("reasoning", "high")
    timeout = int(dcfg.get("codex_timeout", 120))

    # Context enrichment (reuse existing pattern)
    enriched = context
    if file_path and dcfg.get("auto_compress", True):
        for p in [p.strip() for p in file_path.split(",") if p.strip()]:
            try:
                res = svc.compressor.compress_file(str(Path(svc.project_path) / p), "smart")
                if isinstance(res, dict) and res.get("compressed"):
                    enriched += f"\n--- file: {p} ---\n{res['compressed']}"
            except Exception:
                continue

    # Truncate context to avoid blowing Codex's input
    max_ctx = max(200, int(dcfg.get("codex_max_context_tokens", 4000) or 4000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4]

    # Cache check
    ckey = hashlib.md5(f"codex|{task_type}|{model}|{enriched}|{task}".encode()).hexdigest()
    if ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {"task_type": task_type, "backend": "codex", "cached": True},
                        cached_resp, "cached")

    # Run Codex
    _log_progress(svc, f"[delegate] Codex {model} ({sandbox}, reasoning={reasoning})...")
    t0 = time.monotonic()
    output, ok = _run_codex(
        task=task, context=enriched,
        model=model, sandbox=sandbox,
        reasoning=reasoning, timeout=timeout,
        cwd=str(svc.project_path),
    )
    elapsed = round(time.monotonic() - t0, 1)

    if not ok:
        return finalize("c3_delegate",
                        {"task_type": task_type, "backend": "codex", "model": model, "elapsed": f"{elapsed}s"},
                        output, "error")

    _delegate_metrics["total_calls"] += 1
    _delegate_cache[ckey] = (output, count_tokens(output))

    # Memory bridge — auto-extract key findings from substantial Codex responses
    _codex_memory_bridge(output, task_type, task, svc)

    return finalize("c3_delegate",
                    {"task_type": task_type, "backend": "codex", "model": model, "elapsed": f"{elapsed}s"},
                    output, "ok")


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
                            file_path: str, svc, dcfg: dict, finalize) -> str:
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

    # Resolve model from config or defaults
    gdef = GEMINI_MODELS.get(task_type, GEMINI_MODELS.get("ask", {}))
    model = dcfg.get("gemini_default_model") or gdef.get("model", "gemini-2.5-flash")
    timeout = int(dcfg.get("gemini_timeout", 120))

    # Context enrichment (reuse existing pattern)
    enriched = context
    if file_path and dcfg.get("auto_compress", True):
        for p in [p.strip() for p in file_path.split(",") if p.strip()]:
            try:
                res = svc.compressor.compress_file(str(Path(svc.project_path) / p), "smart")
                if isinstance(res, dict) and res.get("compressed"):
                    enriched += f"\n--- file: {p} ---\n{res['compressed']}"
            except Exception:
                continue

    # Truncate context
    max_ctx = max(200, int(dcfg.get("gemini_max_context_tokens", 8000) or 8000))
    if count_tokens(enriched) > max_ctx:
        enriched = enriched[:max_ctx * 4]

    # Cache check
    ckey = hashlib.md5(f"gemini|{task_type}|{model}|{enriched}|{task}".encode()).hexdigest()
    if ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {"task_type": task_type, "backend": "gemini", "cached": True},
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
        return finalize("c3_delegate",
                        {"task_type": task_type, "backend": "gemini", "model": model, "elapsed": f"{elapsed}s"},
                        output, "error")

    _delegate_metrics["total_calls"] += 1
    _delegate_cache[ckey] = (output, count_tokens(output))

    # Memory bridge
    _gemini_memory_bridge(output, task_type, task, svc)

    return finalize("c3_delegate",
                    {"task_type": task_type, "backend": "gemini", "model": model,
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


def handle_delegate(task: str, task_type: str, context: str, file_path: str,
                    svc, finalize, backend: str = "ollama") -> str:
    dcfg = svc.delegate_config or {}
    if not dcfg.get("enabled", True):
        return "[delegate:disabled]"

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

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(fn) for fn in [_check_ollama, _check_codex, _check_gemini, _check_claude]]
            for fut in as_completed(futs):
                name, status, detail, models = fut.result()
                results[name] = (status, detail, models)

        lines = []
        for name in ("ollama", "codex", "gemini", "claude"):
            status, detail, models = results.get(name, ("unknown", "", []))
            line = f"  {name}={status}"
            if detail:
                line += f" {detail}"
            if models:
                line += f" models={len(models)} [{', '.join(models[:5])}]"
            lines.append(line)

        summary_statuses = [results.get(n, ("unknown",))[0] for n in ("ollama", "codex", "gemini", "claude")]
        up_count = sum(1 for s in summary_statuses if s in ("up", "ok"))
        return finalize("c3_delegate", {"task_type": "available"},
                        f"[delegate:available] {up_count}/4 backends up\n" + "\n".join(lines),
                        f"{up_count}/4 up")

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
        timeout = int(dcfg.get("codex_timeout", 120))
        output, ok = _run_codex_resume(task, timeout=timeout,
                                        cwd=str(svc.project_path))
        return finalize("c3_delegate", {"task_type": "codex_resume"},
                        output, "ok" if ok else "error")

    if task_type == "gemini_check":
        info = check_gemini()
        status = info.get("status", "unknown")
        detail = info.get("version") or info.get("detail", "")
        return finalize("c3_delegate", {"task_type": "gemini_check"},
                        f"[delegate:gemini_check] status={status} {detail}".strip(),
                        status)

    # --- Backend routing ---------------------------------------------------
    if backend == "auto":
        # Priority: Codex > Gemini > Ollama for heavy tasks
        heavy_codex = set(dcfg.get("codex_task_types", ["review", "diagnose", "improve", "test"]))
        heavy_gemini = set(dcfg.get("gemini_task_types", ["review", "diagnose", "improve", "test"]))
        # For heavy tasks, prefer cloud CLIs when available (faster than Ollama).
        # "Available" = pre-warm health check passed OR found on PATH.
        # The `enabled` config flag remains the primary gate, but availability
        # on-PATH is enough to prefer cloud over slow Ollama for heavy tasks.
        _light_tasks = {"ask", "explain", "summarize", "docstring"}
        _codex_avail = (_codex_available is True) or (
            _codex_available is None and task_type not in _light_tasks and _is_codex_on_path()
        )
        _gemini_avail = (_gemini_available is True) or (
            _gemini_available is None and task_type not in _light_tasks and _is_gemini_on_path()
        )
        if task_type in heavy_codex and _codex_avail and _codex_available is not False:
            backend = "codex"
        elif task_type in heavy_gemini and _gemini_avail and _gemini_available is not False:
            backend = "gemini"
        else:
            backend = "ollama"

    if backend == "codex":
        _log_progress(svc, f"[delegate] Routing {task_type} → Codex...")
        return _handle_codex_delegate(task, task_type, context, file_path, svc, dcfg, finalize)

    if backend == "gemini":
        _log_progress(svc, f"[delegate] Routing {task_type} → Gemini...")
        return _handle_gemini_delegate(task, task_type, context, file_path, svc, dcfg, finalize)

    if backend == "claude":
        return _handle_claude_delegate(task, task_type, context, file_path, svc, dcfg, finalize)

    # --- Original Ollama path (backend="ollama") ---------------------------

    if task_type == "auto":
        task_type = infer_task_type(task, context)

    tdef = DELEGATE_TASKS.get(task_type)
    if not tdef:
        return f"[delegate:error] Unknown type: {task_type}"
    ollama = svc.ollama_client
    if not ollama or not ollama.is_available():
        return "[delegate:error] Ollama unavailable. Requires Ollama for local LLM tasks."

    # Context enrichment
    enriched = context
    if file_path and dcfg.get("auto_compress", True):
        for p in [p.strip() for p in file_path.split(",") if p.strip()]:
            try:
                res = svc.compressor.compress_file(str(Path(svc.project_path) / p), "smart")
                if isinstance(res, dict) and res.get("compressed"):
                    enriched += f"\n--- file: {p} ---\n{res['compressed']}"
            except Exception:
                continue

    if task_type == "diagnose" and dcfg.get("auto_activity_log", True):
        recent = svc.activity_log.get_recent(limit=8)
        if recent:
            enriched += "\nRecent Activity:\n" + "\n".join(
                [f"[{e.get('timestamp','').split('T')[-1][:8]}] {e.get('tool','')}..."
                 for e in reversed(recent)])

    max_context_tokens = max(200, int(dcfg.get("max_context_tokens", 1400) or 1400))
    if count_tokens(enriched) > max_context_tokens:
        enriched = enriched[:max_context_tokens * 4]

    # Model resolution
    req_model = dcfg.get(f"{task_type}_model") or dcfg.get("preferred_model") or tdef["default_model"]
    avail = ollama.list_models() or []
    model = resolve_model_name(req_model, avail)
    if not model:
        for cand in _fallback_model_order(task_type) + avail:
            model = resolve_model_name(cand, avail)
            if model:
                break
    if not model:
        return "[delegate:error] No compatible local model found"

    # Cache check
    ckey = hashlib.md5(f"{task_type}|{model}|{enriched}|{task}".encode()).hexdigest()
    if ckey in _delegate_cache:
        cached_resp, _ = _delegate_cache[ckey]
        return finalize("c3_delegate", {"task_type": task_type, "cached": True},
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
        return finalize("c3_delegate", {"task_type": task_type, "model": model},
                        f"[delegate:timeout] No response from {model} after {_elapsed}s "
                        f"(limit {timeout_s}s)", "timeout")

    # Self-correction: retry with fallback model on low confidence
    conf = _estimate_confidence(task_type, resp, count_tokens(resp))
    if conf == "low" and dcfg.get("allow_model_fallback", True):
        tried = {model}
        for fallback_cand in _fallback_model_order(task_type) + avail:
            fallback = resolve_model_name(fallback_cand, avail)
            if not fallback or fallback in tried:
                continue
            tried.add(fallback)
            retry_resp = ollama.generate(
                prompt=tdef["prompt_template"].format(context=enriched, task=task),
                model=fallback, system=tdef["system"],
                temperature=tdef.get("temperature", 0.3),
                max_tokens=int(dcfg.get("max_tokens", 512) or 512),
            )
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
    return finalize("c3_delegate", {"task": task_type, "model": model, "elapsed": f"{_elapsed}s"},
                    resp, conf)
