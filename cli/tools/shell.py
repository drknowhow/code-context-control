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
unchanged (shell=True → /bin/sh). Git Bash does not bundle optional tools
such as jq; use Python's stdlib JSON support when portability matters.
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
    r"(?:^|&&|\|\||[;|])\s*git\s+"
    r"(commit|add|mv|rm|merge|rebase|cherry-pick|revert|reset|restore|checkout)\b",
    re.IGNORECASE,
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
_JQ_INVOCATION = re.compile(r"(?:^|&&|\|\||[;|(])\s*jq(?:\s|$)", re.IGNORECASE)


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


def _popen_kwargs(extra_env: dict | None = None) -> dict:
    # Force UTF-8 in child processes so Unicode output (→, box-drawing, emoji)
    # doesn't crash on Windows' legacy cp1252 console encoding. setdefault so an
    # intentional caller-set encoding still wins.
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        env.update(extra_env)
    kw: dict = {"stdin": subprocess.DEVNULL, "env": env}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    return kw


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        try:
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
            if killed.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def _run_sync(cmd: str, cwd: str, timeout: int,
              extra_env: dict | None = None) -> dict:
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
    shell_name = "git-bash" if bash else ("cmd" if sys.platform == "win32" else "sh")
    try:
        proc = subprocess.Popen(
            popen_target, shell=use_shell, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **_popen_kwargs(extra_env),
        )
    except (OSError, ValueError) as exc:
        return {
            "exit_code": 127 if isinstance(exc, FileNotFoundError) else 126,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.time() - start) * 1000),
            "timed_out": False,
            "shell": shell_name,
        }
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
        "shell": shell_name,
    }


def _parse_porcelain_entries(output: str) -> dict[str, str]:
    records = output.split("\0")
    entries: dict[str, str] = {}
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2]
        path = record[3:]
        if path:
            entries[path.replace("\\", "/")] = status
        if ("R" in status or "C" in status) and index < len(records):
            original = records[index]
            index += 1
            if original:
                entries[original.replace("\\", "/")] = f"{status}:source"
    return entries


def _capture_git_state(cwd: str) -> dict:
    status = _run_sync("git status --porcelain=v1 -z --untracked-files=all", cwd, timeout=5)
    if status["exit_code"] != 0:
        return {"is_repo": False, "head": "", "entries": {}, "files": set()}
    head = _run_sync("git rev-parse --verify HEAD", cwd, timeout=5)
    entries = _parse_porcelain_entries(status["stdout"])
    return {
        "is_repo": True,
        "head": head["stdout"].strip() if head["exit_code"] == 0 else "",
        "entries": entries,
        "files": set(entries),
    }


def _git_head_diff(before: dict, after: dict, cwd: str) -> set[str]:
    old_head = before.get("head", "")
    new_head = after.get("head", "")
    if old_head == new_head or not new_head:
        return set()
    if old_head:
        cmd = f"git diff --name-only -z {old_head} {new_head}"
    else:
        cmd = f"git diff-tree --root --no-commit-id --name-only -r -z {new_head}"
    diff = _run_sync(cmd, cwd, timeout=5)
    if diff["exit_code"] != 0:
        return set()
    return {path.replace("\\", "/") for path in diff["stdout"].split("\0") if path}


def _maybe_refresh_ledger(cmd: str, result: dict, svc, before: dict | None = None,
                          cwd: str = "") -> list[str]:
    """If git mutated state, capture affected files for the edit ledger."""
    if result["exit_code"] != 0 or not _GIT_MUTATING.search(cmd):
        return []
    if not getattr(svc, "edit_ledger", None):
        return []
    try:
        before = before or {
            "is_repo": False, "head": "", "entries": {}, "files": set()}
        git_cwd = cwd or svc.project_path
        after = _capture_git_state(git_cwd)
        if not before.get("is_repo") and not after.get("is_repo"):
            return []
        before_entries = before.get("entries")
        after_entries = after.get("entries")
        if before_entries is not None and after_entries is not None:
            changed = {
                path
                for path in set(before_entries) | set(after_entries)
                if before_entries.get(path) != after_entries.get(path)
            }
        else:
            changed = set(before.get("files") or ()) | set(after.get("files") or ())
        if before.get("is_repo") and after.get("is_repo"):
            changed.update(_git_head_diff(before, after, git_cwd))
        files = sorted(changed)[:20]
        for f in files:
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


