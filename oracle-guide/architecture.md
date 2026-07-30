# Oracle Architecture

## System Overview

```
                    ┌───────────────────┐
                    │  Ollama Cloud API │
                    │ https://ollama.com│
                    │  Bearer token auth│
                    └────────┬──────────┘
                             │
                    ┌────────▼──────────┐
                    │   OllamaBridge    │
                    │ (self-contained   │
                    │  HTTP client)     │
                    └────────┬──────────┘
                             │
┌──────────┐        ┌────────▼──────────┐        ┌──────────────┐
│  C3 Hub  │◄──────►│  Oracle Server    │───────►│ Cross Memory │
│ :3330    │  HTTP   │  :3331 (Flask)    │        │ ~/.c3/oracle │
└──────────┘        └────────┬──────────┘        └──────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼──────┐  ┌─────────▼──────┐  ┌──────────▼────────┐
│  Project     │  │  Health        │  │  Insight           │
│  Scanner     │  │  Checker       │  │  Engine            │
└──────────────┘  └────────────────┘  └───────────────────┘
        │                    │                    │
        │           ┌────────▼─────────┐          │
        │           │  Memory Writer   │          │
        │           │ (suggest → approve│          │
        │           │  → write-back)   │          │
        │           └──────────────────┘          │
        └────────────────────┼────────────────────┘
                             │
                    ┌────────▼──────────┐
                    │  Review Agent     │
                    │ (background daemon│
                    │  thread)          │
                    └───────────────────┘
```

## Folder Structure

```
oracle/
  __init__.py              # Package marker
  oracle_server.py         # Flask app + entry point (~1,070 lines)
  oracle_ui.html           # Web UI shell (concat bundle: served at / with oracle/ui/*.js inlined)
  config.py                # Config loader + defaults
  mcp_oracle.py            # FastMCP HTTP/SSE server for the Discovery tools (:3332/mcp)
  ui/                      # 18 vanilla-JS modules, concatenated server-side (see Web UI below)
    core.js  busy.js  theme_tabs.js  crossgraph.js  header.js  projects.js
    insights.js  activity.js  suggestions.js  settings.js  agents.js  app.js
    chat/                  # markdown.js  conversations.js  stream_renderer.js
                           # toolbar.js  input.js  send.js
  services/
    __init__.py
    ollama_bridge.py       # Self-contained Ollama cloud client with Bearer auth + cache
    project_scanner.py     # Discovers projects via hub API or ~/.c3/projects.json (TTL cache)
    memory_reader.py       # Read-only access to .c3/facts/ + MemoryScorer tier computation
    memory_writer.py       # Suggestion queue + approved write-backs to project facts
    cross_memory.py        # Cross-project insight store with dedup + project linking
    health_checker.py      # Validates .c3/ structure + fact/graph integrity (no LLM)
    insight_engine.py      # LLM-powered analysis: per-project + cross-project + consolidation
    review_agent.py        # Background daemon thread: reviews + scheduled activity digest
    activity_reporter.py   # Cross-project daily activity digest from .c3 JSONL artifacts
    api_auth.py            # Discovery Bearer token in the OS keyring (C3_ORACLE_API_KEY override)
    c3_bridge.py           # Per-project C3 runtime cache + read-only c3_* tool proxies
    chat_engine.py         # Streaming chat orchestrator with tool-calling loop + agents
    chat_store.py          # Conversation persistence (~/.c3/oracle/conversations/)
    federated_graph.py     # Cross-project memory graph (embedding/TF-IDF similarity edges)
    local_session.py       # Per-boot dashboard session cookie (HttpOnly, SameSite=Strict)
    tool_executor.py       # Thin adapter: Discovery API -> ChatEngine.run_tool (one dispatch path)
    tool_registry.py       # TOOL_SPECS (JSON schemas + tiers) + validated dispatch + OpenAPI
```

## Data Storage

All Oracle state lives under `~/.c3/oracle/` — never inside any project directory.

