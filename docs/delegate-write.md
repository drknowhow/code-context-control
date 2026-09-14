# Delegate write mode — `c3_delegate(write_paths=...)`

The stronger model decides a change; a cheaper Claude makes it. The caller
names the files the delegate may edit or create, describes the change, and
gets back a diff to review and test. Status: shipped in 2.139.0
(`services/delegate_write.py`, `cli/tools/delegate.py:_handle_claude_write`,
`cli/hook_access_guard.py:worker_run`).

## When to use it

- The change is already decided and can be written down: which files, what
  behaviour, the edge cases. Rename across three files, fix a named bug, add
  a function to a spec, write tests for a given function, add docstrings.
- You will check the result yourself. The worker cannot run commands, so it
  cannot run the tests; you run them after reading the diff.

Not for open-ended work ("improve this module"), anything that needs
commands, or files the guard holds for a human (instruction docs, `.claude/`,
`.mcp.json`) — make those edits yourself. In Claude Code, multi-step work that
needs to run tests fits the `c3-worker` subagent better.

```python
c3_delegate(
    task="Rename calc_total to order_total: its definition and every use. No alias.",
    write_paths="inv/orders.py, inv/report.py, tests/test_orders.py",
)
```

## What the caller gets

```
[delegate:write] claude medium (sonnet) changed 3 file(s) in 21.4s. Review the diff and run the tests before relying on it.
  M inv/orders.py (+1 -1)
  M inv/report.py (+2 -2)
  M tests/test_orders.py (+2 -2)
Refused (1): Edit inv/money.py

--- worker report ---
<the worker's own summary: what it changed, what it could not do, assumptions>

--- diff ---
<unified diff of every change, capped at delegate.claude_write_diff_max_chars>
```

C3 builds the file list and the diff itself, from pre-images the guard hook
saved before each file's first write, so they are right even when the
worker's report is not. Each changed file is logged to the edit ledger
(tags `c3_delegate`, `claude`, the tier). A worker stopped at its deadline
answers `[delegate:write-failed]` and still lists what it changed.

## Parameters and config

| | |
|---|---|
| `write_paths` | Comma-separated project-relative globs. `**` crosses directories, `*` does not. A bare name is that file at the project root, never a basename match anywhere. Absolute paths, `..`, `.git/` and `.c3/` are refused before anything runs. At most 50 entries. |
| `tier` / `model` | As for any Claude delegation. Default `delegate.claude_write_default_tier` (`medium`, Sonnet). |
| `file_path` / `context` | Packed into the prompt as usual; the worker can also Read files itself. |
| `backend` | `claude`, or `host` when the host maps to Claude. Anything else is an error: write mode never cascades. |
| `delegate.claude_write_timeout` | 600 s, but always inside the MCP client's own limit when `MCP_TOOL_TIMEOUT` is set (Claude Code: 120 s, so 105 s). A client that gives up on a call does not stop the worker, so C3's deadline has to fire first. |
| `delegate.claude_write_diff_max_chars` | 24000. The ledger keeps every change regardless. |

A literal write path that Access Guard already refuses (`.env`, `CLAUDE.md`)
is dropped before the worker starts. The answer names it and the rule, and
the worker never sees it in its write set. If every path is refused, nothing
runs (`[delegate:blocked]`).

Telemetry: the call's detail carries `mode: write`, `files_changed`,
`lines_added`, `lines_removed` and `denied`. `delegate_by_backend` counts
modes and files changed per row, and `c3_status` adds `N file(s) written` to
its `[delegate:7d]` line.

`allow_write_delegation` is not needed: that opt-in exists for backends whose
writes C3 cannot fence (gemini, grok write mode). This one runs under Access
Guard in-band.

## The fences

The worker is `claude -p --restricted --strict-mcp-config --tools
Read,Grep,Glob,Edit,Write --permission-mode dontAsk --settings <json>`, in
the project directory, with no MCP servers and no user or project settings.

1. **Permission rules** (Claude Code). `Edit(./<glob>)` allow rules are the
   write set. `Read()` and `Edit()` denies come from every Access Guard deny,
   read_only, confirm and mask rule; deny beats allow, so a broad write set
   cannot reopen them. Measured on 2.1.270 before building: with `Edit(./a.py)`
   and `Edit(./sub/**)` allowed, edits to `a.py`, `sub/c.py` and a new
   `sub/new.py` landed; `b.py`, a new `top.py` and `.env` were refused, and
   `permission_denials` named each one.
2. **`--restricted`**. No command-running tools, file tools confined to the
   project, writes to settings, git and tool-configuration files refused.
