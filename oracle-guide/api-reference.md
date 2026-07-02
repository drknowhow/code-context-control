# Oracle API Reference

Base URL: `http://localhost:3331` (configurable via `port` in config)

All endpoints return JSON. POST endpoints accept JSON bodies with `Content-Type: application/json`.

> **Auth (v2.47.0)**: ALL mutating (POST/PUT/DELETE) endpoints outside `/api/discovery/*`
> require either the dashboard session cookie (a per-boot `HttpOnly` cookie issued on
> `GET /` to loopback browsers) or the Discovery Bearer token
> (`Authorization: Bearer <token>`). Unauthenticated mutations get `401`. `GET` reads
> are open to allowed local origins; `/api/discovery/*` is Bearer-only (see below).

---

## Health & Config

### `GET /api/health`

Returns Oracle status and Ollama cloud availability.

```json
{
  "status": "ok",
  "service": "c3-oracle",
  "model": "gemma4:31b-cloud",
  "ollama_available": true,
  "model_verified": true,
  "hub_available": true
}
```

### `GET /api/config`

Returns current Oracle configuration. API key is masked (first 4 chars + `....`).

```json
{
  "port": 3331,
  "ollama_base_url": "https://ollama.com",
  "ollama_api_key": "sk-1.....",
  "model": "gemma4:31b-cloud",
  "hub_url": "http://localhost:3330",
  "review_interval_seconds": 1800,
  "review_enabled": true,
  "auto_open_browser": true,
  "theme": "dark",
  "max_facts_per_analysis": 100,
  "insight_confidence_threshold": 0.5,
  "log_level": "INFO"
}
```

### `POST /api/config`

Update configuration. Send a partial config object — only included keys are updated.

Hot-reloads: `model`, `ollama_api_key`, and `ollama_base_url` take effect immediately on the running OllamaBridge instance.

```json
// Request
{ "model": "gemma4:31b-cloud", "review_interval_seconds": 900 }

// Response
{ "saved": true, "config": { ... } }
```

---

## Projects

### `GET /api/projects`

List all discovered C3 projects with memory stats and cached health status.

Discovery order: Hub API (`/api/projects`) → fallback to `~/.c3/projects.json`.

```json
[
  {
    "path": "/path/to/project",
    "name": "my-project",
    "has_c3": true,
    "has_facts": true,
    "fact_count": 47,
    "facts_mtime": 1712700000.0,
    "health_status": "ok",
    "health_issues": 0
  }
]
```

### `POST /api/projects/scan`

Force re-discovery of all projects. Returns fresh scan results.

```json
// Response
{ "scanned": 3, "projects": [ ... ] }
```

### `POST /api/projects/review`

Trigger health check for a project (synchronous) and LLM analysis (background).

```json
// Request
{ "path": "/path/to/project" }

// Response — health report
{
  "project_path": "/path/to/project",
  "status": "warning",
  "structure_ok": true,
  "fact_stats": {
    "total": 47,
    "by_category": { "general": 20, "architecture": 15, "convention": 12 },
    "by_tier": { "core": 8, "active": 25, "dormant": 10, "ephemeral": 4 },
    "by_lifecycle": { "active": 43, "archived": 4 }
  },
  "graph_stats": {
    "total_edges": 34,
    "total_nodes": 28,
    "edge_types": { "co_recalled": 20, "touches": 14 },
    "orphaned_edges": 2
  },
  "freshness": {
    "last_fact_timestamp": "2026-04-08T14:30:00+00:00",
    "days_since_last_fact": 2
  },
  "issues": [
    { "severity": "warning", "message": "2 orphaned graph edges (reference deleted facts)" }
  ]
}
```

### `GET /api/projects/health?path=...`

Get health report for a project. Returns cached report from the review agent if available, otherwise runs a fresh check.

### `GET /api/projects/facts?path=...&limit=50`

Get fact statistics and top facts (sorted by relevance_count descending).

```json
{
  "stats": { "total": 47, "by_category": { ... }, "by_tier": { ... }, "by_lifecycle": { ... } },
  "facts": [
    {
      "id": "a1b2c3d4e5f6",
      "fact": "MCP server and Flask REST server have separate instances",
      "category": "architecture",
      "relevance_count": 12,
      "confidence": 1.0,
      "lifecycle": "active"
    }
  ]
}
```

### `GET /api/projects/graph?path=...`

Get memory graph statistics for a project.

```json
{
  "total_edges": 34,
  "total_nodes": 28,
  "edge_types": { "co_recalled": 20, "touches": 14 },
  "orphaned_edges": 2
}
```

---

## Insights

### `GET /api/insights`

