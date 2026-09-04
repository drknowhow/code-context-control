# Shell output spill store

Status: **S1 of the c3_shell remediation** — module and tests built
(`services/shell_output.py`, `tests/test_shell_output.py`); not yet wired
into `c3_shell`. This page records the contract the wiring step relies on.

## Why

Measured 2026-09-04 across 59 registered projects: 21 `c3_shell` calls a month
return more than 25k tokens, and Claude Code discards any MCP result over
25k tokens, so the agent gets an error and re-runs the command. The largest
was a 1.4M-token grep over a minified bundle. Today's filter is lossy
(`[N lines omitted]` is gone for good) and `communicate()` buffers the whole
output in RAM.

`ShellCapture` streams the child's stdout and stderr to two spool files while
keeping bounded head/tail previews and per-stream counts. When the output is
over the response budget, `ShellOutputStore.promote()` keeps the spool under
an opaque id the agent can page through later with `read`, `search` and
`tail`; otherwise `discard()` drops it.

## Where the bytes live

```
~/.c3/shell_out/                          # override: C3_SHELL_OUT_DIR
  .spool/<id>.stdout.part                 # while a command runs
  <project_id>/<session_id|nosession>/
      <id>.stdout  <id>.stderr  <id>.meta.json
```

`project_id` is 12 hex of sha1 over the normalized absolute project path
(forward slashes, no trailing slash, casefolded on Windows). Ids are
`o-` + 12 hex from `secrets`. The store is outside every project, so a
spill can never be committed, indexed, masked, or read through a
project-relative path, and the agent is never handed a path.

Retention: 3 days, and a global 250 MB cap evicted oldest-first. `promote()`
runs the sweep at most once per 10 minutes (`.last_sweep` marker). The sweep
touches only files it named itself (`<id>.stdout|stderr|meta.json` triplets
under a 12-hex project directory, meta-less orphans and `.spool/*.part`
older than a day); anything else under the root is left alone.

Permissions: on POSIX the root and its directories are `0700`, files `0600`.
On Windows the root gets `icacls <root> /inheritance:r /grant:r "<user>:(OI)(CI)F"`
once (marker `.acl`) and every file inherits it. The ACL step never fails a
promotion; `meta.acl_applied` records whether it took.

## Authorization: who may read a spill back

A spill is the raw bytes a command produced under the Access Guard rules in
force *at that moment*. If a later, less-privileged reader could fetch it,
spilling would be less safe than truncation — the store would be a durable
exfiltration channel around the guard. `ShellOutputStore.resolve()` is the only
way to a meta, and it refuses unless all of these hold:

| check | refusal wording |
|---|---|
| id well-formed and found under the caller's `<project_id>/<session>` | `output <id>: not found for this project and session` |
| `meta.project_id` == caller's project | same wording |
| `meta.session_id` == caller's session | same wording |
| not expired, both stream files present | `output <id>: expired …` / `… swept or deleted …` |
| `guard_check(path)` allows `meta.guard.cwd` and every `meta.guard.paths` entry | `output <id>: <path> is no longer readable under the current access rules (<kind> rule <glob>); re-run the command` |

Unknown, malformed, another project's and another session's ids all get the
SAME wording, so a probe cannot learn which ids exist. Only an id the caller
owns can get a more specific reason. `guard_check` is supplied by the caller
(the wiring wraps `services.access_guard.check(path, "read", project_path)`
with the current rules); passing `None` is refused — no verdict, no bytes.

## Redaction

`ShellCapture(proc, spool_dir, redact=..., kill_tree=...)` applies `redact`
to every piece before it is written, so the spool never holds an unredacted
credential. A line longer than 64 KiB arrives in 64 KiB pieces; the last
4 KiB of a partial piece are held back and re-joined with the next piece so a
secret straddling two pieces is still seen whole. `redact` must therefore be
idempotent — `services.credential_store.redact_text` is.

## Capture contract

- `proc` needs binary pipes (`Popen` without `text=True`); `TextIOBase` pipes raise `TypeError`.
- Memory per stream: `head_bytes + tail_bytes` (64 KiB each) plus one 64 KiB piece in flight. Measured: a 3 MB single line plus 200 KB of stderr peaks at ~0.8 MB of traced allocations.
- `wait(timeout)` returns `True` on timeout and calls `kill_tree(proc)`. Like `communicate()`, a child that exits while a grandchild keeps the pipe open counts as a timeout.
- `stats.stdout` / `stats.stderr`: `bytes`, `lines`, `longest_line` (characters), `sha256` (of the redacted spool), `head`, `tail`, `truncated_middle`.
- `text(stream)` returns the whole stream only up to 1 MiB — the small-output fast path; larger streams must be promoted.

## Reading back

- `read(meta, stream, lines=(a, b), max_bytes=18 KiB)` — 1-based inclusive window with a `[stdout La-b of N]` header; stops at the byte budget with a `continue with lines=(n, b)` hint.
- `search(meta, pattern, stream, context=2, max_matches=50, flags=0)` — hits as `>L{n}: …`, context as ` L{n}: …`, `---` between groups.
- `tail(meta, stream, lines=50)` — the newest lines survive when the budget clips.
- Lines longer than 512 characters keep 384 + 128 with `…[+N chars]…` between.
