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
| index stats | files, chunks, chunks over the default budget, build seconds, `file_memory_coverage` (files tracked by `file_memory` ÷ files indexed) |

`file_memory_coverage` was called `exact_coverage` in 2.103.0, when `exact`
iterated `file_memory` and could only see the files an agent had read (427 of
513 on this repo). Since 2.105.0 `exact` walks the indexed manifest; the stat
stays as a health signal for `file_memory` itself.

Ranks must repeat exactly between runs and machines, or floors mean nothing.
Two tie-breakers used to be environmental: the recency factor (mtimes after a
checkout differ by microseconds, enough to order an exact tie) and the
co-occurrence synonym map (built from a bare `set`, so its ties followed the
per-process hash seed; the first CI run of the suite read recall@1 0.708 on
one cell and 0.771 on the other eight). The harness pins every fixture mtime
and the indexer now sorts tokens before counting co-occurrence;
`test_ranking_is_deterministic_across_hash_seeds` re-runs the suite in a
subprocess with a forced `PYTHONHASHSEED` and requires identical ranks.

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

- `params`: extra `handle_search` keyword arguments for the case, e.g.
  `{"ignore_case": true}`.

## Known gaps this suite documents (2.105.0)

- `digits_v2_migration`, `digits_s256_challenge_method`: the tokenizer drops
  digits, so `v2` and `S256` vanish (P2).
- `filter_tests_only`: no `kind`/`path`/`lang` filters (P2).
- Semantic cases run only where Ollama and chromadb are present.

Closed in 2.105.0 (P1) and now gated `must_pass`: the oversized `Ledger`
class chunk comes back as a window; `exact` has `ignore_case`; `files` is a
real filename search (exact name, glob, substring), so `Invoi`, `limit`,
`configs/*.yml` and the masked `customers.csv` all resolve; co-occurrence
synonyms are off by default.