List all cross-project insights (excluding dismissed), stats, and project links.

```json
{
  "insights": [
    {
      "id": "ins_a1b2c3d4e5f6",
      "type": "pattern",
      "text": "Both projects use Flask + CORS with identical setup patterns",
      "source_projects": ["/path/a", "/path/b"],
      "source_fact_ids": {},
      "confidence": 0.85,
      "created_at": "2026-04-10T12:00:00+00:00",
      "last_reviewed": "2026-04-10T12:00:00+00:00",
      "dismissed": false,
      "tags": ["architecture"]
    }
  ],
  "stats": {
    "total_insights": 5,
    "by_type": { "pattern": 3, "risk": 1, "opportunity": 1 },
    "total_links": 2
  },
  "links": [
    { "src": "/path/a", "dst": "/path/b", "link_type": "pattern", "strength": 3, "insight_ids": ["ins_..."] }
  ]
}
```

**Insight types**: `pattern`, `dependency`, `convention`, `risk`, `opportunity`, `drift`

### `GET /api/insights/project?path=...`

Get insights involving a specific project.

### `POST /api/insights/generate`

Force LLM to generate new cross-project insights. Requires at least 2 projects with facts.

```json
// Response
{ "generated": 3, "insights": [ ... ] }

// Error (fewer than 2 projects)
{ "error": "Need at least 2 projects with facts", "available": 1 }
```

### `POST /api/insights/dismiss`

Dismiss an insight (marks `dismissed: true`, excluded from future listings).

```json
// Request
{ "id": "ins_a1b2c3d4e5f6" }

// Response
{ "dismissed": true, "id": "ins_a1b2c3d4e5f6" }
```

### `POST /api/insights/cross`

On-demand cross-project insight generation from the federated graph. Requires at least
2 projects with facts (defaults to all discovered projects with facts; override with
`projects`).

```json
// Request (optional body)
{ "projects": ["/path/a", "/path/b"] }

// Response
{ "generated": 3, "insights": [ ... ], "graph_stats": { ... } }
```

---

## Federated Graph

Cross-project memory graph: per-project fact graphs merged, plus `cross_similar`
edges between facts of different projects (embeddings with TF-IDF fallback). Builds
are cached (`federated_graph_ttl_sec` + facts mtimes); the project hierarchy overlay
is recomputed per serve.

### `GET /api/graph/federated`

**Query params:** `projects` (comma-separated paths; default = all discovered projects
with facts), `min_sim` (similarity threshold override), `top_k` (neighbors per fact),
`force=1` (bypass cache).

Returns `nodes`, `edges`, `projects` (each with `parent_slug`), `stats` (including
`parent_child`), and `hierarchy` (parent/child project links).

### `GET /api/graph/federated/node/<id>`

One node's fact plus its cross-project neighbors.

```json
{ "node": { ... }, "neighbors": [ { "id": "...", "type": "cross_similar",
  "scope": "cross", "weight": 0.82, "label": "...", "project": "..." } ] }
```

### `POST /api/graph/federated/rebuild`

Invalidate the cache and force a rebuild. Optional body `{ "projects": [ ... ] }`.

### `GET /api/graph/federated/stats`

Graph stats + project list only (no nodes/edges).

---

## Activity

### `GET /api/activity/digest`

Cross-project activity digest for a day (or custom window). Aggregates sessions, tool
calls, edits, git mutations, and token/cost across all registered projects (or one, via
`project`).

**Query params:** `date` (UTC `YYYY-MM-DD`, default today); `since` / `until` (ISO bounds,
override `date`); `project` (single-project path); `narrate` (`true` adds an LLM prose
summary — best-effort, omitted on failure).

```json
{
  "window": { "since": "2026-06-14T00:00:00", "until": "2026-06-14T23:59:59.999999",
              "label": "2026-06-14 (today), UTC", "tz": "UTC" },
  "totals": { "projects_active": 2, "sessions": 3, "tool_calls": 184, "edits": 57,
              "git_mutations": 12, "decisions": 4,
              "input_tokens": 120000, "output_tokens": 38000, "cost_usd": 1.42 },
  "projects": [
    { "name": "claude-companion", "path": "/path/to/proj", "sessions": [ ... ],
      "tool_calls": 150, "edits": 57, "git_mutations": 12,
      "tokens": { "input": 100000, "output": 30000 }, "cost_usd": 1.10,
      "first_activity": "2026-06-14T09:00:00+00:00", "last_activity": "2026-06-14T18:30:00+00:00" }
  ],
  "narrative": null
}
```

> Also available as the discovery tool **`activity_report`** (identical payload) for
> external LLMs over MCP, OpenAPI, and `POST /api/discovery/call`.

