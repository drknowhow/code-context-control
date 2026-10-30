# Scheduled tasks

A scheduled task runs a Claude Code skill, a free prompt, or a plain command
at a time or cadence, with the model, effort and permission mode you choose,
either autonomously or only after a human approves each run. The Oracle hosts
the loop; the Hub, the Desk app and the phone show and decide; `c3_schedule`
lets an agent propose one.

## Model

A **schedule** (`~/.c3/oracle/schedules.json`):

| field | meaning |
|---|---|
| `kind` | `skill` (`target.skill` like `/daily_report`, optional `target.args`), `prompt` (`target.prompt`) or `command` (`target.cmd`, no LLM) |
| `provider` | `claude` (default), `codex`, `gemini`, `grok`; ignored for `command` |
| `model`, `effort` | passed through (`--model`, `--effort low..max`); empty = the CLI's default |
| `permission_mode` | Claude's `--permission-mode`; `bypassPermissions` only when `scheduler_allow_bypass_permissions` is on |
| `allowed_tools`, `max_budget_usd` | `--allowedTools`, `--max-budget-usd` |
| `autonomy` | `autonomous` fires; `approval` creates a pending approval first and expires it after `approval_ttl_s` (expire-and-skip) |
| `when` | `{type: cron, expr, tz}`, `{type: every, seconds}`, `{type: at, when}` or `{type: preset, preset: hourly\|daily\|weekdays\|weekly\|monthly, at: "09:00", days}` |
| `misfire` | `{policy: grace, grace_s}` runs a late slot inside the window, else logs `missed`; `skip` runs only slots caught within a minute |
| `overlap` | `skip` (default) logs `skipped_overlap` while a previous run is open; `allow` runs anyway |
| `check_command` | runs first; exit 0 = nothing to do (`check_skipped`, no LLM cost), exit 1 arms the run, anything else fails it |
| `project_path`, `cwd`, `timeout_s`, `enabled` | where and how long; `project_path` must be a registered project |

`tz: local` is the machine's current UTC offset (or `C3_SCHEDULER_TZ`); name an
IANA zone for DST-exact wall clocks.

A **run** (`~/.c3/oracle/schedule_runs/<run_id>.json`) moves through
`claimed → awaiting_approval | check_running | dispatched → running → done |
failed | timeout | cancelled | lost`, or ends early as `missed`,
`skipped_overlap`, `check_skipped`, `denied` or `expired`. It records the
shell job id, exit code, duration, output id, and for LLM providers the cost,
tokens, turns, model and a result excerpt. Also on the run:

| field | meaning |
|---|---|
| `output_format` | how to read its stdout: `claude-stream` (Claude skill/prompt), `codex-json` (Codex) or `text` (commands, Gemini, Grok, check commands); set at dispatch |
| `output_lines` | lines in the job's whole stdout, set when the run ends |
| `output_dropped` | lines cut from the head of the archive (below) |
| `stderr_tail` | the last 60 lines (at most 8 KiB) of stderr, only when the run failed, timed out, was cancelled or lost, or exited non-zero |

When a run ends, its job's stdout (already credential-redacted) is copied to
`schedule_runs/<run_id>.out`, so the transcript lives as long as the run row
rather than the shell output store's 3 days. The archive keeps the last 8 MiB,
starting at a line; its line N is line `output_dropped + N` of the stream. A
run that ends at its check command (`check_skipped`, or a failed check) is not
archived; the check's output stays in the shell output store.

## Loop

`oracle/services/scheduler.py` ticks every `scheduler_tick_seconds` (30):

1. lapse pending approvals past their TTL;
2. fold finished shell jobs into their runs (`JobStore.find`); a run whose job vanished is `lost`;
3. `ScheduleStore.claim_due` — one locked transaction advances `next_run_at` and writes the run row, so a claimed slot is never claimed again by any process; one-shots are disabled here;
4. each claimed run passes the misfire window, the overlap rule, the check command and the autonomy gate; autonomous runs are dispatched as detached `services/shell_jobs.py` jobs (tree-kill on timeout, no console window, output kept, feed notification);
5. approved runs are dispatched, denied or lapsed ones closed;
6. old run files are swept after `scheduler_run_retention_days`, with their `.out` archive and grok `.prompt.txt`.

The tick never waits on a job. `start()` takes `~/.c3/oracle/scheduler.lock`;
a second Oracle stays in `standby` and retries each tick, so two processes on
one machine cannot both fire.

