# Oracle — Cross-Project Memory Agent for C3

Oracle is an independent AI-powered memory management agent for the C3 ecosystem. It uses the Ollama cloud API (default model: `gemma4:31b-cloud`) to analyze, validate, and connect memory across all your C3 projects.

## Quick Start

```bash
# Set your Ollama cloud API key
export OLLAMA_API_KEY=your-key-here

# Launch Oracle via the C3 CLI (v2.49.0+; alias: c3 oracle start)
c3 oracle serve

# Or with options
c3 oracle serve --port 3333 --no-browser

# From a source checkout, the direct entry point still works
python oracle/oracle_server.py [--port N] [--no-browser]
```

Open `http://localhost:3331` in your browser.

## Key Concepts

- **Independent system**: Oracle runs as its own Flask server (default port 3331). C3 works identically whether Oracle is running or not.
- **Ollama cloud**: Uses `https://ollama.com` with Bearer token auth by default. Can also target a local Ollama instance.
- **Hub-aware**: Discovers projects through the C3 hub API or falls back to reading `~/.c3/projects.json` directly.
- **Read + suggest-write**: Oracle reads project memory freely. Writes to project `.c3/facts/` require explicit user approval in the UI.
- **Interactive chat**: a Chat tab (the default view) streams conversations with Oracle over SSE, with a tool-calling loop over the same tool registry the Discovery API exposes — native Ollama tool calling where the model supports it, text-protocol fallback otherwise.
- **Team / Agents**: a configurable roster of specialist agents (Architect, Code Explorer, Memory Analyst by default) that chat can delegate sub-tasks to via `delegate_task`. Agents run on Ollama or, per-agent, on a CLI backend (codex/gemini/claude/auto) in read-only mode.
- **Federated graph**: a Cross-Graph tab visualizes one memory graph across all projects, with embedding/TF-IDF similarity edges linking related facts between projects.
- **Scheduled digest**: opt-in (`digest_enabled`) daily cross-project activity digest written by the background review loop and served at `/api/activity/digest/latest`.
- **Discovery API (v2.32.0)**: External LLMs (Claude Code/Desktop or any function-calling model) can use Oracle's tools over MCP (`:3332/mcp`) and OpenAPI REST (`/api/discovery`) — Bearer-auth'd, loopback-bound, read + safe-action tiers only. See [Discovery API](discovery-api.md).
- **Cross-project memory**: Maintains a global insight store (`~/.c3/oracle/cross_memory.json`) linking patterns, risks, and opportunities across projects.
- **Background review**: A daemon thread periodically scans projects for changes, runs health checks, and generates consolidation suggestions.
- **Notifications & activity**: The UI shows toast notifications for all actions and a global busy indicator when Oracle is processing.

## Documentation

| Guide | Description |
|-------|-------------|
| [Architecture](architecture.md) | System design, data flow, folder structure, service diagram |
| [API Reference](api-reference.md) | All REST endpoints with request/response examples |
| [Discovery API](discovery-api.md) | Expose C3's tools to external LLMs over MCP + OpenAPI (v2.32.0) |
| [Configuration](configuration.md) | Config options, authentication, model selection |
| [Changelog](changelog.md) | Version history and changes |

## Requirements

- Python 3.10+
- Flask (already a C3 dependency in `pyproject.toml`)
- Ollama cloud API key (set `OLLAMA_API_KEY` env var or configure in UI)
- Optionally: local Ollama instance (if using local models instead of cloud)
