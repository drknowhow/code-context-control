<h1 align="center">Code Context Control</h1>

<p align="center">
  <strong>The local code-intelligence layer for AI coding tools.</strong><br>
  Stop burning tokens on whole-file reads, blind greps, and unbounded log dumps.<br>
  Works with Claude Code, Codex, Copilot, Cursor, and Antigravity.
</p>

<p align="center">
  <a href="https://pypi.org/project/code-context-control/"><img alt="PyPI" src="https://img.shields.io/pypi/v/code-context-control?color=blue&logo=pypi&logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="Platforms" src="https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey">
  <img alt="Status: Beta" src="https://img.shields.io/badge/status-beta-yellow">
  <a href="https://github.com/sponsors/drknowhow"><img alt="Sponsor" src="https://img.shields.io/badge/sponsor-%E2%9D%A4-EA4AAA?logo=githubsponsors&logoColor=white"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_dashboard.png" alt="C3 per-project dashboard" width="900">
</p>

---

## The problem

LLM-driven coding tools have one expensive failure mode: **they read too much.** They `cat` whole files, regex the entire repo, dump 10k-line logs into context, edit-and-pray, and burn through budget before they touch a single line of code. On a half-day session you can spend $20+ on token waste that adds zero value.

## What C3 does about it

A thin **local** layer that sits between your IDE and your repo. Every AI tool call gets routed through narrow, surgical operations instead of broad, wasteful ones:

| Without C3 | With C3 |
|---|---|
| `Read` the whole 2,000-line file | `c3_compress` returns a structural map at 40-70% of the original token count (30-60% smaller) → `c3_read(symbols=...)` for the exact function |
| `Grep` the whole repo blindly | `c3_search` returns ranked candidates with TF-IDF + symbol awareness |
| Dump full `pytest` output into the prompt | `c3_filter` distills 500 lines → 30 actionable ones |
| Edit, hope it compiled | `c3_edit` writes via a ledger + `c3_validate` runs `pyright`/`tsc` automatically |
| `Bash` test runs that hang on Windows | `c3_shell` returns structured `{exit_code, stdout, stderr, duration}` with auto-filter |
| Lose all context on `/clear` | `c3_session(snapshot)` + `c3_memory` persist decisions across sessions |
| Re-explain the project every session | Auto-synced `CLAUDE.md` / `AGENTS.md` / `copilot-instructions.md` from a single source of truth |

Everything runs **locally**. No source code, prompts, or model output ever leaves your machine unless you explicitly opt into a third-party model API.

---

## Install

Requires Python 3.10+. No clone needed — C3 is published on PyPI.

