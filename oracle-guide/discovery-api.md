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
> CSRF guard (v2.33.0); the MCP transport enforces a Host-header allowlist too (v2.34.0). A web
> page open in a browser on the same machine therefore cannot reach either transport.
> Only **read** and **safe-action** tools are exposed — no code-editing tools.

---

## 1. Start the Oracle

```bash
c3 oracle serve --no-browser      # v2.49.0+ (alias: c3 oracle start)
# or, from a source checkout:
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
`c3_memory_query`, `c3_edits`, `c3_edits_cross`, `activity_report`, `c3_project`,
`c3_artifacts`.

- `c3_project` (v2.48.0): cross-project ops by registered project name or path —
  `list` | `info` | `subprojects` | `search` | `read` | `compress` | `status` |
  `memory` | `impact` | `edits` | `validate`. Write verbs are blocked; the project
  argument is validated against discovered projects.
- `c3_artifacts` (v2.48.0): agent-config artifact tracking, read-only —
  `list` | `history` | `show` | `diff` | `status` (`scan` and `restore` are blocked).
- `c3_search_cross` / `c3_edits_cross` take a `scope` param (v2.48.0): `''` = all
  projects, `'top'` = top-level projects only, or a project name/path = that project
  plus its sub-projects.

**Safe actions:** `suggest_action` (creates a *pending* memory suggestion a human
approves), `delegate_task` (runs a configured Oracle agent; optional `project_path`,
required when the agent's backend is a CLI — codex/gemini/claude/auto — since CLI
backends run read-only inside a registered project).

See [api-reference.md](api-reference.md#discovery-api-external-llm-tool-surface) for
full endpoint and schema details.

---

## Mobile gateway (`/api/mobile/*`, v2.68.0)

A second Bearer-gated surface on the same Oracle serves the **c3-mobile companion
app** (sibling repo): merged cross-project activity feed, project overview, PM
board read/write, activity digest, and notification ack.

Differences from the Discovery surface:

- The **Bearer token is required on every method, GETs included** — there is no
  `api_require_auth` opt-out and no session-cookie fallback. The phone is
  inherently remote, so this surface is never open.
- Every project path in a request must resolve to a **registered project with a
  `.c3` directory**; anything else is a 404 and touches nothing on disk.
- PM writes are audited to the target project's `.c3/activity_log.jsonl` with
  `source: "oracle-mobile"`, and mutating calls share the Discovery rate-limit
  bucket.
- Disable it with `"mobile_api_enabled": false` in `~/.c3/oracle/config.json`.

**Pairing:** Oracle dashboard → Settings → *Mobile app* → *Show pairing code*.
The QR encodes `{v, kind: "c3-oracle", url, token}`; edit the URL field first so
it carries the address the phone can actually reach (Tailscale IP or LAN). For
remote reachability set `bind_host` to that interface IP (prefer the Tailscale
IP over `0.0.0.0`) and add it to `allowed_hosts` — same recipe as above. The
token in the QR is the Discovery key: rotating it un-pairs every client.

### Security surface: credentials + Access Guard

The gateway also serves the credential vault and Access Guard. This is the
**first network-reachable surface** for either subsystem — `cli/server.py` and
`cli/hub_server.py` are loopback-only, and their whole confidentiality model is
"only localhost can ask", which does not transfer to a phone on a tailnet.

| Route | Methods | Notes |
|---|---|---|
| `/api/mobile/credentials` | GET, POST | `?project=` or `?scope=global`; POST without `value` = metadata-only update |
| `/api/mobile/credentials/<name>` | GET, DELETE | |
| `/api/mobile/credentials/<name>/check` | POST | Resolvability + fingerprint. POST because the fingerprint is computed from the decoded value |
| `/api/mobile/credentials-overview` | GET | Cross-project inventory with shadowing both ways |
| `/api/mobile/access` | GET | Rules per scope, coverage matrix, mask presets + status |
| `/api/mobile/access/check` | POST | Path verdict + the exact refusal string |
| `/api/mobile/access/rule` | POST, DELETE | deny / read_only rules |
| `/api/mobile/access/mask` | POST, DELETE | Mask rules |
| `/api/mobile/access/mask/activate` | POST | Purge → build → validate |
| `/api/mobile/access/denials` | GET | Aggregated counters, each row with a `fix` |
| `/api/mobile/access/denials/search` | GET | Raw events, newest first |
| `/api/mobile/enforcement` | GET, POST | Tool discipline (`strict\|advisory\|off`) |

Capabilities are reported **effectively** by `/api/mobile/info` (`api_version`
2+): a subsystem switched off in config disappears from the list *and* 404s, so
a client hides that UI instead of discovering the truth from a pile of 404s.

Config keys (`~/.c3/oracle/config.json`):

| Key | Default | Effect |
|---|---|---|
| `mobile_credentials_enabled` | `true` | Credential reads |
| `mobile_credentials_write` | `true` | set / delete / metadata |
| `mobile_creds_agent_readable_raise` | **`false`** | *Raising* `agent_readable` from the phone |
| `mobile_access_enabled` | `true` | Access + enforcement reads |
| `mobile_access_write` | `true` | Rule / mask / enforcement mutations |
| `mobile_access_global_scope` | **`false`** | Writing machine-wide `~/.c3` access rules |
| `mobile_security_rate_limit_per_min` | `12` | Second, tighter budget for security mutations |

The two defaulting to `false` are the operations whose blast radius exceeds
what the token can otherwise reach: `agent_readable` moves a secret into model
context and transcripts (and cannot be un-disclosed), and a global access rule
governs every project on the machine. Note what a typed confirmation is and is
not — it stops a fat-finger and a blind replay, but an attacker holding the
Bearer constructs that field trivially. **The config switch is the control that
resists a leaked token.**

Loosening operations require a typed confirmation: removing a `deny` or mask
rule, raising `agent_readable`, first mask activation, and setting enforcement
to `off`. Tightening never does.

#### What is deliberately absent, and why

Read this before adding any of it back.

- **No route returns a credential value.** Entries serialize only through
  `credential_store.public_entry` (an allowlist that cannot emit one), and
  `mobile_api.py` never references `get_value` — `credential_store.is_resolvable`
  exists so it never needs to. Both are asserted by tests, including a canary
  sweep that also covers `/feed`, since that route ships every project's logs
  verbatim and would exfiltrate a value that leaked into an audit line.
- **No `set_builtin_disabled` route.** Disabling a builtin guard needs a keyring
  attestation and a typed confirmation; it stays a local operation performed by
  someone at the machine. No HTTP surface anywhere exposes it.
- **No `/access/preview` equivalent.** The desktop route returns RAW file
  content and is explicitly human-only.
- **No bulk `.env` import.** Largest blast radius in the vault, and no sensible
  phone affordance.
- **No denial-log clearing.** A leaked token's first move after being denied
  would be to erase the evidence.
- **No global-scope or field-level enforcement writes.** `scope` is hard-coded
  to `project`; a machine-wide discipline change from a phone would silently
  re-govern every project.
- **`rebuild_index` is never honored from the wire** on mask activation. The
  purge has already dropped the index, so protection is complete either way,
  and a rebuild is unbounded CPU from one tap. Use
  `c3 access mask activate --reindex` locally.
