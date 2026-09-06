# File map evaluation — `c3 map-eval`

A gold harness for the file map C3 hands the agent (`c3_compress mode=map`,
bare `c3_read`, the `c3_search` prefetch). It exists so the compress
remediation phases (C1 canonical renderer, C2 mode retirement, C3 fold into
`c3_read`, C4 large files) can be argued with numbers against fixtures whose
answers were written by hand, and so a map that silently loses a symbol has
somewhere to fail.

The truth is the annotation, never the parser. Every fixture under
`tests/map_eval/fixtures/` carries, in `tests/map_eval/fixture_suite.jsonl`,
the list of symbols a correct map must show, with the kind, the qualified
name, the parameters and return type as written in the source, and the
1-based inclusive line range — each verified with `cat -n`. Grading
tree-sitter against tree-sitter would be circular; grading it against a
human reading of the file is not.

## What it measures

Each case is copied (or generated) into a temporary project directory, a
`FileMemoryStore` is built on it, and the map is rendered through the same
callable the MCP tool uses (see § The renderer under test). The map is
parsed with both grammars below and compared to the annotation.

Per case:

| Metric | Meaning |
|---|---|
| `raw_tokens`, `map_tokens`, `ratio` | cl100k tokens of the source and of the map; `map / raw` |
| `symbol_recall` | share of expected symbols present in the map |
| `symbol_precision` | share of rendered symbols that were expected (info: fields, members and other unannotated lines lower it) |
| `signature_completeness` | share of expected symbols with `params` whose rendered params equal the expected ones after whitespace normalization AND, when `ret` is expected, whose return type equals it |
| `range_accuracy` | share of matched symbols with the exact `line_start` and `line_end` |
| `determinism` | the map rendered twice from a cold store (record dropped, new `FileMemoryStore`) is byte-identical |
| `render_ms` | wall time of the first (cold) render, parse included |
| `chrome_share` | share of map characters that are emoji / variation selectors or runs of 2+ spaces after a non-space (column padding) — info |
| `record_bytes` | size of the persisted `.c3/file_memory` record — info |
| `must_not_contain_hits` | forbidden names (inner functions, fenced headings, nested keys) that came out as symbols |

Aggregates: pass rate per gate, `symbol_recall_mean` (all and must_pass
only), `symbol_precision_mean`, `signature_completeness_mean`,
`range_accuracy_mean`, `determinism_rate`, `chrome_share_mean`, `ratio`
mean/p50/p95, map `tokens` p50/p95/total, `render_ms` p50/p95, the grammar
each map was parsed with, `must_pass_failed`, `xfail_passing`, per-phase
counts.

Matching. Under the canonical grammar a symbol matches on `(kind, name)`
with the qualified name (`Worker.run`, `Registry.Entry.call`). Under the
legacy grammar it matches on the bare name only (`run`), as a multiset so two
classes sharing a method name need two rendered `run`s; the legacy type word
is mapped onto the canonical kind (function→F, method→M, class→C,
constant→K, variable→V, interface→IF, type→T, enum→E, struct→S, trait→TR,
impl→IM, heading→H, section→SEC, property→P) so the baseline is fair. Within
a name, the candidate with the exact range pairs first, then the same start
line, then whatever is left. Import lines (`I …`) are counted, never matched.

## Grammar

### Canonical (phase C1, `services/file_map.py: render_map`)