| File / Directory | Purpose |
|------------------|---------|
| `config.json` | Port, model, Ollama URL, API key, intervals, theme |
| `cross_memory.json` | Cross-project insights + project-to-project links |
| `review_state.json` | Per-project last-reviewed timestamps + facts.json mtimes |
| `suggestions.json` | Pending write-back suggestions (merge, archive, add) |
| `project_reports/<hash>.json` | Cached health reports per project (SHA256 hash of path) |
| `cache/llm/<md5>.json` | LLM response cache (keyed by model+prompt+options; TTL `llm_cache_ttl_sec`) |
| `conversations/` | Chat conversations + per-conversation state (JSON files + `index.json`) |
| `federated_graph.json` | Cached federated graph build (TTL + facts-mtime keyed) |
| `federated_embeddings.json` | Per-fact embedding cache for cross-project similarity |
| `activity_digests/<date>.json` + `latest.json` | Scheduled activity digests written by the review loop |
| `oracle.log` | Background agent + server log |

## Service Descriptions

### OllamaBridge (`ollama_bridge.py`)
Self-contained HTTP client for the Ollama API. Does **not** depend on C3's `OllamaClient` — it uses `urllib.request` directly with `Authorization: Bearer` headers. Supports both cloud (`https://ollama.com`) and local (`http://localhost:11434`) endpoints. API key priority: explicit config > `OLLAMA_API_KEY` env var. Includes its own disk-based LLM response cache.

### ProjectScanner (`project_scanner.py`)
Discovers registered C3 projects. Primary: HTTP GET to hub `/api/projects` (2s timeout). Fallback: reads `~/.c3/projects.json` directly. Enriches each project with `.c3/` presence, fact count, and `facts.json` mtime.

### MemoryReader (`memory_reader.py`)
Read-only access to a project's `.c3/facts/` directory. Reads `facts.json`, `memory_graph.json`, and `session_fingerprints.json`. Computes summary statistics (fact count by category/lifecycle) and tier distribution using C3's `MemoryScorer`.

### HealthChecker (`health_checker.py`)
Heuristic validation — no LLM calls. Checks: directory structure, JSON validity, required fact fields, duplicate IDs, orphaned graph edges, freshness. Returns a health report with status (`ok`/`warning`/`error`) and issue list.

### MemoryWriter (`memory_writer.py`)
Two-phase write system. Phase 1: `suggest()` stores a pending suggestion in `~/.c3/oracle/suggestions.json`. Phase 2: `approve_suggestion()` executes the write to the target project's `facts.json`. Supports three operations: `merge_facts` (keep survivor, archive duplicates), `archive_facts` (set lifecycle=archived), `add_fact` (inject new fact with source_quality=oracle).

### CrossMemory (`cross_memory.py`)
Stores cross-project insights in `~/.c3/oracle/cross_memory.json`. Each insight has: id, type (pattern/dependency/convention/risk/opportunity/drift), text, source projects, confidence, tags. Deduplicates by Jaccard similarity > 0.6. Maintains a project link graph where link strength increases as more insights connect the same pair.

### InsightEngine (`insight_engine.py`)
LLM-powered analysis via OllamaBridge. Three operations:
1. `analyze_project()` — Reviews top-N facts, returns health narrative + suggestions
2. `find_cross_project_links()` — Compares fact summaries across projects, identifies patterns/risks
3. `suggest_consolidation()` — Finds duplicate/stale facts, generates merge/archive suggestions

All LLM responses are parsed from JSON (with fallback for markdown-fenced JSON).

### ReviewAgent (`review_agent.py`)
Background daemon thread (default: 30 min interval). Each cycle: discover projects → detect changes (facts.json mtime) → run health checks → cache reports → generate cross-project insights (if 2+ projects changed) → auto-suggest consolidation for projects with 30+ facts. State persisted in `review_state.json`. When `digest_enabled` is set, the loop also emits the scheduled activity digest every `digest_interval_seconds` (persisted to `~/.c3/oracle/activity_digests/`, pruned after `digest_retention_days`, optional JSONL notify sink via `digest_notify_file`).

