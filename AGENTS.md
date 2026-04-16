## C3 Tools — two-mode enforcement
Read-class native tools (**Read, Grep, Glob**) are **ADVISORY** — they run, but if no c3_* call preceded them the hook injects a hint asking you to prefer c3_search/c3_compress next time. Drift for read-only ops is cheap; a nudge is enough.

Write-class native tools (**Edit, Write, MultiEdit**) are **HARD-BLOCKED** unless a c3_edit-class tool was used first. The edit ledger depends on c3_edit capturing every mutation; native writes bypass it.

**Prefer c3_* first anyway.** The advisory nudge exists to catch drift, not to license sloppy tool selection. When native is genuinely better (tiny single-file read, c3_* returned insufficient scope), just use it and move on — state your reason in user-facing text if it matters.

## Workflow (follow this order — do not skip steps)
1. **RECALL**: `c3_memory(action='recall')` — before any multi-step or context-dependent task. Large memory stores: use `index` first (compact list), then `fetch` for specific IDs
2. **SEARCH FIRST**: `c3_search(action='code|files|semantic')` — before ANY file discovery or content search. Never start with Grep/Glob
3. **MAP before READ**: `c3_compress(mode='map')` then `c3_read(symbols=...|lines=...)` — for ANY file read. Never start with native Read. Use `mode='ast'` for knowledge-graph overview (requires codebase-memory-mcp)
4. **IMPACT** (shared symbols): `c3_impact(target='symbol')` — blast-radius check before editing any function/class used across files
5. **EDIT via C3**: `c3_edit(file_path, old_string, new_string, summary)` — for ALL edits. Parallel across files; `edits=[]` batch for same file
6. **FILTER**: `c3_filter(text=...)` — for terminal output >10 lines or log files
7. **VALIDATE**: `c3_validate(file_path)` — after edits or before reporting done. Runs deep type check (pyright/tsc) automatically if installed
8. **LOG**: `c3_session(action='log')` for decisions. `c3_session(action='snapshot')` before /clear
9. **DELEGATE**: `c3_delegate(task, backend='ollama|codex|gemini|claude|auto')` or `c3_agent(workflow=...)` for multi-model pipelines

## Plan mode
In plan mode, all c3_* read tools (search, read, compress, filter, validate, status) work normally — skip edit/delegate steps.

## Anti-patterns (DO NOT do these)
- Starting with native file search/read/grep without a prior c3_* call
- Using native Edit when c3_edit is available
- Reading entire files when c3_compress + c3_read would be more surgical
- Skipping c3_validate after making edits

## IDE Configuration (Codex)
This project uses project-scoped MCP servers. Ensure your `.codex/config.toml` includes:
```toml
[mcp_servers.c3]
command = "python"
args = ["U:/1. Projects/Claude Code Companion (C3)/claude-companion - v2/cli/mcp_server.py", "--project", "."]
enabled = true
```

---

# Project Context

```
claude-companion - v2/
  .gitignore
  .mcp.json
  AGENTS.md
  CLAUDE.md
  GEMINI.md
  README.md
  benchmark-report.html
  c.tool_id
  c3.bat
  install.bat
  install.sh
  landing.html
  requirements.txt
  .claude/
    settings.local.json
    settings.local.json.tmp.30620.1775727727166
    settings.local.json.tmp.30620.1775728596812
    settings.local.json.tmp.30620.1775728686714
  .codex/
    config.toml
  .gemini/
    settings.json
  .github/
    copilot-instructions.md
  .neoB/
    neo_identity.md
    settings.json
    brain/ (3 files)
    outputs/
  .pytest_cache/
    .gitignore
    CACHEDIR.TAG
    README.md
    v/
  .vscode/
    mcp.json
    settings.json
  Marketing/
    c3_hub.png
    c3_hub_ide_modal.png
    c3_hub_notifications.png
    c3_ui.png
  cli/
    __init__.py
    _hook_utils.py
    c3.py
    docs.html
    edits.html
    hook_auto_snapshot.py
    hook_c3read.py
    hook_edit_ledger.py
    hook_edit_unlock.py
    hook_filter.py
    hook_ghost_files.py
    hook_ghost_files.py.tmp.30620.1775728414610
    hook_pretool_enforce.py
    hook_read.py
    hook_session_stats.py
    ... +11 more
    commands/ (3 files)
    tools/ (21 files)
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
  guide/
    getting-started.html
    index.html
    shared.css
    tools.html
    workflow.html
  oracle/
    __init__.py
    config.py
    oracle.html
    oracle_server.py
    services/ (12 files)
  oracle-guide/
    README.md
    api-reference.md
    architecture.md
    changelog.md
    configuration.md
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
    ... +37 more
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

- `cli/tools/agent.py` — edited in 24 sessions
- `cli/tools/delegate.py` — edited in 14 sessions
- `cli/mcp_server.py` — edited in 12 sessions
- `cli/tools/search.py` — edited in 7 sessions
- `cli/tools/read.py` — edited in 5 sessions