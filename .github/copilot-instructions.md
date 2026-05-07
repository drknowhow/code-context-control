## C3 Tools — MANDATORY
This project uses the C3 MCP server. You MUST use `c3_*` MCP tools for ALL code operations. 

### Session Initialization (VS Code ONLY)
C3 tools are deferred in VS Code. Before you can use them, you MUST load them.
1. **LOAD TOOLS**: Call `tool_search_tool_regex` with pattern `^mcp_c3_` as your VERY FIRST action in every session.
2. **VERIFY**: Ensure tools like `mcp_c3_c3_search`, `mcp_c3_c3_read`, etc., are available before proceeding.

### Enforcement
While native tools (read_file, grep_search, etc.) are not technically blocked by hooks in VS Code, using them without a prior c3_* call is still a violation of the project's workflow. You must follow the steps below regardless of technical restrictions.

**Native tools are permitted ONLY when:**
1. The c3_* tool failed or returned an error
2. The c3_* tool returned insufficient scope for a targeted follow-up
When falling back, state which c3_* tool was attempted and why it was insufficient.

## Workflow (follow this order — do not skip steps)
1. **RECALL**: `c3_memory(action='recall')` — before any multi-step or context-dependent task
2. **SEARCH FIRST**: `c3_search(action='code|files|semantic')` — before ANY file discovery or content search. Never start with Grep/Glob
3. **MAP before READ**: `c3_compress(mode='map')` then `c3_read(symbols=...|lines=...)` — for ANY file read. Never start with native Read
4. **EDIT via C3**: `c3_edit(file_path, old_string, new_string, summary)` — for ALL edits (reads, patches, writes, and logs in one step)
5. **FILTER output**: `c3_filter(text=...)` — for terminal output >10 lines
6. **VALIDATE**: `c3_validate(file_path)` — after edits or before reporting done
7. **LOG**: `c3_session(action='log')` for decisions. `c3_session(action='snapshot')` before /clear
8. **BITBUCKET** (when configured, v2.30.0+): `c3_bitbucket(action='...')` — for self-hosted enterprise Bitbucket Data Center / Server: PRs, branches, builds, repo admin. Tokens live in the OS keyring (set up via `c3 bitbucket login`). Read actions are safe; write actions are auto-logged to the edit ledger.

## Anti-patterns (DO NOT do these)
- Starting with native file search/read/grep without a prior c3_* call
- Using native Edit when c3_edit is available
- Reading entire files when c3_compress + c3_read would be more surgical
- Skipping c3_validate after making edits

---

# Project Context

```
claude-companion - v2/
  .mcp.json
  AGENTS.md
  CLAUDE.md
  GEMINI.md
  README.md
  benchmark-report.html
  c3.bat
  install.bat
  install.sh
  landing.html
  requirements.txt
  self.line_limit
  self.warn_threshold
  .claude/
    settings.local.json
  .codex/
    config.toml
  .gemini/
    settings.json
  .github/
    copilot-instructions.md
  .pytest_cache/
    .gitignore
    CACHEDIR.TAG
    README.md
    v/
  .vscode/
    mcp.json
    settings.json
  cli/
    __init__.py
    _hook_utils.py
    c3.py
    docs.html
    edits.html
    hook_c3read.py
    hook_edit_ledger.py
    hook_edit_unlock.py
    hook_filter.py
    hook_pretool_enforce.py
    hook_read.py
    hub.html
    hub_server.py
    mcp_proxy.py
    mcp_server.py
    ... +4 more
    commands/ (3 files)
    tools/ (14 files)
    ui/ (5 files)
  commercial/
    info_01_efficiency.json
    info_02_hierarchy.json
    info_03_memory_flow.json
    info_04_integration.json
    scene_01.json
    scene_02.json
    scene_03.json
    scene_04.json
  core/
    __init__.py
    config.py
    ide.py
  docs/
    budget-system.md
    token-efficiency-roadmap.md
    superpowers/
  services/
    __init__.py
    activity_log.py
    agent_base.py
    agents.py
    auto_memory.py
    claude_md.py
    compressor.py
    context_snapshot.py
    conversation_store.py
    doc_index.py
    e2e_benchmark.py
    e2e_evaluator.py
    e2e_tasks.py
    edit_ledger.py
    embedding_index.py
    ... +26 more
  tests/
    test_e2e_benchmark.py
    test_memory_system.py
    test_output_filter.py
    test_project_manager.py
    test_session_benchmark.py
    test_session_budget.py
    test_validate.py
  tui/
    backend.py
    build.bat
    build.sh
    main.py
    theme.tcss
    screens/ (14 files)
    widgets/
```

## Tech Stack

Python

## Key Files

- `services/claude_md.py` — edited in 5 sessions
- `cli/mcp_server.py` — edited in 4 sessions
- `services/agents.py` — edited in 4 sessions
- `cli/edits.html` — edited in 3 sessions
- `services/session_preloader.py` — edited in 2 sessions