```
# <rel/posix/path> (<lines>L <lang>)
I <n> imports                                   # when > 6 imports
I <import statement text> [L<a>-L<b>]           # each, when <= 6
K <NAME> [L<a>-L<b>]                            # constant (Python UPPER_CASE, JS/TS/Go/Rust const)
V <name> [L<a>-L<b>]                            # other module-level binding
C <Class>(<bases>) [L<a>-L<b>]                  # bases only when present in source
  M <Class>.<method>(<full params>) -> <ret> [L<a>-L<b>]
  P <Class>.<prop> [L<a>-L<b>]                  # @property / TS getter
F <func>(<full params>) -> <ret> [L<a>-L<b>]
F async <func>(<full params>) [L<a>-L<b>]       # async prefix before the name (M async … for methods)
IF <Interface> [L<a>-L<b>]                      # ts interface
T <TypeAlias> [L<a>-L<b>]                       # ts/rs type alias
E <Enum> [L<a>-L<b>]
S <Struct> [L<a>-L<b>]                          # go/rs struct
TR <Trait> [L<a>-L<b>]                          # rs trait
IM <Type> [L<a>-L<b>]                           # rs impl block; methods nest as M <Type>.<fn>
H <heading text> [L<a>-L<b>]                    # markdown / html headings
SEC <key> [L<a>-L<b>]                           # yaml/json top-level keys, css selectors, html #ids
```

Every symbol line matches the spec regex, which the harness uses verbatim:

```
^(?P<indent> *)(?P<kind>I|K|V|C|M|P|F|IF|T|E|S|TR|IM|H|SEC) (?P<async>async )?(?P<name>.+?)(?:\((?P<params>.*)\))?(?: -> (?P<ret>.+?))? \[L(?P<a>\d+)-L(?P<b>\d+)\]$
```

Conventions the annotations follow (and C1 must, to pass):

- `params` is the parameter list as written, whitespace-normalized to single
  spaces, multi-line joined, never truncated, a trailing comma dropped;
  `self`/`cls` kept. `ret` only when the source has a return annotation.
  Go receivers fold into the qualified name (`M Server.Start(ctx
  context.Context) -> error`); the receiver is not a parameter.
- Ranges are the symbol's full extent: decorators and Rust attributes
  excluded, body included, one-line symbols `[L7-L7]`. A markdown or html
  heading spans its section — to the line before the next heading of the
  same or a higher level, else end of file — so a heading range is the read
  target for "that section".
- Nested classes qualify through every level (`C Registry.Entry`, `M
  Registry.Entry.call`). Rust methods nest under the implemented type, for
  inherent and trait impls alike (`IM Point` twice, `M Point.fmt`).
- `K` is a declared constant: a `const` keyword in JS/TS/Go/Rust or an
  UPPER_CASE assignment in Python/R. Everything else module-level is `V`.
- Not symbols: inner functions, class fields (unannotated, precision only),
  interface/enum members, nested yaml/json keys, css declarations, html
  elements without an id, `module.exports`, anything inside a code fence.
  Parens inside a heading, selector or key (`H Usage (advanced)`) are part
  of the name, not params.

### Legacy (today, `services/file_memory.py: _format_map`)

```
# <path> (<lines> lines, <lang>)
[<AI summary paragraph>]

  imports    <n> statements (collapsed)                      # when > 6 imports
  <a>-<b>     <emoji> [<access> ][async ]<type> <name>[(<params, cut at 60 chars>)]
            <first docstring line>
            <a>-<b>  <emoji> [<access> ][async ]<name>(<params>)   # child method
            <a>-<b>  <emoji> <type> <name>                          # other child
```

The legacy parser joins the physical continuation lines a multi-line
signature leaves behind (the params are copied with their newlines), reads
the type from the type word or, where the label has none, from the emoji,
strips the decorations that are formatting rather than identity (`h2: ` on
headings, ` (tag)` on html ids, `impl ` on impl names), qualifies child
lines with their parent, and never sees a return type — today's map has
none, so `signature_completeness` is 0 wherever `ret` is expected.

## Gates and phases

| Gate | Meaning |
|---|---|
| `must_pass` | a failure fails CI. Always on: `symbol_recall == 1.0`, no `must_not_contain` hit, deterministic. Plus every check the suite line switches on. |
| `xfail` | known broken; `phase` names the campaign phase that fixes it (`C1` renderer, `C2` mode retirement, `C3` fold, `C4` large files). A pass is reported as *XFAIL PASSING* so the case can be promoted. |
| `info` | measured, never gates (Java has no parser today; the case is the gold a future one must hit). |