### Chat subsystem (`chat_engine.py`, `chat_store.py`)
`ChatEngine` orchestrates the interactive chat: it builds a dynamic system prompt from conversation state (focused projects, model, depth), runs a bounded tool-calling loop, and streams every step as SSE events. Tool calling is **native Ollama tool calling** when the model supports it (capability probe via `/api/show`), with the legacy `<tool_call>` text protocol as fallback — including mid-turn fallback if the model rejects native tools at request time (HTTP 400). Tools are the same registry the Discovery API uses (single dispatch path via `ChatEngine.run_tool`). `delegate_task` runs a configured agent — Ollama-backed agents stream their own sub-loop; CLI-backed agents (codex/gemini/claude/auto) route through `c3_delegate` on a read-only runtime shim.

`ChatStore` persists conversations as JSON files in `~/.c3/oracle/conversations/` (plus `index.json` and per-conversation state). Slash commands (`/project`, `/model`, `/depth`, `/health`, `/clear`, `/help`, `/tools`, `/team`) mutate that state.

**SSE event types** streamed by `POST /api/chat`: `meta`, `status`, `thinking`, `text`, `tool_call`, `tool_result`, the agent sub-stream events `agent_start` / `agent_round` / `agent_thinking` / `agent_text` / `agent_tool_call` / `agent_tool_result` / `agent_done`, then `done` or `error`; the stream ends with a literal `data: [DONE]` terminator.

### C3Bridge (`c3_bridge.py`)
Bridge between Oracle and C3's tool handlers with a per-project runtime cache — the shared `ProjectRuntimeCache` (8 slots by default, env-tunable `C3_RUNTIME_CACHE_SIZE`), with embedding/vector backends warmed on a background thread so the first `c3_search` on a project doesn't pay the init cost inline. Every call goes through `validate_project_path()`, which resolves the path and confirms it is a *discovered* C3 project — a runtime can never be built for an arbitrary path on the machine. Read-only enforcement: write actions on `c3_edits`/`c3_memory`/`c3_project`/`c3_artifacts` are blocked at the bridge, and Oracle-initiated `c3_delegate` calls see a read-only runtime view (memory bridges off, notifications suppressed).

### Federated graph (`federated_graph.py`)
Builds one cross-project memory graph: per-project fact graphs are merged, then `cross_similar` edges are added between facts of *different* projects using Ollama embeddings (`embedding_model`, default `nomic-embed-text`) with a TF-IDF cosine fallback when embeddings are unavailable. Thresholds are tunable (`cross_sim_threshold`, `cross_max_facts_per_project`, `cross_top_k_neighbors`). Builds are cached to disk (`federated_graph.json`) keyed by facts-file mtimes with a TTL (`federated_graph_ttl_sec`); embeddings have their own persistent cache. Project hierarchy (parent/child sub-project links) is overlaid at serve time on both fresh builds and cache hits — responses carry `hierarchy`, `stats.parent_child`, and `projects[].parent_slug`.

### Security model
Layered, default-deny:

1. **Loopback bind** — `bind_host` defaults to `127.0.0.1` for both the Flask app (:3331) and the MCP transport (:3332).
2. **Host/CSRF guard** — Host-header allowlist + Origin/Referer check (shared `core/web_security.py` guard) blocks cross-origin browsers even on the same machine.
3. **Discovery Bearer token** — all `/api/discovery/*` requests (and the MCP transport) require `Authorization: Bearer <token>`; the token lives in the OS keyring (`api_auth.py`), overridable via `C3_ORACLE_API_KEY`.
4. **Local session cookie + write gate** (v2.47.0) — a per-boot `HttpOnly` `SameSite=Strict` session cookie (`local_session.py`; secret regenerated each start, never persisted). A default-deny `before_request` gate requires that cookie OR the Bearer token on **every mutating `/api/*` call outside `/api/discovery/*`** — any future mutating endpoint is covered automatically. Remote viewers on a LAN bind can read GET dashboards but cannot mutate.

