# The file map

A file map is what the model reads to decide which symbols to fetch with
`c3_read`. Since 2.121.0 there is exactly one map, rendered by
`services/file_map.render_map` from a `FileMemoryStore` record, and every
entry point serves the same text:

- `c3_read(file_path)` with no `symbols` / `lines` (a directory path maps its files)
- `c3_compress(file_path)` (single or comma-separated batch)
- the inline map under a `c3_search` hit (shortened to 600 tokens)
- the maps `c3_agent` workflows prefetch
- `c3 map` on the command line

## Grammar

```
# <rel/posix/path> (<lines>L <lang>)
I <n> imports                                 # more than 6 imports
I <import statement> [L<a>-L<b>]              # 6 or fewer: each one
K <NAME> [L<a>-L<b>]                          # module constant
V <name> [L<a>-L<b>]                          # module variable
C <Class>(<bases>) [L<a>-L<b>]                # bases only when the source has them
  M <Class>.<method>(<params>) -> <ret> [L<a>-L<b>]
  P <Class>.<prop> [L<a>-L<b>]                # @property / getter / setter
F <func>(<params>) -> <ret> [L<a>-L<b>]
F async <func>(<params>) [L<a>-L<b>]          # async before the name
IF <Interface> · T <TypeAlias> · E <Enum> · S <Struct> · TR <Trait>
IM <Type> [L<a>-L<b>]                         # Rust impl; its fns nest as <Type>.<fn>
H <heading> [L<a>-L<b>]                       # markdown/html, two spaces per level
SEC <key> [L<a>-L<b>]                         # yaml/json keys, css selectors
```

Rules that hold for every line:

- One symbol per line, in source order. Methods nest two spaces under
  their class, impl, trait, struct, enum or interface and carry the
  qualified name the way `c3_read(symbols=['Class.method'])` accepts it.
- `<params>` is the parameter list exactly as written, whitespace
  normalised to single spaces and multi-line definitions joined. Nothing
  is truncated. `-> <ret>` appears only when the source declares a return
  type. Go receivers fold into the qualified name (`M Server.Start(ctx
  context.Context) -> error`).
- Ranges are 1-based, inclusive and always two-ended (`[L77-L77]` for a
  one-line symbol). A decorated definition's range starts at the `def`,
  not the decorator.
- No emoji, no column padding, no generated summary, no docstrings.
  `render_map(record, include_docs=True)` adds one line per symbol with
  the first sentence of its docstring (120 characters at most); nothing
  in C3 turns that on by default.
- `max_tokens` shortens a map by dropping symbol lines from the end and
  appending `… <k> more symbols`. The renderer never changes shape to fit.
- Paths are project-relative with forward slashes on every platform.

Every symbol line matches `services.file_map.SYMBOL_LINE_RE`, and
`services.file_map.parse_map` turns a map back into symbol dicts — the
`c3 map-eval` harness grades the renderer through that parser.

## Where the symbols come from

`services/parser.py` walks a tree-sitter AST for `.py .js .jsx .ts .tsx
.go .rs .md .html .css .json .yaml`; other extensions fall back to the
regex patterns in `services/compressor.py` (`.r`) or to a single
`(full file)` section. Each record names its extractor in `parser`
(`tree_sitter | regex | generic`) and the telemetry folds maps by it
(`map_by_backend` in `aggregate_tool_telemetry`).

Parsers that emit a flat list (Rust, Go, the regex fallback) are nested
by the renderer through line containment: a callable whose range lies
inside the nearest preceding container becomes that container's child.

## What changed in 2.121.0 and why

Measured on this repository's telemetry (Jul–Sep 2026): the old map spent
24% of its tokens on emoji and column padding, truncated parameter lists
at 60 characters, and opened with an Ollama summary generated from symbol
names alone — 134 of 267 stored summaries were cut mid-sentence. Three
different "maps" existed (the emoji map, a structure-only fall-through
under `compress_file(path, "map")`, and search's truncation of the first).
The canonical map renders the same file with complete signatures at fewer
tokens; the summaries were purged from every record once (marker
`.c3/file_memory/_summaries_purged`) and nothing generates new ones.

## Small files

A map of a very small file can cost more tokens than the file (measured:
a 15-line module mapped at 94 tokens against 49 for the source). When the
map would not be smaller, `c3_read(file_path)` and `c3_compress` serve the
whole file instead, headed `whole file — smaller than its map`.

## Retired modes (2.122.0)

`c3_compress` accepted six modes; telemetry over 3,932 tool calls showed
two calls that were not `map`. The others are retired. Asking for one
still returns the map, headed by one line —
`[compress:deprecated] mode '<name>' retired in 2.122.0 — map is the
only mode (<what to use instead>); see docs/file-map.md` — and the
telemetry row carries `deprecated_mode` so the notice can be removed once
nothing asks any more.

| mode | instead |
|---|---|
| `dense_map` | the map is dense now |
| `smart`, `structure`, `outline` | the map carries full signatures |
| `diff` | `git diff`, or the edit ledger (`c3_edits`) — diff cached a full copy of every file it saw, keyed by basename |
| `bug_scan` | `c3_search(action='exact', query='except ')` |
| `ast` | it never read `file_path`; project architecture comes from codebase-memory-mcp `cli get_architecture` or `c3_status` |

`CodeCompressor.compress_file` keeps `smart` / `structure` / `outline` for
internal callers (delegation, the Hub REST API, context snapshots,
benchmarks); its `map` mode renders the canonical map. `diff` and
`bug_scan` return an error dict there, and the `*.cache` files diff left
under `.c3/cache` are deleted on the compressor's first start.

## Directories (2.123.0)

`c3_read('<dir>')` renders one line per file under a token budget
(`lines=<int>` sets it; default 1500):

```
# services/ (112 files, 55,319L)
file_map.py (280L python) — render_map; parse_signature; parse_map; KINDS
compressor.py (809L python) — CodeCompressor; compress_file; STRUCTURE_PATTERNS
…
… 70 more files
[dir_map] recently edited first, then by structure; c3_read('<file>') maps one file
```

Files come first when the edit ledger saw them recently, then by how much
structure they hold, then by path — the order a reader coming back to a
tree wants. Each line shows up to six symbols, classes before functions,
public before private. Traversal never follows symlinks, prunes the
scanner's skip list and `.gitignore` directories, stops at 400 files
(`[dir_map] traversal capped`), and parses at most 120 unindexed code
files per call inside a 3-second deadline; the rest show their line count.
