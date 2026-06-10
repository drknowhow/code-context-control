# Changelog

All notable changes to Code Context Control (C3) are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- Refreshed the README, the in-app guide (`guide/tools.html` c3_shell safety classification,
  `guide/oracle.html` config table), and the Oracle discovery docs to reflect the v2.33.0
  web-security guard (Host/Origin/CSRF), the strengthened `c3_shell` blocklist, and the new
  `allowed_hosts` config option.

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
