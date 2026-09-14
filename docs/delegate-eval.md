# Delegation evaluation — `c3 delegate-eval`

A harness for what `c3_delegate` hands back, and what it cost to get it. It
exists so routing defaults — which backend, which model tier — are decided
by numbers. Measured 2026-09-14 over 65 projects: 19 delegations since July,
10 of the 12 with a logged outcome errored, and none of the telemetry could
say which backend or model had answered.

## What it measures

Every case in `tests/delegate_eval/gold_suite.jsonl` is a bounded task sent
through `cli.tools.delegate.handle_delegate`, the function the MCP tool calls.
The answer is graded by regex checks, all case-insensitive:

| Check | Passes when |
|---|---|
| `must_match` | every pattern matches the answer |
| `any_match` | at least one pattern matches |
| `must_not_match` | no pattern matches |
| `max_words` | the answer has at most this many words |

A case has a gate:

- **core** — the context (from `contexts/<file>`) or a `file_path` into the
  fixture project carries everything the answer needs. `file_path` cases
  measure how C3 packs a file for the delegate, not only the model.
- **lookup** — no context and no `file_path`. Only a delegate that reads
  the fixture project itself can pass. Aggregated apart.

A delegate status that means no answer came back (`error`, `timeout`,
`blocked`, `disabled`, `unavailable`, `degraded`) grades as **error**, never
as a wrong answer, so a broken backend does not read as a weak model.

Per target the report gives the core and lookup pass rates, pass rate per
task type, total and mean cost (when the backend reports one), token totals,
wall-time p50/p95, which models answered, and the ids that failed or errored.

## Targets and tiers

A target is `backend` or `backend:tier`, e.g. `ollama`, `codex`,
`claude:small`. When several tiers of one backend run together, the report
names, per task type, the **cheapest passing tier**: the lowest mean cost
among tiers whose pass rate reaches the floor (default 0.8) and sits within
0.1 of that backend's best tier. No tier qualifying means no recommendation.

Append `+scout` to run a target with `scout=true` (`claude:small+scout`): the
delegate may read the fixture project itself, which is what the lookup cases
need. Every live run writes a canary into the project copy's `.env`;
`lookup-env-secret` fails any answer that contains it, so a scout that reaches
a guard-denied file fails the suite.

## Running it

```bash
# live: every case costs what the backend costs
c3 delegate-eval --targets claude:small,claude:medium,claude:large --record run.json

# a subset of cases, with delegate config overridden for the run
c3 delegate-eval --targets ollama --cases explain-slice,review-toctou \
  --config '{"allow_model_fallback": false, "preferred_model": "gemma3:12b"}'

# grade a recorded run again (free, deterministic)
c3 delegate-eval --replay run.json

# CI-style gate: exit 1 when a target's core pass rate is under the floor
c3 delegate-eval --replay run.json --floor 0.8
```

Live runs work on a throwaway copy of `tests/delegate_eval/project`, so a
delegate that reads or writes the project never touches the checkout. Pass
`--allow-write-delegation` to measure a write-capable backend on a machine
with Access Guard rules.

## Keeping the checks honest

`tests/delegate_eval/replay_fixture.json` holds a hand-written correct answer
(`reference`) and a plausible wrong answer (`wrong`) for every case.
`tests/test_delegate_eval.py` requires every reference answer to pass and
every wrong answer to fail. A new case needs both.

## Telemetry

Since 2.132.0 every `c3_delegate` response writes a `detail` to
`.c3/tool_telemetry.jsonl`: `host`, `backend`, `backend_requested`,
`task_type`, `outcome`, `wall_ms`, and when known `tier`, `model`, `mode`,
`confidence`, `cascade`, `input_tokens`, `output_tokens`, `cached_tokens`,
`cost_usd`. `aggregate_tool_telemetry` folds them into `delegate_by_backend`
(calls, outcomes, ok rate, cost, tokens, models, task types, wall p50/p95),
with health probes counted apart.
