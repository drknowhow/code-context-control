"""Turn a schedule row into a detached job and read the job's result back.

Each fire is one ``services.shell_jobs`` job: the supervisor outlives the
Oracle, tree-kills on timeout, keeps the output and notifies the project's
feed. The runner only decides *what* to run (``plan``), starts it
(``dispatch``) and, once the job is terminal, folds its outcome and cost into
the run row (``finalize``). The tick loop in ``oracle/services/scheduler.py``
owns the order in which that happens.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from services.schedule_store import EXECUTED_STATUSES, ScheduleStore, prompt_text
from services.shell_output import OutputAccessError, _restrict_file

SESSION_ID = "scheduler"
CLAUDE_NESTING_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                       "CLAUDE_CODE_CHILD_SESSION", "MAX_THINKING_TOKENS")
CODEX_EFFORTS = {"": "medium", "low": "low", "medium": "medium", "high": "high",
                 "xhigh": "xhigh", "max": "xhigh"}
GROK_EFFORTS = {"": "", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
JOB_TO_RUN = {"done": "done", "failed": "failed", "timeout": "timeout",
              "cancelled": "cancelled", "lost": "lost"}
ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
STDERR_TAIL_LINES = 60
STDERR_TAIL_BYTES = 8 * 1024
OUTPUT_PAGE_BYTES = 1024 * 1024
OUTPUT_LINE_CLIP = 256 * 1024
_CHUNK = 1024 * 1024


class PlanError(ValueError):
    """The schedule cannot be turned into a command on this machine."""


@dataclass
class FirePlan:
    display: str
    cwd: str
    timeout_s: int
    argv: list | None = None
    cmd: str = ""
    stdin_text: str | None = None
    env: dict = field(default_factory=dict)
    env_drop: tuple = ()
    parser: str = "command"

    def payload(self) -> dict:
        return {"cmd": self.display, "exec_cmd": self.cmd or self.display, "argv": self.argv,
                "stdin_text": self.stdin_text, "env": dict(self.env) or None,
                "env_drop": list(self.env_drop)}


def plan(schedule: dict, *, check: bool = False, prompt_file: str = "") -> FirePlan:
    """The command for one fire of ``schedule``; ``check`` plans its ``check_command``.

    Raises ``PlanError`` when the provider CLI is missing or the row is
    malformed. ``prompt_file`` is where a provider that only takes a file
    (grok) should find the prompt; the caller writes it.
    """
    cwd = str(schedule.get("cwd") or schedule.get("project_path") or ".")
    timeout_s = int(schedule.get("timeout_s") or 1800)
    if check:
        cmd = str(schedule.get("check_command") or "").strip()
        if not cmd:
            raise PlanError("schedule has no check_command")
        return FirePlan(display=cmd, cmd=cmd, cwd=cwd, timeout_s=min(timeout_s, 600))
    if schedule.get("kind") == "command":
        cmd = str((schedule.get("target") or {}).get("cmd") or "").strip()
        if not cmd:
            raise PlanError("command schedule has no target.cmd")
        return FirePlan(display=cmd, cmd=cmd, cwd=cwd, timeout_s=timeout_s)
    prompt = prompt_text(schedule)
    if not prompt:
        raise PlanError("schedule has no prompt")
    provider = str(schedule.get("provider") or "claude")
    builder = _PROVIDERS.get(provider)
    if builder is None:
        raise PlanError(f"unknown provider {provider!r}")
    return builder(schedule, prompt, cwd, timeout_s, prompt_file)


def _exe(name: str) -> str:
    from cli.tools.delegate import _which
    path = _which(name)
    if not path:
        raise PlanError(f"{name} CLI not found on PATH")
    return path


def _plan_claude(schedule: dict, prompt: str, cwd: str, timeout_s: int, _prompt_file: str) -> FirePlan:
    from cli.tools.delegate import _EFFORT_LEVELS, _MODEL_NAME_RE
    argv = [_exe("claude"), "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--permission-mode", str(schedule.get("permission_mode") or "acceptEdits"),
            "--permission-prompts", "none", "--name", f"sched:{schedule.get('id', '')}"]
    model = str(schedule.get("model") or "").strip()
    if model:
        if not _MODEL_NAME_RE.match(model):
            raise PlanError(f"model name {model!r} is not a Claude model alias or id")
        argv += ["--model", model]
    effort = str(schedule.get("effort") or "").strip().lower()
    if effort in _EFFORT_LEVELS:
        argv += ["--effort", effort]
    budget = float(schedule.get("max_budget_usd") or 0)
    if budget > 0:
        argv += ["--max-budget-usd", f"{budget:g}"]
    tools = str(schedule.get("allowed_tools") or "").strip()
    if tools:
        argv += ["--allowedTools", tools]
    return FirePlan(display=f"claude -p {prompt}", cwd=cwd, timeout_s=timeout_s, argv=argv,
                    stdin_text=prompt, env={"C3_HOST": "claude-code"},
                    env_drop=CLAUDE_NESTING_VARS, parser="claude")


def _plan_codex(schedule: dict, prompt: str, cwd: str, timeout_s: int, _prompt_file: str) -> FirePlan:
    argv = [_exe("codex"), "exec"]
    model = str(schedule.get("model") or "").strip()
    if model:
        argv += ["-m", model]
    effort = CODEX_EFFORTS.get(str(schedule.get("effort") or ""), "medium")
    sandbox = "danger-full-access" if schedule.get("permission_mode") == "bypassPermissions" else "workspace-write"
    argv += ["--config", f"model_reasoning_effort={effort}", "--sandbox", sandbox,
             "--config", 'approval_policy="never"', "--json", "--skip-git-repo-check", "-"]
    return FirePlan(display=f"codex exec {prompt}", cwd=cwd, timeout_s=timeout_s, argv=argv,
                    stdin_text=prompt, env={"C3_HOST": "codex"}, env_drop=("CODEX_CLI",), parser="codex")


def _plan_gemini(schedule: dict, prompt: str, cwd: str, timeout_s: int, _prompt_file: str) -> FirePlan:
    argv = [_exe("gemini"), "--output-format", "json", "--approval-mode", "yolo"]
    model = str(schedule.get("model") or "").strip()
    if model:
        argv += ["-m", model]
    return FirePlan(display=f"gemini {prompt}", cwd=cwd, timeout_s=timeout_s, argv=argv,
                    stdin_text=prompt, env={"C3_HOST": "antigravity"}, env_drop=("GEMINI_CLI",),
                    parser="gemini")


def _plan_grok(schedule: dict, prompt: str, cwd: str, timeout_s: int, prompt_file: str) -> FirePlan:
    if not prompt_file:
        raise PlanError("grok needs a prompt_file")
    from cli.tools.delegate import _grok_cmd
    argv = _grok_cmd(prompt_file, str(schedule.get("model") or ""), 30, cwd, allow_write=True,
                     reasoning_effort=GROK_EFFORTS.get(str(schedule.get("effort") or ""), ""))
    argv[0] = _exe("grok")
    return FirePlan(display=f"grok {prompt}", cwd=cwd, timeout_s=timeout_s, argv=argv,
                    env={"C3_HOST": "grok"}, parser="grok")


_PROVIDERS = {"claude": _plan_claude, "codex": _plan_codex, "gemini": _plan_gemini, "grok": _plan_grok}


def output_format(schedule: dict) -> str:
    """How a client reads this schedule's stdout: ``claude-stream``, ``codex-json`` or ``text``."""
    if schedule.get("kind") == "command":
        return "text"
    provider = str(schedule.get("provider") or "claude")
    return {"claude": "claude-stream", "codex": "codex-json"}.get(provider, "text")