### `GET /api/activity/digest/latest`

Most recent **scheduled** digest, written by the review loop when `digest_enabled` is
set (serves `~/.c3/oracle/activity_digests/latest.json`). On-demand digests from
`/api/activity/digest` are not persisted here.

```json
{ "generated_at": "2026-07-02T00:00:12+00:00", "digest": { ... } }

// No scheduled digest yet
{ "digest": null, "generated_at": null }
```

---

## Suggestions

Suggestions are write-back recommendations that Oracle generates. They modify project `.c3/facts/facts.json` only when explicitly approved by the user.

**Suggestion types**: `merge_facts`, `archive_facts`, `add_fact`

### `GET /api/suggestions?path=...`

List pending suggestions. Optional `path` query param filters by project.

```json
[
  {
    "id": "sug_a1b2c3d4e5f6",
    "project_path": "/path/to/project",
    "type": "merge_facts",
    "data": { "survivor_id": "abc123", "merge_ids": ["abc123", "def456"], "merged_text": "..." },
    "status": "pending",
    "created_at": "2026-04-10T12:00:00+00:00",
    "resolved_at": null
  }
]
```

### `POST /api/suggestions/approve`

Approve and execute a suggestion. This writes to the target project's `facts.json`.

```json
// Request
{ "id": "sug_a1b2c3d4e5f6" }

// Response
{ "approved": true, "id": "sug_...", "result": { "merged": 1, "survivor_id": "abc123" } }
```

### `POST /api/suggestions/dismiss`

Dismiss a suggestion without executing it.

```json
// Request
{ "id": "sug_a1b2c3d4e5f6" }

// Response
{ "dismissed": true, "id": "sug_a1b2c3d4e5f6" }
```

---

## Review Agent

The review agent is a background daemon thread that periodically scans projects, runs health checks, and generates insights/suggestions.

### `GET /api/review/status`

```json
{
  "running": true,
  "last_run": "2026-04-10T12:00:00+00:00",
  "interval_seconds": 1800,
  "projects_tracked": 3
}
```

### `POST /api/review/start`

Start the background review agent.

### `POST /api/review/stop`

Stop the background review agent.

### `POST /api/review/run-now`

Trigger one immediate review cycle in a background thread (non-blocking).

---

## Ollama

### `GET /api/ollama/status`

Check Ollama cloud availability and list accessible models.

```json
{
  "available": true,
  "models": ["gemma4:31b-cloud", "llama3:8b", "nomic-embed-text"],
  "current_model": "gemma4:31b-cloud",
  "has_model": true
}
```

### `POST /api/ollama/test`

Test generation with the current model. Sends a simple prompt and returns the response.

```json
// Response
{ "response": "Oracle is now online." }
```

---

## Chat

