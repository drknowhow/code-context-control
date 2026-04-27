<h1 align="center">Code Context Control (C3)</h1>

<p align="center">
  <strong>The local code-intelligence layer for AI coding tools.</strong><br>
  Stop burning tokens on whole-file reads, blind greps, and unbounded log dumps.<br>
  Works with Claude Code, Codex, Gemini CLI, and VS Code Copilot.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="Platforms" src="https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey">
  <img alt="Status: Beta" src="https://img.shields.io/badge/status-beta-yellow">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/c3_ui.png" alt="C3 web dashboard" width="820">
</p>

---

## Why C3?

LLM-driven coding tools have one expensive failure mode: they read too much. They `cat` whole files, regex the entire repo, dump 10k-line logs into context, and burn through budget before they touch a single line of code.

**C3 is a thin, local layer that sits between your IDE and your repo and forces the model to read like a senior engineer:**

| Without C3 | With C3 |
|---|---|
| `Read` whole 2k-line file | `c3_compress` → 70% smaller map, then `c3_read(symbols=…)` for the exact function |
| `Grep` the whole repo blindly | `c3_search` returns ranked candidates with TF-IDF + symbol awareness |
| Dump full `pytest` output into the prompt | `c3_filter` distills 500 lines → 30 actionable ones |
| Edit, hope it compiled | `c3_edit` writes via a ledger + `c3_validate` runs pyright/tsc automatically |
| `Bash` test runs that hang on Windows | `c3_shell` returns structured `{exit_code, stdout, stderr, duration}` with auto-filter |
| Lose context on `/clear` | `c3_session(snapshot)` + `c3_memory` persist decisions across sessions |

Everything runs **locally**. No source code or prompts ever leave your machine unless you explicitly opt into a third-party model API.

---

## Install

Requires Python 3.10+.

```bash
# Clone and install
git clone https://github.com/drknowhow/code-context-control.git
cd code-context-control
pip install .

# Initialize a project (interactive)
c3 init /path/to/your/project
```

This walks you through:
1. IDE selection (Claude Code / Codex / Gemini / VS Code).
2. Optional local `git init`.
3. MCP server registration.
4. (Claude Code only) Permission tier selection.

Headless / scripted install:

```bash
c3 init /path/to/project --force --ide claude --mcp-mode direct --permissions standard
```

---

## What you get

### Surgical reads
```text
c3_compress(file_path="services/router.py", mode="map")    # structural outline (~30% tokens)
c3_read(file_path="services/router.py", symbols=["ModelRouter._classify_features"])
```

### Ranked search
```text
c3_search(query="prompt cache hit rate", action="code", top_k=5)
```

### Safer edits + automatic validation
```text
c3_edit(file_path=..., old_string=..., new_string=..., summary="fix off-by-one")
c3_validate(file_path=...)        # pyright/tsc if installed; syntax check otherwise
```

### Distilled output
```text
c3_filter(text=pytest_output)     # 500 lines → 30 actionable
c3_shell(cmd="pytest -x")          # structured, auto-filtered
```

### Persistent memory
```text
c3_memory(action="recall", query="why did we switch to direct MCP mode")
c3_session(action="snapshot")     # before /clear
```

### A web dashboard for everything you've done
<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/c3_hub.png" alt="C3 Project Hub" width="820">
</p>

```bash
c3-hub                            # http://127.0.0.1:3330
```

The Hub gives you per-project edit ledgers, session history, memory graphs, benchmark dashboards, and IDE/MCP profile management.

<p align="center">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/c3_hub_notifications.png" alt="C3 Notifications" width="400">
  <img src="https://raw.githubusercontent.com/drknowhow/code-context-control/main/docs/screenshots/c3_hub_ide_modal.png" alt="C3 IDE config" width="400">
</p>

---

## Tiered local AI (optional)

C3 ships with optional Ollama integration for offload work the primary model shouldn't waste context on:

| Tier | Model class | Used for |
|---|---|---|
| **Nano** | `qwen2:0.5b` | Sub-100 ms intent routing |
| **Micro** | `deepseek-r1:1.5b` | Last-turn Q&A, summarization |
| **Base** | `llama3.2:3b`+ | Code analysis, technical reasoning |

```text
c3_delegate(task="summarize this 4k-line stacktrace", backend="ollama")
```

Ollama is **optional**. C3 works fine without it.

---

## Permissions (Claude Code only)

C3 can manage Claude Code permission tiers via `.claude/settings.local.json`:

| Tier | What it allows |
|---|---|
| `read-only` | Exploration — no file writes, no git writes, no installs |
| `standard` | Normal dev workflow — edit, build, test, local git **(recommended)** |
| `permissive` | Full trust — everything except destructive ops |

All tiers always allow C3 MCP tools and include a deny list that blocks `rm -rf`, `sudo`, `git push --force`, etc.

```bash
c3 permissions show
c3 permissions standard
```

---

## Benchmarks

```bash
c3 benchmark /path/to/project
```

Runs C3 against open task suites (Aider Polyglot, SWE-bench Lite) with local-Ollama delegate measurement included.

---

## Security & Privacy

C3 runs locally. The Hub binds to `127.0.0.1` by default. **Do not expose the Hub to a public network without putting auth/TLS in front of it.**

**No telemetry by default.** Opt-in Sentry crash reporting is available via the `[telemetry]` extra and requires both `SENTRY_DSN` (your project) and `C3_TELEMETRY_OPT_IN=1` to activate. Even when enabled, request bodies, local variables, and prompts are stripped before sending.

See [`SECURITY.md`](SECURITY.md) for vulnerability reports, the full hardening guide, and the telemetry scrubbing details.

---

## License

- **Current OSS license** — Apache License 2.0 ([`LICENSE`](LICENSE)). Free for any use, including commercial. Modify, fork, redistribute — all permitted under Apache-2.0 terms.
- **Third-party deps** — see [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).

The author may introduce a paid offering or relicense future major versions; no commitment either way. Releases already published under Apache-2.0 (including all 2.x versions) keep their Apache-2.0 grant — that grant is irrevocable. Background and FAQ in [`LICENSING.md`](LICENSING.md). No warranty or support obligation; see LICENSE Sections 7–8.

---

## Links

- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md)
- **Security policy:** [`SECURITY.md`](SECURITY.md)
- **Issues / questions:** open a GitHub issue or email `dtselenc@gmail.com`
