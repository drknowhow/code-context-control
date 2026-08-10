# AgentCI — local CI execution for agents

Run the repository's **real** CI on this machine, before pushing, and get
structured failures instead of a wall of log.

Guided by `AgentCI_Product_Architecture_PRD_Roadmap.md` (§34 MVP loop, §41
"start narrow"). This document describes what actually shipped.

---

## 1. Why

An agent that only learns whether CI passes *after* pushing burns a
round-trip, some Actions minutes, and a commit on every attempt. The loop we
want is:

```
edit → run CI locally → structured failure → fix → rerun what failed → full CI → push
```

The governing constraint is that C3 **does not define a second CI config**.
`.github/workflows/*.yml` stays the single source of truth. There is nothing to
keep in sync, and no local script that can drift from what CI actually runs.

---

## 2. The one rule that matters

> **`FULL_CI_PASS` is the only verdict that means "safe to push".**

Everything else is `PARTIAL_PASS`, and partial means *something did not run
here*. This distinction is the entire product. A tool that printed a green tick
after running a quarter of the matrix would be at its most dangerous exactly
when it is most used — at the moment someone decides to push.

| verdict | meaning |
|---|---|
| `FULL_CI_PASS` | every job in the repo ran **on this host** and passed |
| `PARTIAL_PASS` | nothing failed, but something did not run — another OS, an unsupported action, or your selection |
| `FAIL` | a job failed, timed out, or its dependency did |

The exit code follows: `c3 ci run` exits non-zero for anything but
`FULL_CI_PASS`, so a pre-push hook can gate on it.

### Job statuses

| status | meaning |
|---|---|
| `passed` / `failed` | ran here |
| `timeout` | killed at the step timeout — effect unknown, which is *not* a clean failure |
| `skipped` | a `needs:` dependency did not pass, so this never ran |
| `unsupported` | the job uses something we cannot faithfully reproduce (see §4) |
| `foreign` | `runs-on` targets a different OS; refused unless you opt in |
| `deselected` | not part of this run's selection |

---

## 3. Use it

### As an agent

```
c3_ci(action='inspect')                  # workflows, job DAG, what runs here
c3_ci(action='run')                      # everything, in dependency order
c3_ci(action='run', job='lint')          # one job (all its matrix cells)
c3_ci(action='failures')                 # structured {file,line,message}
c3_ci(action='rerun')                    # only what failed last time
c3_ci(action='logs', job='CI::lint')     # bounded tail of one job
c3_ci(action='runs')                     # history
```

### As a human

```bash
c3 ci inspect
c3 ci run [--job lint] [--allow-foreign] [--workflow CI] [--timeout 900]
c3 ci rerun
c3 ci failures
c3 ci logs "CI::lint" --tail 200
c3 ci runs
c3 ci inspect --json          # machine-readable; same data the Hub reads
```

### In the Hub

The **CI** tab shows every registered project's workflows, the job graph
(including what cannot run here), run history, live status while a run is in
flight, and structured failures inline. Logs open in a viewer.

---

## 4. What runs, and what refuses

A job is only executed when C3 can reproduce it faithfully. Anything else is
`unsupported` — never silently narrowed, because a job that quietly skipped
half of itself and reported PASS is worse than no local CI.

**Shimmed** — these `uses:` steps have a local equivalent of "the condition
already holds", so they are no-ops and say so in the log:

| action | why it is a no-op |
|---|---|
| `actions/checkout` | the working tree *is* the checkout |
| `actions/setup-python`, `setup-node`, `setup-go` | we run on the toolchain already on PATH |
| `actions/cache` | an optimization; skipping changes timing, not results |
| `actions/upload-artifact` / `download-artifact` | copied to `.c3/ci/runs/<id>/artifacts/` |

**Blocked** — the job is reported, visible in `inspect`, and never run:

- any other `uses:` action (it is somebody else's JavaScript in a runtime we
  do not have);
- an unresolved `${{ ... }}` expression. We resolve `matrix`, `env`, and a few
  `github.*` fields. `${{ secrets.TOKEN }}` quietly becoming `""` is precisely
  how a job passes locally and fails in CI, so it blocks instead;
- job-level `container:` or `services:`;
- a reusable workflow (`jobs.<id>.uses:`).

**Foreign runner** — `runs-on: macos-latest` on a Linux box is not that job.
By default it is refused. `--allow-foreign` runs it anyway, labels the result
`cross-OS`, and caps the verdict at `PARTIAL_PASS` forever.

> On this repository, from Windows: 3 of 15 jobs are runnable, 9 target
> another OS, and 3 use publish actions we do not execute. `c3 ci inspect`
> prints exactly that, which is the honest answer.

---

## 5. Execution details

- **Order.** Jobs run in topological order of `needs`. `needs` is scoped to its
  own workflow — two workflows may each define `build`, and they are unrelated.
- **Matrix.** `strategy.matrix` is expanded to one job instance per cell,
  including `include`/`exclude`. Each cell resolves its own `runs-on`.
- **Shell.** Git Bash when present (matching what most `run:` blocks assume),
  otherwise the platform default. The shell used is recorded per step.
- **Environment.** Workflow → job → step env, layered. `CI=true` is set because
  that is true. `GITHUB_ACTIONS` is deliberately **not** set: we are not GitHub
  Actions, and tools that branch on it would take a path this runner cannot
  reproduce.
- **Timeouts.** 900 s per step by default. A timed-out step kills its whole
  process tree (`taskkill /F /T` on Windows) and marks the job `timeout`.
- **Identity.** A job's `key` is `<workflow>::<id>`, e.g.
  `CI::test (ubuntu-latest, 3.12)`. A selector accepts the key, the id, or a
  bare job name (which selects all of its matrix cells).

---

## 6. Storage

Everything lands under `.c3/ci/`:

```
.c3/ci/index.jsonl                 one line per run — the history
.c3/ci/runs/<run_id>/run.json      the full record
.c3/ci/runs/<run_id>/<job>.log     per-job log, truncated at 200k chars
.c3/ci/runs/<run_id>/artifacts/    whatever upload-artifact named
```

JSONL rather than the SQLite the spec suggested (§17): it matches the
convention every other C3 ledger already uses (`edit_ledger.jsonl`,
`session_stats.jsonl`), needs no migration story, and the query patterns here
are "latest run" and "runs, newest first".

Each run records a **fingerprint** — commit sha, branch, and how many files
were uncommitted. A dirty tree is the normal case for an agent mid-edit, so it
is recorded, never refused; it is how you later tell whether a green run still
describes the code in front of you.

---

## 7. Deliberately not built

Straight from the plan's own §41 "do not begin with" list:

- distributed / remote runners (PRD 6)
- a GitHub App or check-run status bridge (PRD 5)
- complete GitHub Actions emulation — no `if:` evaluation, no expression
  functions, no composite actions, no containers
- multi-CI support (GitLab, CircleCI)
- test-impact prediction and CI intelligence (PRD 7)
- caching and content-addressed reuse (PRD 4)

`if:` conditions deserve a specific note: they are parsed and reported but
**not evaluated**, so a step guarded by `if: github.event_name == 'push'` runs
locally regardless. That is a known over-run — visible in the log rather than
silent, which is the safer direction to be wrong in.

---

## 8. Related

- `docs/enforcement.md` — tool discipline
- `docs/agent-locks.md` — agent leases
- `AgentCI_Product_Architecture_PRD_Roadmap.md` — the full product spec this
  implements a slice of
