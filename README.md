<h1 align="center">Code Context Control</h1>

<p align="center">
  <strong>The local code-intelligence layer for AI coding tools.</strong><br>
  Retrieve less, read less, edit safer — and control what an agent may read, write, or see.<br>
  Works with Claude Code, Codex, Copilot, Cursor, and Antigravity.
</p>

<p align="center">
  <a href="https://pypi.org/project/code-context-control/"><img alt="PyPI" src="https://img.shields.io/pypi/v/code-context-control?color=blue&logo=pypi&logoColor=white"></a>
  <a href="https://github.com/drknowhow/code-context-control/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="Platforms" src="https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey">
  <img alt="Status: Beta" src="https://img.shields.io/badge/status-beta-yellow">
  <a href="https://github.com/sponsors/drknowhow"><img alt="Sponsor" src="https://img.shields.io/badge/sponsor-%E2%9D%A4-EA4AAA?logo=githubsponsors&logoColor=white"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/2026-07/ui_dashboard.png" alt="C3 per-project dashboard" width="900">
</p>

---

## The problem

LLM-driven coding tools have one expensive failure mode: **they read too much.** They `cat` whole files, regex the entire repo, dump 10k-line logs into context, edit-and-pray, and burn through budget before they touch a single line of code. On a half-day session you can spend $20+ on token waste that adds zero value.

And they read too *widely*: the same agent that greps your source also greps your `.env`, your customer CSV, and your production dumps.

## What C3 does about it

A thin **local** layer between your IDE and your repo. Every AI tool call is routed through a narrow, surgical operation instead of a broad, wasteful one:

| Without C3 | With C3 |
|---|---|
| `Read` the whole 2,000-line file | `c3_compress` returns a structural map at 40-70% of the original token count → `c3_read(symbols=...)` for the exact function |
| `Grep` the whole repo blindly | `c3_search` returns ranked candidates with TF-IDF + symbol awareness |
| Dump full `pytest` output into the prompt | `c3_filter` distills 500 lines → 30 actionable ones |
| Edit, hope it compiled | `c3_edit` writes via a ledger + `c3_validate` runs `pyright`/`tsc` automatically |
| `Bash` test runs that hang on Windows | `c3_shell` returns structured `{exit_code, stdout, stderr, duration}` with auto-filter |
| Lose all context on `/clear` | `c3_session(snapshot)` + `c3_memory` persist decisions across sessions |
| Re-explain the project every session | Auto-synced `CLAUDE.md` / `AGENTS.md` / `copilot-instructions.md` from one source of truth |
| Agent reads `.env`, a customer CSV, a prod dump | **Access Guard** denies the path outright, or **Mask Guard** serves a redacted view |

Everything runs **locally**. No source code, prompts, or model output leaves your machine unless you explicitly opt into a third-party model API.

---

## Install