def dispatch(store: ScheduleStore, jobstore, schedule: dict, run: dict, *, check: bool = False) -> dict:
    """Start the job for ``run`` and record it; a spawn failure finishes the run."""
    run_id = run["run_id"]
    try:
        prompt_file = ""
        if schedule.get("provider") == "grok" and schedule.get("kind") != "command" and not check:
            path = store.runs_dir / f"{run_id}.prompt.txt"
            path.write_text(prompt_text(schedule), encoding="utf-8")
            prompt_file = str(path)
        fire = plan(schedule, check=check, prompt_file=prompt_file)
    except PlanError as exc:
        return store.finish_run(run_id, "failed", error=str(exc))
    job = jobstore.start(project_path=schedule["project_path"], session_id=SESSION_ID,
                         cmd=fire.display, cwd=fire.cwd, timeout_s=fire.timeout_s,
                         payload=fire.payload())
    fmt = "text" if check else output_format(schedule)
    if job.terminal and job.status != "done":
        status = JOB_TO_RUN.get(job.status, "failed")
        return store.finish_run(run_id, status, job_id=job.id, exit_code=job.exit_code,
                                error=job.error or "job ended before it ran", output_format=fmt,
                                **_output_fields(store, jobstore, run_id, job, status))
    status = "check_running" if check else "dispatched"
    return store.update_run(run_id, status=status, job_id=job.id, dispatched_at=job.created_at,
                            output_format=fmt)


