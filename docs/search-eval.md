# Search evaluation — `c3 search-eval`

A relevance harness for `c3_search`. It exists so a ranking change can be
argued with numbers and so a regression has somewhere to fail. Until 2.103.0
the search tests covered wiring only (access guard, federation, headers); a
change that put the wrong file first passed every test.

## What it measures

Every case runs through `cli.tools.search.handle_search`, the same function
the MCP tool calls, with `top_k=10` and the agent-default `max_tokens=1200`.
The response is parsed back into ordered hits and graded:

| Metric | Meaning |
|---|---|
| `recall_at_1/3/10` | share of scored cases whose expected file appears within rank n |
| `mrr` | mean of 1/rank of the first expected file (0 when absent) |
| `symbol_recall_at_3` | for cases naming a symbol: the symbol itself within top 3 |
| `zero_result_accuracy` | share of "no valid answer" cases that returned nothing |
| `latency_p50_ms`, `latency_p95_ms` | wall time of `handle_search` per case |
| index stats | files, chunks, chunks over the default budget, build seconds, `exact_coverage` (files `exact` can see ÷ files indexed) |

`exact_coverage` is reported because `exact` iterates `file_memory`, not the
index; on a real project the two sets differ (427 vs 513 on this repo when
the harness was written).

## Two suites

**Fixture** (`tests/search_eval/fixture_suite.jsonl`) — a synthetic repo,
`tests/fixtures/search_eval_repo` (Python, TypeScript, Go, Markdown, YAML,
CSV). It is copied to a temp dir and indexed from scratch on every run, so
the numbers are deterministic. It carries a mask rule on `data/**` and a
canary column, so a masked value reaching a response fails the suite. This
suite gates CI through `tests/test_search_eval.py`.

**Golden** (`tests/search_eval/golden_c3.jsonl`) — real queries against the
C3 source tree, run against the live `.c3` index and `file_memory` exactly as
an agent sees them. Environment-bound, not in CI:

```
c3 search-eval --suite golden            # from the repo root
c3 search-eval --suite golden --semantic on   # also builds embeddings (Ollama)
```

Authoring a golden suite for another repository is the same JSONL format with
`"repo": "<path>"` in the header; run it with `--suite path/to/suite.jsonl`.

## Gates

Each case declares one of:

- `must_pass` — a failure fails CI. Reserved for capabilities that work today
  and must keep working: exact symbol lookup, regex search, whole-token
  filename lookup, zero results for absent terms, mask canaries.
- `xfail` — known broken; `fixed_by` names the plan phase. A pass is reported
  as a warning so the case can be promoted and the baseline refreshed.
- `info` — measured, never gates.

Aggregates are gated by absolute **floors** in
`tests/search_eval/baseline_fixture.json`. Floors were set once when the
suite was created (three points under the measured value, zero-result
accuracy pinned at 1.0) and are not re-derived by a run: `--update-baseline`
rewrites aggregates and per-query status and keeps the floors unless
`--floors '{"mrr": 0.9}'` is passed. Raising a floor after a ranking
improvement is a deliberate, reviewed edit.

## Case format

```json
{"id": "sym_compute_total", "action": "code", "query": "compute_total",
 "expect": {"files": ["src/ledgerlite/billing/invoice.py"], "symbols": ["compute_total"]},
 "require_symbol": true, "k": 3, "gate": "must_pass", "tags": ["symbol"], "why": "..."}
```

- `action`: `code` (default), `exact`, `files`, `semantic`.
- `expect.files`: any listed file counts as relevant. `expect.symbols`: graded
  separately; with `require_symbol` the symbol, not the file, decides.
- `expect.none: true`: the query has no valid answer; passing means 0 hits.
- `forbid_text`: strings that must never appear in the response body (the
  query's own echo in headers is ignored).
- `filters`: reserved for P2; a case with filters is recorded as skipped.
- `k`, `top_k`, `max_tokens`: per-case overrides.

## Workflow

```
c3 search-eval                         # fixture suite, table + verdict, exit 1 on violation
c3 search-eval --json > report.json
c3 search-eval --update-baseline       # after an intentional ranking change
pytest tests/test_search_eval.py -q    # what CI runs
```

Adding a case: append a line, run `c3 search-eval --update-baseline`, commit
both files. `test_baseline_covers_every_case` fails until the baseline knows
the new id.

## Known gaps this suite documents (2.103.0)

- `code_ledger_class_oversize`: a class chunk larger than the token budget is
  skipped rather than windowed (P1).
- `exact_case_insensitive`: `exact` has no ignore-case option (P1).
- `files_substring`, `files_prefix_limit`: `files` matches whole path tokens
  (and camelCase segments) only; a partial name such as `Invoi` or `limit`
  finds nothing (P1). `files_glob_yml` passes only because the glob's tokens
  happen to be path terms; globs are not interpreted.
- `digits_v2_migration`, `digits_s256_challenge_method`: the tokenizer drops
  digits, so `v2` and `S256` vanish (P2).
- `filter_tests_only`: no `kind`/`path`/`lang` filters (P2).
- Semantic cases run only where Ollama and chromadb are present.
