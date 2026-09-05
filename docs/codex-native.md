# Native Codex integration

C3 supports Codex and Claude Code in the same repository. Installing one keeps
the other's MCP configuration, hooks, and instruction document. Installation is
project-scoped by default; machine-wide fallback configuration is opt-in.

## Install and activate

```sh
c3 install-mcp --ide codex
c3 doctor --ide codex
```

The installer merges C3's launcher and arguments into `.codex/config.toml`,
preserving user options, comments, environment subtables, and an explicit
`enabled = false`. New installations default to a 30-second MCP startup timeout
and a 60-second tool timeout. Existing timeout choices are retained. It installs
C3 handlers in `.codex/hooks.json`, keeping unrelated handlers, and refreshes
only the managed C3 block in `AGENTS.md`.

Review and trust the project configuration and hooks in Codex, then restart the
session. C3 never bypasses hook trust. `c3 doctor` distinguishes CLI support,
installed configuration, and hook activity observed for the current thread.
Installation alone is not evidence of activation. A changed hook configuration
invalidates the previous activity evidence. A CLI doctor run without a Codex
thread environment reports activity as unknown.

Use `--global-fallback` explicitly when a home-level Codex MCP fallback is wanted.
It honors `CODEX_HOME`. To install Claude Code alongside Codex:

```sh
c3 install-mcp --ide claude
```

No model call is made by `c3 doctor`; it inspects local configuration and CLI help.
It does not print configured environment values or prove that a client's existing
MCP connection is healthy. If that connection reports `Transport closed`, restart
the client and inspect its MCP error log. A successful fresh server process does
not establish why an older connection closed.

## Host and thread boundaries

Each installed MCP launcher passes an explicit `--host`. The process uses this
host for capabilities, transcript selection, and session attribution, even when
the repository's primary IDE preference names another host. Host-specific thread
IDs are read only for that provider. Delegated children discard inherited parent
thread IDs so a Claude child cannot impersonate its Codex caller.

Codex handoffs are stored under `.c3/host_sessions/codex/`, keyed by a hash of the
thread ID. C3 checkpoints its current session context after tool responses and
uses lifecycle events to offer a bounded, explicitly stale handoff to the same
thread and project. Another thread's latest snapshot is never restored
automatically. These are C3 project notes, separate from Cod's personal memory.

## Hooks and audited edits

The Codex dispatcher handles PreToolUse, PostToolUse, UserPromptSubmit,
SessionStart, Stop, PreCompact, and SessionEnd. It accepts Codex's native Bash and
apply_patch payloads. Multi-file patches check every source and destination,
including moves and deletions, before consuming any one-shot access grant.
Malformed patches and failed pre-tool guard checks deny the operation. Failed
patches are not recorded as successful edits.

Successful patches feed the existing edit ledger and artifact history. Codex
hook settings and `AGENTS.override.md` are included in agent-config protection.
These hooks cover supported client tool paths; they are not an OS sandbox.
Server-side C3 access, confirmation, masking, and credential rules still apply.

## Delegation and conversation data

Codex delegation uses `exec --json`, an explicit sandbox, and a prompt supplied
over stdin. It follows the user's configured model when no model was selected.
It does not use the removed `--full-auto` flag. Output and error streams are
drained concurrently; the total execution deadline bounds quiet reasoning turns.
The returned `thread.started` ID is persisted against the originating project
and caller. Resume requires that exact binding and never uses `resume --last`.
The existing explicit opt-in requirement for resume remains in place.

Conversation sync imports bounded user and assistant messages from Codex rollout
files under `CODEX_HOME/sessions`, after matching project metadata. It skips
reasoning records and duplicate message event views. Rollout files are an adapter
format that may evolve; unrecognized records are ignored.

Usage comes from the latest cumulative token-count event, not the sum of events.
Cached input is separated from total input to avoid double counting. Missing
usage is reported as unavailable; cost remains unavailable without a measured
price. Cumulative Stop snapshots are deduplicated by provider and session ID.

## Verification

The regression tests cover additive installs in either order, TOML preservation,
hook routing and denial, multi-file patch accounting, thread-bound handoffs and
resume, transcript isolation, usage accounting, and real stdio MCP initialization
and tool calls for both Codex and Claude. CLI argument compatibility can be
checked without calling a model. Interactive hook trust and model-backed
delegation still require verification in a normally trusted client session.

Relevant upstream contracts: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

### Local validation, 2026-09-05

- Windows, Python 3.14, Codex CLI 0.153.4: 183 targeted regression tests pass,
  including fresh stdio MCP calls for both hosts and execution of the Windows
  hook command with spaces, quotes, percent signs, and ampersands in paths.
- Repository-wide ruff and diff whitespace checks pass. Both sdist and wheel
  build successfully and pass `twine check`.
- The broad test run recorded 3,703 passes and five skips. Its two failures were
  a new test assertion subsequently corrected and verified in the targeted run,
  and the existing container integration test, whose image pull fails before
  executing the test job. The real CI lint job fails at the same Docker registry
  authentication step. An isolated anonymous Docker configuration did not clear
  the container-test failure. No FULL_CI_PASS is claimed; macOS matrix cells also
  need their own runner.
- This chat's existing C3 MCP transport remained closed, including validation
  and session-log calls; native fallback checks and this document preserve the
  verification record. Fresh MCP processes passed for Codex and Claude. This
  checkout's live Codex configuration still needs reinstall and normal hook
  trust/restart. Interactive trust and model-backed delegation were not tested.
