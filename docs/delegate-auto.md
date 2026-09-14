# Delegation without the agent deciding — subagent downshift and hints

`c3_delegate` only saves anything when an agent calls it, and agents rarely
do. When the delegate work began, 19 of 44,038 C3 calls were delegations.
Status: shipped in 2.140.0. It has two parts:

1. **Subagent downshift.** A native Claude Code `Agent` call with no `model`
   runs one tier below the session's own model. A C3 PreToolUse hook does
   this, with no decision needed from the agent.
2. **Delegation hints.** When the session's own work looks delegable, the
   c3_* response that did the work gets one `[c3:delegate-hint]` line naming
   the call to make instead.

## 1. Subagent downshift

**Why.** Measured 2026-09-14 over `~/.claude/projects` transcripts since
2026-07-01: 963 of 991 `Agent` calls had no `model`. On Claude Code 2.1.270
such a call runs on the parent's model. A probe with a Sonnet parent put all
of an `Explore` run on Sonnet, and a `general-purpose` run too. So a Fable or
Opus session paid its own rate for every search-and-summarise branch.

**How.**
- `cli/hook_agent_model.py` runs for `Agent|Task` in the PreToolUse
  dispatcher. The policy is in `services/agent_downshift.py`.
- It returns `hookSpecificOutput.updatedInput`: the call's own input with
  `model` filled in. Measured before building: with a command hook setting
  `model: "opus"`, `opus` appeared in `modelUsage`, and without the hook it
  did not.
- End to end on the branch: an Opus parent's model-less `Explore` call ran on
  Sonnet (6.6k tokens, $0.010).

**Policy.** Set in `.c3/config.json → delegate`:

| key | default | meaning |
|---|---|---|
| `agent_downshift` | `"one_down"` | Fable → Opus → Sonnet. `"off"` disables it. A fixed alias (`"sonnet"`) applies only when it is below the parent. |
| `agent_downshift_floor` | `"sonnet"` | Never below this. On the delegate evals, a Haiku scout took 28 turns where Sonnet took 8, and Haiku missed subtle write specs that Sonnet got right. A Sonnet session's subagents are left alone. |
| `agent_downshift_skip` | `["fork", "Plan"]` | A fork ignores `model`, and planning is where the parent's strength matters. |
| `agent_models` | `{}` | `{subagent_type: alias}`, an explicit choice per type, applied whenever it is below the parent. |

`C3_AGENT_DOWNSHIFT=0` turns it off for one shell.

**Never touched:**
- a call that passes `model`;
- a subagent whose definition in `.claude/agents/*.md` (project or user) sets
  `model:`, such as `c3-scout` and `c3-worker`;
- plugin agents (`plugin:name`);
- non-Claude hosts.

**The parent's model.** No hook payload carries it: SessionStart,
UserPromptSubmit and PreToolUse were checked. The hook reads it, in order,
from:
1. the transcript's last assistant row (`message.model`);
2. the last model this project saw in the past 14 days
   (`.c3/agent_downshift_state.json`);
3. Claude settings' `model`.

If none gives a model, the call is left unchanged. The first `Agent` call of
a brand-new session has no assistant row yet, so it relies on step 2.

**Rollout.** `c3 install-mcp` registers the `Agent|Task` matcher. The hub
adds it to every installed project at startup, so no project has to
reinstall.

**Measure it.** Each model-less call writes one telemetry row: an
`agent_downshift` row when a model was applied, or an `agent_downshift_skip`
row with the reason. `c3_status` shows `[delegate-auto:7d] N of M model-less
subagent call(s) moved to a lower model`.

## 2. Delegation hints

`services/delegate_hints.py` keeps a per-session window, wired into the
c3_edit, c3_read (symbol or line reads, not maps), c3_search and c3_delegate
MCP tools.

| kind | fires when | suggests |
|---|---|---|
| `write` | ≥ 5 c3_edit calls over ≥ 2 files and ≥ 4,000 characters of new text within 15 minutes, or one edit over 6,000 characters. Those characters are the session's own output tokens, the most expensive kind. | `c3_delegate(task=<spec>, write_paths=...)`. On the write suite, Sonnet passed 33/33 at about $0.03 a change. |
| `explore` | ≥ 6 source reads and searches over ≥ 4 files within 10 minutes, with no edit | `c3_delegate(task, scout=True)` or an Explore subagent, which is now downshifted as well |

**Limits:**
- Each kind waits `delegate.hint_cooldown_minutes` (45) before it can fire
  again.
- No hint fires while the session is already delegating that kind.
- A hint goes only on a successful response.
- `delegate.hints: false` turns them off.

**Measure it.** Each hint shown writes a `delegate_hint` telemetry row. A
matching c3_delegate call within 30 minutes writes a
`delegate_hint_followed` row, counted once per hint. Both appear on the
`[delegate-auto:7d]` status line. That line is the test of whether hints
change behaviour. If `followed` stays near zero, the hints are noise and
should be removed.
