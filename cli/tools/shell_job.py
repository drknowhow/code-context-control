"""c3_shell_job — background shell jobs: start | status | tail | cancel | list (S3).

A job is a c3_shell command that outlives the MCP tool call: the run happens
in a detached supervisor (services/shell_jobs.py) and this handler only
starts, polls, tails and cancels it — nothing here ever waits on a job.

``start`` performs c3_shell's FULL pre-flight before anything is spawned:
the catastrophic-command blocklist, the Access Guard cwd deny, credential
expansion (``{{cred:NAME}}`` + ``env_creds``), the advisory read scan, and the
write scan with its confirm holds. Those pieces are imported from
cli.tools.shell so the two surfaces cannot drift; only the credential block
is repeated here, because it lives inline in handle_shell (S2 owns that file
during this phase — see the S3 brief).

A synchronous c3_shell timeout is never converted into a job: the caller
chooses this surface explicitly, and c3_shell's own `[c3_shell:capped]`
note names it as the escape hatch.
"""
from __future__ import annotations

import re
from pathlib import Path

from cli.tools import _grants
from cli.tools._helpers import finalize_with_tokens
from cli.tools.shell import (
    _BLOCKED,
    _advisory_guard_scan,
    _scan_candidates,
    _write_scan,
)
from cli.tools.shell_render import effective_budget, human_bytes
from core import count_tokens
from services import access_guard
from services.shell_jobs import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    JobAccessError,
    JobState,
    JobStore,
    _log_tail,
)

TOOL = "c3_shell_job"
_ACTIONS = ("start", "status", "tail", "cancel", "list")
_TAG = "[c3_shell_job"


def _err(msg: str) -> str:
    return f"{_TAG}:error] {msg}"


def _guard_check(svc):
    project_path = str(svc.project_path)

    def check(path: str):
        try:
            return access_guard.check(path, "read", project_path)
        except Exception as exc:  # evaluator error fails closed
            return access_guard.Denial("<evaluator-error>", "deny", "builtin",
                                       f"evaluator error: {type(exc).__name__}")
    return check


def _log_denied(svc, work_cwd: str, denial, surface: str) -> None:
    if not getattr(svc, "activity_log", None):
        return
    try:
        svc.activity_log.log("access_denied", {
            "tool": TOOL, "surface": surface, "cwd": work_cwd,
            "rule": denial.rule, "scope": denial.scope,
        })
    except Exception:
        pass


def _redact(text: str) -> str:
    try:
        from services import credential_store as _creds
        return _creds.redact_text(text)
    except Exception:
        return text


