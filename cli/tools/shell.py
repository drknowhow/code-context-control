"""c3_shell — structured shell execution with filter/log/timeout.

Wraps subprocess.Popen with the Windows-safe pattern from
services/edit_ledger.py::_git_combined (Popen + taskkill /F /T + stdin=DEVNULL).
Budgets and shapes the response (cli/tools/shell_render.py + shell_parsers.py:
normalised streams, runner-aware priority regions, omission only over budget),
auto-logs git mutations to the edit ledger, and accounts stdout tokens against
session budget.

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

from cli._shell_writes import shell_write_targets
from cli.tools import _grants
from cli.tools._helpers import finalize_with_tokens
from cli.tools.shell_parsers import (
    SUMMARY_MIN_LINES,
    detect_runner,
    normalize_stream,
    priority_regions,
    structured_tail,
)
from cli.tools.shell_render import (
    allocate,
    cmd_display,
    effective_budget,
    grep_pattern,
    human_bytes,
    shape_stream,
    split_lines,
    split_preview,
)
from core import count_tokens
from services import access_guard
from services.shell_output import (
    TEXT_MAX_BYTES,
    OutputAccessError,
    ShellCapture,
    ShellOutputStore,
)

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

# The transport's own ceiling, and the reason _MAX_TIMEOUT alone is a lie.
#
# An MCP client kills a tool call at MCP_TOOL_TIMEOUT (Claude Code ships
# 120_000 ms and the value is set per-environment) — a limit this process does
# not choose and cannot raise. Until this existed, `c3_shell(timeout=600)` was
# accepted, clamped to 600, and then killed by the client at 120s with the
# subprocess still running: the caller saw the call "moved to background" and
# then fail, with no line anywhere naming the real limit or attributing the
# kill. The subprocess was NOT reaped by us, because our own deadline never
# arrived.
#
# The cost was not hypothetical. Two Higgsfield image generations (2-4 min
# each) were requested at timeout=420, killed at 120s, and the remote jobs
# completed and were BILLED while the local process died before downloading —
# one image landed, one was paid for and stranded.
#
# So: discover the client's ceiling, run just inside it, and SAY SO. Running
# inside it means OUR deadline fires first, which turns a phantom
# client-side kill into an ordinary `[c3_shell:TIMEOUT]` with stdout, a
# duration, and a process tree we actually killed.
_TRANSPORT_MARGIN_S = 5


def _transport_ceiling_s() -> int | None:
    """Seconds this call may run before the MCP client kills it, if knowable.

    None when the variable is unset or unparseable — then nothing is capped
    and behaviour is exactly what it was, because a guess here would cap
    legitimate work on a client with no such limit.
    """
    raw = (os.environ.get("MCP_TOOL_TIMEOUT") or "").strip()
    if not raw:
        return None
    try:
        ceiling = int(float(raw)) // 1000
    except (TypeError, ValueError):
        return None
    return ceiling if ceiling > _TRANSPORT_MARGIN_S else None
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


def _default_redact(text: str) -> str:
    """Scrub vault values from a piece of output; never raises."""
    try:
        from services import credential_store as _creds
        return _creds.redact_text(text)
    except Exception:
        return text


def _run_sync(cmd: str, cwd: str, timeout: int,
              extra_env: dict | None = None, *, redact=_default_redact,
              spool_dir=None) -> dict:
    """Blocking subprocess run with hard kill on timeout. Returns structured dict.

    Since 2.112.0 the child's streams are not buffered in memory: a
    ShellCapture pumps them to spool files with bounded head/tail previews
    (services/shell_output.py). ``stdout``/``stderr`` in the result carry the
    whole text when the stream is at most TEXT_MAX_BYTES and None otherwise;
    ``capture`` carries the stats and previews either way. Callers that
    patch this function with a plain dict (tests) still work: no ``capture``
    means "small output, text present".
    """
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
    if spool_dir is None:
        spool_dir = ShellOutputStore().spool_dir()
    capture = ShellCapture(proc, spool_dir, redact=redact, kill_tree=_kill_tree)
    timed_out = capture.wait(timeout)

    def _text(stream: str):
        if getattr(capture.stats, stream).bytes > TEXT_MAX_BYTES:
            return None
        try:
            return capture.text(stream)
        except Exception:
            return None

    return {
        "exit_code": -1 if timed_out else (proc.returncode or 0),
        "stdout": _text("stdout"),
        "stderr": _text("stderr"),
        "duration_ms": capture.duration_ms or round((time.time() - start) * 1000),
        "timed_out": timed_out,
        "shell": shell_name,
        "capture": capture,
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


# ── Command classification (telemetry only) ─────────────────────────────────
#
# A coarse label per call so .c3/tool_telemetry.jsonl can say which KIND of
# command carries the tokens and the wall time. Measured 2026-09-04 over 7,278
# shell_exec events: file-read/search 48%, python 10%, git 10%, tests 5%,
# ops/device 4%, build/lint 4%. Best-effort: the first word after any leading
# `cd … &&` / `VAR=… ;` prefix, plus a few substring tells. Never used to
# change behaviour.

_CLASS_PREFIX = re.compile(
    r"""^(?:\s*(?:cd\s+(?:"[^"]*"|'[^']*'|\S+)\s*(?:&&|;)\s*|\w+=(?:"[^"]*"|'[^']*'|\S*)\s*;?\s*))*""")
_CLASS_TESTS = re.compile(
    r"\b(?:pytest|python\s+-m\s+(?:pytest|unittest)|npm\s+test|vitest|jest|cargo\s+test|go\s+test)\b")
_CLASS_TABLE = {
    "file-read": {"cat", "head", "tail", "sed", "grep", "rg", "find", "ls", "wc",
                  "awk", "tree", "type", "less", "more", "stat", "file", "du"},
    "git": {"git"},
    "python": {"python", "python3", "py", "pythonw", "pip", "uv"},
    "build": {"npm", "npx", "node", "pnpm", "yarn", "tsc", "eslint", "vite",
              "gradlew", "cargo", "go", "make", "ruff", "mypy", "pyright",
              "dotnet", "mvn", "gradle"},
    "ops": {"curl", "wget", "adb", "powershell", "pwsh", "schtasks", "tasklist",
            "taskkill", "netstat", "ps", "kill", "sleep", "timeout", "ssh",
            "scp", "docker", "systemctl", "sc"},
    "echo": {"echo", "printf", "test", "[", "true", "false"},
    "gh": {"gh"},
    "c3": {"c3", "c3-mcp"},
}


def _cmd_class(cmd: str) -> str:
    """Coarse command class for telemetry (see table above)."""
    try:
        if _CLASS_TESTS.search(cmd):
            return "tests"
        rest = _CLASS_PREFIX.sub("", cmd.strip())
        head = rest.split(None, 1)[0] if rest.split() else ""
        head = head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if head.endswith(".exe"):
            head = head[:-4]
        if head.startswith("python"):
            head = "python"
        for label, names in _CLASS_TABLE.items():
            if head in names:
                return label
        return "other"
    except Exception:
        return "other"


def _longest_line(*texts: str) -> int:
    longest = 0
    for text in texts:
        if not text:
            continue
        for line in text.split("\n"):
            if len(line) > longest:
                longest = len(line)
    return longest


def _stream_facts(result: dict, stream: str) -> dict:
    """Size, line count, longest line and available text/previews of one stream."""
    text = result.get(stream)
    capture = result.get("capture")
    if capture is not None:
        st = getattr(capture.stats, stream)
        return {"text": text, "head": st.head, "tail": st.tail, "bytes": st.bytes,
                "lines": st.lines, "longest": st.longest_line}
    text = text or ""
    return {"text": text, "head": "", "tail": "", "bytes": len(text.encode("utf-8", errors="replace")),
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "longest": _longest_line(text)}


_ZERO_NORM = {"ansi_stripped": 0, "cr_collapsed": 0, "dup_collapsed": 0}


def _normalised(facts: dict, *, collapse: bool, fuzzy: bool) -> dict:
    """One stream after ``normalize_stream``: the text (or previews), its
    logical lines in the index space ``shape_stream`` uses, the raw line
    numbers of those lines when folding shifted them, and the counters."""
    info = dict(_ZERO_NORM)
    if facts["text"] is not None:
        text, ni = normalize_stream(facts["text"], collapse=collapse, fuzzy_dups=fuzzy)
        for k in _ZERO_NORM:
            info[k] = ni[k]
        return {"text": text, "head": "", "tail": "", "lines": split_lines(text),
                "bytes": len(text.encode("utf-8", errors="replace")),
                "total_lines": facts["lines"], "numbers": ni["line_numbers"],
                "tail_numbers": None, "info": info}
    head, hi = normalize_stream(facts["head"], collapse=collapse, fuzzy_dups=fuzzy)
    tail, ti = normalize_stream(facts["tail"], collapse=collapse, fuzzy_dups=fuzzy)
    for k in _ZERO_NORM:
        info[k] = hi[k] + ti[k]
    h_lines, t_lines, _torn = split_preview(head, tail)
    return {"text": None, "head": head, "tail": tail, "lines": h_lines + t_lines,
            "bytes": facts["bytes"], "total_lines": facts["lines"],
            "numbers": hi["line_numbers"], "tail_numbers": ti["line_numbers"], "info": info}


def _region_line_count(regions) -> int:
    seen: set[int] = set()
    for a, b, _why in regions:
        seen.update(range(a, b + 1))
    return len(seen)


def render_shell_response(cmd: str, result: dict, svc, *,
                          filter_output: bool = True, warn: str = "",
                          capped_note: str = "", touched_files=(),
                          cred_names=(), swept_ghosts=(),
                          max_bytes: int | None = None) -> tuple[str, dict]:
    """Turn a finished command into the body the agent reads, plus its stats.

    Pure with respect to the subprocess: ``result`` is the dict _run_sync
    returns (exit_code, stdout, stderr, duration_ms, timed_out, shell, and
    since 2.112.0 an optional ``capture`` with stats and head/tail previews
    for a stream too large to hold), so the shell-eval harness
    (services/bench/shell_eval.py) can feed captured or synthetic streams
    through exactly the code path a live call uses.

    Budget (2.112.0, cli/tools/shell_render.py): the rendered body never
    exceeds ``effective_budget()`` bytes — 18 KiB by default, 22 KiB ceiling,
    ``max_bytes`` and ``hybrid.shell_budget_bytes`` may only lower it,
    ``filter_output=False`` never lifts it.

    Shaping (2.113.0, S2, cli/tools/shell_parsers.py) — the Cod rule: strip
    ANSI and collapse ``\\r`` rewrites always, otherwise preserve complete
    under-budget output, run the parsers always but omit only over budget:

    1. Both streams are normalised: ``\\r\\n`` → ``\\n``, ANSI escape and
       control sequences stripped (not a loss), each line's ``\\r``
       progress rewrites collapsed to the final state, runs of three or
       more identical consecutive lines folded to the first plus
       `` [x N]``. The last two are a loss: the header says
       ``[collapsed …]``, ``stats["filtered"]`` is set and the raw streams
       are spilled. ``filter_output=False`` skips the two collapses (ANSI
       is always stripped) and, as before, never lifts the byte cap.
    2. The runner is detected (pytest, unittest, cargo/rustc, tsc, jest,
       vitest) and its priority regions computed on every call; a stream
       that fits its allocation passes through whole regardless. A stream
       that does not is re-normalised with the duplicate test widened to
       lines differing only in digits / hex / timestamps, then shaped:
       priority regions first (each announced by ``[La-b: why]`` when not
       contiguous), head/tail with the rest, one omission note naming the
       output id. Unrecognised output uses generic error anchors, capped at
       60% of the allocation so head and tail always keep something.
    3. For a recognised runner with more than SUMMARY_MIN_LINES lines a
       ``--- summary ---`` section (totals + one line per failing test,
       cap 20) is appended and counted inside the budget.

    The legacy ``>30 newlines → handle_filter`` path is gone from this
    function (``c3_filter`` the tool is untouched). Whenever anything was
    dropped — collapsed, clipped or windowed — ``stats["needs_spill"]``
    tells handle_shell to keep the raw streams.

    ``stats`` is what telemetry records about the call (see
    SessionManager.record_tool_tokens ``detail``): stdout_bytes and
    stderr_bytes are measured on the RAW streams; longest_line is the tell
    for the single-line monsters (minified bundles, JSONL) that a
    newline-count trigger never sees; filtered (a lossy collapse happened),
    spilled, output_id, runner, ansi_stripped, cr_collapsed, dup_collapsed
    and priority_lines (lines the parsers marked, both streams) describe
    what this renderer did.
    """
    out = _stream_facts(result, "stdout")
    err = _stream_facts(result, "stderr")
    capture = result.get("capture")
    stats: dict = {
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out")),
        "stdout_bytes": out["bytes"],
        "stderr_bytes": err["bytes"],
        "longest_line": max(out["longest"], err["longest"]),
        "filtered": False,
        "spilled": False,
        "output_id": None,
        "cmd_class": _cmd_class(cmd),
        "needs_spill": False,
        "runner": None,
        "ansi_stripped": 0,
        "cr_collapsed": 0,
        "dup_collapsed": 0,
        "priority_lines": 0,
    }

    config_default = None
    try:
        config_default = (getattr(svc, "hybrid_config", None) or {}).get("shell_budget_bytes")
    except Exception:
        config_default = None
    budget = effective_budget(max_bytes, config_default=config_default)
    stats["budget_bytes"] = budget
    output_id = getattr(capture, "output_id", None) if capture is not None else None
    focus = grep_pattern(cmd)
    collapse = bool(filter_output)

    # 1. Normalise. A stream with previews only (over TEXT_MAX_BYTES) is over
    #    budget by definition, so it gets the widened duplicate test at once.
    norm_out = _normalised(out, collapse=collapse, fuzzy=out["text"] is None)
    norm_err = _normalised(err, collapse=collapse, fuzzy=err["text"] is None)

    # 2. Runner, summary, allocation. The summary is measured first so the
    #    streams are allocated what is left of the budget.
    runner = detect_runner(cmd, norm_out["text"] or norm_out["head"] + norm_out["tail"],
                           norm_err["text"] or norm_err["head"] + norm_err["tail"])
    stats["runner"] = runner
    summary = ""
    if runner and (out["lines"] + err["lines"]) > SUMMARY_MIN_LINES:
        summary = structured_tail(runner, norm_out["lines"] + norm_err["lines"], result.get("exit_code"))
    summary_bytes = len(summary.encode("utf-8", errors="replace"))
    out_alloc, err_alloc = allocate(budget - summary_bytes, norm_out["bytes"], norm_err["bytes"])

    # 3. A stream that does not fit is re-normalised with fuzzy duplicates,
    #    then shaped around its priority regions.
    if collapse and norm_out["text"] is not None and norm_out["bytes"] > out_alloc:
        norm_out = _normalised(out, collapse=True, fuzzy=True)
    if collapse and norm_err["text"] is not None and norm_err["bytes"] > err_alloc:
        norm_err = _normalised(err, collapse=True, fuzzy=True)
    for key in ("ansi_stripped", "cr_collapsed", "dup_collapsed"):
        stats[key] = norm_out["info"][key] + norm_err["info"][key]
    stats["filtered"] = bool(stats["cr_collapsed"] or stats["dup_collapsed"])

    prio_out = priority_regions(runner, norm_out["lines"]) if norm_out["lines"] else []
    prio_err = priority_regions(runner, norm_err["lines"]) if norm_err["lines"] else []
    stats["priority_lines"] = _region_line_count(prio_out) + _region_line_count(prio_err)
    share = 1.0 if runner else 0.6

    shaped_out, info_out = shape_stream(
        full_text=norm_out["text"], head=norm_out["head"], tail=norm_out["tail"],
        total_bytes=norm_out["bytes"], total_lines=norm_out["total_lines"], alloc=out_alloc,
        output_id=output_id, focus=focus, priority=prio_out, priority_share=share,
        line_numbers=norm_out["numbers"], tail_line_numbers=norm_out["tail_numbers"])
    if err["bytes"] > 0 or (err["text"] or "").strip():
        shaped_err, info_err = shape_stream(
            full_text=norm_err["text"], head=norm_err["head"], tail=norm_err["tail"],
            total_bytes=norm_err["bytes"], total_lines=norm_err["total_lines"], alloc=err_alloc,
            output_id=output_id, focus=focus, priority=prio_err, priority_share=share,
            line_numbers=norm_err["numbers"], tail_line_numbers=norm_err["tail_numbers"])
    else:
        shaped_err, info_err = "", {"cut": False, "omitted_lines": 0, "omitted_bytes": 0,
                                    "clipped_lines": 0, "rendered_bytes": 0, "priority_kept": 0}

    cut = bool(info_out["cut"] or info_err["cut"])
    stats["needs_spill"] = bool(stats["filtered"] or cut)
    stats["spilled"] = bool(stats["needs_spill"] and output_id)
    stats["output_id"] = output_id if stats["spilled"] else None
    stats["omitted_lines"] = info_out["omitted_lines"] + info_err["omitted_lines"]
    stats["clipped_lines"] = info_out["clipped_lines"] + info_err["clipped_lines"]

    if result.get("timed_out"):
        status = "TIMEOUT"
    elif result.get("exit_code") == 0:
        status = "OK"
    else:
        status = f"FAIL({result.get('exit_code')})"

    collapsed_note = ""
    if stats["filtered"]:
        parts = []
        if stats["cr_collapsed"]:
            parts.append(f"{stats['cr_collapsed']} cr rewrites")
        if stats["dup_collapsed"]:
            parts.append(f"{stats['dup_collapsed']} dup lines")
        collapsed_note = f" [collapsed {', '.join(parts)}]"
    size_note = ""
    if cut or stats["spilled"]:
        size_note = (f" (stdout {human_bytes(out['bytes'])}/{out['lines']} lines,"
                     f" stderr {human_bytes(err['bytes'])}/{err['lines']} lines,")
        size_note += f" output_id={output_id})" if stats["spilled"] else " not spilled)"

    body = (
        f"{warn}{capped_note}"
        f"[c3_shell:{status}] {result.get('duration_ms', 0)}ms{collapsed_note}{size_note}\n"
        f"$ {cmd_display(cmd)}\n"
        f"--- stdout ---\n{shaped_out.rstrip()}\n"
    )
    if shaped_err.strip():
        body += f"--- stderr ---\n{shaped_err.rstrip()}\n"
    if summary:
        body += summary
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

    stats["response_bytes"] = len(body.encode("utf-8", errors="replace"))
    stats["response_tokens"] = count_tokens(body) if body else 0
    return body, stats


# ── Access Guard advisory scanner (T2c) ────────────────────────────────────
# ADVISORY ONLY — this is best-effort, NEVER enforcement. A shell command can
# reach paths through subshells, variables, globs, quoting, and indirection
# that no static token scan can see. A hit here is meaningful (the command is
# refused); a clean scan proves nothing and must never be described as
# enforcement. The enforced surfaces are the MCP read/edit tools and the cwd
# check in handle_shell.
_TOKEN_SEPARATORS = re.compile(r"[\s;&|<>()`]+")
_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")   # /c/foo → c:/foo (any drive)
_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_DRIVE_PREFIX = re.compile(r"^[a-zA-Z]:[/\\]")
_MAX_SCAN_TOKENS = 64


def _scan_candidates(command: str, work_cwd: str) -> list[str]:
    """Path-shaped tokens from a command string (best-effort tokenizer).

    Advisory heuristics: strips quotes, splits `--flag=path` / `VAR=path`,
    translates MSYS drive spellings, skips flags/URLs/`host:port`-shaped
    tokens, and keeps tokens that look like paths (separator, extension, or
    an existing entry under the effective cwd). Quoted paths containing
    spaces are split and may be missed — documented best-effort behavior.
    """
    out: list[str] = []
    for chunk in _TOKEN_SEPARATORS.split(command):
        for raw in chunk.split():
            tok = raw.strip("'\"")
            todo = [tok]
            if "=" in tok:                          # --flag=path / VAR=path
                todo.append(tok.split("=", 1)[1].strip("'\""))
            for t in todo:
                if not t or t.startswith("-"):
                    continue
                m = _MSYS_DRIVE.match(t)
                if m:                               # MSYS spelling → drive path
                    t = m.group(1) + ":/" + t[len(m.group(1)) + 2:]
                if _URL_SCHEME.match(t):
                    continue                        # URLs are not local paths
                if ":" in t and not _DRIVE_PREFIX.match(t):
                    continue                        # host:port, {{cred:N}}, ADS…
                pathish = "/" in t or "\\" in t or "." in t.strip(".")
                if not pathish:
                    try:
                        pathish = os.path.exists(os.path.join(work_cwd, t))
                    except (OSError, ValueError):
                        pathish = False
                if not pathish:
                    continue
                out.append(t)
                if len(out) >= _MAX_SCAN_TOKENS:
                    return out
    return out


def _advisory_guard_scan(command: str, work_cwd: str, project_path: str):
    """(denial, token) when a path-shaped token hits a deny rule, else (None, '').

    ADVISORY: only deny-kind rule hits (and the corrupt-config deny-all)
    refuse; canonicalization artifacts on tokens that may not be paths at
    all (UNC-ish, 8.3, unresolvable) are skipped so non-path tokens cannot
    veto commands. Evaluator errors fail closed (synthetic denial), matching
    the hook layer's posture.
    """
    for tok in _scan_candidates(command, work_cwd):
        target = tok if os.path.isabs(tok) else os.path.join(work_cwd, tok)
        try:
            denial = access_guard.check(target, "read", project_path)
        except Exception as exc:  # fail closed — mirror hook _FAIL_CLOSED
            return access_guard.Denial(
                "<evaluator-error>", "deny", "builtin",
                f"evaluator error: {type(exc).__name__}"), tok
        if denial is None or denial.kind != "deny":
            continue
        if denial.rule.startswith("<") and denial.rule != "<corrupt-config>":
            continue  # canonicalization artifact on a maybe-not-a-path token
        return denial, tok
    return None, ""


def _write_scan(cmd: str, exec_cmd: str, work_cwd: str, svc) -> tuple:
    """WRITE-class Access Guard verdict for the files the command writes.

    ``(refusal, granted_lines)`` — a non-empty refusal means do not run.

    The read scan above asks "read" of every token, and a confirm hold, a
    read_only rule and the builtin write-denies are all write-class — so a
    heredoc into CLAUDE.md, ``sed -i`` on .mcp.json or ``cp`` over a hook
    body ran with no hold, the one route the doc could only ask the agent
    not to take (v2.102.0). Targets come from the extractor the edit ledger
    already trusts after the fact (cli/_shell_writes); same best-effort
    caveat as the read scan, and the same skip of synthetic spelling denials
    (a token that is not a path must not veto the command).

    Two passes on purpose: every target is judged first, and grants are
    consumed only when EVERY held target has one — so a refusal never spends
    a grant on a command that does not run. Grant identity is
    ``tool="c3_shell", op="write", path=<target>``: bound to the file, not
    the command text (the shell_warn layer's stated limitation, shared).
    """
    project = str(svc.project_path)
    held: list = []
    seen: set = set()
    for text in ([cmd] if exec_cmd == cmd else [cmd, exec_cmd]):
        for target in shell_write_targets(text, work_cwd):
            key = os.path.normcase(os.path.normpath(target))
            if key in seen:
                continue
            seen.add(key)
            try:
                denial = access_guard.check(target, "write", project)
            except Exception as exc:  # fail closed — mirror the read scan
                denial = access_guard.Denial(
                    "<evaluator-error>", "deny", "builtin",
                    f"evaluator error: {type(exc).__name__}")
            if denial is None:
                continue
            if denial.rule.startswith("<") and denial.rule != "<corrupt-config>":
                continue
            held.append((target, denial))
    if not held:
        return "", []
    for target, denial in held:
        if _grants.allow(svc, denial, tool="c3_shell", op="write",
                         path=target, peek=True):
            continue
        rid, note = _grants.confirm_request(svc, denial, tool="c3_shell",
                                            op="write", path=target)
        _log_access_denied(svc, work_cwd, denial, "write_scan")
        return (access_guard.refusal(denial, target, "write", request_id=rid,
                                     request_note=note)
                + "\n[c3_shell:note] shell write scanning is best-effort "
                "(advisory) — a held or denied write target refuses the whole "
                "command, but a clean scan is not enforcement."), []
    lines = []
    for target, denial in held:
        line = _grants.allow(svc, denial, tool="c3_shell", op="write",
                             path=target)
        if not line:  # spent between peek and use by a concurrent call
            return (f"[c3-override:spent] the grant covering {target} was "
                    "used up before this command ran. The rule stands — ask "
                    "again with c3_override if it is still needed."), []
        lines.append(line)
    return "", lines


def _log_access_denied(svc, work_cwd: str, denial, surface: str) -> None:
    """Record a guard refusal — with the effective cwd — in the activity log."""
    if not getattr(svc, "activity_log", None):
        return
    try:
        svc.activity_log.log("access_denied", {
            "tool": "c3_shell", "surface": surface, "cwd": work_cwd,
            "rule": denial.rule, "scope": denial.scope,
        })
    except Exception:
        pass


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


def _shell_warn_grant(svc, work_cwd: str) -> str | None:
    """The `[c3-override:granted]` line when a live shell_warn grant covers
    this cwd, or None to keep the ordinary caveat (docs/confirm-guard.md §7).

    Lazy import, fail-closed to warn-on: a broken grant store keeps the
    caveat, never removes it. Consumes a use — called only when the warn
    would actually print.
    """
    try:
        from cli.tools import _grants  # noqa: PLC0415 — lazy
        from services import override_grants as og  # noqa: PLC0415 — lazy
        return og.gate_shell(svc.project_path, path=work_cwd,
                             session_id=_grants.session_id(svc))
    except Exception:
        return None


_OUTPUT_ACTIONS = ("read", "search", "tail", "delete")


def _handle_output(output_id: str, action: str, pattern: str, lines, stream: str,
                   max_bytes, svc, finalize) -> str:
    """Page a spilled output back by id (2.112.0).

    The id is resolved for THIS project and THIS host session under the
    CURRENT Access Guard rules — services/shell_output.py documents why a
    spill from a more privileged call must not be readable by a later, less
    privileged one. Every read is re-budgeted like a live response.
    """
    action = (action or "read").strip().lower()
    if action not in _OUTPUT_ACTIONS:
        return (f"[c3_shell:error] output_action must be one of {', '.join(_OUTPUT_ACTIONS)}; "
                f"got {action!r}")
    stream = (stream or "stdout").strip().lower()
    if stream not in ("stdout", "stderr"):
        return "[c3_shell:error] stream must be 'stdout' or 'stderr'"
    project_path = str(svc.project_path)

    def guard_check(path: str):
        try:
            return access_guard.check(path, "read", project_path)
        except Exception as exc:  # evaluator error fails closed
            return access_guard.Denial("<evaluator-error>", "deny", "builtin",
                                       f"evaluator error: {type(exc).__name__}")

    store = ShellOutputStore()
    try:
        meta = store.resolve(output_id, project_path=project_path,
                          session_id=_grants.session_id(svc), guard_check=guard_check)
    except OutputAccessError as exc:
        return f"[c3_shell:error] {exc}"
    config_default = None
    try:
        config_default = (getattr(svc, "hybrid_config", None) or {}).get("shell_budget_bytes")
    except Exception:
        pass
    budget = effective_budget(max_bytes, config_default=config_default) - 512
    facts = getattr(meta, stream) or {}
    header = (f"[c3_shell:output] {meta.id} {action} {stream} · $ {meta.cmd_display} · "
              f"exit {meta.exit_code}{' (timed out)' if meta.timed_out else ''} · "
              f"{human_bytes(facts.get('bytes', 0))}/{facts.get('lines', 0)} lines · "
              f"captured {meta.created_at[:19]}\n")
    try:
        if action == "delete":
            store.delete(meta)
            body = header + "deleted\n"
        elif action == "search":
            if not pattern:
                return "[c3_shell:error] output_action='search' needs a pattern"
            body = header + store.search(meta, pattern, stream, max_bytes=budget)
        elif action == "tail":
            n = 50
            if isinstance(lines, int) and lines > 0:
                n = lines
            body = header + store.tail(meta, stream, lines=n, max_bytes=budget)
        else:
            window = None
            if isinstance(lines, (list, tuple)) and len(lines) == 2:
                window = (int(lines[0]), int(lines[1]))
            elif isinstance(lines, int) and lines > 0:
                window = (lines, lines)
            elif isinstance(lines, str) and lines.strip():
                parts = [p for p in re.split(r"[,\-:\s]+", lines.strip()) if p]
                if len(parts) == 1:
                    window = (int(parts[0]), int(parts[0]))
                elif len(parts) >= 2:
                    window = (int(parts[0]), int(parts[1]))
            body = header + store.read(meta, stream, lines=window, max_bytes=budget)
    except (ValueError, re.error) as exc:
        return f"[c3_shell:error] {exc}"
    if not body.endswith("\n"):
        body += "\n"
    return finalize_with_tokens(
        finalize, svc, "c3_shell",
        {"output_id": output_id, "output_action": action},
        body, f"shell output {action} {output_id}",
        detail={"cmd_class": "output", "output_id": output_id, "output_action": action,
                "response_bytes": len(body.encode("utf-8", errors="replace")),
                "response_tokens": count_tokens(body)},
        response_tokens=count_tokens(body),
    )


async def handle_shell(cmd: str, cwd: str, timeout: int, filter_output: bool,
                       log: bool, svc, finalize, env_creds: str = "",
                       enable_creds: bool = True, *, output_id: str = "",
                       output_action: str = "", pattern: str = "",
                       lines=None, stream: str = "stdout",
                       max_bytes: int | None = None) -> str:
    if output_id:
        return await asyncio.to_thread(
            _handle_output, output_id, output_action, pattern, lines, stream,
            max_bytes, svc, finalize)
    if not cmd or not cmd.strip():
        return "[c3_shell:error] empty command"
    # _BLOCKED never consults grants — no approval flow reaches the
    # catastrophic tier, by spec (docs/confirm-guard.md §7).
    if _BLOCKED.search(cmd):
        return (
            "[c3_shell:error] blocked pattern — use native Bash with explicit "
            "approval if this is truly intended"
        )

    timeout = max(1, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    # Cap to what the transport will actually allow, and remember that we did
    # so — the caller asked for a number, and a number quietly not honoured is
    # the whole defect (see _transport_ceiling_s).
    capped_from = 0
    _ceiling = _transport_ceiling_s()
    if _ceiling is not None and timeout > _ceiling - _TRANSPORT_MARGIN_S:
        capped_from, timeout = timeout, _ceiling - _TRANSPORT_MARGIN_S
    work_cwd = cwd or svc.project_path
    work_cwd = str(Path(work_cwd).resolve())

    # ── Access Guard: HARD deny when the effective cwd is under a deny rule
    # (docs/access-guard.md §3, handle_shell row). The cwd itself is
    # enforced; what the command touches beyond it is only covered by the
    # ADVISORY token scan below. Evaluator errors fail closed.
    try:
        cwd_denial = access_guard.check(work_cwd, "read", str(svc.project_path))
    except Exception as exc:
        cwd_denial = access_guard.Denial(
            "<evaluator-error>", "deny", "builtin",
            f"evaluator error: {type(exc).__name__}")
    if cwd_denial is not None and cwd_denial.kind == "deny":
        _log_access_denied(svc, work_cwd, cwd_denial, "cwd")
        return access_guard.refusal(cwd_denial, work_cwd, "read")

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
            # Structured entries are excluded from auto-injection even when a
            # (possibly hostile) registry says inject:true — belt over the
            # get_value gate that already refuses their whole payload.
            auto = [n for n, e in _creds.list_entries(svc.project_path).items()
                    if e.get("inject") and n not in requested
                    and e.get("type") not in _creds.STRUCTURED_TYPES]
            values, missing = _creds.resolve(requested + auto, svc.project_path)
            # Explicitly requested / templated refs must resolve; inject:true
            # entries that don't resolve are silently inert (see the
            # no-fall-through invariant in services/credential_store.py).
            hard_missing = sorted(set(tmpl_missing) | (set(missing) & set(requested)))
            if hard_missing:
                reasons = _creds.describe_missing(hard_missing, svc.project_path)
                detail = "\n".join(
                    f"  {r}: {reasons.get(r, 'unresolvable')}" for r in hard_missing)
                return (
                    "[c3_shell:error] unresolvable credential ref(s):\n"
                    f"{detail}\nsee c3_credentials(action='list')"
                )
            env_owner: dict[str, str] = {}
            for ref, cval in values.items():
                base_name, _, field = ref.partition(".")
                entry = _creds.get_entry(base_name, project_path=svc.project_path)
                env_name = entry.get("env_var") or base_name
                if field:
                    env_name = f"{env_name}_{field.upper()}"
                if env_name in env_owner and env_owner[env_name] != ref:
                    return (
                        f"[c3_shell:error] env-var collision: {env_owner[env_name]} "
                        f"and {ref} both map to ${env_name} — set a distinct "
                        "env_var on one of the entries"
                    )
                env_owner[env_name] = ref
                extra_env[env_name] = cval
            cred_names = sorted(set(tmpl_used) | set(values))
        except RuntimeError as exc:  # keyring/crypto unavailable
            return f"[c3_shell:error] credential store unavailable: {exc}"
        if exec_cmd != cmd and _BLOCKED.search(exec_cmd):
            return "[c3_shell:error] blocked pattern after credential expansion"

    # ── Access Guard ADVISORY token scan — best-effort, not enforcement.
    # Mirrors the _BLOCKED post-expansion re-check above: the raw template
    # `cmd` is always scanned and, when credential expansion changed the
    # string, the expanded `exec_cmd` is scanned too — a {{cred:NAME}}
    # template can neither smuggle in nor mask a denied path. Subshells,
    # variables, and globs are invisible to a token scan, so a clean pass
    # is never treated (or described) as enforcement.
    for scan_text in ([cmd] if exec_cmd == cmd else [cmd, exec_cmd]):
        scan_denial, hit_tok = _advisory_guard_scan(
            scan_text, work_cwd, str(svc.project_path))
        if scan_denial is not None:
            _log_access_denied(svc, work_cwd, scan_denial, "token_scan")
            msg = access_guard.refusal(scan_denial, hit_tok, "read") + (
                "\n[c3_shell:note] shell path scanning is best-effort "
                "(advisory) — a denied hit refuses the whole command, but a "
                "clean scan is not enforcement."
            )
            if enable_creds:
                try:  # never leak a decoded credential via the refusal text
                    msg = _creds.redact_text(msg)
                except Exception:
                    pass
            return msg

    # ── Access Guard WRITE-class scan (v2.102.0) — see _write_scan. Runs
    # after the read scan so a denied READ still reports as the read denial
    # it is; the same raw-and-expanded pair is scanned.
    write_refusal, write_grants = _write_scan(cmd, exec_cmd, work_cwd, svc)
    if write_refusal:
        if enable_creds:
            try:
                write_refusal = _creds.redact_text(write_refusal)
            except Exception:
                pass
        return write_refusal

    ghost_root = Path(work_cwd)
    _ghosts_before = _list_root_files(ghost_root)
    git_before = (
        _capture_git_state(work_cwd)
        if log and _GIT_MUTATING.search(cmd)
        else None
    )

    # Decoded values a child process may echo (env dumps, set, crash output)
    # are scrubbed IN THE STREAM by _run_sync's redactor, before a byte
    # reaches the spool or any preview, so neither the response nor a spill
    # ever holds one.
    result = await asyncio.to_thread(
        _run_sync, exec_cmd, work_cwd, timeout, extra_env or None)
    capture = result.get("capture")

    swept_ghosts = _sweep_new_ghost_files(ghost_root, _ghosts_before)

    # Belt over the in-stream scrub for the small-output text (and for a
    # result a test handed in without a capture).
    if enable_creds:
        if result.get("stdout") is not None:
            result["stdout"] = _creds.redact_text(result["stdout"])
        if result.get("stderr") is not None:
            result["stderr"] = _creds.redact_text(result["stderr"])
        if cred_names:
            # Usage state is keyed by entry NAME; collapse dotted field refs.
            _creds.touch_last_used(
                sorted({r.partition(".")[0] for r in cred_names}),
                svc.project_path)
            # Usage HISTORY: one event per ref, split by how the value left
            # the vault. `cmd` is the raw template form by construction.
            try:
                from services import cred_telemetry as _ct
                tmpl_set = set(tmpl_used)
                common = dict(project_path=svc.project_path, surface="shell",
                              session_id=str(getattr(svc, "session_id", "") or ""),
                              cmd_preview=cmd, exit_code=result["exit_code"])
                _ct.record_use([r for r in cred_names if r in tmpl_set],
                               action=_ct.ACTION_TEMPLATE, **common)
                _ct.record_use([r for r in cred_names if r not in tmpl_set],
                               action=_ct.ACTION_INJECT, **common)
            except Exception:
                pass
            if getattr(svc, "activity_log", None):
                try:  # digest integration: one low-volume event per exec
                    svc.activity_log.log("cred_use", {
                        "names": cred_names, "count": len(cred_names),
                    })
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
                    "creds": cred_names,
                })
            except Exception:
                pass

    # Approved write-target grants report themselves, once per use.
    warn = "".join(line + "\n" for line in write_grants)
    if _SOFT_WARN.search(cmd):
        granted = _shell_warn_grant(svc, work_cwd)
        if granted:
            # An approved shell_warn grant replaces the caveat, once per use.
            warn = granted + "\n"
        else:
            warn = "[c3_shell:warn] destructive pattern detected — verify before re-running\n"
    capped_note = ""
    if capped_from:
        # Named at the moment of the mistake, not in documentation nobody
        # reads at the moment they need it. The alternative IS the escape
        # hatch, so the line says which one.
        capped_note = (
            f"[c3_shell:capped] timeout={capped_from}s was requested but this "
            f"MCP client kills a tool call at {_ceiling}s; ran with {timeout}s. "
            f"For longer work use c3_shell_job(action='start', cmd=..., timeout=...), "
            f"which runs detached (up to 6 h), keeps the ledger and telemetry, and "
            f"pages its output back by id.\n"
        )

    try:
        body, stats = await asyncio.to_thread(
            render_shell_response, cmd, result, svc,
            filter_output=filter_output, warn=warn, capped_note=capped_note,
            touched_files=touched_files, cred_names=cred_names,
            swept_ghosts=swept_ghosts, max_bytes=max_bytes,
        )
        # Keep the raw streams only when the body dropped something: the
        # spill is what makes a clipped or filtered response recoverable
        # without re-running the command (docs/shell-output.md).
        if capture is not None:
            if stats.get("needs_spill"):
                try:
                    ShellOutputStore().promote(
                        capture, project_path=str(svc.project_path),
                        session_id=_grants.session_id(svc), cmd=cmd, cwd=work_cwd,
                        guard_paths=_scan_candidates(cmd, work_cwd),
                        exit_code=result.get("exit_code", -1),
                        timed_out=bool(result.get("timed_out")),
                        duration_ms=result.get("duration_ms", 0))
                except Exception as exc:
                    stats["spilled"] = False
                    stats["output_id"] = None
                    body += (f"[c3_shell:note] the raw output could not be kept "
                             f"({type(exc).__name__}: {exc}); the output id above is not retrievable\n")
                    try:
                        capture.discard()
                    except Exception:
                        pass
            else:
                capture.discard()
    except Exception:
        if capture is not None:
            try:
                capture.discard()
            except Exception:
                pass
        raise
    stats.pop("needs_spill", None)

    if result["timed_out"]:
        status = "TIMEOUT"
    elif result["exit_code"] == 0:
        status = "OK"
    else:
        status = f"FAIL({result['exit_code']})"
    summary = f"shell {status} in {result['duration_ms']}ms"
    # Telemetry: duration was always measured and never recorded (null on
    # 100% of records before 2.111.0); the stats dict is the per-call detail
    # a later budget phase is sized against.
    return finalize_with_tokens(
        finalize, svc, "c3_shell",
        {"cmd": cmd[:120], "cwd": work_cwd},
        body,
        summary,
        duration_ms=result.get("duration_ms"),
        detail=stats,
        response_tokens=stats.get("response_tokens", 0),
    )
