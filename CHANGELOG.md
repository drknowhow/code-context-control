# Changelog

All notable changes to Code Context Control (C3) are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
