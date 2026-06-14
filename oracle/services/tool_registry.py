"""Canonical catalog of Oracle Discovery tools — the single source of truth.

``TOOL_SPECS`` defines every tool exposed to external LLMs: its name, capability
tier, human/LLM-facing description, and a real JSON Schema for its parameters.
Both transports consume this list:

* the REST surface (``oracle_server.py``) serves the catalog + a generated
  OpenAPI document and dispatches calls, and
* the MCP server (``oracle/mcp_oracle.py``) registers one MCP tool per spec.

The schemas mirror the tools dispatched by ``ChatEngine._execute_tool`` (and the
``C3Bridge`` methods behind ``_dispatch_c3``). ``c3_filter`` is intentionally
absent — it is not wired into the chat dispatcher, so it is not callable here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Capability tiers, lowest privilege first. ``api_max_tier`` (Oracle config)
# caps which tiers are exposed.
TIER_READ = "read"
TIER_ACTION = "action"
_TIER_RANK = {TIER_READ: 0, TIER_ACTION: 1}

# Reusable schema fragments.
_PROJECT_PATH = {"type": "string", "description": "Absolute path to the target C3 project."}


def _c3_version() -> str:
    """Read __version__ from cli/c3.py without importing it (heavy side effects)."""
    try:
        c3_py = Path(__file__).resolve().parents[2] / "cli" / "c3.py"
        for line in c3_py.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split('"')[1]
    except Exception:
        pass
    return "0"


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# ── The catalog ───────────────────────────────────────────
# Each entry: {name, tier, description, parameters(JSON Schema)}.
TOOL_SPECS: list[dict[str, Any]] = [
    # ── read tier: cross-project discovery ──
    {
        "name": "list_projects",
        "tier": TIER_READ,
        "description": "List all registered C3 projects with paths and fact counts. Start here to discover what is available.",
        "parameters": _obj({}),
    },
    {
        "name": "search_facts",
        "tier": TIER_READ,
        "description": "Search durable memory facts across ALL projects. Returns matches with their source project.",
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "Keyword query (space-separated terms)."},
                "limit": {"type": "integer", "default": 20, "description": "Max results."},
            },
            ["query"],
        ),
    },
    {
        "name": "query_memory",
        "tier": TIER_READ,
        "description": "Search or list durable memory facts within ONE project.",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "query": {"type": "string", "default": "", "description": "Optional keyword filter on fact text."},
                "category": {"type": "string", "default": "", "description": "Optional category filter."},
                "limit": {"type": "integer", "default": 10, "description": "Max results."},
            },
            ["project_path"],
        ),
    },
    {
        "name": "project_health",
        "tier": TIER_READ,
        "description": "Run a health check on a project's memory: status, issues, and stats.",
        "parameters": _obj({"project_path": _PROJECT_PATH}, ["project_path"]),
    },
    {
        "name": "analyze_project",
        "tier": TIER_READ,
        "description": "Deep LLM-powered analysis of a project's memory patterns and themes (uses the local Oracle model).",
        "parameters": _obj({"project_path": _PROJECT_PATH}, ["project_path"]),
    },
    {
        "name": "cross_insights",
        "tier": TIER_READ,
        "description": "Get cross-project insights. Omit project_path for all insights, or pass it to filter to one project.",
        "parameters": _obj(
            {"project_path": {"type": "string", "default": "", "description": "Optional project filter."}}
        ),
    },
    {
        "name": "read_graph",
        "tier": TIER_READ,
        "description": "Get memory-graph statistics for a project (node/edge/type counts).",
        "parameters": _obj({"project_path": _PROJECT_PATH}, ["project_path"]),
    },
    {
        "name": "activity_report",
        "tier": TIER_READ,
        "description": "Cross-project daily activity digest: sessions, tool calls, edits, git "
                       "mutations, and token/cost across ALL projects (or one, via project_path). "
                       "Defaults to today (UTC); pass date (YYYY-MM-DD) or since/until. Set "
                       "narrate=true to add an LLM prose summary.",
        "parameters": _obj(
            {
                "date": {"type": "string", "default": "",
                         "description": "UTC day YYYY-MM-DD (default today)."},
                "since": {"type": "string", "default": "",
                          "description": "ISO start timestamp (overrides date)."},
                "until": {"type": "string", "default": "",
                          "description": "ISO end timestamp (overrides date)."},
                "project_path": {"type": "string", "default": "",
                                 "description": "Optional: limit the digest to one project."},
                "narrate": {"type": "boolean", "default": False,
                            "description": "Add an LLM-narrated prose summary."},
            }
        ),
    },
    # ── read tier: C3 code intelligence ──
    {
        "name": "c3_search",
        "tier": TIER_READ,
        "description": "Code-intelligence search within one project. Use to find which files/symbols are relevant.",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "query": {"type": "string", "description": "What to search for."},
                "action": {
                    "type": "string",
                    "default": "code",
                    "enum": ["code", "exact", "files", "semantic", "transcript"],
                    "description": "Search mode.",
                },
                "top_k": {"type": "integer", "default": 3, "description": "Max hits."},
                "max_tokens": {"type": "integer", "default": 1200, "description": "Token budget for results."},
            },
            ["project_path", "query"],
        ),
    },
    {
        "name": "c3_search_cross",
        "tier": TIER_READ,
        "description": "Code-intelligence search across ALL projects. No project_path needed.",
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "What to search for."},
                "action": {"type": "string", "default": "code", "description": "Search mode (code|exact|files|semantic)."},
                "top_k": {"type": "integer", "default": 3, "description": "Max hits."},
            },
            ["query"],
        ),
    },
    {
        "name": "c3_read",
        "tier": TIER_READ,
        "description": "Read exact file content from a project, optionally narrowed to symbols or line ranges.",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "file_path": {"type": "string", "description": "Path to the file (relative to the project or absolute)."},
                "symbols": {"type": "string", "description": "Optional comma-separated symbol names to extract."},
                "lines": {"type": "string", "description": "Optional line or range, e.g. '10' or '10-40'."},
            },
            ["project_path", "file_path"],
        ),
    },
    {
        "name": "c3_compress",
        "tier": TIER_READ,
        "description": "Token-efficient structural map of a file (classes/functions/imports). Use before c3_read.",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "file_path": {"type": "string", "description": "Path to the file."},
                "mode": {
                    "type": "string",
                    "default": "map",
                    "enum": ["map", "dense_map", "smart", "diff", "bug_scan", "ast"],
                    "description": "Compression mode.",
                },
            },
            ["project_path", "file_path"],
        ),
    },
    {
        "name": "c3_validate",
        "tier": TIER_READ,
        "description": "Syntax/type validation on a file in a project.",
        "parameters": _obj(
            {"project_path": _PROJECT_PATH, "file_path": {"type": "string", "description": "Path to the file."}},
            ["project_path", "file_path"],
        ),
    },
    {
        "name": "c3_status",
        "tier": TIER_READ,
        "description": "Project health/budget/sessions overview.",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "view": {
                    "type": "string",
                    "default": "health",
                    "enum": ["budget", "health", "sessions", "notifications", "ghost_files"],
                    "description": "Which view to return.",
                },
                "detailed": {"type": "boolean", "default": False, "description": "Include extra detail."},
            },
            ["project_path"],
        ),
    },
    {
        "name": "c3_memory_query",
        "tier": TIER_READ,
        "description": "Query a project's memory (read-only actions: recall, query, list, score, graph, trends).",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "action": {"type": "string", "default": "query", "description": "Read-only memory action."},
                "query": {"type": "string", "default": "", "description": "Query text."},
                "category": {"type": "string", "default": "", "description": "Optional category."},
                "top_k": {"type": "integer", "default": 10, "description": "Max results."},
            },
            ["project_path"],
        ),
    },
    {
        "name": "c3_edits",
        "tier": TIER_READ,
        "description": "Query a project's edit ledger (history, versions, stats) — read-only audit trail.",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "action": {"type": "string", "default": "history", "description": "history|versions|stats."},
                "file": {"type": "string", "default": "", "description": "Optional file filter."},
                "limit": {"type": "integer", "default": 50, "description": "Max records."},
                "since": {"type": "string", "default": "", "description": "Optional ISO date lower bound."},
                "tag": {"type": "string", "default": "", "description": "Optional tag filter."},
            },
            ["project_path"],
        ),
    },
    {
        "name": "c3_edits_cross",
        "tier": TIER_READ,
        "description": "Query edit ledgers across ALL projects. No project_path needed.",
        "parameters": _obj(
            {
                "action": {"type": "string", "default": "history", "description": "history|stats."},
                "tag": {"type": "string", "default": "", "description": "Optional tag filter."},
                "limit": {"type": "integer", "default": 20, "description": "Max records."},
            }
        ),
    },
    # ── action tier: safe, non-code-edit ──
    {
        "name": "suggest_action",
        "tier": TIER_ACTION,
        "description": "Create a PENDING memory suggestion for a human to approve (no direct write).",
        "parameters": _obj(
            {
                "project_path": _PROJECT_PATH,
                "action": {
                    "type": "string",
                    "enum": ["merge_facts", "archive_facts", "add_fact"],
                    "description": "Kind of suggestion.",
                },
                "fact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fact IDs involved (may be empty for add_fact).",
                },
                "reason": {"type": "string", "description": "Why this is suggested."},
            },
            ["project_path", "action", "fact_ids", "reason"],
        ),
    },
    {
        "name": "delegate_task",
        "tier": TIER_ACTION,
        "description": "Delegate a sub-task to a configured Oracle agent and return its result.",
        "parameters": _obj(
            {
                "agent_id": {"type": "string", "description": "ID of an active Oracle agent."},
                "task": {"type": "string", "description": "Detailed instructions for the agent."},
            },
            ["agent_id", "task"],
        ),
    },
]

_SPECS_BY_NAME = {s["name"]: s for s in TOOL_SPECS}


class ToolRegistry:
    """Tier-aware view over ``TOOL_SPECS`` plus validated dispatch.

    Construct with a ``ToolExecutor`` and the configured ``max_tier`` cap.
    """

    def __init__(self, executor, max_tier: str = TIER_ACTION):
        self._executor = executor
        if max_tier not in _TIER_RANK:
            max_tier = TIER_ACTION
        self._max_rank = _TIER_RANK[max_tier]
        self.max_tier = max_tier

    # ── Introspection ──
    def _available(self) -> list[dict]:
        return [s for s in TOOL_SPECS if _TIER_RANK[s["tier"]] <= self._max_rank]

    def list_tools(self) -> list[dict]:
        """Return available tool specs (name/tier/description/parameters)."""
        return [
            {
                "name": s["name"],
                "tier": s["tier"],
                "description": s["description"],
                "parameters": s["parameters"],
            }
            for s in self._available()
        ]

    def tool_names(self) -> list[str]:
        return [s["name"] for s in self._available()]

    def get_spec(self, name: str) -> dict | None:
        spec = _SPECS_BY_NAME.get(name)
        if spec and _TIER_RANK[spec["tier"]] <= self._max_rank:
            return spec
        return None

    # ── Dispatch ──
    def call_tool(self, name: str, args: dict | None = None) -> dict:
        """Validate tier + required args, then dispatch through the executor.

        Unknown keys are dropped (tools forward args as kwargs). Returns the
        executor's result dict, or an ``{"error": ...}`` envelope on a bad call.
        """
        spec = _SPECS_BY_NAME.get(name)
        if spec is None:
            return {"error": f"Unknown tool: {name}"}
        if _TIER_RANK[spec["tier"]] > self._max_rank:
            return {"error": f"Tool '{name}' is not available at the configured capability tier "
                             f"('{self.max_tier}')."}
        args = dict(args or {})
        schema = spec["parameters"]
        declared = set(schema.get("properties", {}).keys())
        # Drop unknown keys so downstream **kwargs dispatch never TypeErrors.
        filtered = {k: v for k, v in args.items() if k in declared}
        missing = [r for r in schema.get("required", []) if filtered.get(r) in (None, "")]
        if missing:
            return {"error": f"Tool '{name}' missing required argument(s): {', '.join(missing)}"}
        return self._executor.execute(name, filtered)

    # ── OpenAPI ──
    def openapi_spec(self, server_url: str = "") -> dict:
        """Generate an OpenAPI 3.1 document from the available tool specs."""
        server_url = (server_url or "").rstrip("/")
        paths: dict[str, Any] = {
            "/api/discovery/tools": {
                "get": {
                    "operationId": "list_discovery_tools",
                    "summary": "List available discovery tools and their schemas.",
                    "security": [{"bearerAuth": []}],
                    "responses": {"200": {"description": "Tool catalog",
                                          "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/api/discovery/call": {
                "post": {
                    "operationId": "call_discovery_tool",
                    "summary": "Invoke any tool by name with an args object.",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": _obj(
                            {"tool": {"type": "string"}, "args": {"type": "object"}}, ["tool"])}},
                    },
                    "responses": {"200": {"description": "Tool result",
                                          "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
        }
        for s in self._available():
            first_line = s["description"].split(". ")[0].rstrip(".") + "."
            paths[f"/api/discovery/tools/{s['name']}"] = {
                "post": {
                    "operationId": s["name"],
                    "summary": first_line,
                    "description": f"{s['description']} (capability tier: {s['tier']})",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": bool(s["parameters"].get("required")),
                        "content": {"application/json": {"schema": s["parameters"]}},
                    },
                    "responses": {"200": {"description": "Tool result",
                                          "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        doc: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": "C3 Oracle Discovery API",
                "version": _c3_version(),
                "description": (
                    "Use C3's cross-project code & memory intelligence as tools, for external LLMs.\n\n"
                    "**Workflow:** call `list_projects` first to get project names + absolute paths; "
                    "discover across all projects with `search_facts` (memory) or `c3_search_cross` "
                    "(code); then narrow to one project (pass its `project_path`) using `c3_search`, "
                    "`c3_compress` (mode `map` to see a file's shape before reading), `c3_read` (exact "
                    "content), `query_memory`, `read_graph`, or `cross_insights`.\n\n"
                    "**Auth:** every request requires an `Authorization: Bearer <token>` header "
                    "(get it from `/api/discovery/mcp-info` or by running `c3 oracle api info`).\n\n"
                    "**Capability tiers:** `read` tools are pure discovery; `action` tools are safe and "
                    "non-destructive — `suggest_action` creates a PENDING suggestion for human approval "
                    "(not a direct write) and `delegate_task` runs a configured Oracle agent. No "
                    "code-editing tools are exposed.\n\n"
                    "**Invoke:** POST the arguments object to `/api/discovery/tools/{name}`, or POST "
                    "`{\"tool\": \"<name>\", \"args\": {...}}` to `/api/discovery/call`. Per-project "
                    "tools require a `project_path` from `list_projects`."
                ),
            },
            "security": [{"bearerAuth": []}],
            "components": {
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
            },
            "paths": paths,
        }
        if server_url:
            doc["servers"] = [{"url": server_url}]
        return doc
