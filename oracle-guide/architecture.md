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
  oracle_server.py         # Flask app + entry point (404 lines)
  oracle.html              # Web UI dashboard with toast notifications (673 lines)
  config.py                # Config loader + defaults
  services/
    __init__.py
    ollama_bridge.py       # Self-contained Ollama cloud client with Bearer auth + cache
    project_scanner.py     # Discovers projects via hub API or ~/.c3/projects.json
    memory_reader.py       # Read-only access to .c3/facts/ + MemoryScorer tier computation
    memory_writer.py       # Suggestion queue + approved write-backs to project facts
    cross_memory.py        # Cross-project insight store with dedup + project linking
    health_checker.py      # Validates .c3/ structure + fact/graph integrity (no LLM)
    insight_engine.py      # LLM-powered analysis: per-project + cross-project + consolidation
    review_agent.py        # Background daemon thread with configurable review interval
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
| `cache/llm/<md5>.json` | LLM response cache (keyed by model+prompt+options) |
| `oracle.log` | Background agent + server log |

## Service Descriptions

### OllamaBridge (`ollama_bridge.py`)
Self-contained HTTP client for the Ollama API. Does **not** depend on C3's `OllamaClient` — it uses `urllib.request` directly with `Authorization: Bearer` headers. Supports both cloud (`https://ollama.com`) and local (`http://localhost:11434`) endpoints. API key priority: explicit config > `OLLAMA_API_KEY` env var. Includes its own disk-based LLM response cache.

### ProjectScanner (`project_scanner.py`)
Discovers registered C3 projects. Primary: HTTP GET to hub `/api/projects` (2s timeout). Fallback: reads `~/.c3/projects.json` directly. Enriches each project with `.c3/` presence, fact count, and `facts.json` mtime.

### MemoryReader (`memory_reader.py`)
Read-only access to a project's `.c3/facts/` directory. Reads `facts.json`, `memory_graph.json`, and `session_fingerprints.json`. Computes summary statistics (fact count by category/lifecycle) and tier distribution using C3's `MemoryScorer` (the one import from C3's `services/`).

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
Background daemon thread (default: 30 min interval). Each cycle: discover projects → detect changes (facts.json mtime) → run health checks → cache reports → generate cross-project insights (if 2+ projects changed) → auto-suggest consolidation for projects with 30+ facts. State persisted in `review_state.json`.

## Read-Only Contract

Oracle **never** writes to any project's `.c3/` directory unless the user explicitly clicks "Approve" on a suggestion in the UI. This guarantees:

1. C3 sessions are never disrupted by Oracle activity
2. No race conditions between concurrent C3 and Oracle writes
3. C3 works identically whether Oracle is running or not

## Import Dependencies

Oracle imports from C3:
- `services.memory_scorer.MemoryScorer` — Used by `MemoryReader` for tier computation

Oracle does **not** import `services.ollama_client.OllamaClient`. The `OllamaBridge` is fully self-contained with its own HTTP client and cache.

No C3 code imports from Oracle. The dependency is strictly one-directional.

## Hub Integration

Two small, non-breaking changes to existing C3 code:

1. **`cli/hub_server.py`**: Added `oracle_url` field to `_HUB_CONFIG_DEFAULTS` and its POST handler. ~5 lines.
2. **`cli/hub.html`**: JS in the init block fetches `oracle_url` from hub config (default `http://localhost:3331`), hits Oracle's `/api/health`, and if it responds with `{"service": "c3-oracle"}`, shows an "Oracle" link in the hub header. ~8 lines.

## Web UI

Single HTML file (`oracle.html`, 673 lines) with inline CSS + vanilla JS. No build step, no framework dependencies.

**4 tabs**: Projects, Insights, Suggestions, Settings

**Notification system**: Toast notifications (bottom-right corner, 4 types: success/error/info/warning) shown on all user actions.

**Activity indicator**: Header spinner + indeterminate progress bar below tabs, shown during async operations. Ref-counted to handle overlapping requests.

**Theme**: Dark/light mode using CSS variables, matching C3 hub's design system.
