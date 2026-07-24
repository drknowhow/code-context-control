# Security Policy

> **No warranty, no SLA, no obligation.** This project is provided "as is"
> per `LICENSE` Sections 7–8. The notes below describe how to *get in
> touch* about security issues, on a best-effort basis. Nothing here
> creates an obligation, response timeline, or fix commitment.

## Reporting a vulnerability

Please do not open public GitHub issues for security vulnerabilities.
Email **`yepgent@gmail.com`** with subject line `[c3-security]`. Include:

- A clear description of the issue and its impact.
- Steps to reproduce, including any relevant configuration.
- The version of C3, your OS, and Python version.
- Any proof-of-concept code (please do not exploit beyond what is needed
  to demonstrate the issue).

The maintainer will look at reports as time permits. No acknowledgement,
response, triage, or fix timeline is committed. Coordinated disclosure is
preferred over public dropping of details, but again — no obligation
runs in either direction.

## Scope

In scope:

- The C3 CLI (`cli/c3.py`)
- The C3 MCP server (`cli/mcp_server.py`)
- The C3 Hub web server (`cli/hub_server.py`) and per-project UI server
  (`cli/server.py`)
- C3 hooks (`cli/hook_*.py`)
- The Oracle service (`oracle/oracle_server.py`)
- Generated installer scripts and `pyproject.toml` build artifacts.

Out of scope:

- Vulnerabilities in third-party dependencies (please report upstream).
- Vulnerabilities that require physical access to the user's machine.
- Social-engineering attacks against C3 maintainers.
- Issues only reproducible on unsupported versions.

## Credential vault (v2.58.0+)

`c3_credentials` stores named secrets in the OS keyring (service `c3-creds`);
values larger than the keyring blob limit go to a Fernet-encrypted
`.c3/secrets.enc` whose random master key is itself a keyring entry. Design
boundaries operators should understand:

- **The trust boundary is your OS user account.** Windows Credential Manager
  (DPAPI user scope) and Linux Secret Service let *any* process running as
  the same user read entries — C3 cannot claim per-app isolation the OS
  doesn't provide. macOS Keychain offers per-app prompts.
- **Injection-first by design.** Decoded values reach child processes
  (`env_creds` / `{{cred:NAME}}`) but not the model's context, transcripts,
  or C3's logs; echoed values are best-effort redacted. The `reveal` action
  is disabled per entry until the user sets `agent_readable` — treat
  enabling it as putting that value into the conversation record.
- **No endpoint returns a value.** The HTTP APIs are write-only for secrets
  (enforced by tests); the hub reads metadata only. `c3_credentials` is
  excluded from the Oracle Discovery API and from cross-project
  (`c3_project`) dispatch, and proxied shells run with credentials disabled.
- **Redaction is exact-match**, not semantic: a value transformed by a child
  process (base64, split, re-encoded) will not be caught. Prefer scoped,
  revocable tokens over long-lived master secrets.

## Hardening notes for operators

- All C3 web servers (Hub, per-project UI, Oracle) bind to `127.0.0.1` by
  default and are guarded against browser-based attacks even on loopback:
  a Host-header allowlist (anti DNS-rebinding) plus an Origin/Referer check
  on every request (anti cross-origin CSRF), with scoped, non-wildcard CORS
  (see `core/web_security.py`). A malicious web page you visit therefore
  cannot drive C3's local endpoints. **There is still no user
  authentication**, so do not expose these servers to a public network
  without setting up TLS and an auth proxy in front of them. Setting `host`
  to `0.0.0.0`/another interface in `~/.c3/hub_config.json` (or `bind_host`
  for Oracle) is an opt-in advanced setting; add the externally-facing
  hostnames/IPs to an `allowed_hosts` list in the same config so the guard
  permits them.
- API keys for third-party model providers (Anthropic, OpenAI, etc.) are
  read from environment variables and never persisted by C3.
- Hooks executed by C3 inherit the calling process's privileges. Run C3
  under your own user account, never as root/Administrator.

## Telemetry (opt-in)

C3 has **no built-in telemetry**. The OSS package collects nothing.

If you install the optional `[telemetry]` extra
(`pip install code-context-control[telemetry]`) AND set both
`SENTRY_DSN` and `C3_TELEMETRY_OPT_IN=1` in your environment, the
`services/error_reporting.py` module forwards unhandled exceptions to
your own Sentry project. Even when enabled, the `before_send` hook
strips:

- HTTP request bodies, query strings, cookies, headers
- Local variables from stack frames (often contain file content / prompts)
- All `extra` payloads
- All contexts except `runtime`, `os`, `device`

No performance / tracing data is transmitted. No source code, prompts,
file paths, or model output is transmitted in normal operation. The
DSN points to **your** Sentry project — no events are sent to Anthropic
or to the C3 maintainers.

Alternative opt-in: write `{"opt_in": true}` to
`~/.c3/telemetry.json` instead of setting `C3_TELEMETRY_OPT_IN=1`.
