# Oracle Configuration

Oracle stores its configuration at `~/.c3/oracle/config.json`.

## Config Options

| Key | Default | Description |
|-----|---------|-------------|
| `port` | `3331` | Flask server port |
| `ollama_base_url` | `https://ollama.com` | Ollama cloud API endpoint |
| `ollama_api_key` | `""` | Ollama cloud API key (or set `OLLAMA_API_KEY` env var) |
| `model` | `gemma4:31b-cloud` | Default LLM model for analysis |
| `hub_url` | `http://localhost:3330` | C3 Hub URL for project discovery |
| `review_interval_seconds` | `1800` | Background review cycle interval (30 min) |
| `review_enabled` | `true` | Enable/disable background review agent |
| `auto_open_browser` | `true` | Open browser on startup |
| `theme` | `dark` | UI theme (`dark` or `light`) |
| `max_facts_per_analysis` | `100` | Max facts sent to LLM per analysis |
| `insight_confidence_threshold` | `0.5` | Minimum confidence to store an insight |
| `log_level` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

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

Other fields (port, review interval, etc.) require a restart to take effect.

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
