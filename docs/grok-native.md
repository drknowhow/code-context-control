# Native Grok Build integration

C3 supports xAI's Grok Build CLI (`grok`) as an agent host and as a
`c3_delegate` backend. The profile key is `grok` (alias `grok-build`). Like the
Codex profile, installation is project-scoped: C3 writes files inside the
project and nothing under Grok's home directory (`~/.grok`).

A Grok project can also carry Claude Code, Codex or Antigravity. Installing one
host keeps the others' MCP configuration, hooks and instruction documents.

## Install and activate

```sh
c3 install-mcp --ide grok
grok --trust          # first launch in the project folder: grants folder trust
c3 doctor --ide grok  # confirms CLI, hooks, MCP entry and trust
```

Later sessions start with plain `grok`.

`c3 init --ide grok` does the same as part of a first-time setup. A project that
already has `.grok/config.toml` or a `.grok/` directory is detected as Grok
automatically.

**Folder trust is required.** Grok loads nothing a project supplies (MCP servers,
hooks, `AGENTS.md`, skills) until the folder is trusted. Launch `grok --trust`
once in the project, or run `/hooks-trust` inside a Grok session. Grok records trust in
`~/.grok/trusted_folders.toml`. C3 never writes that file: the installer prints
the trust step, and `c3 doctor --ide grok` reads the store to tell you whether
the step is done. Until then the install is on disk but inactive.

Trust covers subdirectories of the same repository. A nested git checkout, such
as a worktree inside the project, is a separate workspace and needs its own
`grok --trust`.

`c3 doctor --ide grok` makes no model call. It reports the `grok --version`
output, whether C3's hooks are installed, whether `.grok/config.toml` has a C3
entry, and the folder-trust state. It warns when the installed Grok major version
differs from the hook contract C3 was built against.

## What gets written

| Path | Contents | Ownership |
|---|---|---|
| `.grok/config.toml` | `[mcp_servers.c3]`: the C3 launcher, `--host grok`, `startup_timeout_sec = 60` | Merged. Other servers and your own keys are kept |
| `.grok/hooks/c3.json` | C3's hook handlers | C3 owns the whole file and replaces it on reinstall |
| `AGENTS.md` | The C3 managed block | Only the block between the C3 markers is refreshed |

The MCP table is the same `[mcp_servers.<name>]` shape Codex uses, so the
Hub's MCP manager can add, update, disable (`enabled = false`) and remove servers
in it.

C3 never edits `~/.grok/config.toml`. That includes its `[compat.claude]` and
`[compat.cursor]` switches, which decide whether Grok also imports Claude Code or
Cursor configuration. If you have turned those off, they stay off.

Access Guard's builtin agent-config tier holds writes to `**/.grok/config.toml`,
`**/.grok/skills/**`, `**/.grok/agents/**` and `**/.grok/rules/**` for approval, the
same way it holds `.mcp.json` or `.codex/config.toml`. Hook files under
`**/.grok/hooks/**` are hook registration, so agent writes there are refused
outright, like `.claude/settings*.json`; `c3 install-mcp --ide grok` is how C3's
own hook file is written. Reads stay open. See [confirm-guard.md](confirm-guard.md).

## Hooks

`.grok/hooks/c3.json` registers C3's dispatcher for six events, all with matcher
`*`:

| Grok event | C3 route |
|---|---|
| PreToolUse | `pretool` (guards, deny, hints) |
| PostToolUse | `posttool` (edit ledger, bookkeeping) |
| SessionStart | `start` |
| PreCompact | `compact` |
| Stop | `stop` |
| SessionEnd | `end` |

UserPromptSubmit is not registered; see the limitations below. Every command
passes `--host grok`. Grok's native tool names (`read_file`, `search_replace`,
`write`, `run_terminal_command`, `grep`, `list_dir`) are translated to C3's
equivalents before the guards run, so enforcement, the edit ledger and artifact
history work as they do on Claude Code.

On Windows each hook command has the form
`powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand <base64>`.
Grok picks pwsh, PowerShell, bash or cmd to run hook commands, and a quoted
interpreter path breaks under some of them. The encoded form runs the same way in
all of them, including when the project path contains spaces.

Hooks cover Grok's own tool paths. They are not an OS sandbox. Server-side C3
access, confirmation, masking and credential rules still apply to every C3 tool
call.

## Known limitations

1. **MCP tools sit behind `search_tool` / `use_tool`.** Grok's model does not see
   C3's tools directly. It discovers them with `search_tool` and calls them with
   `use_tool`. Hooks, logs and Grok's UI show the qualified names `c3__c3_search`,
   `c3__c3_read`, `c3__c3_edit` and so on. The C3 block in `AGENTS.md` explains
   this to the model.
2. **PreToolUse hints arrive after the call.** A deny from a PreToolUse hook blocks
   the tool before it runs. An allowing hook's `additionalContext` (for example a
   "use c3_read instead" hint) reaches the model only after the call has already
   executed.
