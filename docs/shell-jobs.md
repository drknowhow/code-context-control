# Background shell jobs (`c3_shell_job`)

Status: **S3 of the c3_shell remediation** — `services/shell_jobs.py`,
`cli/tools/shell_job.py`, the `c3_shell_job` MCP tool, and
`tests/test_shell_jobs_s3.py`. Builds on the S1 spill store
(`docs/shell-output.md`).

## Why

Measured 2026-09-04 across the registered projects: 44% of `c3_shell` wall
time is in calls over 60 s, and the MCP client kills a tool call at 120 s
(`c3_shell` clamps to 115 s and says so). So the full C3 test suite and every
long build left C3 for native Bash and lost the edit ledger, the telemetry
and the spill store for exactly the jobs that matter most.

The design decision for this phase (Cod, 2026-09-04): keep background
execution, but as a **separate phase and API** — `c3_shell_job(start | status
| tail | cancel | list)` — with a detached supervisor, persisted job state,
pid plus process creation time, a bounded spool and tree cancellation. A
thread inside the MCP process is not durable. **A synchronous `c3_shell`
timeout is never converted into a job.** Prefer `c3_ci` for a repository's
own workflows; jobs cover arbitrary builds and device commands.

## Contract

| action | needs | does |
|---|---|---|
| `start` | `cmd` (+ `cwd`, `timeout` s, `env_creds`) | c3_shell's full pre-flight, then spawns a detached supervisor; replies `[c3_shell_job:started] j-… $ <cmd> (timeout Ns)` at once |
| `status` | `job_id` | one line: state, exit code, duration, `output_id` once finished |
| `tail` | `job_id` (+ `stream`, `lines`, `max_bytes`) | last N lines under the c3_shell byte budget — the GROWING spool while running, the kept output after; header `[stream La-b of N, <state>]` |
| `cancel` | `job_id` | kills the child process tree only if its recorded creation time still matches; records `cancelled` |
| `list` | — | this project + session's jobs, oldest first |

`timeout` defaults to 1800 s and is clamped to **21600 s (6 h)**; a larger
request is run at 6 h and the reply says so. The MCP server never waits on a
job: `start` waits at most 3 s for the supervisor to report `running`,
`cancel` at most 5 s for the supervisor to record the outcome.

