# Changelog

All notable changes to Code Context Control (C3) are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Stream B — Hook dispatcher, consolidated enforcement state (P2+P3)

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