Interactive chat with Oracle (the dashboard's Chat tab). Conversations are persisted
in `~/.c3/oracle/conversations/`.

### `POST /api/chat`

Streaming chat over SSE (`text/event-stream`). Each event is a JSON object on a
`data:` line with a `type` field; the stream ends with a literal `data: [DONE]`.

```json
// Request
{ "message": "what changed across my projects today?", "conversation_id": "c_..." }
```

**Event vocabulary** (documented once here; the same shapes appear in the agent
sub-stream):

| Type | Meaning |
|------|---------|
| `meta` | First event: `conv_id`, `model`, conversation `state` |
| `status` | Progress updates (`message` + `detail`) |
| `thinking` | Streamed model reasoning chunks |
| `text` | Streamed visible answer chunks |
| `tool_call` | Oracle invoking a tool (`name`, `args`, `tool_id`) |
| `tool_result` | Tool output (`tool_id`, result) |
| `agent_start` / `agent_round` / `agent_thinking` / `agent_text` / `agent_tool_call` / `agent_tool_result` / `agent_done` | Sub-stream while `delegate_task` runs a roster agent |
| `done` | Turn complete (timing/stats) |
| `error` | Terminal error for the turn |

Tool calling is native Ollama tool calling when the model supports it (probed via
`/api/show`), with automatic mid-turn fallback to the legacy `<tool_call>` text
protocol if the model rejects native tools.

### `GET /api/chat/conversations?limit=50`

List conversations (most recent first).

### `POST /api/chat/conversations`

Create a conversation. Body `{ "title": "..." }` (optional). Returns `201` with `{ "id": "c_..." }`.

### `GET /api/chat/conversations/<id>`

Full message history: `{ "conversation_id": "...", "messages": [ ... ] }`.

### `DELETE /api/chat/conversations/<id>`

Delete a conversation. Returns `{ "ok": true }`.

### `PUT /api/chat/conversations/<id>/title`

Rename. Body `{ "title": "..." }`.

### `GET /api/chat/conversations/<id>/state`

Per-conversation state (focused projects, model, depth): `{ "state": { ... } }`.

### `GET /api/chat/commands`

Slash-command registry for frontend autocomplete: `{ "commands": [ ... ] }`.

### `POST /api/chat/command`

Execute a slash command (e.g. `/project`, `/model`, `/depth`, `/team`).

```json
// Request
{ "conversation_id": "c_...", "command": "/depth deep" }
```

---

## Discovery API key (`/api/apikey`)

Dashboard-facing management of the Discovery Bearer token (stored in the OS keyring).
The POST mutations are covered by the v2.47.0 write gate: they require the session
cookie or the Bearer token.

### `GET /api/apikey`

Token status + connection info (`exists`, `masked`, `mcp_url`, `rest_base`,
`openapi_url`, a ready-to-paste `.mcp.json` `snippet`, and the `api_enabled` /
`api_require_auth` / `mcp_enabled` flags). The **unmasked** `key` is included only
when the caller presents a valid Bearer token or the dashboard session cookie;
otherwise `key` is empty and the snippet carries a `<token>` placeholder.

### `POST /api/apikey/generate`

Create a token if none exists. Reveals the key in the response (explicit local
creation action, shown once for copy).

### `POST /api/apikey/rotate`

Replace the token with a fresh one (revealed once for copy).

### `POST /api/apikey/clear`

Delete the stored token.

---

## Discovery API (external LLM tool surface)

> Added in v2.32.0. Lets Claude or any function-calling LLM use C3's cross-project
> code & memory intelligence as tools. **Bearer-token auth required**; the Oracle
> binds `127.0.0.1` by default.

Get your token + connection snippet with `c3 oracle api info` (or `c3 oracle api key`
for just the token). All `/api/discovery/*` requests require
`Authorization: Bearer <token>`.

### `GET /api/discovery/tools`

Returns the available tools, each with a JSON Schema and capability tier.

```json
{ "tier": "action",
  "tools": [ { "name": "c3_search_cross", "tier": "read",
               "description": "...", "parameters": { "type": "object" } } ] }
```

### `POST /api/discovery/call`

Invoke any tool by name.

```json
// Request
{ "tool": "list_projects", "args": {} }
```

### `POST /api/discovery/tools/<name>`

Invoke a named tool with the request body as its arguments object (one operation
per tool — matches the generated OpenAPI paths).

```json
// POST /api/discovery/tools/c3_search_cross
{ "query": "rate limiter", "top_k": 5 }
```

### `POST /api/discovery/call/stream`

Same as `/call`, but streams `start` → `result`|`error` → `[DONE]` as SSE
(`text/event-stream`).

### `GET /api/discovery/openapi.json`

OpenAPI 3.1 document describing every available tool as a
`POST /api/discovery/tools/{name}` operation. Point a generic function-calling LLM
at this URL to auto-register the tools.

### `GET /api/discovery/mcp-info`

Connection details for the MCP transport.

```json
{ "enabled": true, "transport": "http",
  "url": "http://127.0.0.1:3332/mcp", "auth": "bearer",
  "rest_base": "http://127.0.0.1:3331/api/discovery" }
```

### Capability tiers

- **read** — discovery only: `list_projects`, `search_facts`, `query_memory`,
  `project_health`, `analyze_project`, `cross_insights`, `read_graph`,
  `activity_report`, `c3_search`, `c3_search_cross`, `c3_read`, `c3_compress`,
  `c3_validate`, `c3_status`, `c3_memory_query`, `c3_edits`, `c3_edits_cross`,
  `c3_project`, `c3_artifacts` (the latter two are read-only by construction:
  write verbs are blocked).
- **action** — adds safe, non-code-edit writes: `suggest_action` (creates a
  *pending* suggestion for human approval) and `delegate_task` (optional
  `project_path`, required for CLI-backed agents).
- Code-editing tools are **never** exposed. Cap the tier via `api_max_tier`
  (`read` | `action`) in `~/.c3/oracle/config.json`.

---

## MCP transport

The same tools are served over MCP streamable-HTTP at `http://127.0.0.1:3332/mcp`
(port = `mcp_port`). Every request requires `Authorization: Bearer <token>`. See
[discovery-api.md](discovery-api.md) for a Claude `.mcp.json` example.

---

## Error Responses

All endpoints return errors as JSON with appropriate HTTP status codes:

```json
// 400 Bad Request
{ "error": "path required" }

// 404 Not Found
{ "error": "not found" }

// 500 Internal Server Error
{ "error": "not initialized" }
```
