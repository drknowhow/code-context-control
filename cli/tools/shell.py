"""c3_shell — structured shell execution with filter/log/timeout.

Wraps subprocess.Popen with the Windows-safe pattern from
services/edit_ledger.py::_git_combined (Popen + taskkill /F /T + stdin=DEVNULL).
Auto-filters long stdout via handle_filter, auto-logs git mutations
to the edit ledger, and accounts stdout tokens against session budget.

Shell selection: on Windows, commands are run through Git Bash (bash.exe) when
it is available, so c3_shell speaks the same POSIX dialect as the native Bash
tool (forward-slash paths, single quotes, `$VAR`, ls/grep/cat, heredocs). This
avoids the cmd.exe/POSIX mismatch that forced callers to fall back to native
Bash for bash-flavored commands. Set C3_SHELL_BASH=0 to force cmd.exe, or point
C3_SHELL_BASH at a specific bash.exe to override discovery. POSIX platforms are
unchanged (shell=True → /bin/sh).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
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


# Cache for discovered Git Bash path: [] = uncomputed, [None]/[path] = computed.
_bash_cache: list = []


def _discover_git_bash() -> str | None:
    """Locate a Git-for-Windows bash.exe, never WSL/System32 bash.

    WSL's bash runs in a Linux subsystem with /mnt/c paths, which would break
    the Windows `cwd` semantics every caller relies on — so it is rejected.
    """
    candidates: list[str] = []
    for base_env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(base_env)
        if base:
            candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
            candidates.append(os.path.join(base, "Git", "usr", "bin", "bash.exe"))
    candidates.append(r"C:\Program Files\Git\bin\bash.exe")
    candidates.append(r"C:\Program Files\Git\usr\bin\bash.exe")
    for path in candidates:
        if os.path.isfile(path):
            return path
    # Last resort: PATH lookup, but reject WSL/Store bash (System32/WindowsApps).
    found = shutil.which("bash")
    if found:
        low = found.lower()
        if "system32" not in low and "windowsapps" not in low:
            return found
    return None


def _select_bash() -> str | None:
    """Return the bash.exe to run commands through on Windows, else None.

    None means "use the platform default shell" (cmd.exe on Windows via
    shell=True, /bin/sh on POSIX). Honors the C3_SHELL_BASH override:
    '0'/'cmd' forces the platform default; an existing file path forces that
    bash. Discovery is cached after the first call.
    """
    if sys.platform != "win32":
        return None
    override = os.environ.get("C3_SHELL_BASH")
    if override is not None:
        if override.strip().lower() in ("0", "", "cmd", "false", "off"):
            return None
        if os.path.isfile(override):
            return override
        # Unrecognized override → fall through to discovery.
    if not _bash_cache:
        _bash_cache.append(_discover_git_bash())
    return _bash_cache[0]


def _popen_kwargs() -> dict:
    # Force UTF-8 in child processes so Unicode output (→, box-drawing, emoji)
    # doesn't crash on Windows' legacy cp1252 console encoding. setdefault so an
    # intentional caller-set encoding still wins.
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kw: dict = {"stdin": subprocess.DEVNULL, "env": env}
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
    bash = _select_bash()
    if bash:
        # POSIX dialect via Git Bash — matches the native Bash tool.
        popen_target: object = [bash, "-c", cmd]
        use_shell = False
    else:
        # Platform default: cmd.exe on Windows, /bin/sh on POSIX.
        popen_target = cmd
        use_shell = True
    proc = subprocess.Popen(
        popen_target, shell=use_shell, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
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


# git diagnostics whose output the caller almost always needs verbatim — never
# auto-filter these, even past the line threshold.
_GIT_DIAGNOSTIC = re.compile(
    r"^\s*git\s+(status|diff|log|show|branch|stash\s+list)\b", re.IGNORECASE
)


def _list_root_files(root: Path) -> set[str]:
    try:
        return {e.name for e in root.iterdir() if e.is_file()}
    except OSError:
        return set()


def _sweep_new_ghost_files(root: Path, before: set[str]) -> list[str]:
    """Delete 0-byte 'ghost' files (shell-redirect / metacharacter artifacts —
    e.g. a `>Lnnn` marker or `2>$null` leaking a filename) that appeared in
    *root* during this command. Only files absent from *before* are removed, so
    pre-existing files are never touched. Detection is reused from
    hook_ghost_files so the rules live in one place; this makes c3_shell
    self-clean regardless of whether the external PostToolUse ghost hook is
    wired for this tool."""
    try:
        from cli.hook_ghost_files import scan_ghost_files
    except Exception:
        return []
    swept: list[str] = []
    for g in scan_ghost_files(root):
        name = g.get("name", "")
        if not name or name in before:
            continue
        try:
            Path(g["path"]).unlink()
            swept.append(name)
        except OSError:
            pass
    return swept


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

    ghost_root = Path(work_cwd)
    _ghosts_before = _list_root_files(ghost_root)

    result = await asyncio.to_thread(_run_sync, cmd, work_cwd, timeout)

    swept_ghosts = _sweep_new_ghost_files(ghost_root, _ghosts_before)

    raw_stdout = result["stdout"]
    filtered_note = ""
    if (filter_output and raw_stdout.count("\n") > _FILTER_THRESHOLD_LINES
            and not _GIT_DIAGNOSTIC.search(cmd)):
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
    if swept_ghosts:
        body += (
            f"--- ghost-sweep ---\nremoved {len(swept_ghosts)} stray 0-byte "
            f"file(s): {', '.join(swept_ghosts)}\n"
        )

    summary = f"shell {status} in {result['duration_ms']}ms"
    resp_tokens = count_tokens(body) if body else 0
    return finalize(
        "c3_shell",
        {"cmd": cmd[:120], "cwd": work_cwd},
        body,
        summary,
        response_tokens=resp_tokens,
    )