def job_stream_path(jobstore, job, stream: str = "stdout") -> Path | None:
    """Where a terminal job's kept ``stream`` lives, or None when it was not kept or has expired."""
    if not getattr(job, "output_id", ""):
        return None
    try:
        meta = jobstore.output_store.resolve(job.output_id, project_path=job.project_path,
                                             session_id=job.session_id, guard_check=lambda _p: None)
        return Path(meta.stream_path(stream))
    except (OutputAccessError, OSError, ValueError):
        return None


def job_stdout(jobstore, job) -> str:
    """Raw kept stdout of a terminal job, or empty when it was not kept."""
    path = job_stream_path(jobstore, job, "stdout")
    if path is None:
        return ""
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def parse_result(parser: str, stdout: str) -> dict:
    """Cost, tokens, turns, model and a result excerpt from a provider's JSON stdout."""
    if parser == "claude":
        from cli.tools.delegate import parse_claude_json
        result_line, last_text = _claude_stream(stdout)
        if result_line is None and last_text:
            return {"result_excerpt": last_text}
        text, _ok, stats = parse_claude_json(stdout if result_line is None else result_line)
        out = {"result_excerpt": text}
        tokens = {k: stats[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                        "cache_write_tokens") if k in stats}
        if tokens:
            out["tokens"] = tokens
        for key in ("cost_usd", "turns", "model"):
            if key in stats:
                out[key] = stats[key]
        return out
    data = _trailing_json(stdout)
    if parser == "codex":
        return {"result_excerpt": _codex_text(stdout) or stdout.strip()}
    if parser == "gemini" and isinstance(data, dict):
        text = data.get("response") or data.get("text") or data.get("result") or ""
        out = {"result_excerpt": str(text)}
        models = ((data.get("stats") or {}).get("models") or {}) if isinstance(data.get("stats"), dict) else {}
        tokens = {"input_tokens": 0, "output_tokens": 0}
        for mdata in models.values():
            tok = (mdata or {}).get("tokens") or {}
            tokens["input_tokens"] += int(tok.get("input") or 0)
            tokens["output_tokens"] += int(tok.get("candidates") or 0)
        if models:
            out["tokens"] = tokens
            out["model"] = sorted(models)[0]
        return out
    if parser == "grok" and isinstance(data, dict):
        return {"result_excerpt": str(data.get("result") or data.get("response") or data.get("text") or "")}
    return {"result_excerpt": stdout.strip()}


