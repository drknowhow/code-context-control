# Oracle Configuration

Oracle stores its configuration at `~/.c3/oracle/config.json`.

## Config Options

| Key | Default | Description |
|-----|---------|-------------|
| `port` | `3331` | Flask server port |
| `bind_host` | `127.0.0.1` | Interface to bind, loopback only by default (also used by the MCP transport) |
| `api_enabled` | `true` | Serve the `/api/discovery/*` REST surface |
| `api_require_auth` | `true` | Require the Bearer token on `/api/discovery/*` |
| `api_max_tier` | `action` | Cap exposed discovery tools: `read` or `action` |
| `mcp_enabled` | `true` | Start the FastMCP HTTP/SSE discovery server |
| `mcp_port` | `3332` | MCP transport port (loopback) |
| `ollama_base_url` | `https://ollama.com` | Ollama cloud API endpoint |
| `ollama_api_key` | `""` | Ollama cloud API key (or set `OLLAMA_API_KEY` env var) |
| `llm_cache_ttl_sec` | `86400` | Disk-cache TTL for LLM `generate()` responses |
| `model` | `gemma4:31b-cloud` | Default LLM model for analysis and chat |
| `hub_url` | `http://localhost:3330` | C3 Hub URL for project discovery |
| `scanner_ttl_seconds` | `20` | Project-discovery cache TTL (the dashboard's explicit Scan bypasses it) |
| `review_interval_seconds` | `1800` | Background review cycle interval (30 min) |
| `review_enabled` | `true` | Enable/disable background review agent |
| `digest_enabled` | `false` | Scheduled activity digest inside the review loop (off = on-demand only) |
| `digest_interval_seconds` | `86400` | Digest cadence once enabled (daily) |
| `digest_narrate` | `false` | Add an LLM prose summary to scheduled digests (costs a cloud call; opt-in) |
| `digest_notify_file` | `""` | JSONL sink appended after each scheduled digest (`""` = disabled) |
| `digest_retention_days` | `14` | Prune stored digests older than this |
| `auto_open_browser` | `true` | Open browser on startup |
| `theme` | `dark` | UI theme (`dark` or `light`) |
| `max_facts_per_analysis` | `100` | Max facts sent to LLM per analysis |
| `insight_confidence_threshold` | `0.5` | Minimum confidence to store an insight |
| `log_level` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `federated_graph_ttl_sec` | `3600` | Federated-graph build cache TTL |
| `cross_sim_threshold` | `0.75` | Minimum similarity for cross-project `cross_similar` edges |
| `cross_max_facts_per_project` | `200` | Cap on facts per project fed into the federated graph |
| `cross_top_k_neighbors` | `3` | Cross-project neighbors kept per fact |
| `embedding_model` | `nomic-embed-text` | Ollama embedding model for graph similarity (TF-IDF fallback if unavailable) |
| `agents` | 3 defaults | Chat/delegation agent roster — see [Agents](#agents) below |

`allowed_hosts` (list, no default entry) may also be set — extra hostnames/IPs accepted
by the Host-header/Origin guard when `bind_host` is non-loopback. See
[discovery-api.md](discovery-api.md#configuration).

## Agents

The `agents` key holds the Team/Agents roster used by the chat UI and the
`delegate_task` tool. It ships with three defaults (Architect, Code Explorer,
Memory Analyst). Each agent is an object:

```json
{
  "id": "architect",
  "name": "Architect",
  "description": "Expert in system architecture, design patterns, ...",
  "system_prompt": "You are the Architect. Focus on structural integrity, ...",
  "model": "gemma4:31b-cloud",
  "backend": "ollama",
  "active": true
}
```

| Field | Meaning |
|-------|---------|
| `id` | Unique ID — what `delegate_task` takes as `agent_id` |
| `name` / `description` | Shown in the Team/Agents tab and `/team` |
| `system_prompt` | The agent's persona/instructions |
| `model` | Ollama model used when `backend` is `ollama` |
| `backend` | `ollama` (default) \| `codex` \| `gemini` \| `claude` \| `auto` (v2.48.0). CLI backends run via `c3_delegate` inside a registered project's workspace, read-only, and require `delegate_task`'s `project_path` |
| `active` | Inactive agents cannot be delegated to |

## Authentication

Oracle uses the Ollama cloud API at `https://ollama.com`. You need an API key:

**Option 1 — Environment variable** (recommended):
```bash
export OLLAMA_API_KEY=your-key-here
python oracle/oracle_server.py
```

**Option 2 — Config file**:
```json
{
  "ollama_api_key": "your-key-here"
}
```

**Option 3 — UI**: Settings tab > Ollama API Key field > Save

The API key is sent as `Authorization: Bearer <key>` on all Ollama API calls.
The config GET endpoint masks the key (shows first 4 chars only).

**Priority**: Explicit config value > `OLLAMA_API_KEY` env var.

## Local Ollama Fallback

If you want to use a local Ollama instance instead of cloud, set:

```json
{
  "ollama_base_url": "http://localhost:11434",
  "ollama_api_key": ""
}
```

Without an API key, requests are sent without auth headers (compatible with local Ollama).

## Changing the Model

You can change the model via:

1. **UI**: Settings tab > Model dropdown > Save
2. **API**: `POST /api/config` with `{"model": "new-model-name"}`
3. **File**: Edit `~/.c3/oracle/config.json` directly

The model dropdown in the UI is populated from the Ollama `/api/tags` endpoint. If the configured model isn't in the returned list, it appears with a "(not pulled)" suffix.

## Hot Reload

These config fields take effect immediately when changed via `POST /api/config` without restarting Oracle:

| Field | Effect |
|-------|--------|
| `model` | Updates `OllamaBridge.model` — next LLM call uses the new model |
| `ollama_api_key` | Updates `OllamaBridge.api_key` — next request uses the new key |
| `ollama_base_url` | Updates `OllamaBridge.base_url` — next request targets the new endpoint |
| `theme` | Applied in UI on save (CSS variable swap) |

The scheduled-digest keys (`digest_*`) are also read live by the review loop each
cycle, so toggling them needs no restart. Other fields (port, review interval, etc.)
require a restart to take effect.

## Hub Integration

To make the C3 Hub show an Oracle link, either:

1. Oracle auto-detects on `http://localhost:3331` (default)
2. Or set `oracle_url` in hub config: `~/.c3/hub_config.json`

```json
{
  "oracle_url": "http://localhost:3331"
}
```

## Environment

Oracle reads from these locations:

| Path | Purpose |
|------|---------|
| `~/.c3/oracle/` | All Oracle state (config, insights, cache) |
| `~/.c3/projects.json` | Project registry (fallback discovery) |
| `<project>/.c3/facts/` | Per-project memory (read-only by default) |