3. **No prompt-time memory recall.** Grok discards the context an allowing
   UserPromptSubmit hook returns, so C3's prompt recall injection cannot reach the
   model. C3 does not register that hook at all, which also saves a process spawn
   per prompt. Call `c3_memory(action='recall')` explicitly instead.
4. **The C3 block can load twice.** Grok reads `AGENTS.md` and also `CLAUDE.md`
   when both exist. In a project with both Claude Code and Grok installed, the
   model sees C3's instructions in both files. This costs context but is harmless.
5. **The Stop hook never prints.** Context returned from a Grok Stop hook makes
   the agent keep working. C3's Stop route still does its bookkeeping, but on Grok
   it emits nothing to stdout.
6. **Windows hook commands are encoded.** They read as base64 in
   `.grok/hooks/c3.json`, not as a plain Python command line. See Hooks above.

Conversation sync is not implemented for Grok yet (`supports_transcripts` is off
in the profile).

## Delegation backend

`c3_delegate(task=..., backend='grok')` sends work to Grok headless:

```sh
grok --prompt-file <tmp> --output-format json --tools read_file,grep,list_dir --max-turns N [-m model] --cwd <dir>
```

Grok is also part of the `backend='auto'` cascade (heavy tasks: codex, gemini,
grok, then ollama), for the task types in `grok_task_types`.
`c3_delegate(task='', task_type='grok_check')` runs `grok --version` and makes no
model call.

**Read-only (default).** Grok runs from a throwaway temp directory, which is
removed afterwards, with only `read_file`, `grep` and `list_dir` enabled. The
temp directory matters because `--tools` does not stop a trusted project's
`.grok/config.toml` MCP servers or `.grok/hooks` from loading. Running outside the
project means none of them start. Requested file context is compressed and
included in the prompt. Read-only answers are cached.

**Write mode** (`delegate.grok_allow_write = true`). Grok runs in the project
directory with `--yolo`, which auto-approves every tool call. That run does load
the project's trusted `.grok` MCP servers and hooks, because writing into the
project is the point. While Access Guard rules are active, write mode needs the
explicit `allow_write_delegation` opt-in, the same as the gemini and claude
backends. Write runs are never cached.

In both modes the child process drops the parent's host session identity
(`GROK_SESSION_ID` and Grok's hook variables included), Grok's auto-updater is off, and Grok's Claude Code and Cursor imports
are disabled through `GROK_{CLAUDE,CURSOR}_*_ENABLED=0`, so a delegate never picks
up another host's agents, hooks, MCP servers, rules or skills.

Settings live under `delegate` in `.c3/config.json` and in the Settings tab's
**Grok Build Integration** section:

| Key | Default | Meaning |
|---|---|---|
| `grok_enabled` | `true` | Master switch. The `grok` CLI must also be on PATH |
| `grok_model` | `""` | Empty uses the Grok CLI's own default model (no `-m`) |
| `grok_timeout` | `120` | Total seconds for one run |
| `grok_max_turns` | `8` | `--max-turns` |
| `grok_allow_write` | `false` | Write mode, described above |
| `grok_task_types` | `review, diagnose, improve, test` | Task types `auto` routes to Grok |

Grok uses its own login (for example a SuperGrok account). C3 stores no xAI key.

## Uninstall

```sh
c3 mcp-remove c3 --ide grok      # remove only the [mcp_servers.c3] entry
c3 init --clear                  # remove everything C3 wrote to the project
```

`c3 init --clear` removes the `[mcp_servers.c3]` table (deleting
`.grok/config.toml` only if nothing else is left in it), deletes
`.grok/hooks/c3.json`, strips the C3 block from `AGENTS.md`, and deletes `.c3/`.
`~/.grok/config.toml` and `~/.grok/trusted_folders.toml` are untouched. To
withdraw trust, use Grok's own commands.

## Troubleshooting

- **C3 tools are missing in Grok.** Check trust first: `c3 doctor --ide grok`. Then
  run `grok inspect` (or `grok inspect --json`) in the project. It lists the MCP
  servers, skills and rules Grok discovered and where each one came from.
- **The C3 server fails to start.** `grok mcp doctor c3 --json` starts the server
  the way Grok does and reports the error. The first start can be slow while C3
  loads its index, which is why the install sets `startup_timeout_sec = 60`.
- **Hooks don't fire.** Run `/hooks` inside Grok to open the Hooks tab. If C3's
  hooks are missing, the folder is not trusted (untrusted project hooks are
  skipped silently) or Grok was started from a different directory. After a
  reinstall, press `r` in the Hooks tab to reload hooks from disk, or start a new
  session.
- **A C3 edit to `.grok/` is waiting.** That is the agent-config confirm tier. Approve
  the request in the Hub or with `c3_override`. An agent write to `.grok/hooks/` is
  refused rather than held; rerun `c3 install-mcp --ide grok` instead.
- **A newer Grok breaks hooks.** `c3 doctor --ide grok` warns on a major-version
  change. The hook contract is pinned by fixtures captured from Grok 1.0.30
  (`tests/fixtures/grok/`).
