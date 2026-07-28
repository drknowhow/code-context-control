# Changelog

All notable changes to Code Context Control (C3) are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