def _fmt_s(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _stream_note(job: JobState, stream: str) -> str:
    facts = getattr(job, stream) or {}
    if facts:
        return f"{stream} {human_bytes(int(facts.get('bytes', 0)))}/{int(facts.get('lines', 0))} lines"
    path = (job.spool or {}).get(stream)
    if path:
        try:
            return f"{stream} {human_bytes(Path(path).stat().st_size)} so far"
        except OSError:
            pass
    return f"{stream} —"


def _status_line(job: JobState) -> str:
    """One line the agent can act on: state, exit, duration, and where the output is."""
    head = f"{_TAG}:status] {job.id} {job.status}"
    if job.status == "running":
        body = (f"{head} {_fmt_s(job.elapsed_s())} of {job.timeout_s}s · $ {job.cmd_display} · "
                f"child pid {job.child_pid} · {_stream_note(job, 'stdout')}, {_stream_note(job, 'stderr')}\n"
                f"poll with action='status', read with action='tail' (stream='stdout'|'stderr', lines='80'), "
                f"stop with action='cancel'\n")
    elif job.status == "queued":
        body = f"{head} · $ {job.cmd_display} · supervisor pid {job.supervisor_pid} starting\n"
    else:
        exit_note = f"exit {job.exit_code}" if job.exit_code is not None else "no exit code"
        if job.timed_out:
            exit_note += f" (timed out at {job.timeout_s}s)"
        body = f"{head} {exit_note} in {_fmt_s((job.duration_ms or 0) / 1000)} · $ {job.cmd_display}"
        if job.output_id:
            body += (f"\noutput_id={job.output_id} ({_stream_note(job, 'stdout')}, {_stream_note(job, 'stderr')}) — "
                     f"page with c3_shell(output_id='{job.output_id}', output_action='read'|'search'|'tail')")
        if job.error:
            body += f"\nnote: {job.error}"
        body += "\n"
    return body


def _list_line(job: JobState) -> str:
    when = job.finished_at or job.started_at or job.created_at
    extra = ""
    if job.status == "running":
        extra = f" {_fmt_s(job.elapsed_s())}/{job.timeout_s}s"
    elif job.exit_code is not None:
        extra = f" exit {job.exit_code}"
    if job.output_id:
        extra += f" output_id={job.output_id}"
    return f"{job.id} {job.status}{extra} · {when[:19]} · $ {job.cmd_display}"


def _finalize(svc, finalize, action: str, args: dict, body: str, summary: str, job_id: str = "") -> str:
    if not body.endswith("\n"):
        body += "\n"
    tokens = count_tokens(body)
    return finalize_with_tokens(
        finalize, svc, TOOL, {"action": action, **args}, body, summary,
        detail={"cmd_class": "job", "action": action, "job_id": job_id,
                "response_bytes": len(body.encode("utf-8", errors="replace")),
                "response_tokens": tokens},
        response_tokens=tokens,
    )


# ── start ───────────────────────────────────────────────────────────────────

def _start(cmd: str, cwd: str, timeout, env_creds: str, svc, finalize) -> str:
    if not cmd or not cmd.strip():
        return _err("empty command")
    # _BLOCKED never consults grants — no approval flow reaches the
    # catastrophic tier, by spec (docs/confirm-guard.md §7).
    if _BLOCKED.search(cmd):
        return _err("blocked pattern — use native Bash with explicit approval if this is truly intended")
    try:
        timeout_s = int(timeout or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    capped_from = 0
    if timeout_s > MAX_TIMEOUT_S:
        capped_from, timeout_s = timeout_s, MAX_TIMEOUT_S
    timeout_s = max(1, timeout_s)
    work_cwd = str(Path(cwd or svc.project_path).resolve())
    project_path = str(svc.project_path)

    # ── Access Guard: HARD deny when the effective cwd is under a deny rule
    # (same row as handle_shell; evaluator errors fail closed).
    try:
        cwd_denial = access_guard.check(work_cwd, "read", project_path)
    except Exception as exc:
        cwd_denial = access_guard.Denial("<evaluator-error>", "deny", "builtin",
                                         f"evaluator error: {type(exc).__name__}")
    if cwd_denial is not None and cwd_denial.kind == "deny":
        _log_denied(svc, work_cwd, cwd_denial, "cwd")
        return access_guard.refusal(cwd_denial, work_cwd, "read")

    # ── credential expansion — `cmd` stays the RAW template form for every
    # echo/log; `exec_cmd` and the decoded values travel to the supervisor on
    # its stdin only (never argv, env or a file).
    from services import credential_store as _creds
    exec_cmd = cmd
    extra_env: dict[str, str] = {}
    secret_values: dict[str, str] = {}
    cred_names: list[str] = []
    tmpl_used: list[str] = []
    try:
        exec_cmd, tmpl_used, tmpl_missing = _creds.expand_templates(cmd, project_path)
        requested = [n.strip() for n in (env_creds or "").split(",") if n.strip()]
        auto = [n for n, e in _creds.list_entries(project_path).items()
                if e.get("inject") and n not in requested
                and e.get("type") not in _creds.STRUCTURED_TYPES]
        values, missing = _creds.resolve(requested + auto, project_path)
        hard_missing = sorted(set(tmpl_missing) | (set(missing) & set(requested)))
        if hard_missing:
            reasons = _creds.describe_missing(hard_missing, project_path)
            detail = "\n".join(f"  {r}: {reasons.get(r, 'unresolvable')}" for r in hard_missing)
            return _err(f"unresolvable credential ref(s):\n{detail}\nsee c3_credentials(action='list')")
        env_owner: dict[str, str] = {}
        for ref, cval in values.items():
            base_name, _, fld = ref.partition(".")
            entry = _creds.get_entry(base_name, project_path=project_path) or {}
            env_name = entry.get("env_var") or base_name
            if fld:
                env_name = f"{env_name}_{fld.upper()}"
            if env_name in env_owner and env_owner[env_name] != ref:
                return _err(f"env-var collision: {env_owner[env_name]} and {ref} both map to "
                            f"${env_name} — set a distinct env_var on one of the entries")
            env_owner[env_name] = ref
            extra_env[env_name] = cval
            secret_values[ref] = cval
        if tmpl_used:
            tvals, _ = _creds.resolve(list(tmpl_used), project_path)
            secret_values.update({k: v for k, v in tvals.items() if isinstance(v, str)})
        cred_names = sorted(set(tmpl_used) | set(values))
    except RuntimeError as exc:  # keyring/crypto unavailable
        return _err(f"credential store unavailable: {exc}")
    if exec_cmd != cmd and _BLOCKED.search(exec_cmd):
        return _err("blocked pattern after credential expansion")

    # ── Access Guard ADVISORY token scan — best-effort, not enforcement;
    # the raw and (when different) the expanded command are both scanned.
    for scan_text in ([cmd] if exec_cmd == cmd else [cmd, exec_cmd]):
        scan_denial, hit_tok = _advisory_guard_scan(scan_text, work_cwd, project_path)
        if scan_denial is not None:
            _log_denied(svc, work_cwd, scan_denial, "token_scan")
            return _redact(access_guard.refusal(scan_denial, hit_tok, "read") + (
                f"\n{_TAG}:note] shell path scanning is best-effort (advisory) — a denied hit "
                "refuses the whole job, but a clean scan is not enforcement."))

    # ── Access Guard WRITE-class scan with confirm holds (v2.102.0 semantics).
    write_refusal, write_grants = _write_scan(cmd, exec_cmd, work_cwd, svc)
    if write_refusal:
        return _redact(write_refusal.replace("[c3_shell:note]", f"{_TAG}:note]"))

    payload = {"cmd": cmd, "exec_cmd": exec_cmd, "env": extra_env, "secrets": secret_values,
               "cred_names": cred_names, "tmpl_used": list(tmpl_used)}
    session_id = _grants.session_id(svc)
    store = JobStore()
    try:
        job = store.start(project_path=project_path, session_id=session_id, cmd=cmd, cwd=work_cwd,
                          timeout_s=timeout_s, payload=payload,
                          guard_paths=_scan_candidates(cmd, work_cwd), cred_names=cred_names)
    except Exception as exc:
        return _err(f"could not start the supervisor: {type(exc).__name__}: {exc}")
    if cred_names:
        try:
            _creds.touch_last_used(sorted({r.partition(".")[0] for r in cred_names}), project_path)
        except Exception:
            pass
    if getattr(svc, "activity_log", None):
        try:
            svc.activity_log.log("shell_job", {
                "action": "start", "job_id": job.id, "cmd": cmd[:200], "cwd": work_cwd,
                "timeout_s": timeout_s, "creds": cred_names, "status": job.status,
            })
        except Exception:
            pass

    if job.status in ("failed", "lost"):
        log_tail = _log_tail(job.log_path)
        body = (f"{_TAG}:error] {job.id} {job.status} before running: {job.error or 'no detail'}"
                + (f"\n--- supervisor log ---\n{log_tail}" if log_tail else "") + "\n")
        return _finalize(svc, finalize, "start", {"cmd": cmd[:120], "cwd": work_cwd}, body,
                         f"shell job {job.id} {job.status}", job.id)
    lines = [f"{_TAG}:started] {job.id} $ {job.cmd_display} (timeout {timeout_s}s) — "
             f"poll with action='status', read with action='tail'"]
    pids = f"supervisor pid {job.supervisor_pid}"
    if job.child_pid:
        pids += f", child pid {job.child_pid}"
    lines.append(f"{job.status} · {pids} · cwd {work_cwd}" + (f" · creds {', '.join(cred_names)}" if cred_names else ""))
    if capped_from:
        lines.append(f"{_TAG}:capped] timeout={capped_from}s was requested; jobs run at most "
                     f"{MAX_TIMEOUT_S}s (6 h)")
    lines.extend(write_grants)
    body = "\n".join(lines) + "\n"
    return _finalize(svc, finalize, "start", {"cmd": cmd[:120], "cwd": work_cwd}, body,
                     f"shell job started {job.id}", job.id)


# ── entry point ─────────────────────────────────────────────────────────────

def handle_shell_job(action: str, cmd: str = "", cwd: str = "", timeout=DEFAULT_TIMEOUT_S,
                     env_creds: str = "", job_id: str = "", stream: str = "stdout",
                     lines=None, max_bytes=None, svc=None, finalize=None) -> str:
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return _err(f"action must be one of {', '.join(_ACTIONS)}; got {action!r}")
    if finalize is None:
        def finalize(name, args, resp, summ, **kw):  # noqa: E306 — plain passthrough
            return resp
    if action == "start":
        return _start(cmd, cwd, timeout, env_creds, svc, finalize)

    project_path = str(svc.project_path)
    session_id = _grants.session_id(svc)
    store = JobStore()
    if action == "list":
        jobs = store.list(project_path=project_path, session_id=session_id)
        if not jobs:
            body = f"{_TAG}:list] no jobs for this project and session\n"
        else:
            body = f"{_TAG}:list] {len(jobs)} job(s) for this project and session\n" + "\n".join(
                _list_line(j) for j in jobs) + "\n"
        return _finalize(svc, finalize, "list", {}, body, f"shell jobs list ({len(jobs)})")

    if not job_id:
        return _err(f"action='{action}' needs job_id='j-…'")
    guard_check = _guard_check(svc)
    try:
        job = store.resolve(job_id, project_path=project_path, session_id=session_id, guard_check=guard_check)
    except JobAccessError as exc:
        return _err(str(exc))

    if action == "status":
        return _finalize(svc, finalize, "status", {"job_id": job.id}, _status_line(job),
                         f"shell job {job.id} {job.status}", job.id)

    if action == "cancel":
        job, note = store.cancel(job)
        body = f"{_TAG}:cancel] {job.id} {note}\n" + _status_line(job)
        return _finalize(svc, finalize, "cancel", {"job_id": job.id}, body,
                         f"shell job {job.id} cancel → {job.status}", job.id)

    # tail
    stream = (stream or "stdout").strip().lower()
    if stream not in ("stdout", "stderr"):
        return _err("stream must be 'stdout' or 'stderr'")
    n = 50
    if isinstance(lines, int) and not isinstance(lines, bool) and lines > 0:
        n = lines
    elif isinstance(lines, str) and lines.strip():
        digits = re.sub(r"[^0-9]", "", lines)
        if digits:
            n = max(int(digits), 1)
    config_default = None
    try:
        config_default = (getattr(svc, "hybrid_config", None) or {}).get("shell_budget_bytes")
    except Exception:
        pass
    budget = effective_budget(max_bytes, config_default=config_default) - 512
    try:
        text = store.tail(job, stream, lines=n, max_bytes=budget, guard_check=guard_check)
    except Exception as exc:
        return _err(f"{job.id}: {exc}")
    header = f"{_TAG}:tail] {job.id} {job.status} · $ {job.cmd_display}"
    if job.terminal and job.output_id:
        header += f" · output_id={job.output_id}"
    body = header + "\n" + text + "\n"
    return _finalize(svc, finalize, "tail", {"job_id": job.id, "stream": stream, "lines": n}, body,
                     f"shell job {job.id} tail {stream}", job.id)


__all__ = ["handle_shell_job", "TOOL"]
