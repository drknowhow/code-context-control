# Oracle Changelog

## v1.4.0 (2026-07-02)

### UI Rebuild — Concat Bundle (C3 v2.49.0)
- **Modular UI**: the dashboard is now `oracle/oracle_ui.html` (shell) + 18 JS modules under `oracle/ui/` (`core`, `busy`, `theme_tabs`, `crossgraph`, `header`, `projects`, `insights`, `activity`, `suggestions`, `settings`, `agents`, `chat/markdown`, `chat/conversations`, `chat/stream_renderer`, `chat/toolbar`, `chat/input`, `chat/send`, with `app.js` last), concatenated server-side by `_build_oracle_html()` into a single response. Still vanilla JS — deliberately no framework, no build step (unlike the React-based hub).
- **`GET /` serves the bundle**; **`GET /legacy`** serves the frozen pre-bundle `oracle.html` as an escape hatch for one release, then it is removed.
- **`c3 oracle serve`** (alias `start`; flags `--port`, `--no-browser`) launches the server from the C3 CLI. `python oracle/oracle_server.py` still works from a source checkout.
- **Docs refresh**: this guide updated to match the shipped code (chat subsystem, agents, federated graph, security model, new endpoints and config keys).

---

## v1.3.0 (2026-07-02)

### Security (C3 v2.47.0)
- **Dashboard session cookie**: `GET /` (and `/legacy`) issues a per-boot `HttpOnly` `SameSite=Strict` session cookie to loopback browsers. The secret is regenerated on every server start and never written to disk.
- **Default-deny local write gate**: every mutating `/api/*` call outside `/api/discovery/*` now requires the session cookie OR the Discovery Bearer token (`/api/discovery/*` stays Bearer-only). Closes the rotate-then-read kill chain — previously any local process could POST `/api/apikey/rotate` unauthenticated and read the fresh token.

### Chat & Performance (C3 v2.47.0)
- **Native Ollama tool calling** in chat: capability probe via `/api/show` (`capabilities` list contains `tools`); the legacy `<tool_call>` text protocol is kept as fallback, including mid-turn fallback when the model rejects native tools (HTTP 400).
- **Runtime-cache unification**: `C3Bridge` now uses the shared `ProjectRuntimeCache` (8 slots, env-tunable `C3_RUNTIME_CACHE_SIZE`) with background embedding/vector warm — replaces a hand-rolled 3-slot LRU that thrashed cross-project search.
- **Scanner TTL cache**: `ProjectScanner.discover()` results are cached for `scanner_ttl_seconds` (default 20); the dashboard's explicit Scan action bypasses it.
- New config keys: `scanner_ttl_seconds` (20), `llm_cache_ttl_sec` (86400).

### Cross-Project & Agents (C3 v2.48.0)
- **`c3_project` tool** (read tier, Discovery + chat): registry listing, project info, sub-project tree, and read-only proxied ops (`search`/`read`/`compress`/`status`/`memory`/`impact`/`edits`/`validate`) by registered project name or path. Write verbs are blocked; project resolution is validated against discovered projects.
- **`c3_artifacts` tool** (read tier, Discovery + chat): agent-config artifact inventory, version history, show, diff, status. `scan` and `restore` are blocked (they mutate the target project).
- **Sub-project awareness**: `/api/projects` entries carry `parent_path` / `is_subproject` / `subproject_rel_paths` / `subproject_count`; federated graph responses gain a `hierarchy` list (parent_child project links) plus `stats.parent_child` and `projects[].parent_slug`; `c3_search_cross` / `c3_edits_cross` gain a `scope` param (`''` = all, `'top'` = top-level only, or a project name/path = that project plus its sub-projects).
- **Scheduled activity digest**: config keys `digest_enabled` (default false), `digest_interval_seconds` (86400), `digest_narrate` (false), `digest_notify_file` (`""`), `digest_retention_days` (14). Runs inside the review loop; persists to `~/.c3/oracle/activity_digests/<date>.json` + `latest.json`; new `GET /api/activity/digest/latest`.
- **Multi-backend agents**: each agent in the config `agents` roster has a `backend` field (`ollama` default | `codex` | `gemini` | `claude` | `auto`). CLI backends route through `c3_delegate` against a read-only runtime shim (memory bridges off, notifications suppressed, codex sandbox read-only) and require a project — `delegate_task` gained an optional `project_path` argument.

---

## Previously undocumented (C3 v2.32–v2.38 era)

Features that shipped without a changelog entry here, noted retroactively:
- **Interactive Chat subsystem**: `ChatEngine` (streaming tool-calling loop) + `ChatStore` (conversation persistence in `~/.c3/oracle/conversations/`) + `POST /api/chat` SSE streaming, slash commands, and per-conversation state (project focus / model / depth). Chat tab in the dashboard.
- **Team / Agents roster**: configurable specialist agents (`agents` config key) usable from chat and via the `delegate_task` tool; Team/Agents tab.
- **FederatedGraph + Cross-Graph tab**: cross-project memory graph merging per-project fact graphs with embedding/TF-IDF `cross_similar` edges; `/api/graph/federated*` endpoints + `POST /api/insights/cross`.
- **C3Bridge**: per-project C3 runtime access powering the `c3_*` discovery/chat tools.
- **`/api/apikey` endpoints**: Discovery API token status / generate / rotate / clear, backing the dashboard's Discovery API panel.
- **Activity tab** in the dashboard (companion to the v1.2.0 digest endpoint).

