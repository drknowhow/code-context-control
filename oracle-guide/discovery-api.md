# Oracle Discovery API — connect an external LLM

The Oracle can expose C3's cross-project code & memory intelligence as **tools** for
an external LLM. Point Claude (or any function-calling model) at a running Oracle and
it can discover your projects, search code and memory across all of them, traverse the
memory graph, and surface insights — without ever touching the chat UI.

Two transports share one tool core:

- **MCP** (streamable HTTP/SSE) at `http://127.0.0.1:3332/mcp` — native for Claude
  Code / Claude Desktop / any MCP client.
- **OpenAPI REST** at `http://127.0.0.1:3331/api/discovery` — for any LLM with
  function-calling.

> Security: both bind to `127.0.0.1` (loopback) by default and require a Bearer token.
> The REST surface is additionally protected by C3's Host-header allowlist + Origin/Referer
> CSRF guard (v2.33.0), so a web page open in a browser on the same machine cannot reach it.
> Only **read** and **safe-action** tools are exposed — no code-editing tools.

---

## 1. Start the Oracle

```bash
python oracle/oracle_server.py --no-browser
```

On startup it prints both URLs and ensures an API key exists.

## 2. Get your token + connection info

```bash
c3 oracle api info        # REST + MCP URLs and a ready-to-paste .mcp.json snippet
c3 oracle api key         # just the token
c3 oracle api rotate      # replace the token
c3 oracle api clear       # delete the stored token
```

You can also generate, rotate, and copy the token from the **Oracle dashboard →
Settings → Discovery API** (it shows the live MCP URL, REST base, and `.mcp.json` snippet).

The token lives in your OS keyring (Windows Credential Manager / macOS Keychain /
Linux Secret Service). For headless/CI, set `C3_ORACLE_API_KEY` instead — it
overrides the keyring and is never persisted.

## 3a. Connect Claude (MCP)

Add this to your `.mcp.json` (project root) or Claude Desktop config:

```json
{
  "mcpServers": {
    "c3-oracle": {
      "type": "http",
      "url": "http://127.0.0.1:3332/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Claude will list the `c3-oracle` tools (e.g. `list_projects`, `c3_search_cross`,
`query_memory`, `read_graph`). Start a session and ask it to "discover what projects
exist and search them for X".

## 3b. Connect any function-calling LLM (OpenAPI)

Fetch the spec and register the tools, then call them with the Bearer header:

```bash
curl -H "Authorization: Bearer <token>" \
     http://127.0.0.1:3331/api/discovery/openapi.json

curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     -d '{"tool":"list_projects","args":{}}' \
     http://127.0.0.1:3331/api/discovery/call
```

Each tool is also a discrete operation at `POST /api/discovery/tools/<name>` whose
request body is the tool's arguments object.

---

## Configuration

Set in `~/.c3/oracle/config.json`:

| Key | Default | Meaning |
|-----|---------|---------|
| `bind_host` | `127.0.0.1` | Interface to bind (use `0.0.0.0` to expose on a network — then add TLS/firewalling). |
| `allowed_hosts` | `[]` | Extra hostnames/IPs the Host-header + Origin guard accepts. Needed when `bind_host` is non-loopback so legitimate browsers/clients are not blocked (v2.33.0). |
| `api_enabled` | `true` | Serve the REST surface. |
| `api_require_auth` | `true` | Require the Bearer token. |
| `api_max_tier` | `action` | Cap exposed tools: `read` (discovery only) or `action` (adds `suggest_action`, `delegate_task`). |
| `mcp_enabled` | `true` | Start the MCP HTTP/SSE server. |
| `mcp_port` | `3332` | MCP transport port. |

## Tools at a glance

**Discovery (read):** `list_projects`, `search_facts`, `query_memory`,
`project_health`, `analyze_project`, `cross_insights`, `read_graph`, `c3_search`,
`c3_search_cross`, `c3_read`, `c3_compress`, `c3_validate`, `c3_status`,
`c3_memory_query`, `c3_edits`, `c3_edits_cross`.

**Safe actions:** `suggest_action` (creates a *pending* memory suggestion a human
approves), `delegate_task` (runs a configured Oracle agent).

See [api-reference.md](api-reference.md#discovery-api-external-llm-tool-surface) for
full endpoint and schema details.