Requires Python 3.10+. Recommended via [pipx](https://pipx.pypa.io):

```bash
pipx install code-context-control
c3 init /path/to/your/project
```

Or `pip install "code-context-control[tui]"` for the optional Textual UI.

`c3 init` walks you through IDE selection (Claude Code, Codex CLI, VS Code, Cursor, Antigravity, or Custom), optional `git init`, MCP registration, and — for Claude Code — a permission tier. Headless:

```bash
c3 init /path/to/project --force --ide claude --mcp-mode direct --permissions standard
c3 init /path/to/huge-repo --force --no-embed     # skip the embedding index on large repos
```

Upgrade with `c3 upgrade` (or `pipx upgrade code-context-control`). MCP is wired through the `c3-mcp` entry point, so upgrading needs no per-project reconfiguration.

> **Upgrading from before v2.60.1 on Windows:** existing projects are *not* repaired by upgrading — re-run `c3 init` once per project to fix hook registration.

Full notes: [Upgrading](https://github.com/drknowhow/code-context-control/blob/main/docs/upgrading.md) · [Contributing](https://github.com/drknowhow/code-context-control/blob/main/docs/upgrading.md#from-source-contributors)

---

## The UI

Three local web apps, no Electron — pure Flask + vanilla JS: the **Hub** (`c3 hub`, port 3330), the **per-project UI** (`c3 ui`), and the optional **Oracle** (`c3 oracle serve`).

### Project Hub — multi-project mission control

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/2026-07/hub_projects.png" alt="C3 Project Hub" width="900">
</p>

Every C3-initialized project registers itself here on `c3 init`. Filter by active/idle, see IDE / version / port / last activity per row, and launch your IDE or the project UI in one click. <kbd>Ctrl</kbd>+<kbd>K</kbd> searches code and memory across *every* registered project.

Three top-level views — **Projects**, **Tasks** (a cross-project kanban), and **Credentials** — plus a drill-in panel per project covering Overview, Sub-projects, Tasks, Artifacts, Memory, Ledger, Sessions, Health, Budget, Credentials, Config, and MCP.

**Sub-projects** make a nested repo a first-class child with its own `.c3`: the parent's index excludes the child's subtree, and `c3_search` / `c3_memory` fan out on demand (`scope='all'`, or one child by name). → [Sub-projects guide](https://github.com/drknowhow/code-context-control/blob/main/docs/sub-projects.md)

### Per-project UI

```bash
c3 ui                             # binds the first free port from 3333
```

Twelve tabs. The dashboard above shows token savings, indexed files, the live session, and a stream of recent tool calls and file changes.

| Tab | What it's for |
|---|---|
| **Dashboard** | Token savings, codebase breakdown, live session counters, recent activity |
| **Chat** | Browse and search indexed IDE chat transcripts |
| **Sessions** | Every session with duration, decisions, files, tool calls, token cost |
| **Memory** | Durable facts across sessions — categories, semantic search, list ↔ graph |
| **Tasks** | Per-project PM: dependencies, milestones, time tracking, health |
| **Edits** | The Edit Ledger — every AI-driven change, versioned and restorable |
| **Bitbucket** | PRs, branches, activity, admin against Bitbucket Data Center |
| **Jira** | My Work board, JQL search, transitions, comments |
| **Credentials** | Named secrets — metadata only, values are never returned to the browser |
| **Access Guard** | Path rules: deny / read-only / mask, plus the path tester |
| **Instructions** | One editor for `CLAUDE.md`, `AGENTS.md`, `copilot-instructions.md` |
| **Settings** | Budgets, feature flags, background agents, delegate routing, MCP |

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/2026-07/ui_tasks.png" alt="C3 Tasks" width="900">
</p>

**Tasks** (v2.45.0, extended v2.53.0) is a durable per-project tracker — dependencies and subtasks, milestones, decision notes, full event history, health reports, and both automatic and manual time tracking. It rolls up into the Hub's cross-project board.

**Instructions** keeps your agent-facing docs in sync. C3-generated content sits inside a `<!-- C3:BEGIN … -->` block; anything you write outside it is preserved. Since v2.60.0 generated docs point at `.c3/MAP.md` — a machine-owned, byte-stable repo map C3 refreshes automatically — instead of embedding a tree that goes stale. `AGENTS.md` serves both Codex and Antigravity; `GEMINI.md` is read if present but no longer generated (the Gemini CLI profile was removed in v2.52).

---

## The MCP tool suite

C3 exposes **21 tools** as a native MCP server. Your IDE calls them directly:

| Tool | What it does |
|---|---|
| `c3_search` | TF-IDF / regex / semantic search, ranked; fans out to sub-projects with `scope=` |
| `c3_compress` | AST-based file map (`map`, `dense_map`, `smart`, `diff`, `bug_scan`, `ast`) |
| `c3_read` | Surgical reads — by symbol name, regex, or line ranges |
| `c3_edit` | Atomic patch with ledger logging + content-addressable history |
| `c3_validate` | Type / syntax check (pyright, tsc, ruff — auto-detected) |
| `c3_filter` | Distill long terminal/log output via pattern + LLM summarization |
| `c3_shell` | Shell commands with structured returns + auto-filtered stdout |
| `c3_status` | Views: `budget`, `health`, `notifications`, `sessions`, `ghost_files`, `access` |
| `c3_memory` | Fact store with categories, recall, graph queries, `index`→`fetch` two-step |
| `c3_session` | Snapshot, restore, log decisions, compact history |
| `c3_impact` | Blast-radius analysis before editing shared symbols |
| `c3_locks` | Agent leases — who is working on which file, so two agents don't collide (v2.65.0) |
| `c3_delegate` | Offload heavy work to local Ollama / Codex / Gemini |
| `c3_agent` | Workflows: `review_changes`, `investigate`, `preflight`, `prepare_context`, `validate_compress` |
| `c3_edits` | Edit-ledger queries, version diffs, restore points, per-branch filter |
| `c3_task` | Per-project PM — tasks, dependencies, milestones, time tracking (v2.53.0) |
| `c3_artifacts` | Agent-config version history, diff & restore (v2.46.0) |
| `c3_credentials` | Named-secret vault; values never enter model context (v2.58.0) |
| `c3_bitbucket` | Bitbucket Data Center — PRs, branches, builds, admin (v2.30.0) |
| `c3_jira` | Jira Cloud + Data Center — JQL, issues, transitions (v2.56.0) |
| `c3_project` | Cross-project discovery & operations; guarded writes (v2.31.0) |

Every tool is **read-only safe in plan mode** except `c3_edit`, `c3_shell`, `c3_artifacts(action='restore')`, `c3_delegate` with write delegation enabled, and write actions on `c3_bitbucket` / `c3_jira` / `c3_credentials` / `c3_project` / `c3_task` / `c3_locks`.

On Windows `c3_shell` uses Git Bash when available. Git Bash bundles no `jq`; use `python -m json.tool` for portable JSON formatting.

---

## Guards — what the agent may touch, and what it sees

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/2026-07/ui_access_guard.png" alt="C3 Access Guard and Mask Guard" width="900">
</p>

Two questions, two answers. **Access Guard** (v2.62.0) answers *may the agent touch this path?* **Mask Guard** (v2.63.0) answers *what should it see when it does?*

```bash
c3 access add "secrets/**" --kind deny          # never read, never write
c3 access add "migrations/**" --kind read_only  # readable, never written
c3 access mask add "data/*.csv" --preset sample_rows --params "count=20,strategy=first"
c3 access mask activate                         # purge pre-mask artifacts, build views
```

```diff
- AWS_KEY = "AKIAIOSFODNN7EXAMPLE"          # your file
+ AWS_KEY = "«c3:redacted:aws_access_key»"  # what the agent sees
```

- **Tighten-only.** No allow list exists; global and project scopes merge as a union, so a cloned repo's config can only *add* protection.
- **`deny` means deny-enumerate too** — denied paths never appear in search results, maps, or the vector index.
- **Masked means read-only, always.** Cropped rows have no inverse, so an edit in transformed coordinates would corrupt the one file you protected.
- **Four deterministic presets, no LLM in the read path:** `redact_secrets`, `redact_columns` (salted one-way pseudonyms — joins survive, no reverse dictionary), `sample_rows`, `signatures_only`.
- **Rule changes are human-only** (UI tab or CLI) and ledger-logged. Agents have no mutation surface.
- **Built-ins can be switched off when you need to** (v2.65.0). `**/.env*`, `**/.c3/**`, `**/.claude/settings*.json` and `**/.git/**` yield to `c3 access builtin disable <glob>`, which makes you retype the glob first. It takes **two keys** — a config entry *and* a keyring attestation — so an agent that manages to write `config.json` still cannot grant itself write access to your settings. The credential vault stays absolute.
- **Honest coverage.** This guards *cooperative* agents against mistakes and prompt injection. It is not a sandbox — a raw shell outside C3's tools still sees the real bytes.

→ [Access Guard](https://github.com/drknowhow/code-context-control/blob/main/docs/access-guard.md) · [Mask Guard](https://github.com/drknowhow/code-context-control/blob/main/docs/mask-guard.md)

---

## Agent Locks — several agents, one repo (v2.65.0)

Guards answer *may the agent touch this?* Locks answer *is someone already touching it?*

Two agents editing one file used to be silent data loss. `c3_edit` serialized
same-file edits with an in-process lock, but every session runs its own `c3-mcp`
server — so two sessions could interleave read → replace → write and lose an edit
with no error on either side. Create mode was worse: both agents "succeeded" and
one file won.

Now there are two mechanisms, because they solve different problems:

- **Torn writes** — a cross-process file lock, held across create, single-edit and
  batch alike. No daemon, no configuration, nothing to switch on.
- **Overlapping work** — a *lease*. `c3_edit` takes one on the file it edits,
  carrying the intent from your edit summary. A second agent gets told who holds
  it and why:

```text
[c3-lock:held] services/router.py is held by claude-code:a3f19c2b.
  intent: "refactor retry backoff"
  lease expires in 6m12s.
```

```bash
c3 locks list                              # who holds what, across the project
c3 locks force-release services/router.py  # break a stuck lease (human-only, audited)
```

- **TTL is the real release mechanism.** Agents forget to release, so nothing
  assumes they will — a crashed agent can never wedge a repo.
- **Acquisition is all-or-nothing** over a sorted path list, so two agents grabbing
  the same pair in opposite order cannot deadlock.
- **`force-release` bumps a fencing counter**, so a holder that comes back is stale
  by construction rather than by hope. It lives in the CLI and the Hub, never in
  the agent-facing tool.
- **The Hub's Locks tab badges a project it cannot read as `UNREADABLE`**, not as
  zero leases — "all clear" is a different claim from "we don't know".
- **Honest coverage, again.** Leases gate C3's own tool surfaces. A raw `c3_shell`
  redirect, a non-Claude agent, or a human in an editor is *not* covered, and the
  UI says so on screen.

Set `locks.enabled: false` in `.c3/config.json` to opt out.

→ [Agent Locks](https://github.com/drknowhow/code-context-control/blob/main/docs/agent-locks.md)

---

## Integrations

| | What you get | Guide |
|---|---|---|
| **Credential vault** (v2.58.0) | Named secrets, global + per-project, in the OS keyring. Agents use them by name — `env_creds='NPM_TOKEN'` or `{{cred:NAME}}` — and values are decoded only at the subprocess boundary, never in model context. Hub-wide Credentials view since v2.59.0. | [guide](https://github.com/drknowhow/code-context-control/blob/main/docs/integrations.md#credential-vault) |
| **Bitbucket DC/Server** (v2.30.0) | PRs, branches, builds, repo admin over REST + PAT. Merges and branch deletes land in the edit ledger. | [guide](https://github.com/drknowhow/code-context-control/blob/main/docs/integrations.md#bitbucket-data-center--server) |
| **Jira** (v2.56.0) | Cloud (REST v3) and Data Center (REST v2) behind one tool: raw JQL, My Work board, transitions, comments, and an Activity view linking ledger work to issue keys. | [guide](https://github.com/drknowhow/code-context-control/blob/main/docs/integrations.md#jira--cloud--data-center) |
| **Oracle Discovery API** (v2.32.0) | Expose cross-project code + memory intelligence as tools for an external LLM, over MCP (`:3332/mcp`) or OpenAPI REST (`:3331/api/discovery`). Read + safe-action tools only, Bearer token in the keyring. | [guide](https://github.com/drknowhow/code-context-control/blob/main/oracle-guide/discovery-api.md) |

All tokens live in the **OS keyring** (Windows Credential Manager, macOS Keychain, Linux Secret Service) — never in `.c3/config.json`. Each supports `login --global` so one login is reusable across every C3 project.

---

## Tiered local AI (optional)

Optional Ollama integration so the primary model doesn't spend context on grunt work:

| Tier | Model class | Used for | Latency target |
|---|---|---|---|
| **Nano** | `qwen2:0.5b` | Intent routing, classification | <100 ms |
| **Micro** | `deepseek-r1:1.5b` | Last-turn Q&A, summarization | <1 s |
| **Base** | `llama3.2:3b`+ | Code analysis, technical reasoning | <5 s |

```text
c3_delegate(task="summarize this 4k-line stacktrace", backend="ollama")
c3_delegate(task="rate-limit refactor", backend="auto")    # picks the right tier
```

Ollama is fully optional. C3 works without it.

---

## Permissions (Claude Code)

C3 manages `.claude/settings.local.json` with three tiers:

| Tier | What it allows |
|---|---|
| `read-only` | Exploration only — no file writes, no git writes, no installs |
| `standard` | Normal dev workflow — edit, build, test, local git **(recommended)** |
| `permissive` | Full trust — everything except destructive ops |

```bash
c3 permissions show
c3 permissions standard
```

All tiers allow C3's MCP tools and include a hard deny list (`rm -rf`, `sudo`, `git push --force`). Switching a tier **preserves your own `allow`/`deny` rules** — only C3-managed entries are replaced. The same applies to `.mcp.json` and your hooks.

---

## Benchmarks

Every number C3 advertises is reproducible on your own machine, against your own project:

```bash
c3 bench session                  # six realistic workflow scenarios, A/B with vs without C3
c3 benchmark /path/to/project     # per-operation micro-benchmarks
c3 bench aider                    # Aider Polyglot suite (external; burns real API tokens)
c3 bench swe                      # SWE-bench Lite (external)
```

The session benchmark's baseline models a *competent* agent working without C3 — one targeted search, each file read once — and scores answer quality alongside tokens. Runs against C3's own repository land around **~50% token savings (2×)** at quality parity — 51.8% at v2.43.0, 49.9% at v2.63.2, with C3 scoring 98.8% on answer quality against the baseline's 96.5% in both. File sampling is deterministic (largest files first, no RNG), so a given commit reproduces the same figure. Your numbers will differ with your project's shape; that's why the harness ships with the tool.

C3 also records real per-tool usage to `.c3/tool_telemetry.jsonl`, so estimates can be checked against what actually happened.

---

## Security & privacy

- **All web servers bind `127.0.0.1` by default** and are guarded against browser-based attacks even on loopback — a Host-header allowlist (defeats DNS rebinding) plus an Origin/Referer check on every request (defeats cross-origin CSRF), with scoped, non-wildcard CORS. There is still **no user authentication**, so do not expose these servers to an untrusted network without auth/TLS in front. Binding to a non-loopback interface is opt-in and warned at startup. *(Hardening added in v2.33.0.)*
- **No telemetry by default.** The OSS package collects nothing. Opt-in Sentry crash reporting requires the `[telemetry]` extra plus both `SENTRY_DSN` and `C3_TELEMETRY_OPT_IN=1`; even then request bodies, local variables, and prompts are stripped.
- **LLM memory distillation is local-first.** Cloud distillation (v2.51.0) is off by default and opt-in per project.
- **API keys** for third-party providers are read from the environment and never persisted by C3.
- Full hardening guide and disclosure policy: [`SECURITY.md`](https://github.com/drknowhow/code-context-control/blob/main/SECURITY.md)

---

## Support C3

C3 is free, open source, and built by one person. If it saves you tokens — that's the whole point — consider [sponsoring on GitHub](https://github.com/sponsors/drknowhow). Sponsorship funds API costs for cross-model test runs and dedicated development time.

## License

Apache License 2.0 ([`LICENSE`](https://github.com/drknowhow/code-context-control/blob/main/LICENSE)) — free for any use, including commercial. Third-party deps: [`THIRD_PARTY_LICENSES.md`](https://github.com/drknowhow/code-context-control/blob/main/THIRD_PARTY_LICENSES.md).

The author may introduce a paid offering or relicense future major versions; no commitment either way. Releases already published under Apache-2.0 (including all 2.x versions) keep that grant irrevocably. Background: [`LICENSING.md`](https://github.com/drknowhow/code-context-control/blob/main/LICENSING.md).

## Links

- **PyPI:** https://pypi.org/project/code-context-control/
- **Changelog:** [`CHANGELOG.md`](https://github.com/drknowhow/code-context-control/blob/main/CHANGELOG.md)
- **Issues:** https://github.com/drknowhow/code-context-control/issues
- **Sponsor:** https://github.com/sponsors/drknowhow
