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
import shutil
from dataclasses import dataclass, field

from services.schedule_store import EXECUTED_STATUSES, ScheduleStore, prompt_text

SESSION_ID = "scheduler"
CLAUDE_NESTING_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                       "CLAUDE_CODE_CHILD_SESSION", "MAX_THINKING_TOKENS")
CODEX_EFFORTS = {"": "medium", "low": "low", "medium": "medium", "high": "high",
                 "xhigh": "xhigh", "max": "xhigh"}
GROK_EFFORTS = {"": "", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
JOB_TO_RUN = {"done": "done", "failed": "failed", "timeout": "timeout",
              "cancelled": "cancelled", "lost": "lost"}


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
    argv = [_exe("claude"), "-p", "--output-format", "json", "--no-session-persistence",
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
    if job.terminal and job.status != "done":
        return store.finish_run(run_id, JOB_TO_RUN.get(job.status, "failed"), job_id=job.id,
                                exit_code=job.exit_code, error=job.error or "job ended before it ran")
    status = "check_running" if check else "dispatched"
    return store.update_run(run_id, status=status, job_id=job.id, dispatched_at=job.created_at)


def job_stdout(jobstore, job) -> str:
    """Raw kept stdout of a terminal job, or empty when it was not kept."""
    if not job.output_id:
        return ""
    try:
        meta = jobstore.output_store.resolve(job.output_id, project_path=job.project_path,
                                             session_id=job.session_id, guard_check=lambda _p: None)
        return meta.stream_path("stdout").read_bytes().decode("utf-8", "replace")
    except Exception:
        return ""


def parse_result(parser: str, stdout: str) -> dict:
    """Cost, tokens, turns, model and a result excerpt from a provider's JSON stdout."""
    if parser == "claude":
        from cli.tools.delegate import parse_claude_json
        text, _ok, stats = parse_claude_json(stdout)
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
    return store.finish_run(run["run_id"], status, **extra)


def cli_available(provider: str) -> bool:
    return bool(shutil.which({"claude": "claude", "codex": "codex", "gemini": "gemini",
                              "grok": "grok"}.get(provider, provider)))
