# Security Policy

We take the security of Code Context Control (C3) seriously. This document
describes how to report vulnerabilities and what to expect in response.

## Supported versions

Only the latest minor release is supported with security fixes. We recommend
all users upgrade promptly when a security release is published.

| Version | Supported |
|---|---|
| 2.28.x | ✅ |
| < 2.28 | ❌ |

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Email reports to **`dtselenc@gmail.com`** with subject line
`[c3-security]`. Include:

- A clear description of the issue and its impact.
- Steps to reproduce, including any relevant configuration.
- The version of C3, your OS, and Python version.
- Any proof-of-concept code (please do not exploit beyond what is needed
  to demonstrate the issue).

You should receive an acknowledgement within **3 business days**. We aim
to provide a substantive response (triage, fix plan, or request for more
information) within **7 business days**.

## Disclosure timeline

- **Day 0:** report received.
- **Day ≤ 3:** acknowledgement sent.
- **Day ≤ 7:** triage complete; severity assigned.
- **Day ≤ 30:** fix released for high/critical issues; longer windows
  negotiated case-by-case for low/medium severity.
- **Day ≤ 30 + 14:** coordinated public disclosure (CVE if applicable),
  with credit to the reporter unless anonymity is requested.

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

## Hardening notes for operators

- The C3 Hub binds to `127.0.0.1` by default. **Do not expose it to a
  public network without setting up authentication, TLS, and access
  control in front of it.** Setting `host` to `0.0.0.0` or another
  interface in `~/.c3/hub_config.json` is an opt-in advanced setting.
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