Every terminal run writes a `kind=schedule` notification into the project's
`.c3/notifications.jsonl` (which wakes the Desk and phone feeds) and an
`activity_log` row.

## Headless Claude

A skill or prompt run is `claude -p --output-format stream-json --verbose
--no-session-persistence --permission-mode <mode> --permission-prompts none
--name sched:<id> [--model] [--effort] [--max-budget-usd] [--allowedTools]` with
the prompt on stdin, run in `cwd` with `CLAUDECODE` and the other nesting
markers removed so Claude does not treat it as a child session. Skills,
CLAUDE.md, hooks and MCP servers load as in an interactive session. stdout is
one JSON event per line (`system`, `assistant`, `user`, `result`, ...) as the
run goes, which is what makes it watchable; `-p` needs `--verbose` for
`stream-json`. Cost, tokens, turns, model and the excerpt are read from the last
`"type": "result"` event. A run cut off before that event keeps the model's
last text as its excerpt.

## REST

Oracle (`/api/schedules`, GETs open on loopback, mutations behind the dashboard
cookie or Discovery Bearer):

```
GET    /api/schedules?project=            POST /api/schedules
GET    /api/schedules/<id>                PATCH /api/schedules/<id>   (expected_rev → 409)
DELETE /api/schedules/<id>                POST  /api/schedules/<id>/run   {bypass_approval}
POST   /api/schedules/<id>/enable|disable GET   /api/schedules/<id>/runs
GET    /api/schedules/runs?project&status&schedule_id&limit
GET    /api/schedules/runs/<run_id>
GET    /api/schedules/runs/<run_id>/output?after&limit
GET    /api/schedules/approvals?status&project
POST   /api/schedules/approvals/<id>      {decision: approve|deny, note}   (lapsed → 409)
POST   /api/schedules/preview             {when, count} → next fire times
GET    /api/schedules/status              POST /api/schedules/kick
```

Desk and phone (`/api/mobile/schedules…`, Bearer on every method, capabilities
`schedules` / `schedules_write` switched by `mobile_schedules_enabled` /
`mobile_schedules_write`): the same list, one, runs, approvals, create, PATCH,
DELETE, `/run`, `/enable`, `/disable` and `/approvals/<id>/decide`, every row
filtered to the projects the gateway serves; `decided_by` is the client kind.
`GET /api/mobile/schedules/runs/<run_id>/output?after&limit` (capability
`schedule_output`, on with `mobile_schedules_enabled`) serves a run's output;
a run whose project the gateway does not serve is a 404.

Hub (`/api/hub/schedules…`, loopback): the same operations over the same files
plus `?path=` project filters, `project_name` on rows, `/preview`,
`/runs/<run_id>/output`, and `/status`, which proxies the Oracle's scheduler
status and reports `reachable: false` when the Oracle is down. The Hub never
dispatches; a Hub write is picked up on the Oracle's next tick.

### Run output

`…/runs/<run_id>/output?after=<n>&limit=<k>` returns the stdout lines numbered
above `after` (default 0), at most `limit` (default 500, at most 2000) and
about 1 MiB of text:

```
{ "run_id", "status", "open": run not terminal, "job_id",
  "format": "claude-stream" | "codex-json" | "text",
  "source": "archive" | "live" | "output_store" | "none",
  "lines": [{"n": 1-based line number, "text": line without newline, "clipped": true}],
  "next": n of the last line returned, or after when none,
  "total": lines available so far, "dropped": lines cut from the archive's head,
  "stderr_tail": "" unless the run ended badly }
```

The source is the run's `.out` archive once it ended; before that the job's
spool while it runs (`live`: complete lines only, a line still being written
waits for its newline), or the shell output store between the job ending and
the next tick folding it into the run. A line keeps its number across the
three, so a client polls with `after=next` until `open` is false. A line over
256 KiB comes back as its first 256 KiB with `clipped: true`. Unknown run →
404; a non-integer `after` or `limit` → 400.

## Config (`~/.c3/oracle/config.json`)

`scheduler_enabled`, `scheduler_tick_seconds`, `scheduler_default_timeout_s`,
`scheduler_max_timeout_s`, `scheduler_approval_ttl_s`,
`scheduler_misfire_grace_s`, `scheduler_run_retention_days`,
`scheduler_default_permission_mode`, `scheduler_allow_bypass_permissions`,
`scheduler_default_max_budget_usd`, `mobile_schedules_enabled`,
`mobile_schedules_write`.
