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

- `action`: `code` (default, hybrid when embeddings are ready), `lexical`,
  `exact`, `files`, `semantic`.
- `expect.files`: any listed file counts as relevant. `expect.symbols`: graded
  separately; with `require_symbol` the symbol, not the file, decides.
- `expect.none: true`: the query has no valid answer; passing means 0 hits.
- `forbid_text`: strings that must never appear in the response body (the
  query's own echo in headers is ignored).
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
- `filters`: `{"path": "src/**", "lang": "go", "kind": "test"}` — passed to
  the tool as its `path` / `lang` / `kind` parameters.

## The engine under test (2.106.0, store 2.107.0)

The index lives in `.c3/index/index.sqlite` (`services/index_store.py`):
documents with a content hash, chunks with their content, and the FTS5 table.
`CodeIndex.refresh()` re-chunks only files whose hash changed and the watcher
calls it instead of rebuilding; a full build writes a temp store and swaps
it in. `index.json` from earlier releases is migrated on first load.

`code` queries go through SQLite FTS5 when this Python's SQLite has it
(`CodeIndex.lexical_engine == "fts5"`; `c3 stats` and `search-eval` report
it): BM25 over four weighted columns — path (3), symbol (6), kind (1), body
(1) — followed by small additive boosts (whole symbol name spelled out
+0.25, path tokens up to +0.15), an intent prior (+0.15 for test files when
the query names tests, for docs when it asks a how-to), and the weak recency
factor. Without FTS5, or with `search_engine: "tfidf"` in `.c3/config.json`,
the pre-2.106.0 TF-IDF scan runs with the same tokenizer.

The tokenizer keeps every identifier verbatim and adds its camelCase /
snake_case parts, digits included, no stemming: `parseIso8601` indexes as
`parseiso8601 parse iso8601`, so `iso8601`, `parse iso8601` and the whole
name all hit; `sha256`, `oauth2`, `v2`, `S256` are tokens. Synonyms come
only from `search_synonyms` in `.c3/config.json` (`{"endpoint": ["route"]}`).

### Hybrid fusion (2.108.0)

When the embedding index is ready the runtime attaches it to the code index
as its dense backend (`services/retrieval.RetrievalBackend`: anything with
`ready` and `candidates(query, limit)`), and `code` queries fuse the lexical
ranking with the dense candidates by Reciprocal Rank Fusion
(`score = Σ 1/(k + rank)`, `k = 60`, `search_rrf_k` to tune). Each list
contributes its top 20-50; exact-symbol matches keep their override after
fusion; `action='lexical'` asks for the BM25 ranking alone; `search_fusion:
"off"` disables it. The report line says `fusion=rrf` when it was active.

A dense index always has nearest neighbours, so the backend applies an
admission floor on cosine similarity before anyone sees its candidates:
`search_dense_min_score`, default 0.62 for nomic-embed-text with task
prefixes (measured on the fixture: unanswerable queries top out at 0.58,
real answers start at 0.70), 0.55 for other models until measured. Without
it, fusion turned every zero-result query into ten hits.

nomic-embed-text is embedded with its task prefixes (`search_document:` /
`search_query:`) since 2.108.0, in a new collection `code_embeddings_v2`;
the v1 collection is dropped best-effort on first init and its vectors
rebuild lazily.

### Reranker (2.109.0): measured, and off by default

`services/reranker.py` defines a reranker contract (`ready`,
`rerank(query, [(id, text)]) -> [(id, score)]`) and a FlashRank adapter
(ONNX cross-encoders, 4-22 MB, downloaded once into
`~/.c3/models/flashrank`; the `rerank` extra installs the package). With
`search_rerank: "auto"` the top 16 fused candidates of a natural-language
query (three or more words, at least two plain) are reordered by the model;
identifier queries are never reranked and exact-symbol matches keep their
place ahead of the block. `c3 search-eval --rerank on` attaches it
regardless of config so the two can be compared.

Compared, on 2026-09-03, with fusion on:

| Suite | No reranker | ms-marco-TinyBERT-L-2 | ms-marco-MiniLM-L-12 |
|---|---|---|---|
| Golden (C3, 26 cases): recall@1 / recall@3 / MRR | 0.800 / 0.967 / 0.878 | 0.767 / 0.933 / 0.857 | 0.733 / 0.967 / 0.844 |
| Golden p95 latency | 404 ms | 513 ms | 1142 ms |
| Fixture: recall@1 / recall@3 / MRR | 0.933 / 1.0 / 0.961 | 0.900 / 0.983 / 0.943 | 0.900 / 1.0 / 0.947 |

Both models make every aggregate worse. They are trained on web passages
and mis-rank code chunks — a limiter's `Allow` over a session store for a
question about expiring sessions is the shape of the errors. The reranker
therefore stays off; the contract and the flag exist so a code-trained
cross-encoder, or an LLM judge, can be measured the same way later.

### What the agent sees (2.110.0, P5)

The last phase changed the response, not the ranking:

- A zero result names the next move and never repeats the query: `code`
  points at `exact` (a literal or regex) and `files` (a filename), or at
  just one of them when the query looks like a path or a regex; `exact`
  points at `ignore_case=True` and `code`; `files` at a glob and `exact`.
  An active filter is named so it can be dropped.
- `exact` collects every matching file and prints definitions first: files
  whose symbol table defines the identifier, then files with a declaring
  line (`def` / `class` / `fn` / `func` / `type` / `const` / a top-level
  assignment, with `export` / `pub` / `static` prefixes), then the rest;
  within a group source, config, docs, tests, then path order. The header
  reads `--- path --- [definition]` on the first two groups. C-family
  functions without a keyword rely on the symbol table. The fixture gate
  `exact_definition_first` and the golden case
  `exact_definition_first_code_index` pin it.
- `files` and `exact` honour `top_k` up to 50; `code`, `lexical` and
  `semantic` keep 10. The 2400-token response cap is unchanged.
- Chunk headers end in a provenance tag: `[lexical]`, `[dense]`,
  `[lexical+dense]`, `symbol+` for an exact-symbol match, `, reranked` when
  the reranker reordered the block; `semantic` hits end in `[dense]`.
  `CodeIndex.search` exposes the same as `via` / `reranked`. `parse_hits`
  accepts the tag on chunk and exact headers, so a case file needs no
  change.

Fixture after P5: recall@1 0.965, recall@3 1.0, MRR 0.980 (57 scored). The
golden aggregates moved with the corpus and the new case, not with the
ranking (no ranking code changed in P5).

## Known gaps this suite documents (2.106.0)

- Semantic cases run only where Ollama and chromadb are present.
- The golden suite's remaining failures are natural-language queries where a
  test file outranks the source it exercises; the intent prior helps when the
  query names tests, and `kind=source` settles it, but an unfiltered how-to
  question still lands on tests sometimes. Hybrid fusion (P3) is next.

Closed in 2.105.0 (P1) and 2.106.0 (P2), all gated `must_pass`: the
oversized `Ledger` class chunk comes back as a window; `exact` has
`ignore_case`; `files` is a real filename search; `v2` and `S256` are tokens;
`path` / `lang` / `kind` filters work on every action.