The recommended install is [pipx](https://pipx.pypa.io) (isolated environment, on your PATH):

```bash
pipx install code-context-control
c3 init /path/to/your/project
```

Or with pip:

```bash
pip install "code-context-control[tui]"   # [tui] adds the optional Textual UI
c3 init /path/to/your/project
```

Running `c3` with no arguments opens the interactive TUI. `c3 init` walks you through:
1. **IDE selection** (Claude Code CLI/App, Codex CLI, VS Code, Cursor, Antigravity, or Custom — the Gemini CLI profile was removed in v2.52; use Antigravity, which reads AGENTS.md)
2. Optional local `git init`
3. MCP server registration (auto-wired into your IDE)
4. (Claude Code only) Permission tier selection

Headless / scripted install:

```bash
c3 init /path/to/project --force --ide claude --mcp-mode direct --permissions standard
```

### Upgrading

```bash
c3 upgrade                          # upgrade the running install in place
c3 upgrade --check                  # just report whether a newer release exists
# equivalently:
pipx upgrade code-context-control
pip install -U code-context-control
```

MCP is wired through the `c3-mcp` entry point, so upgrading needs **no per-project
reconfiguration** — your existing `.mcp.json` files keep working. C3 also nudges you
in-app when a newer release is available.

### From source (contributors)

```bash
git clone https://github.com/drknowhow/code-context-control.git
cd code-context-control
pip install -e ".[dev]"             # editable dev install: tests, linters, build tools
```

---

## A tour of the UI

C3 ships with two web UIs (no electron, no install — pure Flask + vanilla JS):

- **The Hub** (`c3-hub`, port 3330) — manage all your C3 projects from one dashboard.
- **The per-project UI** (`c3 ui`, per-project port) — deep dive into one project's session, memory, edits, instructions, and settings.

### 1. Project Hub — multi-project mission control

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/hub_projects.png" alt="C3 Project Hub - all projects" width="900">
</p>

Every C3-initialized project on your machine appears here automatically — `c3 init` registers the project with the hub on first run, no extra step. Group them by tag, filter by active/idle, see which IDE each project uses, jump straight into your IDE with one click, and monitor session activity at a glance. Each project card shows live status, version, MCP wiring mode, port, and last activity.

Per-card actions cover the full lifecycle: launch the IDE, open the per-project UI, edit name / tags / notes, transfer the registration to a new path, **merge** another project's memory + conversation history + edit ledger into this one (with optional source cleanup), or remove it from the registry.

**Sub-projects — nested repos as first-class children.** Designate any sub-folder (a service in a monorepo, a vendored tool) as a linked child project with its own `.c3`: the parent's index excludes the child's subtree, and `c3_search` / `c3_memory` fan out across children on demand (`scope='all'` or one child by name). The whole hierarchy is manageable from the hub:

- **Designate** from any project card or the drill-in **Sub-projects** tab — folder picker with upfront validation, adopt-vs-initialize preview, and IDE choice. An existing top-level project that physically sits inside a parent can be re-linked via **"Make sub-project of…"**.
- **Link health is passive** — parent cards show a red "N link issues" badge and children show their link status automatically; **Reconcile** shows exactly what's broken and repairs it on confirm.
- **Cascade** update / reindex / health across all children (cancellable, optionally including the parent), **promote** a child back to top-level, or **de-initialize** it entirely behind a typed-name confirm.
- **Change parent…** moves a child between parents honestly: folders must physically nest, so the wizard stages the move, validates every step, and never leaves a broken state silently.
- Federation behavior per parent — memory roll-up, search fan-out, children per query — is editable in the project's config editor.

**Open in your IDE of choice** — C3 auto-detects which CLIs you have installed and gives you one-click launchers:

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/hub_ide_config.png" alt="C3 Hub IDE picker" width="700">
</p>

Hub runs as a **background Windows service** if you want it to (no terminal, auto-starts on login):

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/hub_settings.png" alt="C3 Hub settings" width="700">
</p>

### 2. Per-project dashboard — at-a-glance health

```bash
c3 ui                             # opens http://127.0.0.1:3333
```

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_dashboard.png" alt="C3 per-project dashboard" width="900">
</p>

Illustrative example from one project's dashboard (numbers vary by project): **448K tokens saved** (89.9% rate) — C3's estimate versus a full-file-read baseline — plus 208 files indexed, 20 sessions, codebase breakdown by language, current-session live counters (in/out tokens, cache reads, services online), and a stream of recent tool calls and file changes. Run `c3 bench session` on your own project to generate your own scorecard.

### 3. Edit Ledger — every AI-driven edit tracked

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_edits.png" alt="C3 Edit Ledger" width="900">
</p>

A complete audit trail of every file change made via `c3_edit` (or via Bash git commands intercepted by the C3 shell). Each entry shows timestamp, file, version number, change summary, and +/- line counts. Filter by file path, switch to **Stats** view for aggregate trends. Backed by a content-addressable store so any prior version is one click away.

### 4. Memory — durable knowledge across sessions

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_memory.png" alt="C3 Memory" width="900">
</p>

A categorized store of facts the AI has learned about your project (architecture decisions, conventions, gotchas, references). Categories, full-text + semantic search, decision tracking, list ↔ graph toggle, and one-click Markdown export. Backed by TF-IDF + optional Ollama embeddings + `chromadb` (when the `[vector]` extra is installed).

### 5. Sessions — history, decisions, costs

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_sessions.png" alt="C3 Sessions" width="900">
</p>

Every session you've ever run, with duration, decision count, file count, tool calls, token usage, and cost. Captured automatically via the IDE's Stop hook — nothing to remember to click. Click any row to see the full task list, decisions, and file diffs from that session.

### 6. Instructions — sync your project context across IDEs

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_instructions.png" alt="C3 Instructions" width="900">
</p>

Manage `CLAUDE.md`, `AGENTS.md` (Codex), `GEMINI.md`, and `.github/copilot-instructions.md` from one editor. Generate from project state, run a Health Check (drift detection vs the actual codebase), Compact stale sections, or Promote insights captured during sessions. **One source of truth** instead of four out-of-sync files.

C3-generated content is wrapped in a `<!-- C3:BEGIN … -->` / `<!-- C3:END -->` block. Regenerating (or `Compact`) only rewrites that block — **anything you write outside it is preserved**, so it's safe to keep your own notes in the same file.

**Live repo map (v2.60.0):** generated docs no longer embed a frozen project tree. They carry a stable pointer to `.c3/MAP.md` — a machine-owned, byte-stable map (commands, entry points, module one-liners, tree) that C3 refreshes automatically via edit hooks and a first-tool-call freshness check. Manage it with `c3 map status|ensure|refresh`; set `map.enabled=false` in `.c3/config.json` to restore the embedded tree.

### 7. Chat — browse prior AI conversations

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_chat.png" alt="C3 Chat" width="900">
</p>

C3 syncs and indexes your IDE's chat transcripts (currently Claude Code; others coming). Filter by source, search, click a row to view the full conversation. Useful for "wait, what did we decide about X last week?".

### 8. Settings — feature flags + integrations

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/ui_settings.png" alt="C3 Settings" width="900">
</p>

Per-project knobs for everything: budget thresholds, feature flag mode, edit ledger, background agents, delegate routing, Codex/Gemini integrations, agent workflows, proxy mode, MCP servers, Claude Code permission tier, and more.

---

## The MCP tool suite

C3 exposes 18 tools as a native MCP server. Your IDE calls them directly:

| Tool | What it does |
|---|---|
| `c3_search` | TF-IDF / regex / semantic search ranked across the indexed repo |
| `c3_compress` | AST-based file map (modes: `map`, `dense_map`, `smart`, `diff`, `bug_scan`, `ast`) |
| `c3_read` | Surgical reads — by symbol name, regex, or line ranges |
| `c3_edit` | Atomic patch with automatic ledger logging + content-addressable history |
| `c3_validate` | Type / syntax check (pyright, tsc, ruff, etc. — auto-detected) |
| `c3_filter` | Distill long terminal/log output via pattern + LLM summarization |
| `c3_shell` | Run shell commands with structured returns + auto-filtered stdout |
| `c3_status` | Project health, token budget, notifications, ghost-file detection |
| `c3_memory` | Persistent fact store with categories, recall, and graph queries |
| `c3_session` | Snapshot, restore, log decisions, compact session history |
| `c3_impact` | Blast-radius analysis before edits to shared symbols |
| `c3_delegate` | Offload heavy work to local Ollama / Codex / Gemini / etc. |
| `c3_agent` | Multi-step agentic workflows (review, investigate, refactor) |
| `c3_edits` | Edit-ledger queries + version diffs + restore points + per-branch filter |
| `c3_bitbucket` | Bitbucket Data Center integration — PRs, branches, builds, repo admin (v2.30.0) |
| `c3_jira` | Jira integration — Cloud + Data Center: JQL search, issues, transitions, My Work board (v2.56.0) |
| `c3_credentials` | Credential vault — named secrets (global + per-project), injection-first: agents use them by name, values never enter model context (v2.58.0) |
| `c3_project` | Cross-project — discover & operate on other c3-installed projects; guarded writes (v2.31.0) |
| `c3_task` | Durable per-project PM — tasks with dependencies & subtasks, milestones, decision notes, event history, health reports, and auto+manual time tracking (v2.53.0) |
| `c3_artifacts` | Agent-config tracking — version history, diff & restore for CLAUDE.md, settings/hooks, MCP configs, skills (v2.46.0) |

On Windows, `c3_shell` uses Git Bash when available. Git Bash does not bundle
optional utilities such as `jq`; use `python -m json.tool` for portable JSON
formatting, or install `jq` separately when filter expressions are required.

Every tool is **read-only safe in plan mode** (except `c3_edit`, `c3_shell`, `c3_artifacts(action='restore')`, and write actions on `c3_bitbucket` / `c3_jira` / `c3_credentials` / `c3_project` / `c3_task`).

### Bitbucket Data Center / Server (v2.30.0)

`c3_bitbucket` connects to self-hosted enterprise Bitbucket via REST + Personal
Access Token. Tokens live in the **OS keyring** (Windows Credential Manager,
macOS Keychain, Linux Secret Service) — never in `.c3/config.json`.

```bash
# One-time login per server (stored under this project's .c3/config.json)
c3 bitbucket login --url https://bitbucket.example.com
#  -> prompts for username + PAT (masked)

# ...or store it globally so every C3 project can use it
c3 bitbucket login --global --url https://bitbucket.example.com

# Pin defaults so subsequent calls don't need project/repo
c3 bitbucket set-default --project PROJ --repo my-service

# Inspect status
c3 bitbucket status
```

**Account resolution precedence:** the project's `.c3/config.json` wins, but when
it has no active account C3 falls back to the global `~/.c3/config.json`. So a
single `login --global` (or any login done from your home directory) is reusable
across every C3 project — the PAT always lives in the OS keyring, never on disk.

### Credential vault (v2.58.0)

`c3_credentials` gives agents a protected, user-managed place for API keys,
tokens, and `.env`-style values — **global** (`~/.c3`, every project) or
**per-project** (`.c3`, shadows the global name). Values live in the **OS
keyring** (large values in a Fernet-encrypted `.c3/secrets.enc` whose master
key lives in the keyring) — never in config files, and *never in the model's
context*: the agent addresses secrets by name and C3 decodes them only at the
subprocess boundary.

```bash
# Store a secret for this project (value prompted, masked)
c3 creds set OPENAI_KEY --desc "OpenAI billing key"

# ...or globally for every C3 project
c3 creds set NPM_TOKEN --global

# Bulk-import an existing .env; list what the agent can see
c3 creds import .env
c3 creds list
```

The agent then runs commands *with* the secret but without ever seeing it:

```
c3_shell(cmd='npm publish', env_creds='NPM_TOKEN')       # injected as env var
c3_shell(cmd='curl -H "Authorization: Bearer {{cred:OPENAI_KEY}}" …')  # expanded server-side
```

Echoed values are auto-redacted from output (`env` dumps come back as
`[cred:NAME]`), every use is ledger-logged by name, and `reveal` — the only
action that returns a value — is disabled per entry until you flip
`agent_readable` in the **Credentials UI tab** or via
`c3 creds set NAME --agent-readable`. A hostile repo config can't siphon your
global secrets (realm-atomic resolution, tested), cross-project shells run
with credentials disabled, and the vault is hard-excluded from the Oracle
Discovery API.

Since **v2.59.0** the Hub has a top-level **Credentials** view: manage the
global vault (`~/.c3`) and every registered project's entries from one place,
with overriding shown both ways ("overrides global" on project entries,
"overridden ×N" on globals). **v2.61.0** makes it navigable at scale:

- **Cross-project search** — <kbd>/</kbd> or <kbd>Ctrl/⌘-K</kbd>. Free words
  match name / description / env var / project; `project:` `scope:` `type:`
  `storage:` `name:` `env:` `inject:` `agent:` `shadow:` qualifiers narrow
  further. Results are **grouped by credential name**, so "where is
  `STRIPE_KEY` defined and which one wins?" is one glance instead of forty
  accordions. `agent:true` and `inject:true` are one-key exposure audits.
- **Per-credential settings drawer** — metadata, the two exposure switches
  with their blast radius written out, an on-demand resolution check +
  fingerprint, write-only value replacement, usage and override
  relationships, and a separated danger zone.
- **Right-click context menu** on any row (also `⋯` and <kbd>Shift</kbd>+<kbd>F10</kbd>),
  with typed confirmations replacing `window.confirm`: deleting or granting
  agent-read access requires typing the credential's name.

The write-only wire contract extends to the hub: values are submitted
inbound-only, **no hub route ever returns a stored value** (there is no
`reveal` on the hub at all), and search indexes metadata only.

Full documentation: [`cli/guide/credentials.html`](cli/guide/credentials.html)
— open it in the app at `/guide/credentials.html`.

### Access Guard (v2.62.0)

The Credential Vault protects *values*; **Access Guard** protects *files and
folders*. Two glob lists in your config mark what an agent must never read
(`deny`) or never write (`read_only`), and one shared evaluator enforces them
at every C3 surface — the MCP tools (`c3_read`/`c3_edit`/`c3_search`/…, so
any agent using C3 is covered), Claude Code's native tools via PreToolUse
hooks (fail-closed: a broken guard denies writes instead of waving them
through), and `c3_shell` via a best-effort command scan.

```bash
c3 access add "secrets/**" --kind deny        # never read, never write
c3 access add "migrations/**" --kind read_only
c3 access check secrets/key.txt               # probe: verdict + matched rule
```

- **Tighten-only.** No allow list exists; scopes (global `~/.c3` + project
  `.c3`) merge as a union. A cloned repo's config can only add protection,
  never grant itself access.
- **`deny` means deny-create and deny-enumerate** too: alternate spellings
  are canonicalized before matching, and denied paths never appear in search
  results, maps, or the vector index.
- **Refusals teach the agent to stop.** Every denial carries a stable
  `[c3-access:*]` tag, the matched rule and scope, and explicit
  do-not-retry guidance — no retry loops, no "file must not exist, let me
  recreate it".
- **All rule changes are human-only** (Access Guard UI tab or `c3 access`
  CLI), ledger-logged; agents have no mutation surface.
- **Built-ins always on:** `.env*` files, the credential vault's sidecars,
  and write-denies on `.c3/`, `.claude/settings*.json`, `.git/`, and the
  installed C3 package itself.
- **Honest coverage:** this guards *cooperative* agents against mistakes and
  prompt-injection. It is not a sandbox — raw shell or direct file access
  outside C3's tools and hooks is not stopped. The guide states exactly
  where each layer holds.

Full documentation: [`cli/guide/access.html`](cli/guide/access.html)
— open it in the app at `/guide/access.html`.

### Jira — Cloud + Data Center (v2.56.0)

`c3_jira` connects to Jira Cloud (REST v3, email + API token) or self-hosted
Jira Data Center / Server (REST v2, PAT) behind one tool. Tokens live in the
**OS keyring** — never in `.c3/config.json`.

```bash
# One-time login — Cloud is inferred for *.atlassian.net
c3 jira login --url https://yoursite.atlassian.net
#  -> prompts for email + API token (masked)

# Self-hosted Data Center / Server
c3 jira login --url https://jira.example.com --deployment data_center

# ...or store it globally so every C3 project can use it
c3 jira login --global --url https://yoursite.atlassian.net

# Pin a default project; check connectivity
c3 jira set-default --project PROJ
c3 jira status
```

The agent gets `c3_jira`: raw-JQL `search`, `my_issues`, issue reads, and
ledger-logged mutations (`create_issue` is pre-validated against create
metadata and returns machine-readable missing required fields; `transition`
accepts an id or a name). The web UI gains a **Jira tab** — a My Work board
grouped by status category, JQL search, an issue drawer with transitions and
comments, and an Activity view that links edit-ledger work to issue keys
(`PROJ-123` detected in branch names and edit summaries; works even before
login). Named accounts support multiple sites (`--name work`, `--name
internal`); the registry resolves project → home **wholesale from one file**,
so a repository's config can never override the credential-bound server URL
or TLS settings of a globally registered account.

> **Upgrading:** stop the running `c3-mcp` server / CLI before `c3 upgrade`. A live
> process can hold package files open, leaving pip's `~`-prefixed backup dirs
> (`~ervices`, `~ools`, …) in `site-packages`; those are inert and safe to delete
> after the upgrade completes.

The MCP tool dispatches by `action`. Read-only actions: `status`, `whoami`,
`list_projects`, `list_repos`, `get_repo`, `list_prs`, `get_pr`, `get_pr_diff`,
`get_pr_activities`, `list_branches`, `list_commits`, `list_activity`,
`build_status`, `repo_settings`, `list_webhooks`, `list_permissions`. Write
actions: `create_pr`, `comment_pr`, `approve_pr`, `unapprove_pr`, `decline_pr`,
`merge_pr`, `create_branch`, `delete_branch`, `update_repo_settings`,
`create_webhook`, `delete_webhook`. PR merges and branch deletes are recorded
to the C3 edit ledger so the audit trail covers platform-side changes too.

The **Hub UI** (per-project) gains a "Bitbucket" tab with sub-views for
Overview / Pull Requests / Branches / Activity / Admin.

### Oracle Discovery API (v2.32.0)

The **Oracle** is C3's optional cross-project memory agent (a local web app). As of
v2.32.0 it can expose C3's cross-project code & memory intelligence as **tools for an
external LLM** — point Claude (or any function-calling model) at a running Oracle and
it can discover your projects and search code, memory, and the cross-project graph
across all of them.

Two transports share one tool core:

- **MCP** (streamable HTTP/SSE) at `http://127.0.0.1:3332/mcp` — native for Claude
  Code / Claude Desktop / any MCP client.
- **OpenAPI REST** at `http://127.0.0.1:3331/api/discovery` — for any LLM with
  function-calling (fetch `/openapi.json` to auto-register the tools).

```bash
# Start the Oracle (serves the REST + MCP discovery endpoints)
python oracle/oracle_server.py --no-browser

# Print the Bearer token + a ready-to-paste .mcp.json snippet
c3 oracle api info
```

Only **read** and **safe-action** tools are exposed (no code editing); requests need a
**Bearer token** (stored in the OS keyring) and both servers bind `127.0.0.1` by
default. Generate, rotate, and copy the token from the dashboard's **Settings →
Discovery API** tab. See the [Oracle Discovery API guide](oracle-guide/discovery-api.md).

As of v2.38.0 the Oracle also reports a **cross-project activity digest** — sessions,
tool calls, edits, git mutations, and token/cost for a day — via the `activity_report`
discovery tool, the `GET /api/activity/digest` endpoint, and the dashboard's **Activity** tab.

---

## Tiered local AI (optional)

C3 ships with optional Ollama integration so the primary model doesn't have to waste context on grunt work:

| Tier | Model class | Used for | Latency target |
|---|---|---|---|
| **Nano** | `qwen2:0.5b` | Intent routing, classification | <100 ms |
| **Micro** | `deepseek-r1:1.5b` | Last-turn Q&A, summarization | <1 s |
| **Base** | `llama3.2:3b`+ | Code analysis, technical reasoning | <5 s |

```text
c3_delegate(task="summarize this 4k-line stacktrace", backend="ollama")
c3_delegate(task="rate-limit refactor", backend="auto")    # picks the right tier
```

Ollama is **fully optional**. C3 works without it.

---

## Permissions (Claude Code)

C3 manages `.claude/settings.local.json` for you, with three sensible tiers:

| Tier | What it allows |
|---|---|
| `read-only` | Exploration only — no file writes, no git writes, no installs |
| `standard` | Normal dev workflow — edit, build, test, local git **(recommended)** |
| `permissive` | Full trust — everything except destructive ops |

All tiers always allow C3 MCP tools and include a hard deny list (`rm -rf`, `sudo`, `git push --force`, etc.).

```bash
c3 permissions show
c3 permissions standard
```

Applying or switching a tier **preserves your own `allow`/`deny` rules** (and keys like `ask`/`defaultMode`) — only C3-managed entries are replaced. Likewise, C3 never clobbers your other entries in `.mcp.json` (only its own `c3` server) or the hooks you've added to `settings.local.json` (only its own hooks).

---

## Benchmarks

Don't take our word for it — every number C3 advertises is reproducible on your own machine, against your own project:

```bash
c3 bench session                  # six realistic workflow scenarios, A/B with vs without C3
c3 benchmark /path/to/project     # per-operation micro-benchmarks (compression, retrieval, filtering, validation)
c3 bench aider                    # Aider Polyglot suite (external; burns real API tokens)
c3 bench swe                      # SWE-bench Lite (external)
```

The session benchmark's baseline models a *competent* agent working without C3 — one targeted search, each file read once — not a strawman that re-reads everything, and it scores answer quality alongside tokens. For reference, a run against C3's own repository (v2.43.0, 2026-07-02) measured **51.8% token savings (2.07×)** across the six scenarios at quality parity. Your numbers will differ with your project's shape — that's why the harness ships with the tool.

Beyond synthetic scenarios, C3 records real per-tool usage to `.c3/tool_telemetry.jsonl`, so estimated savings can always be checked against what actually happened in your sessions.

Reports include token deltas, cost deltas, win rates, tool-usage analysis, and per-task breakdowns. See the **Benchmark Dashboard** under Settings → Background Agents in the Hub.

---

## Security & privacy

- **All web servers (Hub, per-project UI, Oracle) bind to `127.0.0.1` by default and are guarded against browser-based attacks even on loopback** — a Host-header allowlist (defeats DNS rebinding) plus an Origin/Referer check on every request (defeats cross-origin CSRF), with scoped, non-wildcard CORS. A malicious web page you visit therefore cannot drive C3's local endpoints. There is still **no user authentication**, so do not expose these servers to an untrusted network without auth/TLS in front. Binding to a non-loopback interface in `~/.c3/hub_config.json` (`host`) or Oracle's config (`bind_host`) is opt-in and warned at startup; add externally-facing hostnames/IPs to an `allowed_hosts` list there so the guard permits them. _(Cross-origin/CSRF + DNS-rebinding hardening added in v2.33.0.)_
- **No telemetry by default.** The OSS package collects nothing. Opt-in Sentry crash reporting requires the `[telemetry]` extra plus both `SENTRY_DSN` and `C3_TELEMETRY_OPT_IN=1`. Even when enabled, request bodies, local variables, and prompts are stripped before sending.
- **API keys** for third-party model providers are read from environment variables and never persisted by C3.
- See [`SECURITY.md`](SECURITY.md) for the full hardening guide and disclosure policy.

---

## Support C3

C3 is free, open source, and built by one person. If it saves you tokens (it should — that's the whole point), consider [sponsoring on GitHub](https://github.com/sponsors/drknowhow). Sponsorship directly funds API costs for cross-model test runs and dedicated development time.

---

## License

- **Current OSS license** — Apache License 2.0 ([`LICENSE`](LICENSE)). Free for any use, including commercial. Modify, fork, redistribute — all permitted under Apache-2.0 terms.
- **Third-party deps** — see [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).

The author may introduce a paid offering or relicense future major versions; no commitment either way. Releases already published under Apache-2.0 (including all 2.x versions) keep their Apache-2.0 grant — that grant is irrevocable. Background and FAQ in [`LICENSING.md`](LICENSING.md). No warranty or support obligation; see LICENSE Sections 7–8.

---

## Links

- **PyPI:** https://pypi.org/project/code-context-control/
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md)
- **Oracle Discovery API:** [`oracle-guide/discovery-api.md`](oracle-guide/discovery-api.md)
- **Security policy:** [`SECURITY.md`](SECURITY.md)
- **Licensing FAQ:** [`LICENSING.md`](LICENSING.md)
- **Issues:** https://github.com/drknowhow/code-context-control/issues
- **Sponsor:** https://github.com/sponsors/drknowhow
