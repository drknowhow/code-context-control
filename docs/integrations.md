# Integrations

All tokens live in the **OS keyring** (Windows Credential Manager, macOS
Keychain, Linux Secret Service) — never in `.c3/config.json`. Each integration
supports `login --global` so a single login is reusable across every C3
project; resolution precedence is always **project → home**.

---

## Credential vault

*(v2.58.0; Hub view v2.59.0; search + drawer v2.61.0)*

`c3_credentials` gives agents a protected, user-managed place for API keys,
tokens, and `.env`-style values — **global** (`~/.c3`, every project) or
**per-project** (`.c3`, shadows the global name). Values live in the OS keyring;
large values go to a Fernet-encrypted `.c3/secrets.enc` whose master key is
itself in the keyring.

```bash
c3 creds set OPENAI_KEY --desc "OpenAI billing key"   # value prompted, masked
c3 creds set NPM_TOKEN --global
c3 creds import .env                                   # bulk import
c3 creds list
```

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/2026-07/ui_credentials.png" alt="C3 Credentials tab" width="900">
</p>

The agent runs commands *with* the secret without ever seeing it:

```text
c3_shell(cmd='npm publish', env_creds='NPM_TOKEN')
c3_shell(cmd='curl -H "Authorization: Bearer {{cred:OPENAI_KEY}}" …')
```

C3 decodes only at the subprocess boundary. Echoed values are auto-redacted
(`env` dumps come back as `[cred:NAME]`), every use is ledger-logged by name,
and `reveal` — the only action returning a value — stays disabled per entry
until you flip `agent_readable` in the UI or via
`c3 creds set NAME --agent-readable`. The agent cannot raise that flag itself.

A hostile repo config cannot siphon global secrets (realm-atomic resolution,
tested), cross-project shells run with credentials disabled, and the vault is
hard-excluded from the Oracle Discovery API.

**Hub Credentials view.** Manage the global vault and every registered
project's entries in one place, with overriding shown both ways ("overrides
global" / "overridden ×N").

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/2026-07/hub_credentials.png" alt="C3 Hub Credentials view" width="900">
</p>

Cross-project search on <kbd>/</kbd> or
<kbd>Ctrl</kbd>+<kbd>K</kbd>: free words match name / description / env var /
project, and `project:` `scope:` `type:` `storage:` `name:` `env:` `inject:`
`agent:` `shadow:` qualifiers narrow further. Results group by credential name,
so "where is `STRIPE_KEY` defined and which one wins?" is one glance.
`agent:true` and `inject:true` are one-key exposure audits.

The write-only wire contract extends to the Hub: values are submitted
inbound-only, **no hub route ever returns a stored value**, and search indexes
metadata only.

Full guide: `cli/guide/credentials.html` (open in-app at `/guide/credentials.html`).

---

## Bitbucket Data Center / Server

*(v2.30.0)*

`c3_bitbucket` connects to self-hosted enterprise Bitbucket via REST + Personal
Access Token.

```bash
c3 bitbucket login --url https://bitbucket.example.com    # prompts user + PAT
c3 bitbucket login --global --url https://bitbucket.example.com
c3 bitbucket set-default --project PROJ --repo my-service
c3 bitbucket status
```

The MCP tool dispatches by `action`.

**Read:** `status`, `whoami`, `list_projects`, `list_repos`, `get_repo`,
`list_prs`, `get_pr`, `get_pr_diff`, `get_pr_activities`, `list_branches`,
`list_commits`, `list_activity`, `build_status`, `repo_settings`,
`list_webhooks`, `list_permissions`.

**Write:** `create_pr`, `comment_pr`, `approve_pr`, `unapprove_pr`,
`decline_pr`, `merge_pr`, `create_branch`, `delete_branch`,
`update_repo_settings`, `create_webhook`, `delete_webhook`.

PR merges and branch deletes are recorded to the C3 edit ledger, so the audit
trail covers platform-side changes too.

The per-project UI gains a **Bitbucket** tab with Overview / Pull Requests /
Branches / Activity / Admin sub-views.

Full guide: `cli/guide/bitbucket.html`.

---

## Jira — Cloud + Data Center

*(v2.56.0)*

`c3_jira` covers Jira Cloud (REST v3, email + API token) and self-hosted Jira
Data Center / Server (REST v2, PAT) behind one tool. Cloud is inferred for
`*.atlassian.net`.

```bash
c3 jira login --url https://yoursite.atlassian.net        # email + API token
c3 jira login --url https://jira.example.com --deployment data_center
c3 jira login --global --url https://yoursite.atlassian.net
c3 jira set-default --project PROJ
c3 jira status
```

**Read:** raw-JQL `search`, `my_issues`, `get_issue`, `list_transitions`,
`get_create_metadata`, `search_users`.
**Write (ledger-logged, identifiers only — never bodies):** `create_issue`,
`update_issue`, `comment`, `transition`, `assign`. `create_issue` is
pre-validated against create metadata and returns machine-readable missing
required fields; `update_issue` edits an existing issue (summary,
description, or a `fields` JSON of field ids → values); `transition`
accepts an id or a name.

A caveat worth knowing: `get_create_metadata` returns Jira's createmeta,
which is the **field configuration** for the project + issue type — not the
create screen. Jira can list a field (Epic Link is the classic case) that
`create_issue` then rejects with *"cannot be set. It is not on the
appropriate screen"*. The tool says so in its response; when it happens,
create the issue without the field and set it afterwards with
`update_issue` — the edit screen is configured separately from the create
screen. Field entries carry both `id` and `name`; the `fields` JSON on
create/update takes **ids** (`customfield_…`), not display names.

The per-project UI gains a **Jira** tab: a My Work board grouped by status
category, JQL search, an issue drawer with transitions and comments, and an
Activity view linking edit-ledger work to issue keys (`PROJ-123` detected in
branch names and edit summaries — this works even before login).

Named accounts support multiple sites (`--name work`, `--name internal`). The
jira config section resolves project → home **wholesale from one file**, never
field-merged, so a repository's config can never override the
credential-bound server URL or TLS settings of a globally registered account.

---

## Oracle Discovery API

*(v2.32.0; activity digest v2.38.0)*

The **Oracle** is C3's optional cross-project memory agent. It can expose
cross-project code and memory intelligence as **tools for an external LLM**:
point Claude — or any function-calling model — at a running Oracle and it can
discover your projects and search code, memory, and the cross-project graph
across all of them.

Two transports share one tool core:

- **MCP** (streamable HTTP/SSE) at `http://127.0.0.1:3332/mcp`
- **OpenAPI REST** at `http://127.0.0.1:3331/api/discovery` — fetch
  `/openapi.json` to auto-register the tools

```bash
c3 oracle serve --no-browser     # serves the dashboard + discovery endpoints
c3 oracle api info               # Bearer token + a paste-ready .mcp.json snippet
```

Only **read** and **safe-action** tools are exposed — no code editing. Requests
need a Bearer token (stored in the OS keyring) and both servers bind
`127.0.0.1` by default. Generate, rotate, and copy the token from the
dashboard's **Settings → Discovery API** tab.

The Oracle also reports a **cross-project activity digest** — sessions, tool
calls, edits, git mutations, and token/cost for a day — via the
`activity_report` discovery tool, `GET /api/activity/digest`, and the
dashboard's **Activity** tab.

Full guide:
[`oracle-guide/discovery-api.md`](https://github.com/drknowhow/code-context-control/blob/main/oracle-guide/discovery-api.md).