5. **Single-use session bootstrap** (#31) — `GET /` alone does **not** issue the cookie: a local process running as a *different* OS user could otherwise fetch the page and obtain one. On boot the server writes a bootstrap key to `~/.c3/oracle/bootstrap.key` (owner-only; home-directory ACLs are the gate, the same assumption `~/.c3/secrets.enc` already makes). `c3 oracle open` reads that key, calls `POST /api/session/bootstrap`, and opens `GET /?bootstrap=<code>`; redeeming the code sets the cookie and redirects to a clean `/` so it never lingers in history or a `Referer`. Codes are single-use with a 120s TTL. `/api/session/bootstrap` is deliberately exempt from the write gate — it is how a browser *acquires* the cookie, so it cannot require one; it runs its own loopback + key/Bearer check instead. `c3 oracle serve` auto-opens an already-signed-in URL, so the common case is unchanged.

## Read-Only Contract

Oracle **never** writes to any project's `.c3/` directory unless the user explicitly clicks "Approve" on a suggestion in the UI. This guarantees:

1. C3 sessions are never disrupted by Oracle activity
2. No race conditions between concurrent C3 and Oracle writes
3. C3 works identically whether Oracle is running or not

## Import Dependencies

Oracle imports from C3:
- `services.memory_scorer.MemoryScorer` — Used by `MemoryReader` for tier computation
- `services.project_runtime.ProjectRuntimeCache` + `services.runtime.C3Runtime` — Used by `C3Bridge` for the per-project runtime cache behind the `c3_*` tools
- `services.subprojects` (config reader) — Used by `FederatedGraph` for the project-hierarchy overlay
- `core.web_security` — Host-header/Origin guard shared with the hub

Oracle does **not** import `services.ollama_client.OllamaClient`. The `OllamaBridge` is fully self-contained with its own HTTP client and cache.

No C3 code imports from Oracle. The dependency is strictly one-directional.

## Hub Integration

Two small, non-breaking changes to existing C3 code:

1. **`cli/hub_server.py`**: Added `oracle_url` field to `_HUB_CONFIG_DEFAULTS` and its POST handler. ~5 lines.
2. **`cli/hub.html`**: JS in the init block fetches `oracle_url` from hub config (default `http://localhost:3331`), hits Oracle's `/api/health`, and if it responds with `{"service": "c3-oracle"}`, shows an "Oracle" link in the hub header. ~8 lines.

## Web UI

The UI is a **concat bundle**: an HTML shell (`oracle/oracle_ui.html`) plus 18 JS modules in `oracle/ui/` (`core`, `busy`, `theme_tabs`, `crossgraph`, `header`, `projects`, `insights`, `activity`, `suggestions`, `settings`, `agents`, `chat/markdown`, `chat/conversations`, `chat/stream_renderer`, `chat/toolbar`, `chat/input`, `chat/send`, with `app.js` — the init IIFE — always last). `_build_oracle_html()` concatenates them server-side into one response, cached until restart; the modules share one script scope, so load order matters. `GET /` serves the bundle. (The frozen pre-bundle `oracle.html` was served at `GET /legacy` for one release as an escape hatch and has since been removed.)

Still vanilla JS with inline CSS — deliberately **no framework and no build step** (unlike the React-based C3 hub).

**8 tabs**: Chat (default), Projects, Insights, Activity, Cross-Graph, Suggestions, Team/Agents, Settings

**Notification system**: Toast notifications (bottom-right corner, 4 types: success/error/info/warning) shown on all user actions.

**Activity indicator**: Header spinner + indeterminate progress bar below tabs, shown during async operations. Ref-counted to handle overlapping requests.

**Theme**: Dark/light mode using CSS variables, matching C3 hub's design system.
