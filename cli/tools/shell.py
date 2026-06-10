"""c3_shell — structured shell execution with filter/log/timeout.

Wraps subprocess.Popen with the Windows-safe pattern from
services/edit_ledger.py::_git_combined (Popen + taskkill /F /T + stdin=DEVNULL).
Auto-filters long stdout via handle_filter, auto-logs git mutations
to the edit ledger, and accounts stdout tokens against session budget.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from cli.tools.filter import handle_filter
from core import count_tokens

# Commands that mutate repo state — trigger edit-ledger refresh after success.
_GIT_MUTATING = re.compile(
    r"^\s*git\s+(commit|add|mv|rm|merge|rebase|cherry-pick|revert|reset|restore|checkout)\b"
)
# Hard deny — the handful of genuinely catastrophic, irreversible commands.
# This is a BEST-EFFORT guard, NOT a sandbox: c3_shell runs arbitrary commands
# by design and a determined caller can trivially reword around these patterns.
# The escape hatch for an intentional dangerous command is native Bash.
# Covered: rm -rf of the filesystem root / a top-level system dir / $HOME / ~,
# the classic fork bomb, and Windows whole-drive-root wipes (del/rd/format C:\).
# A top-level system dir only matches when it is the *whole* target, so deleting
# a nested path like /home/me/project/build is intentionally NOT blocked.
_BLOCKED = re.compile(
    r"""
      (?<!git\ )\brm\b (?:\s+-\S+)* \s+        # rm + any flags, then a target:
      (?:
          /(?=\s|$|\*)                          #   filesystem root:  /   /*
        | ~(?=/|\s|$)                            #   home dir:         ~   ~/
        | \$HOME\b                               #   $HOME
        | /(?:etc|usr|bin|sbin|lib|lib64|var|boot|root|home|srv|sys|proc|dev|opt)
            (?=/?(?:\s|$|\*))                    #   a whole top-level system dir
      )
    | :\(\)\s*\{\s*:\s*\|\s*:\s*\}?              # fork bomb        :(){ :|: };:
    | \b(?:format|rd|rmdir|del)\b [^\n]*?        # windows whole-drive-root wipe
        \b[a-zA-Z]:\\?(?=\s|\*|$|["'])
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Soft warn — run but prepend a caveat to the response.
# `(?<!\w)` / `(?!\w)` anchor against word chars, so `--force` (which starts
# with a non-word `-`) still matches at word/space boundaries.
_SOFT_WARN = re.compile(
    r"(?<!\w)(rm\s+-rf|--force|--no-verify|reset\s+--hard|DROP\s+(TABLE|DATABASE)|TRUNCATE)(?!\w)",
    re.IGNORECASE,
)

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_FILTER_THRESHOLD_LINES = 30


def _popen_kwargs() -> dict:
    kw: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()


def _run_sync(cmd: str, cwd: str, timeout: int) -> dict:
    """Blocking subprocess run with hard kill on timeout. Returns structured dict."""
    start = time.time()
    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace",
        **_popen_kwargs(),
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except Exception:
            stdout, stderr = "", ""
        timed_out = True

    return {
        "exit_code": -1 if timed_out else (proc.returncode or 0),
        "stdout": stdout or "",
        "stderr": stderr or "",
        "duration_ms": round((time.time() - start) * 1000),
        "timed_out": timed_out,
    }


def _maybe_refresh_ledger(cmd: str, result: dict, svc) -> list[str]:
    """If git mutated state, capture affected files for the edit ledger."""
    if result["exit_code"] != 0 or not _GIT_MUTATING.match(cmd):
        return []
    if not getattr(svc, "edit_ledger", None):
        return []
    try:
        probe = _run_sync("git diff --name-only HEAD~1..HEAD", svc.project_path, timeout=5)
        files = [f.strip() for f in probe["stdout"].splitlines() if f.strip()]
        for f in files[:20]:
            try:
                svc.edit_ledger.log_edit(
                    file=f,
                    change_type="shell_git",
                    summary=f"via c3_shell: {cmd[:80]}",
                    include_git=True,
                )
            except Exception:
                pass
        return files
    except Exception:
        return []


async def handle_shell(cmd: str, cwd: str, timeout: int, filter_output: bool,
                       log: bool, svc, finalize) -> str:
    if not cmd or not cmd.strip():
        return "[c3_shell:error] empty command"
    if _BLOCKED.search(cmd):
        return (
            "[c3_shell:error] blocked pattern — use native Bash with explicit "
            "approval if this is truly intended"
        )

    timeout = max(1, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    work_cwd = cwd or svc.project_path
    work_cwd = str(Path(work_cwd).resolve())

    result = await asyncio.to_thread(_run_sync, cmd, work_cwd, timeout)

    raw_stdout = result["stdout"]
    filtered_note = ""
    if filter_output and raw_stdout.count("\n") > _FILTER_THRESHOLD_LINES:
        try:
            filtered = await asyncio.to_thread(
                handle_filter,
                "", raw_stdout, "", 50, "smart", True,
                svc, lambda *a, **kw: a[2],
            )
            result["stdout_raw_bytes"] = len(raw_stdout)
            result["stdout"] = filtered
            filtered_note = " [stdout filtered]"
        except Exception:
            pass

    touched_files: list[str] = []
    if log:
        touched_files = _maybe_refresh_ledger(cmd, result, svc)
        if getattr(svc, "activity_log", None):
            try:
                svc.activity_log.log("shell_exec", {
                    "cmd": cmd[:200],
                    "cwd": work_cwd,
                    "exit_code": result["exit_code"],
                    "duration_ms": result["duration_ms"],
                    "timed_out": result["timed_out"],
                    "touched_files": touched_files,
                })
            except Exception:
                pass

    warn = ""
    if _SOFT_WARN.search(cmd):
        warn = "[c3_shell:warn] destructive pattern detected — verify before re-running\n"

    if result["timed_out"]:
        status = "TIMEOUT"
    elif result["exit_code"] == 0:
        status = "OK"
    else:
        status = f"FAIL({result['exit_code']})"

    body = (
        f"{warn}"
        f"[c3_shell:{status}] {result['duration_ms']}ms{filtered_note}\n"
        f"$ {cmd}\n"
        f"--- stdout ---\n{result['stdout'].rstrip()}\n"
    )
    if result["stderr"].strip():
        body += f"--- stderr ---\n{result['stderr'].rstrip()}\n"
    if touched_files:
        body += f"--- ledger ---\nlogged {len(touched_files)} file(s)\n"

    summary = f"shell {status} in {result['duration_ms']}ms"
    resp_tokens = count_tokens(body) if body else 0
    return finalize(
        "c3_shell",
        {"cmd": cmd[:120], "cwd": work_cwd},
        body,
        summary,
        response_tokens=resp_tokens,
    )