Output lands in the same spill store as `c3_shell` and is promoted **always**
(a job's output is the deliverable even when it is small): page it with
`c3_shell(output_id='o-…', output_action='read' | 'search' | 'tail')`, retention
3 days.

Jobs are **not proxied** through `c3_project`; a job is bound to the project
and session that started it.

## Where things live

```
~/.c3/shell_out/                                   # override: C3_SHELL_OUT_DIR
  .spool/<output-id>.{stdout,stderr}.part          # while the job runs
  <project_id>/<session_id|nosession>/
      jobs/<job-id>.json                           # the job record (atomic writes)
      jobs/<job-id>.supervisor.log                 # the supervisor's own stdout/stderr
      jobs/<job-id>.cancel                         # marker: cancel was requested
      <output-id>.{stdout,stderr,meta.json}        # the promoted output (S1 layout)
```

Job ids are `j-` + 12 hex from `secrets`. The record carries: project and
session identity, `cmd_sha256` and `cmd_display` (first line ≤ 240 chars of
the RAW template form — never the expanded command), `cwd`, timestamps
(`created_at`, `started_at`, `finished_at`), `status`, `exit_code`,
`timed_out`, `duration_ms`, `timeout_s`, `supervisor_pid` and `child_pid`
each with `*_start_time` (creation time as the OS reports it) and
`*_start_source` (how it was read), `output_id`, the spool paths while
running, per-stream `bytes/lines/longest_line/sha256` once finished, the
guard snapshot `{cwd, paths}`, `acl_applied`, credential NAMES, and an
`error` note. Records of finished jobs are swept after 3 days.

Permissions follow the S1 store: the root is user-only (POSIX `0700`,
Windows `icacls` with inheritance) and every file under it inherits.

## State machine

```
queued ──► running ──► done | failed | timeout | cancelled
   │           │
   └───────────┴──► lost
```

- `queued` — record written, supervisor spawned, payload not yet acted on.
- `running` — child spawned; `child_pid` + creation time and the spool paths
  are on disk.
- `done` / `failed` — exit 0 / non-zero. `timeout` — the job's own limit
  fired and the tree was killed (`exit_code` −1, `timed_out` true).
- `cancelled` — a cancel marker existed when the child ended.
- `lost` — the supervisor process is gone while the record still said
  `queued`/`running`. Whoever notices (status, tail, list, cancel, `reap()`)
  writes it: the child is killed if it is provably still ours, and the spool
  is promoted if it exists, so the bytes are not lost with the supervisor.

The supervisor is the only writer of `running` and of the four ordinary
terminal states. `cancel` never edits the record while the supervisor is
alive — it touches the `.cancel` marker, kills the child tree, and the
supervisor records `cancelled` — so two processes never race on one file.

## PID + creation time

A pid alone is not an identity: once the child exits, the OS may hand the
number to an unrelated process. Every recorded pid is stored with its
creation time and the method used to read it:

| platform | value | source |
|---|---|---|
| Windows | `GetProcessTimes` creation FILETIME (ctypes; a non-zero exit time means "not alive") | `GetProcessTimes` |
| Windows fallback | `(Get-Process -Id N).StartTime.ToFileTime()` | `powershell` |
| Linux | `/proc/<pid>/stat` field 22 (starttime) | `procfs` |
| other POSIX | `ps -o lstart=` | `ps` |

A kill is sent **only** when the live value, read the same way, equals the
recorded one. A reused pid is reported (`refused: pid N is now a different
process …; nothing was killed`) and nothing is signalled. A value read one
way and compared against another is treated as "not provably ours", which is
the conservative side for a kill.

Tree kill: Windows `taskkill /F /T /PID`; POSIX `killpg(SIGKILL)` (children
are started in their own session, so the pgid is the pid).

## Authorization

`status`, `tail` and `cancel` resolve a job under the **same rules
`ShellOutputStore.resolve` applies to an output**: same project (ids are
looked up under the caller's own `<project_id>/<session>` directory), same
session, and the CURRENT Access Guard rules re-applied to the job's `cwd`
and to every path its guard scan saw. Unknown, malformed, other-project and
other-session ids all answer `job <id>: not found for this project and
session`, so a probe learns nothing. A rule added after the job started
denies its state and its output exactly as it would deny the file.

`start` runs c3_shell's pre-flight before anything is spawned: the
catastrophic-command blocklist (before and after credential expansion), the
Access Guard cwd deny, the advisory read scan, and the write scan with its
confirm holds (an approved grant is consumed and echoed, a hold refuses the
job). Refusal texts are redacted.

## What the supervisor writes where

`python -m services.shell_jobs --supervise <job-id> --root <store root>`,
spawned with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`
and `close_fds=True` on Windows, `start_new_session=True` on POSIX; stdout and
stderr go to `jobs/<job-id>.supervisor.log`, stdin is the payload pipe.

1. Reads its payload from stdin (raw `cmd`, expanded `exec_cmd`, injected env,
   secret values, credential names). **Secrets travel on the pipe only** —
   never argv, never the supervisor's environment, never a file. The S3
   brief suggested a private env file deleted right after spawn; a pipe has
   no on-disk window at all, so it is used instead. A payload that does not
   arrive within 20 s fails the job.
2. Registers the values with `services.credential_store` so the streaming
   redactor scrubs them before a byte reaches the spool.
3. Spawns the command exactly as `c3_shell` does (Git Bash / cmd / sh
   selection, `_popen_kwargs` env), streams both pipes through
   `ShellCapture(live=True)` — the spool is flushed per piece so `tail` sees
   it grow — and writes `running` with the pids and creation times.
4. Enforces the job's own timeout with the same tree kill as `c3_shell`.
5. Refreshes the edit ledger for a git-mutating command that succeeded.
6. Promotes the spool to the store **always**; records `finished_at`, exit,
   duration, per-stream stats and `output_id`.
7. Appends a `shell_exec` record (with `job_id`, `job_status`, `output_id`) to
   the project's `.c3/activity_log.jsonl`, a telemetry record to
   `.c3/tool_telemetry.jsonl` (`tool: c3_shell`, `action: job`, `source:
   supervisor`, `detail: {cmd_class: 'job', job_id, exit_code, timed_out,
   stdout_bytes, stderr_bytes, duration_ms, output_id}`) so the S0
   instrument counts it under the `job` class, and credential usage events.
8. A crash inside the supervisor still leaves a terminal `failed` state with
   the last traceback line, never a phantom `running`.

## Paging output

```
c3_shell_job(action='tail', job_id='j-…', stream='stderr', lines='80')   # while running or after
c3_shell(output_id='o-…', output_action='read', lines='120-180')         # after: the kept stream
c3_shell(output_id='o-…', output_action='search', pattern='FAILED')
c3_shell(output_id='o-…', output_action='tail', lines='200')
```

`tail` is budgeted like every `c3_shell` response (18 KiB default, `max_bytes`
may only lower it) and keeps the newest lines when it clips; lines over
512 chars are clipped to prefix + suffix.

## Limits

- One job = one supervisor process (~40 MB of Python). Jobs are meant for
  the handful of long runs a session has, not for fan-out.
- `tail` counts the whole spool to report `of N`; on a multi-hundred-MB log
  that is a fraction of a second per call.
- A `lost` job's promoted meta carries the display form of the command as its
  `cmd` (the raw command only ever existed in the supervisor's payload).
- Cross-platform: built and tested on Windows; the POSIX branches
  (`start_new_session`, procfs / `ps`, `killpg`) are present but were not
  exercised on this box.
- The `[c3_shell:capped]` note in `c3_shell` still names native Bash as the
  escape hatch; pointing it at `c3_shell_job` is a one-line change in
  `cli/tools/shell.py`, which S2 owns during this phase.