def _dependency_hint(cmd: str, result: dict) -> str:
    if result.get("exit_code") != 127 or not _JQ_INVOCATION.search(cmd):
        return ""
    stderr = (result.get("stderr") or "").lower()
    if "jq" not in stderr and result.get("shell") != "git-bash":
        return ""
    return (
        "jq is not bundled with Git Bash. For portable JSON formatting use "
        "`python -m json.tool`; for field extraction use Python's `json` module, "
        "or install jq separately."
    )


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
                       log: bool, svc, finalize, env_creds: str = "",
                       enable_creds: bool = True) -> str:
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

    # ── credential injection (local runtime only) ─────────
    # `cmd` stays the RAW template form for every echo/log below; only
    # `exec_cmd` (never logged) carries decoded values. Cross-project proxies
    # (cli/tools/project.py) pass enable_creds=False so one project can never
    # siphon another's secrets through a proxied shell.
    from services import credential_store as _creds
    exec_cmd = cmd
    extra_env: dict[str, str] = {}
    cred_names: list[str] = []
    if enable_creds:
        try:
            exec_cmd, tmpl_used, tmpl_missing = _creds.expand_templates(
                cmd, svc.project_path)
            requested = [n.strip() for n in (env_creds or "").split(",") if n.strip()]
            auto = [n for n, e in _creds.list_entries(svc.project_path).items()
                    if e.get("inject") and n not in requested]
            values, missing = _creds.resolve(requested + auto, svc.project_path)
            # Explicitly requested / templated names must resolve; inject:true
            # entries that don't resolve are silently inert (see the
            # no-fall-through invariant in services/credential_store.py).
            hard_missing = sorted(set(tmpl_missing) | (set(missing) & set(requested)))
            if hard_missing:
                return (
                    f"[c3_shell:error] unknown credential(s): "
                    f"{', '.join(hard_missing)} — see c3_credentials(action='list')"
                )
            for cname, cval in values.items():
                entry = _creds.get_entry(cname, project_path=svc.project_path)
                extra_env[entry.get("env_var") or cname] = cval
            cred_names = sorted(set(tmpl_used) | set(values))
        except RuntimeError as exc:  # keyring/crypto unavailable
            return f"[c3_shell:error] credential store unavailable: {exc}"
        if exec_cmd != cmd and _BLOCKED.search(exec_cmd):
            return "[c3_shell:error] blocked pattern after credential expansion"

    ghost_root = Path(work_cwd)
    _ghosts_before = _list_root_files(ghost_root)
    git_before = (
        _capture_git_state(work_cwd)
        if log and _GIT_MUTATING.search(cmd)
        else None
    )

    result = await asyncio.to_thread(
        _run_sync, exec_cmd, work_cwd, timeout, extra_env or None)

    swept_ghosts = _sweep_new_ghost_files(ghost_root, _ghosts_before)

    # Scrub decoded values a child process may have echoed (env dumps, set,
    # crash output) BEFORE filtering/ledger/logging see the text.
    if enable_creds:
        result["stdout"] = _creds.redact_text(result["stdout"])
        result["stderr"] = _creds.redact_text(result["stderr"])
        if cred_names:
            _creds.touch_last_used(cred_names, svc.project_path)

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
        touched_files = _maybe_refresh_ledger(
            cmd, result, svc, before=git_before, cwd=work_cwd)
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
    dependency_hint = _dependency_hint(cmd, result)
    if dependency_hint:
        body += f"--- hint ---\n{dependency_hint}\n"
    if touched_files:
        body += f"--- ledger ---\nlogged {len(touched_files)} file(s)\n"
    if cred_names:
        body += f"--- creds ---\ninjected: {', '.join(cred_names)}\n"
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
