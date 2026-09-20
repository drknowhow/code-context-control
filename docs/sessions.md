# Sessions: find an old one, see what it was, get back into it

*Since 2.143.0.*

Every Claude Code conversation is kept as a transcript
(`~/.claude/projects/<slug>/<uuid>.jsonl`). C3 keeps its own session records,
snapshots and tasks beside the project. Before 2.143.0 nothing joined the two,
so "which session was I doing X in, is it worth going back to, and how do I
get back" meant opening JSONL files by hand. Nothing could record that a
session was a dead end, either.

The **Sessions** view in the Hub, and its twin in C3 Desk, lists past sessions
per project, one row each. It resumes one with a click. The agent can mark a
session **stale**.

This is not the same "session" as [session-liveness.md](session-liveness.md).
That page covers the running MCP server and the per-project UI. This one covers
the history.

## One row per conversation

The row key is the Claude Code session id, which is the transcript's file
name and the value `claude --resume` takes. `services/session_catalog.py`
joins everything that belongs to it:

| field | from |
| --- | --- |
| `title` | the transcript's custom title, else its newest AI title, else the first prompt, else the C3 session description |
| `first_prompt`, `last_prompt` | the transcript head and its `last-prompt` row. System reminders, meta rows and tool results are stripped, and a slash command reads as `/plan <args>` |
| `started`, `last_active`, `branch` | transcript timestamps and `gitBranch` |
| `live` | a fresh `.c3/live/` heartbeat ([session-liveness.md](session-liveness.md)) |
| `note` | an agent note (`c3_session action='note'`), else the newest snapshot a person or agent wrote. Machine labels such as `auto-snapshot on stop` are skipped |
| `links.c3_sessions` | every C3 session this conversation ran. An MCP restart inside one conversation starts a new one, so there can be several |
| `links.tasks` | tasks created during the session (`origin_session`) or linked to it (`c3_task link_type='session'`) |
| `links.decisions`, `links.snapshots` | counted across all linked C3 sessions |
| `links.predecessor` / `successor` | a `/clear` chain (`session_end reason=clear`, then `session_open start_source=clear` within 2 minutes), or the successor named when the session was marked stale |
| `resume` | `command`, `cwd`, `remote_url`, `can_launch`, `why_not` |

A transcript is read from its head (64 KB, widened to 512 KB if a pasted
prompt pushes the first one out) and its tail (256 KB, then 1 MB, then 4 MB
until a title turns up). The whole file is never parsed. Results are cached
per file in `.c3/cache/session_catalog.json`, keyed on size and mtime. The
newest 200 transcripts per project are listed.

The transcript folder is the exact Claude Code slug of the project path, or an
exact alphanumeric match of it. It never falls back to "a folder whose name
contains the project name", because a wrong folder would resume another
project's session. A transcript whose recorded `cwd` is a different directory
is skipped. A Claude session run from a worktree has its own slug; it is still
found through its C3 record, and it resumes in the worktree.

`CLAUDE_CONFIG_DIR` is honoured, as it is by Claude Code.

## Stale is a flag, hints are not

**Stale** means superseded, finished, or abandoned: don't resume this. Only an
agent or a person sets it, and always with a reason. A successor session can
be named as well.

```
c3_session(action='stale', target='<id | 8+ char prefix | current>',
           reasoning='superseded by the 2.143 branch', data='<successor id>')
c3_session(action='unstale', target='<id>')
c3_session(action='note', data='Hub tab done', reasoning='Desk segment next')
c3_session(action='list')                  # unmarked; target=stale|likely|all
```

`list` returns titles and flags only. It never returns another session's
prompts. The managed instructions ask the agent to leave a `note` before it
stops, and to mark a session stale when its own work supersedes or abandons
that session.

**Hints** are grey chips that suggest a look. They never hide a row:

| hint | when |
| --- | --- |
| `idle Nd` | no activity for `sessions.idle_days` (default 14, `.c3/config.json`) |
| `ended by /clear` | the session ended with `/clear` |
| `branch gone` | its git branch no longer exists locally |
| `short` | the transcript is under 16 KB |

A live session gets no hints. The **Likely stale** filter shows unmarked rows
that have at least one hint.

Marks are appended to `.c3/session_marks.jsonl` (`{ts, provider, id, op,
reason, successor, summary, next_steps, by: agent|user, by_session}`) and
folded last-wins. Several MCP processes and the Hub can write at once, and an
append never loses a concurrent row the way rewriting one JSON file would.
The file is also the audit trail. Every mark writes a `session_mark` activity
row too.

## Resuming

**Resume ▸** asks the Hub to open a terminal in the session's directory
running `claude --resume <id>`. On Windows that is Windows Terminal, falling
back to a console. It uses the same launcher as "Open in Claude Code". Safety
properties:

- The route resolves `path` against the project **registry** first. An
  unregistered folder is 404, an uninitialized one 409.
- The id must be a UUID whose transcript exists for that project. The argv is
  fixed (`["claude", "--resume", id]`), and nothing from the request body
  reaches the command line.
- A **live** session is refused with 409. Two processes appending to one
  transcript would corrupt it. Continue it where it is open, or use the remote
  link.
- Codex and Gemini sessions are listed but not resumable yet.

**Copy** puts the exact command on the clipboard. **Remote ↗** opens the
claude.ai/code page when the session was bridged. The link is
`https://claude.ai/code/session_<x>`, built from the transcript's
`bridgeSessionId` `cse_<x>`. That mapping was inferred from one sample and
has not been confirmed by Anthropic; an id in any other shape gets no link.

## Routes (Hub, loopback + CSRF guard)

| route | what |
| --- | --- |
| `GET /api/hub/sessions/overview` | per project: `counts {total, live, stale, idle}`, `last_active`, `newest`; `features` for capability probing. Stats files only |
| `GET /api/hub/sessions?path=&stale=hide\|likely\|only\|all&q=&limit=&before=` | rows for one project, or all projects when `path` is empty. Built in parallel, newest first |
| `GET /api/hub/sessions/detail?path=&id=` | row + `decisions`, `snapshot`, `tasks`, and a `preview` of the last 8 turns. Each run of tool calls is one `⚙` line |
| `POST /api/hub/sessions/mark {path, id, op, reason?, successor?, summary?, next_steps?}` | recorded as `by: user` |
| `POST /api/hub/sessions/resume {path, id}` | 200 `{launched, command, cwd}`, 404, 409, or 500 with the command to run by hand |

These are deliberately **not** on the Oracle gateway. Oracle can bind to the
tailnet, and transcript previews should stay on this machine. The first
all-projects listing on a machine reads every transcript head and tail once.
Around 1,000 transcripts took about 20 seconds; later loads are served from
the cache.