A must_pass case must pass on the renderer at the commit that sets it.
The 2026-09-06 baseline against the legacy renderer: 4 must_pass
(`py_malformed`, `js_basic`, `json_basic`, `py_large_generated`), 10
xfail(C1), 1 xfail(C4) (`js_minified`), 1 info (`java_generic`).

Baseline floors and ceilings (`tests/map_eval/baseline_fixture.json`,
hand-set): floors `pass_rate_must_pass 1.0`, `symbol_recall_mean_must_pass
1.0`, `symbol_recall_mean 0.70`, `determinism_rate 1.0`,
`range_accuracy_mean 0.95`; ceilings `tokens_p95 10000`, `ratio_p95 1.10`,
`chrome_share_mean 0.15` (render time is reported, not gated — CI runners
are not a timing bench). Two per-case comparisons
ride along: a case whose `symbol_recall` fell below its baseline value
(violation for must_pass, warning otherwise — the campaign only moves recall
up), and the campaign's token gate — *never more tokens than today's map for
the same file* — applied where the baseline map was complete (recall 1.0):
`map_tokens` above the baseline is a violation for must_pass and a warning
otherwise. A map that missed symbols is no bar to clear, so `yaml_basic`
(10 tokens, nothing rendered) sets no ceiling for C1.

## Checks vocabulary

| Check | Meaning |
|---|---|
| `signature_completeness: <rate>` | gate the case when its measured completeness is below the rate (1.0 = every annotated signature exact) |
| `range_accuracy: <rate>` | gate the case when the share of exact ranges is below the rate |
| `max_map_tokens: <n>` | gate the case when the map exceeds `n` cl100k tokens (`js_minified` uses the 600-token prefetch cap) |

Both rate metrics are measured for every case; they gate only where the
suite line names them. Cases that fail them on the legacy renderer are
`xfail(C1)` rather than switched off, so C1 is graded by the same line.

## Case format

```json
{"id": "py_basic", "file": "py_basic.py", "gate": "xfail", "phase": "C1",
 "expected": [{"kind": "M", "name": "Worker.run", "params": "self, job: dict", "ret": "bool",
               "line_start": 34, "line_end": 36},
              {"kind": "F", "name": "gather_all", "async": true, "params": "urls: list", "ret": "list",
               "line_start": 60, "line_end": 63}],
 "must_not_contain": ["wrap"],
 "checks": {"signature_completeness": 1.0, "range_accuracy": 1.0},
 "tags": ["python", "ast"], "why": "..."}
```

`file` names a fixture under `tests/map_eval/fixtures/`; `generator` (+
`params`) names a seeded generator in `tests/map_eval/generators.py` whose
`Generated` carries its own `expected` and `must_not_contain`, computed from
the layout it produced (a 20 KB single line and a 250 KB / 400-function
module are not checked in as literals). `params: ""` on an expected symbol
means "no parameters, and I want that checked".

## Adding a fixture

1. Write the source under `tests/map_eval/fixtures/<case>.<ext>`, 20–80
   lines, exercising one thing the map must get right. Keep every text-mode
   `open()` / `read_text()` in a Python fixture on `encoding="utf-8"` —
   `tests/test_windows_reliability.py` scans fixtures too. The directory is
   excluded from ruff (one fixture is deliberately malformed).
2. `cat -n` the file and write the suite line by hand: every symbol's kind,
   qualified name, params/ret as written, exact range; the names that must
   not become symbols; the checks to gate on.
3. Run `c3 map-eval` (from a source checkout: `python -m cli.c3 map-eval`).
   If the case passes today, `gate: must_pass`; if the renderer misses it,
   `gate: xfail` with the phase that will fix it and a one-line finding in
   `why`. Do not weaken the annotation to make the renderer pass.