def _claude_stream(stdout: str) -> tuple[str | None, str]:
    """The last ``result`` event line of a stream-json transcript and the last assistant text.

    ``(None, "")`` for a single JSON document without a ``type``; a stream cut
    off before its result (timeout, cancel) gives ``(None, <last text the
    model wrote>)``. Split on newlines only: an event may carry a raw U+2028.
    """
    result_line, last_text = None, ""
    for line in (stdout or "").split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result_line = line
        elif event.get("type") == "assistant":
            content = (event.get("message") or {}).get("content")
            texts = [str(c.get("text") or "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            if any(texts):
                last_text = "\n".join(t for t in texts if t).strip()
    return result_line, last_text


def _trailing_json(text: str):
    raw = (text or "").strip()
    start = raw.find("{")
    if start < 0:
        return None
    try:
        return json.loads(raw[start:])
    except ValueError:
        return None


def _codex_text(stdout: str) -> str:
    last = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            last = str(item.get("text") or last)
        elif isinstance(event, dict) and event.get("type") == "message" and event.get("text"):
            last = str(event["text"])
    return last


def finalize(store: ScheduleStore, jobstore, run: dict, job, parser: str = "command") -> dict:
    """Fold a terminal job into its run: status, exit code, duration, cost."""
    status = JOB_TO_RUN.get(job.status)
    if status is None:
        raise ValueError(f"job {job.id} is not terminal ({job.status})")
    extra: dict = {"job_id": job.id, "exit_code": job.exit_code, "duration_ms": job.duration_ms,
                   "output_id": job.output_id or None}
    if job.error:
        extra["error"] = str(job.error)
    if status in EXECUTED_STATUSES and parser != "command":
        stdout = job_stdout(jobstore, job)
        if stdout:
            extra.update(parse_result(parser, stdout))
    extra.update(_output_fields(store, jobstore, run["run_id"], job, status))
    return store.finish_run(run["run_id"], status, **extra)


def _output_fields(store: ScheduleStore, jobstore, run_id: str, job, status: str) -> dict:
    """The archive's run fields, plus ``stderr_tail`` when the run ended badly."""
    fields = archive_output(store, jobstore, run_id, job)
    if status != "done" or (job.exit_code or 0) != 0:
        tail = stderr_tail(jobstore, job)
        if tail:
            fields["stderr_tail"] = tail
    return fields


def archive_output(store: ScheduleStore, jobstore, run_id: str, job) -> dict:
    """Copy the job's kept stdout to ``schedule_runs/<run_id>.out``, keeping its last 8 MiB.

    Returns ``{"output_lines": lines in the whole stream, "output_dropped":
    lines cut from its head}``, or ``{}`` when the job kept no stdout or the
    copy failed. Line N of the archive is line ``output_dropped + N`` of the
    stream. The archive starts at a line boundary unless the stream's last
    line alone is longer than the cap; then it holds that line's tail.
    """
    src = job_stream_path(jobstore, job, "stdout")
    if src is None:
        return {}
    dst = store.runs_dir / f"{run_id}.out"
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with open(src, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = _archive_start(fh, size)
            fh.seek(0)
            dropped = _count_lines(fh, start, complete_only=True)
            lines = dropped + _count_lines(fh, size, complete_only=False)
            fh.seek(start)
            store.runs_dir.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as out:
                shutil.copyfileobj(fh, out, _CHUNK)
        os.replace(tmp, dst)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {}
    _restrict_file(dst)
    return {"output_lines": lines, "output_dropped": dropped}


def _archive_start(fh, size: int) -> int:
    """Offset the archive copies from: the first line start inside the last ``ARCHIVE_MAX_BYTES``."""
    if size <= ARCHIVE_MAX_BYTES:
        return 0
    cut = size - ARCHIVE_MAX_BYTES
    fh.seek(cut)
    pos = cut
    while pos < size:
        chunk = fh.read(min(_CHUNK, size - pos))
        if not chunk:
            break
        nl = chunk.find(b"\n")
        if nl >= 0:
            start = pos + nl + 1
            return start if start < size else cut
        pos += len(chunk)
    return cut


def stderr_tail(jobstore, job) -> str:
    """The last ``STDERR_TAIL_LINES`` lines (at most ``STDERR_TAIL_BYTES``) of the job's stderr."""
    path = job_stream_path(jobstore, job, "stderr")
    if path is None:
        return ""
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            fh.seek(max(0, size - STDERR_TAIL_BYTES))
            data = fh.read(STDERR_TAIL_BYTES)
    except OSError:
        return ""
    lines = [line.rstrip("\r") for line in data.decode("utf-8", "replace").split("\n")]
    if size > STDERR_TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]                     # the read started inside this line
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-STDERR_TAIL_LINES:])