---

## v1.2.1 (2026-06-22)

### Security (C3 v2.39.0)
- **`POST /api/config`** now requires the Bearer token and an allowlisted key set. It was
  previously unauthenticated, allowing any local process to disable Discovery auth
  (`api_require_auth=false`) or repoint `ollama_base_url`.
- **`GET /api/apikey`** returns a masked token unless a valid Bearer token is presented; it
  previously leaked the raw token over HTTP. `generate`/`rotate` still reveal the new token
  once.
- **Discovery `project_path` validation**: project paths are validated against discovered
  projects before any read (previously any `.c3` project on the machine was readable by
  path).
- **MCP transport auth** now reads live config, so dashboard auth toggles apply without a
  restart; chat/config endpoints return JSON errors for malformed bodies; the activity
  digest now flags truncated scans.

---

## v1.2.0 (2026-06-14)

### Activity Reporting (C3 v2.38.0)
- **`ActivityReporter`** (`oracle/services/activity_reporter.py`): cross-project daily
  digest aggregating sessions, tool calls, edits, git mutations, and token/cost. Reads
  `.c3` JSONL artifacts directly per project (no C3Runtime build); skips non-C3 projects.
- **`activity_report` discovery tool** (read tier): auto-exposed on MCP, OpenAPI,
  `POST /api/discovery/call`, and the internal Oracle chat. `narrate=true` adds a
  best-effort LLM prose summary.
- **`GET /api/activity/digest`** endpoint (`date` / `since` / `until` / `project` /
  `narrate` query params) + an **Activity** tab in the Oracle dashboard.

---

## v1.1.0 (2026-04-10)

### Ollama Cloud Migration
- **OllamaBridge rewritten**: No longer wraps C3's `OllamaClient`. Now a self-contained HTTP client using `urllib.request` with `Authorization: Bearer` headers.
- **Default endpoint**: Changed from `http://localhost:11434` to `https://ollama.com`
- **API key support**: `ollama_api_key` config field + `OLLAMA_API_KEY` env var. Key priority: config > env var.
- **API key masking**: `GET /api/config` returns first 4 chars only
- **Local fallback**: Still works with local Ollama by setting `ollama_base_url` to `http://localhost:11434` and clearing `ollama_api_key`
- **Chat API**: Added `OllamaBridge.chat()` method for Ollama `/api/chat` endpoint

### UI Notification System
- **Toast notifications**: Slide-in cards (bottom-right) for all user actions. 4 types: success (green), error (red), info (blue), warning (yellow). Auto-dismiss with configurable duration.
- **Activity spinner**: Animated spinner in the header logo area, shown during async operations
- **Progress bar**: Indeterminate sliding bar below tabs, shown during busy state
- **Activity label**: Text label next to spinner showing current operation (e.g., "Loading claude-companion")
- **`tracked()` wrapper**: All action buttons go through this function which manages busy state + toast lifecycle
- **Ref-counted busy state**: Overlapping async operations correctly show/hide the spinner

### UI Settings
- **API Key field**: Password input in Settings tab for Ollama cloud API key

---

## v1.0.0 (2026-04-10)

Initial release of Oracle Memory Agent.

### Features
- **Project Discovery**: Auto-discovers C3 projects via hub API or `~/.c3/projects.json` fallback
- **Health Checking**: Validates `.c3/` structure, fact integrity, graph integrity, freshness — all heuristic, no LLM
- **Memory Reading**: Read-only access to per-project facts, graph, and session fingerprints
- **Tier Analysis**: Computes core/active/dormant/ephemeral tier distribution using C3's `MemoryScorer`
- **Cross-Project Insights**: LLM-powered pattern, risk, and opportunity detection across projects
- **Insight Types**: pattern, dependency, convention, risk, opportunity, drift
- **Suggestion Queue**: Pending write-backs (merge, archive, add) requiring explicit user approval
- **Memory Writer**: Approved suggestions written back to project `.c3/facts/facts.json`
- **Background Review Agent**: Daemon thread reviews changed projects on configurable interval (default 30 min)
- **Web UI Dashboard**: Projects, Insights, Suggestions, Settings tabs
- **Hub Integration**: Auto-detected Oracle link in C3 Hub header via health check

### Architecture Decisions
- Oracle is a separate Flask process (port 3331), fully independent of C3
- Read-only contract: never writes to project `.c3/` without explicit user approval
- All Oracle state stored in `~/.c3/oracle/` (never in project directories)
- One-way import dependency: Oracle imports `MemoryScorer` from C3, nothing in C3 imports Oracle

### Files Created
- `oracle/` — 11 files: `__init__.py`, `config.py`, `oracle_server.py`, `oracle.html`, `services/__init__.py`, `services/ollama_bridge.py`, `services/project_scanner.py`, `services/memory_reader.py`, `services/memory_writer.py`, `services/cross_memory.py`, `services/health_checker.py`, `services/insight_engine.py`, `services/review_agent.py`
- `oracle-guide/` — 5 files: `README.md`, `architecture.md`, `api-reference.md`, `configuration.md`, `changelog.md`

### Files Modified
- `cli/hub_server.py` — Added `oracle_url` to hub config defaults + POST handler (~5 lines)
- `cli/hub.html` — Added Oracle link auto-detection in header (~8 lines JS)