4. `c3 map-eval --update-baseline` so the baseline knows the case
   (`test_baseline_covers_every_case` fails until it does). Floors and
   ceilings are kept from the file; pass `--floors` / `--ceilings` JSON to
   move them, deliberately.

## Workflow

```
c3 map-eval                       # fixture suite, compare to the bundled baseline, exit 1 on violation
c3 map-eval --json                # full report as JSON (per-case missing / extra / misses lists)
c3 map-eval --suite path/to.jsonl --baseline path/to.json
c3 map-eval --update-baseline --floors '{"symbol_recall_mean": 0.9}' --ceilings '{"tokens_p95": 4000}'
python -m pytest tests/test_map_eval.py -q      # the CI gate: same suite, same baseline
```

When a phase lands: run the suite, read the *XFAIL PASSING* lines, flip
those cases to `must_pass`, refresh the baseline, raise the floors to what
was measured. An xfail that stays failing after its phase is a finding
against that phase, not a reason to relax the annotation.

## The renderer under test

`services.bench.map_eval.resolve_renderer(store)` returns the callable and
its name; the report header prints the name (`renderer=`) so a baseline can
never be mistaken for one produced by a different code path.

- Today: `lambda rel: store.get_or_build_map(rel)` on a
  `FileMemoryStore(work_dir)` — the exact call behind `c3_compress mode=map`
  and bare `c3_read` (`cli/tools/compress.py`, `cli/tools/read.py`).
- From C1: when `services.file_map.render_map` can be imported it is
  preferred, rendered from `store.update(rel)`'s record, and reported as
  `services.file_map.render_map`. The suite, annotations and baseline do not
  change; only the grammar the parser detects does.

Determinism is probed by dropping the record and rendering again from a
fresh store, so a renderer that is stable only through its in-memory cache
is caught.

## Findings from the 2026-09-06 baseline (legacy renderer)

Concrete for C1, each with the fixture that pins it:

- No return types anywhere; `@property` and TS getters render as methods
  (`py_basic`, `ts_basic`); class bases are not shown (`ts_basic`).
- Multi-line Python signatures are printed with their newlines, breaking the
  one-symbol-per-line shape (`py_basic` `render`).
- Methods of a nested class are dropped — only one level of children is
  rendered (`py_few_imports`).
- Go: no `const` block, the struct is typed `type`, the receiver is shown as
  the parameter list (`go_basic`).
- Rust: no `const`, `impl impl Point`, impl methods flat as `function`, no
  return types (`rs_basic`).
- Markdown: the last headings end one line past EOF — the parser splits on
  `\n` and counts the trailing empty element (`md_basic`).
- YAML: `_walk_yaml` breaks after the first stream child, so a file that
  starts with a comment renders no keys at all; nested mapping ranges end
  one line late (`yaml_basic`).
- CSS: a selector spanning two lines is cut to the first line's columns
  (`.card .t`); a media query renders as `@media @media` (`css_basic`).
- HTML: `id` attributes are never found (tree-sitter wraps the value in
  `quoted_attribute_value`), so no `#id` sections (`html_basic`).
- R (regex path): symbols are named by their raw source line
  (`safe_mean <- function(x, na.rm = TRUE) {`), local assignments inside
  functions become top-level constants, ranges are off by one
  (`r_regex_fallback`).
- Java: no parser, one `(full file)` line (`java_generic`, info).

For C4: a 20 KB single-line bundle renders 549 `variable vN` lines (7,133
tokens for a 7,117-token file) and persists the whole line as every
symbol's `signature` — an 11.3 MB record for a 20 KB file (`js_minified`).
The 250 KB / 400-function module is parsed completely today (recall 1.0,
ranges exact, 41 ms) and is `must_pass` so C4's bounds may never drop a
symbol (`py_large_generated`).
