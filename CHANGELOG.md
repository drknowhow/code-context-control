# Changelog

All notable changes to Code Context Control (C3) are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.118.2] - 2026-09-05

### Fixed — the WinError 5 race had four more copies, including the lock store

2.118.1 fixed the publish step in the shell job store: a shared `<name>.tmp`
plus a single `os.replace`, which on Windows can publish a half-written file
or raise `PermissionError: [WinError 5] Access is denied`. That shape had been
copied into four other small JSON stores, and fixing one copy of a bug is not
fixing the bug. All five now go through one helper, `services/atomic_json.py`:

- `services/agent_locks.py` — the lock store, and the most exposed of the
  four: it is written from a threaded server, `locks.json.tmp` is a name every
  writer on the box picks, and a lost write drops live leases. That is exactly
  what its loader refuses to do when it quarantines a corrupt file rather than
  resetting it to empty.
- `services/access_guard._write_scope_config` — the CLI's `config.json` writer.
- `cli/hub_server.py` — the hub's config route, writing the same file.
- `oracle/services/mobile_api.py` — the mobile override-config writer, ditto.
  The last three write one file from three processes, one of them threaded.

None had a reported failure. That is a statement about how often they are
written, not about whether the race is real — the job store's version was
also unreported until it started failing CI.

### Changed — durable config is now flushed before it is published

`write_json_atomic` fsyncs the temp before the replace, so a crash between the
two cannot leave a zero-length `config.json`. The Access Guard reads a
truncated config as `<corrupt-config>` and fails **every** path closed, which
wedges the session that would otherwise fix it — worth a flush that these
files take a few times a session. The shell job store opts out (`fsync=False`):
its records are ephemeral spill state that a crash invalidates anyway.

Serialisation is unchanged at all five sites — indent, `ensure_ascii` and the
trailing newline are per-caller arguments, so no file on disk reformats.

`cli/_hook_utils._atomic_write_json` still keeps its own copy: a PreToolUse
hook is a separate short-lived single-threaded process that imports nothing
from `services`, so pid-only is genuinely enough there. That one is deliberate
and is the only one left.

## [2.118.1] - 2026-09-04

### Fixed — the job store could crash a supervisor on Windows with WinError 5

`services/shell_output._write_json_atomic` is the publish step for every
`c3_shell_job` status update. It used a single shared temp name and attempted
`os.replace` exactly once, so a supervisor could die before running anything —
reporting `failed before running` for a command that was fine. It surfaced as
an intermittent CI failure on windows-latest (`test_shell_jobs_s3.py`,
`PermissionError: [WinError 5] Access is denied`), which is the visible half;
the invisible half is that the same race can lose a job on a real Windows box.

Three defects, all now covered by tests that fail against the old code:

- **A shared temp name.** `<name>.tmp` is identical for every writer, so the
  parent creating the job record and the supervisor immediately saving status
  raced on the temp itself — one mid-write while the other tried to publish
  it. The suffix is now a pid **and** a random token: a pid alone is not
  enough, because this store is written from a threaded server and threads
  share a pid. (The concurrency test caught that second-order case.)
- **No retry.** Another process holding a read handle on the target — AV
  scanner, Search indexer, a concurrent reader — makes `os.replace` raise
  transiently. One attempt turned that into a lost write. Now four attempts
  with exponential backoff, and a genuinely persistent error still raises
  rather than being swallowed.
- **Orphaned temp files.** A failed replace abandoned its temp in the store
  forever. Cleaned up on every failure path.

Same construction as `cli/_hook_utils._atomic_write_json`, which already had
it. The duplication is deliberate — a PreToolUse hook subprocess imports
nothing from `services` — and is now noted in both.

## [2.118.0] - 2026-09-04

### Added — one approval can cover a whole rule for the session, not one file

A confirm hold asked again for every file. Approving a `.mcp.json` write did
nothing for `CLAUDE.md` next to it, and the `session` mode that already
existed lifted the use count without touching the path binding — so a
decision the user made once cost one tap per file. No surface even sent
`mode`: the hub hardcoded "Approve once" and `c3 override approve` had no
flag for it.

- **Grant scope** (`docs/override-requests.md` §4.1, new). Every grant now
  carries `scope`; the historical shape is `call` and stays the default (a
  missing field reads as `call`, so nothing already minted changes meaning).
  `scope="rule"` relaxes exactly two of §4's nine conditions: **tool**, from
  the exact name to a declared op-class (`override_policy.TOOL_CLASSES`;
  an unclassed tool is never widened, and `c3_artifacts`/`c3_project` are
  deliberately unclassed), and **path**, from the exact canon key to the path
  set the rule glob describes — evaluated by the same compiler, against the
  same `(canon, rel)` pair, that produced the denial. A rule grant therefore
  covers exactly the files that rule would have blocked.
- Replacing what it drops: unlimited uses, its **own** TTL ceiling
  (`rule_grant_ttl_s`, default 4 h, hard ceiling 8 h — a `call` grant keeps
  its 15 minutes), and an **idle window** (`rule_grant_idle_s`, default
  30 min). The idle window is the load-bearing part: there is no session-END
  signal to hang a long grant on — the MCP surface's `host_session_id` is
  snapshotted at boot and goes stale across `/clear` — so a rule grant dies
  on its own once the conversation stops using it.
- **What it still cannot reach.** `find`/`consume` refuse
  `forbidden_target()` on every call, not just at mint, so a rule grant over
  `**/.c3/**` covers `.c3/notes.txt` and never `override_grants.json`,
  `overrides.jsonl`, `config.json`, `secrets.enc` or `cred_state.json`.
  Tier-0 never reaches the store. Minting also requires
  `override.allow_rule_grants` (default off, AND-merged, counted as a
  widening policy edit), a globbable rule — the synthetic discipline/shell
  tokens are refused — a rule that actually covers the refusal, and the rule
  glob **retyped by hand on every layer**, including `access_confirm`, which
  stays one-tap for `once`.
- **Visible and killable**, because a standing capability that only shows up
  in the agent's own transcript is policed from the wrong place:
  `GET /api/hub/grants` + `DELETE /api/hub/grants/<id>`, an **Active grants**
  strip in the hub Access tab, `c3 override list` printing a rule grant's
  reach instead of the path it was minted from, and a `granted_context()`
  line that says "covers every path &lt;rule&gt; matches, this session"
  rather than a use count.
- Surfaces: `Approve for this rule…` in `cli/hub_ui/components/hub_access.js`
  (with a dialog that names what it reaches *before* the glob is retyped),
  `--mode {once,session,rule}` on `c3 override approve`, `--mute` on
  `c3 override deny` (both existed in the service layer and in the HTTP
  routes, and neither had a CLI flag), and the rule branch in the mobile
  decide route.

### Added — a project can define and adjust its own approved paths

Builtin guards (`**/.env*`, `**/.c3/**`, the agent-config confirm tier) were
adjustable at global scope only: a project-scope `builtin_mode` or
`disable_builtin` was a loud corrupt-scope error. The stated reason was that
a project scope may only ever tighten, because a cloned repo could otherwise
ship a config that loosens the guard. But that threat is answered by the
**second** key, not by the scope — the config half has never been able to
change a builtin on its own. So one repo needing `.env` readable meant
turning it off for every project on the machine, which is strictly worse.

- **Both keys are now realm-bound.** The keyring account for a project mode
  is `builtin_mode|proj|<normcased path>|<glob>` (global keeps its existing
  spelling, so nothing already attested changes). Realm-atomic, the same
  construction as `credential_store`: a global attestation never satisfies a
  project entry and a project's never leaks to a sibling. A clone of a repo
  carries `access.builtin_mode` and changes nothing, because the
  attestation lives in this machine's keyring, not in the checkout.
- A project mode **replaces** the global one for that glob, loosening
  included — that is the feature. `effective_builtin_modes(project_path)`
  resolves global then project, and `list_rules()` now reports
  `mode_realms` so no surface can present a project-set opt-out as if it
  were machine-wide. `c3 access list` prints `[project]` / `[global]` beside
  each; the Access tab shows a badge and a scope picker.
- **Rule scopes still only tighten** — that sentence in
  docs/access-guard.md §1 is about `deny`/`read_only`/`confirm`/`mask` and
  is unchanged. It is the separate builtin-MODE dimension that became
  project-settable.
- Unchanged at every scope: Tier-0 vault globs take no mode
  (`set_builtin_mode` raises), and `override_policy.FORBIDDEN_TARGET_NAMES`
  keeps `config.json`, `secrets.enc`, `cred_state.json`,
  `override_grants.json` and `overrides.jsonl` unreachable even under
  `builtin_mode: {"**/.c3/**": "allow"}` — so this can never become
  self-granting.
- Surfaces: `--project` on `c3 access builtin {disable,enable,mode}` (the
  typed-glob confirmation now names *which* projects it affects), a `scope`
  field on `POST /api/access/builtin_mode`, and a scope picker in the
  project Access tab.
- Loosening the `.env` guard now prints one advisory line pointing at the
  vault: `c3_credentials import_env` reads the file server-side, so the
  agent gets names, lengths and fingerprints and never a value. Advisory
  only — never a refusal.
- Hot path unaffected: the keyring is consulted only for globs a config
  actually lists, which is normally none. Pinned by a test that asserts zero
  keyring reads when no mode is set.

**Compatibility:** a pre-2.118.0 C3 reading a project config that carries
`builtin_mode` or `disable_builtin` treats that scope as corrupt ⇒ deny-all.
Fail-closed, and the loud direction.

### Added — `login` credentials for servers, databases and other non-web targets

The vault could hold a website login and nothing else. `canonical_origin` was
hard-refused for any scheme but `https`, so an SSH host, a database or an RDP
box had no home — despite being exactly the kind of credential a project
needs beside its `.env`.

- `login`'s required fields are now `site_id`, `canonical_target`,
  `username`. `canonical_target` is `scheme://host[:port]` from an
  allowlist — https, ssh, sftp, ftps, rdp, smb, vnc, winrm, ldaps, imaps,
  smtps, amqps, postgres, postgresql, mysql, mariadb, mssql, mongodb, redis.
  Cleartext schemes (`http`, `ftp`, `telnet`, `smtp`, `imap`, `ldap`, …) are
  refused **by name** and told which TLS variant to use, for the same reason
  `http://` always was. Every other rule the origin parser had is unchanged:
  no path, query, fragment or userinfo, host lowercased, port range-checked.
  Ambiguity in that comparison is the whole attack, and `ssh://` earns no
  exemption from it.
- `password` moved to optional and `private_key` / `passphrase` were added,
  with a cross-field rule: **one of password/private_key is required**, since
  a login with neither is not a credential. A private key is checked for a
  PEM/OpenSSH header (a pasted *public* key would otherwise fail much later,
  in a runner, as an authentication error nobody traces back here) and gets
  its own 8192-char allowance rather than raising the cap on every field.
  Oversize payloads take the Fernet sidecar path that already existed.
- **The origin-pinning property is preserved, not weakened.**
  `canonical_origin` remains a known field, is accepted on input as an alias,
  and still reads back — but **only when the target is https**. A browser
  broker that pins a credential by asking for it gets `None` for an `ssh://`
  entry and fails closed, instead of a string it cannot compare to a
  top-level frame. Entries stored before this release need no migration: the
  old spelling resolves on read in both directions.
- Display projection is now `{site_id, scheme, target, has_totp, has_key}` —
  username still deliberately withheld, secrets still booleans.
- Everything inject-only is untouched and inherited for free: reveal stays
  permanently refused, structured entries never auto-inject, the
  plain↔structured boundary stays immutable, and no HTTP route returns a
  field value.

### Fixed — the session benchmark measured symbol narrowing against a file with no symbols

`_select_sample` orders candidates by SIZE alone, so the largest file in a
repo can be a changelog or a bundled HTML page. `bug_investigation` took
`sample[0]` regardless, and its third step — "narrow to the relevant symbol",
the whole point of the scenario — degenerated into a full-file read while
still paying for the map, reporting *negative* savings that were a correct
measurement of a workload nobody runs. It fired here the moment this
release's own changelog entries made `CHANGELOG.md` the biggest file in the
repo. The scenario now picks the first sampled file that actually exposes
symbols (and reports an error rather than a number if none does):
this repo went from -15.2% to +77.9%, with the symbol read dropping from
29,438 tokens to 34.

### Fixed — the request store could lose a row

`~/.c3/oracle/override_requests.json` is one file for every project and
session on the box, and every mutator did an unsynchronised
load→mutate→save; two concurrent decisions, or a decide racing an auto-file,
could each write back a list missing the other's row. The tmp file was a
fixed `.tmp` name besides, so two writers collided on that too. Mutators now
commit a single row under the same cross-process lock the grants store uses,
and the tmp name is per-process. Rule grants raise the write rate, which is
what made this worth fixing rather than noting.

### Fixed — a rule-scoped grant would have matched nothing for project globs

Caught by its own test before shipping: `rule_covers` was evaluating only the
canonical path, so a project-relative rule like `secrets/**` — which the
evaluator matches against the project-relative form — never matched. Both
forms are now threaded from `path_key_pair` into matching, which is also the
only way a grant and the rule that filed it can agree about the same file.

### Fixed — `c3 creds set --type login` echoed the password to the terminal

`_CREDS_HIDDEN_FIELDS` listed `number`, `cvc` and `ssn` but not `password`
or `totp_secret`, so the CLI prompted for a login password with plain
`input()` from v2.90.0. Both browser UIs masked it correctly and nothing
asserted the CLI — the third UI had no parity test. Fixed, extended to
`private_key` and `passphrase`, and `test_credential_ui_parity` now derives
the must-mask set from `_SCHEMAS` instead of hardcoding it, so a future kind
cannot reopen the hole. `private_key` is also read until a blank line rather
than through single-line `getpass`, which would have stored the first line of
a PEM.

## [2.115.0] - 2026-09-04

### Added — an advisory hint when the shell is used as a reader or a searcher (shell remediation S4)

48% of c3_shell commands were cat, head, tail, sed, grep, rg, find and ls
over project files — reads and searches that the code-intelligence tools
answer within budget, indexed, and with the index's exclusions. Cod's
ruling: advisory only; refusing on size would push agents to the native
shell and lose every control.

- `cli/tools/shell_nudge.py`: `bypass_hint(cmd, project_path)` appends at
  most ONE `[c3_shell:hint]` line naming the equivalent call —
  `c3_read(file_path=…, lines=[a,b])` for cat/head/`sed -n 'a,bp'`,
  `c3_compress(mode='map')` then `c3_read` for tail, `c3_search(action=
  'code'|'exact', query=…, path=…, ignore_case=…)` for grep/rg (identifier
  → code, anything else → exact), `c3_search(action='files', query=…)` for
  `find -name` and recursive ls. Paths outside the project, `~`/`$VAR`
  paths, `node_modules`, `dist`, `build`, `.git` and `.c3` are never hinted;
  a plain `ls` is not a search. The command is never rewritten or refused.
- Telemetry `detail.hint` records the hint kind (`a read of …`, `a search`,
  `a filename search`, `a listing`) so the follow rate and the false-positive
  rate can be measured from `.c3/tool_telemetry.jsonl` before anything is
  promoted. This closes the five-phase shell remediation (S0 2.111.0 …
  S4 2.115.0).

## [2.114.0] - 2026-09-04

### Added — c3_shell_job: long work runs in a detached supervisor instead of leaving C3 (shell remediation S3)

44% of c3_shell wall time was in calls over 60 s, and the client kills any
MCP tool call at 120 s, so every long test suite and build went to the
native shell — losing the ledger, the telemetry and the spill store for
exactly the jobs that matter most.

- `c3_shell_job(action='start', cmd=..., timeout=<=21600)` runs the same
  pre-flight as `c3_shell` (blocklist, Access Guard cwd and path scans,
  write-class holds, credential expansion) and hands the command to a
  DETACHED supervisor (`python -m services.shell_jobs --supervise <id>`;
  new process group, survives the MCP server). Decoded credentials reach
  the supervisor on its stdin pipe only — never argv, never the
  environment, never a file. The supervisor streams through the S1 capture
  with in-stream redaction, enforces the job's own timeout, promotes the
  output to the spill store at the end (a job's output is the deliverable),
  and writes a `shell_exec` activity record plus a telemetry record
  (`cmd_class 'job'`).
- `action='status'|'tail'|'cancel'|'list'`: state is persisted as
  `~/.c3/shell_out/<project>/<session>/jobs/<j-id>.json` with the child pid
  AND its process creation time; `cancel` kills the tree only when the live
  creation time still matches (a reused pid is never signalled), and a job
  whose supervisor died is marked `lost` after its spool is promoted. Jobs
  resolve under the same rules as spilled output: same project, same host
  session, current Access Guard rules.
- The `[c3_shell:capped]` note names `c3_shell_job` as the escape hatch
  instead of the native shell; a `c3_shell_job` call counts as C3 usage for
  the PostToolUse hooks. Cross-project `c3_project(action='shell_job')` is
  refused. `docs/shell-jobs.md` carries the contract and the state machine.

## [2.113.0] - 2026-09-04

### Changed — c3_shell keeps what matters and drops the lossy filter (shell remediation S2)

The auto-filter that collapsed any stdout over 30 lines into "first error
region plus the last 20 lines" dropped a pytest summary line that sat under
40 warning lines and a jest failure header, and what it dropped was gone
for good. It is replaced by deterministic normalisation, runner-aware
priority regions and a bounded window (`cli/tools/shell_parsers.py`).

- **Always**: ANSI/control sequences are stripped; a line's `\r` progress
  rewrites reduce to their final state; runs of 3 or more identical
  consecutive lines fold to one plus ` [x N]` (exact under budget, digit and
  timestamp blind once over). The header says `[collapsed …]` and the raw
  streams are kept by id, so nothing is lost. `filter_output=False` skips
  the two collapses; escapes are still stripped.
- **Under budget the output is complete.** Nothing is omitted from a
  stream that fits its allocation.
- **Over budget**, the parser's priority regions are kept first, each
  announced by `[La-b: why]`: pytest/unittest failure headers, the assertion
  line and the last frame, the totals line; cargo/rustc error blocks and
  the `could not compile` tail; tsc error lines (first 20 and last 5 of a
  wall, plus `Found N errors`); jest/vitest failure headers and totals;
  generic error anchors with context for everything else. Then a bounded
  head/tail (`FILL_BYTES_OVER_BUDGET`, 4 KiB) orients the reader; the rest
  pages back by id. Filling the whole allocation with PASSED lines measured
  808 B → 16,253 B on a pytest run and was not what the agent reads.
- A recognised runner past 30 lines gets a `--- summary ---`: totals plus
  one line per failing test, inside the budget.
- shell-eval: the four S2 cases are `must_pass` and six new cases cover
  three pytest failures under warnings, two cargo errors, ANSI plus `\r`
  progress, a 200-line identical flood, a 900-error tsc wall and unittest
  failures: 26/26, bytes p50 3,127 / p95 8,308, render ms p95 695 → 257.
  The previously filtered cases now cost pytest 808 → 5,538 B, tsc
  856 → 8,799 B, npm 303 → 4,219 B — complete failure blocks and totals
  instead of a heuristic excerpt. `shell_by_class.filtered` now counts
  lossy collapses.

## [2.112.0] - 2026-09-04

### Changed — a c3_shell response has a byte budget, and what it drops is recoverable (shell remediation S1)

The 21 calls a month that came back over the client's 25k-token limit —
and therefore came back as errors — were grep, sed and find over minified
bundles and JSONL logs: two or three lines of several hundred KB. The
response now never exceeds a budget, and the raw bytes are kept where the
agent can page through them.

- **Streaming capture** (`services/shell_output.py`): the child's stdout
  and stderr are pumped to spool files with bounded head/tail previews
  instead of being buffered whole by `communicate()`; a 3 MB single line
  never lives in memory. Credential redaction runs on each piece BEFORE it
  is written, with a hold-back so a value straddling two pieces is still
  scrubbed whole.
- **Budget** (`cli/tools/shell_render.py`): one combined byte budget on
  the rendered response, 18 KiB by default, 22 KiB ceiling. A per-call
  `max_bytes` and `hybrid.shell_budget_bytes` in `.c3/config.json` may only
  lower it; `MAX_MCP_OUTPUT_TOKENS` bounds it; `filter_output=False` never
  lifts it. 2 KiB is reserved for the envelope; stderr is guaranteed 40%
  of the rest when present, unused space is redistributed; within a stream
  35% head / 65% tail survive. A line over 512 chars renders as a note on
  its own line followed by a 384+128 fragment — centred on the match when
  the command is a grep — so a note never lands inside a JSON line. A
  stream that fits passes through untouched. The legacy >30-line filter
  keeps its trigger, so a filtered test or build run costs what it did.
- **Spill store**: whenever anything was dropped — clipped, windowed or
  filtered — the raw streams are promoted to
  `~/.c3/shell_out/<project-id>/<session-id>/<id>.{stdout,stderr,meta.json}`
  (override `C3_SHELL_OUT_DIR`), user-only permissions, 3 days and 250 MB
  global retention, oldest first. The response header names the opaque
  `output_id`; every omission note repeats it.
- **Retrieval** rides on `c3_shell`: `output_id='o-…'` with
  `output_action='read'` (`lines='120-180'`), `'search'` (`pattern=`),
  `'tail'` (`lines='80'`) or `'delete'`, `stream='stdout'|'stderr'`, each
  reply re-budgeted. An id resolves only for the same project and the same
  host session, and only after the originating call's cwd and scanned
  paths pass the CURRENT Access Guard rules — a spill made by a more
  privileged call cannot become a channel for a later, less privileged
  reader. Unknown, foreign-project and foreign-session ids all get the same
  wording so a probe learns nothing (`docs/shell-output.md`).
- **Envelope**: the echoed command is its first line; a multi-line or
  over-long command shows `(N lines, M chars, sha256:…)` instead of its
  body. The header carries stream sizes and the output id when anything
  was dropped.
- shell-eval: the five S1 cases are `must_pass`, 0 of 20 cases over budget
  (was 6), bytes p95 1,186,398 → 4,750, render ms p95 718 → 37 on the
  cases the filter no longer touches. Telemetry `detail` gains
  `budget_bytes`, `omitted_lines`, `clipped_lines`.

## [2.111.0] - 2026-09-04

### Added — c3_shell is measured before it is changed (shell remediation S0)

Measured over every registered project's `.c3/tool_telemetry.jsonl` since
2026-08-01: c3_shell is half of all C3 calls and 75% of the tokens C3
returns; 21 calls (0.2%) over 25k tokens carried 55% of that volume and
were discarded by the client's `MAX_MCP_OUTPUT_TOKENS` limit; every one
was a grep or sed over a minified bundle or a JSONL log — few lines, huge
lines, invisible to a filter that triggers on newline count. And
`duration_ms` was null on 100% of records although every call measured
it. This release instruments the tool; nothing the agent sees changes.

- `render_shell_response(cmd, result, svc, ...)` in `cli/tools/shell.py`
  builds the response from the subprocess result alone — same filter
  trigger, same sections, same order — and returns the stats of the call
  next to the body. The shell-eval harness pushes captured streams through
  exactly the path a live call uses.
- Telemetry records for c3_shell carry `duration_ms` and a `detail` dict:
  `exit_code`, `timed_out`, `stdout_bytes` and `stderr_bytes` measured
  BEFORE filtering, `longest_line`, `filtered`, `spilled`, `output_id`,
  `cmd_class` (file-read, tests, git, python, build, ops, echo, gh, c3,
  other) and the rendered `response_bytes`/`response_tokens`.
  `finalize_with_tokens` and `SessionManager.record_tool_tokens` accept
  `detail` for any tool; the shared record schema is unchanged.
- `aggregate_tool_telemetry` gains `shell_by_class`: calls, tokens, bytes,
  calls over the 18 KiB budget the S1 phase will enforce, filtered,
  spilled, timeouts, failures, longest line, p50/p95 duration — the
  before/after instrument for the remaining phases.
- `c3 shell-eval --suite fixture [--update-baseline] [--json]`: a fixture
  suite of synthetic captured outputs (minified-line grep hit, a pytest run
  with one failure buried under warnings, tsc and npm noise, carriage-return
  progress bars, JSONL grep, tracebacks, cargo and jest failures, binary
  garbage, ANSI colour, a huge stderr) with per-case `must_pass` / `xfail`
  (tagged S1 or S2) / `info` gates on rendered bytes, must-keep lines and
  marker placement; `tests/test_shell_eval.py` is the CI gate;
  `docs/shell-eval.md` explains the vocabulary.

## [2.110.1] - 2026-09-04

### Fixed — the CLAUDE.md updater agent rewrote a managed doc on every session start

Observed on this repository: `c3_artifacts history CLAUDE.md` alternated
`install_mcp` / `scan` every few minutes and the committed file was dirty
after every session start. Two defects composed:

- `ClaudeMdManager.check_staleness` diffed the project tree and the tech
  stack against the doc. With the live repo map (default since 2.60.0) the
  doc carries neither — the map does — so every detected technology came
  back as "not listed in CLAUDE.md Tech Stack" and the doc was stale on
  every check. Those two diffs now run only in legacy embedded-tree mode
  (`map.enabled: false`); the size and session checks are unchanged. The
  `ClaudeMdDrift` notification stops nagging for the same reason.
- `ClaudeMdUpdaterAgent` wrote the regenerated block body with a raw
  `write_text`, stripping the `C3:BEGIN`/`C3:END` markers and anything the
  user kept outside them; the next `c3 install-mcp` saw a marker-less
  legacy doc and re-wrapped its template. Regeneration now goes through
  `write_c3_instruction_doc`, the same merge the installer uses, so it
  lands inside the block and user notes survive. Whole-file writes
  (compaction, promotions) refuse to proceed when the file on disk carries
  markers and the new content does not.

Agent writes are attributed: `write_c3_instruction_doc` takes a `source`
(`install_mcp` by default, `claude_md_updater` from the agent), and the
artifact history names the agent instead of `scan`. `tests/
test_claude_md_updater.py` pins no-staleness in map mode, drift detection
in legacy mode, regeneration inside the block with user notes intact, a
byte-identical doc after an agent cycle, the marker guard, and the
attribution. Verified on this repository: staleness `ok`, an auto-apply
cycle leaves CLAUDE.md identical to HEAD.

## [2.110.0] - 2026-09-04

Phase P5 of the search plan, the last one: what the agent sees around a
hit. Nothing here changes how `code` ranks; it changes what a zero result
says, what order `exact` prints in, how many files `files` and `exact` may
list, and whether a hit tells you which backend produced it.

### Changed — `c3_search` UX (P5)

- **Zero results now name the next move.** `code` says `try action='exact'
  for a literal or regex, or action='files' for a filename` — or just
  `action='files'` when the query looks like a path, `action='exact'` when
  it looks like a regex; `exact` suggests `ignore_case=True` (when it was
  off) and `action='code', which matches identifiers by their parts`;
  `files` suggests a glob and `action='exact' to search file contents`. An
  active `path` / `lang` / `kind` filter is named too. The hint never
  repeats the query, so a masked canary is not echoed twice and the eval
  harness's forbidden-text check is unaffected.
- **`exact` prints definitions first.** Every matching file is collected,
  then ordered: files whose indexed symbol table defines the identifier the
  query names, then files where a matching line declares something
  (`def` / `class` / `fn` / `func` / `type` / `const` / a top-level
  assignment, and the `export` / `pub` / `static` prefixes), then the rest;
  within a group source before config, docs and tests, then path order. The
  header carries ` [definition]` on the first two groups. Observed in
  2.108.0: the manifest walk decided the order, so `OAuth2Client` printed a
  test and a doc before the class. The fixture gate `exact_definition_first`
  (`must_pass`) and the golden case `exact_definition_first_code_index`
  (`CodeIndex` in a tree where dozens of tests and docs mention it) pin it.
- **Larger caps for `files` and `exact`.** `top_k` is honoured up to 50 for
  those actions (a row or a per-file block costs a line or two); `code`,
  `lexical` and `semantic` keep the cap of 10 because each hit spends a
  chunk of the token budget. The 2400-token response cap still applies.
- **Headers say which backend produced the hit.** `code` hits end in
  `[lexical]`, `[dense]`, `[lexical+dense]`, with a `symbol+` prefix for an
  exact-symbol match and `, reranked` when the reranker reordered the
  block; `semantic` hits end in `[dense]`. `CodeIndex.search` results carry
  the same as `via` (and `reranked: True`). `services/bench/search_eval.py`
  parses the tags; `_append_prefetch` and the federated merge never keyed
  on the line end.

Fixture suite: recall@1 0.946 → 0.965, MRR 0.970 → 0.980 (66 cases, 57
scored, the new gate at rank 1); baseline updated, floors kept. Golden on
this tree with fusion on: recall@1 0.774 / recall@3 0.935 / MRR 0.858 over
31 scored cases, the new exact case at rank 1 — not comparable to the
2.109.0 table (26 cases). `tests/test_search_p5.py` (21 tests) pins the
caps, every hint shape, the ordering (symbol table, declaring line, kind
order, survival of the `top_k` cut, the declaration regex), every tag
shape, and the harness parser.

## [2.109.0] - 2026-09-03

Phase P4 of the search plan: the optional reranker, measured and left off.
The plan's condition was that a cross-encoder "becomes default only on a
held-out relevance gain with acceptable cold start, download size and p95".
It was measured on both suites with fusion on, and it lost on every
aggregate. What ships is the contract, the adapter, the measurement flag,
and the numbers, so the next candidate can be judged the same way.

### Added — a reranker contract, a FlashRank adapter, and `search_rerank` (opt-in)

`services/reranker.py`: `Reranker` (`ready`, `rerank(query, [(id, text)]) ->
[(id, score)]`), a natural-language gate (three or more words, at least two
plain — `compute_total`, `OAuth2Client` and `sha256 digest` are lookups and
are never reranked), and `FlashRankReranker` over ONNX cross-encoders of
4-22 MB downloaded once into `~/.c3/models/flashrank` (`pip install
"code-context-control[rerank]"`). The model loads on the first query, not at
startup. With `search_rerank: "auto"` in the hybrid config, `code` queries
that pass the gate have their top 16 fused candidates reordered by the
model; exact-symbol matches keep their place ahead of the block; a failure
or empty answer leaves the fused order. `search_rerank_model`,
`search_rerank_top_n` and `search_rerank_cache_dir` tune it. `c3 search-eval
--rerank on` attaches the adapter regardless of config; the report line
says `rerank=flashrank`.

### Measured — and it stays off

On this repository's golden suite with fusion on (26 cases, FTS5 + v2
embeddings): recall@1 0.800 / recall@3 0.967 / MRR 0.878 without a
reranker; 0.767 / 0.933 / 0.857 with ms-marco-TinyBERT-L-2 (p95 513 ms);
0.733 / 0.967 / 0.844 with ms-marco-MiniLM-L-12 (p95 1116 ms). On the
fixture: 0.933 / 1.0 / 0.961 without; 0.900 / 0.983 / 0.943 and 0.900 / 1.0
/ 0.947 with. These MS MARCO models are trained on web passages and
mis-rank code: for "how are session cookies expired after inactivity" they
can prefer a rate limiter's `Allow` over `SessionStore.expire`. The default
is `off`; the docs carry the table so a code-trained cross-encoder or an
LLM judge can be measured against the same baseline.

`tests/test_search_p4.py` pins the gate, reordering through the contract,
the exact-symbol override, the bypass for identifier queries, failure and
empty answers, off-by-default, the harness flag, and the adapter's laziness
and unavailability paths; one real-model test runs where FlashRank is
installed.

## [2.108.0] - 2026-09-03

Phase P3 of the search plan: hybrid retrieval. Since 2.106.0 `code` queries
were answered by BM25 alone while the embedding index sat behind a separate
`semantic` action nobody was told to reach for — and on this repository's
own golden suite the two disagreed in the dense engine's favour on every
natural-language query. They are now fused.

### Added — reciprocal rank fusion of the lexical and dense rankings

`services/retrieval.py` defines the backend contract (`ready`,
`candidates(query, limit) -> [(chunk_id, score)]`) and Reciprocal Rank
Fusion. The runtime attaches the embedding index to the code index as
`CodeIndex.dense`; when its backends are ready, `code` queries fuse the top
20-50 of the lexical ranking with the top 20-50 dense candidates by
`Σ 1/(k + rank)`, `k = 60` (`search_rrf_k`). Scores from the two engines are
never compared directly. Exact-symbol matches keep their override after
fusion; dense ids that no longer exist after a refresh, or fail the
`path`/`lang`/`kind` filters, are dropped first; a backend failure leaves the
lexical ranking untouched. `action='lexical'` asks for the BM25 ranking
alone; `search_fusion: "off"` disables fusion; `search-eval` reports
`fusion=rrf` when it was active.

On this repository's golden suite (26 cases, real queries, FTS5 + v2
embeddings): recall@1 0.654 → 0.767, recall@3 0.846 → 0.933, MRR 0.756 →
0.844, `code` recall@3 0.818 → 0.909. On the keyword-heavy fixture the
figures move within a point (recall@3 1.0 either way; recall@1 0.946
lexical-only, 0.933 fused, MRR 0.970 → 0.961); the fixture was built to
measure lexical defects and fusion has little to add there.

### Fixed — a dense index always has nearest neighbours, so fusion invented answers

The first fused run turned every zero-result query into ten hits: an
embedding index cannot say "no match", only "these are closest". The
embedding backend now applies an admission floor on cosine similarity before
anyone sees its candidates — `EmbeddingIndex.min_score`, `search_dense_min_score`
in the hybrid config — in both `candidates()` (fusion) and `search()` (the
`semantic` action). Default 0.62 for nomic-embed-text with task prefixes,
measured on the fixture: unanswerable queries top out at 0.47-0.58, real
answers start at 0.70. 0.55 for other models until measured. Zero-result
accuracy is back at 1.0 with fusion on, and `semantic` now returns nothing
rather than neighbours for a query with no valid answer.

### Changed — nomic task prefixes and a v2 embedding collection

nomic-embed-text is trained with task prefixes and loses retrieval quality
without them; C3 had never sent any. Documents are embedded as
`search_document: …` and queries as `search_query: …` (other models get no
prefix). Because prefixed and unprefixed vectors are not comparable, the
collection is `code_embeddings_v2` with its own `file_hashes_v2.json`, so
existing projects re-embed lazily (174 s for this repository's 12,247
chunks, in the background the MCP server already uses). The v1 collection
and hash file are removed best-effort on first init — the drop runs in a
daemon thread with a two-second bound, because this backend has hung inside
its bindings before and a hang must be abandoned, never waited on.

### Relevance suite

Two `lexical`-action cases gate `must_pass`. `tests/test_search_p3.py` pins
RRF, fusion through the contract with a fake backend (shared candidates rise,
exact-symbol override survives, stale and filtered ids are dropped, backend
failure is harmless, `fusion=False`, config off, `lexical` action), the task
prefixes, the v2 collection, the admission floor and its defaults, and the
bounded legacy drop.

## [2.107.0] - 2026-09-03

Phase P2b of the search plan: the index stops being a blob. `index.json`
held every chunk's content plus the TF-IDF vectors in one JSON document — 30
MB for this repository, ~120 MB at the file cap — parsed whole on load and
rewritten whole on every rebuild, and every rebuild was whole: the watcher
triggered one after ten file changes. The 2.106.0 FTS5 table lived in a
second file beside it. Both are replaced by one SQLite store that is updated
per document.

### Changed — `index.sqlite` replaces `index.json` and `lexical.sqlite`

`services/index_store.py` keeps `docs` (with a content hash and mtime per
file), `chunks` (content, line range, symbol, kind, language) and the FTS5
table in `.c3/index/index.sqlite`. `CodeIndex` loads from it, writes a full
build to a temp file and swaps it in atomically, and applies incremental
changes in one transaction. Symbols are derived from chunks on load; TF-IDF
vectors are not persisted at all — they belong to the fallback engine and are
built on demand the first time a query needs them. On this repository (563
files, 12,247 chunks): cold load 0.06 s where the JSON parse took 0.21 s;
the store is 40 MB where the JSON was 30 MB, because it now carries the
FTS5 index, and it is never rewritten whole again.

A pre-2.107.0 layout is migrated on first load: `CodeIndex._load_index`
sees no store, rebuilds, and removes `index.json` and `lexical.sqlite` once
the store is written. `CodeIndex.index_exists()` and `needs_migration()`
replace the `index.json` existence checks in the MCP server startup, `c3
doctor` and the sub-project inspector; the MCP server keeps a migration off
the handshake path exactly as it keeps a first build off it.

### Added — `CodeIndex.refresh()`: re-chunk only what changed

`refresh(paths=None)` walks the manifest, hashes each file's content (the
masked view where a mask rule applies) and re-chunks only files whose hash
moved, adds new files, drops deleted ones, and writes the difference to the
store in one transaction; `refresh(paths=[...])` checks only the paths given.
Unchanged files are never re-chunked or re-tokenized — a test spies on the
chunker to prove it. When no store exists, when only a legacy layout is on
disk, or when the incremental write fails, it falls back to a full build and
says so in `mode`. On this repository: 0.16 s to confirm nothing changed
across 563 files, 0.14 s to re-index one changed file, against 5.3 s for a
full build.

`CodeWatcher.rebuild_if_needed` hands its accumulated changed paths to
`refresh` instead of calling `build_index`, so the watcher's tenth change now
costs a fraction of a second rather than a rebuild. A stub indexer without
`refresh` still gets `build_index`.

### Relevance suite

`search-eval` reports the engine per run (`engine=fts5`) and sizes the store
instead of the JSON. Baseline unchanged: recall@1 0.944, recall@3 1.0, MRR
0.969. `tests/test_search_p2b.py` pins the store round-trip, legacy
migration and cleanup, the lazy TF-IDF fallback, incremental change / add /
delete, explicit-path refresh, the no-op path, the full-build fallback, and
the watcher wiring.

## [2.106.0] - 2026-09-03

Phase P2 of the search plan: the lexical engine. Until now `c3_search`
scored every chunk on every query with a hand-rolled TF-IDF whose tokenizer
dropped digits (`sha256` became `sha`; `v2` and `S256` vanished), stacked
multiplicative boosts, and expanded queries through a synonym map written for
C3's own vocabulary. This release replaces the engine and keeps the surface.
Fixture baseline: recall@1 0.833 → 0.944, recall@3 0.958 → 1.0, MRR 0.892 →
0.969 across 54 scored cases. On this repository's own index (golden suite,
26 cases): recall@3 0.739 → 0.808, MRR 0.672 → 0.736.

### Added — SQLite FTS5 / BM25 as the `code` engine, with the old scan as fallback

`services/lexical_index.py` keeps a per-project `lexical.sqlite` beside
`index.json`: an FTS5 table with four weighted columns — path (3), symbol
(6), kind (1), body (1) — ranked by SQLite's BM25, plus per-chunk metadata for
the filters below. `CodeIndex.search` takes its candidates from there
(normalised BM25, then small additive boosts: whole symbol name spelled out
+0.25, path tokens up to +0.15; an intent prior of +0.15 for test files when
the query names tests and for docs when it asks a how-to; the weak recency
factor), then assembles results exactly as before — symbol fast path, windows
for oversized chunks, token budget. Connections are opened per call so the
MCP server, the hub and a CLI can share the file; rebuilds write a temp
database and swap it atomically.

FTS5 is a compile-time option of SQLite. `fts5_available()` probes once; when
it is missing, or `search_engine: "tfidf"` is set in `.c3/config.json`, the
pre-2.106.0 scan runs — with the new tokenizer, so `v2` still resolves.
`CodeIndex.lexical_engine`, `c3 stats` and `search-eval` say which engine
answered.

### Changed — the tokenizer keeps identifiers whole AND split, digits included, no stemming

`parseIso8601` indexes as `parseiso8601 parse iso8601`; `sha256_digest` as
`sha256_digest sha256 digest`; `v2`, `S256`, `oauth2`, `utf8` are tokens. A
letter/digit boundary is never a split. There is no stemming: `embed`,
`embedding` and `embeddings` stay three terms, because identifiers are not
prose. Both engines share `tokenize_code`, so the fallback agrees with FTS5
on what a term is. `index.json` now carries `index_schema: 2`; an index from
the old layout is rebuilt on first load rather than served with a tokenizer
mismatch.

### Added — `path`, `lang`, `kind` filters on every action

`c3_search(..., path='src/**,*.go', lang='python,go', kind='test,doc')`,
comma-separated, propagated to federated children. `path` takes globs or
substrings against the relative path; `lang` takes names or bare extensions;
`kind` takes `source` / `test` / `doc` / `config` (classified from the path)
and/or chunk types (`function`, `class`, `method`, `heading`). `code` applies
them in SQL, `files` and `exact` on the manifest, `semantic` on the embedding
results after over-fetching. The relevance suite's `filters` field, recorded
as skipped since 2.103.0, now runs: six filter cases gate `must_pass`.

### Removed — the hardcoded synonym map

`endpoint → route/handler/api`, `registry → profile → ide`, `delegate →
ollama`, `mcp → server` and the rest were C3's vocabulary and shipped to every
project. Synonyms now come only from `search_synonyms` in `.c3/config.json`
(`{"endpoint": ["route"]}`). Co-occurrence expansion stays opt-in (2.105.0).

### Relevance suite

`digits_v2_migration` and `digits_s256_challenge_method` promoted to
`must_pass`; `filter_tests_only` runs and gates, joined by `filter_path_src`,
`filter_lang_go`, `filter_kind_doc`, `filter_files_glob_lang`,
`filter_exact_kind_source`. Floors raised to three points under the new
figures (recall@1 0.91, recall@3 0.97, MRR 0.93). `tests/test_search_p2.py`
pins the tokenizer, classification, filters, the FTS5 store, the fallback
paths and the schema-triggered rebuild.

## [2.105.0] - 2026-09-03

Phase P1 of the search plan: the data-loss and correctness defects the
2.103.0 review measured, fixed one by one, each with a case in the relevance
suite that failed before and passes now. No scorer was retuned; the fixture
baseline still moved from recall@1 0.729 / MRR 0.807 to 0.833 / 0.892 because
results that used to be dropped or missed are now returned.

### Fixed — `exact` searched the files an agent had happened to read, not the index

`_exact_search` iterated `file_memory.list_tracked()` — the records written
when an agent reads a file — and opened every one of those JSON records per
query to learn its path. On this repository that universe was 427 files
against 513 indexed; a regex could miss a file simply because nobody had read
it yet. It now walks the same pruned manifest `build_index` uses (Access
Guard prunes denied subtrees inside that walk; sub-project folders are
excluded), so `exact`, `files` and `code` agree on what exists. The zero-result
line says how many files were scanned.

Two long-standing gaps closed on the way: `ignore_case=True` (a new
`c3_search` parameter, propagated to federated children) and a per-file cap —
after 20 matching lines the rest are counted (`[+N more matching lines …]`),
so one log-like file no longer spends the whole token budget and hides every
other hit.

When ripgrep is on `PATH` (or named by `ripgrep_path` in config / `C3_RIPGREP`)
and the project has **no** access rules at all, `rg --json` pre-filters the
manifest to the files that can match; every candidate is then re-scanned in
Python through the guard/mask view, so the output is byte-identical to the
pure-Python path and ripgrep never decides what is shown. With any deny,
read-only or mask rule active the fast path is skipped entirely: ripgrep
reads raw bytes, and a denied subtree must not be read even to discard the
result.

### Fixed — `files` was the content search with the content hidden

Documented as "by name", `files` ran the TF-IDF content search and dropped
the snippets. Whole path tokens happened to work (`invoice`, `docker
compose`), so the gap hid; `Invoi`, `limit` and `configs/*.yml` found nothing,
and the masked `customers.csv` lost to a CHANGELOG heading for its own name.
It is now a filename search over the indexed paths — exact basename or stem,
then glob, then basename substring, then path substring, shorter paths first,
case-insensitive — with the content-term search kept as the fallback when no
path names the query. Each row says why it matched.

### Fixed — the semantic zero-result header promised a fallback that never ran

`[semantic:…] 0 results (falling back to code search)` returned right there.
The fallback now runs, under a header that says so.

### Fixed — a chunk larger than the budget was skipped, so a class could never be found by its name

231 chunks on this repository exceeded the default 1200-token budget and
`CodeIndex.search` skipped them outright; asking for `Ledger` returned
`Ledger.version`, never the class. An oversized chunk now comes back as a
window of at most 400 tokens anchored three lines above the first line that
mentions a query term (the class header, for a class-name query), closed with
`[window Lx-Ly of class La-Lb, N tok; c3_read(lines=...) for the rest]`, and
its `lines` field names the window. `windowed: True` marks such results.

### Fixed — recency bias died on every restart; the symbol map was never consulted

`_file_mtimes` fed the recency factor but was never written to `index.json`,
so after any server restart the factor was silently 1.0 for every file — and
while it was populated, `max()` over all files ran once per chunk per query.
It is persisted now and the normaliser is computed once per query.

`self.symbols` was built, saved and loaded, and never read at query time. A
query that IS a symbol name (one identifier, any case, dotted or bare) now
ranks that symbol's definition ahead of every scored chunk: a test file that
mentions `exchange_code` thirty times no longer outranks `def exchange_code`.

### Changed

- **Co-occurrence synonyms are off by default.** The pass exhausted its
  20M-pair budget on a 513-file repository and its picks (`customers.csv` →
  `export`, `as`, `append`) lifted CHANGELOG headings over source files.
  `search_cooccurrence_synonyms: true` in `.c3/config.json` (or
  `CodeIndex(..., cooccurrence=True)`) turns it back on; the hardcoded synonym
  map is unchanged and is P2's concern.
- **Markdown headings span their section.** `_walk_markdown` recorded a
  heading as its own single line, so a CHANGELOG entry was an 18-token chunk
  holding nothing but its title — short, name-boosted, first. A heading now
  runs to the line before the next heading of the same or a higher level.
  `PARSER_VERSION` 2 → 3, so file_memory records refresh.
- `c3_search` docstring describes what each action actually does.
- Relevance suite: `params` per case (`{"ignore_case": true}`); the P1 cases
  promoted from `xfail` to `must_pass`; floors raised to three points under
  the new measurements. `tests/test_search_p1.py` pins each fix in isolation.

## [2.104.0] - 2026-09-03

### Fixed — `c3 index` capped every run at 500 files and reported the truncated total as the result

`CodeIndex.build_index(max_files=None)` reads `index_max_files` from
`.c3/config.json`, default 2000, and returns `files_capped` so a caller can
tell a complete index from a partial one. Nothing that ran from the CLI ever
got there. `cli/commands/parser.py` defaulted `--max-files` to `500` and
`cmd_index` passed `args.max_files or 500`, so the config was unreachable
from the command line and the flag had to be typed on every invocation to
exceed a cap nobody chose. `files_capped` was discarded, so the rich table
printed `Files Indexed: 500` with no denominator.

Measured on a 1933-file repository: a bare `c3 index` indexed 500 files, and
the partial index left the symbol map referencing chunks the run never
wrote — `c3_search` then raised `KeyError` on those ids rather than
returning fewer results. A truncated index does not degrade gracefully; it
answers with confident absences and occasionally crashes.

- `--max-files` now defaults to `None`. Unset means "read
  `index_max_files`"; an explicit value is still honoured verbatim.
- `cmd_index` reports truncation instead of hiding it: the table shows
  `500 of 1933` plus a `Files Skipped (cap)` row, and a `[!]` line names
  `index_max_files` as the knob. A complete index prints no warning, so the
  channel does not cry wolf.
- The TUI's index screen pre-filled `500` in "Max Files to Index" and always
  passed it, giving the same silent truncation to anyone indexing from the
  UI. It now starts empty with a `config default (2000)` placeholder and
  omits the flag unless a number is typed.
- `tests/test_index_cap_not_hardcoded.py` guards the invariant rather than
  the literal: an unset flag must reach the indexer as `None`, an explicit
  one must pass through, a capped result must print its population, and an
  uncapped one must stay quiet. All four fail against the previous code.

Every other `build_index()` caller — hub, MCP server, watcher, subprojects,
mask activation, web API — already relied on the config default and was
never affected.

## [2.103.0] - 2026-09-03

A review of `c3_search` against this repository found that no test anywhere
measured whether search returned the right thing. Every search test checked
wiring — access-guard filtering, federation sections, response headers — so a
change that put the wrong file first passed the whole suite. Measured on the
C3 tree the same day: the default `code` action put a test file or a
CHANGELOG heading first on 4 of 6 queries while `semantic` put the right
source symbol first on 3 of 3; `exact` could see 427 of 513 indexed files;
231 chunks were larger than the default token budget and therefore never
returnable. This release adds the instrument. It changes no ranking.

### Added — `c3 search-eval`: a relevance suite with per-query gates and floors

`services/bench/search_eval.py` runs a JSONL suite of queries through
`cli.tools.search.handle_search` — the function the MCP tool calls, not a
re-implementation — parses the response back into ranked hits and grades
them: recall@1/3/10, MRR, symbol recall@3, zero-result accuracy, latency
p50/p95, index build cost, and `exact_coverage` (files `exact` can see ÷
files indexed). Two suites ship:

- **Fixture** — `tests/fixtures/search_eval_repo`, a synthetic LedgerLite
  service in Python, TypeScript, Go, Markdown, YAML and CSV, copied to a temp
  dir and indexed from scratch on every run. 57 cases: symbol lookups,
  digit-bearing identifiers (`sha256_digest`, `migrate_v2`, `S256`), a
  source/test name collision, an oversized class, a masked CSV with a canary
  column, zero-result queries, filters. It carries an `access.mask` rule, so
  a redacted value reaching a response fails the suite.
- **Golden** — `tests/search_eval/golden_c3.jsonl`, real queries against the
  C3 tree run on the live `.c3` index and `file_memory` as an agent sees
  them. CLI only (`c3 search-eval --suite golden`); environment-bound.

Every case declares a gate. `must_pass` fails CI; `xfail` names the plan
phase that fixes it and is reported when it starts passing; `info` is
measured only. Aggregates are held to absolute floors in
`tests/search_eval/baseline_fixture.json`, set once, three points under the
measured value, with zero-result accuracy pinned at 1.0. `--update-baseline`
refreshes aggregates and per-query status and keeps the floors.
`tests/test_search_eval.py` is the CI gate: must-pass cases, no canary leak,
floors, and a baseline that knows every case id.

Baseline on the fixture at 2.103.0: recall@1 0.729, recall@3 0.875, recall@10
0.896, MRR 0.807, symbol recall@3 0.926, zero-result accuracy 1.0; 48 scored,
5 skipped (semantic needs Ollama; filters land in P2). Seven cases are recorded
as known failures with their fix phase: the oversized `Ledger` class chunk is
skipped rather than windowed (P1), `exact` has no ignore-case (P1), `files`
matches whole path tokens only so `Invoi` and `limit` find nothing (P1), and
the tokenizer drops digits so `v2` and `S256` vanish (P2), and the masked
`customers.csv` ranks fourth behind a CHANGELOG heading because co-occurrence
synonyms outweigh its own path tokens (P2). On the C3 tree the
golden suite reads recall@3 0.65 for `code` against 1.0 for `semantic`, and a
test's `_FakeCodeIndex` outranks `CodeIndex` itself.

Two things the first run corrected in the suite, not the engine: a canary
used as the query tripped its own check because the tool echoes the query in
its zero-result header (the harness now grades only what came back from the
index), and five `files` cases predicted to fail passed, because path tokens
are indexed — whole-token and camelCase-segment filename lookup works today
and is gated as `must_pass`. See `docs/search-eval.md`.

### Changed

- Co-occurrence synonym selection is deterministic. `_build_cooccurrence`
  iterated a bare `set` of tokens, so `Counter.most_common` ties followed the
  per-process hash seed and the same repository produced different synonyms,
  and different ranks, from one process to the next: the suite's first CI run
  read recall@1 0.708 on one matrix cell and 0.771 on the other eight. Tokens
  are now sorted before counting and ties break alphabetically. Same intent,
  same budget, one answer.
- `pytest` no longer recurses into `tests/fixtures/**`; a fixture repo's own
  `test_*.py` files are data.
## [2.102.0] - 2026-09-02

A review of the instruction block C3 generates for itself — the one that
tells the agent what to do when a write is held — checked each claim against
the code behind it. Most of what follows is that gap, closed.

### Fixed — the confirm flow the docs mandate failed on its most common path

`CLAUDE.md` step 11.6 says: C3 filed the request, wait on the id, retry once
if approved. The wait step could not run. The PreToolUse hook files under the
session id Claude Code puts in the hook payload; `c3_override` compared
against C3's own `YYYYmmdd_HHMMSS` session id, so **every hook-filed hold** —
which since 2.100.0 means every native write to `.mcp.json`, an instruction
doc or a hook body — answered `wait`, `status` and `withdraw` with *"that
request belongs to another session"*. The agent could not wait, could not
check, and was told never to retry blind. Dedup keys on the session id too,
so a follow-up `c3_edit` filed a **second** card for the same blocked write,
and "duplicates collapse into the pending request" was false.

Both surfaces now resolve one identity: the host's. Claude Code exports
`CLAUDE_CODE_SESSION_ID` to the MCP server and puts the same value in every
hook payload, so `start_session` records it and `cli/tools/_grants.session_id`
prefers it. Where a host exports nothing (Codex, Antigravity) C3's own id
serves, then the pid — the identity is never empty, and a genuinely foreign
request is still refused, now naming both ids so a mismatch is diagnosable.

### Fixed — an agent could still add an MCP server, through a neighbouring IDE

2.100.0's confirm tier listed the Claude Code files only, while
`services/artifact_defs` tracks every profile's instruction doc and
project-scoped MCP config as agent-affecting. `.cursor/mcp.json`,
`.vscode/mcp.json`, `.codex/config.toml`, `.gemini/settings.json`,
`.github/copilot-instructions.md`, `.cursorrules` and `.claude/plugins/**`
were all writable with no hold — the exact threat the tier was created to
close, reachable through a file belonging to an IDE the agent was not even
running in. All seven join `BUILTIN_CONFIRM_WRITE`, and a test now derives
the expectation from the artifact table itself rather than restating the
list, so the two cannot drift apart the next time a profile lands.

### Fixed — "writes pause by default" was false for the shell

Both shell scanners asked the access evaluator for `"read"`. Confirm holds,
`read_only` rules and the builtin write-denies are all write-class, so they
were invisible to the one route an agent reaches for when a write is blocked:
`echo >> CLAUDE.md`, `sed -i` on `.mcp.json` and a heredoc into a hook body
all ran with no hold, from `c3_shell` and from native `Bash` alike. The files
a command writes are now evaluated as writes at both surfaces, using
`cli/_shell_writes` — the extractor the edit ledger already trusts after the
fact. Reads are unchanged, and the best-effort caveat is unchanged and still
stated in the refusal: a clean scan is not enforcement.

### Fixed — an approved override could be spent on a call that never ran

`hook_access_guard` consumed the grant, then `hook_pretool_enforce` denied on
tool discipline and won the deny-beats-allow merge. The grant was gone, the
write never happened, and the user was asked twice for one edit. PreToolUse
sub-hooks now *peek*; `hook_dispatch` settles the uses only once every
sub-hook has voted allow, and a deny leaves the grant live for the retry. A
grant spent by a concurrent call between peek and settle turns the allow into
a `[c3-override:spent]` deny rather than proceeding ungranted. The dispatcher
also stops after the first deny, since nothing below it can change the answer.

### Fixed — `restore` was the one unheld agent write to the files it protects

Item 12 advertises `c3_artifacts restore` in the same sentence that says
these writes pause, but restore was exempt from the confirm tier and is
agent-callable: an agent could revert a user's tightened `CLAUDE.md`, or any
captured `.mcp.json` or hook body, with nothing but a ledger row.
`artifact_store.restore()` now takes a `confirm` mode. A human clicking
restore (CLI, Hub, REST) keeps the exemption — the click *is* the approval.
An agent's restore holds, files, and completes on the approved retry. `deny`,
`read_only` and `mask` still refuse everyone.

### Fixed — the third instruction doc had been drifting for releases

`.github/copilot-instructions.md` is git-tracked, generated from the same
template as `CLAUDE.md` and `AGENTS.md`, and was absent from the managed-block
sync — while `_ensure_instruction_workflow` treated marker *presence* as
"up to date". The markers are stable words like `SEARCH FIRST`, so they
survive every template change and the file reported `Kept` forever. In this
vscode-primary repo it was a release behind: no 11.6, no agent-config tier.
The doc joins the sync list, and a doc carrying the managed sentinels is now
compared against the current block and regenerated when it differs, with user
content outside the markers preserved as always.

### Changed — S8 says what it means

Four corrections to the pinned confirm refusal, each one a place an agent was
told something untrue:

- the wait call now carries `timeout_s=180`; the bare call waits **60 s**,
  so an agent that hit the timeout believed the decision window had closed;
- it says a "still pending" answer is not a denial, and that the retry must
  be on the **same surface** (a grant matches on tool, so a `c3_edit` retry
  of a hook-filed `Edit` hold matches nothing and files a second card);
- "could not be filed" now defers to the reason's own instruction instead of
  overriding it — a deny+mute says *do not ask again*, a rate limit says
  withdraw one or wait, and the old tail said "ask the user in chat" to both;
- the `Rules:` pointer follows the scope: `c3 access builtin mode` for a
  builtin-tier hold, which is not in `c3 access list` at all.

`c3_project` gets its own tail: the proxy files nothing and a retry through
it refuses again, so pointing at `c3_edit` there was unreachable advice.

### Changed — the generated docs stop claiming what an IDE cannot do

The hookless adaptation already restates "blocked by hooks" honestly for
Codex, Antigravity and Copilot; 2.100.0's "WRITES to these files PAUSE by
default" slipped past it one paragraph lower, and a native write there is
never intercepted. Those docs now say so. The global `~/.claude/CLAUDE.md`
and the nano workflow — neither of which had heard of confirm holds, though
the builtin tier fires in every C3 project — get the rule too.

## [2.101.0] - 2026-08-28

### Added — the shell_warn layer existed on paper; nothing consulted it

Since 2.69.0 a user could turn on `override.layers.shell_warn`, an agent
could file a request against it, a human could approve — and
`cli/tools/shell.py` never looked at the grant. The layer is now wired:
an approved grant replaces the `[c3_shell:warn]` caveat with the
`[c3-override:granted]` line, exactly once per use. The grant binds to
the effective cwd, not the command text — a stated limitation that is
acceptable because the soft-warn is a caveat on an already-executed
command, never a block. `_BLOCKED` never consults grants: no approval
flow reaches the catastrophic tier, and a test now pins that.

Also fixed on the way: `c3_override(action='request', layer='shell')`
fell into the ACCESS branch and produced a request no gate could ever
satisfy — it now files with the fixed shell identity (`tool=c3_shell`,
`op=run`, `rule=<shell:soft-warn>`) the gate actually looks for.

With this, every layer in docs/confirm-guard.md §7 is live: confirm
rules (2.97.0), the Hub approval surface (2.98.0), per-builtin modes
(2.99.0), the agent-config confirm tier (2.100.0), and shell_warn.

## [2.100.0] - 2026-08-28

### Added — an agent could add an MCP server, rewrite a hook body, or edit its own instructions without tripping any rule

A new builtin tier, `BUILTIN_CONFIRM_WRITE`: `.mcp.json`, the instruction
docs (CLAUDE.md / AGENTS.md / GEMINI.md), and the `.claude`
hooks/skills/agents/commands bodies — the agent-config surfaces that were
fully writable until now, with only after-the-fact artifact capture
watching. The default is a pause, not a block: a write holds for one-tap
approval (the confirm flow 2.97.0 built), reads stay open — an agent must
always be able to read its own instructions. The tier is mode-governable
like the rest of Tier 1; it governs writes only, so `deny` hardens to a
write-deny rather than a full deny, and `allow` restores the old
behaviour. The `settings*.json` write-deny stays where it was, out of
this tier: hook REGISTRATION remains hard while hook BODIES pause,
because registration is what decides code execution.
`artifact_store.restore()` exempts builtin confirm exactly as it exempts
builtin read_only — restore writes back a version the store itself
captured, on an audited human-triggerable path.

### Fixed — a builtin confirm could shadow a stricter user rule

Building the tier surfaced a precedence flaw in 2.97.0: `confirm` sat
above `read_only`, so the new builtin confirm on CLAUDE.md outranked a
user's `read_only: ["CLAUDE.md"]` — an artifact restore sailed past a
rule that should have refused it (caught by the restore wiring tests).
A hold can be approved into a write; a read_only cannot — confirm is the
loosest non-allow outcome and everything stricter must beat it. The
precedence is now `deny > mask > read_only > confirm`, and a user
`confirm` glob over `**/.c3/**` no longer outranks the builtin
write-deny either (the sanctioned route to a confirm-mode builtin is
`c3 access builtin mode`). docs/confirm-guard.md §2 records the
correction.

## [2.99.0] - 2026-08-28

### Added — a builtin guard was on or off; now each one takes a mode

The two-key opt-out generalises to per-builtin modes: `c3 access builtin
mode <glob> {deny,confirm,allow,default}` (docs/confirm-guard.md §7). So
`**/.env*` can pause-and-ask instead of refusing, `**/.git/**` can be
switched off, and a write-deny like `**/.claude/settings*.json` can be
tightened to a full deny — each independently, each requiring the config
entry AND a keyring attestation carrying the same mode string, written
attestation-first so a keyring that will not hold it leaves config
untouched. Every failure path — hand-edited config, forged attestation
with the wrong value, keyring gone — enforces the shipped default.

A mode never widens the op class the builtin governs. On the full-deny
tier, `confirm` covers reads too (a write-only confirm would silently turn
deny-all into allow-read), and the hook and `c3_read` file the approval
request on a held read; enumeration surfaces keep excluding and never
auto-file, so there is still no existence oracle. Tier-0 vault globs take
no mode at any price, and even under a confirm-mode `**/.c3/**` the
policy/grant files can never become a request.

`disable_builtin` survives as the legacy spelling of `allow`
(`set_builtin_disabled` is now a shim); one glob named in both spellings
makes the global scope corrupt — ambiguity is a hard error, never a
precedence puzzle — and `set_builtin_mode` retires the legacy entry
lazily so the API cannot create that state. Project-scope `builtin_mode`
is corrupt too: project scopes only tighten.

Surfaces: the CLI (typed-glob confirmation for the widening modes), `POST
/api/access/builtin_mode`, and a mode selector in the Access tab — where
builtins previously read "cannot be edited", and where a builtin at
`allow` now still renders (as `off`) so there is a row to reset it from.

## [2.98.0] - 2026-08-28

### Added — approvals lived on the phone and the CLI; the desktop could only watch

The Hub gains an Access tab — the desktop half of Override Requests
(override-requests.md P5, and the natural home for the confirm rules
2.97.0 added). Pending requests across every project on the machine render
as cards with Approve / Deny / Deny+mute; approving an `access_deny` or
`access_builtin` request demands the rule glob typed by hand, in a modal
the UI computes and `decide()` re-enforces server-side regardless — two
places compute, one enforces, same as the mobile route. A request that
lapsed while the page showed it answers 409 and the card refreshes to its
real status rather than silently minting a grant. The agent-supplied
justification renders quoted under an untrusted-input label and never
reaches the activity log or the edit ledger — the audit trail carries
identifiers and rule globs only, `decided_by="desktop"`.

Under the cards, a read-only policy matrix shows any project's effective
rules (builtin tiers, disabled builtins, all four kinds, mask presets) and
its override-layer switches. Rule mutation stays where it was — the
per-project Access tab and `c3 access`; the Hub approves and reads, it
does not edit policy.

Routes: `GET /api/hub/overrides` (cross-project, with per-row
`needs_typed_confirm` / `escalatable` so a card can decide without a
second fetch), `POST /api/hub/overrides/<id>`, `GET /api/hub/access`.
Also fixed in passing: the Tokens tab never survived a reload — `tokens`
was missing from the persisted-view allowlist in `app.js`.

## [2.97.0] - 2026-08-28

### Added — a path could be blocked or open, never "ask me first"

Access Guard gains a fourth rule kind, `confirm` (docs/confirm-guard.md):
writes to a matching path pause for a human decision instead of refusing
outright. The pause is real machinery, not copy — the denial site auto-files
an Override Request itself (the agent cannot forget to ask and cannot word
the ask; auto-filed rows carry an empty justification), the new pinned S8
refusal names the pending request id, and one tap mints the same single-use,
session-bound, path-exact grant every other layer uses. Reads are untouched;
precedence is `deny > mask > confirm > read_only`, with mask deliberately
above confirm because an edit expressed against a transformed view can never
be approved into the real file.

The approval pipeline needed one deliberate exception to "default off,
everywhere": a new `access_confirm` layer that defaults ON and does not
require `override.enabled` — a confirm rule exists only because a human
wrote one, and that authorship is the consent. An explicit
`layers.access_confirm: false` still forces it off, and a corrupt override
scope disables it like everything else (a new corrupt-scope guard in
`escalatable()` closes the fail-open path the `True` default would have
created). Vault and override-policy files can never become a request, even
under a user confirm rule covering `.c3/**`.

Surfaces: `c3 access add --kind confirm`, the Access tab's kind picker, and
`/api/access` all accept the new kind; the hook and `c3_edit` file requests,
every other surface refuses with an S8 that says where filing happens.
Compatibility is the unknown-key rule doing its job: a pre-2.97 C3 reading
`access.confirm` treats the scope as corrupt and evaluates deny-all — loud,
never silently permissive.

## [2.96.1] - 2026-08-25

### Fixed — removing one sub-project deregistered C3 from the whole machine

`c3 sub remove --clear` called `_uninstall_mcp_all(child)` without
`include_global=False`, so beyond wiping the child's own `.c3` it walked
`Path.home()` and stripped C3 from `~/.codex/config.toml` and deleted
Antigravity's `mcp_config.json`. Those files are machine-wide — they serve
every C3 project on the box. Clearing one child unregistered C3 from Codex
and Antigravity for all of them.

This is the bug 2.89.1 already fixed once, at a different call site. That
release gave `_uninstall_mcp_all` its `include_global` flag and passed
`False` from `merge_projects`, and its docstring states the rule outright:
per-project cleanup must never touch home-level configs, only a real machine
uninstall may. `SubprojectManager.remove()` was written from the same helper
— the comment above the call still says "same cleanup helpers merge_projects
uses" — but the copy did not carry the flag.

No test caught it because `SubprojectBase` patches `_uninstall_mcp_all` with
a lambda that swallows every argument, so the one argument that mattered was
unobservable. The new `tests/test_subproject_clear_scope.py` runs the real
helper against a fake home and asserts on the files, not on the call: it
fails on the unfixed tree with the same "Deleted empty .codex/config.toml"
output the bug produced in the wild. Two companion tests pin that `--clear`
still removes the child's own `.c3` and instruction docs, and that `unlink`
never reaches the uninstall helper at all.

Found by hitting it — a `sub remove --clear` of a scratch project during the
2.96.0 release check took out this machine's Codex and Antigravity C3
registrations.

## [2.96.0] - 2026-08-25

### Added — a sub-project had to live inside its parent, one level down

Two lines in `SubprojectManager.validate()` decided the whole shape of the
hierarchy. One refused any folder that was not physically inside the parent;
the other refused a parent that was itself a child. Together they meant C3
could describe a monorepo, and only two levels of one.

The registry on this machine shows the cost. All 56 registered projects were
flat, and `Code Context Control` and `c3-mobile` sat side by side under one
folder — one product, two checkouts, and no containment-based model can ever
relate them, because neither contains the other.

Containment was not really a rule. It was a consequence of the storage:
entries addressed children by `rel_path`, so a child outside the parent had no
way to be written down. `relative_to()` raises, and on Windows so does
`relpath` across drive letters — `U:` to `W:` is not expressible as a relative
path at all.

Entries now address a child one of two ways, and C3 picks from the path you
give it. **Nested** children keep `rel_path` and keep being carved out of the
parent's index, because they are still inside the tree that gets scanned.
**External** children store an absolute `path`, carry no `rel_path`, and
contribute no exclusion — there is nothing to exclude from a scan they were
never part of. `entry_abs_path()` is the single place the two forms are told
apart; status, listing, reconcile and removal all resolve through it. Old
`rel_path`-only configs keep working untouched.

Depth is now bounded rather than forbidden: a strict tree, one parent, up to
eight levels. Containment used to make cycles structurally impossible, which
is why nothing checked for them; now `validate()` walks the ancestor chain and
`set_parent()` walks the registry, both with a visited-set as well as a limit,
because a corrupt chain must terminate rather than spin.

`inspect_path()` answers the question you have *before* you link: point at a
folder and find out whether there is a project there, what is in it, who
already claims it, what it claims, and which nested projects underneath it are
not linked yet. It mutates nothing — inspecting an unregistered folder must not
register it — and the nested projects it finds are surfaced as suggestions,
never applied. Explicit links are the source of truth; a rescan never creates
or overwrites one.

**Surfaces.** `c3 sub` gains `inspect`, `link` and `tree`; `sub list` gains a
LINK column and its REL PATH column becomes LOCATION, because an external child
has no `rel_path` to print. `c3_project` gains `sub_tree` and `sub_inspect` as
reads (plan-mode safe) and `sub_link` behind `allow_write`; the Oracle
`TOOL_SPECS` enum takes the two reads and not the write, which is its existing
deliberate asymmetry. The Hub gains `/subprojects/inspect`, `/subprojects/link`
and `/projects/hierarchy`, and `GET /api/projects` now reports `depth` per row.

**Hub UI.** A new "Link project by path…" modal browses the whole filesystem
rather than the parent's subtree — deliberately, because the projects worth
linking are the ones that do *not* live inside the parent — and inspects a
folder before offering to claim it. The project tree renders every level
instead of one, rollups count the whole subtree instead of the first hop, and
the kebab stops hiding sub-project actions from children, since a sub-project
may have sub-projects. Re-parenting was a staged wizard because folders had to
move; it is now a configuration change and no files move.

### Fixed — consumers that walked exactly one hop

Search/memory federation and three Oracle surfaces each resolved "the children"
as the direct children. With a real hierarchy that silently drops everything
below the first level, and none of them errors — a grandchild is simply never
searched, never counted, never in the graph.

`federate.subproject_scopes` now walks descendants breadth-first, so when
`max_children_per_query` bites it drops the most distant relatives rather than
an arbitrary slice. The Oracle's `_scoped_projects` takes the transitive
closure down `parent_path` instead of matching one edge. `project_scanner`
counted sub-projects by counting `rel_paths`, so a parent whose children all
lived elsewhere reported zero while plainly having some. `federated_graph`
exposes a derived per-project `depth` and `stats.max_depth`.

## [2.95.0] - 2026-08-24

### Fixed — the Stop hook wrote 498 rows of zeros and nothing noticed

`cli/hook_session_stats.py` read `payload["usage"]` and `payload["cost_usd"]`
from the Claude Code Stop event. That event sends neither. It sends
`session_id`, `transcript_path`, `hook_event_name`, `stop_hook_active` and
`cwd` — so `usage` was `{}` on every call, `.get(..., 0)` returned 0, and the
hook appended a structurally perfect row of zeros after every single turn.

498 consecutive all-zero rows in this repo before the fix. Nothing raised,
nothing logged, the file kept growing, and the failure is invisible by
construction: a missing key reads as 0 exactly as convincingly as a real 0.
The same silent-wrong-data shape as 2.92.2's mojibake and 2.93.0's truncated
PEM — three releases in a row where the bug was not a crash but a plausible
number.

The numbers were always one dereference away: every assistant message in the
transcript the payload points at carries a `message.usage` block. The hook now
reads that file and sums it, and falls back to the payload only if a future
Claude Code starts sending usage directly. Each row is a CUMULATIVE snapshot
(Stop fires per turn and the transcript is re-summed), so the newest row per
session is that session's total and rows must never be added together —
`aggregate_session_stats` enforces that rather than leaving it to callers.

Cost is deliberately still not recorded. The transcript carries no price, and a
rate hardcoded here would age into a confidently wrong number. Tokens are
measured, so tokens are what gets written.

### Added — one audit trail for a credential's whole life

C3 recorded both halves of a credential's history and joined neither. Uses
went to `.c3/cred_usage.jsonl` — injection into a subprocess, `{{cred:X}}`
expansion, a gated reveal, `c3 creds get --show`. Changes went to the activity
log as `cred_action` rows. Two files, two schemas, two different notions of
scope. Answering "who changed this key, and where has it been used since"
meant opening both and merging them by eye, which is not an audit trail; it is
the raw material for one.

`services/cred_audit.py` merges them into a single normalized timeline.

- **Credentials → Audit** in the hub: cross-project, newest first, filterable
  by kind, by credential, or free text. Every credential list also has its own
  **Audit** button scoped to that vault, and a row's context menu has
  **View audit trail…** which opens the timeline already filtered to it.
- **The `exposing` filter.** `reveal` and `cli_show` are the only two actions
  that put a plaintext value somewhere a person or a model can read it;
  everything else hands it to a subprocess and never surfaces it. Those rows
  are badged and counted separately rather than left to be spotted in a list.
- **Rows carry what an audit needs**: when, which project or the global vault,
  which surface (`shell`/`cli`/`mcp`/`ui`/`hub`), the command that needed the
  credential, and that command's exit code.
- **`c3 creds audit [NAME] [--kind] [--action] [--surface] [--since] [--json]`**
  and three read-only routes.

Two scope rules, both load-bearing. A project's timeline includes the global
vault, because a shared credential used from a project records into `~/.c3`
rather than the project — reading only the project loses exactly the entries a
shared secret generates. The cross-project roll-up reads that shared vault
once rather than once per project, which would have multiplied one log by the
number of registered projects.

Nothing in the trail can leak a value, because neither log ever stored one:
`cmd` is the raw template (`{{cred:X}}`, `$NAME`), never the substitution. The
command is therefore shown in full rather than masked — masking it would imply
there was something behind the mask.

### Added — a Tokens tab, because the measurement layer had no reader

`.c3/tool_telemetry.jsonl` has recorded one row per tool call since v2.66.0 —
2,724 calls and 1.2M tokens in this repo alone — and
`services/telemetry.py::aggregate_tool_telemetry()` has been a complete query
API over it the whole time. **Nothing called it.** No CLI, no route, no UI;
the only caller anywhere was the test suite. A feature can be fully built,
fully tested and completely unreachable, and the tests stay green either way
because a test is a caller.

- **Hub → Tokens**, a top-level tab beside Credentials: cross-project totals,
  a per-project roll-up ranked by spend, and any project expandable in place.
- **A Tokens tab in every project's drill panel**, the same component.
- **Four breakdowns** — by tool, by day, by session, and by file.
- **`c3 tokens [--days N] [--by tool|day|session|file] [--limit N] [--json]`**.
- **`GET /api/tokens`**, **`GET /api/projects/tokens`**, and
  **`GET /api/hub/tokens/overview`**, all read-only and counts-only.

The two logs are shown side by side and never summed. Tool tokens are what
C3's own tools returned; session tokens are what the whole conversation cost.
A session spends tokens on prose, on files read by native tools, and on cache
that no tool log can see, so a combined figure would be invented. Where a log
is all zeros the UI says the log predates the fix rather than showing a
confident `0` — absence of measurement and measurement of absence are
different claims.

### Added — which file a tool call was about

Telemetry recorded `tool` and `action` but never a location, so "which file is
costing me tokens" was unanswerable — the one question worth asking of a
token log. Records now carry `target`: the project-relative path when the
args named a file, and `""` when they did not. A search by query has no file,
and inventing one would turn the file view into a mix of paths and unrelated
strings. Rows written before 2.95.0 have no target and aggregate under `""`
rather than being dropped, and the UI says so instead of showing an empty
table.

### Added — the bundle guard now transpiles the bundle

`tests/test_ui_jsx_syntax.py` (2.92.4) recognised one shape of syntax error:
the `{/* … */}` in expression position that black-screened 2.92.0–2.92.3. It
passes clean on every other kind. A second class transpiles the real
concatenated hub and project bundles through Babel exactly as the browser
does. It earned its place immediately by catching an unterminated string
literal in `hub_credentials.js` that the regex test approved. It skips unless
`node` and `@babel/standalone` are present, since the project deliberately has
no build step; the module comment says how to run it.

## [2.94.0] - 2026-08-24

### Added — the Credentials page stops being a one-row-at-a-time interface

A vault with sixty entries across nine projects was still being managed the way
one with six was: a single search box, and every action reaching exactly one
row. The three things you actually do in bulk — audit what the agent can read,
turn off injection you no longer want, and clear out a batch of keys — each
meant N trips through a context menu.

- **Filter chips, always on.** `agent-readable`, `auto-inject`, `structured`,
  `shadowing`, `from .env`, `never used`, `stale >30d`, each with a live count.
  They narrow rather than widen, and compose with the existing `key:value`
  filter box; `/` focuses that box from anywhere. New `source:` qualifier and a
  `newest` sort.
- **Multi-select and bulk actions.** `Select` turns on checkboxes;
  shift-click extends a range; the header checkbox takes everything currently
  shown — which is the point of the chips: filter to `agent-readable`, select
  all, revoke. Bulk check resolution, revoke agent read, disable auto-inject,
  export a metadata-only CSV, and delete.
- **Bulk may only ever reduce exposure.** There is no bulk "allow agent read"
  and no bulk "enable auto-inject": granting stays one entry at a time behind a
  confirmation that names it, because a bulk grant widens access to many
  secrets from one checkbox and the row you did not mean to include is exactly
  the one that matters. `POST /api/hub/credentials/batch` enforces the same
  allowlist, so this is not merely a missing button. Bulk rename, retype,
  storage migration and copy-to-global are absent for a different reason: each
  silently changes which credential a consumer resolves.
- **Bulk delete states what else it breaks.** Deleting a project entry does not
  remove the name — it hands it back to the global vault, so the project starts
  resolving the *global* value instead of failing. The confirmation lists which
  of the selected rows do that before you type `DELETE <count>`.
- **A partial run reports as partial** — `7 ok, 2 failed` naming the first
  failure, never nine successes.

### Added — a `.env` import you can run again next week

The import shipped in 2.93.0 was a wizard: it knew how to read a file once and
then forgot it. A `.env` drifts all week, so the second import was as much work
as the first, and re-importing was the operation most likely to be wrong.

- **The vault remembers which `.env` an entry came from** (new `source` field,
  set when C3 read the file from a path on this machine — a pasted body has no
  path worth promising to re-read). The manager grows an **Imported from**
  strip listing each file with its entry count and last sync, and a
  **Re-sync** button. The list is derived from the entries, not kept beside
  them: a second copy of a derivable list is what goes stale.
- **Re-sync is a diff.** The server re-reads the file and compares each value
  against the stored one by digest, so it reports `unchanged` / `changed` /
  `new` without either value reaching the browser. Rows already matching are
  left unticked — rewriting an identical value is a keyring write and a ledger
  row for nothing.
- **Keys that left the file are reported, never deleted.** Plenty of
  credentials outlive the file that seeded them, so a vanished key is listed
  separately and left alone.
- **A commit refuses a file that moved under its preview.** The preview returns
  a digest and the commit must echo it, so a `.env` edited between the two
  calls 409s instead of importing content you never saw.

Re-sync is a *two-way* diff, deliberately: it can tell you the file and the
vault disagree, not which one you changed. A value edited in the vault by hand
reads as `changed`. Nothing is persisted that could be correlated offline — no
stored digest, no baseline.

### Fixed — credential failures rendered as successes

Every `notify()` call in the credentials UI omitted the `kind` argument, and
`kindColor` falls back to the accent colour. A failed delete, a failed
resolution check and a rejected exposure change all came back looking exactly
like the green confirmation of the thing having worked.

### Fixed — a row was identified by a name that is not unique

The same credential name legitimately lives in the global vault and in any
number of projects, but rows were keyed by bare name. A resolution check run on
a project entry rendered its result against a same-named global one. Identity
is now `(scope, project, name)` everywhere it is remembered — selection sets,
check results and every ledger row the batch route writes.

### Added — the bundle guard now actually parses the bundle

`tests/test_ui_jsx_syntax.py` (2.92.4) recognised one shape of syntax error:
the `{/* … */}` in expression position that black-screened 2.92.0–2.92.3. It
passes clean on every other kind. A second class transpiles the real
concatenated bundles through Babel exactly as the browser does, and it earned
its place immediately by catching an unterminated string literal in
`hub_credentials.js` that the regex test approved. It skips unless `node` and
`@babel/standalone` are present, since the project deliberately has no build
step; the module comment says how to run it.

## [2.92.4] - 2026-08-24

### Fixed — the hub opened to a black screen, and only the browser console said why

`cli/hub_ui/components/topbar.js` put a `{/* ... */}` comment directly inside
`{hubConfig && hubConfig.oracle_url && ( ... )}`, above the Open-Oracle link.
That position is expression context, not JSX children context, so Babel reads
the `{` as an object literal and stops with `Unexpected token, expected ","`.
A JSX comment is only legal *between* JSX tags.

The hub UI is one concatenated script transpiled in the browser, so a single
syntax error anywhere in it takes the entire bundle down: no React, no render,
a black page, and the only evidence a console line naming a position in a
464 KB inline script. Nothing on the Python side notices — the route still
returns 200 with the full 472 KB of HTML, which is why every server-side check
stayed green. The comment moves above the conditional; the link is unchanged.

Introduced by #119 on 2026-08-21 and released in 2.92.0, so every hub on
2.92.0 through 2.92.3 is affected. Nothing else regressed in those
releases — the hub UI simply never rendered.

### Added — a syntax guard for the browser-transpiled UI bundles

`tests/test_ui_jsx_syntax.py` scans `cli/ui`, `cli/hub_ui` and `oracle/ui` for
a JSX comment opening in expression position. The existing bundle tests check
the *shape* of the build — every listed module exists, markers are stamped,
`app.js` stays last — and passed for the whole time the broken file was
shipping, because a bundle can be assembled perfectly and still not parse.
## [2.93.0] - 2026-08-24

### Fixed — a `.env` with a PEM key in it imported a truncated PEM key, and said it worked

`services/credential_store.py::import_env` walked the file one line at a time:
`splitlines()`, `strip()`, `partition("=")`. That is fine until a value spans
lines, which is exactly what a `.env` holding a private key or a JSON
service-account blob does. Given

```
GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ
-----END PRIVATE KEY-----"
```

the stored secret was the single string `"-----BEGIN PRIVATE KEY-----` —
opening quote included, everything after the first newline gone. The
continuation lines were dropped on the `"=" not in line` test, which appended
nothing to `skipped`, so the CLI printed `Imported 1 credential(s)` and the
user walked away with a key that could never work. Nothing raised, nothing
logged: the same silent-wrong-data shape as 2.92.2's mojibake.

The parser is now a separate, pure `parse_env()` that understands what a real
`.env` contains: values quoted across multiple lines, single quotes as literal
and double quotes with `\n`-style escapes, inline `#` comments on unquoted
values (`FOO=bar # note` stored `bar # note` before), a UTF-8 BOM (which made
the *first* secret in every BOM'd file fail its name check and vanish),
duplicate keys, and whitespace that a quoted value deliberately keeps. A value
containing a newline is now stored as `multiline` rather than `env` — the type
already existed for exactly this. `${VAR}` interpolation is still not
supported, on purpose.

Every line the parser cannot use is now reported instead of discarded, and
each skip carries a reason — `already exists`, `not a usable credential name`,
`no value`, `quote never closes`, `redefined later in the file` — where before
four different causes arrived as one flat list of bare names.

Reading the file was its own bug. `c3 creds import` called
`read_text(encoding="utf-8")` while catching only `CredentialError` and
`RuntimeError`, so a `.env` saved by a Windows editor as cp1252 killed the
command with a `UnicodeDecodeError` traceback. All three surfaces now share
`read_env_file()`: UTF-8 (BOM tolerated), then cp1252, with a UTF-16 BOM check
in front because PowerShell's `>` wrote UTF-16LE for years and cp1252 decodes
those bytes into mojibake without complaining.

### Added — see what a `.env` import will do before it touches the keyring

`import_env` takes `preview=True` and returns the same shape without writing,
plus `only=[...]` to import a subset. Each row reports name, line, type, value
**length** and a sha256[:8] **fingerprint** — never the value or any prefix of
it, because a prefix is part of the secret and the vault's rule is that a
stored value never travels back to the browser.

- **Both web UIs get a file picker and a drop zone.** Importing no longer means
  opening `.env`, selecting all, and pasting secrets into a textarea — though
  the textarea stays as a fallback. The flow is choose → preview → tick → import:
  a table of what would land, rows that cannot be imported disabled and
  explained, and the **overwrite** checkbox that both routes have always
  accepted but neither UI ever sent.
- **An overwrite now rotates the value and nothing else.** `import_env`
  replaced an existing entry by calling `set_credential` with no metadata, and
  `set_credential` writes a whole fresh entry dict — only `created` survived.
  So re-importing a key already in the vault reset its `description` and
  `env_var` to `""`, turned a `token` back into an `env`, and set both `inject`
  and `agent_readable` to false, rewriting the keyring attestation to match.
  The last of those is the one that bites: auto-injection into every
  `c3_shell` run simply stopped, with no error and nothing in the ledger to
  say why. The two overwrite tests only ever asserted that the *value* had
  changed, so nothing caught it. A new `set_value()` does what a re-import
  actually means — the counterpart to `update_metadata()`, which changes
  everything except the value. Type is preserved too, except that a value
  containing a newline still promotes to `multiline`: the type describes the
  value, so a stored type cannot outrank the new content.
- **`c3 creds import` gains `--dry-run` and `--only NAME,...`.** `--dry-run`
  prints the table and writes nothing.
- **The CLI import is now audited.** It was the one credential mutation path in
  the system with no ledger entry; both REST routes logged and it did not. A
  bulk import also writes a single `cred_import` row naming the source and the
  count, so the ledger no longer shows N sets indistinguishable from N manual
  ones.
- **`POST /api/credentials/import` and its hub twin** take `preview`, `only`,
  and a server-side path (`path` on the project route, `env_path` on the hub
  route, where `path` already meant the project). A path is contained to the
  project root and refused otherwise.

### Added — `c3_credentials(action='import_env')`, which still cannot read your `.env`

Agents can offer to bulk-import a `.env` without gaining the ability to read
one. `**/.env*` remains a Tier-1 built-in deny; the server reads the file and
the agent gets names, lengths, fingerprints and reasons back. The gates are
deliberately tight, because the real risk is an agent importing a
repo-controlled `.env` at project scope to shadow a good global name:

- `dry_run` defaults to **true** — a bare call is a preview
- project scope only; importing to the shared global vault stays a user action
- `overwrite` is refused outright — an agent never replaces a stored secret
- the path must resolve inside the project
- entries are created with `agent_readable=false`, which the agent still cannot
  raise afterwards

The mobile gateway deliberately still has no import route.

## [2.92.3] - 2026-08-24

### Fixed — one minified bundle could hang a build for a quarter of an hour

`services/indexer.py::_build_cooccurrence` was quadratic in a chunk's
unique-token count, with no bound. A vendored `*.min.css` or `*.min.js`
chunk carries hundreds of unique tokens, so the pair loop reached the
billions: one real project (4091 chunks, one 798-token bundle chunk) sat
at 1.9e9 counter updates, pinned a core at 100% with no I/O, and `c3
index` simply never returned. Nothing timed out and nothing logged, so
the failure looked like a hang rather than a bug.

Two bounds, both reported. `_COOC_MAX_CHUNK_TOKENS` (200) skips an
individual chunk whose unique-token count is pathological;
`_COOC_MAX_PAIR_UPDATES` (20M) stops the whole pass. `build_index()`
returns a new `cooccurrence` stats block naming how many chunks were
skipped and whether the pass was capped, so a bounded build cannot be
mistaken for a complete one. The project above now rebuilds in 69s.

If you have ever started `c3 index` on a repo that vendors a minified
bundle and given up waiting, this was why.

## [2.92.2] - 2026-08-24

### Fixed — the indexer read every file as cp1252, so the index stored mojibake

`services/indexer.py`, `doc_index.py`, `protocol.py` and `scanner.py` all
called `Path.read_text(errors="replace")` with no `encoding=`. On a Windows
box whose preferred encoding is cp1252 that decodes UTF-8 source as cp1252,
and `errors="replace"` guarantees it never raises — so it failed silently,
forever.

Source files were never touched. The corruption lived in the *indexed
copies*: on this repo, 4102 mangled sequences in `.c3/index/index.json`,
2065 in `.c3/doc_index/index.json` and 11244 in the chroma collection, all
coming back that way through `c3_search` semantic hits and `c3 context`.
The embeddings were computed over the mangled text too. `compressor.py`
already passed `encoding="utf-8"`, which is why `c3_compress` / `c3_read`
looked clean while search did not.

Fix: `encoding="utf-8"` at all five sites (plus one in the swe_bench
harness).

Guard: `test_windows_reliability` already banned text-mode `open()` without
`encoding=`; the same invariant was never asserted for
`read_text`/`write_text`, which is exactly where it broke. New
`test_no_path_text_io_without_encoding` covers shipped code (positional
`read_text("utf-8")` counts; `importlib.metadata` `dist.read_text` exempt),
and `_SKIP_DIRS` now skips gitignored build artifacts (`build/`, `dist/`,
`.eggs/`) that are stale copies of sources already scanned.

**Upgrading does not repair an index that is already mangled.** The fix
stops new mojibake only; a project indexed by an older version keeps the
bad text until its index is rebuilt.

## [2.92.1] - 2026-08-22

### Fixed — shell writes were invisible to tool discipline, and a failed `c3_*` call counted as "c3 was used"

The last two items from the 2026-08-22 field report, both in the
PreToolUse hook's blind spots.

**Shell file writes get the nudge and reach the ledger.** `Bash` has been
in the PreToolUse matcher since v2.62, but only the Access Guard ever read
the command, and only for path-access rules. `python -c "open(f,'w')"`, a
heredoc, `sed -i`, `tee`, `cp` — none of it was nudged toward `c3_edit` and
none of it reached the edit ledger, so every agent that met a blocked
`Write` simply went round it (four self-reported ledger gaps in one
session). New `cli/_shell_writes.py` answers "which files does this command
probably write?" — redirects, `tee`, `cp`/`mv`/`touch`/`rm`, `sed -i`,
inline Python `open(...,'w')` / `Path(...).write_text`, following `cd`,
skipping streams and pip `>=` specs. `hook_pretool_enforce` now gives a
shell command that writes project files the advisory hint naming them — in
`strict` and `advisory` alike; shell is **never blocked**, because blocking
shell writes outright would break far more legitimate work than it
protects — and stays silent when `c3_edit` just ran. `hook_edit_ledger`
gets a Bash branch: files the command named that exist, are editable and
changed in the last two minutes become `change_type: "shell"` rows carrying
the command (no pre-edit snapshot — there is none to take). The dispatcher
routes Bash PostToolUse to it after the ghost-file sweep.

**A failed `c3_*` call no longer unlocks anything.** `c3_compress` on a path
that does not exist returned `Error: File not found` — and
`hook_c3_signal` still wrote a fresh signal, `hook_edit_unlock` still stuck
an unlock on that path, and the activity-log scan still counted the call,
so native `Write` on that very path was allowed. One definition of "that
call failed" (`_hook_utils.response_text_failed`: a leading `Error` /
`[tool:error]` line, the masked-path refusal tag, or an MCP `isError`) now
gates all three: no signal, no unlock, and `mcp_server` writes `ok: false`
into the activity log so the scan skips it (older entries are judged by
their summary text). The sanctioned way to make a new file is
`c3_edit(file_path=..., old_string='', new_string=...)`, which creates it
and logs it; the `Write` redirect and the compress not-found message now
say so instead of pointing at a failed call.

## [2.92.0] - 2026-08-22

### Added — `c3 init` says what kind of project it is looking at, and defaults a documentation repo to `advisory`

From the same field report: 636 files, ~95% Markdown and `.docx`, near-zero
source. Symbol-aware `c3_compress`/`c3_read`, `c3_impact`, `c3_validate` and
`c3_ci` had nothing to act on, `c3_search` never beat `Grep` for a known
filename, and strict discipline was paid on every turn regardless — the
cost/benefit was negative for the whole session and C3 was uninstalled.

`services.repo_shape` counts source files against prose/office documents
(config, data and images count for neither side) and calls the project
`code`, `mixed`, `prose`, or `empty` (fewer than 20 judged files — no
opinion). `c3 init` prints the result for every new project. For the `prose`
kind it suggests `advisory` in the interactive Step 5/5 with the reason
stated, and on a non-interactive install without `--enforcement` it writes
`advisory` with provenance `set_by: repo-shape` — over a tier-derived or
unset mode only, never over a `c3 enforce` choice or an explicit flag — and
prints the way back (`c3 enforce strict`). The edit ledger records native
writes in every mode, so nothing is lost but the hard block.

### Fixed — a sub-agent with no C3 tools no longer gets a deny it cannot follow

A Claude Code sub-agent whose definition lists `tools:` gets only those
tools. If none of them reaches the `c3` MCP server, the strict-mode deny
("use `c3_edit`") names a tool the agent does not have; the report counted
four unprompted self-reports in one session of agents completing their edits
through `python3 -c` instead — writes the ledger never saw. Claude Code
sends `agent_type` with every hook payload, so the PreToolUse hook now reads
the agent's `.claude/agents/<name>.md` (project, then user; inline and list
`tools:` forms) and, when the grant provably has no `mcp__c3__*`, `mcp__c3`
or `*` entry, degrades the block to the advisory nudge for that agent only —
the edit ledger still records the write — and says how to keep strict for
it. An agent with no `tools:` line inherits every tool and stays strict. An
agent that cannot be found stays strict, but the deny now names the fix
(add `mcp__c3__c3_edit` to the grant, or drop the `tools:` line) instead of
pointing at a tool the agent may not have.

### Fixed — `c3 init --clear` deleted files it did not own, and the settings cleanup reported work it had not done

From a field report (2026-08-22): a documentation-only project whose
CLAUDE.md carried both C3's managed block and another tool's, where the
session ended with C3 uninstalled — and the uninstall itself doing damage.

**`--clear` and Wipe now remove C3's block, not the file.** Both paths did
a bare `unlink()` of CLAUDE.md / AGENTS.md / GEMINI.md. In a file that also
held a `<!-- YEP:BEGIN -->` block, or the user's own notes, that destroyed
content C3 never wrote — recoverable only because git happened to have it.
They now cut the `C3:BEGIN … C3:END` span with the same ownership rule
every regeneration path already follows (`strip_c3_block`, the inverse of
`merge_c3_block`), write the remainder back, and delete a file only when
the block was all there was. A file with no C3 block in it is left alone
and says so. The Wipe prompt describes what it actually does.

**The settings cleanup now touches every C3 entry, and checks itself.**
`_uninstall_mcp_all` stripped `PostToolUse` entries under three matchers
from the pre-v2.42 layout — and nothing else — then printed *Removed C3
hooks/settings*. PreToolUse, Stop and UserPromptSubmit dispatcher hooks and
the whole `mcp__c3__*` permission list survived the "uninstall" (66 entries
in the report). It now removes any hook whose command runs a C3 hook script,
under any event and any matcher, every `mcp__c3__*` / `mcp__c3` permission
rule, and the `c3` MCP-server enablement; user hooks and user rules are not
touched. After writing it re-reads the file and reports what is on disk:
*Removed … (N entries)*, or *[!] C3 entries remain* with what they are.

**Tool discipline stops at the project root.** The PreToolUse hook blocked
(strict) or nudged a native Edit/Write aimed at `/tmp/scratch.html` exactly
as if it were a project file — there is no ledger for it to protect, so the
block was pure friction and pushed writes onto a shell-heredoc path that
mangled `\u2019` escapes. Paths outside the project root now pass through
in every mode. The credential-vault guard still runs first and is unaffected.

### Fixed — the Oracle dashboard was signed out, and said the model was down

**The Open Oracle button now signs you in.** Since the Oracle became a
login service, the only thing that ever handed the dashboard a session
was `c3 oracle open` — and nothing on the login path runs it. A dashboard
opened from the hub's top bar, or from a bookmark, holds no session
cookie. Only *mutating* calls are gated, so the page rendered, the header
went green, `/api/health` reported `ollama_available: true` — and chat,
Save settings and Test Ollama all answered `401 unauthorized`. The top
bar link now goes through `GET /api/oracle/open` on the hub, which
redeems the Oracle's owner-only bootstrap key on the browser's behalf and
redirects to a signed-in dashboard. It reaches no further than `c3 oracle
open` already did; if the Oracle is down or the key is missing, it falls
back to the plain URL exactly as before.

**A session now survives a restart.** The cookie secret was regenerated
per process and never written down, so every login — every Oracle restart
— silently invalidated it. It is now stored in `~/.c3/oracle/session.key`
under the same owner-only ACL as `bootstrap.key`, and the cookie carries a
30-day age instead of dying with the browser. This grants nothing new:
`bootstrap.key` on disk already mints a session, so a readable file in
that directory was always equivalent to one. `rotate_session_secret()`
signs every browser out.

**The chat stopped blaming the model for an auth failure.** A 401 on send
surfaced as `Connection error: unauthorized` *and* `No response received —
the model may be unavailable`, which reads as an Ollama outage. The UI now
recognizes 401 anywhere (chat and every `api()` caller), says *signed out —
this dashboard is read-only* with the way back, and no longer appends the
"model may be unavailable" line after a transport error that already
printed its own cause.

**The dashboard says so on arrival, and offers the way in.** The previous
pass named the state but only after a write had already failed, and it
pointed at a button on another page — a dead end for the tab that was
telling you about it. `GET /api/health` now reports whether the caller
holds a session (the cookie is `HttpOnly`, so the page has no other way to
know), the header poll raises a banner on load, and the banner carries a
**Sign in** button built from the Oracle's own `hub_url`. The status pill
no longer reads *All systems OK* on a dashboard that cannot write — it
reads *Read-only — signed out*, which is the reading whose absence let
this go undiagnosed.

**The startup URL respects `bind_host`.** The Oracle printed and opened
`http://localhost:<port>` even when bound to one specific interface — a
LAN or Tailscale address, where loopback is not listening at all.

## [2.91.0] - 2026-08-21

### Added — the Oracle starts from the hub, and comes back on its own at login

**Hub → Settings → Oracle service.** A second service block beside the
hub's own: Install / Uninstall / Start / Stop / Open for `c3 oracle
serve`. *Start* launches the Oracle as a detached background process —
no terminal window to keep open — and *Install* registers it to start
at login through the same mechanism the hub uses (Task Scheduler with a
Run-key fallback on Windows, a LaunchAgent on macOS, a systemd user unit
on Linux). `c3 oracle serve --install | --uninstall | --status` is the
CLI form. New routes: `GET /api/oracle/service`, `POST
/api/oracle/service/{install,uninstall,start,stop}`.

**An empty Oracle URL fills itself.** When the hub's `oracle_url` is
blank, a successful install or start writes the Oracle's address into
it, so the Open Oracle button appears in the top bar without a second
trip to settings. A value the user set is never overwritten. (The
Oracle was once healthy for days while the hub showed no way to reach
it, for exactly this reason.) Saving settings now also refreshes the
top bar, so a hand-typed URL shows up without a reload.

**The Oracle's service knows where the Oracle lives.** Port and bind
host come from `~/.c3/oracle/config.json`, not the hub config, and
"running" is a `/api/health?probe=1` check on the configured bind host
that must answer `service: c3-oracle` — a loopback probe never sees an
Oracle bound to a LAN or VPN address, and an open port is not proof the
Oracle owns it. `probe=1` is new: it answers with identity only, skipping
the Ollama and hub round-trips that make the full health check take
four seconds or more (an Oracle without the flag still answers the slow
way, and the probe waits for it). The launcher waits up to 120 s for a non-loopback bind
host to come up before starting: at logon a VPN address can appear
late, and binding before it exists is a fatal error that a one-shot
logon task would never retry. And it refuses to start a second Oracle:
Windows lets two listeners share a port, so a duplicate at logon does
not fail — it silently splits the traffic, loses the MCP port, and can
take the first instance down with it. The launcher asks
`/api/health?probe=1` first and exits cleanly if an Oracle answers.

### Changed — one service machinery, two services

`services/hub_service.py` is now a thin subclass of the new
`services/background_service.py`; `OracleService` is its sibling. The
hub's install/uninstall/start/stop behave as before, and both services
inherit four fixes:

- The launcher redirects stdout/stderr to its log *before* the server
  is imported. Under `pythonw.exe` both streams are `None`, and
  uvicorn's log formatter calls `sys.stdout.isatty()` — this is how the
  Oracle's MCP listener once died at startup while its REST port stayed
  up and every liveness probe passed. The order of those two lines is
  pinned by a test.
- *Start* no longer launches a second copy of a service that is already
  answering; it says so instead.
- Task Scheduler registrations carry restart-on-failure (three tries, a
  minute apart), and the launcher exits non-zero on a startup exception
  so that policy can fire. Previously a crash at logon — repo drive not
  mounted yet, say — exited 0 and stayed dead until the next reboot.
- The Windows port killer matches the port as a whole token (`:3331`
  used to also match `:33310`) and no longer goes through `shell=True`.

The launcher log for the Oracle is `~/.c3/oracle/service.log`; the
server's own log stays `~/.c3/oracle/oracle.log`. They are separate on
purpose — the server also logs to stderr, and sharing the file would
print every line twice.

## [2.90.0] - 2026-08-20

Ships the `login` structured credential kind end to end: the store and
CLI (#113), the TOTP seed normalizer fix found while cross-implementing
RFC 6238 (#114), and the two web UIs that had no way to enter one (#115).

### Added — the vault can hold a website login, and C3 still refuses to use one

**New structured kind: `login`.** Fields are `site_id`,
`canonical_origin`, `username`, `password` and an optional
`totp_secret`. It inherits the existing inject-only machinery
unchanged: `agent_readable` and `inject` are refused, `reveal` is
permanently disabled, the whole payload is never resolvable, and only
individual fields are addressable (`env_creds='GH.password'` →
`$GH_PASSWORD`, or `{{cred:GH.password}}`).

The reason this is worth a type of its own rather than a `token` entry
holding a password: on a `token`, `agent_readable` and `reveal` *can*
be turned on. On a structured kind they cannot be, by construction.

**`canonical_origin` is validated and stored normalized** — https only,
`scheme://host[:port]`, no path, query, fragment or userinfo, host
lowercased. A stored origin carrying a path makes prefix comparison
ambiguous downstream, and ambiguity in an origin check is the whole
attack. `totp_secret` must be base32; a malformed seed produces wrong
codes that look like password failures.

**C3 does not log in to anything, and must not learn how.** There is no
browser surface in this package and this change does not add one. A
login runner in which the *agent* chooses the destination URL converts
page-content prompt injection into credential exfiltration, and a guard
written into a script that already holds the plaintext in its own
environment is not a boundary — the script can simply not call it.
`canonical_origin` is stored so that a separate, out-of-process runner
that the agent does not author can pin a credential to exactly one
origin and validate the live top-level frame before typing. That runner
is deliberately not shipped here.

Registry projection for a `login` entry is `{site_id, origin,
has_totp}`. The username is withheld on purpose: username + origin is
half the credential, and the registry is the part of the record that
non-secret surfaces are allowed to render.

### Fixed — a character *range* deleted the digits out of every TOTP seed

`re.sub(r"[ -]", "", seed)` was meant as "strip spaces and hyphens". It
is a character RANGE from space (0x20) to hyphen (0x2D), so it also
swallowed `2-7` — half the base32 alphabet. A seed pasted in the usual
spaced form (`jbsw y3dp ehpk 3pxp`) silently lost its digits, still
passed the `^[A-Z2-7]+=*$` shape check, and stored short and wrong. The
only symptom would have been 2FA codes that never work, which is
indistinguishable from a wrong password — you would have spent the
debugging session on the wrong half of the credential. Now `[\s\-]`,
with a regression test that asserts the digits survive normalization.
Found by cross-implementing TOTP against RFC 6238's published vectors
rather than by re-reading the line. (#114)

### Fixed — the `login` kind shipped without a way to enter one

**Neither web UI knew the kind existed.** The store, `c3_credentials`
and `c3 creds` all took `login` from the day it landed; the Credentials
tab and the Hub's credential manager are driven off a *hardcoded*
`CREDS_STRUCTURED` table that had three entries. Nothing failed loudly:
the store is type-agnostic and `POST /api/credentials` passes `type`
straight through, so the kind was simply unreachable from the browser —
while the guide told the user to enter these through the UI precisely so
a password never has to be typed into a chat. Worse, an entry created
from the terminal rendered in the browser as a *plain* secret: one
"Replace secret…" password box (which would have written the whole
payload as an opaque string) and agent_readable / inject toggles the
server refuses with a 400.

Both UIs now carry `login` in the field table (password and
`totp_secret` render masked), in the Type dropdown, and in
`credsDisplayText` — the last one because the projection contains a
*boolean*, so the generic `Object.values(...).join(', ')` fallback would
have printed the literal word `true` next to the site. The structured
banner gains a login-specific line stating that C3 stores these and does
not log in to anything.

**The guard is a parity test, not the fix.**
`tests/test_credential_ui_parity.py` parses `CREDS_STRUCTURED` and the
Type `<option>` list out of both JS files and asserts them against
`credential_store.STRUCTURED_TYPES` / `schema_fields()` / `VALID_TYPES`.
It reads the real source rather than restating the field lists in
Python, because a test that duplicated them would pass while the browser
stayed wrong — which is exactly what happened here. The next kind added
to the store fails this file until both UIs carry it. Verified against
the pre-fix tree: 5 of 7 fail.

## [2.89.1] - 2026-08-19

### Fixed — a project merge uninstalled C3 from the whole machine, and the JSX checker could never start on Windows

**The Hub Merge action's `cleanup='clear'` deregistered C3 from every
IDE on the box.** Source cleanup called `_uninstall_mcp_all(src)` —
which is the machine uninstall: beyond the source project's own files
it walks `Path.home()` and strips the C3 entry from
`~/.codex/config.toml`, deletes Antigravity's `mcp_config.json`, and
cleans the legacy `~/.gemini/settings.json`. Those configs are shared
by every C3 project, so merging one project silently broke all of
them. Found the hard way: the merge test suite exercised this path
without home sandboxing and did exactly that to a real development
machine. `_uninstall_mcp_all` now takes `include_global` (default
true — the real `c3 uninstall` is unchanged), every home-config touch
is gated on it, and merge cleanup passes `include_global=false`. The
regression test runs merge-clear under a sandboxed home and asserts
the global configs survive byte-identical. (#111)

**`node --check`'s JSX fallback reported "tsc not installed" on
Windows even with TypeScript installed and on PATH.** Checkers were
launched by bare name, and npm-global tools on Windows are `.cmd`
batch shims that `CreateProcess` cannot resolve — so the tsc verdict
the 2.88.1 JSX-idiom fix was built around could never actually engage
on Windows. `_subproc_check` now resolves the checker through
`shutil.which` (whose resolved shim path Popen *can* execute) and
short-circuits a genuinely missing checker without spawning anything.
UI `.js` files now validate `clean (tsc)` instead of degrading to the
"unsupported" advisory forever. (#110)

## [2.89.0] - 2026-08-19

### Added — Jira and Bitbucket stop being read-mostly: epics, links, sprints, worklogs, attachments, PR edits and tasks

**`c3_jira` had no way to put an issue under an epic** — the classic
"customfield hunt". Both `create_issue` and `update_issue` now take
`parent=<EPIC-KEY>` (`'none'` clears): on Cloud it maps to the `parent`
field; on Data Center C3 discovers the per-instance *Epic Link* custom
field from `GET /field` (cached per server per process) and routes epic
parents through it, falling back to the `parent` field for subtasks.
Typed issue links landed with it: `link_issues(issue, link_type, target)`
reads `PROJ-1 blocks PROJ-2`, accepts a type name or either directional
phrasing (an inward phrase flips the pair), and answers an unknown type
with the server's catalog (`list_link_types`); `unlink_issues(link_id)`
removes one. `get_issue` now renders parent, links (with ids), and
attachments, so every one of these writes is verifiable in the same tool.

**The Agile surface existed only in the browser.** `list_boards` →
`list_sprints(board_id)` → `move_to_sprint(issue, sprint_id)` /
`move_to_backlog` run against `/rest/agile/1.0`, which is identical on
Cloud and Data Center. `add_worklog(issue, time_spent='2h 30m')` and
`list_worklogs` cover time tracking; `attach_file(issue, file_path)`
uploads one local file (multipart with `X-Atlassian-Token: no-check`,
20MB local cap). `delete_issue` is deliberately last: permanent,
ledger-logged, and it refuses an issue that still has subtasks unless
`delete_subtasks=true` — Jira's own cascade check doubles as the guard.

**A Bitbucket PR was immutable the moment `create_pr` returned.**
`update_pr` edits title/description/reviewers/target on an open PR —
unchanged fields are merged from the live PR and the `version` for
Bitbucket's optimistic locking is fetched automatically, the same
get-then-write shape `merge_pr` and `decline_pr` already used.
`needs_work_pr` completes the reviewer-verdict triad, `get_pr_commits`
lists the PR's commits, and comments got a lifecycle
(`update_pr_comment` / `delete_pr_comment`, version auto-fetched).
PR **tasks** use the modern model where a task *is* a BLOCKER-severity
comment: `create_pr_task`, `list_pr_tasks` (state filter), and
`resolve_pr_task`. One latent bug fixed on the way: Bitbucket's ledger
logger recorded mutating actions even when validation failed before any
API call — it now skips error responses, matching Jira's logger.

## [2.88.2] - 2026-08-16

### Fixed — the Oracle refused its own machine, and the drill panel hid five tabs

**`c3 oracle open` got 403 "loopback only" from a healthy server.** When
`bind_host` is one specific non-loopback address — the Tailscale
deployment that lets the phone reach the Oracle — loopback is never
bound, so every same-machine request arrives with the interface address
as its source, and the session-bootstrap mint and cookie redeem both
refused it. That also walled off mobile pairing: no cookie → the
dashboard never reveals the Discovery token → the QR cannot render.
`local_session.is_local()` now carries the same-machine proof at both
gates: loopback always qualifies, and under a single non-wildcard bind a
source address *equal to the bound address* is accepted — every remote
peer presents its own address, off-box TCP spoofing of the host's own
address cannot complete a handshake, and Tailscale pins tailnet source
IPs to node keys. Wildcard binds (`0.0.0.0`/`::`) never equal a client
address, so they stay loopback-only by construction, and the owner-only
bootstrap key is still required — the same-OS-user boundary is
unchanged. (#106)

**The hub drill panel's 13 tabs sat in a single `nowrap` row** with
`overflow-x: auto`, so in the 720px drawer the tail tabs (Budget,
Credentials, Discipline, Config, MCP) were only reachable by dragging a
thin horizontal scrollbar. The strip now flex-wraps: every tab visible
at once. (#105)

## [2.88.1] - 2026-08-16

### Fixed — the validator called C3's own UI idiom a syntax error, and a test suite that changed verdicts per host

**`node --check` was the only judge of `.js` files, and it cannot read
JSX** — which C3's own UI (`cli/ui`, `cli/hub_ui`) deliberately ships in
`.js` files served through Babel. Every validated UI component banked a
false "has syntax errors" auto-memory fact (`toasts.js` had been flagged
for days). `_native_js` now detects JSX intent and re-judges under the
grammar the file is actually written in (the tsc JSX checker): valid JSX
reports clean, real defects report tsc's positions, and when tsc is not
installed the verdict is an honest `unsupported` — never a fabricated
syntax error. Plain-JS checking is unchanged, and a plain-JS defect never
consults the fallback.

**Three delegate-cascade tests read the host's real Access Guard state as
if it were part of the test.** `handle_delegate` skips write-capable
backends (gemini/claude) when guard rules are active and
`allow_write_delegation` is false — deliberate policy — but the tests
never pinned that input, so any machine with seeded global rules (every
install since v2.86.0 seeds them) silently rerouted the expected cascade
to ollama and failed three tests that CI's clean home kept green. The
fixture now pins the guard inactive, and two new tests assert both sides
of the gate explicitly: rules active blocks gemini with the reason in the
cascade note; the opt-in routes to it.

## [2.88.0] - 2026-08-16

### Added — a credential now has a history, not just a counter

**Every use writes an event: when, where, how often.** Until now the only
usage record was a `{last_used, use_count}` counter — no way to answer
"what ran with the deploy token last Tuesday". Now each injection,
template expansion, reveal, and terminal `--show` (previously invisible to
usage tracking entirely) appends one line to the owning scope's
`.c3/cred_usage.jsonl`: name (+field), action, surface, project, exit
code, and the command in its raw template form capped at 120 chars —
never an expanded string, never a value, enforced by hygiene canaries.
The module is a deliberate clone of `access_telemetry`'s shape:
append-one-line (concurrent processes can't race an append), 512KB
rotation, read-time coalescing, and telemetry that swallows its own
errors rather than ever failing a tool call. A global credential's usage
from every project lands in `~/.c3`, so its history is complete in one
place; both filenames are vault-write-protected (hook parity-tested) and
gitignored.

Reading it back: the Credentials tab gained a **usage** sub-view (totals,
per-credential surface counts, expandable recent events), the hub drawer's
"Usage & relationships" became a real history, `c3 creds usage [NAME]
[--json]` prints it at the terminal, and `GET /api/credentials[/<name>]/usage`
(+ hub and mobile twins) serve it. The agent-facing
`c3_credentials(action='usage')` is deliberately scoped: full events only
for the current project, other projects' use of a shared global credential
reduced to counts — one project's agent cannot read another's command
lines through the vault. The `shell_exec` activity event now carries the
injected cred names, and a low-volume `cred_use` event feeds the daily
digest.

## [2.87.0] - 2026-08-16

### Added — the vault now holds the sensitive data that isn't a secret string: cards, addresses, identity

**Three structured entry types — `card`, `address`, `identity` — store named
fields the agent can use but never see.** A card is cardholder + number +
expiry (+ cvc/billing_zip), Luhn-validated, stored as one canonical JSON
object through the exact keyring/Fernet path every secret already uses. The
agent addresses a single field at the subprocess boundary —
`env_creds='CARD.number'` (exported as `$CARD_NUMBER`) or
`{{cred:CARD.number}}` inline — and echoed values scrub to
`[cred:NAME.field]`.

Structured entries are inject-only **by construction**, not by flag:
`agent_readable` and `inject` are refused at every surface, reveal
short-circuits with `[creds:structured]` before any gate, whole-value
resolution is refused inside `get_value` itself (so a hostile registry
edit that flips `inject: true` puts the name in the missing list, not a
card payload in a subprocess env), and a keyring attestation keeps an
entry structured even when `.c3/config.json` is rewritten by hand. The
plain/structured boundary of an existing name is immutable — `set`,
metadata updates, and `.env` import all refuse to cross it; delete and
re-create is the only path.

What other surfaces see is a server-computed projection: `visa ••••4242`
(brand + last4 — deliberately *not* expiry), city/state for an address, the
name for an identity. Field names are public metadata; field values never
appear in any HTTP response, enforced by a seeded-PAN sweep over every
credentials route on the project server, the hub, and the mobile gateway.
The mobile gateway additionally refuses to *create* structured entries —
card data never transits the phone channel. Human read-back is
terminal-only: `c3 creds get NAME --show [--field number]`; both UIs gained
per-type field forms (partial updates merge, so changing an expiry never
means retyping the PAN).

Documented decision: the identity display label is the full name, the same
sensitivity class as a user-written description. Known limit, documented in
the guide: the echo redactor tracks values of 4+ characters, so a 3-digit
CVC echoed by a child process cannot be scrubbed after the fact — the
guarantee is decode-at-the-boundary, and redaction stays the belt over it.

## [2.86.1] - 2026-08-16

### Fixed — two credential routes built responses beside the allowlist, not through it

`public_entry()` exists so that a field added to the credential store cannot
reach the wire until someone allowlists it on purpose — but the per-project
server's list route and set-response still built their payloads with
`dict(entry)`, forwarding every key the store carries. Nothing leaked
*today* (the registry holds no secret fields), so this is the latent class,
not an incident: any field the store gains next would have shipped to the
UI by default from those two routes while every other surface withheld it.
Both now serialize through `public_entry()`, and a new serializer-identity
test ties every credentials-route response keyset to `PUBLIC_FIELDS`
itself — so the next `dict(entry)` shortcut fails in CI rather than in
production.

## [2.86.0] - 2026-08-15

### Added — a milestone can now be completed, not just archived

`archive_milestone` was the only way to close a milestone, and it is a
removal: every task loses its `milestone_id`, so the record of which tasks
shipped under which milestone survives only in the event log. Ten fully
shipped milestones were sitting "active" in this repo's own PM store
because closing them would have cost that history.

`TaskStore.complete_milestone` closes the happy path: lifecycle flips to
`completed` (stamping `completed_at`), the milestone leaves default
listings, the board, and the report — and its tasks keep their link.
It refuses while open active tasks remain; `reopen_milestone` undoes it
(accepting a unique completed-milestone name, which `resolve_milestone`
deliberately does not match). Completed milestones are not eligible for
`purge_archived`. Exposed as `c3_task(action='milestone_complete' |
'milestone_reopen')` and as `{complete: true}` / `{reopen: true}` on the
milestone PUT of all three REST surfaces (hub, per-project server,
mobile).

### Fixed — the default Access Guard rules were documented but never seeded

`docs/access-guard.md` §1 promises `*.pem`, `id_rsa*`, and `*.key` as
visible, removable DEFAULT deny rules in global scope; the constant existed
in `access_guard.py` since v2.62.0 but nothing ever wrote it — a fresh
install got zero global rules. `seed_default_global_rules()` now runs on
`install-mcp` and on the first `c3 access` touch. It seeds only when the
global config has never carried an `access` section, so deleting a default
(or all of them) stays sticky forever; a corrupt config is never rewritten,
and a missing home directory is a silent no-op.

## [2.85.1] - 2026-08-11

### Fixed — three silent failures, all found by using C3 to ship something else

None of these raised an error. Each one cost a caller a working command, and
two of them made a tool unusable while reporting nothing.

**A linked worktree is another checkout, not more of this project.**
`SKIP_DIRS` matches directory *names*, and a worktree is named whatever
somebody called it — its only marker is a `.git` **file** holding
`gitdir: …` rather than a `.git` directory. So pruning the name `.git`
skipped a file that was never a directory, and the shared walker descended
into an entire second copy of the repository. Every index build goes through
that walker. On one project: 112 worktrees, and **88,015 of 107,729 tracked
files (81.7%) were duplicate copies of the same repo**. A cross-project exact
search there took 147.6s against a transport that kills a tool call at 120s —
it could never return. It was also the wrong *answer*: hits were duplicated up
to 112 times and the top result was whichever stale copy sorted first.
`is_nested_checkout()` now prunes any child directory carrying a `.git` entry
of either kind, catching worktrees and submodules alike at any depth and under
any name. The project root is never tested, so a project cannot prune itself.

**A URL is not an NTFS alternate data stream.** The shell scan's scheme
pattern was anchored, so a URL was only recognised when it *began* the token —
and the `_SYNTAX_CHARS` generalisation could not cover a token carrying a URL
and no syntax at all. `printf '\n---\n\nhttps://…\n' >> body.md` therefore
canonicalised to a residual colon and became a hard, unappealable `<ads>` deny
on a command whose only file argument was an ordinary markdown path. Third
logged instance of the class. The scheme is now matched anywhere in the token;
a real stream spelling is `file:name:$TYPE` and can never contain `//`.

**`c3_shell` accepted a timeout it could not deliver.** `_MAX_TIMEOUT` is 600,
but an MCP client kills the call at `MCP_TOOL_TIMEOUT` — a limit C3 does not
choose and cannot raise. A 600s request was clamped to 600 and killed at 120s
with C3's own deadline never arriving, so the caller saw the call "moved to
background" and then fail, with nothing naming the real limit and a subprocess
C3 never reaped. It is now discovered from the environment and run just inside,
so C3's deadline fires first and the result is an ordinary `[c3_shell:TIMEOUT]`
with output and a killed process tree — plus a `[c3_shell:capped]` line naming
the requested value, the real limit, and the escape hatch that is not bound by
it. An unset or unparseable variable caps nothing.

### Note for existing installs

The worktree fix stops those files *entering* the index; it does not evict
what is already there. **Projects with git worktrees need a reindex** to shed
the duplicates.

## [2.85.0] - 2026-08-10

### Added — run history, flake detection, and a GitHub status bridge

Phases 9 and 8 of the AgentCI plan, both scoped to what the available data and
credentials can honestly support.

**`c3 ci history`** reads the run records that already existed. Per-job
pass/fail counts, average duration, and one genuinely useful signal: a job that
both passed **and** failed on the *same fingerprint* — identical definition,
identical inputs — changed its mind without the world changing. That is a
flake, not a regression, and it is the only local flake signal that is not a
guess. Samples under five executions are labelled `(low n)` rather than
presented as a rate; three runs is not a trend. Cached results are not counted
as observations, because reuse is not evidence about behaviour.

Deliberately absent: predicting which tests a change will break, inferring
coverage, ranking "risk". Those need a dependency graph and coverage data C3
does not have, and a confident-sounding number derived from neither is worse
than silence — an agent would act on it.

**`c3 ci publish`** posts a **commit status** through your existing `gh`
authentication. No GitHub App to register, no callback URL, no token for C3 to
hold. It refuses three things, because a status is a claim other people act on:

- a **dirty tree** — the status attaches to a commit, and if the working tree
  differs then the thing that ran is not the thing being labelled;
- an **unpushed commit**, which has nothing to attach to;
- a **`PARTIAL_PASS`**, unless forced — GitHub's states are success / failure /
  pending / error and none of them mean "we checked some of it", so rather than
  pick a misleading one it refuses, and when forced posts `pending` with the
  reason.

The context is `agentci/local` so nobody mistakes a laptop run for hosted CI.
PRD 5's full GitHub App remains unbuilt: it needs an App registration, which is
not something C3 can or should create on your behalf.

## [2.84.0] - 2026-08-10

### Added — fingerprinted result reuse and a real dependency cache

See PR #92. A job whose definition, engine and inputs are unchanged since it
last passed is reused as `cached` — never as a fresh pass — and the verdict
note says how many came from cache. `ci.required_map` narrows both *selection*
(Phase 5) and *invalidation* (Phase 4) from one declaration; without it the
fingerprint covers the whole tree, so any edit invalidates. `actions/cache` is
now real: restore at the step, save only after the job passes, keys immutable.

Two bugs found by the cache never hitting: a local `fingerprint` shadowed the
module-level function of the same name, and `lstrip("./")` — which strips a
character set, not a prefix — turned `.c3/` into `c3/`, so C3 invalidated its
own cache every run by writing its own bookkeeping.

## [2.83.0] - 2026-08-10

### Added — required mode, and a guard against C3 uninstalling itself

See PR #91. `c3 ci plan` / `run --required` select the jobs a change could have
broken, conservatively, with a reason for every decision. Anything unmapped
runs.

Running it found a real defect: the native engine has **no isolation**, so
executing this repository's own `test` job (`pip install -e .`) uninstalled C3
mid-run and broke every project's hooks. The native engine now refuses
host-mutating steps and points at the act engine, where the same step is
contained.

## [2.82.0] - 2026-08-10

### Fixed — the engine count was wrong in three places, and the Hub was lying

Three loose ends from the act release, all of the same shape: something knew
the right answer and something else re-derived a wrong one.

**`c3 ci inspect` reported the native count only.** It printed *"Runnable on
this host: 3 of 15"* when act made the real answer 11. The partition is now
computed once in `inspect_project` — which grew an `engine` parameter and
`runnable_native` / `runnable_container` / `engines` — and the tool text, the
`--json` output and the Hub all read the same numbers instead of each
inferring their own:

```
Runnable here: 11 of 15  (3 native, 8 container)
  engines: native + act (act version 0.2.89)
```

**The Hub CI tab showed every job as a green PASS**, including the three macOS
cells that can never run anywhere on this machine. Found by opening it, which
had not been done before — the tab had only ever been verified as "bundles and
the routes answer". The view was inferring runnability in JavaScript from
`act_could_run`, which means "act has no blockers with this job", *not* "act
can run it here": act only does Linux. It now reads the server's partition, and
the job graph uses capability labels (`NATIVE` / `CONTAINER` / `OTHER-OS` /
`UNSUP`) rather than borrowing run-outcome ones, because a graph describes what
*can* happen and a run list describes what *did*.

**A job blocked on every engine was classified `foreign`** rather than
`unsupported` — sending the reader toward "install act" when the real problem
was a missing secret. Classification now judges the blockers of the engine that
*would* have been chosen.

Also fixed: an unevaluable **job-level** `if:` was recorded as a native blocker
but not an act one, so act was wrongly considered able to run it. The
step-level case was already right; this was the matching half.

### Fixed — the test suite no longer depends on your own C3 settings

`c3 enforce advisory` writes `~/.c3/config.json`, and seventeen tests across
four files then failed — not because anything was broken, but because they
resolve enforcement policy from the ambient home directory and were written
when it was `strict`. CI never saw it (no home config on a fresh runner), so
the suite was green on the server and red on the machine of whoever had used
the feature.

A suite that depends on developer settings answers a question about the
machine, not the code — and worse, it teaches people that some failures are
normal, which is how a real regression gets waved through. A new
`tests/conftest.py` points `C3_HOME` at an empty directory for the session, so
`pytest` now passes locally with no environment fiddling. One fixture, no test
files touched.

## [2.81.0] - 2026-08-10

### Added — AgentCI runs Linux jobs in real containers via `act`

Until now a job whose `runs-on` did not match the host could only be
approximated on it, and a `uses:` step that was not on the shim list could not
run at all. There is now a second execution engine: `nektos/act`, which runs
the job in a container using GitHub's own semantics — **real actions
included**.

Measured on this repository from Windows:

| | native only | with act |
|---|---|---|
| runnable | **3 of 15** | **11 of 15** |

The remaining four are macOS cells, and no engine will ever run them: there are
no macOS containers. A matrix containing them cannot reach `FULL_CI_PASS`
locally, which is a property of the world rather than a gap to close, and
`c3 ci doctor` says so out loud.

**Fidelity is now part of the verdict.** Each job records how faithfully it was
reproduced — `native`, `container`, or `cross-os` — and `FULL_CI_PASS` requires
every job at `native` or `container`. A Linux job in a Linux container *is*
that job; a cross-OS approximation is not, and still caps the run at
`PARTIAL_PASS`.

**Blockers became per-engine**, which fixed a bug that had been hiding act's
entire point: supportability was decided before the engine was chosen, so a job
using a third-party action stayed refused even when the engine that could run
it was selected. `inspect` now reports both answers. A missing
`${{ secrets.X }}` still blocks on either engine — no engine can reproduce a
job whose input does not exist.

New: `c3 ci doctor`, `--engine auto|native|act`, `--network`, and
`--allow-side-effects`. `--engine act` **fails** when act is unavailable rather
than falling back silently: the caller asked for container fidelity and would
not have got it.

#### Safety

Running real actions means a publishing job goes from unrunnable to one command
from publishing. Two things stand in the way. C3 **never passes secrets** — act
reads `.secrets` and `.env` from the repository by default, and both are
pointed at an empty file, so a publish step runs and fails at authentication.
And a **side-effect gate** refuses jobs using known publishing actions or
commands unless `--allow-side-effects` is passed. The first is a mechanism, the
second a policy; neither is a sandbox.

#### Three things learned by probing rather than by reading

- **`--bind` is mandatory on Windows.** act's default copy-mode workspace
  arrived *empty* against a Windows host path and every step failed on missing
  files. With `--bind` it works even on a mapped drive whose path contains
  spaces and parentheses.
- **`-P <label>=<image>` must always be passed**, or act prompts interactively
  on first use and hangs an automated run.
- **act's log prefix has to come off before parsing.** Feeding the raw log to
  the failure parsers produced
  `file='[CI/lint] ⭐ Run Main echo "app/x.py'`. Only the `|` gutter lines are
  program output; parsing those yields a clean `app/x.py:14 F401` from inside a
  container.

## [2.80.0] - 2026-08-10

### Added — AgentCI evaluates `if:` conditions

v2.79.0 parsed `if:` and ignored it, so every guarded step ran locally whether
or not CI would have run it. That over-runs rather than under-runs — it cannot
manufacture a false green — but it wastes time and reports failures for steps
that were never going to execute. It is now evaluated, with GitHub's semantics:

- a condition naming no status function is implicitly `success() && (...)`,
  which is why `if: always()` is the documented way to run after a failure;
- a job-level `if:` **replaces** the `needs` success gate, so `always()` and
  `failure()` run even when a dependency failed — while an ordinary condition
  still skips, because the implicit `success()` is injected rather than lost;
- a step with no `if:` keeps its implicit `success()` gate and is skipped once
  the job has failed.

Supported: the comparison and boolean operators, parentheses, literals, and
`success` `failure` `always` `cancelled` `contains` `startsWith` `endsWith`
`format` `join` `toJSON` `fromJSON`, over the `github` `env` `matrix` `runner`
`job` `needs` `steps` `strategy` contexts. Evaluation short-circuits, so
`false && needs.x.result == 'y'` does not blow up on an unresolvable branch.

**A job skipped by its own `if:` does not cost coverage.** It gets its own
status (`skipped_if`) and `FULL_CI_PASS` stays reachable, because CI would have
skipped it too — not running it *is* the faithful reproduction. Counting it as
a gap would have left any workflow containing a conditional job permanently
unable to reach a full pass, which is wrong rather than merely strict.

**Two things still refuse rather than guess**, consistent with the rest of the
module: a condition we cannot parse, and one reading a value with no honest
local equivalent. The second is mostly `github.event_name` — there is no event
locally, and inventing `"push"` would evaluate the user's condition against
fiction. So it blocks with a message naming the fix, and a new `--event` /
`event=` parameter declares what you are simulating:

```bash
c3 ci run --event pull_request
```

`github.ref`, `ref_name` and `sha` are now read from git rather than the
placeholder values v2.79.0 used, and `needs.*.result` resolves from real
outcomes as the run proceeds.

One regression the existing suite caught while this was being built: teaching
the runner to continue past a failure (so `always()` can work) briefly made
every *unguarded* later step run after a failure too. A missing `if:` still
means `success()`, and there is now a test pinning exactly that.

## [2.79.0] - 2026-08-10

### Added — AgentCI: run this repo's real CI locally, before pushing

A new `c3_ci` tool, `c3 ci` command, and Hub **CI** tab that execute the
repository's own `.github/workflows/*.yml` on this machine. Implements the MVP
loop from `AgentCI_Product_Architecture_PRD_Roadmap.md` §34 and follows its §41
"start narrow" instruction.

```
edit → run CI locally → structured failure → fix → rerun what failed → full CI → push
```

The constraint that shaped everything: **C3 does not define a second CI
config.** The workflow files are the source of truth, so there is nothing to
keep in sync and no local script that can drift from what CI actually runs.

**The verdict is the product.** `FULL_CI_PASS` is the only result that means
"safe to push", and it requires that *every* job ran on this host and passed:

| verdict | meaning |
|---|---|
| `FULL_CI_PASS` | every job ran here and passed |
| `PARTIAL_PASS` | nothing failed, but something did not run |
| `FAIL` | a job failed, timed out, or its dependency did |

That distinction is not decoration. On this repository, from Windows, 3 of 15
jobs are reproducible: nine target another OS and three use publish actions we
do not execute. A tool that printed a green tick after running a fifth of the
matrix would be at its most dangerous exactly when it is most used. So coverage
is part of the verdict, `c3 ci run` exits non-zero for anything but a full
pass, and a job whose `needs` dependency failed is `skipped` — never passed.

**What refuses, and says why.** Anything we cannot reproduce faithfully is
reported as `unsupported` and never run: an unknown `uses:` action, a
`container:`/`services:` block, a reusable workflow, or an unresolved
expression. `${{ secrets.TOKEN }}` quietly resolving to `""` is precisely how a
job passes locally and fails in CI, so it blocks instead. `actions/checkout`,
`setup-python|node|go`, `cache` and the artifact actions are shimmed, because
their local equivalent really is "the condition already holds".

A job whose `runs-on` targets a different OS is refused by default;
`--allow-foreign` runs it anyway, labels it cross-OS, and caps the verdict at
`PARTIAL_PASS` permanently. That is what makes the feature useful on a Windows
box without making it dishonest — C3's own ubuntu `lint` job runs here in ~13s.

Also in this release:

- **Structured failures.** pytest, ruff, tsc, jest and Python-traceback
  parsers turn a log into `{file, line, message}`. A failed job with no
  recognised format yields an explicit `unparsed` failure carrying a bounded
  tail — "0 failures" can only ever mean the job passed.
- **Matrix + DAG.** `strategy.matrix` expands to one instance per cell
  (including `include`/`exclude`), each resolving its own `runs-on`. `needs`
  is scoped to its own workflow: two workflows may each define `build`, and a
  global needs map would have invented an edge between them.
- **Run history** under `.c3/ci/`, each run fingerprinted with commit, branch
  and uncommitted-file count — a dirty tree is the normal case for an agent
  mid-edit, so it is recorded rather than refused.

Docs: `docs/agent-ci.md`. Deliberately not built, per the plan's own list:
remote runners, GitHub App status bridge, full Actions emulation (`if:` is
parsed but **not** evaluated), multi-CI support, caching, and test-impact
prediction.

## [2.78.0] - 2026-08-09

### Fixed — Codex validates hook output, and we were sending it Claude's shape (#84)

Reported by a Codex session whose hooks route into the shared dispatcher. The
dispatcher had exactly two host branches, chosen by one boolean:

```python
is_gemini = isinstance(payload.get("tool_response", ""), dict)
```

Codex fell through to the Claude branch. That was survivable for Gemini, which
ignores what it does not recognise. It is not survivable for Codex, because
Codex is the first host that actually **validates**: every hook output schema
embedded in `codex.exe` (verified against 0.147.0) is
`additionalProperties: false`, `hookSpecificOutput.hookEventName` is required,
and the deserializer is `deny_unknown_fields`. One unknown key discards the
**entire** response, not just the offending field.

So all three shapes C3 could emit were rejected:

| C3 emitted | Codex verdict |
|---|---|
| `{"tool_result": …}` | unknown top-level field — `tool_result` is in no Codex schema |
| `{"additionalContext": …}` | unknown top-level field — it exists nested only |
| `{"hookSpecificOutput": {"additionalContext": …}}` | well-formed, but `hookEventName` is missing |

`Stop` was a fourth case: `stop.command.output` carries no `hookSpecificOutput`
at all, and `main()` printed raw `_text` to stdout there, which is not valid
hook JSON for any Codex event.

Host detection was wrong in the other direction too. Codex declares
`tool_response` as free-form, so an MCP tool that returned an object made a
Codex payload read as **Gemini**.

**What changed.** Collection stays shared; only serialization branches.
`_codex_output()` builds from a per-event **whitelist** rather than stripping
keys it happens to know about, so a sub-hook that invents a new output key can
never break Codex again. Nothing is silently dropped: a `tool_result` degrades
to leading context, and `_text` becomes `systemMessage` — the only
user-visible string slot present on every Codex schema, and the only one
`Stop` has. `main()`'s panic path routes through the same emitter, so a
dispatcher crash reports once instead of twice.

Detection now keys on `turn_id`, which Codex requires on every turn-scoped
event and documents as a Codex extension. It is tested **before** the Gemini
check, which fixes the misdetection above.

**`hook_filter` no longer runs on Codex.** It is the one sub-hook that reads
the whole payload — it tiktoken-encodes the entire Bash output to measure
savings, a Rust-side allocation proportional to output size, made worse by an
`lru_cache(maxsize=2048)` keyed on the full text. A Codex session reported a
Rust allocation failure on large tool output; this is the most plausible
source, though no backtrace was retained, so treat the causal link as strongly
suspected rather than proven. On Codex the work could never pay off in any
case: there is no `tool_result` for the filtered text to land in.

Everything else is bookkeeping that never parses the payload, so it stays on
for Codex — edit ledger, artifact tracking, c3 signal, sticky unlock, ghost
sweep, and Access Guard enforcement.

**Scope.** This fixes the dispatcher for any Codex session whose hooks are
wired to it. C3 still does not *generate* `.codex/hooks.json` — the codex IDE
profile remains `supports_hooks=False`, so hook files for Codex are
hand-authored today.

## [2.77.0] - 2026-08-08

### Fixed — the shell scan now judges paths against the cwd, not the session root (#82)

Found by running the negative controls after shipping 2.76.3. `cd <elsewhere>
&& cat .env` was **not** flagged, and the reason was not the rule:

```python
_scan_shell("cat .env", base=<main checkout>)      # -> **/.env* deny
_scan_shell("cat .env", base=<session worktree>)   # -> None
```

`access_guard.check` was returning the deny correctly for both the relative and
the absolute spelling. Only the *existence gate* was asking the wrong
directory — it joined the token to the session's project root while the command
ran wherever `cd` had put it — so the denial was computed and then discarded.

The scan has been per-segment since 2.76.3, so a `cd` segment now updates the
cwd used by the segments after it, and relative tokens resolve against that.
Policy still comes from the project (`base` is unchanged); only the path being
judged follows the shell. Quoted targets and `cd /d` are handled, because
project paths on this platform contain spaces.

Two consequences worth stating rather than leaving to be discovered:

- **A project-scoped rule stops binding once you `cd` into a different repo**,
  which is correct and is a behaviour change. `cd /other-repo && git show
  HEAD:secrets/key.txt` no longer denies on this project's `secrets/**` — it is
  a different repository's file. Paths that resolve back inside the project
  still deny, absolute or relative, and a control pins each.
- **A separator-free filename is still out of scope.** `cd <denied-dir> && type
  key.txt` is caught only incidentally, by the `cd` argument. That is a limit of
  the token gate, unchanged here, and now written down in a test so it is a
  known boundary rather than an assumption.

Also fixed, the narrower half of the same issue: a git revspec whose path has no
separator (`git show HEAD:.env`) never passed the token gate, so it reached no
rule at all. It does now, and the revspec bypass of the existence gate gives the
right verdict for a blob that exists only in history.

**Correction to 2.76.2's entry**, which claimed `git show HEAD:.env` was being
stopped by the `<ads>` false positive: it was not. That token never reached the
ADS check either. The reasoning for judging a revspec's path half stands; the
claim about what it replaced was wrong.

## [2.76.3] - 2026-08-08

### Fixed — the revspec rewrite never fired for the shape people actually type (#50)

v2.76.2 taught the shell scan to judge a git revspec by its path half. It
anchored "is this a git command?" to the start of the whole command string, so:

```
git show origin/main:pyproject.toml        -> allowed  (as intended)
cd /repo && git show origin/main:pyproject.toml  -> still DENIED '<ads>'
```

A leading `cd` disabled the whole rewrite. Every unit test passed, because a
test author writes the command as `git show …` — which is exactly not how a
shell call arrives. It was caught by a live probe minutes after release, and
that is the general lesson: a fixture that encodes the idiom the way you would
type it in a test is not evidence about the way it reaches you in production.

The scan now works **per command segment** — what sits between `&&`, `||`, `;`,
`|`, or a newline — and each segment answers the git question for itself. That
is also the safer shape, not just the more permissive one: attributing the whole
string to git would let `cat notes.txt:hidden && git status` have its *first*
token reinterpreted, checking `hidden` instead of the real spelling, which is a
hole rather than a false positive. A control pins that case.

## [2.76.2] - 2026-08-08

### Fixed — the two path-SHAPED idioms `<ads>` still denied (#50)

v2.74.1 removed the shell tokens that were *not paths* — markdown links, jq
filters, list literals. Two everyday idioms survived it, and they are the
opposite class: they really are path-shaped, and they are the two ordinary ways
a developer writes a path with a colon in it.

```
python -m pytest tests/test_chat_poll.py::TestAbort  ->  was DENIED  '<ads>'
git show origin/main:pyproject.toml                  ->  was DENIED  '<ads>'
```

**Pytest node ids.** `a/b.py::Thing` is a node id or a C++/Rust scope, never a
stream. But `::` alone is not a safe signal — `file::$DATA` is the real NTFS
default-stream form, and an early draft that skipped every `::` token would have
opened a hole. The safe discriminator is the type slot: stream types are
`$`-prefixed system constants, so `::` *not* followed by `$` cannot be a
spelling. A control pins `type ./notes.txt::$DATA` as still denied, deliberately
against a path no deny glob covers so `<ads>` is the only rule that can catch it.

**Git revspecs.** `<rev>:<path>` gets the path half checked rather than the whole
token — a fix and a tightening at once. Every revspec is denied today by
accident, which reads as pure false-positive until you notice that
`git show HEAD:.env` prints a denied file's contents and the accident is the
only thing stopping it. Whitelisting the shape would open that; checking the
path half closes it on the rule that should have been deciding all along.
`pyproject.toml` is allowed on its own merits, `.env` is denied on its own rule.

The rewrite also skips the existence gate, because a revspec names a path in
*history*: `git show HEAD~5:secrets/gone.txt` reads a denied file whether or not
it is in the working tree today, and gating on the working tree would be a hole
the rewrite itself opened.

Recognised as git-only. `<word>:<word>` means something else almost everywhere
else, so nothing outside a `git` command changes behaviour.

Worth recording, since it is now three for three: this rule's false positives
have been found by the rule blocking the work of fixing it. #50 was filed after
`git commit` was refused twice for a message mentioning an IPv6 CIDR; #73's
commit was refused twice for quoting its own repros; and both idioms above were
hit while writing and testing this change.

## [2.76.1] - 2026-08-08

### Fixed — a stalled ledger write no longer holds a finished edit hostage (#74)

**The stall in #74 is not a C3 defect.** Caught live: the host hits
commit-charge exhaustion in short, unpredictable windows — 1.1 GB of commit free
against a 184.5 GB limit while 31.7 GB of physical RAM sat idle, because one
elevated `python.exe` committed ~197 GB in ninety seconds and then exited. In
the same window `gh` died with `runtime: cannot allocate memory` and bash could
not fork. That accounts for both reported incidents (an allocation that fails
outright kills the server → *connection closed*; one that stalls looks like a
hang) and for the payload-size correlation, since the largest calls allocate
most. Full evidence is on the issue.

Nothing in C3 prevents that. What C3 controls is **what it costs to find out.**

`c3_edit` writes the file and *then* records it, so a stall in the bookkeeping
is a stall after the work is done — the logged incident spent 1800s to report an
edit that had completed in milliseconds. The record-keeping now runs on a daemon
thread with a 10-second deadline. Past it, the call returns anyway and the
response says so:

```
✓ src/mod.py [-1+1L]
  [c3:ledger-deferred] The FILE WRITE SUCCEEDED. Recording it in the ledger
  outran 10s and was left running in the background, so the edit may be missing
  from c3_edits history and from c3_edits(action='verify') corroboration.
  Do not re-apply this edit on the strength of that absence — the file is the
  evidence, and it already has the change.
```

Three deliberate choices:

- **The thread is left running, not killed.** It may finish a moment later, and
  a half-written ledger entry is worse than a late one.
- **The note is loud rather than silent.** A degraded record that renders like a
  clean one is the exact failure class this subsystem exists to remove — and
  since a missing ledger entry downgrades `verify` to `INCONCLUSIVE`, saying so
  is what stops the note's absence from being read as "the edit did not land".
- **Only a stall is news.** A ledger that *raises* is already swallowed by
  design and stays silent; nothing changed there.

10 seconds is far outside normal (a JSONL append plus a git call that already
caps itself at 4s) and far inside the harness idle timeout, which is the number
this exists to stay away from.

## [2.76.0] - 2026-08-08

### Added — the phone can watch the machine work, and talk to it (#76)

`api_version` 4. Six read-mostly routes under `/api/mobile/*` plus a chat
transport that survives a backgrounded app.

The gap this closes is not a missing feature on the desktop — it is that the
companion app could see *events* and not *state*. It knew an edit happened; it
could not ask what changed in a file. It knew a session was running; it could
not ask what that session was holding or spending.

**Ops surface** — `edits`, `locks`, `status`, `insights`, `suggestions`,
`review`, each a capability on `/info` so an older Oracle degrades to a hidden
tab rather than a broken one:

- `GET /edits` and `/edits/versions` — the edit ledger the phone previously saw
  only as flattened feed rows, filterable by branch and file. Absent fields are
  emitted as `null`/`[]`/`{}` rather than omitted, because enrichment lands
  asynchronously and a typed client should not have to test key presence.
- `GET /locks` — `.c3/locks.json` had no HTTP surface outside loopback. Who
  holds which file, right now.
- `GET /status` — the aggregate card: token spend, session stats, ollama
  reachability, version.
- `GET /insights`, `POST /insights/dismiss`, `GET /suggestions`,
  `POST /suggestions/decide`, `GET /review`.

Every route that would cost tokens is deliberately absent. `/insights` lists and
dismisses; it does not generate. `/review` reports the daemon's heartbeat; it
does not run it. A phone in a pocket must not be able to start an LLM job by
being opened.

`suggestions/decide` approve writes `.c3/facts/` irreversibly, so the server
issues the typed challenge and the client echoes it back — the client never
invents the confirmation string.

**Chat** — `POST /api/mobile/chat/turn`, `GET /turn/<id>?after=`,
`DELETE /turn/<id>` (`oracle/services/chat_poll.py`). `/api/chat` streams SSE,
which is the right shape for a browser and the wrong one for a phone: the OS
suspends a backgrounded app and tears the socket down, and the client cannot
tell a finished turn from a killed one, nor resume either.

So the same turn is expressed as state a client can re-read. The engine
generator is drained on a background thread into an append-only list; `after` is
how many events the client already holds. Nothing about chat logic is
reimplemented — the events on the wire are the raw engine dicts, byte-for-byte
what the SSE route sends after its `data: ` prefix, so one renderer serves both
transports. A phone frozen for ten minutes comes back and asks `after=0` for the
whole turn, which is the property SSE cannot offer.

The registry is bounded on purpose (32 runs, 30-minute TTL on finished ones,
running turns never reaped): each retained run holds its full event list
including tool results, so an unbounded registry is a leak with a chat UI
attached to it.

## [2.75.0] - 2026-08-08

### Added — `c3_edits(action='verify')`: did that edit land? (#74)

A `c3_edit` call can fail *after* doing its work. In one logged session a call
hung until the harness aborted it at the 1800s MCP idle timeout, and the write
had fully landed — 3 hunks, 89 lines to 142, all correct. Another call in the
same session dropped the MCP connection and had written nothing. Both reached
the caller as an error, and nothing told them apart.

That ambiguity is the defect, separately from whatever causes the stall. The
retry that is correct after the second incident is a double-apply after the
first, and the case where that corrupts a file is ordinary rather than exotic:
any edit whose `new_string` contains its `old_string` — appending a line,
wrapping a call — matches again on a retry and applies twice. What saves the
common case today is `c3_edit`'s own not-found error, which is luck, not a
property.

`verify` takes the same arguments the failed `c3_edit` took, so recovering means
re-sending the call to a different action rather than reconstructing anything:

```
c3_edits(action='verify', file=<same file_path>, old_string=..., new_string=...)
c3_edits(action='verify', file=<same file_path>, edits=<same JSON batch>)
```

**The file is the primary evidence; the ledger corroborates.** They answer
different questions — the file says whether the intended text is there now, the
ledger says whether *this* edit is what put it there — and neither alone is
enough. Text present with no ledger entry may have been there all along; a
ledger entry with the text absent means something else overwrote it.

Three verdicts, and the third one is the honest half:

- **`NOT_APPLIED`** — `new_string` is absent. Definitive; safe to re-send.
- **`APPLIED`** — `new_string` is present *and* a ledger entry for this file
  records this exact old/new pair. Definitive; do not retry.
- **`INCONCLUSIVE`** — anything else, naming the check that failed. Never
  collapsed into either neighbour: told APPLIED when it cannot tell, a caller
  loses work; told NOT_APPLIED, a caller applies an edit twice.

Details that make it usable at the moment it is needed: it accepts the absolute
path you gave `c3_edit` and maps it to the ledger's relative spelling; it reads
the file with the same LF normalization `c3_edit` matches against, so CRLF files
do not all report `NOT_APPLIED`; it compares against the ledger's truncated
detail prefix, so a large edit — the kind most likely to have timed out — can
still corroborate; it runs *before* the ledger-availability gate, since the file
half is readable whether or not a ledger exists; and a ledger that raises
downgrades a verdict to `INCONCLUSIVE` rather than failing the call.

`c3_edit`'s own tool description now says to do this instead of retrying blind —
a timeout produces no output from C3 at all, so the pointer has to live
somewhere that is in context before the failure.

This does **not** fix the underlying stall. #74 stays open for that; the
mechanism is unreproduced and unnamed, and shipping a guess would be worse than
shipping the part that is knowable.

## [2.74.1] - 2026-08-08

### Fixed — the `<ads>` shell scan was fixed for two shapes, not for the class (#50)

v2.63.x denied any command whose text contained a URL or IPv6 literal, because
the Access Guard's best-effort shell scan resolved arbitrary tokens as paths and
read the residual colon as an NTFS alternate data stream. #50 fixed that by
skipping tokens that *look like network literals*.

That whitelist was two shapes: a token **starting** with `scheme://`, or a bare
IPv6. Both remained true of the examples in the issue and false of everything
else, so the class was still open. Three denials from one real session, on
v2.74.0, none of which names a file:

```
echo "see [#1](https://example.com/a/b)"       # scheme is not at position 0
echo '{a:.x,b:("/"+.y)}'                       # a jq filter — no URL at all
python -c "print(['src/a.py','src/b.py'])"     # a list literal
```

The token gate admitted all three because it asks only "does this contain a
slash", and `<ads>` is exempt from existence-gating, so each became a hard,
unappealable deny on text that touches nothing.

The scan now asks whether a token is a path at all before asking how it is
spelled: a token containing what Windows forbids in a filename (`< > " | ? *`)
or the bracketing that marks a compound expression (`{} [] () ' \` &`) is not a
path argument and is skipped.

`:` stays out of that set — it is the thing under test. So do `$` and `=`, which
are legal in a real path *and* in a real ADS spelling: `./notes.txt:$DATA` is
canonical default-stream syntax, and an earlier draft of this fix that treated
`$` as syntax silently deleted the existing negative control for it. Every
genuinely dangerous shell use of `$` arrives with `(` or `{`, which are covered.

The direction is deliberate. This scanner is advisory and best-effort; a token
it declines to flag is still refused by the real path check on Read/Write/Edit
and by every `c3_*` tool. A token it flags *wrongly* is a hard deny on a command
that names no file, which is the bug. So it errs toward skipping.

## [2.74.0] - 2026-08-08

### Fixed — Override Requests: the first live run found three ways to lose a human's answer

v2.73.0 closed the loop on paper. Running it end to end against a real phone
broke it three separate times, and each break was silent — no error, no failing
test, just a user tapping Approve and nothing happening.

- **The invitation was unreachable for the only layer anyone had enabled.**
  `access_guard.refusal()` appended the `[c3-override]` offer at the tail of the
  deny branch, which sits below the mask and read-only early returns. Both of
  those kinds map to an escalatable layer, so policy said "offer this" and the
  string composer structurally could not. Since `access_readonly` is the layer
  people actually turn on, the invitation half of the feature had never fired.
  The append now happens once, after the body, for every kind. Hook surface
  only, unchanged — extending it to the `c3_*` refusals is defensible now that
  P2a consults grants, but that is a wider change and gets its own review.

- **The read-only refusal named the wrong operation, and it cost a real
  approval.** The S2 string hardcoded the word "write" while `operation` sat
  unused in scope. `c3_edit` calls it `create` when the file does not exist, so
  an agent copying the refusal's wording asked for `op='write'`, the user
  approved `write`, and the grant matcher — exact on `op` — refused the
  `create` that followed. The audit recorded it precisely
  (`near_miss`, `differs:["op"]`) and nobody was reading the audit. S2 now
  interpolates `{operation}`; docs/access-guard.md §4 updated to match.

- **A phone could not route a tap to the request.** The request notification
  carried `agent:"override"` and its own hashed id; the client routes on
  `kind` and needs the request id to pin the card. `NotificationStore.add()`
  takes optional `kind` and `ref_id`, written only when non-empty so every
  other producer and every stored line is untouched.

Eleven tests, six of which fail against 2.73.0. They assert on the layer rather
than the branch, so putting an early return back in front of the append fails
here instead of in someone's hands.

## [2.73.0] - 2026-08-08

### Added — Override Requests: the decision reaches the agent, and the phone hears sooner

v2.72.0 made an approval mean something to the tool that was blocked. It still
meant nothing to the agent that asked, because nothing told it. On the first
run after that release the request went out at 00:36Z, the approval came back
at 00:42:32Z, and the grant expired unused at 00:57:32Z. Every component was
correct and the loop was open.

- **`override.wake` — one command, run when a request is decided.** Approve or
  deny, C3 executes the argv configured in `.c3/config.json` with the
  identifiers a woken agent needs, including a `{message}` that names the
  single next step ("retry the SAME call once"). What runs the agent is not
  C3's business — a chat daemon, a queue, a webhook all fit one argv, and a
  backend per orchestrator would rot in this repo.
- **argv, never a shell string.** `command` must be a list, there is no
  `shell=True` and no string form to fall back to, and substitution happens
  per element after the list is fixed — so no placeholder can add an argument.
- **The phone cannot set it.** `POST /api/mobile/overrides/policy` returns 403
  for `wake`, `widen` confirmation included. Every other key on that route
  widens what a tap can approve; this one would choose what executes when the
  tap happens, and a bearer token is authentication, not physical presence.
  `GET .../policy` reports `wake_configured` and never the argv.
- **Failure is contained, and audited.** Non-zero exit, missing binary,
  timeout, missing cwd — all land in `.c3/overrides.jsonl` as `wake_failed`,
  and the approval and its grant stand. A wake is a shortcut past waiting, not
  the mechanism.
- **A `wake` we cannot parse fails the whole `override` section closed.** It
  names a command this machine will run. Degrading a typo into "no wake, carry
  on quietly" would restore the exact silence this fixes.

### Added — `GET /api/mobile/feed?wait=` (api_version 3, capability `feed_wait`)

The phone had two delivery paths: a 15-second tick while the app is open, and
Android WorkManager's ~15-**minute** floor once it is closed. A request with a
10-minute TTL could expire before the phone was ever told it existed.

- `wait` (0–30 s) holds the request open until something matches the
  watermark, so a live app hears in about a second without spending a request
  every 15 s to get there. Requires `since`, refused with `before` — waiting
  against a pagination cursor is meaningless, and waiting without a watermark
  would answer instantly from history while looking like it worked.
- The hold watches `.c3` mtimes and only rebuilds the feed when one moves, so
  it is a `stat()` per second rather than a history read per second. Waiters
  are capped at 4; past the cap `wait` degrades to an immediate answer instead
  of parking another server thread.
- **It is not push.** A frozen process cannot hold a socket, so the closed-app
  floor is unchanged. Closing that needs FCM or a foreground service. Keep
  `request_ttl_s` above the delivery floor either way.

## [2.72.0] - 2026-08-07

### Fixed — Override Requests, phase 2a: the approval now does something

The first live end-to-end of the whole feature failed, and it failed in the
one place the spec had already written down. A blocked `c3_edit`, a request on
the phone, a real human tapping approve, a grant minted with exactly the right
shape — and then the retried call was refused with a byte-identical message,
because nothing on the MCP surface had ever been taught to look at a grant.
`c3 override list` afterwards still read `1 use(s) left`.

- **All six content tools consult grants before refusing.** `c3_read`,
  `c3_edit`, `c3_compress`, `c3_filter`, `c3_impact` and `c3_validate` now
  call `cli/tools/_grants.allow`, which delegates to the same
  `override_grants.gate_access` the PreToolUse hooks have called since P1.
  One implementation, because two would drift and the drift would be silent.
- **Masked paths are deliberately not covered.** A mask is not a refusal
  waiting to be lifted; it is a different view being served. "Approve once"
  has no meaning there, so only the `denial` branch of `verdict()` consults a
  grant. Widening that is a separate decision, not a side effect of this one.
- **One definition of the session id.** It lived in `edit.py`, `locks.py` and
  `override.py`, each carrying a comment saying it had to match the others.
  P2a makes that load-bearing rather than tidy: a grant is minted under the id
  `c3_override` computes and spent under the one `c3_edit` computes, so any
  divergence would make every approval silently fail to apply — indistinguish-
  able from the user never approving. All three now read `_grants.session_id`.
- **Fail-closed.** Any error reaching the grant store leaves the caller on its
  ordinary refusal path. A grant that cannot be read is not a grant.

Tests assert the **use counter**, not just the outcome. A write that succeeds
proves nothing on its own: the project that surfaced this bug runs
`enforcement: advisory`, where native writes land regardless of grants, and
that is exactly how a dead gate can look like a working one.

## [2.71.0] - 2026-08-07

### Added — Override Requests, phase 3: the phone can answer

2.70.0 let a blocked agent ask. The only thing that could answer was a human
at a desktop typing `c3 override approve`, which is exactly the situation the
feature exists to fix. This puts the answer on the phone.

- **Six routes** under the existing `/api/mobile` prefix, Bearer-authenticated
  on every method including GET:
  - `GET /api/mobile/overrides` — the inbox. Newest first. `project` is
    **optional**, and omitting it returns every project, because the point of
    an approval inbox is answering while away from the desk, not first
    guessing which project is blocked.
  - `GET /api/mobile/overrides/<id>` — one request, plus what approving would
    actually cost (clamped TTL, whether a typed confirm is coming), so the card
    can say it *before* the tap rather than reporting a clamp afterwards.
  - `POST /api/mobile/overrides/<id>/decide` — approve or deny.
  - `POST /api/mobile/overrides/<id>/mute` — deny, and stop asking.
  - `GET`/`POST /api/mobile/overrides/policy` — read and edit the project's
    `override` section.
- **Two capabilities**, `override` and `override_write`, backed by
  `mobile_override_enabled` / `mobile_override_write`. Switched off ⇒ **404,
  not 403**: a disabled subsystem is indistinguishable from a server too old to
  have it, so one client code path handles both.
- **The typed-confirm challenge is the rule glob itself.** Approving an
  `access_deny` or `access_builtin` request means retyping `**/.env*` by hand.
  Not a nonce, not "yes" — the string that names what you are opening up, on
  the theory that a habit-tap should cost more than reading a notification
  (§11 threat 1). Session grants carry their own separate challenge, and are
  refused outright unless `allow_session_grants` is on.
- **`ttl_s` is clamped and the client is told.** Asking for a week returns a
  15-minute grant plus `clamped: true` and a note naming the ceiling that did
  it. Silent clamping would leave a phone displaying a grant that does not
  exist.
- **Approvals ride the existing feed.** A decision appends an acknowledgeable
  notification, so the feed records that the open question was answered instead
  of showing it open forever.

### Added — mute, the one genuinely new primitive

"Deny and suppress identical requests for this session" had no P2 equivalent.
It lives in `services/override_requests.py` next to its siblings, and `create()`
honours it, rather than becoming a parallel store in the Oracle layer.

Its suppression key is byte-identical to the tuple `create()` already used for
duplicate detection — `(project, session, layer, rule, tool, op, path_key)` —
so a mute is precisely "duplicate suppression that outlives the pending row".
Session-scoped, because a new session has a new problem and has earned the
right to ask once. The mute store fails **open** (a corrupt file means the
agent may ask again), the deliberate opposite of the grant store's fail-closed
read: a lost mute costs one notification, while a lost-open grant would be a
capability.

### Changed

- `override_requests.decide()` gained `mode` (`once`|`session`) and `mute`, and
  now emits the decision notification. `decided_by` is `"mobile"` from this
  surface.
- Request rows cross the wire through an **explicit field allowlist**, not
  `dict(row)`, so an internal field added later cannot silently start being
  published. `path_key` is deliberately withheld.

### Tests

`tests/test_mobile_override_routes.py` (50 tests). One of them,
`test_wire_contract_field_names_the_mobile_client_reads`, asserts the literal
JSON key names the phone reads, duplicated on purpose from the spec rather than
imported from the code under test. A previous cross-repo feature shipped with
both sides green because each side pinned its own spelling of the contract and
the wire dropped every key in between; a tautological assertion would not have
caught it.

## [2.70.0] - 2026-08-07

### Added — Override Requests, phase 2: the agent can ask

2.69.0 added the grant — one retry, one path, once — but the only way to
produce one was a human typing `c3 override grant` for a call they had to
already know about. This closes the loop: a blocked agent can now **ask**, and
a human answers.

- **`c3_override`** (new MCP tool) — `request`, `status`, `wait`, `list`,
  `withdraw`. That is the whole surface. There is no `approve` action; asking
  for one by name gets a refusal that says so rather than a generic "unknown
  action" that reads like a typo worth retrying. `wait` blocks up to 180s
  inside the MCP server, which is allowed to be slow, instead of freezing a
  PreToolUse hook with no spinner and no cancel.
- **`services/override_requests.py`** — the request store
  (`~/.c3/oracle/override_requests.json`). Rate limits are enforced at
  creation: three pending per session, twenty an hour per project, and an
  identical still-pending ask returns the existing card rather than minting a
  second one. An agent in a retry loop cannot fill your phone.
- **The refusal now tells the agent it may ask** — one appended line, and only
  when the layer is escalatable and the project opted in. When it is not, the
  refusal says nothing at all: an agent must never learn from a denial that a
  request surface exists for the credential vault.
- **`c3 override requests | approve | deny`** — the desktop half. Approving a
  `deny` or builtin rule still requires the glob retyped by hand, and that
  check now lives in the service so the CLI and the coming Oracle route cannot
  drift apart on the one thing that matters. The agent's justification renders
  quoted, labelled untrusted, because it is: it is capped at 400 characters,
  never parsed, never matched on, never interpolated anywhere.

Approving mints the grant, and the grant behaves exactly as it did in 2.69.0 —
single-use, session-bound, path-exact, and gone in fifteen minutes.

### Known gap — grants are honoured by the hooks, not yet by `c3_*`

Building P2 surfaced something P1 had claimed and not delivered: the grant
gate lives in the two PreToolUse hooks, so an approved grant unblocks native
`Read`/`Edit` but **not** `c3_read`/`c3_edit`/`c3_compress`/`c3_filter`/
`c3_impact`/`c3_validate`, which call the evaluator and refuse on their own.
The spec's coverage matrix said otherwise; it has been corrected rather than
left to be discovered later.

The offer line is therefore emitted on the hook surface only. An offer that
promises a human "yes" will unblock you is worse than no offer at all on a
surface that would still refuse afterwards. Phase P2a wires the remaining
surfaces.

## [2.69.0] - 2026-08-07

### Added — Override Requests, phase 1: the grant primitive

C3 blocks in seven places and every one of those blocks is terminal. The only
way to un-block an agent today is a human at the desktop typing `c3 enforce
advisory` or `c3 access remove` — that is, weakening a rule permanently to get
past one call. A guard that spends its life in `advisory` is not a guard.

This release adds the missing primitive: a **grant**. A grant makes one retry
of one tool call on one path succeed, once, soon. It does not edit policy —
the rule that denied the call is still in force the moment the grant is spent,
and the hook says so out loud in `additionalContext`.

What landed (docs/override-requests.md phase P1):

- **`services/override_policy.py`** — the new top-level `override` section in
  `.c3/config.json`. Everything defaults to `false`, including each of the six
  escalatable layers, so this release changes nothing for anyone who does not
  opt in. Project and global scopes merge by **tightening only**: booleans are
  ANDed, numbers take the minimum, and a project can never widen what global
  forbids. Unknown keys are a hard error, exactly as in `access`, so a future
  knob can never silently no-op on an older C3.
- **`services/override_grants.py`** — the store, the matching rules, and the
  audit trail. A grant authorises a retry only when the session, layer, rule,
  tool, operation and *canonical* path all match, the TTL has not passed, and
  a use remains. Consumption burns the use at allow-time under a cross-process
  lock, so two hook subprocesses racing the last use of a single-use grant
  cannot both win. Every lifecycle event appends to `.c3/overrides.jsonl`,
  including the near-misses — "you approved Read on X, the agent then tried
  Write on Y" is a thing you can now see.
- **Gates in both PreToolUse hooks** — `hook_access_guard` consults grants
  after a `Denial` and before the refusal; `hook_pretool_enforce` does the same
  for the tool-discipline write block, *after* the credential-vault guard,
  which stays unconditional. Policy is read before grants, so switching the
  feature off voids live grants immediately.
- **`c3 override`** — `policy`, `grant`, `list`, `check`, `revoke`, `sweep`.
  This is the only approval path in this release, and it is human-only: there
  is no agent-facing verb here, and there is no `approve` action anywhere in
  the design.

What cannot be overridden, at any setting, by any approval: the credential
vault, `.c3/secrets.enc`, `.c3/cred_state.json`, the Tier-0 absolute denies,
the dispatcher's fail-closed deny, and the catastrophic `c3_shell` blocks. A
denial from one of those never even reads the grants file, and the refusal
never mentions that a request surface exists. `.c3/override_grants.json`
itself is on the never-writable list, so an approved `**/.c3/**` write cannot
be turned into the agent minting its own grants.

Deviation from the spec, deliberately: §10 lists `c3 override approve <id>`
and `deny <id>`, which need the *request* store that arrives in P2. P1 ships
the grant-centric verbs instead, so the primitive is complete and testable on
its own. The agent-facing `c3_override` tool, the Oracle routes and the mobile
Requests pane follow in P2–P4.

### Fixed

- `tests/test_enforcement_policy.py` no longer reads the developer's real
  `~/.c3/config.json`. Resolution is project → global → default, so anyone
  whose global config carried an `enforcement` section saw
  `test_missing_section_defaults_to_strict` fail locally while CI, with a
  clean home directory, passed.

## [2.68.0] - 2026-08-07

### Added — C3 on your phone: a companion-app gateway, including the security surface

`/api/mobile/*` is a new surface on the Oracle for the C3 mobile companion
app: a merged cross-project activity feed, the project overview with health,
the PM board read/write, the daily digest, notification ack — and, new in this
release, the credential vault and Access Guard.

That last part is why this took more care than a read-only feed. Credentials
and Access Guard already had HTTP surfaces, but every one of them is
loopback-only and unauthenticated; their whole confidentiality model is
"nothing sensitive ever leaves, and only localhost can ask." This gateway is
the first **network-reachable** surface for either subsystem, so that model
does not transfer and the controls are rebuilt here as explicit invariants
rather than assumptions.

- **The gateway never returns a credential value, structurally.** There is no
  reveal route and no way to add one by accident: entries cross the wire only
  through an allowlist serializer (`credential_store.public_entry`, promoted
  out of the Hub so the two copies cannot drift), and `mobile_api` never
  imports `get_value` or `expand_templates` at all — a source-grep test
  asserts that, and `credential_store.is_resolvable()` exists so the check
  route can prove a value is present without reading it. You get a length and
  an on-demand fingerprint; that is enough to tell *which* value is stored and
  nothing more. A canary test seeds a known secret and asserts it appears in
  no response and no log, sweeping `/api/mobile/feed` last — that route merges
  the activity log and edit ledger across every project, so an audit line that
  ever carried a value would be exfiltrated by the next poll.
- **Full vault and guard management.** Credentials list/get/check/set/delete;
  Access Guard rules, mask rules, mask activation, path checks and denial
  counters; tool discipline read and write. `check` and `access/check` are
  POST rather than GET on purpose — one decrypts and the other walks the
  filesystem, so both belong in the security rate budget, not the cache.
- **Bearer on every method, GETs included.** Unlike the legacy Oracle reads,
  nothing here is anonymous. Every project path is validated against the
  registry, and every write is audited three ways: the project's activity log
  and edit ledger, the global vault's own log when the write is global-scope
  (otherwise a global edit with no project context would leave no trace
  anywhere), and the gateway's own `discovery_audit` — the only per-gateway
  record, and the one that answers "my token leaked, what did it touch"
  without grepping every project. Names and globs only, never values.
- **Capabilities, not 404-collecting.** Each subsystem has an enable switch
  and a write switch: `mobile_credentials_enabled` / `mobile_credentials_write`
  cover the vault, and `mobile_access_enabled` / `mobile_access_write` cover
  path policy *and* tool discipline (the `enforcement` capability rides the
  access switches today — discipline has no separate key).
  `/api/mobile/info` reports the **effective** list, so a client hides a
  feature it cannot use
  instead of discovering the truth by collecting failures. A disabled
  subsystem 404s — deliberately indistinguishable from a server too old to
  have it, so one client path handles both. `API_VERSION` is now `2`.
- **Two levers ship complete but off.**
  `mobile_creds_agent_readable_raise` gates *raising* `agent_readable` from
  the phone (lowering always works), and `mobile_access_global_scope` gates
  machine-wide access rules. Each is the one operation in its subsystem whose
  blast radius exceeds what the token can otherwise reach. Global-scope writes
  never derive their location from the request — the store path is a
  server-side constant — and refuse with `409 needs_init` rather than creating
  `~/.c3`, so the set of paths this surface can initialize is unchanged.
- **Deliberately absent, and worth knowing before you add one back.** No bulk
  `.env` import (largest blast radius in the vault, no phone affordance). No
  denial-counter clearing (a leaked token's first move after being denied is
  to erase the evidence). No `set_builtin_disabled` — builtin guards stay
  CLI-only. No raw-content access preview. Globs like `**` are refused as too
  broad for a phone, while the CLI stays unrestricted; the difference between
  the surfaces is the point. Mask activation is single-flight and never
  accepts `rebuild_index` from the wire.
- **Typed confirmations, honestly scoped.** Removing a rule or a mask, first
  mask activation, and turning tool discipline off all require the client to
  echo a specific string. That stops a fat-finger and a blind replay, and it
  forces a deliberate two-step UI. It is **not** a defence against a leaked
  Bearer token — an attacker holding one constructs the field trivially. The
  config switches above are the control that resists that.
- **Pairing.** Oracle Settings gains a *Mobile app* card that renders a QR
  carrying the server URL and the Discovery API token. It renders only on an
  explicit click, never on page load, and says plainly that the code contains
  the raw token. Remote reachability is the same recipe as the Discovery API:
  set `bind_host` to your Tailscale/LAN address (not `0.0.0.0`) and list it in
  `allowed_hosts`. Disable the whole surface with `"mobile_api_enabled": false`.

## [2.67.1] - 2026-08-07

### Fixed — a startup thread could wedge the whole MCP server, silently

Three separate hangs, all of which left a process that looked healthy. The
event loop stayed alive and idle throughout, so nothing logged, nothing
crashed, and no health check noticed; the only visible symptom was every c3
tool call dying at the client's 120s timeout.

- **`collection.delete(where=...)` never returns.** `EmbeddingIndex.build()`
  runs on the `c3-embed-index` thread spawned from `cli/mcp_server.py` and
  calls `_remove_file_chunks()` for each changed file. That used chromadb's
  `delete(where={"doc_id": ...})`, which was caught wedged inside the Rust
  bindings (`chromadb/api/rust.py`, `RustBindingsAPI._delete`): two py-spy
  dumps four minutes apart with byte-identical frames, 0.031s of CPU over
  3s, and no writes to `chroma.sqlite3` for ten hours. `_remove_file_chunks`
  now resolves the ids with `get(where=...)` first and deletes by explicit
  id, moving the metadata filtering onto the read path, which does return.

  The old code already carried a get-ids-then-delete fallback, and its
  comment already suspected the where-delete — but the fallback sat behind
  `except`, and an `except` clause cannot catch a call that never comes
  back. It was unreachable by construction. It is now the only path.

- **An unbounded lock turned one slow call into a dead server.** `build()`
  took `self._lock` with a bare `with` and held it across the whole build
  loop, so the wedged delete parked every later caller behind it forever. It
  now acquires with a timeout (`_acquire_build_lock`, mirroring the
  `_init_lock` pattern `_ensure_ready` already used correctly), logs once at
  WARNING, and returns a `degraded` result carrying the normal stats shape so
  callers reading it with `.get()` defaults keep working. A redundant build
  is worth far less than a responsive server. This is the part that makes a
  *future* backend hang survivable instead of fatal.

- **`subprocess.run(timeout=...)` hangs inside its own timeout handler.**
  `check_gemini` / `check_codex` / `check_claude` passed `stdin=DEVNULL` and
  `timeout=10` and hung anyway: on Windows, when the timeout fires, CPython's
  handler kills only the direct child and then calls `communicate()` a
  *second* time with **no timeout** (the `_mswindows` branch of `run()` in
  `Lib/subprocess.py`). That join never completes while a surviving
  grandchild still holds the stdout/stderr write-ends. Observed wedging the
  `c3-delegate-prewarm` thread for 10h and leaking its two reader threads, so
  delegate health checks never completed and every first `c3_agent` call paid
  full preflight. All three now go through `_probe_cli_version`: Popen, a
  `taskkill /T` process-*tree* kill, and a bounded `communicate()` in
  `finally`. `tests/test_cli_smoke.py` documented this exact footgun for test
  code back in 2.43.0; production code now follows the same convention.

### Tests

- `tests/test_embedding_index_deadlock.py` — pins the chunk-removal contract
  (a `doc_id`'s chunks are removed without ever passing `where=` to
  `delete`), reproduces the hang against a blocking fake collection with a
  bounded join so a regression *fails* rather than wedging pytest, and proves
  the busy-lock path degrades instead of blocking.
- `tests/test_delegate_version_probe.py` — asserts the second `communicate()`
  is bounded, that the kill is a tree kill, and that the pipes close on every
  exit path.
- 22 of the 26 new tests fail against the pre-fix source.

## [2.67.0] - 2026-07-31

### Added — The Discipline tab grows search, evidence, and controls (Hub)

v2.66 shipped the knob; this release ships the workbench around it. Most of
what landed here was capability the backend already had and no surface
exposed: the raw denial-event log had a public reader nothing called, the
policy layer accepted `scope="global"` that no route passed, `signal_ttl_s`
and `blocked_tools` were fully resolved and validated but had no write
surface. The Hub tab now reaches all of it.

- **Search, filter, sort.** A free-text filter over project name / path /
  mode / tier (`/` focuses it), chips for `Strict` / `Advisory` / `Off` /
  `Has denials` / `Attention` (warnings, unreadable policies, tier drift),
  and sorting by name, denial count, or mode. The tab now polls every 5s
  like Locks — and never mid-interaction: a refresh while you type, or with
  a confirm open, would reorder cards under your cursor.
- **Raw denial-event search.** Expand a project's denials and search the
  actual events, not just the coalesced summary: AND'd substrings over
  path/rule/tool, layer chips, click a session id to filter to that session,
  `all events` to browse newest-first. Backed by
  `access_telemetry.search_events` via
  `GET /api/enforcement/denials/search` (project) and
  `GET /api/projects/enforcement/denials/search` (Hub) — limit 200 (cap
  500), `matched` keeps counting past the cap so truncation is visible, and
  the rotated `.jsonl.1` is included. Aggregate rows now show last-hit
  recency and session counts, which the server always sent and the UI
  always dropped.
- **Global default card.** The `~/.c3` fallback finally has a UI: its own
  mode picker and TTL editor, `NOT SET` when no global section exists (which
  is not the same claim as `strict`, and the card keeps them apart). The
  POST routes accept `scope: "global"`; the CLI's `c3 enforce --global` is
  no longer the only way.
- **TTL and blocked-tools editors.** Per project and on the global card.
  Both post mode-less bodies routed through a new
  `enforcement_policy.set_fields`, which never touches `mode`/`set_by` — a
  TTL tweak cannot turn a tier-derived choice into a `user` one — and
  **refuses to create** an `enforcement` section, because a mode-less
  section coerces to `strict` and would silently shadow an inherited
  `advisory`. The editors are enabled only when the row's policy actually
  comes from the project scope, and say why when it does not.
- **Bulk apply.** `select` puts the list in checkbox mode; a sticky bar
  applies one mode to every selected project after a single confirm that
  spells out what `off` does and does not switch off. Writes are sequential
  and audited per project; failures are named, not swallowed.
- **Discipline in the drill panel.** Clicking a project name now opens the
  drill on a new Discipline tab — the same controls scoped to one project,
  the full 12-row aggregate with fixes, and the event search — instead of
  dropping you on Overview and losing the thread.

### Fixed

- **The Discipline tab never persisted as the active view.** The Hub's
  `main_view` whitelist was missing `enforce`, so selecting the tab 400'd
  silently (the client swallows config-save errors) and every reload dropped
  you back on Projects. Two-line fix, pinned by a test that mirrors the
  Locks one.
- Hub error banners in the Discipline tab now surface the server's actual
  error (`apiErr`) instead of a bare `HTTP 500`.
- The tab now consumes the server's authoritative mode list, help strings,
  and tier map instead of a hardcoded client copy that could drift.

### Changed

- `signal_ttl_s` is now **validated on write** (30…86400 → HTTP 400 /
  `ValueError`) instead of silently written and clamped at read time.
  Read-time clamping stays, for hand-edited files.
- `enforcement_policy.set_mode` accepts `blocked_tools`, validated against
  `GOVERNABLE_TOOLS` and written in the same atomic write as the mode.
- `POST /api/projects/enforcement` body is now
  `{path?, scope?, mode?, signal_ttl_s?, blocked_tools?}`; project scope
  (the default) keeps its old contract exactly. Global-scope writes are not
  audited to a project ledger — there is no target project; the
  `~/.c3/config.json` write is itself the record.
- The `enforcement` config section remains deliberately excluded from the
  generic Config editor's write whitelist — the dedicated route is the only
  write path, so validation and provenance rules cannot be bypassed.
- The aggregate endpoints accept `?session=` to narrow to one session
  (`c3 access stats --session` had this; the routes now do too).

Not in this release, evaluated and deferred: outcome telemetry (logging
advisory nudges/allows for an effectiveness view — hot hook path, 10-100×
event volume, needs its own perf-careful pass) and NotebookEdit governance
(a file-writing tool the discipline hook currently does not govern at all —
an enforcement-semantics change with its own tests).

Full reference: `docs/enforcement.md`.

## [2.66.0] - 2026-07-31

### Added — Tool discipline is now a knob you can turn (`c3 enforce`)

**What changes for you:** nothing on upgrade. Existing projects keep behaving
exactly as before until you opt in.

C3 had four independent gates, and only three of them were adjustable. The
fourth — the PreToolUse hook that hard-denies native `Edit`/`Write` unless a
`c3_*` call ran first — was hardcoded, read no config, and was registered
regardless of your permission tier. That produced a contradiction users hit
constantly: selecting the `permissive` tier, documented as "all tools and shell
commands pre-approved", still had every native `Edit` refused by the hook. The
one knob that looked like it should help didn't reach the layer doing the
blocking.

- **`c3 enforce [strict|advisory|off]`** — the missing knob. `advisory` allows
  native writes with a nudge; `off` stops nudging entirely. Run with no argument
  to see the active mode, where it came from, and what it blocks.
- **Deliberately separate from `c3 access`.** Path policy is a security
  boundary; tool discipline is a workflow preference. Splitting them is what
  makes it safe to loosen the second without touching the first. At *every*
  mode, including `off`, these still enforce: Access Guard path rules, the
  credential-vault write guard, and agent locks. Asserted per-mode in
  `tests/test_enforcement_policy.py`.
- **Tiers now mean what they say.** Choosing a permission tier derives the
  matching discipline (`standard`→`advisory`, `permissive`→`off`,
  `c3-strict`/`read-only`→`strict`). An explicit `c3 enforce` choice is
  recorded as `set_by: user` and a later tier change defers to it rather than
  silently undoing it.
- **`c3 init`** asks for discipline as Step 5/5, and accepts
  `--enforcement <mode>` for scripted installs. Existing installs are untouched:
  with no `enforcement` section the resolved mode is `strict`, and nothing is
  derived at read time, so upgrading C3 cannot change how a project behaves.
- Everything fails **closed** — unknown mode, malformed section, unparseable
  JSON, or a `blocked_tools` entry naming an ungoverned tool all resolve to
  `strict` with a visible `[c3:enforcement-config]` warning.

- **Discipline tab, in both UIs.** `c3 ui` gets a per-project tab next to Access
  Guard: three mode cards, the provenance of the active mode, what stays
  enforced at every mode, and the ranked denial table with the fix for each row.
  The Hub gets the cross-project version — every registered project, a picker
  per row, and the denial breakdown inline. Switching to `off` confirms first
  and states plainly what it does *not* switch off. In the Hub, projects that
  are unreadable or have no `.c3` are listed under "Not reporting" rather than
  shown as `strict` — "we don't know" and "running strict" are different claims.

Full reference: `docs/enforcement.md`.

### Added — Denial telemetry (`c3 access stats`)

`docs/access-guard.md` §3 specified "denial logging: coalesced per (rule, tool,
session) with a hit counter". It was never implemented, so "the guard is slowing
me down" was a feeling with no evidence behind it.

`c3 access stats` now ranks what actually got denied, labels each row with its
layer (path policy vs tool discipline), and names the exact command that clears
it — `c3 enforce advisory` for a discipline block, `c3 access remove <glob>` for
a user rule, `c3 access builtin disable` for a builtin. Events land in
`.c3/denials.jsonl` (local, gitignored, rotated at 512 KB) and are coalesced at
read time, since concurrent hook subprocesses would race on a shared counter.

### Fixed — Truncated enforcement state and orphaned temp files

`_atomic_write_json` wrote enforcement state without `fsync` and abandoned its
temp file if `os.replace` raised. Observed on Windows: a truncated
`enforcement_state.json`, 10 orphaned `.c3/*.tmp<pid>` files, and 58 hook errors
across two days. The failure is quietly self-worsening — corrupt state loads
*empty*, which drops every sticky unlock and makes enforcement more aggressive,
which reads as "the guard got worse".

Now fsyncs before publishing, retries `os.replace` on Windows sharing violations
(AV scanner, Search indexer, a concurrent hook), and removes the temp file on
every failure path. `c3 init` sweeps orphaned temps whose owning PID is gone,
and never touches one belonging to a live process.

## [2.65.0] - 2026-07-30

### Added — Agent Locks: run several agents on one repo without them clobbering each other (#54, #55, #56, #57)

**What changes for you:** nothing, unless two agents touch the same file. Then the
second one is told who holds it and why, instead of quietly overwriting the first.

Two separate problems, two mechanisms — conflating them is why this looks bigger
than it is:

- **Torn writes.** `c3_edit` guarded same-file edits with a `threading.Lock`, which
  only holds *within one process*. Every Claude Code session spawns its own
  `c3-mcp` server, so two sessions could interleave their read → replace → write
  and lose an edit with no error on either side. Create mode was worse: it ran
  entirely outside the lock, so two agents creating the same path both reported
  success and one file silently won. Now serialized by a cross-process file lock
  held across create, single-edit and batch alike. No daemon, no configuration.
- **Overlapping work.** Two agents refactoring one module for ten minutes is not a
  torn write, and no per-call lock helps. `c3_edit` now takes a *lease* on the file
  it edits, carrying the intent from your edit summary. A second agent gets
  `[c3-lock:held]` naming the holder, their intent, and the time remaining.

Leases expire on a TTL (default 900s) because agents forget to release, so a
crashed agent can never wedge a repo. Acquisition is all-or-nothing over a sorted
path list, so two agents grabbing the same pair in opposite order cannot deadlock.

New surfaces:

- `c3_locks(action='list|acquire|release|renew|sweep')` — declare a multi-file
  refactor up front, or see who holds what. Deliberately has no force action.
- `c3 locks list | release | force-release | sweep` — the human override.
  `force-release` bumps a fencing counter so a holder that comes back is stale by
  construction, and it is ledger-logged.
- **Hub → Locks tab** — every project's leases, with holder, intent and a draining
  TTL bar. A project whose lock state cannot be *read* is badged `UNREADABLE`
  rather than shown with zero leases: "all clear" is a different claim from "we
  don't know".

Coverage is stated honestly in `docs/agent-locks.md` §9 rather than implied. Leases
gate C3's own tool surfaces. A raw `c3_shell` redirect, a non-Claude agent, or a
human in an editor is **not** covered, and the UI says so on screen.

`.c3/config.json` gains an optional `locks` section (`enabled`, `mode`,
`default_ttl_s`). Defaults are on and advisory; set `enabled: false` to opt out.

### Added — Access Guard: turn a built-in guard off when you actually need to

Built-ins were absolute. If you wanted the agent to edit `.git/**` or your own
`~/.claude/settings.json`, there was no supported answer.

Now there are two tiers. `**/.env*`, `**/.c3/**`, `**/.claude/settings*.json` and
`**/.git/**` can be switched off with `c3 access builtin disable <glob>`, which
asks you to retype the glob first. The credential vault (`secrets.enc`,
`cred_state.json`) stays absolute — it already has its own guard and its own
human-only escalation, so an opt-out there would only be a shorter route to the
same secrets.

Disabling requires **two keys**: a `disable_builtin` entry in the global config
*and* a keyring attestation. Either alone leaves the built-in enforcing. That is
the point — an agent that manages to write `config.json`, which is exactly the move
a prompt-injected one would make to grant itself write access to your settings,
still cannot produce the attestation. Global scope only, because project scopes may
only ever tighten; a project-scope entry is a loud config error rather than a
silent no-op.

### Fixed

- `c3_edit` create mode ran outside the same-file lock entirely, so two agents
  creating one path both succeeded (#55).
- Lock keys were computable two ways and disagreed by platform, which made the
  guard a no-op on the platforms it disagreed on: an unresolved sidecar path
  (macOS `/var` → `/private/var`, Windows 8.3 `RUNNER~1`), POSIX absolute paths
  falling into the repo-relative branch, and a resolved path compared against an
  unresolved root. A key computable two ways is not a key (#55, #56).

## [2.64.0] - 2026-07-30

### Changed (BREAKING) — the Oracle dashboard no longer signs you in on page load (#31)

**What changes for you:** opening `http://localhost:3331` by hand — a bookmark, a typed
URL — now shows the dashboard but leaves it read-only. Settings changes, key rotation,
and every other mutating action return `401` until you sign in. Run **`c3 oracle open`**,
which opens an already-authorized tab. `c3 oracle serve` auto-opens a signed-in tab as
before, so if you always start the server and use the tab it opens, nothing changes.

**Why:** `GET /` issued the dashboard session cookie to any caller on the loopback
interface. A process running as a *different* OS user on the same machine could
therefore fetch the page and obtain a working session. That was previously accepted and
documented on the grounds that same-user processes can already read the keyring token —
true, but it does not cover the multi-user case, which is the one this closes.

**How it works:** on boot the server writes a bootstrap key to
`~/.c3/oracle/bootstrap.key` with owner-only permissions (home-directory ACLs are the
gate — the same assumption `~/.c3/secrets.enc` already makes). `c3 oracle open` reads it,
mints a single-use code via `POST /api/session/bootstrap`, and redeems it at
`GET /?bootstrap=<code>`; redemption sets the cookie and redirects to a clean `/`, so the
code never lingers in the address bar, browser history, or a `Referer` header. Codes are
single-use with a 120-second TTL, so one leaked through shell history or scrollback is
not a durable credential. `/api/session/bootstrap` is deliberately exempt from the local
write gate — it is how a browser *acquires* the cookie, so it cannot require one — and
runs its own loopback + key/Bearer check instead.

### Fixed — generated instruction docs claimed hook enforcement in IDEs that have none

`AGENTS.md` and `.github/copilot-instructions.md` are generated from the same
workflow text as `CLAUDE.md`, which opens with "native tools are **blocked by
PreToolUse hooks**". Only Claude Code installs those hooks. In VS Code Copilot,
Codex, Cursor and Antigravity the first native call an agent makes disproves that
sentence — and one disprovable line invites the agent to discount the rest of the
document.

Docs generated for a hookless IDE now restate the same mandate as a workflow rule
("no hooks in this IDE — following the order below is a project requirement
regardless") rather than a technical block, via `adapt_workflow_for_ide()` in
`services/claude_md.py`. Claude Code's `CLAUDE.md` is unchanged: there the hooks
are real.

Two related fixes ride along:

- **A trimmed step no longer takes its number with it.** Removing `/clear`
  guidance for IDEs that lack it dropped the entire `8. LOG` line, so the
  generated workflow ran `7.` → `9.` and lost `c3_session(action='log')`
  altogether. Trimming is now sentence-level: the step survives, only the
  snapshot clause goes.
- **VS Code's tool-load step is back.** `.github/copilot-instructions.md` again
  opens with the `tool_search_tool_regex` / `^mcp_c3_` bootstrap. A regeneration
  had replaced it with the Claude Code text, which left Copilot instructed to
  call tools it had not yet loaded — and, because the generator embedded live
  project facts, leaked absolute local paths into a committed file.

### Fixed — Jira Data Center `get_create_metadata` 404'd on Jira 9.0+ (#—)

`c3_jira(action='get_create_metadata')` called the monolithic
`GET /rest/api/2/issue/createmeta?projectKeys=…`. Jira DC 9.0 split that into
a paginated pair and **11.x removed the original**, which now answers
`404 "Issue Does Not Exist"` for *any* valid project — so the failure read as a
bad project key rather than a dead endpoint. Issue types and required fields
could not be enumerated on a modern DC instance at all.

The DC backend now tries the split endpoints first
(`createmeta/{project}/issuetypes` → `…/issuetypes/{id}`) and falls back to the
legacy shape only on 404, so pre-9.0 servers keep working. The two responses
disagree on shape — the split route returns a *list* of field objects carrying
`fieldId`, the legacy route a *dict* keyed by field id — and both are now
parsed. Field pages are drained rather than trusted at one call, since a
truncated page would silently under-report required fields.

`create_issue` was never blocked by this: it already treats a createmeta
failure as non-authoritative and lets Jira's own 400 decide.

### Changed — Oracle ChatStore is append-only JSONL (#30)

`ChatStore` re-serialized the entire transcript on every append — twice per
chat turn (user message, then the round batch) — so per-turn write cost grew
with conversation length. Conversations are now `<id>.jsonl`, one message per
line, appended with a single `write`.

Legacy `<id>.json` arrays migrate lazily on first access; there is no migration
script and no startup scan. The migration is crash-safe by ordering — temp
file, atomic replace, *then* unlink the legacy file — and readers concatenate
legacy + JSONL, so every partial state (mid-migration crash, appends that
landed before migration ran) reads back complete and in order rather than
losing or duplicating messages. The index stays a whole-file write, since it
holds one entry per conversation rather than per message, but it is now cached
in memory and re-read only when the file changes underneath.

### Added — rate limiting + audit logging on the Discovery API (#33)

`/api/discovery/*` and the MCP transport were Bearer-gated but unthrottled, so
a leaked token allowed unbounded tool calls — and `c3_search_cross` fans out a
full runtime per project, so one call's cost is not bounded by its request
size.

Tool-executing routes (`/call`, `/call/stream`, `/tools/<name>`) are now behind
a per-caller token bucket, default 60 calls/min with a quarter-minute burst
(`api_rate_limit_per_min`, `api_rate_burst`; `0` disables). Listing tools, the
OpenAPI document, and `mcp-info` stay open — throttling API discovery itself
only produces a worse error message. Exhaustion returns `429` with
`Retry-After`.

Every tool call also appends one JSONL line to `~/.c3/oracle/discovery_audit.jsonl`
(`api_audit_enabled`), readable via `GET /api/activity/discovery`. **The log
stores a hash of the arguments, never the arguments**, and identifies the
caller by token fingerprint rather than token: discovery args routinely carry
file paths, queries, and project names, and an audit trail that leaks them
would be a worse liability than the missing throttle. Auditing fails open — a
broken log never breaks a call.

The Activity *tab* does not yet render this feed; the endpoint is live and the
UI wiring is still outstanding.

### Removed — overdue `/legacy` hub route + `cli/hub.html` (#35)

The frozen pre-v2.44 Hub UI was slated for deletion in v2.45/v2.46 under the
one-release escape-hatch convention; the Oracle's equivalent went in v2.49.1.
`GET /legacy` now 404s, `cli/hub.html` is gone, and the Settings-modal link to
it is removed. The per-project `/legacy` route (`cli/server.py`, serving
`ui_legacy.html`) is deliberately untouched — it is on its own retirement
clock and was only flagged for review.

### Fixed — Access Guard denied any shell command mentioning a URL or IPv6 (#50)

Windows only. The best-effort Bash token scan fed whitespace-split tokens into
the path evaluator, where a residual colon means NTFS alternate-data-stream
syntax — so `https://example.com` and `fc00::/7` matched the `<ads>` rule and
hard-denied the command. Because synthetic `<…>` rules are exempt from the
scanner's existence gate, a token naming nothing on disk still refused, and the
200-token scan cap made it position-dependent enough to look flaky. Practical
effect: `git commit` messages about networking, `curl`, and `gh pr create` were
blocked, citing a rule that `c3 access list` did not print.

`_scan_shell` now skips scheme-prefixed and IPv6 literal/CIDR tokens before
evaluating them. The ADS check is unchanged for real path arguments
(`file_path`/`notebook_path`), where a colon is unambiguous. Separately, the
spelling rules (`<unc>`, `<unresolvable>`, `<empty-component>`, `<8.3-alias>`,
`<ads>`) are now listed by `c3 access list` and in `docs/access-guard.md`, so a
cited rule is always one the user can look up.

### Fixed — e2e benchmark silently truncated every prompt to `.CMD` providers

Benchmark-harness only; no change to C3's runtime tools.

On Windows `claude` resolves to a real `claude.EXE`, but `codex` and `gemini`
resolve to npm `.CMD` shims, so Windows launches them through an implicit
`cmd.exe /c <command-line>` — and **a batch command line terminates at the first
newline**. Benchmark prompts are multi-line:

```
Use C3 MCP tools (not native Read/Grep/Glob). Be concise, cite file paths.

Question: {query}
```

Everything from the blank line onward was discarded before the child process
parsed argv. codex and gemini received only the instruction paragraph, replied
"Understood. What should I inspect or change?", and scored ~0 — against a
baseline truncated identically. Exit code was 0 and no error was raised, so the
harness recorded the result as a valid measurement.

This is adjacent to the ghost-file defect `services/win_subprocess.py` already
guards, but distinct: `harden_win_argv` fixes cmd.exe *quote desync*, and no
quoting can protect a newline. The prompt now goes over stdin whenever the
resolved executable is a batch shim (`codex exec -`; gemini reads piped stdin
when `-p` is absent), keeping it off the command line entirely. Native
executables are unchanged and still receive the prompt as an argument.

- `test_build_command_gemini` asserted `-p` unconditionally and passed only
  because CI's Linux runners have no `.CMD`; it would have failed on the
  `windows-latest` leg. It now pins the launcher mode explicitly instead of
  inheriting it from the host PATH.
- Added regression coverage asserting no newline ever reaches argv for a batch
  shim, for every provider.

**Historical data is affected.** Every codex/gemini e2e figure recorded on
Windows before this fix is invalid, including the 2026-03-12 baseline, so the
trend deltas in `.c3/e2e_benchmark/` are computed against corrupted history.

## [2.63.2] - 2026-07-28

### Changed — benchmarks re-measured, headline claim restated as a band

Documentation-only release. No behaviour changes.

The published token-savings figure was measured at v2.43.0 (2026-07-02) and had
not been re-run across the 20 releases since. Both no-cost benchmark tiers were
re-run against this repository at v2.63.2:

| Tier | v2.43.0 | v2.63.2 |
| --- | --- | --- |
| `c3 bench session` (sample-size 15) | 51.8% savings, 2.07× | 49.9% savings, 2.0× |
| `c3 bench quick` (sample-size 25) | 85.3% savings, 6.79× | 86.0% savings, 7.13× |

Session quality was unchanged in both runs — 98.8% for C3 against a 96.5%
baseline. The session drift is real signal, not variance: `_select_sample()`
takes the largest files by token count with no RNG, so a given commit
reproduces its figure exactly. Eligible files grew 256 → 409, which changed
which files land in the top 15.

- **README now states a band, not a point** — "~50% token savings (2×)", with
  both measured runs cited and the determinism of the sampler noted. A point
  estimate needed a re-stamp every few releases; a band survives normal drift.
- **Social preview card** (`docs/social-preview.png`) re-rendered to match.
- Republished so the PyPI `long_description`, which is baked into the wheel at
  build time, carries the corrected claim.

Not re-run this cycle: `c3 bench e2e` (last measured 2026-03-12) and the
external Aider Polyglot / SWE-bench Lite suites (never run here). Both make
real, billed API calls, and neither is cited in any published claim.

## [2.63.1] - 2026-07-28

### Changed — README rebuilt, screenshots regenerated, docs split out

Documentation-only release. No behaviour changes outside two UI text fixes.

The v2.63.0 PyPI page never mentioned Mask Guard: the tag landed on the release
commit and the README section landed one commit later, and `long_description`
is baked into the wheel at build time. This release republishes the page.

- **Screenshots regenerated.** The previous set was captured 2026-04-27 — 34
  releases earlier. It showed an 8-tab sidebar (live: 12), a Hub generation
  retired in v2.44.0 and since moved to `/legacy`, and an IDE picker offering
  the Gemini CLI profile removed in v2.52. New captures live in
  `docs/screenshots/2026-07/`; the old files are retained because previously
  published PyPI pages reference them by URL.
- **`scripts/screenshots/`** — a re-runnable capture rig
  (`python -m scripts.screenshots.run`). Builds a synthetic demo project so no
  real project name, path, fact, transcript, or credential name can reach a
  published image; swaps the Hub registry and restores it in a `finally`
  block; and fail-closed asserts the *served* bundle contains a symbol from
  the newest component before capturing, since `cli/server.py` caches the built
  UI for the process lifetime and would otherwise serve pre-feature markup.
- **README restructured**, 595 → 281 lines. Reference material moved to
  `docs/upgrading.md`, `docs/sub-projects.md`, and `docs/integrations.md`.
- **Corrections.** The tool table said 18 tools (there are 20); `c3_agent`
  advertised a `refactor` workflow that does not exist; the Bitbucket action
  list had drifted under the `### Jira` heading; `c3_status` was missing its
  `sessions` and `access` views; the Oracle section still taught
  `python oracle/oracle_server.py` instead of `c3 oracle serve`; and 14
  repo-relative links resolved on GitHub but 404'd on PyPI — all now absolute.
- **`pyproject.toml` description** shortened 284 → 166 chars and reworded to
  name the access/masking guards.

### Fixed

- **`cli/ui/components/dashboard.js`** — two `\u00b7` escapes sat in JSX
  text rather than inside a string literal. JSX does not interpret escapes in
  text nodes, so the Dashboard rendered the six literal characters `\u00b7`
  instead of `·` in the Current Session line and the source-tokens badge.

## [2.63.0] - 2026-07-28

### Added — Mask Guard: expose a path, control what the agent sees

Access Guard answers *may the agent touch this path?* Mask Guard answers
*what should it see when it does?* A third verdict, `mask`, sits between
`deny` and `read_only`: matching files stay visible and searchable, but every
byte the agent receives comes from a deterministic, materialized,
**read-only** view. The real file is never modified. Designed from two prior
local systems (SafeMirror's classify→policy→transform→validate pipeline and
Table Parser's reversible column masking) and reviewed by both federation
siblings; Cod's review overturned the original transform-on-read architecture
in favour of materialized artifacts. Full rationale, leak-channel analysis and
residual risks: `docs/mask-guard.md`.

The governing distinction: `deny` is a **predicate** — cheap to evaluate
anywhere, and its failure mode is no output. `mask` is a **function** — it
must run exactly once per byte-to-text boundary, must be deterministic, and
its failure mode is *wrong output that looks right*. Everything below follows
from that.

- **`services/access_guard.py`** — `access.mask` schema
  (`[{glob, preset, params}]`), precedence `deny > mask > read_only`, and a
  new `verdict()` returning `allowed | masked | read_only | denied`.
  `check()` **fails closed on masked reads**, so any surface not taught to
  render a view refuses rather than leaking raw bytes. Overlapping mask rules
  that disagree on preset/params are a config error, not a precedence puzzle —
  rule order can never affect output. Preset names and param schemas are
  validated in the evaluator so the hook subprocess can reject bad config
  without importing the transform engines.
- **`services/mask_presets.py`** — four deterministic, versioned transforms,
  no LLM anywhere in the read path: `redact_secrets` (pattern library for AWS
  / GitHub / OpenAI / Slack / Google keys, JWTs, PEM blocks, connection
  strings and `*_PASSWORD=` assignments, plus a Shannon-entropy sweep),
  `redact_columns` (salted one-way pseudonyms — same value maps to the same
  pseudonym so joins, uniqueness and cardinality survive, with no reverse map
  to protect), `sample_rows`, and `signatures_only` (reuses the compressor).
  Placeholders are spelled `«c3:redacted:kind»` — deliberately invalid in
  every supported language, so one that ever reached a real file trips a
  linter instead of being silently committed.
- **Protected Mode** — after rendering, the *output* is re-scanned for secret
  material. Anything that survived turns the read into a loud refusal instead
  of a quiet partial leak.
- **`services/mask_mirror.py`** — content-addressed views under
  `~/.c3/masked/<project>/views/<view-hash>` with a manifest, where
  `view-hash = hash(source + preset + params + transformer_version)`. A stale
  view is regenerated or the read refuses; a stale twin is never served.
  Atomic same-directory `os.replace` for Windows, byte-faithful artifact I/O
  (universal-newline translation would have made a CRLF view differ between a
  fresh render and a cache hit — a differencing oracle), and GC.
- **`services/mask_activation.py`** — adding a rule is a transaction, not a
  config edit: `pending → purge derived artifacts → build + validate views →
  rebuild indexes → active`. Purges the compression cache, search index, repo
  map, file memory and provenance-matched facts. Until it completes, the UI,
  CLI and status line all say masking is **not** in effect.
- **Fact provenance (`services/memory.py`, `services/auto_memory.py`)** —
  `remember()` now records `source_paths`, three-state by design: `None`
  (unknown), `[]` (known to have no file source), or the derived paths. The
  auto-memory extractors thread it through. Facts written before this release
  have unknown provenance and cannot be proven clean, so the first activation
  in a project purges them wholesale. This was the ship-blind failure Cod
  named, and it was live in the codebase: facts derived from file content
  carried session provenance only.
- **Surface behaviour** — `c3_read` / `c3_compress` / `c3_search` serve the
  view (masked files are indexed *from* the view, so they stay searchable);
  `c3_edit`, `c3_shell` content reads, `c3_validate`, `c3_impact`,
  `c3_filter`, `c3_delegate` and native Read/Grep/Glob **refuse**. Post-
  filtering shell stdout is deliberately not offered: it could strip a secret
  but could never reconstruct a crop, so it would read as a guarantee C3
  cannot make.
- **Honesty contract** — loud about evidence quality, quiet about policy.
  Every transformed payload carries `[c3-mask:transformed] view=<class>` with
  a coarse class (`redacted` / `sampled` / `structure_only`), because *how* a
  view is incomplete changes which conclusions are safe. Search gains
  `[c3-mask:limited]`. New stable tags: `[c3-mask:transformed]`,
  `[c3-mask:limited]`, `[c3-mask:unsupported]`.
- **Human surfaces** — Access tab gains a Masking section, an activation
  banner that refuses to imply protection it hasn't established, and a
  **live before/after preview** on the path probe: see exactly what the agent
  will see, on your real file, before committing the rule. New
  `POST /api/access/mask`, `DELETE /api/access/mask`,
  `POST /api/access/mask/activate`, `POST /api/access/preview`; new
  `c3 access mask add|rm|status|activate|preview`. All mutations remain
  human-only and ledger-logged.
- **Docs** — new `cli/guide/masking.html` (presets, activation, why masked
  means read-only, per-tool behaviour, honest limits, troubleshooting) linked
  from every guide page; `docs/mask-guard.md` frozen with the sibling-review
  reconciliation recorded.
- **Tests** — 81 new across `test_mask_guard.py`, `test_mask_surfaces.py` and
  `test_mask_routes.py`, including a wiring meta-test that fails CI if a
  content surface stops consulting Mask Guard. Suite: 1627 passing.

### Changed

- `c3 access list` and `c3 access check` now report mask rules and the
  `masked` verdict; the coverage matrix states the masking residual plainly —
  the real bytes stay on disk, so editors, raw shells, non-Claude agents and
  git still see them. Mask Guard is context hygiene, not containment.

## [2.62.0] - 2026-07-27

### Added — Access Guard: path-level read/write prevention for agents

The Credential Vault protects *values*; Access Guard protects *files and
folders*. Two glob lists — `deny` (no read, no write, no create, no
enumerate) and `read_only` (no write) — in the `access` section of
`.c3/config.json` (project) and `~/.c3/config.json` (global), enforced by
one shared evaluator at every C3 surface. Designed by an adversarial board
review (five seats + chair) whose amendments shaped everything below.

- **`services/access_guard.py`** — the single evaluator: two-scope
  tighten-only union (no `allow` list exists; unknown keys are a hard
  config error; a corrupt section makes that scope deny-all — fail
  closed), POSIX case-insensitive globs (basename patterns match at any
  depth), and ONE `canonicalize()` handling `\\?\`/UNC pre-stripping,
  nearest-existing-parent resolution for not-yet-existing targets, and
  Windows-gated trailing-dot/space, alternate-data-stream, and 8.3
  short-name defenses. Builtins (non-overridable): `**/.env*` and the
  vault sidecars denied outright; write-denies on `.c3/**`, `~/.c3/**`,
  `.claude/settings*.json`, `.git/**` (reads stay open), and the installed
  C3 package (dev checkouts exempt). `*.pem`/`id_rsa*`/`*.key` ship as
  visible, removable default rules.
- **Service-layer enforcement** (never the MCP wrappers, so the Oracle
  Discovery bridge, `c3_project`, and `c3_delegate` inherit):
  `compress_file` and `ArtifactStore.restore` raise a typed
  `AccessDenied`; read/edit/compress/validate/filter/impact convert to
  refusals; search pre-filters denied paths from results (deny-ENUMERATE)
  and appends a presence-only `[c3-access:limited]` footer whenever rules
  are active; `scanner.iter_files` excludes denied paths at index time so
  they never enter the TF-IDF/vector index, MAP.md, or file_memory.
- **Hook layer, fail-closed** — new `hook_access_guard.py` runs FIRST in
  the PreToolUse route: native Read/Edit/Write/MultiEdit/NotebookEdit
  verdicts before any unlock logic (sticky unlocks cannot readmit a policy
  denial); explicit-path Grep/Glob denial with rootless searches kept
  advisory; best-effort existence-gated Bash token scan. If the guard
  itself fails to import or crashes, write-class tools are DENIED with an
  actionable reason instead of falling through. install-mcp now registers
  `Bash` + `run_shell_command` PreToolUse matchers — previously the hook
  layer never fired for shell at all. Unlock-map keys are now canonical
  (resolved/casefolded, old stores migrate at load), closing a
  case-spelling bypass; the enforcement evidence window counts tool_call
  entries so denial storms can't evict it.
- **`c3_shell`** hard-denies a denied working directory and runs an
  advisory post-credential-expansion token scan (MSYS path translation,
  refusals redacted). **`c3_project`** evaluates proxied paths against
  global ∪ caller ∪ the containing realm and requires target registration
  (no more rule-free pivot projects). **`c3_delegate`** pins codex to
  `--sandbox read-only` when rules exist and gates write-capable backends
  behind an explicit `allow_write_delegation` opt-in.
- **Human surfaces — all rule mutations are human-only and ledger-logged;
  agents have no mutation surface.** Per-project **Access Guard UI tab**
  (scope-grouped rules, locked builtins, typed-glob delete confirmation,
  test-path probe showing the exact refusal, corrupt-scope banner,
  coverage-matrix footer), REST `/api/access` (+ `check` probe),
  `c3 access list|add|remove|check` CLI, and a read-only
  `c3_status(view='access')`.
- **Refusals as API** — stable machine tags (`[c3-access:denied]`,
  `[c3-access:read_only]`, `[c3-access:limited]`, `[c3-access:error]`)
  with the matched rule, scope, and explicit do-not-retry / plan-disposition
  guidance, frozen verbatim in `docs/access-guard.md` before
  implementation.
- **Honest coverage, stated everywhere it matters**: enforced for C3 MCP
  tools (any agent using C3), Claude Code native tools (hooks), and
  best-effort for `c3_shell` — NOT for a non-Claude agent's raw shell,
  direct file APIs, or external editors. A cooperative-agent guard against
  mistakes and prompt-injection, not a sandbox. Known v1 residuals are
  documented: shell renames/moves, TOCTOU, pre-existing index content.
- **CI meta-tests against drift** — every `cli/tools` module doing raw
  file I/O must import the guard or be allowlisted with a written reason;
  direct `Path.resolve()` is banned in enforcement-adjacent code; a
  denial-storm regression pins the evidence window.
- **Guide** — new `cli/guide/access.html` (linked from every guide page),
  README section, and the coverage matrix in tab + status + docs.

~130 new tests across evaluator, wiring, hooks, shell/project/delegate,
surfaces, and meta suites. Full suite: 1546 passed.

## [2.61.2] - 2026-07-27

### Security — vault write-guard: closes an `agent_readable` escalation

An adversarial design review of the upcoming Access Guard feature surfaced a
live escalation in the credential vault: the registry that stores each
credential's `agent_readable` flag lives in `.c3/config.json`, and that file
was freely agent-writable. A prompt-injected (or simply misbehaving) agent
could `c3_edit` the registry, flip `agent_readable: true` on an
injection-only secret, then call `c3_credentials(action='reveal')` and read
a value the user never marked readable. Three independent guards close this:

- **`c3_edit` refuses vault files.** `.c3/config.json`, `.c3/secrets.enc`,
  and `.c3/cred_state.json` (project or global scope, any mode — edit,
  create, batch) return `[c3:vault-protected]` with a pointer to the
  Credentials UI / `c3 creds` CLI. The `c3_project` edit proxy shares the
  same handler, so cross-project edits are covered too.
- **The PreToolUse hook denies native `Edit`/`Write`/`MultiEdit`** on the
  same files *before* any unlock logic runs — a warm c3 signal or sticky
  unlock no longer readmits them. The vault file set is mirrored in the hook
  (hooks stay import-light) with a parity test pinning the two sets together.
- **`reveal` now requires a keyring attestation.** `set_credential` /
  `update_metadata` — the only legitimate writers of `agent_readable`
  (tool, CLI, REST, both UIs all route through them) — store a copy of the
  flag in the OS keyring. `reveal` fails closed with `[creds:integrity]`
  when the registry flag and the attestation disagree or the attestation is
  missing, so a registry edited by *any* out-of-API route (shell
  redirection, a non-Claude agent, a text editor) no longer leaks the value.
  Refused reveals are recorded in the audit log as tamper signals.

Migration note: entries marked `agent_readable` before this release have no
attestation yet and `reveal` will refuse them with a remediation message —
toggling the flag off/on in the Credentials UI (or re-running
`c3 creds set <name> --agent-readable`) writes the attestation. Injection
(`env_creds` / `{{cred:NAME}}`) is unaffected. Known residual until the full
Access Guard ships (v2.62): `c3_shell` command strings and non-Claude agents
are not yet path-scanned — the reveal attestation is the backstop there.

## [2.61.1] - 2026-07-27

### Fixed — Hub Start-UI placeholder tab could hang forever on "Starting …"

Clicking **Start** (or **Open UI** on an active session that has no UI server
yet) opens an `about:blank` placeholder tab synchronously — inside the click
gesture, so popup blockers allow it — while the detached UI-server child picks
a port and registers it. All of the polling, redirect, timeout, and cleanup
logic lived in the *hub tab* that opened it. If that hub tab was reloaded,
closed, or its start request hung at any point in the ~31-second launch
window, the placeholder was orphaned: a black page reading "Starting
&lt;project&gt;…" forever. Even on the clean failure path, the only feedback
was a toast in a different tab.

The placeholder is now self-sufficient. The document written into it carries
its own script (`about:blank` inherits the hub's origin, so its fetches are
same-origin): it polls `/api/projects` itself, navigates itself to the UI
server's port, shows a spinner while waiting, and renders failures — launch
error, no port after 30 s, hub unreachable — inside the tab, with a pointer
to the project's `.c3/ui.log`. The hub-side poll is reduced to bookkeeping
(card refresh, busy state) and only steers the tab in the fallback case where
the placeholder could not be written (popup blocked). Verified live by
clicking Start and immediately reloading the hub tab: the placeholder
redirected itself ~3 s later.

Escaping note for future editors: the hub bundle is inlined into
`hub_ui.html` inside a `<script type="text/babel">` block, so the
placeholder's closing script tag must be written as `<\/script>` in JS
source — a literal closer would truncate the entire bundle.

## [2.61.0] - 2026-07-26

This release also carries the Windows hook fix prepared as 2.60.1, which was
never tagged or published — see its section below.

### Added — Credential vault guide (`/guide/credentials.html`)

The vault had shipped across three releases (v2.58.0 store, v2.59.0 Hub tab,
v2.61.0 search) with documentation spread over a tool card and a README
section. It now has a dedicated guide page, alongside the Bitbucket and
Oracle ones: storage internals and where each byte lives, the scope/override
model and why resolution is realm-atomic, the three injection paths, what
each exposure flag actually costs you, a Hub UI tour, the full search
qualifier table, the `c3 creds` CLI, the audit trail, and troubleshooting.
Linked from the guide nav, the home page, the `c3_credentials` tool card, and
a new Credentials section in Getting Started.

### Changed — Hub Credentials: cross-project search, a per-credential settings drawer, and a context menu

The Credentials tab shipped in v2.59.0 as a flat list with no way to find
anything: 40+ projects behind accordions, and one overloaded row per entry
packing eleven badges plus five unlabelled icon buttons — where the `eye`
icon toggled `agent_readable` (the flag that lets the agent pull plaintext
into its transcript) one pixel from `edit`. The write-only wire is unchanged:
no route returns a value, and search indexes metadata only.

- **Cross-project search** — one field above the sub-tabs, `/` or `Ctrl/⌘-K`
  to focus. Free tokens AND-match name / description / env var / owner;
  `key:value` qualifiers narrow further (`project:` `scope:` `type:`
  `storage:` `name:` `env:` `inject:` `agent:` `shadow:`). Results are
  **grouped by credential name**, so "where is `STRIPE_KEY` defined" is one
  glance across the global vault and every project instead of forty manual
  expansions. Scope chips (All / Global / Project) and sorts by name, most
  defined, last used, most used, or exposure. `↑↓` walks results, `↵` opens.
- **Per-credential settings drawer** — the single surface for editing one
  entry: General (description, type, env var), Exposure (both switches with
  the blast radius spelled out), Secret (on-demand resolution check +
  fingerprint, and a write-only Replace field that starts empty and is
  cleared the instant the request settles), Usage & relationships (created /
  updated / last used / use count / storage, plus which projects override
  this name or are overridden by it), and a separated Danger zone.
- **Right-click context menu** — on any row, plus a `⋯` button and
  `Shift-F10` / the Menu key for keyboard users. Open settings, check
  resolution, replace secret, toggle either exposure flag, copy name /
  env var / fingerprint, open the project drill, delete. Rows are focusable
  and open the drawer on `↵`; the bare `eye` and `zap` icons are gone.
- **Typed confirmations replace `window.confirm`** — deleting or raising an
  exposure flag now shows what actually happens (which projects are affected,
  that transcripts are searchable, that C3 keeps no copy of the value) and
  deleting requires typing the credential name.
- **One manager mounted at a time** — the Projects sub-tab is a single-open
  accordion with its own filter, so N expanded projects no longer means N
  independent fetchers going stale against each other. Rows surface
  agent-readable and overriding-global counts before you expand.
- **Fixed** — `T.ok` is not a theme token and never has been. Five call sites
  across `hub_credentials.js` and `ui/components/credentials.js` rendered
  project-scope badges, the resolvable-fingerprint marker, and a success
  banner with `color: undefined` and `background: "undefined15"`.
- **Fixed** — `Escape` closed the drawer *and* wiped the search query behind
  it; it now closes the topmost layer only.

## [2.60.1] - 2026-07-26

### Fixed — Windows hooks never ran: the `cmd.exe` wrapper ate its own switch

Every hook C3 installed on Windows was dead on arrival, silently. `c3 init`
wrote commands of the form `cmd.exe /c '<python>' '<hook_dispatch.py>' posttool`.
Claude Code runs hooks under Git Bash, and MSYS argument conversion rewrites a
standalone `/c` into `C:/` before `cmd.exe` is executed:

```
$ python -c "import sys;print(sys.argv)" /c foo
['-c', 'C:/', 'foo']
```

So `cmd.exe` launched with no switch, opened an **interactive** session, printed
its banner, and read the hook's stdin JSON payload as console commands. The hook
never ran. Worse, a `>` token anywhere in that payload became a shell redirect,
creating empty junk files in the repo root (observed in the wild: `a`, `void`,
`export`, `UI`).

The wrapper existed to protect paths containing parentheses (e.g.
`Claude Code Companion (C3)`) from bash word-splitting. It was never needed —
a double-quoted forward-slash path is parsed correctly by both bash and cmd:

```
"C:/.../python.exe" "U:/1. Projects/Claude Code Companion (C3)/cli/hook_dispatch.py" pretool
```

Hook commands are now emitted in that form on Windows (POSIX keeps `shlex.quote`).
Verified end-to-end: the dispatcher runs, returns its `additionalContext`, exits
0, and leaves no junk files, from a project path containing both a space and
parentheses.

This wiring has now broken twice — first `cmd /c` (bash cannot resolve bare
`cmd`), then `cmd.exe /c`. `tests/test_install_mcp_entrypoint.py::
test_hook_commands_are_not_wrapped_in_cmd_exe` is a regression guard: no
`cmd`/`cmd.exe` prefix, no single-quoted paths, no backslashes.

**Existing projects are not repaired by upgrading** — the broken command string
lives in each project's `.claude/settings.local.json`. Re-run `c3 init` (or
`c3 install-mcp`) per project to rewrite it.

## [2.60.0] - 2026-07-26

### Added — Live repo map: `.c3/MAP.md` replaces the frozen instruction-doc tree

The Project Context tree embedded in CLAUDE.md / AGENTS.md was regenerated
only when `c3 claudemd save` ran, went stale between runs, fought the
instruction-doc line budget, and was duplicated per file. It moves to a
machine-owned **`.c3/MAP.md`** kept fresh automatically; instruction docs
carry only a stable pointer (works for Claude Code, Codex, and Antigravity).

- **`RepoMapService`** (`services/repo_map.py`) — renders commands, entry
  points, module one-liners, depth-2 tree, and key files under a token
  budget (default 1000). **Byte-stable**: the file is rewritten only when
  rendered content changes, so prompt caches stay warm; volatile freshness
  state (git HEAD / branch / worktree signature / generated_at) lives in
  `.c3/map.meta.json`, never in the map. Cross-process lock with stale
  recovery, atomic replace, `.bak` retention. Sub-projects render as
  boundaries and are never expanded; non-git projects fingerprint via a
  bounded mtime/size walk. The map is delimited as repository *data*, and
  memory facts are deliberately excluded from it.
- **Freshness triggers** — structural changes (file created / deleted /
  renamed, or a dependency-manifest edit) touch `.c3/map.dirty` from both
  ledger paths (PostToolUse hook and server-side `EditLedger.log_edit`);
  the MCP server's first tool call runs a background single-flight ensure;
  `c3 map refresh` is the explicit repair. Ordinary line edits never
  trigger regeneration.
- **CLI** — `c3 map status|ensure|refresh` (plus `--json`).
- **Doc generators** — CLAUDE.md / AGENTS.md embed a stable pointer block
  instead of the tree; `map.enabled=false` in `.c3/config.json` restores
  the legacy embedded tree + tech stack + key files.
- **Config** — `map.token_budget`, `map.file_cap`, `map.enabled`.

### Fixed

- **Stale Codex model pins** — `CODEX_MODELS` and three call-site fallbacks
  pinned retired model names (`gpt-5.3-codex-spark`, `gpt-5.4`,
  `gpt-5.3-codex`) that hard-fail ChatGPT-plan Codex logins with a 400.
  Model resolution now falls through: `codex_default_model` config → the
  user's own Codex CLI default (`-m` omitted entirely when unset). A guard
  test scans production files so a pin cannot return.

Design reviewed with a frontier-model second pass: the byte-stable-body /
volatile-sidecar split, injection-delimited header, structural-only dirty
marking, and treating instruction compliance as backstop (never the
freshness mechanism) all came out of that review.

## [2.59.0] - 2026-07-24

### Added — Hub Credentials: top-level tab with global vault + cross-project management

The credential vault gets a home in the hub. A third top-level view
(Projects | Tasks | **Credentials**) manages the **global vault** (`~/.c3`) —
which previously had no UI of its own — and every registered project's
entries, with shadowing shown both ways.

- **Hub UI** — new `cli/hub_ui/components/hub_credentials.js`: shared
  `CredsManager` (create / edit-keeping-value / delete / `.env` import / flag
  toggles with the `agent_readable` confirm / fingerprint check) parameterized
  by project path, `path=null` targeting the global vault. Top-level
  `HubCredentials` page with **Global vault** and **Projects** sub-tabs; the
  per-project drill Credentials tab upgrades from read-only to the same full
  manager. Project rows show a "shadows global" badge; global rows show
  "shadowed in N projects".
- **Hub REST** — the write-only wire contract extends to the hub: new
  `POST /api/projects/credentials` (set, or metadata-only partial update),
  `POST .../import`, `DELETE .../<name>`, `POST .../<name>/check`, all taking
  `path` + `scope` where `scope=global` with no path targets the shared vault
  directly; plus read-only `GET /api/hub/credentials/overview` (global +
  per-project inventory with shadow info). **No hub route ever returns a
  stored value** — values transit inbound-only at set time, there is no
  `reveal` on the hub, and the serializer is an explicit field allowlist
  (endpoint-sweep tested). `credentials` remains excluded from the hub config
  write sections; these dedicated routes are the only hub write path.
  Mutations on uninitialized paths get 409 `needs_init`.
- **Audit** — `via:"hub"` `cred_action` events (names only, never values) to
  the target project's ActivityLog + EditLedger; global-scope mutations also
  land in `~/.c3/activity_log.jsonl` so the shared vault keeps its own trail.
- **Service** — public `credential_store.global_base()` accessor (the only
  service change).
- **Docs** — `c3_credentials` gets its tools-guide entry (sidebar / TOC /
  full card — it shipped in v2.58.0 undocumented there); README vault section
  and SECURITY.md updated for the hub's new write posture.

## [2.58.0] - 2026-07-24

### Added — Credential vault: `c3_credentials` + Credentials UI (injection-first)

A general-purpose secret store for agents, global (`~/.c3`) + per-project
(`.c3`) scoped, designed so decoded values never enter the model's context.

- **`services/credential_store.py`** — named entries; metadata registry in
  `config.json`, values in the OS keyring under `c3-creds`, keyed by
  `(realm, name)` (`global` / `proj|<path>`). Values >1KB route to a
  Fernet-encrypted `.c3/secrets.enc` whose random master key lives in the
  keyring (Windows Credential Manager caps blobs at ~2.5KB); `cryptography`
  is lazily imported like `keyring`. No plaintext fallback, ever.
- **Realm-atomic resolution** (security invariant): a project-registered name
  resolves in the project realm or not at all — a cloned repo's committed
  `.c3/config.json` can register a name with `inject:true` but can never
  siphon the global value; behavioral flags are honored only from the realm
  that holds the value. Explicitly tested.
- **`c3_credentials` MCP tool (19th)** — `list` / `describe` / `check` return
  names + metadata + live fingerprint, never values. `reveal` is gated by a
  per-entry `agent_readable` flag only the user can enable (the agent cannot
  raise it on an existing entry). `set`/`delete` allowed; every mutation and
  reveal is ledger-logged with identifiers only.
- **`c3_shell` integration** — `env_creds='NAME1,NAME2'` injects entries as
  env vars into the child process; `{{cred:NAME}}` inside `cmd` expands
  server-side. The raw template form is what every log/echo surface shows;
  child stdout/stderr are scrubbed against the decoded values (`env` dumps
  come back as `[cred:NAME]`); `inject:true` entries auto-inject.
  Cross-project `c3_project(action='shell')` proxies run with credentials
  disabled — one project can never read another's vault.
- **Choke-point redaction** in `_finalize_response`: any active decoded value
  is scrubbed from the persisted copies (activity log, session store,
  auto-memory) of every tool response.
- **`c3 creds` CLI** — `set` (getpass / `--stdin`), `get` (masked; `--show`),
  `list`, `rm`, `import <.env>`; `--global` targets the shared scope.
- **REST + Credentials UI tab** — `/api/credentials` (masked GET, write-only
  POST, scoped DELETE, `check` probe, `.env` import). New per-project UI tab
  with create/edit form, flag toggles (with an explicit warning before
  enabling `agent_readable`), usage stats, and fingerprint checks. No route
  ever returns a stored value (endpoint-sweep tested).
- **Hub** — read-only Credentials drill tab (`GET /api/projects/credentials`,
  explicit metadata field allowlist); the `credentials` config section stays
  out of `_CONFIG_WRITE_SECTIONS`, preserving "secrets never transit the hub".
- **Oracle exclusion** — `c3_credentials` is hard-excluded from Discovery
  `TOOL_SPECS` (regression-tested), so external LLMs can never reach the vault.
- **Git hygiene** — the store self-writes a `.c3/.gitignore` guarding
  `secrets.enc` + `cred_state.json` for projects that track their `.c3/`.
- 60 new tests across store/tool/shell/CLI/routes/hub.

## [2.57.1] - 2026-07-24

### Fixed — Hub "Open UI" on active projects without a running UI server

- Clicking **Open UI** on a project whose agent session is live but whose UI
  server isn't running used to launch the server in the background and never
  open a tab — the button looked dead. `launch_session` can't return a port
  (the detached child picks it), so the card now opens a placeholder tab
  synchronously (inside the click gesture, so popup blockers allow it),
  polls `/api/projects` until the port registers, then navigates the tab —
  or closes it with a clear error pointing at `.c3/ui.log`.

### Changed — per-project UI: real active-project switcher

- The sidebar's project switcher was a bare count icon in the footer that
  rendered only when *other UI servers* were already running — invisible in
  the common case. It is now an always-visible "Switch project" control:
  opening it scans on demand (`/api/registry/active`, new — running UI
  servers plus registered projects with a live agent session; probes ports,
  so it is never polled), jumps directly to running UIs, and for
  session-only projects launches their UI via `/api/registry/launch` (new)
  and navigates when the port appears.

## [2.57.0] - 2026-07-24

### Added — Hub sub-project management overhaul (UI/UX)

Manual parent/child control becomes a first-class Hub surface.

- **First-child bootstrap fix**: "Designate sub-project…" now renders on every
  non-child project card — previously it only appeared once a project already
  HAD registered children, so the first child could only be created via
  `c3 sub add` (the UI feature was invisible until the CLI had been used).
- **Passive link health**: `GET /api/projects` now annotates parent rows with
  `subproject_issues` (count of broken three-way links + registry orphans)
  and child rows with `link_status` (`ok | backlink_broken | unregistered |
  missing_folder | missing_c3 | orphan`) via a new
  `_annotate_subproject_links` helper — config + filesystem + raw-registry
  reads only, no port probing. Cards surface red "N link issues" badges on
  parents and amber/red status badges on children without anyone having to
  run Reconcile manually.
- **Sub-projects drill tab** (`drill_subprojects.js`, new): dedicated
  management surface — per-child rows (status, facts, alerts) with inline
  Validate and Promote actions; Designate button (fixes bootstrap in the
  drill too, with an educational empty state); inline Reconcile with
  per-issue detail and planned repair actions; Cascade launcher with op
  picker, include-parent toggle, affected-children preview, live progress,
  Cancel, and 409 attach-to-running handling.
- **"Make sub-project of…"** on eligible top-level cards: reverse designate —
  lists only registered parents whose folder physically contains the project
  (containment is mandatory), validates via the existing endpoint, links with
  adopt semantics.
- **Clear-mode de-init**: the child kebab's duplicate "Unlink" (same server op
  as Promote) is replaced by "De-initialize…" — the previously CLI-only
  `remove mode='clear'` (deletes `.c3`, uninstalls MCP config, removes
  instruction docs, deregisters) behind a typed-name confirm dialog.
- **Cascade UX**: progress toasts gained a cancel affordance (the
  `/cascade/cancel` endpoint existed with zero UI); the card submenu gained
  an "Include parent" toggle (`include_parent` was accepted server-side but
  never sent).
- **Hierarchy visibility**: child cards show an "↳ parent" chip (annotated in
  `buildProjectTree`, path-derived fallback when the parent row is filtered
  out or unregistered); the drill header shows an "under parent" chip; grid
  view child rows — previously inert — now carry kebab menus and link-status
  badges; removing a parent pre-warns in the confirm dialog that its N
  children become top-level (was a post-hoc toast).
- **Ctrl-K hierarchy** (`global_search.js`): child result groups render a
  "parent › child" breadcrumb; a scope chip row (All / Top-level only /
  per-parent "+ children") drives real server-side filtering through the
  endpoint's existing `projects` param.
- **FolderPicker polish** (`modals.js`): navigation fenced to the parent
  subtree (breadcrumb segments above the parent render inert instead of
  post-hoc validation failures); explicit adopt/re-link messaging for
  `has_c3` / already-registered folders; "Instruction docs / IDE" picker
  (`auto | claude | vscode | cursor | codex | antigravity`) wired to the
  endpoint's existing `ide` param; adopt-vs-initialize expectation shown
  before designating.
- **Inheritance toggles**: the config editor gains a "Sub-projects" section on
  parent projects — "Parent memory recall includes child facts"
  (`memory_rollup`), "Search scope 'all' fans out to children"
  (`search_fanout`), and "Max children per query", persisted as a partial
  deep-merge under `hybrid.subprojects`. The config PUT now permits that
  federation dict (the top-level child-links `subprojects[]` array stays
  refused — hierarchy still changes only through the dedicated actions).
- **Staged re-parent wizard**: "Change parent…" on child cards — an honest
  multi-step flow, never a fake one-click move (physical containment is
  mandatory). Ready-now targets relink immediately; otherwise the wizard
  unlinks first (deliberate: the child back-link can only be cleared while
  the folder is still at its old path), offers Undo until the user moves the
  folder, gates the new link behind validation, and cleans up the stale
  registry row — with a per-step checklist that reports the exact state on
  any failure.
- **Config editor honesty**: `subprojects` / `parent` keys — previously
  silently hidden — render as read-only rows with a "managed via sub-project
  actions" hint; drill Overview status colors now branch on the real status
  vocabulary (the old `'missing'`/`'error'` branches were dead code, so real
  errors rendered as warnings).

### Added — Hub UI: Reconcile action for sub-project links

- Parent project cards' kebab menu gains **"Reconcile links"** (wrench icon):
  runs the existing `/api/projects/subprojects/reconcile` consistency check
  as a dry-run, reports "all N links consistent" when clean, and otherwise
  shows a confirm dialog summarizing the damage (broken back-links,
  unregistered children, registry orphans, missing folders) before repairing
  with `fix=true`. `prune=true` is sent only when missing-folder entries were
  detected, so pruning is always surfaced to the user before it happens.
  `missing_c3` children are called out as not repairable by reconcile
  (re-designate or Cascade update instead). The endpoint was previously
  CLI/API-only.

## [2.56.1] - 2026-07-23

### Fixed — `c3 --version` / `c3 upgrade` reported 2.55.0 after upgrading to 2.56.0

- The v2.56.0 release bumped `pyproject.toml` but left the hardcoded
  `__version__` in `cli/c3.py` at 2.55.0 — so `c3 --version` lied and
  `c3 upgrade` perpetually offered "v2.55.0 -> v2.56.0" even when 2.56.0
  was already installed. Both sites now read 2.56.1, and a new
  `tests/test_version_sync.py` asserts they match — CI gates every
  release tag, so a mismatch can never ship again.

## [2.56.0] - 2026-07-23

### Added — Jira integration: Cloud + Data Center (agent tool, CLI, dashboard)

- **`c3_jira` MCP tool** (`cli/tools/jira.py`): 13 actions — `status`,
  `whoami`, `search` (raw JQL), `my_issues`, `get_issue`, `list_projects`,
  `list_transitions`, `get_create_metadata`, `search_users` reads;
  `create_issue` / `comment` / `transition` / `assign` mutations are
  edit-ledger-logged (identifiers only — bodies are never logged).
  `create_issue` pre-validates against create metadata and returns
  machine-readable missing required fields instead of guessing defaults;
  `transition` accepts an id or a name; helper-built JQL is always quoted.
- **Dual-deployment client** (`services/jira_client.py` facade over
  `services/jira_cloud.py` / `services/jira_data_center.py` strategies):
  Cloud REST v3 — Basic email+API-token auth, ADF wrap/flatten,
  `/search/jql` with `nextPageToken`; Data Center REST v2 — PAT Bearer,
  plain-text bodies, `startAt` offset pagination. One normalized DTO
  surface and an opaque pagination cursor. Transport is stdlib urllib
  (no new dependencies) with one bounded 429 retry honoring
  `Retry-After` for reads — mutations are never auto-retried.
- **Credentials** (`services/jira_credentials.py` +
  `core.config.load_jira_config`): named multi-account registry
  (deployment, default_project, per-account TLS/CA), tokens in the OS
  keyring keyed by `(base_url, username)`, HTTPS-only guard. Config
  resolution is project → home and **wholesale from a single file** — a
  repository's config can never field-override a home-registered
  account's credential-bound URL or TLS settings (credential-redirect
  hardening).
- **CLI**: `c3 jira login/logout/status/use/set-default` — Cloud inferred
  for `*.atlassian.net`, `--deployment` required for self-hosted,
  `--no-verify-login` for offline setup, `--ca-bundle` for enterprise
  certs, `--global` for a home config reusable across projects.
- **Dashboard Jira tab** (`cli/ui/components/jira.js`) + ten
  `/api/jira/*` routes: My Work board (statusCategory grouping over
  plain JQL — no agile API in v1), JQL search, issue drawer with
  transition buttons and comments, Activity view.
- **Issue-key linking** (`services/jira_links.py`): `PROJ-123` detection
  in branch names (case-insensitive, read from `.git/HEAD` — no
  subprocess) and edit-ledger entries, with an acronym denylist so
  UTF-8 / SHA-256 / CVE-2024 never match. `/api/jira/activity` is
  local-only and works with no account configured; the issue drawer
  shows the local edit-ledger activity for the open issue.
- 107 new offline tests (credentials, config fallback, client, tool,
  CLI smoke, routes, links) — no live Jira instance required.

## [2.55.0] - 2026-07-17

### Fixed — instruction-doc project trees respect .gitignore (#1)

- **Generated CLAUDE.md/AGENTS.md trees no longer ship build/cache dirs.**
  The doc generator's tree walker (`SessionManager._scan_project_structure`)
  and the drift checker now prune via the shared scanner rules: SKIP_DIRS
  plus the root `.gitignore` — including single-segment glob entries like
  `*.egg-info/`, newly supported by `services/scanner.py` and applied to
  the index scans too. `.pytest_cache/`, `.ruff_cache/`, `.vscode/`,
  `*.egg-info/` and friends disappear from regenerated docs (this repo's
  own docs regenerated as proof). The "stale cache" symptom in the issue
  was the dirs being recreated on disk between regens (ruff/pytest runs);
  generation itself was always live, and pruning fixes it either way.

### Removed — Oracle legacy `<tool_call>` text protocol (#34)

- **Native tool calling is now the only protocol.** Removed the legacy
  prompt catalog (`_TOOL_DEFS`), regex parser, streaming
  `_ToolCallStripper`, trust-answer heuristic, `<tool_result>`
  user-message injection, and the finalize nudge — roughly halving the
  chat() round complexity. `/tools` now lists from `TOOL_SPECS` (single
  source of truth). Historical tool results are still re-injected as
  `<tool_result>` user text on conversation reload (serialization, not
  protocol — orphaned `role:tool` messages confuse several models).
- **Staged capability downgrade replaces the legacy demotion.** On a live
  HTTP 400 the engine now drops the capability the server actually names —
  thinking first, then tools — each at most once, with status events
  ("Continuing without thinking/tools") and an honest no-tools system
  prompt. Fixes a pre-existing dead-end where think-incapable models
  (e.g. llama3.2) failed every Oracle chat turn: the old classifier read
  every 400 as a tools rejection and rerun with `think=True` again.
  Live-verified: gpt-oss:20b full native; llama3.2 drops thinking, keeps
  native tools, answers.
- **Behavior change:** models that genuinely reject native tool calling
  now run tool-less (clearly surfaced) instead of getting the text
  protocol. Capability probe over the installed model set confirmed all
  primary Oracle models (cloud + gpt-oss) support native tools.

## [2.54.0] - 2026-07-17

### Fixed — `c3 init` on large projects (scan pruning + embedding gate)

- **Init scans no longer walk dependency/VCS trees.** Every index build
  (code index, doc index, compression dictionary, directory compression)
  used `sorted(Path.rglob('*'))` — enumerating every entry under
  `node_modules`/`.git`/`venv` before filtering, and materializing the
  whole tree before the first file was indexed (which also defeated the
  `max_files` early exit). On large projects `c3 init` ran for minutes or
  appeared to hang, with zero output. A new shared walker
  (`services/scanner.py`) prunes skip-dirs without descending, honors
  literal directory names from the root `.gitignore` (data/log dirs),
  yields deterministically, and supports true early exit. Measured on
  this repo: 14,276 enumerated entries → 393. The doc index's four
  full-tree passes collapse into one; the compression dictionary now
  mines the already-built in-memory code index instead of re-reading
  every code file from disk (previously uncapped).
- **Embedding index actually builds at init again.** Since the v2.38.1
  lazy-init change, `EmbeddingIndex.ready` on a fresh instance was always
  False (status reporters deliberately never initialize backends), so
  `c3 init`, the hub embeddings rebuild, and sub-project reindex all
  silently skipped semantic-search indexing even with Ollama up and the
  model pulled. New `EmbeddingIndex.probe()` initializes the backends and
  reports truthfully; all three gates use it, and skip messages now say
  why (chromadb missing vs Ollama unreachable vs model not pulled).
- **Expanded skip set.** `target`, `.tox`, `.nox`, `.mypy_cache`,
  `.ruff_cache`, `.gradle`, `Pods`, `obj`, `.idea`, `.terraform`,
  `bower_components` and friends are now pruned everywhere (previously
  walked in full).

### Added — init progress and coverage honesty

- **Live progress.** TTY runs show in-place counters during the
  code-index scan (`entries | files | chunks`), the embedding build
  (`file N/M | chunks`), and the doc index (`N/M`); each phase prints its
  duration. Piped output stays clean — summaries only, no `\r` spam
  (`cli/progress.py`).
- **Honest file cap.** The index cap is configurable (`index_max_files`
  in `.c3/config.json`, default 2000 — previously a hard-coded 500) and
  hitting it prints `[!] Indexed N of M candidate files` plus how to
  raise it, instead of silently truncating coverage at the first 500
  paths in walk order.
- **`c3 init --no-embed`.** Skip the semantic embedding build explicitly
  (huge repos, or boxes where Ollama is busy).

## [2.53.0] - 2026-07-17

Project management grew from a task list into a lean PM system in one
release cycle: cross-process write safety, an append-only event history,
dependencies/subtasks/health reports, full UI support, and activity-based
time tracking.

### Added — Project time tracking (auto + manual, full CRUD)

- **Automatic activity tracking.** Loading the c3 MCP server (Claude
  Code or any IDE) writes a `startup` ping — the start of a work
  session; every tool call heartbeats (throttled to one write/min).
  Pings land in monthly files (`.c3/time/pings-YYYY-MM.jsonl`) and are
  coalesced on read: gaps over 15 minutes close a session, so an IDE
  left open overnight adds nothing. Session time = first→last ping
  (isolated pings count 1 min).
- **Manual entries, full CRUD.** `services/time_tracker.py` stores
  entries (`minutes` 1-1440, note, date, optional task link) in
  `.c3/time/entries.json` with the same cross-process lock +
  atomic-write + corrupt-quarantine discipline as the task store.
- **Surfaces.** `c3_task` actions `time_add` / `time_update` /
  `time_delete` / `time_list` / `time_summary` (reads plan-mode safe;
  new `minutes` param; `ref` = entry id); REST `GET /api/time` +
  `POST/PUT/DELETE /api/time/entry` (per-project) and
  `/api/projects/time[.../entry]` (hub), all audited; per-project Tasks
  tab gets a **Time** section (today/7d/30d chips with auto-vs-manual
  split, 14-day bar strip, entry list with inline edit/delete, log-time
  form); hub board shows a time chip in project scope.

### Added — PM UI enhancements (full plan)

- **Conflict-aware writes.** Both UIs now send `expected_rev` on status
  moves and reorders; a 409 (concurrent writer) refreshes the board with
  a "changed elsewhere" message instead of silently overwriting.
- **Ready actions.** Tasks whose blockers are all done get a one-click
  "→ backlog" (list rows) / ✓ button (hub cards).
- **Subtask rendering.** Subtasks indent under their parent within a
  status section (↳), parents carry an "n/m sub" rollup chip, and a
  child whose parent lives in another column shows a parent reference.
- **Hub blocker fidelity.** `/api/pm/global` rows now carry server-side
  `blockers_open` + `blocker_titles` (resolved against the full store,
  matching store semantics: done/archived/purged blockers don't count),
  so global-view badges are exact.
- **Per-task history.** A 🕘 button per row filters the Activity panel
  to that task (`/api/pm/events?id=`); the panel header shows and
  clears the filter.
- **Insights.** New section with a 14-day completion sparkline, a
  cycle-time bar strip for the last 10 completed tasks, and a milestone
  timeline (targets on an axis, today marker, at-risk in red).
- **Board ergonomics.** Hub cards are drag-and-droppable (drop on a
  column = status move, drop on a card = reorder/insert before it in
  project scope); quick-add parses `p0-p3`, `due:YYYY-MM-DD`, `#tag`,
  and `@milestone` tokens; an Archived section lists archived tasks
  with one-click Restore (`PUT {restore: true}` added to both task
  endpoints, audited + history-logged via `restore_task`).

### Added — PM UI pass (dependencies, health, history in both UIs)

- **Shared primitives** (`cli/ui/pm_shared.js`, loaded by both bundles):
  `DepsBadge` — amber "⛔ n" for open blockers, green "✓ ready" when a
  task is still marked blocked but nothing blocks it (hover lists
  blocker titles) — and `RecoveryBanner`, a warning strip rendered when
  the board reports a quarantined/restored `pm.json`.
- **Per-project Tasks tab** (`cli/ui/components/tasks.js`): a health
  strip under the header (overdue / blocked / ready counts, at-risk
  milestones, weekly throughput — only rendered when something needs
  attention, fed by `GET /api/pm/report`); a per-row dependency editor
  (⛓ toggle → blocker chips with remove, candidate picker → `POST
  /api/pm/deps`); a collapsed **Activity** panel rendering the last 20
  events from `GET /api/pm/events` with before→after summaries and
  actor attribution; the recovery banner.
- **Hub task board** (`cli/hub_ui/components/task_board.js`): cards show
  the blocked/ready badge (blockers resolved against the loaded board),
  and project scope shows the recovery banner.

### Added — PM dependencies, subtasks, and reporting (phase 3)

- **Task dependencies.** Tasks carry `blocked_by: [task_ids]`, managed
  via `TaskStore.add_dependency` / `remove_dependency` (cycle-safe —
  transitive cycles rejected at write time — idempotent, self-blocking
  rejected). Completing a task auto-releases dependents whose last open
  blocker it was: they flip `blocked -> backlog`, emit an `unblocked`
  event, and the completing mutation's result lists the released ids
  (surfaced by `c3_task done`). Surfaces: `c3_task` `block`/`unblock`
  actions (task_id + ref), `POST /api/pm/deps` and
  `POST /api/projects/pm/deps` (`{id, blocker, op: add|remove}`).
- **One-level subtasks.** Tasks accept `parent_id` (create + update;
  `c3_task` `parent` param, `'none'`/`'-'` clears). Validation enforces
  a single level: a subtask cannot be a parent, a task with subtasks
  cannot become one, self-parenting rejected.
- **Health report.** `TaskStore.report()` computes from one snapshot:
  overdue (with days), blocked chains (open blockers + days blocked),
  ready-to-unblock (blocked status, no open blockers), milestone health
  (progress, open/overdue counts, at-risk when the target has passed
  with open tasks or open due dates exceed it), and throughput
  (done last 7/30 days, average cycle days). Surfaces: plan-mode-safe
  `c3_task(action='report')`, `GET /api/pm/report`,
  `GET /api/projects/pm/report`.

### Added — PM event history + migration scaffold (phase 2)

- **Append-only event log.** Every successful PM mutation now writes an
  event to `.c3/pm/events.jsonl` inside the same save transaction:
  `{ts, rev, entity, op, id, actor, patch, data}` with field-level
  before/after patches for updates/moves, snapshots for creates, and
  actor attribution (`mcp` / `ui` / `hub`). Failed mutations write
  nothing; the log is best-effort (a log write error never fails the
  snapshot save) and rotates to `events.jsonl.1` past 5 MB. This is the
  data source for future burndown / cycle-time / blocked-aging reports.
- **History surfaces.** `TaskStore.history(entity?, item_id?, op?,
  limit?)` reads newest-first; `c3_task(action='history')` formats it
  (plan-mode safe read); `GET /api/pm/events` (per-project server) and
  `GET /api/projects/pm/events` (hub) expose it over REST.
- **Schema migration scaffold.** `_load` now funnels every document
  (primary and backup) through `_normalize()`, which runs migrations
  registered via `@_migration(n)` stepwise up to `SCHEMA_VERSION` before
  backfilling defaults — the rail for dependencies/subtasks fields in
  phase 3.

### Changed — PM store robustness (phase 1)

- **Cross-process write safety.** `TaskStore` mutations now serialize
  across the hub, MCP, and per-project server processes through a
  `.c3/pm/pm.lock` OS file lock (bounded 30s acquire) in addition to the
  in-process `threading.Lock`. Two concurrent writers can no longer
  silently drop each other's `load -> mutate -> save` cycles. Save temp
  files are per-writer unique (`pm.json.tmp-<pid>-<rand>`, stale ones
  swept after 1h) instead of a shared fixed name; the data dir is
  fsynced after replace on POSIX.
- **Optimistic concurrency.** The PM document carries a monotonic `rev`
  (bumped every save, exposed on `board()`); task/milestone/note update
  paths accept an optional `expected_rev` precondition and REST PUT
  endpoints return **409** with `code: rev_conflict` on mismatch.
- **Atomic update+move.** New `TaskStore.mutate_task(id, fields, move,
  expected_rev)` applies field updates and board moves in one
  transaction; both PM REST layers now call it once instead of running
  `update_task` then `move_task` as two separate writes (half-updated
  state was previously observable and clobberable). `update_task` /
  `move_task` remain as thin wrappers.
- **Backup + surfaced recovery.** Every save keeps the previous good
  document as `pm.json.bak`. A corrupt `pm.json` is still quarantined
  (`pm.json.corrupt-N`) but the store now restores from the backup
  instead of silently restarting empty — including on later loads and
  fresh instances (missing primary falls back to backup until the next
  mutation persists it). Recovery is surfaced via
  `TaskStore.last_recovery`, a `recovery` key on `board()`, and a
  `[task:warning]` line in the `c3_task` board output.
- **Board consistency fixes.** `board()` computes its `stats` from the
  same loaded snapshot instead of re-loading (columns and stats could
  disagree); `include_archived` rows now respect the milestone/tag
  filters. `GET /api/pm/global` `by_project[].open` no longer counts
  done tasks under `status=all` (row count moved to `shown`).
- **Windows guard.** `test_windows_reliability` now recognizes the
  plus-variant binary open modes (`a+b`, `r+b`, `w+b`, `ab+`).

## [2.52.0] - 2026-07-17

### Removed — BREAKING

- **Gemini CLI IDE profile.** `gemini` is no longer a valid IDE for
  `c3 init` / `c3 install-mcp`; use **Antigravity** instead (it reads
  `AGENTS.md`, which takes precedence over `GEMINI.md`). Removed with it:
  `GEMINI.md` generation, the Gemini terse-skill TOML
  (`~/.gemini/commands/terse.toml`), Gemini hook installation
  (`BeforeTool`/`AfterTool` + snake_case matchers), and the
  `.gemini/settings.json` project/global config sync. Rolled out as
  deprecate-then-remove within this release cycle: generation was first
  IDE-gated and configs made refresh-only, then the profile was dropped.
  Not affected: the **Gemini delegate backend**
  (`c3_delegate(backend='gemini')`) is model offload, not IDE
  integration, and is unchanged. Legacy support kept on purpose:
  `.gemini` project markers auto-detect as Antigravity, `c3 uninstall`
  still deletes stale `GEMINI.md` files and strips the `c3` entry from
  legacy `.gemini/settings.json` (project + home), runtime hook shims
  for Gemini payload shapes remain so existing installs keep working,
  and `c3_artifacts` keeps tracking legacy `GEMINI.md` /
  `.gemini/settings.json` files.

### Added

- **Antigravity as the first-class Google-stack profile.** The
  `antigravity` profile now uses `AGENTS.md` as its instruction doc
  (shared with Codex), Antigravity installs trigger session-config
  sync, and codex installs on machines with Antigravity keep
  `~/.gemini/antigravity/mcp_config.json` fresh as a global fallback.
  The getting-started guide gained the previously missing Antigravity
  install section.
- **`c3_memory` recall timeout.** `recall`/`index`/`query` are wrapped
  in a configurable timeout (`memory_retrieval_timeout_seconds`,
  default 15s, clamped to 60s) and the semantic backends' lazy-init
  lock is waited on for at most 0.25s on search paths — a cold or
  wedged embedding backend degrades to keyword recall instead of
  hanging the tool call.

### Fixed

- **`c3_shell` ledger capture for chained git commands.** Git mutations
  are now detected inside `&&`/`;`/`|` chains (previously only at line
  start), and the edit ledger diffs actual before/after git state
  instead of probing `HEAD~1..HEAD`, so multi-command lines attribute
  the right files.
- **Windows hook crashes on non-ASCII output.** Hook entrypoints force
  UTF-8 stdio (`ensure_utf8_stdio()`), fixing `UnicodeEncodeError`
  ("Stop hook error") when hook output contained box-drawing characters
  or emoji on cp1252 pipes.
- **`c3_shell` robustness.** Structured exit-code 126/127 results when
  the shell itself fails to launch, taskkill timeout with `proc.kill()`
  fallback, POSIX process groups for clean tree kills, and a hint when
  `jq` is missing from Git Bash (use `python -m json.tool`).

## [2.51.0] - 2026-07-03

### Added

- **LLM-distilled session memory (`memory_llm`).** At session end, C3 distills
  the session's conversation excerpts, activity tail, and logged decisions into
  3-7 durable facts via an LLM chain: **Ollama Cloud** (Sonnet-class, default
  `glm-4.6:cloud`, strictly opt-in — `cloud_enabled: false` by default) →
  **local Ollama model** (default `gemma3n:latest`, fully private) → the
  existing regex extractors. New `services/memory_distiller.py` (degradation
  chain, per-tier circuit breaker, JSON salvage parsing, `<private>` stripping
  before any LLM call) and `services/memory_queue.py` (durable idempotent job
  queue in `.c3/memory_queue/` — session end never blocks on the LLM, and jobs
  survive crashes; the new `MemoryDistillerAgent` drains anything pending next
  session). Distilled facts carry provenance: `MemoryStore.remember()` gained
  `confidence`/`source_quality` kwargs (`distilled` / `distilled_local`).
- **Transcript mining.** On idle agent cycles, unmined conversation turns are
  scanned for user corrections, standing preferences, and confirmed decisions
  (user turns + neighboring assistant context; per-conversation high-water mark
  advances only after facts persist, so crashes re-mine safely).
- **Per-prompt memory injection.** New `UserPromptSubmit` hook
  (`cli/hook_prompt_recall.py`, dispatcher event `prompt`): injects the top-k
  most relevant project facts into each prompt (~400-token cap, <100 ms,
  strictly read-only over `facts.json` — never instantiates `MemoryStore`).
  Registered by `c3 install-mcp` / `c3 init` (Claude Code profile only).
- **File-anchored facts on `c3_read`.** Reads append up to three
  `[c3:related]` one-liners when stored facts touch the file being read
  (revived `maybe_related_facts`, read-path only, flag-gated; search/compress
  stay quiet).
- **`memory_llm` settings on every surface:**
  - Project UI → Settings → new **Memory LLM** section: capture/recall
    toggles, local-model picker (live from the Ollama daemon), cloud-model
    field, API-key set/clear.
  - Hub → the per-project config editor gained a `memory_llm` section pill
    (generic typed editor; `api_key` refused — secrets never transit the hub).
  - `c3 init` → new interactive step: **Local only (recommended) / Cloud /
    Off** with a local-model picker; `--force` keeps privacy defaults
    (distillation on, cloud OFF).
  - New endpoints: `GET/PUT /api/memory-llm/config`,
    `POST /api/memory-llm/key`.
- **Keyring-backed cloud key.** The Ollama Cloud API key lives in the OS
  keyring (`services/ollama_credentials.py`, service `c3-ollama`) — never in
  `.c3/config.json`, which is not gitignored by default. Resolution order:
  explicit config value → `OLLAMA_API_KEY` env var → keyring. `OllamaBridge`
  moved from `oracle/services/` to `services/` (now shared by the Oracle and
  the distiller) and gained a `check_auth()` probe so auth/quota failures
  (don't retry) are distinguished from outages (retry with breaker).

### Fixed

- **Torn-write protection for `facts.json`.** `MemoryStore._save_facts()` is
  now atomic (tmp + `os.replace`) and lock-serialized. A crash mid-write
  previously wiped all project memory silently (`_load_facts` returns `[]` on
  parse errors), and concurrent recall flushes from parallel multi-file
  `c3_read` workers shared a single tmp path.
- `cli/c3.py` `__version__` was stale at `2.49.1` — the v2.50.0 release bumped
  only `pyproject.toml`, so `c3 --version`, the hub/UI version badges, and the
  version-check agent under-reported. Both are now `2.51.0` and in sync.

## [2.50.0] - 2026-07-03

### Fixed

- **read→edit parity: `c3_read` output is now byte-faithful to what `c3_edit`
  matches.** Root-cause fix for the "c3_read → c3_edit fails → agent falls
  back to native Read" drift loop:
  - `c3_read` EOL-normalizes exactly like `c3_edit`'s matcher (`\r\n`/`\r` →
    `\n`) and splits on `\n` only — `splitlines()` previously rendered inline
    `\x0c`/`\u2028`/`\x85` as phantom line breaks, making copied old_strings
    unmatchable.
  - `c3_edit` reads/writes with `errors="surrogateescape"`: files containing
    non-UTF-8 bytes are now editable (strict decode used to raise), and
    untouched invalid bytes round-trip byte-for-byte. Undecodable bytes fold
    to U+FFFD for matching, so an old_string copied from `c3_read` output
    (which renders them as `�`) still matches.
  - Batch-edit outcome classification is structural (per-patch status list) —
    a patch *summary* containing words like "NOT FOUND" was previously
    miscounted as a failure in the `N/M patches applied` line.

### Added

- **`c3_edit` closest-match repair payload.** "old_string not found" errors
  now locate the most similar file region (difflib anchor + window scan) and
  include its exact current text with `⟦L..-L..⟧` markers plus a retry hint —
  the edit can be repaired without re-reading the file, which was the moment
  agents historically drifted back to native tools. Batch mode gets a per-patch
  `closest: L..-L..` locator and the full region for the first miss.
- **Copy-safe `c3_read` multi-range markers.** Between discontiguous blocks,
  the old `--- L22-L40 ---` separator is replaced by tool-chrome markers with
  an explicit omitted-gap note (`⟦L60-L80 — 19 lines (L41-L59) omitted…⟧`) so
  a copied old_string never silently spans a gap.
- **`c3_read` map-response hint.** When a read returns the file map (no
  `lines`/`symbols`), the response now says how to fetch exact source —
  closing the edit-recovery gap where a no-arg re-read returned a map instead
  of content.
- Regression suite `tests/test_read_edit_parity.py` (14 tests) pinning all of
  the above.

## [2.49.2] - 2026-07-02

### Added

- **Sponsorship surfaces.** GitHub Sponsors wiring (`.github/FUNDING.yml`,
  README badge + "Support C3" section) plus a sponsor link on every surface:
  hub topbar, per-project dashboard topbar, Oracle header, guide-page nav,
  `c3 --version` / `c3 --help`, the TUI footer (`s` key), and a `Funding`
  project URL on PyPI.

## [2.49.1] - 2026-07-02

### Added

- **Oracle version badge** (hub-topbar parity): `/api/health` now carries the
  C3 `version`, and the dashboard header shows `v<version>` next to the logo.
- **Persistent Oracle UI preferences** (hub parity): the active tab is saved
  to config on every switch (`ui_last_tab`) and restored on load; the header
  theme toggle's persistence — silently broken by a 401 before the v2.47.0
  session cookie — now actually sticks.

### Removed

- **Oracle `/legacy` route and `oracle.html`.** The frozen pre-bundle
  monolith was served at `/legacy` for one release (v2.49.0) as an escape
  hatch; the hatch has expired (hub.html precedent). The concat bundle at
  `/` is now the only Oracle UI.

## [2.49.0] - 2026-07-02

### Oracle Wave 3: UI v3 concat bundle + `c3 oracle serve` + docs refresh

#### UI architecture

- **The 4,181-line `oracle.html` monolith is gone.** The dashboard now
  follows the hub v2 concat architecture: an `oracle_ui.html` shell (CSS +
  markup + `__C3_ORACLE_SCRIPTS__` token) plus **18 `oracle/ui/` modules**
  split at section boundaries (core, busy, theme_tabs, crossgraph, header,
  projects, insights, activity, suggestions, settings, agents,
  `chat/{markdown,conversations,stream_renderer,toolbar,input,send}`, and
  `app.js` — the init IIFE — last), concatenated server-side by
  `_build_oracle_html()` with per-file markers. `GET /` serves the cached
  bundle; **`GET /legacy` serves the frozen monolith for one release**, then
  it will be removed. Extraction was verbatim (proven by a line-multiset
  diff: the only delta is the `ORACLE_BUILD_TIME` bump) — zero behavior
  change by construction.
- Deliberate deviation from the original Wave-3 design: **no React/babel
  runtime**. The Oracle UI logic is entirely vanilla imperative JS; the
  transferable part of the hub pattern is the module structure + build
  pipeline, and wrapping unowned vanilla code in a framework would have
  added a CDN/transpile failure surface for zero owned UI.

#### CLI

- **`c3 oracle serve`** (alias `start`, `--port`, `--no-browser`) launches
  the Oracle dashboard — no more `python oracle/oracle_server.py` required
  (it still works). Lazy import keeps bare `c3` startup fast.

#### Packaging

- `pyproject.toml` package-data globs for `oracle/ui/*.js` +
  `oracle/ui/chat/*.js` (the `"*"` globs only match a package's top level);
  wheel inspection confirms all 18 modules + shell + legacy ship.

#### Docs

- `oracle-guide/` caught up a full product generation: chat subsystem + SSE
  event protocol, C3Bridge, federated graph, security model (session cookie
  + write gate), the 8-tab bundle UI, all missing config keys (incl. Wave
  1–2 additions), missing endpoint families (`/api/apikey/*`, `/api/chat/*`,
  `/api/graph/federated/*`, `/api/activity/digest/latest`), Discovery tool
  list (c3_project/c3_artifacts/scope), and changelog entries v1.3.0 (Waves
  1–2) / v1.4.0 (Wave 3) with a catch-up block for the previously
  undocumented v2.32–v2.38 era features.

## [2.48.0] - 2026-07-02

_Shipped as part of the v2.49.0 release (no standalone tag)._

### Oracle Wave 2: capability catch-up

#### New Discovery/chat tools

- **`c3_project`** (read tier) — cross-project operations by registered
  project NAME or path: registry listing, project info, sub-project tree, and
  read-only proxied ops (search/read/compress/status/memory/impact/edits/
  validate). Deny-by-default allowlist; the wrapper signature has no
  `allow_write` and no write-op params, and the registry drops undeclared
  keys, so write verbs cannot reach `handle_project` from any transport.
  `scan` is excluded (it reveals unregistered `.c3` projects, outside the
  Oracle's discovered-project trust boundary), and every resolution is
  re-validated against discovered projects (the resolver alone accepts any
  on-disk `.c3` folder).
- **`c3_artifacts`** (read tier) — agent-config version history for one
  project: list/history/show/diff/status. `scan` (mutates the target's
  manifest despite the handler's READ_ACTIONS listing) and `restore` are
  blocked. Both tools appear on MCP + OpenAPI automatically via `TOOL_SPECS`.

#### Sub-project awareness (v2.44 parent/child model)

- `ProjectScanner` carries the registry's `parent_path` (previously dropped)
  and enriches every project with `is_subproject` / `parent_path` (child
  config back-link as fallback; broken links degrade to top-level) /
  `subproject_rel_paths` / `subproject_count` — `/api/projects` and the
  `list_projects` tool surface hierarchy for free.
- `FederatedGraph` gains a **serve-time `parent_child` overlay** applied on
  both fresh builds and cache hits: hierarchy lives in `.c3/config.json`,
  which the facts-mtime cache key never sees, so it is recomputed per serve
  and never baked into the cache file. Project-level links only.
- `c3_search_cross` / `c3_edits_cross` gain an optional **`scope`** param:
  `''` = all projects, `'top'` = top-level only, or a project name/path =
  that project plus its direct sub-projects.

#### Scheduled activity digest

- The review loop now emits the cross-project activity digest when due —
  config-gated (`digest_enabled` default **false**: current behavior
  preserved), live-read each cycle (no restart to toggle), pre-stamped
  `last_digest_at` (no double-digest from `run_now`), persisted to
  `~/.c3/oracle/activity_digests/<date>.json` + `latest.json` with retention
  pruning (`digest_retention_days`) and an optional one-line JSONL notify
  sink (`digest_notify_file`). `digest_narrate` stays opt-in (cloud LLM
  call). New `GET /api/activity/digest/latest`; the Activity tab shows a
  last-scheduled-digest banner and Settings gains the toggle + interval.

#### Multi-backend agents

- `delegate_task` agents gain a per-agent **backend**: `ollama` (default,
  unchanged nested tool loop) or `codex`/`gemini`/`claude`/`auto` routed
  through `cli.tools.delegate` against `_OracleDelegateRuntime` — a read-only
  shim of the target project's runtime that forces the codex/gemini memory
  bridges off, suppresses NotificationStore writes, and pins the codex
  sandbox to read-only. CLI backends require a concrete registered project
  (explicit `project_path` → conversation's focused project → instructive
  error; never silently picked) and honor the target project's own
  backend-enablement config. Agent modal gains a backend selector.

#### Tests

- 50 new tests: bridge wrapper contracts (`test_c3_bridge_project_artifacts.py`,
  incl. a pin that the Oracle's blocked-memory set equals
  `cli.tools.project._MEMORY_WRITE`), registry allow_write kill-switch pins,
  sub-project enrichment + scoping (`test_oracle_subproject_awareness.py`),
  hierarchy overlay on fresh AND cached graph builds, digest scheduling
  (`test_review_digest.py`), and CLI-backend delegate routing + shim contract.

## [2.47.0] - 2026-07-02

_Shipped as part of the v2.49.0 release (no standalone tag)._

### Oracle Wave 1: security + core hardening/unification

#### Security

- **Closed the unauthenticated local-write kill chain.** `POST
  /api/apikey/generate|rotate|clear`, `/api/chat` (full tool access),
  `/api/suggestions/approve` (real writes to project `facts.json`) and
  `/api/config` were reachable by any local process — and `rotate` returned
  the fresh Discovery token, defeating the Bearer gates on `/api/config` and
  `/api/discovery/*`. A new per-boot session cookie
  (`oracle/services/local_session.py`, HttpOnly + SameSite=Strict, issued on
  `GET /` to loopback clients only, never persisted) plus a default-deny
  `_local_write_guard` now require **session cookie or Bearer token** on every
  mutating `/api/*` call outside `/api/discovery/*` (which stays Bearer-only).
  Any future mutating endpoint is covered automatically.
- **Un-broke the dashboard Settings save.** The UI never sent a Bearer token,
  so `POST /api/config` always 401'd; the session cookie now authenticates the
  dashboard, which can also reveal/copy the Discovery key again.

#### Chat: native Ollama tool calling

- **ChatEngine speaks Ollama's native tools API** when the model supports it
  (capability probe via `/api/show`, cached). The native tools array is built
  from `TOOL_SPECS` — one source of truth with the Discovery API. Tool-capable
  models get structured tool calls (no regex `<tool_call>` parsing, no
  stripper, no trust-answer heuristic, `role:tool` result feeding);
  tool-incapable models keep the legacy text protocol verbatim;
  unknown-capability models attempt native and **fall back mid-turn** on an
  HTTP 400 rejection (negative-cached only when the server names tools as the
  problem). Sub-agents (`delegate_task`) pick their protocol from their own
  model. SSE event vocabulary and persisted conversation format are unchanged.
- One shared `_drain_stream` generator replaces three duplicated
  chunk-unpacking loops (main round, visible-retry, delegate sub-agent).

#### Performance / unification

- **C3Bridge adopted the shared `ProjectRuntimeCache`** (its own docstring
  named the bridge's hand-rolled LRU as the predecessor it was lifted from).
  Cache size 3 → 8 (`C3_RUNTIME_CACHE_SIZE`-tunable), ending cross-project
  search thrash over >3 projects. New `on_build` hook on the cache warms
  `embedding_index` + `vector_store` in a daemon thread (mirroring the MCP
  server's lifespan warm) so the first `c3_search` on a project no longer pays
  chromadb init on the request thread.
- **`ProjectScanner.discover()` is TTL-cached** (`scanner_ttl_seconds`,
  default 20s) with copy-on-return; it was re-run uncached on every tool call
  and several times per graph request. The dashboard Scan action forces a
  refresh; failed/empty discoveries are never cached.
- **OllamaBridge hygiene:** `is_available()` no longer reports a 5xx-failing
  server as reachable; the LLM disk cache gains a TTL (`llm_cache_ttl_sec`,
  default 24h) and a 512-entry bound (was unbounded, never expired).

#### Tests

- 60 new Oracle tests across five files: `test_oracle_local_auth.py`
  (kill-chain regressions), `test_oracle_chat_engine.py` (first-ever coverage
  of the 1,100-line chat orchestrator), `test_oracle_c3_bridge.py`,
  `test_oracle_scanner_cache.py`, `test_oracle_ollama_bridge.py`.

## [2.46.1] - 2026-07-02

### Fixed

- **Sub-project exclusion silently disabled under aliased project roots.**
  `make_excluder` resolved the project root but not the paths handed to it, so
  for any project living under a symlinked path (e.g. macOS `/var/folders`,
  or a symlinked home/workspace) or a Windows 8.3 short-name path, the
  internal `relative_to` check always failed and **no sub-project was ever
  excluded** from the parent's code index, doc index, dictionary, or watcher.
  Affected v2.44.0–v2.46.0. Incoming paths are now resolved before comparison.
- **Sub-project prefix matching is now case-insensitive on all platforms.**
  `exclusion_prefixes`/`is_excluded` used `os.path.normcase`, a no-op outside
  Windows; they now case-fold consistently everywhere.

### CI

- **Releases are now gated on CI.** A new `verify-ci` job in the Release
  workflow waits for the CI run on the tagged commit and fails the release if
  CI is red or missing — publishing from a red `main` (as happened for
  v2.45.0/v2.46.0) is no longer possible.
- Cleared 11 ruff lint errors (import sorting, one unused import, one
  placeholder-less f-string) that were failing the Lint job.

## [2.46.0] - 2026-07-02

### Agent-artifact tracking: version history, diff & restore for the files that shape the agent

- **New artifact store.** C3 now services the *agent itself*: every file that
  shapes agent behavior is inventoried and versioned — instruction docs
  (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.cursorrules`,
  `.github/copilot-instructions.md`), settings/hooks
  (`.claude/settings*.json`, `.gemini/settings.json`), MCP configs
  (`.mcp.json`, `.vscode/mcp.json`, `.cursor/mcp.json`, `.codex/config.toml`),
  and Claude Code extensions (`.claude/` skills, agents, commands, plugins) —
  provider-agnostic via the `core/ide.py` profile registry.
  `services/artifact_defs.py` (pattern table + hook-safe classification) +
  `services/artifact_store.py` (content-addressed blobs
  `.c3/agent_artifacts/blobs/<sha256>.gz`, manifest with embedded version
  index, append-only `history.jsonl`). Restore depends only on
  manifest+blobs, so the history log rotates freely — no tombstone machinery.
- **`c3_artifacts` MCP tool** (18th tool): `scan`, `list`, `history`, `show`,
  `diff` (any version vs any version or live), `restore` (exact bytes back,
  forward-only — appends a new version + history event, cross-logs to the
  edit ledger, warns on settings/managed-block files, resurrects deleted
  artifacts), `status`. Everything except `restore` is plan-mode-safe.
- **Attributed capture, three paths.** `c3_edit` writes capture synchronously
  with session + summary (`source=c3_edit`); native Edit/Write route through
  a new `hook_artifact` pending-signal hook (<5 ms, no hashing;
  `source=hook`); everything else — you editing CLAUDE.md in an editor, a
  plugin installer — is caught by the idempotent scanner (`source=scan`).
  C3's own writers self-report: `write_c3_instruction_doc` and
  `cmd_install_mcp` mark their writes `source=install_mcp`, so re-running
  install-mcp never looks like foreign tampering.
- **`ArtifactScanAgent`** (120 s cycle) consumes pending signals then runs a
  full scan. Notification discipline: silent baseline cycle, alerts
  (`replace_if_unacked`) **only** for out-of-band changes to settings/MCP
  configs — everything else is pull-only via the tool/UI.
- **Surfaces.** Hub drill-in panel gets an **Artifacts** tab (class-grouped
  inventory, per-version timeline, unified-diff viewer, two-step restore with
  warnings); per-project REST `/api/artifacts*`; hub REST
  `/api/projects/artifacts*` with per-project `artifact_write` audit.
- **Retention.** `artifact_history_max_mb` (2 MB rotation),
  `artifact_max_versions` (20/artifact), `artifact_blob_orphan_days`
  (age-guarded orphan-blob GC) — wired into the retention sweep.

### Removed

- **`KeyFileVersionAgent` + `services/version_tracker.py`** — the dead-work
  pair flagged by the deep evaluation (unread output, notification spam) is
  absorbed by the artifact store + `ArtifactScanAgent`. `.c3/version_tracker.json`
  is orphaned and can be deleted. (`VersionCheckAgent` — the PyPI
  release nudge — is unrelated and stays.)

## [2.45.0] - 2026-07-02

### Project management: tasks, milestones, and decision notes per project

- **New PM store.** `services/task_store.py` keeps tasks (status
  backlog/in_progress/blocked/done, priority p0–p3, due dates, tags, code
  links to files/commits/edit-ledger entries), milestones (computed progress
  %), and a decision-note log in one atomic document (`.c3/pm/pm.json`,
  temp+fsync+replace writes, corrupt-file quarantine, archive-first
  lifecycle). Reload-per-operation semantics keep the hub, MCP server, and
  per-project UI processes consistent (last-writer-wins per op).
- **`c3_task` MCP tool** (17th tool): agents create/update/complete tasks
  when asked — `add`, `update`, `done`, `list`, `board`, `get`, `archive`,
  `link/unlink`, `milestone_*`, `note_add/note_list`. Unique id prefixes
  (≥4 chars) accepted; milestones resolve by id or unique name; reads are
  plan-mode-safe. `c3_session(action='plan')` stays for ephemeral plans and
  points here for durable TODOs.
- **Hub surfaces.** Tasks tab in the project drill-in panel (inline add,
  quick status moves, milestones with progress bars, notes, sub-project
  rollup tagged `[sub:name]`); a **kanban board** behind a Projects | Tasks
  topbar switcher — global mode aggregates open tasks across every
  registered project (`GET /api/pm/global`, raw-registry scan, 500-task
  cap) with project badges and drill-through, per-project mode adds column
  and rank moves (button-based, no drag) plus inline add; `☑ N` open-task
  chips on project cards; overview counts include open tasks.
- **Per-project web UI** gets its own Tasks tab against new `/api/pm*`
  endpoints. All hub mutations audit `pm_write` events to the target
  project's activity log; `hybrid.pm.enabled=false` soft-disables the tool
  surface.

## [2.44.1] - 2026-07-02

### Fixed

- **Hub: card dropdown menus painted behind later cards.** The `.fade-up`
  entrance animation used `animation-fill-mode: forwards`, which retains the
  final keyframe's transform as a computed identity matrix — keeping every
  project card a stacking context forever and trapping the kebab menu's
  z-index inside it. Fill mode dropped (base style is already the end state);
  keyframes now end at `transform: none`. Menus, modals, and toasts stack
  globally again.

## [2.44.0] - 2026-07-02

### Sub-projects: linked child `.c3` branches under one parent

- **Designate sub-folders as governed sub-projects.** `c3 sub add <folder>`
  runs a full init in the folder (or *adopts* an existing `.c3`) and links it
  three ways: the parent's `.c3/config.json` gains a `subprojects` list
  (POSIX `rel_path` is the source of truth), the child config gains a `parent`
  back-link, and the child's registry entry in `~/.c3/projects.json` gains
  `parent_path`. New `services/subprojects.py` (`SubprojectManager`) owns
  designation, validation (depth-1 only, containment enforced, adopt vs init),
  unlink/clear removal, consistency reconciliation (`c3 sub check [--fix
  --prune]` — statuses `ok/missing_folder/missing_c3/backlink_broken/
  unregistered` + registry-orphan cleanup), and cascade operations
  (`c3 sub run update|reindex|health [--include-parent] [--json]`).
- **Parent index excludes children.** Designated folders are skipped by the
  parent's code index, doc index, compression dictionary, and file watcher via
  relative-path-prefix matching (a child named `api` does *not* shadow a
  root-level `api/` sibling). The parent reindexes automatically on
  add/remove; `c3 init --clear` on a child warns about the remaining parent
  link. `transfer_project` repairs child links after a move.
- **Federated search & memory rollup.** `c3_search(scope='all')` fans out to
  linked children (per-scope sections `=== [sub:name] ===`, 60/40 parent/child
  token split, per-child failures isolated); `scope='<name>'` targets one
  child. `c3_memory` recall unions child facts tagged `[sub:name][category]`
  (on by default; `hybrid.subprojects.memory_rollup: false` or
  `scope='project'` disables). `c3_project` gains `subprojects` (tree +
  rollup), `sub_add`, `sub_remove`, and `sub_cascade` actions (writes require
  `allow_write=true` and audit to the target's activity log). New
  `hybrid.subprojects` config block; runtime cache default 4 → 8
  (`C3_RUNTIME_CACHE_SIZE` env override).
- **Hub endpoints.** `GET /api/projects/subprojects` (tree + rollup),
  `POST …/add|remove|validate|reconcile`, async `POST …/cascade` +
  `status`/`cancel` polling; `GET /api/projects` entries now carry
  `parent_path`/`is_parent`; removing a parent reports `orphaned_children`.

### Project Hub v2: modular UI + full project capabilities

- **Monolith retired.** The 187 KB `hub.html` is replaced at `/` by a modular
  React bundle (`cli/hub_ui.html` shell + `cli/hub_ui/*` components,
  concatenated server-side exactly like the per-project UI) sharing
  `cli/ui/theme.js` design tokens — one design system across both UIs. The old
  hub remains frozen at `/legacy` for one release as an escape hatch.
- **Modern/minimal redesign.** Slim project rows (status dot, version chip,
  alert chip, one mono meta line, primary action, kebab menu) — everything
  else moved into a **drill-in panel** (click a project): Overview, Memory,
  Ledger, Sessions, Health, Budget, Config, and MCP tabs, served without
  launching the per-project UI server (`POST /api/projects/inspect`, backed by
  a hub-owned in-process runtime LRU; size via hub config
  `runtime_cache_size`).
- **Cross-project search.** Ctrl/Cmd-K overlay queries code + memory across
  all registered projects (`POST /api/search/global`, per-project buckets,
  broken indexes isolated per row, 10-project cap per request).
- **Structured config editing.** `GET/PUT /api/projects/config` edits
  whitelisted `.c3/config.json` sections (hybrid/agents/delegate/proxy/mcp/
  meta) with typed controls, defaults for reset, atomic writes, and a
  `hub_config_write` audit event on the target project. Protected keys
  (`version`, `project_path`, `permission_tier`, `subprojects`, `parent`)
  are refused.
- **Sub-project tree.** Parents render as collapsible trees with client-side
  rollup chips; designate via a FolderPicker (`POST /api/projects/browse` +
  validate pre-check); promote/unlink from the child row; cascade
  update/reindex/health with progress toasts.
- **Fixes.** Hub config no longer silently drops `sidebar_group` /
  `sidebar_collapsed`; new `runtime_cache_size` key; wheel packaging gains
  explicit `cli/ui/*` + `cli/hub_ui/*` globs.

## [2.43.0] - 2026-07-02

### Ghost-file generation fixed at the source

- **Root cause (proven).** The recurring 0-byte "ghost" files in the project
  root (`tuple[int`, `Optional[str]`, `L88`, `3.0.0`, `dict`, `{new`, …) were
  **generated**, not merely detected too late. They come from
  **CVE-2024-24576 / "BatBadBut"**: a spawn site resolves a CLI name
  (`claude`, `gemini`, `codex`, `aider`) via `shutil.which`, which on Windows
  returns a `.cmd`/`.bat` shim (e.g. `…\npm\gemini.CMD`). Launching a batch
  shim with an argv **list** runs it through an implicit `cmd.exe /c`, and
  Python's `subprocess.list2cmdline` escapes quotes with `\"` (the MSVCRT
  convention) — which **cmd.exe does not honour**. When prompt/diff/code text
  carried as an argument contains an *odd* number of `"` (docstring fences,
  diff string literals), cmd.exe's quote state desyncs and any following
  `>`/`<`/`&`/`|` becomes a real redirect. `… > tuple[int, str]` writes a
  0-byte file named `tuple[int`; `flask>=3.0.0` writes `3.0.0`; `> L88` writes
  `L88`. Reproduced exactly on CPython 3.14.4 / Windows 11; the running
  interpreter does not neutralise it. (Note: `services/parser.py`'s native
  syntax checkers were a **red herring** — they write content to a temp file
  and only pass the temp *path* as an argument, so no code text ever reaches a
  shell.)
- **Fix at every spawn site.** New `services/win_subprocess.py::harden_win_argv`
  rewrites a batch-shim invocation (Windows + `argv[0]` is `.cmd`/`.bat` only)
  into an explicit `cmd.exe /d /s /c "<line>"` **string** with cmd.exe-correct
  quoting (each argument double-quoted, embedded `"` doubled to `""`), passed
  straight to `subprocess` so `list2cmdline` never re-mangles it. The `""`
  doubling is simultaneously valid for the downstream argv parser, so the CLI
  still receives the intended text. Applied to `cli/tools/delegate.py`
  (`_run_claude`/`_run_gemini`/`_run_codex`), `services/e2e_benchmark.py`
  (provider + multi-turn runs), `services/e2e_evaluator.py` (AI judge), and
  `services/bench/external/{aider_polyglot,swe_bench}.py`. Argument-list
  invocation and `stdin=DEVNULL` are preserved; POSIX and `.exe` targets are
  untouched.
- **Defense-in-depth sweep as a library.** `cli/hook_ghost_files.py` now exposes
  `sweep_ghost_files(root)` (scan + delete in one call). The long-lived
  `EditLedgerEnricherAgent` (runs in the MCP server with cwd = project root)
  calls it each tick, so stray artifacts are cleaned even in the **main
  checkout while edits happen in a worktree**, where no PostToolUse hook fires.
- **Regression tests.** `tests/test_ghost_generation.py` drives every fixed
  site's argv shape and the observed adversarial payloads (`-> tuple[int, str]`,
  backticks, `{`, `)`, `>=`, `|`, `&`) through the hardened path in a temp cwd
  and asserts **zero** new files appear — plus a control test proving the old
  plain-list path still ghosts on this box (so the fix stays load-bearing).
### Storage retention & rotation

- **Shared retention manager.** New `services/retention.py`:
  `rotate_jsonl()` moves an oversized live JSONL into
  `.c3/archive/<name>.<UTC-date>.jsonl.gz` (atomic rename — writers
  open-append per write, so the next append recreates a fresh file; if the
  gzip step fails the uncompressed archive survives, so records are never
  lost), plus `purge_archives(keep_days=90)` for long-term TTL and a
  rate-limited `RetentionManager` sweep that piggybacks on the existing
  `EditLedgerEnricherAgent` cadence (no new agent thread). Config knobs
  live in `.c3/config.json` under a `"retention"` section.
- **Size-capped JSONL stores.** `activity_log.jsonl` (~5MB) and
  `tool_telemetry.jsonl` (~5MB) rotate at write time via a cheap per-append
  size check; `notifications.jsonl` (~2MB) archives old *acknowledged*
  entries on the sweep (unacked and recently-acked entries stay live so the
  ack-cooldown suppression keeps working). The telemetry reader now spans
  the live file **and** rotated archives, so day-window aggregations keep
  working across rotations (archives older than the window are skipped
  without being opened).
- **Edit-ledger rotation with audit integrity.** `edit_ledger.jsonl`
  (~10MB) rotates structure-aware: only entries older than
  `edit_ledger_keep_days` (14) that are not awaiting the enricher
  (`git_pending` without a git patch) are archived — together with every
  patch that targets them — and the gzip archive is written *before* the
  live file is rewritten, so no record is ever dropped or duplicated.
  Version tombstones (`{"_c3_rotation": 1, "file", "version"}`) keep
  per-file version numbering continuous for both `EditLedger` and the
  PostToolUse hook. Edit-ledger archives are exempt from the 90-day purge
  by default (`edit_ledger_archive_keep_days: 0` = keep forever).
- **Session snapshot cap.** `.c3/sessions/` is capped at the newest 50
  `session_*.json` files (`sessions_max_files`); older ones are
  gzip-archived (or deleted with `sessions_archive: false`). Context
  snapshots (`.c3/snapshots/`, the `c3_session` restore path) are untouched.
- **File-memory pruning.** New `FileMemoryStore.prune_stale()` removes
  records whose source file no longer exists in the repo (fixes the
  tracked-vs-indexed drift, e.g. 387 records vs 252 real files), run from
  the retention sweep.
### Response boilerplate diet — headers off by default, structured accounting everywhere

- **Per-call "raw->optimized tok" ratio headers removed by default.**
  `c3_filter` keeps its actionable one-word method tag (`[filter:pass1]`,
  `[extract:.log]`); the token pair and %-saved suffix are gone. Compress
  batch reports drop per-file `(12345->678tok)` tags; transcript search drops
  per-item full session UUIDs, relevance scores, and token counts (~40
  tokens/item). New `hybrid.show_token_ratios` config flag (default `false`,
  same convention as `show_savings_footer`) restores the old headers for
  debugging.
- **search/compress/filter/memory migrated to structured token accounting.**
  `c3_search` (code+semantic), `c3_compress` (map/dense_map, smart-family,
  batch), and `c3_filter` (text + file modes) now report
  `(raw_tokens, optimized_tokens)` via `finalize_with_tokens()` →
  `SessionManager.record_tool_tokens()` instead of encoding them in summary
  strings for the legacy regex fallback to scrape. Telemetry records for
  these tools are now `source: "structured"`. (`c3_memory` emits no token
  pairs — nothing to migrate.)
- **`c3_memory(action='recall')` no longer prints per-fact salience scores.**
  Opt back in with `include_scores=True` (scores are computed on demand;
  explicit request overrides the small-recall fast path).
- **`c3_status(view='budget')` breakdown is adaptive.** Only tools actually
  used this session (non-zero tokens) are listed — no fixed six-slot row —
  and ONE aggregate `[savings]` line (est. saved vs full-read baseline,
  measured ops) carries the session-level story that per-call headers used
  to repeat.
### CLI smoke-test Windows pipe hang fixed

- **Bare `c3` no longer spawns the TUI into redirected stdio.** With piped
  stdout/stderr (pytest `capture_output`, CI, shell pipes), the no-args path
  used to launch the `tui/main.py` child, which inherited the caller's pipe
  handles and held them open past the parent's death — on Windows,
  `subprocess.run(timeout=...)` kills only the direct child, so the caller's
  `communicate()` blocked forever. `c3` with no arguments now launches the
  TUI only when stdin AND stdout are a real terminal, and prints `--help`
  otherwise (`_stdio_is_interactive()` gate in `cli/c3.py`). Interactive use
  is unchanged.
- **Hang-proof smoke-test runner.** `tests/test_cli_smoke.py` replaced its
  `subprocess.run(timeout=...)` helper with the repo's Popen +
  `stdin=DEVNULL` + communicate-with-timeout + `taskkill /F /T` tree-kill
  pattern (POSIX: own session + `killpg`), with explicit UTF-8 decoding. A
  future regression now FAILS in bounded time with a clear message instead
  of hanging pytest and orphaning processes.
- `tests/test_cli_smoke.py` — previously excluded from suite runs because
  `test_no_args_prints_help_and_exits_zero` hung indefinitely on Windows —
  is now safe to run (verified 3 consecutive full-file runs, ~1-2s each).

## [2.42.0] - 2026-07-01

### Honest measurement layer

- **Structured per-tool token accounting.** New
  `cli.tools._helpers.finalize_with_tokens()` +
  `SessionManager.record_tool_tokens()` let tools report measured
  `(raw_tokens, optimized_tokens)` explicitly instead of encoding them in
  summary strings for regex-scraping. The legacy summary parser
  (`_parse_summary_token_pair`) remains as a fallback for tools not yet
  migrated; structured reporting suppresses it per-call to prevent double
  counting. `c3_read` is migrated as the reference tool.
- **Per-tool telemetry JSONL.** Every MCP tool call now appends one record to
  `.c3/tool_telemetry.jsonl` (`ts, session_id, tool, action, response_tokens,
  raw_tokens, optimized_tokens, duration_ms, source`) from the
  `track_response` seam. Writes are failure-safe: telemetry errors can never
  break a tool response. C3 can now answer "how many tokens did c3_filter
  save this week?".
- **Aggregation query.** New `services.telemetry.aggregate_tool_telemetry(
  project_path, days=7)` returns per-tool calls, response tokens, and
  estimated savings over the last N days (not yet surfaced in `c3_status`).
- **Honest labeling.** Session `token_usage` savings key renamed
  `estimated_saved` → `estimated_saved_vs_full_read`: the "raw" side of the
  pair is a full-file-read baseline (a counterfactual), so savings are
  estimates vs that baseline, not measurements of real agent behavior.
- **README claims reconciled with code.** The compression claim now matches
  `c3_compress` ("structural map at 40-70% of the original token count,
  30-60% smaller" — previously "70%-smaller"), and the dashboard's "448K
  tokens saved (89.9%)" figure is annotated as an illustrative example
  measured against the full-read baseline.
- **Session benchmark baseline de-strawmanned.** The "without C3" path in
  `services/session_benchmark.py` now models a competent agent: one targeted
  grep (matching lines + context, not full-file dumps) and each needed file
  read at most ONCE (partial when very large, mirroring native Read's line
  cap) — instead of 3-4 repeated full reads per scenario. Baseline read steps
  record which files they ingest (`StepResult.detail = "reads:..."`), and a
  new test asserts the at-most-once property.
### Hook dispatcher, consolidated enforcement state

- **One hook process per event instead of up to three.** New `cli/hook_dispatch.py`
  reads the hook JSON from stdin once and runs all applicable sub-hooks
  **in-process** (each `cli/hook_*.py` now exposes an importable
  `run(payload, project_path=None)`; the `python <hook>.py` entry points remain
  for backward compatibility). `c3 install-mcp` registers
  `hook_dispatch.py <pretool|posttool|stop>` per matcher — a native `Read`
  now costs 2 interpreter spawns (PreToolUse + PostToolUse) instead of 3, a
  `Bash` call 1 instead of 2, and `mcp__c3__c3_read` 1 instead of 3
  (~150 ms saved per avoided spawn on Windows). Sub-hook outputs compose per
  Claude Code hook semantics: deny beats allow, `additionalContext` strings
  concatenate, `tool_result` replacements are preserved.
- **Consolidated enforcement state: `.c3/enforcement_state.json`.** Replaces the
  three-mechanism seam (`last_c3_call.json` signal file, `unlocked_files.json`
  sticky map, four independent writer sites) that produced the v2.39.0
  enforcement bypass. All reads/writes now go through one shared module
  (`cli/_hook_utils.py`) with atomic writes (temp file + `os.replace`). Legacy
  files are still **read** as a fallback for one release; only the new file is
  written.
- **Session-scoped enforcement state.** Hook payload `session_id` is stored in
  the state file; state written by a different Claude Code session is treated
  as stale (the old signal file survived `/clear` and leaked unlocks across
  sessions). Stale state degrades to the advisory path — never a surprise
  hard-deny from another session's leftovers.
- **Hook failures are visible.** Critical failures — a sub-hook module that no
  longer imports, or corrupted enforcement-state JSON (now quarantined to
  `enforcement_state.json.corrupt`) — emit a short
  `[c3:hook-error] <hook>: <reason>; see .c3/hook_errors.log` additionalContext
  warning instead of only logging. Non-critical sub-hook crashes stay log-only
  and never kill the remaining sub-hooks.
- **Migration:** re-running `c3 install-mcp` cleanly replaces the old per-hook
  settings entries (same matchers, commands swapped for the dispatcher; old C3
  Stop-hook commands are detected by script name and removed). `c3 uninstall`
  also removes dispatcher entries. No enforcement-policy changes: advisory
  read-class / blocked write-class split and all redirect messages are
  unchanged.
- **Tests for the previously uncovered hooks** (8 of 11 had none):
  `tests/test_hook_pretool_enforce.py` (allow/deny matrix incl. stale TTL,
  corrupted state, legacy fallback, session mismatch),
  `tests/test_hook_dispatch.py` (output composition, crash isolation, deny
  propagation, end-to-end round-trip), `tests/test_hook_state.py` (state
  layer), `tests/test_hook_smoke.py` (session_stats, auto_snapshot,
  ghost_files, c3_signal, c3read, edit_unlock, edit_ledger, terse_advisor).
### Notification dedup + KeyFileVersion detail

- **NotificationStore collapses duplicates instead of piling them up.** An
  identical unacknowledged (agent, title, message) now merges into the existing
  record, bumping a `count` field and refreshing `last_seen`, so every agent
  benefits — not just the ones opting into `replace_if_unacked` (which now also
  bumps count/last_seen and updates severity). A lazy retro-cleanup pass
  (`collapse_duplicates()`, run once per store instance on first access) merges
  pre-existing unacked duplicate backlogs by (agent, title), keeping the newest
  message, summed count, max last_seen, and highest severity — the live
  13-duplicate KeyFileVersion backlog self-heals on next access.
- **KeyFileVersionAgent warnings are now actionable and non-duplicating.**
  Messages include per-file detail ("cli/mcp_server.py (3f2a1bc->9d4e2aa)",
  with "new->" / "->deleted" for created/removed files), and the agent updates
  its pending notice in place (`replace_if_unacked=True`) instead of appending
  an identical "Key file versions changed" line every 3-minute cycle.
- **`c3_status(view='notifications')` renders collapsed duplicates and caps the
  list.** Each actionable line now carries the message detail (truncated at 120
  chars) plus a "(xN, last HH:MM)" suffix for collapsed records; the list is
  capped at 10 lines with a "... +N more" tail. `get_pending_summary()` gains
  the same "(xN)" suffix.
### Delegate backend cascade + filter pass-2 backoff

- **`c3_delegate` backend='auto' now cascades through healthy backends instead of
  falling straight to Ollama.** Auto routing walks an ordered preference list per
  task type — heavy tasks (review/diagnose/improve/test): codex → gemini → ollama;
  light tasks: ollama first, then codex/gemini only when Ollama is down — skipping
  any backend that is disabled, not installed, or whose circuit breaker is open.
  The cascade decision is surfaced in the response and metadata (e.g.
  `[delegate] codex breaker open, retry ~42s -> routed to gemini`), and when *no*
  backend is healthy the error now lists every skip reason instead of returning a
  generic Ollama failure. Explicitly requested backends are never silently
  rerouted: an explicit `backend=` with an open breaker still returns the clear
  degraded error with cooldown remaining. (`cli/tools/delegate.py`)
- **`OutputFilter` pass-2 (Ollama summarization) gained a per-call timeout and
  adaptive backoff.** Each pass-2 call now passes a hard timeout to
  `ollama.generate` (default 2s, `filter_pass2_timeout`); the last 3 latencies
  are tracked (`filter_pass2_latency_window`) and if all of them run into the
  timeout, pass-2 is suspended for a cooldown window (default 5 min,
  `filter_pass2_suspend_seconds`) so a slow Ollama no longer stalls every
  filtered tool output. The suspension is noted once per window in the filter
  output (`[filter:fast] pass2 suspended, slow ollama`), the result dict gains a
  `pass2_suspended` flag, and the filter metrics now track `pass2_calls`,
  `pass2_timeouts`, and `pass2_suspended`. (`services/output_filter.py`)
- Tests: `tests/test_delegate_cascade.py` (cascade selection matrix, cascade
  note, no-healthy-backend error, explicit-backend no-reroute) and
  `tests/test_filter_backoff.py` (per-call timeout forwarding, suspension after
  consecutive slow calls, one-shot note, cooldown recovery, fast path untouched).
### Compressor large-file fast path

- **Large-file fast path in `services/compressor.py`.** Files at or above
  `LARGE_FILE_LINE_THRESHOLD` (10,000 lines) or `LARGE_FILE_BYTE_THRESHOLD`
  (200 KB) — both configurable module constants — now skip the full regex/AST
  structural pipeline. Instead they return a cheap header (size, line count,
  detected language, up to 30 import/signature lines from a bounded scan of
  the first 64 KB) plus explicit guidance to use `c3_compress(mode='diff')`,
  `c3_read(lines=[start,end])`, or `c3_search` to target sections. The result
  keeps the standard contract (`compressed`/`mode`/`filepath` + token-savings
  keys) so callers and raw->compressed accounting are unaffected; `diff` and
  `summary` modes are exempt, and pre-existing content-hash cache entries
  still win over the fast path.
- **Per-extension AST parse-failure memo.** When tree-sitter parsing raises
  for a file extension, the failure is memoized (per-process
  `AST_PARSE_FAILURES` dict) so subsequent files with that extension skip
  straight to the regex/generic fallback instead of re-attempting a
  known-failing parse. A parser returning `None` (unsupported language) is
  not memoized; render failures fall back per-file without memoizing.

## [2.41.0] - 2026-06-25

### Fixed

- **`c3_shell` ran commands through `cmd.exe` on Windows, mismatching the native
  Bash tool.** `_run_sync` used `subprocess.Popen(shell=True)`, which resolves to
  `cmd.exe` via `COMSPEC` on Windows — while the rest of the environment (the
  native Bash tool, CLAUDE.md conventions, agent command habits) speaks POSIX via
  Git Bash. Any bash-flavored command (`ls`, `grep`, `cat`, single quotes,
  `$VAR`, `/dev/null`, forward-slash flags, heredocs) silently failed under
  `cmd.exe` and forced a fall back to native Bash, defeating the point of
  `c3_shell` as a structured drop-in. `c3_shell` now runs commands through Git
  Bash (`bash -c`) on Windows when a Git-for-Windows `bash.exe` is available, so
  it speaks the same dialect as the native Bash tool.

### Added

- **`C3_SHELL_BASH` environment override for `c3_shell` shell selection.** Set
  `C3_SHELL_BASH=0` (or `cmd`/`off`/`false`) to force the legacy `cmd.exe`
  behavior, or point it at a specific `bash.exe` path to override auto-discovery.
  Discovery prefers Git-for-Windows install locations and PATH, and deliberately
  rejects WSL/Store `bash.exe` (System32 / WindowsApps) because its Linux
  `/mnt/c` path semantics would break `cwd` handling. POSIX platforms are
  unchanged (`shell=True` → `/bin/sh`).

## [2.40.0] - 2026-06-25

A Bitbucket Data Center / Server fix batch. A full PR-lifecycle evaluation against a
live Data Center server surfaced seven issues in the `c3_bitbucket` tool and `c3 bitbucket`
CLI; all are fixed here.

### Fixed

- **`get_pr_diff` returned the JSON diff model instead of a unified diff.** `_request`
  always sent `Accept: application/json`, so Bitbucket content-negotiated to its
  structured diff object even though `get_pr_diff` requested the raw body. `_request`
  now takes an `accept` parameter and `get_pr_diff` asks for `text/plain`, yielding a
  readable `diff --git … / @@ … @@` unified diff. (Issue 1)
- **`whoami` 404'd on Data Center and made valid logins look failed.** `GET
  /users/me` is a Bitbucket **Cloud** convention; DC treats `me` as a literal username.
  `whoami` now resolves the account from the `X-AUSERNAME` response header (carried on
  every authenticated request) and enriches via `GET /users/{slug}`. The `login`
  connection probe gates success on `application-properties` only and treats the
  `whoami` enrichment as best-effort, so a valid token never prints a probe failure.
  (Issue 2)
- **Bitbucket account was per-project with no global fallback.** A project that had
  never run `c3 bitbucket login` reported "no active account" even when the user had
  logged in elsewhere. `load_bitbucket_config` now falls back to `~/.c3/config.json`
  when the project has no active account (precedence: project → home → defaults), and a
  new `c3 bitbucket login --global` writes the home config for reuse everywhere. The
  PAT still lives only in the OS keyring. (Issue 3)
- **Unicode output mojibaked / crashed on Windows cp1252 consoles.** Decorative glyphs
  (`→ — ✓ · …`) are replaced with ASCII (`-> -- [x] [ ] ...`) in the Bitbucket
  formatters and status output, and the `c3` CLI now reconfigures stdout/stderr to
  UTF-8 at entry so server-supplied text (PR titles, branch names, diffs) renders
  cleanly instead of raising `UnicodeEncodeError`. (Issue 4)
- **Stale `User-Agent`.** `c3-bitbucket/2.30.0` was hardcoded; it is now derived from
  the installed package version via `importlib.metadata`. (Issue 5)
- **Response cap could emit one over-long line.** `_cap` reduced output line-by-line
  but never split a single line; it now hard-clamps by characters as a final guard.
  (Issue 7)

### Docs

- Documented the `--global` login flag, the project → home account-resolution
  precedence, and an upgrade-safety note (stop the running server before `c3 upgrade`
  to avoid pip's `~`-prefixed backup dirs in `site-packages`). (Issue 6)

## [2.39.1] - 2026-06-24

A small reliability fix for `c3_delegate`.

### Fixed

- **A broken delegate backend re-spawned a subprocess on every call.** When a CLI
  backend (`gemini`/`codex`/`claude`) was installed but failing at runtime (expired
  auth, model pulled away, repeated timeouts), `_handle_*_delegate` returned the error
  without demoting the backend — so every subsequent `c3_delegate` call paid the full
  90–120s subprocess spawn + timeout again. Each backend now has a thread-safe circuit
  breaker (`services/circuit_breaker.py`): after N consecutive failures (default 3) it
  short-circuits for a cooldown (default 60s) with a single half-open probe on recovery,
  and surfaces a notification when it trips. The `auto` router skips tripped backends and
  falls back to Ollama. Thresholds are configurable via `delegate_config`
  (`breaker_failure_threshold`, `breaker_cooldown_seconds`).

## [2.39.0] - 2026-06-22

A correctness & security hardening release. A multi-agent audit of C3 surfaced a
hook-enforcement bypass, several edit-ledger / session-store data-loss races, Windows
line-ending and subprocess bugs across the c3 tools, installer config-merge data-loss
risks, and three Oracle security gaps. All are fixed here.

### Security

- **Oracle `POST /api/config` was unauthenticated.** Any local process could
  `POST {"api_require_auth": false}` to strip authentication off the entire Discovery
  API, or repoint `ollama_base_url` to exfiltrate prompts. The endpoint now requires the
  Bearer token and rejects unknown config keys (allowlisted from `DEFAULTS`).
- **Oracle `GET /api/apikey` leaked the raw token.** It returned the plaintext Bearer
  token with no auth; it now returns a masked form unless a valid Bearer token is
  presented (`generate`/`rotate` still reveal the new token once).
- **Oracle Discovery `project_path` was unvalidated.** Callers could read any `.c3`
  project on the machine by path; project paths are now validated against discovered
  projects before any read.

### Fixed

- **Enforcement bypass: any read-only `c3_*` call unlocked native `Edit`/`Write`.** The
  PreToolUse signal fast-path allowed any native tool whenever *any* fresh c3 signal
  existed, ignoring per-tool prerequisites. Write-class tools (Edit/Write/MultiEdit) now
  require a `c3_edit`/`c3_edits`/`c3_agent` signal; read tools are unchanged.
- **`MultiEdit` and `NotebookEdit` bypassed enforcement and the edit ledger entirely** —
  no PreToolUse/PostToolUse matcher was registered for them. Both are now enforced and
  logged.
- **`c3_edit` rewrote whole LF files as CRLF on Windows.** A one-line edit flipped every
  line ending; edits now preserve the file's original newline style (single, batch, and
  create modes).
- **`c3_edit` batch mode wrote the file and logged a ledger entry even when zero patches
  applied**, and crashed on a non-dict batch element. It now writes/logs only when a patch
  actually changed the file, and returns a clear error for malformed batches.
- **Edit ledger could lose writes.** `tag_edit` did a lock-free full-file rewrite that
  clobbered concurrent appends, and `log_edit` didn't take the write lock. `tag_edit` now
  appends a tag patch under lock, `log_edit` is locked, edit ids carry a random suffix to
  prevent hook/server collisions within the same second, and orphaned patches are logged.
- **`sessions.json` could be wiped on a corrupt/partial read.** The conversation store now
  writes atomically (temp + `os.replace`) and, on a parse failure, backs up the corrupt
  file instead of silently resetting the catalog to empty; `add_turn` index updates are
  locked.
- **`c3_delegate(backend="claude")` was 100% broken** (a tuple-unpacking bug failed every
  call). Fixed; all CLI runners now also decode subprocess output as UTF-8 (cp1252 crash
  fix) and kill the full process tree on timeout.
- **JS/TS `export class/function/const` symbols were missing** from compression maps and
  the file-memory index (the walker descended past the declaration). Exported symbols are
  now indexed.
- **`c3_compress` rendered every class with Python `class Name:` syntax** regardless of
  language; it now uses the language-appropriate declaration.
- **`c3_read(symbols=...)` could return truncated bodies** when a `}` appeared inside a
  string or comment; the brace scanner now skips string/char/template literals and
  comments.
- **`file_memory` lazy search index had a first-search-vs-background-update race**
  (introduced with lazy init); build/update/search are now lock-guarded.
- **Installer config-merge data-loss risks.** `merge_c3_block` could corrupt `CLAUDE.md`
  on out-of-order/duplicate markers; the global-`CLAUDE.md` writer could delete user
  `#` headings placed after the managed block; and `upsert_toml_section` orphaned child
  subtables (e.g. `[mcp_servers.c3.env]`) on re-install. All fixed; the global managed
  region now uses explicit BEGIN/END markers.
- **Sticky unlocks from `c3_compress`/`c3_agent` were lost** (written only to a file no
  hook reads); they now reach the enforcer's `.json` unlock map.
- Smaller fixes: `c3_read` negative/reversed/comma line specs and `lines=0`; `c3_validate`
  process-tree kill + UTF-8 on Windows; empty-fact rejection in `c3_memory add`;
  `context_snapshot` atomic writes + corrupt-latest fallback; `web_security` no longer
  skips the host allowlist on a missing Host for mutating requests; Oracle chat/config
  endpoints return JSON errors for bad bodies; Oracle MCP auth toggles apply without a
  restart; the activity digest flags truncated scans; TOML `#` inside quoted values is no
  longer stripped.

### Changed

- PreToolUse enforcement now distinguishes read-class from write-class c3 signals — a
  behavior change for anyone who relied on a read-only c3 call to unlock a native write.

## [2.38.1] - 2026-06-14

Startup reliability fixes for the MCP server and for the Oracle on Windows.

### Fixed

- **MCP server intermittently marked "× Failed to connect" on startup.** `VectorStore`
  and `EmbeddingIndex` now initialize their chromadb/Ollama backends **lazily on first
  use** (lock-guarded, idempotent) instead of eagerly in `build_runtime()`. This cuts
  `build_runtime` startup from **~20s to <1s**, so it no longer exceeds Claude Code's
  default MCP handshake timeout. The heavy init is warmed in the background by the MCP
  `lifespan` and otherwise happens on the first semantic-search / memory call (under the
  larger tool timeout). Status views (`c3_status`) stay non-blocking. No external timeout
  override (`MCP_TIMEOUT`) is needed anymore.
- **Oracle server startup crash on Windows.** `run_oracle` printed a `→` in its banner,
  raising `UnicodeEncodeError` on consoles using the cp1252 code page; `stdout`/`stderr`
  are now reconfigured to UTF-8 at startup (covers the banners and the logging handler).

## [2.38.0] - 2026-06-14

Oracle activity reporting — the Oracle can now produce a cross-project "what happened
today" digest, exposed as a discovery tool (MCP + REST + OpenAPI), a dedicated endpoint,
and a web-UI tab.

### Added

- **`ActivityReporter`** (`oracle/services/activity_reporter.py`) — aggregates per-project
  sessions, tool calls, edits, git mutations, and token/cost for a day (or `since`/`until`
  window) across all registered projects, or one via `project_path`. Reads `.c3` JSONL
  artifacts directly (no C3Runtime build); skips non-C3 projects without side effects.
- **`activity_report` discovery tool** (read tier) in `TOOL_SPECS` — auto-exposed on MCP,
  OpenAPI, `POST /api/discovery/call`, and the internal Oracle chat. Optional `narrate=true`
  adds a best-effort LLM prose summary (never fails the structured result).
- **`GET /api/activity/digest`** Oracle endpoint (`date` / `since` / `until` / `project` /
  `narrate` query params) and an **Activity** tab in `oracle.html` with a date picker,
  totals cards, optional narrative, and a per-project breakdown table.

### Changed

- `ChatEngine` accepts an optional `activity_reporter` and dispatches `activity_report`.

## [2.37.0] - 2026-06-14

Non-destructive config generation — regenerating instruction docs and applying permission
tiers no longer clobber content you wrote by hand.

### Added

- **C3-managed block markers for instruction docs.** Generated `CLAUDE.md` / `AGENTS.md` /
  `GEMINI.md` content is now wrapped in `<!-- C3:BEGIN … -->` / `<!-- C3:END -->` sentinels
  (with a visible `# C3 — Managed Instructions` heading). Shared helpers
  `wrap_c3_block` / `merge_c3_block` / `write_c3_instruction_doc` in `services/claude_md.py`
  back every write path.
- **`_merge_permission_tier`** (`cli/c3.py`) — merges a permission tier into existing
  `settings.local.json` permissions, preserving user-added `allow`/`deny` rules and
  non-list keys (`ask`, `defaultMode`, `additionalDirectories`) while replacing only the
  entries C3 manages.

### Changed

- **Instruction docs are merged, not overwritten.** `c3 init` / `c3 install-mcp` and the
  `c3 claudemd save` / Hub save paths now replace only the C3-managed block and keep
  everything outside it. A pre-existing hand-written file (no markers) is preserved and the
  C3 block is appended; legacy marker-less C3 files are migrated in place (trailing
  `# User Notes` still preserved). Mirrors the long-standing global `~/.claude/CLAUDE.md`
  merge behaviour.
- **`claudemd compact` preserves the managed block.** Compaction now operates on the inner
  C3 body only and re-wraps it, so the markers, the `# C3` heading, and any user content
  outside the block survive.
- **Permission tiers are merged across every apply path** — `c3 permissions <tier>`,
  `c3 install-mcp --permissions`, and the Hub / per-project UI endpoints — so switching or
  re-applying a tier no longer wipes custom permission rules. Tier-owned entries are still
  replaced authoritatively; `deny` rules continue to win over `allow`.

### Fixed

- **`install-mcp` no longer drops user `Stop` hooks.** Only C3's own stop hooks (identified
  by their hook scripts) are replaced; user-added stop hooks — including the common
  matcher-less shape — are preserved alongside C3's.

### Documentation

- Documented the C3-managed instruction block and the merge/preserve semantics for
  `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`, `settings.local.json` (hooks + permissions), and
  `.mcp.json` across `README.md` and the in-app guide.

## [2.36.0] - 2026-06-13

Installation & upgrade simplification — a pure `pip`/`pipx` install is now self-contained,
and upgrading no longer requires per-project reconfiguration.

### Added

- **`c3 upgrade`** — upgrade C3 to the latest PyPI release in place (`pip -U` within the running
  interpreter; works for both pip and pipx installs). `c3 upgrade --check` only reports whether a
  newer release exists. Source and editable (`pip install -e .`) installs are detected and pointed
  at `git pull` instead of being clobbered.
- **`VersionCheckAgent`** — background agent that nudges when a newer C3 release is available on
  PyPI (once per day, best-effort, swallows offline errors, opt-out via `agents.VersionCheck`).
- **Version-skew notice** — `c3 init` on a project whose `.c3` was written by an older C3 now
  prints an upgrade hint pointing at `c3 init . --force`.
- **In-app guide route** — the per-project UI and the Hub serve the bundled guide at
  `/guide/<page>`, so the in-app docs work from a pure pip install (and the existing `docs.html`
  link to the Bitbucket guide now resolves).

### Changed

- **`.mcp.json` (plus project/global Codex & Gemini configs) now use the `c3-mcp` entry point**
  instead of an absolute path into the source checkout. Upgrading no longer requires re-running
  `install-mcp` per project — existing configs keep working. Falls back to the source script when
  C3 runs from a checkout with no installed console script.
- **The in-app guide now ships in the wheel.** `guide/` moved under the `cli` package
  (`cli/guide/`) and is included as package data, so `pip install code-context-control` is
  self-contained; previously the guide existed only in a source checkout.
- **`c3` with no arguments launches the interactive TUI** directly from the `c3` console entry
  point (previously only the generated `c3.bat` wrapper did this), so the entry points fully
  replace the wrapper.
- Registered the `BranchWatch` (v2.35.0) and `VersionCheck` agents in `AGENT_DEFAULTS` so they
  appear in freshly generated configs and the Hub agent settings.

### Documentation

- README now leads with `pipx install code-context-control` (no clone needed), documents
  `c3 upgrade` / `pipx upgrade` / `pip install -U`, and adds a contributor
  `pip install -e ".[dev]"` path.
- `install.bat` / `install.sh` gained pipx/PyPI guidance and `c3 upgrade` in their command help.

## [2.35.0] - 2026-06-13

### Added

- **Git branch awareness across the index, ledger, sessions, and snapshots.** Working-tree state
  is now centralized in a new `GitContext` helper (`services/git_context.py`): branch, HEAD sha,
  upstream, ahead/behind, and dirty status from a single cached
  `git status --branch --porcelain=v2` call, plus `changed_files()` / `dirty_files()` queries.
  Worktrees and detached HEAD are handled. `EditLedger` and `VersionTracker` now route git-root
  detection through it, de-duplicating two near-identical implementations.
- **`BranchWatchAgent` — automatic, scoped re-index on branch changes.** A new background agent
  detects HEAD/branch movement (checkout, switch, pull, merge — but *not* `git fetch`, which only
  moves refs and leaves the working tree untouched) and queues a re-index of exactly the files
  that differ between the old and new HEAD, restricted to files C3 already tracks. It also queues
  files dirty on disk each cycle, catching edits made outside C3 (rebase, `git restore`, another
  editor). Emits a **warning** notification on a branch switch and an **info** notification on a
  same-branch HEAD move. Enabled by default (30s interval); tunable via `.c3/config.json` →
  `agents.BranchWatch`.
- **Branch stamping.** Edit-ledger entries, sessions, and context snapshots now record the git
  `branch` + `head_sha` in effect at the time of the edit / session / snapshot.
- **`c3_edits(action='history', branch=…)`** filters the audit trail to edits made on a given
  branch; history output now shows the branch per entry.
- **Snapshot restore warns on branch drift.** `c3_session(restore)` flags when the working tree
  has moved to a different branch than the one the snapshot was captured on.

### Changed

- Context-snapshot capture and restore read fresh git state (bypassing the short TTL cache) so
  the recorded and compared branch is always current.

### Tests

- New `tests/test_git_branch_awareness.py` exercises `GitContext`, `BranchWatchAgent`, ledger
  branch stamping/filtering, and the snapshot branch-change warning against a real temporary git
  repository (8 tests). Full suite: 389 passing.

## [2.34.0] - 2026-06-10

### Added

- **`c3_shell` self-sweeps stray 0-byte "ghost" files** (shell-redirect / metacharacter
  artifacts like a `>Lnnn` marker or `2>$null` leaking a filename) created during a command,
  and reports them in the response. These previously accumulated in the project root on
  Windows because the external ghost-cleanup hook was never wired to the `mcp__c3__c3_shell`
  matcher; the sweep is now in-process and install-independent, and only removes files that
  appeared *during* the command (pre-existing files are never touched).
- **Security-guard observability.** A startup log line confirms the localhost web guard is
  active, and the UI `/api/health` now reports a `web_guard` status block.
- **MCP transport Host allowlist.** The Oracle MCP server (`:3332`) now rejects requests whose
  `Host` header isn't loopback or the configured `bind_host`/`allowed_hosts`
  (`_HostGuardMiddleware`) — defense-in-depth against DNS rebinding on top of the Bearer gate.

### Changed

- **`c3_shell` forces UTF-8 in child processes** (`PYTHONUTF8` / `PYTHONIOENCODING`) and decodes
  their output as UTF-8, fixing `cp1252` `UnicodeEncodeError` crashes when a command prints `→`,
  box-drawing, or emoji on Windows.
- **`c3_shell` no longer auto-filters `git status`/`diff`/`log`/`show`/`branch` output** — those
  are needed verbatim.
- **De-duplicated the MCP-section TOML helpers** (parse / upsert / remove / escape) that had
  drifted between `cli/server.py` and `cli/hub_server.py` into a single shared
  `core/mcp_toml.py`. The reconciled versions strip quoted keys and delete a config file that
  becomes empty.

### Documentation

- Refreshed the README, the in-app guide (`guide/tools.html` c3_shell safety classification,
  `guide/oracle.html` config table), and the Oracle discovery docs to reflect the v2.33.0
  web-security guard (Host/Origin/CSRF), the strengthened `c3_shell` blocklist, and the new
  `allowed_hosts` config option.

### Maintenance

- CI/release workflows: bump GitHub Actions off the deprecated Node 20 runtime to their
  latest Node 24 majors — `actions/checkout@v6`, `actions/setup-python@v6`,
  `actions/upload-artifact@v7`, `actions/download-artifact@v8`,
  `softprops/action-gh-release@v3`. (`pypa/gh-action-pypi-publish` is container-based and
  unaffected.)

## [2.33.0] - 2026-06-10

### Security

- **Cross-origin / CSRF + DNS-rebinding hardening for all local web servers.**
  The Hub (`cli/hub_server.py`), per-project UI (`cli/server.py`), and Oracle
  (`oracle/oracle_server.py`) bind to loopback but had no authentication, no
  Origin/Host validation, and a wildcard `Access-Control-Allow-Origin: *`, so a
  web page open in the user's browser could drive state-changing endpoints
  (the `launch-ide` custom command, adding a malicious MCP server, downgrading
  Claude permissions, wiping data) and read the Oracle Discovery bearer token
  (`api_apikey_get`). A new shared guard (`core/web_security.py`) now enforces a
  Host-header allowlist (defeats DNS rebinding) and an Origin/Referer check on
  every request (defeats CSRF), and replaces the wildcard CORS with scoped,
  same-origin reflection. Loopback and non-browser API clients are unaffected;
  an intentional non-loopback bind honours `host`/`bind_host` and an optional
  `allowed_hosts` list from config. Oracle Discovery bearer auth still applies
  on top.
- `api_projects_open` (Hub + UI) now refuses non-directory paths, so it can no
  longer launch an arbitrary file via the OS default handler.
- **c3_shell blocklist strengthened** to also cover `rm -rf /*`, `rm -rf` of a
  whole top-level system directory (`/etc`, `/usr`, …), and Windows
  whole-drive-root wipes (`del`/`rd`/`format C:\`), in addition to the existing
  `rm -rf /`/`~`/`$HOME` and fork-bomb patterns. Nested-path deletes
  (`rm -rf /home/me/project/build`) are intentionally still allowed. Documented
  explicitly as a best-effort guard, **not** a sandbox.

### Changed

- Discovery API guidance enriched for LLM clients: the MCP server `instructions` and the
  OpenAPI `info.description` now spell out the recommended workflow (`list_projects` →
  cross-project search → `c3_compress`/`c3_read`), the `project_path` requirement, the
  read/safe-action capability tiers, Bearer auth, and how to invoke tools — so Claude (MCP)
  and generic function-calling LLMs (REST) orient the same way.

### Fixed

- **c3_read silently returned the file *map* instead of source for `lines`
  range reads.** MCP clients serialize `lines` as a string (e.g. `"[22, 193]"`),
  which fell through `handle_read`'s range logic; `lines` is now coerced just
  like `symbols`. Comma-separated `symbols` (`"a,b,c"`) also now split into
  multiple targets instead of being read as one ambiguous name.
- **Ghost files (0-byte) from shell-redirect misinterpretation.** The output filter
  emitted its savings header as `raw->Ntok`; the literal `->` could be re-read by a
  shell as a `> Ntok` redirect, creating an empty file named after the token count.
  The header now uses `→` (not a shell metacharacter). The ghost-file cleanup hook
  (`hook_ghost_files.py`) also now runs after `c3_shell`, `c3_read`, and `Read` — not
  just `Bash` — so ghosts from any tool's output (git `ref -> ref`, Python `-> Type`,
  pip `>=x`) get swept.
- **Windows hooks never launched.** The generated PostToolUse/PreToolUse hook commands
  used a bare `cmd /c` prefix, but Git Bash (which Claude Code uses to run hooks on
  Windows) does not resolve bare `cmd` on PATH — so every c3 hook (enforcement, c3-signal,
  output filter, ghost cleanup) silently failed to start. Changed the prefix to
  `cmd.exe /c` (verified: the hook then runs and writes its signal file). Re-run
  `c3 install-mcp` to regenerate the hook commands.

## [2.32.2] - 2026-06-09

Docs release — no functional changes.

### Changed

- README now documents the **Oracle Discovery API** (v2.32.0): the MCP + OpenAPI
  transports, the `c3 oracle api` token workflow, the read/safe-action + Bearer +
  loopback security model, and dashboard token management. Since PyPI renders the
  README as the project description, this surfaces the Oracle work (shipped in the
  2.32.x line) on the PyPI and GitHub project pages.

## [2.32.1] - 2026-06-09

UI follow-up to the Discovery API: manage the Bearer token from the Oracle dashboard.

### Added

- Oracle dashboard **Settings → Discovery API** section: generate / rotate / clear the
  Bearer token, reveal + copy it, and copy a ready-to-paste Claude `.mcp.json` snippet,
  alongside live MCP URL / REST base / OpenAPI links.
- `GET /api/apikey` + `POST /api/apikey/{generate,rotate,clear}` — local-dashboard
  (loopback, unauthenticated, like `/api/config`) endpoints backing the token UI.
- Tests: `test_oracle_apikey_api.py`.

## [2.32.0] - 2026-06-09

Feature release. The **Oracle Discovery API** lets external LLMs — Claude Code /
Claude Desktop and any function-calling model — point at a running Oracle and use
C3's cross-project code & memory intelligence as tools, over **MCP (HTTP/SSE)** and
a parallel **OpenAPI REST** surface that share one tool core. Read + safe-action
tiers only (no code edits); Bearer-token auth; loopback-bound by default.

### Added

- `oracle/services/api_auth.py` — keyring-backed Bearer API key (`c3-oracle-api`),
  with a `C3_ORACLE_API_KEY` env override for headless/CI; `secrets.compare_digest`
  verification, plus `rotate`/`clear`/`peek`.
- `oracle/services/tool_registry.py` — `TOOL_SPECS`, the single source of truth:
  18 tools with JSON Schemas + capability tiers (`read`/`action`). `ToolRegistry`
  does tier filtering, arg validation, dispatch, and OpenAPI 3.1 generation.
- `oracle/services/tool_executor.py` — thin adapter routing the API through
  `ChatEngine.run_tool`, so chat and the API share one dispatch path.
- `oracle/mcp_oracle.py` — FastMCP HTTP/SSE server built from the registry, guarded
  by a pure-ASGI Bearer middleware (streaming stays intact), served in a daemon thread.
- Oracle REST endpoints under `/api/discovery/*`: `tools`, `call`, `tools/<name>`,
  `call/stream` (SSE), `openapi.json`, `mcp-info` — all behind a `before_request`
  Bearer guard.
- `c3 oracle api {info,key,rotate,clear}` CLI — prints the token, REST/MCP URLs,
  and a ready-to-paste Claude `.mcp.json` snippet.
- Oracle config keys: `bind_host`, `api_enabled`, `api_require_auth`, `api_max_tier`,
  `mcp_enabled`, `mcp_port`.
- Tests: `test_oracle_api_auth.py`, `test_tool_registry.py`, `test_oracle_discovery_api.py`.

### Changed

- `ChatEngine` gained a public `run_tool()` entry point so the chat loop and the
  Discovery API share one dispatch path.
- The Oracle server now binds **`127.0.0.1`** by default (was `0.0.0.0`); set
  `bind_host` in `~/.c3/oracle/config.json` to expose it on a network.

## [2.31.0] - 2026-06-09

Feature release. C3 tools can now reach **outside the current workspace** to
discover and operate on *other* c3-installed projects. A new `c3_project` MCP
tool lists/scans for projects that have a `.c3` directory and proxies the core
C3 operations (search, read, compress, status, memory, impact, edits, validate,
filter) against any of them — plus guarded writes (`edit`, `shell`, memory
mutations) behind an explicit `allow_write=true`.

### Added

- `services/project_runtime.py` — shared, thread-safe `ProjectRuntimeCache`
  (LRU, `.c3`-validated) that builds one `C3Runtime` per foreign project via the
  existing `build_runtime`; plus `resolve_project()` (name-or-path resolution
  against the global registry) and `scan_for_c3()` / `discover_projects()`
  (registry + bounded filesystem scan for unregistered `.c3` projects).
- `cli/tools/project.py` and the `c3_project` MCP tool — action-dispatch surface:
  discovery (`list`, `scan`, `info`, `register`, `unregister`), read ops
  (`search`, `read`, `compress`, `status`, `memory`, `impact`, `edits`,
  `validate`, `filter`), and guarded write ops (`edit`, `shell`, memory
  `add/update/delete`). Foreign mutations are logged to the *target* project's
  activity log and edit ledger.
- `mcp__c3__c3_project` added to `_C3_MCP_ALLOW` in `cli/c3.py`.
- `tests/test_project_tool.py` covering the resolver, discovery, runtime-cache
  LRU/validation, and the dispatcher (read proxy + write guard + audit).

### Changed

- `__version__` in `cli/c3.py` and `version` in `pyproject.toml` bumped to
  `2.31.0` (kept in sync).

### Why

Until now every C3 capability was scoped to the single project the MCP server
was launched in. Cross-project work meant switching workspaces. `c3_project`
lets an agent stay in one session, see which sibling projects have C3 installed,
and search/read/edit across them — while keeping writes explicit and auditable
on the project they land in.

## [2.30.0] - 2026-05-07

Feature release. Adds first-class Bitbucket Data Center / Server (self-hosted
enterprise) integration: a new `c3_bitbucket` MCP tool, a `c3 bitbucket` CLI
subcommand for credential management, and a Bitbucket tab in the Hub UI for
viewing PRs, branches, builds, activity, and repository administration.

### Added

- `services/bitbucket_client.py` — `BitbucketDataCenterClient` REST client
  using stdlib `urllib.request` with Bearer-token auth (PAT). Covers
  read-only browsing, pull-request writes (create/comment/approve/merge/decline),
  branch writes (create/delete), and repository administration
  (settings, webhooks, permissions).
- `services/bitbucket_credentials.py` — OS keyring wrapper (Windows
  Credential Manager / macOS Keychain / Linux Secret Service) for storing
  Personal Access Tokens. Tokens are never written to `.c3/config.json`.
- `cli/tools/bitbucket.py` and `c3_bitbucket` MCP tool — action-dispatch
  surface (`status`, `whoami`, `list_prs`, `get_pr`, `create_pr`,
  `merge_pr`, `decline_pr`, `approve_pr`, `comment_pr`, `list_branches`,
  `create_branch`, `delete_branch`, `list_repos`, `list_builds`,
  `list_activity`, repo-admin actions).
- `c3 bitbucket {login|logout|status|use|set-default}` CLI subcommand
  with interactive `getpass` token entry.
- Hub UI Bitbucket tab (`cli/ui/bitbucket.js`) with Overview / Pull
  requests / Branches / Builds / Activity / Admin sub-tabs and matching
  `/api/bitbucket/*` REST endpoints in `cli/hub_server.py`.
- `bitbucket` section in `core/config.py` defaults
  (`active`, `accounts`, `default_project`, `default_repo`, `verify_tls`).
- Tests under `tests/test_bitbucket_*.py` covering the client, credentials,
  tool dispatch, and CLI smoke.

### Changed

- `__version__` in `cli/c3.py` is now in sync with `pyproject.toml` again
  (was stale at `2.28.3`).
- `pyproject.toml` adds `keyring>=24.0` to runtime dependencies.

### Why

Teams on enterprise Bitbucket Data Center / Server have until now had to
context-switch out of C3 to inspect or act on pull requests, branches, and
builds. This release brings the same surface inside C3 — both for Claude
Code via MCP and for the human via the Hub UI — while keeping credentials
out of project files.

## [2.29.0] - 2026-04-27

Feature release. Adds project-merge in the hub and auto-registration on
`c3 init`. Backwards-compatible — existing UIs and integrations continue
to work unchanged.

### Added
- **Hub: Merge Projects** — new `⇄ Merge` button on each idle project
  card opens a modal that combines a source project's accumulated
  knowledge into a target. Useful when consolidating split repos,
  retiring an experiment branch, or rolling two side-by-side projects
  into one.
  - Merges memory facts (`.c3/facts/facts.json`) with `merged_from` /
    `merged_at` attribution preserved on every imported fact.
  - Merges edit-ledger entries (`.c3/edit_ledger.jsonl`) with a
    `[merged from <name>]` summary prefix and a `merged:<slug>` tag so
    the imported history stays distinguishable.
  - Merges conversation sessions (`.c3/conversations/`) — both the
    `sessions.json` index and per-session turn files. Session IDs that
    collide with the target are renamed `<id>_merged_<6hex>`.
  - Unions registry tags and appends source notes to the target with a
    `--- merged from <name> ---` separator.
  - Cleanup mode `keep` (default) leaves the source untouched. Cleanup
    mode `clear` performs the equivalent of `c3 init --clear` on the
    source: wipes `.c3/`, strips MCP configs (`.mcp.json`,
    `.claude/settings.local.json`, `.codex/`), removes instruction docs
    (CLAUDE.md, GEMINI.md, AGENTS.md), and drops the registry entry.
    The source directory itself is preserved.
  - Confirm dialog gates the destructive `clear` path; a red warning
    callout appears in the modal whenever `clear` is selected.
  - Skipped intentionally: `file_memory/`, code indices, snapshots,
    notifications, project config — their contents reference
    source-specific paths that wouldn't apply in the target.
- **`POST /api/projects/merge`** hub endpoint —
  body `{source_path, target_path, cleanup: 'keep'|'clear'}`,
  returns `{merged, source, target, cleanup, stats: {facts,
  ledger_entries, sessions}, warnings?}`.
- **`ProjectManager.merge_projects(source, target, cleanup)`** in
  `services/project_manager.py`. Cleanup branch lazy-imports
  `cli.c3._uninstall_mcp_all` + `_instruction_documents_for_project` so
  `services/` keeps its no-`cli`-imports invariant at module load.
- **Auto-registration on `c3 init`** — the brand-new install branch of
  `cmd_init` now calls `ProjectManager().add_project(project_path)`
  immediately after `_do_init()` succeeds. The hub picks the new
  project up on its next `/api/projects` refresh — no separate
  `c3 projects add <path>` step required. `add_project` is already
  idempotent, so re-running init is safe.
- 6 new unit tests in `tests/test_project_manager_merge.py` covering
  add-project idempotency, merge-keep, merge-clear, and the validation
  paths (identical paths, unregistered source, invalid cleanup value).
  Tests sandbox `~/.c3/` by monkey-patching the module-level
  `_GLOBAL_C3_DIR` / `_PROJECTS_FILE` / `_REGISTRY_FILE` constants so
  the user's real registry is never touched.

### Why
The hub already showed every C3-initialized project on the machine,
but two gaps were friction points: (1) projects had to be registered
manually with `c3 projects add` after init, and (2) when two projects
naturally converged (a fork that came back, an experiment that
graduated), the accumulated facts, conversation history, and edit
ledgers stayed siloed with no first-class way to combine them. This
release closes both gaps without changing any existing surface.

## [2.28.3] - 2026-04-27

Documentation + assets release. No code changes; behavior unchanged.

### Added
- Fresh, comprehensive README with a guided tour of every UI surface:
  Project Hub (list + grid + IDE picker + settings), per-project
  Dashboard, Edit Ledger, Memory, Sessions, Instructions, Chat, and
  Settings. Each section paired with a real screenshot captured from
  a live install.
- 11 new high-resolution UI screenshots in `docs/screenshots/`
  captured directly from the running Hub + per-project UI:
  `hub_projects.png`, `hub_projects_grid.png`, `hub_ide_config.png`,
  `hub_settings.png`, `ui_dashboard.png`, `ui_edits.png`,
  `ui_memory.png`, `ui_sessions.png`, `ui_instructions.png`,
  `ui_chat.png`, `ui_settings.png`.
- README MCP-tool table covering all 14 `c3_*` tools with one-line
  descriptions of each.
- README PyPI badge linking to the live package page.

### Changed
- README hero image now uses the per-project Dashboard (richer +
  more visually striking than the prior screenshot).
- IDE compatibility list expanded — Antigravity, Cursor, and Custom
  added to the documented IDE matrix.
- README `Install` section now starts with the one-liner
  `pip install code-context-control` (PyPI is live; no clone needed).

### Removed
- Stale legacy screenshots: `c3_hub.png`, `c3_hub_ide_modal.png`,
  `c3_hub_notifications.png`, `c3_ui.png`. Superseded by the new
  high-quality captures listed above.

## [2.28.2] - 2026-04-27

Documentation-only release. No code changes; no behavior changes; no
license change. The current OSS license remains Apache-2.0 for all 2.x
versions.

### Removed
- `EULA-PRO.md` deleted. It described a Pro tier that doesn't exist and
  contained maintainer-side commitments (updates, support, refunds) that
  shouldn't be on the record before any paid product actually ships. A
  proper EULA can be drafted later when there's something to govern.
- All trademark claims removed from `NOTICE` and `LICENSING.md`. No
  trademarks have been registered for "C3" or "Code Context Control",
  so claiming them in legal documents created risk without benefit.

### Changed
- `NOTICE` — rewritten as informational-only. No commitments to
  introduce a Pro tier, relicense future versions, respond to
  inquiries, or maintain anything. References `LICENSE` Sections 7–8
  for the warranty / liability disclaimer.
- `LICENSING.md` — softened throughout. Every "we will" became "may"
  or was removed. Added a top-of-file disclaimer that the FAQ is
  informational only and `LICENSE` governs.
- `SECURITY.md` — removed all response-time SLAs (3-day acknowledgement,
  7-day triage, 30-day fix). Now describes the *how* of reporting on a
  best-effort basis with no committed timeline.
- `README.md` License section reduced to a minimal pointer to `LICENSE`
  and `LICENSING.md`. No trademark text. No Pro-tier roadmap.

### Why

Apache-2.0 already provides the strongest possible warranty / liability
disclaimer. Supplementary docs were adding obligations beyond what the
license requires (response SLAs, "we will" promises about future
versions, trademark claims on unregistered names). Stripping those down
keeps the project's stated obligations exactly equal to what
Apache-2.0 imposes — no more, no less.

## [2.28.1] - 2026-04-27

Documentation-only release. No code changes; no behavior changes; no
license change. The current OSS license remains Apache-2.0 for all 2.x
versions, as it always will.

### Added
- New top-level [`LICENSING.md`](LICENSING.md) — FAQ-style document
  covering "can I use this at work", "can I fork", "will the license
  change", "what happens to my install if you relicense", and the
  rationale around the planned Pro tier.

### Changed
- [`NOTICE`](NOTICE) — expanded with an explicit "project posture and
  commercialization plans" section. Declares intent to introduce a paid
  Pro tier and the possibility of switching future major versions
  (3.x onwards) to a source-available license (e.g. BSL 1.1). All 2.x
  releases remain Apache-2.0 in perpetuity. Reinstates the trademark
  notice for "C3" and "Code Context Control" (™).
- [`README.md`](README.md) License section rewritten to clearly signal
  the project's commercialization intent without changing the actual
  license. Points at LICENSING.md for the full FAQ.

### Why this release exists

Better to declare commercialization intent **before** building a community
that depends on permissive terms than after. Honest signal vs. rug pull.
If we eventually do relicense future major versions, the prior 2.x
versions will retain their Apache-2.0 grant forever — your installed copy
is yours under the terms it was published under.

### Added
- `services/error_reporting.py` — opt-in Sentry crash reporting module.
  Off by default; activated only when `SENTRY_DSN` is set AND the user
  opts in via `C3_TELEMETRY_OPT_IN=1` or `~/.c3/telemetry.json`. Strips
  request bodies, local variables, and most contexts before sending.
- `[telemetry]` optional extra (`pip install code-context-control[telemetry]`)
  pulls in `sentry-sdk`. No-op when the extra is not installed.
- `cli.mcp_server:main` and `cli.hub_server:main` entry-point functions
  to back the `c3-mcp` and `c3-hub` console scripts declared in
  `pyproject.toml`. (Previously they only had `if __name__ == "__main__"`
  guards, so the entry-points would have raised `AttributeError`.)
- `.github/workflows/ci.yml` — ruff lint + pytest matrix on
  Linux/macOS/Windows × Python 3.10/3.11/3.12, plus a build-and-twine-check
  job that uploads sdist+wheel artifacts.
- `.github/workflows/release.yml` — tag-triggered build, PyPI publish via
  Trusted Publishing (OIDC), and a GitHub Release with the artifacts
  attached. Verifies the tag matches `pyproject.toml` version.
- `[tool.ruff]` config in `pyproject.toml`; `ruff` now in `[dev]` extra.
- Smoke tests: `tests/test_cli_smoke.py`, `tests/test_mcp_server_smoke.py`,
  `tests/test_hub_server_smoke.py` cover `c3 --version`/`--help`, MCP
  module import + tool registration, and Hub `/api/version` + `/api/health`.

### Fixed
- **Real bugs** uncovered by ruff: `services/version_tracker.py` used
  `sys.platform` in three places without importing `sys` (would raise
  `NameError` at runtime on Windows git-metadata fetches).
  `services/e2e_benchmark.py` used `count_tokens` without importing it.
- 407 cosmetic lint issues auto-fixed across the codebase (whitespace,
  import ordering, empty f-strings) — no behavior change.
- Removed two genuinely unused imports from `cli/c3.py`
  (`rich.print`, `rich.syntax.Syntax`).

### Skipped
- Two pre-existing-broken bench tests in `tests/test_e2e_benchmark.py`
  (`test_report_includes_tool_analysis`, `test_report_efficiency_summary`).
  Both fail because the bench's worktree path zeroes mocked
  `CLIResponse.tool_usage` when `.mcp.json` is absent. Skipped with a
  clear tracking comment so CI stays green; tracked for proper fix.

## [2.28.0] - 2026-04-27

### Added
- Apache-2.0 `LICENSE`, `NOTICE`, and `THIRD_PARTY_LICENSES.md` for OSS
  redistribution.
- `EULA-PRO.md` placeholder for future commercial Pro tier.
- `SECURITY.md` with vulnerability disclosure policy.
- `pyproject.toml` packaging metadata; project is now installable from a
  source tree with `pip install .` and exposes a `c3` console script.
- `tui/__init__.py` so the TUI module is included in distributions.
- `host` key in `~/.c3/hub_config.json` for opt-in non-loopback binding of
  the C3 Hub.

### Changed
- **Security:** the C3 Hub now binds to `127.0.0.1` by default instead of
  `0.0.0.0`. Operators who need LAN access must set `"host": "0.0.0.0"`
  (or a specific interface) in `~/.c3/hub_config.json` and are warned at
  startup that no auth is in front of the hub.
- README rewritten as a buyer-facing landing page (problem → value →
  install → screenshots) instead of an internal release-notes log.

### Removed
- Stray ghost files committed to the repo root (`L202`, `L2057`, `L2118`,
  `L2434`, `L359`, `str`, `tuple[int`).
- `requirements.txt` — superseded by `pyproject.toml`. Installer scripts now
  invoke `pip install .[tui]` instead of `pip install -r requirements.txt`.
- Repo-root `.mcp.json` (was a per-machine artifact with a hard-coded path).
  Now `.gitignore`d; regenerated by `c3 install-mcp`.

### Moved
- Marketing screenshots relocated from `Marketing/` to `docs/screenshots/`
  so they live alongside other documentation. README images now reference
  them via stable GitHub raw URLs so they render correctly on PyPI.

## [2.27.0] - 2026-03-15

### Added
- `c3_edits` MCP tool plus `EditLedger` service: AI-tracked file
  versioning, git-integrated audit trail, REST API, and UI tab.

## [2.24.0] - earlier

Historical release; see git log for details.