3. **The guard hook in worker mode** (`hook_access_guard.py --worker-state`),
   on every file tool. Reads go through the same guard a scout gets. A write
   must pass, on the canonical path: inside the project; not under `.git/` or
   `.c3/`; not a credential-vault file; Access Guard with **no grants and no
   filed override requests** (a confirm hold is a refusal here: a headless
   worker cannot wait for a human, and the caller can make that edit itself);
   the write set; and no other agent holding the file's lock (the caller's own
   leases do not block it). Any error in the hook denies.
4. **Pre-images.** The same hook saves each file's content before its first
   write in the run. C3 diffs against them afterwards, so the caller sees
   exactly what changed, whatever the worker says.

## Measurements — why Sonnet is the default

2026-09-14, Claude Code 2.1.270, the write suite below. 13 cases: 10 run twice,
the 5 hardest (a subtle bug fix, a two-file change, two feature-plus-tests
cases, the `.env` trap) run 3–4 times. Every run starts from a fresh copy of
the fixture. `MCP_TOOL_TIMEOUT` was unset, so the durations are what the
workers took, not a cap.

| target | runs | pass | cost per run | wall p50 | wall max | turns | tokens the lead reads |
|---|---|---|---|---|---|---|---|
| `claude:small` (Haiku, thinking capped at 1024) | 33 | 30/33 | $0.032 | 23.2 s | 74.4 s | 6.0 | 836 |
| `claude:medium` (Sonnet) | 33 | **33/33** | $0.034 | **15.4 s** | 67.5 s | 5.1 | **582** |
| Haiku, no thinking cap (hard cases only) | 10 | 8/10 | $0.039 | 42.0 s | 68.5 s | 6.6 | 1000 |

On the five hard cases alone: Haiku 14/17 ($0.037 a run), Sonnet 17/17 ($0.044).

- **Haiku failed where the spec was subtle.** `csv-import-feature` 2 of 4 runs
  (the error's line number ignored blank lines; its own tests asserted the
  same wrong number), `fix-negative-parse` 1 of 3 (`$-0.05` parsed as +5).
  Every Sonnet run passed.
- **Removing Haiku's thinking cap did not help:** 8/10, slower, and it failed
  the same CSV case both times. The cap stays.
- **Cost is a wash; the lead's reading is not.** Sonnet cost about the same
  per run and finished faster. Its diffs were smaller: first-round tests for
  `Stock.remove` were 40 lines against Haiku's 151, and the answers the lead
  has to read averaged 582 tokens against 836. Reading them costs the lead
  model at its own price.
- **Traps held on both.** No run changed a file outside its write set or
  touched `.env`. Both reported the part they could not do. Before the
  up-front guard check, a Haiku worker spent 17 turns and 74 s retrying the
  refused `.env` write.
- **Inside the 120 s client limit.** The slowest run took 74 s; nothing came
  near the 105 s worker deadline.

So `claude_write_default_tier` is `medium`. `tier='small'` still works, for a
caller that accepts a lower pass rate on subtle specs.

**Dogfood.** Part of this change was made through write mode itself. From
this branch, a Sonnet worker got a three-file spec: fold `mode` and
`files_changed` into `delegate_by_backend`, add a test, and add the files
count to the status line. It returned a correct +26 −2 diff in 28 s for
$0.09; the tests passed without edits.

## Evaluating it — the write suite

`tests/delegate_eval/write_suite.jsonl`, run by
`python -m services.bench.delegate_write_eval --targets claude:small,claude:medium --record run.json`.
Each case is a change written the way a lead would hand it over, applied to a
fresh copy of `tests/delegate_eval/fixtures/write_project` (a small inventory
package with its own tests, plus a `.env` canary). After the worker finishes,
hidden checks from `fixtures/write_checks` are copied in and run:

| Check | Passes when |
|---|---|
| `pytest` | these paths pass (`@checks/x` is a hidden check) |
| `mutants` + `mutant_pytest` | each mutant applied alone makes those tests fail — tests the worker wrote must catch real bugs |
| `min_tests` | the file has at least n tests |
| `max_count` | a pattern occurs at most n times (a rename left nothing behind, a helper really replaced the duplicates) |
| `script` | a check script exits 0 (docstrings-only: the AST is unchanged apart from docstrings) |
| `unchanged` | a path inside the write set stayed byte-identical (`.env`) |
| `report_any_match` | the worker's report names what it could not do |

Always: every file outside the write set is byte-identical. Trap cases ask for
something the worker must not do — a change outside the write set, a write to
`.env` — and pass only if the rest is done and the report says what was left.

`tests/test_delegate_write_eval.py` runs every case against its reference
change in `fixtures/write_gold` (must pass) and against the untouched fixture
(must fail), so a check that cannot fail is caught in CI, without a model.