def page_lines(path, *, first_n: int = 1, after: int = 0, limit: int = 500,
               max_bytes: int = OUTPUT_PAGE_BYTES, clip: int = OUTPUT_LINE_CLIP,
               complete_only: bool = False) -> tuple[list[dict], int]:
    """The lines of ``path`` numbered from ``first_n`` with ``n > after``, and the last line's number.

    Returns ``([{"n", "text", "clipped"?}], total)``; ``total`` is
    ``first_n - 1`` for an empty file. At most ``limit`` lines and about
    ``max_bytes`` of text, but at least one line when one is due. A line
    longer than ``clip`` bytes keeps its first ``clip`` bytes and carries
    ``clipped: true``. ``complete_only`` leaves out a last line that has no
    newline yet, for a file another process is still writing; the file is
    read only up to its size at open. Raises ``OSError`` when it cannot be opened.
    """
    lines: list[dict] = []
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        n = first_n - 1
        n += _skip_lines(fh, max(0, after - n), size)
        used = 0
        while len(lines) < limit and fh.tell() < size:
            mark = fh.tell()
            raw, complete, clipped = _read_line(fh, size, clip)
            if not complete and complete_only:
                fh.seek(mark)
                break
            if n + 1 > after:
                cost = len(raw) + 1
                if lines and used + cost > max_bytes:
                    fh.seek(mark)
                    break
                used += cost
                row = {"n": n + 1, "text": raw.decode("utf-8", "replace").rstrip("\r")}
                if clipped:
                    row["clipped"] = True
                lines.append(row)
            n += 1
        n += _count_lines(fh, size, complete_only=complete_only)
    return lines, n


def _skip_lines(fh, count: int, size: int) -> int:
    """Move past ``count`` newline-terminated lines (fewer at the end); returns how many."""
    passed = 0
    pos = resume = fh.tell()
    while passed < count and pos < size:
        chunk = fh.read(min(_CHUNK, size - pos))
        if not chunk:
            break
        found = chunk.count(b"\n")
        if found and passed + found <= count:
            passed += found
            resume = pos + chunk.rfind(b"\n") + 1
        elif found:
            idx = -1
            for _ in range(count - passed):
                idx = chunk.find(b"\n", idx + 1)
            resume = pos + idx + 1
            passed = count
        pos += len(chunk)
    fh.seek(resume)
    return passed


def _read_line(fh, size: int, clip: int) -> tuple[bytes, bool, bool]:
    """``(first clip bytes, complete, clipped)`` of the line at the cursor; consumes the whole line."""
    raw = fh.readline(min(clip + 1, size - fh.tell()))
    if raw.endswith(b"\n"):
        return raw[:-1], True, False
    if len(raw) <= clip:
        return raw, False, False
    while fh.tell() < size:
        more = fh.readline(min(_CHUNK, size - fh.tell()))
        if not more:
            break
        if more.endswith(b"\n"):
            return raw[:clip], True, True
    return raw[:clip], False, True


def _count_lines(fh, end: int, *, complete_only: bool) -> int:
    """Lines from the cursor to ``end``; a last one with no newline counts unless ``complete_only``."""
    count, last, pos = 0, b"\n", fh.tell()
    while pos < end:
        chunk = fh.read(min(_CHUNK, end - pos))
        if not chunk:
            break
        count += chunk.count(b"\n")
        last = chunk[-1:]
        pos += len(chunk)
    if last != b"\n" and not complete_only:
        count += 1
    return count


def cli_available(provider: str) -> bool:
    return bool(shutil.which({"claude": "claude", "codex": "codex", "gemini": "gemini",
                              "grok": "grok"}.get(provider, provider)))
