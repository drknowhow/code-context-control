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
tokens, turns, model and a result excerpt.

## Loop

`oracle/services/scheduler.py` ticks every `scheduler_tick_seconds` (30):

1. lapse pending approvals past their TTL;
2. fold finished shell jobs into their runs (`JobStore.find`); a run whose job vanished is `lost`;
3. `ScheduleStore.claim_due` — one locked transaction advances `next_run_at` and writes the run row, so a claimed slot is never claimed again by any process; one-shots are disabled here;
4. each claimed run passes the misfire window, the overlap rule, the check command and the autonomy gate; autonomous runs are dispatched as detached `services/shell_jobs.py` jobs (tree-kill on timeout, no console window, output kept, feed notification);
5. approved runs are dispatched, denied or lapsed ones closed;
6. old run files are swept after `scheduler_run_retention_days`.

The tick never waits on a job. `start()` takes `~/.c3/oracle/scheduler.lock`;
a second Oracle stays in `standby` and retries each tick, so two processes on
one machine cannot both fire.

Every terminal run writes a `kind=schedule` notification into the project's
`.c3/notifications.jsonl` (which wakes the Desk and phone feeds) and an
`activity_log` row.

## Headless Claude

A skill or prompt run is `claude -p --output-format json --no-session-persistence
--permission-mode <mode> --permission-prompts none --name sched:<id> [--model]
[--effort] [--max-budget-usd] [--allowedTools]` with the prompt on stdin, run in
`cwd` with `CLAUDECODE` and the other nesting markers removed so Claude does not
treat it as a child session. Skills, CLAUDE.md, hooks and MCP servers load as
in an interactive session. Cost is read from the JSON result.

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
GET    /api/schedules/approvals?status&project
POST   /api/schedules/approvals/<id>      {decision: approve|deny, note}   (lapsed → 409)
POST   /api/schedules/preview             {when, count} → next fire times
GET    /api/schedules/status              POST /api/schedules/kick
```

## Config (`~/.c3/oracle/config.json`)

`scheduler_enabled`, `scheduler_tick_seconds`, `scheduler_default_timeout_s`,
`scheduler_max_timeout_s`, `scheduler_approval_ttl_s`,
`scheduler_misfire_grace_s`, `scheduler_run_retention_days`,
`scheduler_default_permission_mode`, `scheduler_allow_bypass_permissions`,
`scheduler_default_max_budget_usd`, `mobile_schedules_enabled`,
`mobile_schedules_write`.
