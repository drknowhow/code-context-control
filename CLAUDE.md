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
6.5. **SHELL via C3**: `c3_shell(cmd, cwd='', timeout=60)` — for tests, git, build, scripts. Returns structured `{exit_code, stdout, stderr, duration_ms}`. Auto-filters stdout >30 lines; auto-logs git-mutating commands (commit/add/merge/rebase/reset/restore/checkout) to the edit ledger. Blocks fork bombs and `rm -rf /` or `~`; soft-warns on `--force`, `--no-verify`, `reset --hard`. Native Bash remains the fallback for interactive/TTY commands
7. **VALIDATE**: `c3_validate(file_path)` — after edits or before reporting done. Runs deep type check (pyright/tsc) automatically if installed
8. **LOG**: `c3_session(action='log')` for decisions. `c3_session(action='snapshot')` before /clear
9. **DELEGATE**: `c3_delegate(task, backend='ollama|codex|gemini|claude|auto')` or `c3_agent(workflow=...)` for multi-model pipelines
10. **BITBUCKET** (when configured, v2.30.0+): `c3_bitbucket(action='...')` — for self-hosted enterprise Bitbucket Data Center / Server: PRs, branches, builds, repo admin. Tokens live in the OS keyring (set up via `c3 bitbucket login`). Read actions are safe in plan mode; write actions (`merge_pr`, `create_branch`, etc.) are auto-logged to the edit ledger.

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
  .gitignore
  .manifest.swo
  .manifest.swp
  .mcp.json
  AGENTS.md
  CHANGELOG.md
  CLAUDE.md
  GEMINI.md
  LICENSE
  LICENSING.md
  README.md
  SECURITY.md
  THIRD_PARTY_LICENSES.md
  c3.bat
  install.bat
  ... +3 more
  .claude/
    settings.local.json
  .codex/
    config.toml
  .gemini/
    settings.json
  .github/
    copilot-instructions.md
    workflows/ (2 files)
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
  .ruff_cache/
    .gitignore
    CACHEDIR.TAG
    0.15.12/
  .vscode/
    mcp.json
    settings.json
  cli/
    __init__.py
    _hook_utils.py
    c3.py
    docs.html
    edits.html
    hook_auto_snapshot.py
    hook_c3_signal.py
    hook_c3read.py
    hook_edit_ledger.py
    hook_edit_unlock.py
    hook_filter.py
    hook_ghost_files.py
    hook_pretool_enforce.py
    hook_read.py
    hook_session_stats.py
    ... +9 more
    commands/ (3 files)
    tools/ (17 files)
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
    screenshots/ (11 files)
  guide/
    bitbucket.html
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
    services/ (13 files)
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
    benchmark_dashboard.py
    bitbucket_client.py
    bitbucket_credentials.py
    claude_md.py
    compressor.py
    context_snapshot.py
    conversation_store.py
    doc_index.py
    e2e_benchmark.py
    e2e_evaluator.py
    ... +33 more
    bench/ (1 files)
  tests/
    test_aider_polyglot.py
    test_bitbucket_cli_smoke.py
    test_bitbucket_client.py
    test_bitbucket_credentials.py
    test_bitbucket_tool.py
    test_c3_shell.py
    test_cli_smoke.py
    test_e2e_benchmark.py
    test_edit_normalization.py
    test_enforcement_flip.py
    test_federated_graph.py
    test_ghost_files.py
    test_hub_server_smoke.py
    test_mcp_server_smoke.py
    test_memory_graph_api.py
    ... +11 more
  tui/
    __init__.py
    backend.py
    build.bat
    build.sh
    main.py
    theme.tcss
    screens/ (14 files)
    widgets/
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

- [architecture] File Memory system: FileMemoryStore in services/file_memory.py provides persistent structural index of so
- [ui] Left sidebar and right bar both support hover-to-open + pin. App state: sidebarPinned/rightBarPinned (localStorage 
- c3_delegate now supports allow_model_fallback/fallback_models and resolves nearest installed Ollama model when the reque
- ConversationStore sync now supports source='all|claude|imports', and sessions/turns persist normalized source labels for
- SessionManager.parse_claude_session_tokens now resolves Claude transcript dirs via project slug candidates and constrain