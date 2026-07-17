## C3 Tools — MANDATORY (enforced by hooks)
Native tools (Read, Grep, Glob, Edit, Write) are **blocked by PreToolUse hooks** unless a c3_* tool was called first. Do NOT attempt native tools without prior c3_* usage — they will be denied.

**Native tools are permitted ONLY when:**
1. The c3_* tool failed or returned an error
2. The c3_* tool returned insufficient scope for a targeted follow-up
When falling back, state which c3_* tool was attempted and why it was insufficient.

## Workflow (follow this order — do not skip steps)
1. **RECALL**: `c3_memory(action='recall')` — before any multi-step or context-dependent task. Large memory stores: use `index` first (compact list), then `fetch` for specific IDs
2. **SEARCH FIRST**: `c3_search(action='code|files|semantic')` — before ANY file discovery or content search. Never start with Grep/Glob
3. **MAP before READ**: `c3_compress(mode='map')` then `c3_read(symbols=...|lines=...)` — for ANY file read. Never start with native Read. Use `mode='ast'` for knowledge-graph overview (requires codebase-memory-mcp)
4. **IMPACT** (shared symbols): `c3_impact(target='symbol')` — blast-radius check before editing any function/class used across files
5. **EDIT via C3**: `c3_edit(file_path, old_string, new_string, summary)` — for ALL edits. Parallel across files; `edits=[]` batch for same file
6. **FILTER**: `c3_filter(text=...)` — for terminal output >10 lines or log files
6.5. **SHELL via C3**: `c3_shell(cmd, cwd='', timeout=60)` — for tests, git, build, scripts. Returns structured `{exit_code, stdout, stderr, duration_ms}`. Auto-filters stdout >30 lines; auto-logs git-mutating commands (commit/add/merge/rebase/reset/restore/checkout) to the edit ledger. Best-effort blocks the most catastrophic commands (`rm -rf` of `/`, a top-level system dir, or `$HOME`/`~`; fork bombs; whole-drive wipes) — a guard, not a sandbox; soft-warns on `--force`, `--no-verify`, `reset --hard`. Native Bash remains the fallback for interactive/TTY commands
7. **VALIDATE**: `c3_validate(file_path)` — after edits or before reporting done. Runs deep type check (pyright/tsc) automatically if installed
8. **LOG**: `c3_session(action='log')` for decisions. `c3_session(action='snapshot')` before /clear
9. **DELEGATE**: `c3_delegate(task, backend='ollama|codex|gemini|claude|auto')` or `c3_agent(workflow=...)` for multi-model pipelines
10. **BITBUCKET** (when configured, v2.30.0+): `c3_bitbucket(action='...')` — for self-hosted enterprise Bitbucket Data Center / Server: PRs, branches, builds, repo admin. Tokens live in the OS keyring (set up via `c3 bitbucket login`, or `login --global` for a home config reusable across projects; account resolution precedence is project → home). Read actions are safe in plan mode; write actions (`merge_pr`, `create_branch`, etc.) are auto-logged to the edit ledger.
11. **CROSS-PROJECT** (v2.31.0+): `c3_project(action='list|scan|info|search|read|edit|shell|...', project='<name|path>')` — discover and operate on OTHER c3-installed projects. `list`/`scan` need no project; reads (search/read/compress/status/memory/impact/edits/validate/filter) run freely; writes (`edit`, `shell`, memory add/update/delete) require `allow_write=true` and are logged to the target project's ledger.
12. **AGENT CONFIG** (v2.46.0+): `c3_artifacts(action='status|list|history|diff|restore')` — version history for the files that shape the agent itself: instruction docs (CLAUDE.md/AGENTS.md/GEMINI.md), settings/hooks, MCP configs, .claude skills/agents/commands. Out-of-band edits are captured automatically; `diff` any version against live, `restore` writes a prior version back (forward-only, ledger-logged).

## Plan mode
In plan mode, all c3_* read tools (search, read, compress, filter, validate, status) work normally — skip edit/delegate steps.

## Anti-patterns (DO NOT do these)
- Starting with native file search/read/grep without a prior c3_* call
- Using native Edit when c3_edit is available
- Reading entire files when c3_compress + c3_read would be more surgical
- Skipping c3_validate after making edits

# Project Context

```
claude-companion - v2/
  .gitattributes
  .gitignore
  .manifest.swo
  .manifest.swp
  .mcp.json
  AGENTS.md
  CHANGELOG.md
  CLAUDE.md
  LICENSE
  LICENSING.md
  README.md
  SECURITY.md
  THIRD_PARTY_LICENSES.md
  c3.bat
  install.bat
  ... +3 more
  .agents/
  .claude/
  .codex/
  .github/
  .neoB/
  .pytest_cache/
  .ruff_cache/
  .vscode/
  cli/
  code_context_control.egg-info/
  commercial/
  core/
  docs/
  oracle/
  oracle-guide/
  services/
  tests/
  tui/
```

## Tech Stack

Python (Modern)

## Key Files

- `cli/tools/agent.py` — edited in 17 sessions
- `cli/mcp_server.py` — edited in 15 sessions
- `cli/tools/search.py` — edited in 11 sessions
- `cli/c3.py` — edited in 8 sessions
- `cli/hook_pretool_enforce.py` — edited in 5 sessions

## Key Facts (use c3_memory for more)

- Session summary (20260701): Decision: Deep 4-agent evaluation of C3 completed (tool surface, hooks, services, claims-vs-
- Session summary (20260701): Files (modified): README.md, services/subprojects.py, services/project_manager.py, services/
- [validate] U:\1. Projects\Claude Code Companion (C3)\claude-companion - v2\cli\hub_ui\components\toasts.js has syntax er
- Hub v2 (v2.44.0): cli/hub_ui.html + cli/hub_ui/ 22-file concat bundle served at / (hub.html frozen at /legacy until v2.4
- Session summary (20260703): Files (modified): cli/tools/edit.py, cli/tools/read.py, tests/test_read_edit_parity.py, C:\U