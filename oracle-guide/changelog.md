# Oracle Changelog

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
