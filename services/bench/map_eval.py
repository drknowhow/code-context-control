"""Gold evaluation harness for C3's file map (``c3 map-eval``).

Renders hand-annotated source fixtures (``tests/map_eval/fixtures``, suite
``tests/map_eval/fixture_suite.jsonl``) through the SAME renderer the MCP
tool uses — today ``FileMemoryStore.get_or_build_map`` (c3_compress
mode=map and bare c3_read), from phase C1 ``services.file_map.render_map``
when it exists — and grades the map against the annotation: did every
symbol the author listed come out (recall), with the right parameters and
return type (signature completeness), on the right lines (range
accuracy), and nothing that must stay hidden (inner functions, fenced
headings). The annotation is the truth; tree-sitter output is not.

Two grammars are parsed so a baseline taken against today's renderer and a
run against C1's can be graded from the same fixtures:

* canonical (C1, docs/map-eval.md § Grammar): ``K Qualified.name(params)
  -> ret [La-Lb]`` — matched on (kind, name);
* legacy (today, ``file_memory._format_map``): ``  a-b   <emoji> type
  name(params)`` — matched on the bare name, with the legacy type words
  mapped onto the canonical kinds so the baseline is fair.

Metrics per case: raw/map tokens and their ratio, symbol recall and
precision, signature completeness, range accuracy, determinism (render
twice from a cold store, byte-equal), render time, chrome share (emoji +
padding, info), persisted record size (info). Aggregates: means and
p50/p95, pass rates per gate, ``must_pass_failed``, ``xfail_passing``. A
checked-in baseline carries FLOORS and CEILINGS; ``compare_to_baseline``
turns them into violations (exit 1) and warnings, exactly like
``services.bench.shell_eval``.

Gates per case: ``must_pass`` (a failure fails CI), ``xfail`` (known
broken; ``phase`` names the campaign phase — C1 renderer, C2 mode
retirement, C3 fold, C4 large files — that fixes it; a pass is reported so
the case can be promoted), ``info`` (measured, never gates). A must_pass
case fails when recall < 1.0, a forbidden name is a symbol, the render is
not deterministic, or a check the suite line switches on
(``signature_completeness``, ``range_accuracy``, ``max_map_tokens``) is
missed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = ("C", "M", "F", "K", "V", "IF", "T", "E", "S", "TR", "IM", "H", "SEC", "P")
QUALIFIED_KINDS = ("M", "P", "C", "IM")  # kinds whose name may carry a `Parent.` prefix
GATES = ("must_pass", "xfail", "info")
PHASES = ("C1", "C2", "C3", "C4")
CHECK_KEYS = ("signature_completeness", "range_accuracy", "max_map_tokens")

SUITE_DIR = Path(__file__).resolve().parents[2] / "tests" / "map_eval"
FIXTURES_DIR = SUITE_DIR / "fixtures"
GENERATORS_PATH = SUITE_DIR / "generators.py"
BUNDLED_SUITES = {"fixture": SUITE_DIR / "fixture_suite.jsonl"}
BUNDLED_BASELINES = {"fixture": SUITE_DIR / "baseline_fixture.json"}

# Legacy section types (file_memory / parser) onto canonical kinds.
LEGACY_TYPE_TO_KIND = {
    "function": "F", "method": "M", "class": "C", "constant": "K", "variable": "V",
    "interface": "IF", "type": "T", "enum": "E", "struct": "S", "trait": "TR", "impl": "IM",
    "heading": "H", "section": "SEC", "property": "P",
}
# The legacy renderer's icon table (file_memory._format_map), inverted, so a
# label that carries no type word (headings, sections, child methods) can
# still be typed. Variation selectors are stripped before lookup.
LEGACY_ICON_TO_TYPE = {
    "\U0001F3D7": "class", "\u2728": "function", "\u2699": "method", "\U0001F4E6": "import",
    "\U0001F48E": "constant", "\U0001F4C4": "variable", "\U0001F9E9": "interface",
    "\U0001F3F7": "type", "\U0001F522": "enum", "\U0001F4AC": "comment", "\U0001F527": "property",
    "\U0001F3A8": "decorator", "\U0001F516": "heading", "\U0001F4CD": "section",
    "\U0001F9F1": "struct", "\U0001F4DC": "trait", "\U0001F6E0": "impl",
}
LEGACY_TYPE_WORDS = set(LEGACY_TYPE_TO_KIND) | {"import", "comment", "decorator", "content"}
LEGACY_ACCESS_WORDS = ("public", "private", "protected")

# Canonical symbol line (docs/map-eval.md § Grammar). ``I`` lines are import
# summaries, parsed but never matched as symbols.
CANON_LINE_RE = re.compile(
    r"^(?P<indent> *)(?P<kind>I|K|V|C|M|P|F|IF|T|E|S|TR|IM|H|SEC) (?P<async>async )?"
    r"(?P<name>.+?)(?:\((?P<params>.*)\))?(?: -> (?P<ret>.+?))? \[L(?P<a>\d+)-L(?P<b>\d+)\]$")
CANON_HEADER_RE = re.compile(r"^# (?P<path>.+?) \((?P<lines>\d+)L (?P<lang>\S*)\)$")
CANON_IMPORTS_RE = re.compile(r"^ *I (?P<n>\d+) imports$")

LEGACY_HEADER_RE = re.compile(r"^# (?P<path>.+?) \((?P<lines>\d+) lines, (?P<lang>[^)]*)\)$")
LEGACY_IMPORTS_RE = re.compile(r"^  imports\s+(?P<n>\d+) statements \(collapsed\)$")
LEGACY_TOP_RE = re.compile(r"^  (?P<a>\d+)-(?P<b>\d+)\s*(?P<label>\S.*)$")
LEGACY_CHILD_RE = re.compile(r"^ {12}(?P<a>\d+)-(?P<b>\d+)\s*(?P<label>\S.*)$")

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
PADDING_RE = re.compile(r"(?<=\S)( {2,})")


def _bare_name(kind: str, name: str) -> str:
    """The unqualified name: ``Class.method`` → ``method`` for the kinds that
    nest; selectors, headings and keys keep their dots."""
    if kind in QUALIFIED_KINDS and "." in name:
        return name.rsplit(".", 1)[-1]
    return name


# ── Suite model ─────────────────────────────────────────────────────────────


@dataclass
class ExpectedSymbol:
    kind: str
    name: str
    a: int
    b: int
    params: str | None = None
    ret: str | None = None
    is_async: bool = False

    @classmethod
    def from_dict(cls, d: dict, case_id: str) -> "ExpectedSymbol":
        kind = d.get("kind")
        if kind not in KINDS:
            raise ValueError(f"case {case_id!r}: unknown kind {kind!r} (expected one of {KINDS})")
        a, b = int(d["line_start"]), int(d["line_end"])
        if a < 1 or b < a:
            raise ValueError(f"case {case_id!r}: bad range {a}-{b} for {d.get('name')!r}")
        return cls(kind=kind, name=str(d["name"]), a=a, b=b,
                   params=d.get("params"), ret=d.get("ret"), is_async=bool(d.get("async", False)))

    @property
    def bare(self) -> str:
        return _bare_name(self.kind, self.name)


@dataclass
class MapCase:
    id: str
    file: str | None = None
    generator: str | None = None
    params: dict = field(default_factory=dict)
    gate: str = "info"
    phase: str | None = None
    expected: list[ExpectedSymbol] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    why: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "MapCase":
        cid = str(d.get("id"))
        gate = d.get("gate", "info")
        if gate not in GATES:
            raise ValueError(f"case {cid!r}: unknown gate {gate!r}")
        phase = d.get("phase")
        if phase is not None and phase not in PHASES:
            raise ValueError(f"case {cid!r}: unknown phase {phase!r} (expected one of {PHASES})")
        if gate == "xfail" and not phase:
            raise ValueError(f"case {cid!r}: xfail must name the phase that fixes it")
        if bool(d.get("file")) == bool(d.get("generator")):
            raise ValueError(f"case {cid!r}: exactly one of 'file' or 'generator' is required")
        checks = dict(d.get("checks") or {})
        unknown = sorted(set(checks) - set(CHECK_KEYS))
        if unknown:
            raise ValueError(f"case {cid!r}: unknown check(s) {unknown}")
        if d.get("file") and "expected" not in d:
            raise ValueError(f"case {cid!r}: a fixture case must carry its hand-written 'expected' list")
        return cls(
            id=cid,
            file=d.get("file"),
            generator=d.get("generator"),
            params=dict(d.get("params") or {}),
            gate=gate,
            phase=phase,
            expected=[ExpectedSymbol.from_dict(e, cid) for e in (d.get("expected") or [])],
            must_not_contain=list(d.get("must_not_contain") or []),
            checks=checks,
            tags=list(d.get("tags", [])),
            why=d.get("why", ""),
        )


def load_suite(path: str | Path) -> tuple[dict, list[MapCase]]:
    """Read a JSONL suite: first object is the header, the rest are cases."""
    path = Path(path)
    header: dict = {}
    cases: list[MapCase] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            obj = json.loads(raw)
            if lineno == 1 or (not cases and "suite" in obj and "id" not in obj):
                header = obj
                continue
            case = MapCase.from_dict(obj)
            if case.id in seen:
                raise ValueError(f"{path.name}: duplicate case id {case.id!r}")
            seen.add(case.id)
            cases.append(case)
    if not header:
        raise ValueError(f"{path.name}: missing suite header line")
    return header, cases


def resolve_suite(name_or_path: str) -> Path:
    if name_or_path in BUNDLED_SUITES:
        p = BUNDLED_SUITES[name_or_path]
        if not p.exists():
            raise FileNotFoundError(
                f"bundled suite {name_or_path!r} not found at {p} — run from a C3 "
                "source checkout or pass --suite PATH")
        return p
    p = Path(name_or_path)
    if not p.exists():
        raise FileNotFoundError(f"suite not found: {name_or_path}")
    return p


# ── Generators ──────────────────────────────────────────────────────────────

_generators_module = None


def load_generators(path: str | Path | None = None):
    """Import ``tests/map_eval/generators.py`` by path (``tests`` is not a
    package the wheel ships)."""
    global _generators_module
    if path is None and _generators_module is not None:
        return _generators_module
    p = Path(path) if path else GENERATORS_PATH
    if not p.exists():
        raise FileNotFoundError(f"generators not found: {p} — run from a C3 source checkout")
    name = "c3_map_eval_generators"
    spec = importlib.util.spec_from_file_location(name, p)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if path is None:
        _generators_module = module
    return module


# ── Renderer ────────────────────────────────────────────────────────────────


def resolve_renderer(store):
    """Return ``(callable(rel_path) -> str, name)`` — the renderer the live
    tool uses. Prefers ``services.file_map.render_map`` (phase C1) when it
    exists; otherwise today's ``FileMemoryStore.get_or_build_map``, which is
    what c3_compress mode=map and bare c3_read return."""
    render_map = None
    try:
        from services import file_map  # type: ignore[attr-defined]
        render_map = getattr(file_map, "render_map", None)
    except ImportError:
        render_map = None
    if render_map is not None:
        def _render(rel: str) -> str:
            record = store.update(rel)
            if record is None:
                return f"[file_map] Could not build map for {rel} — file not found or unreadable."
            return render_map(record)
        return _render, "services.file_map.render_map"
    return (lambda rel: store.get_or_build_map(rel)), "services.file_memory.FileMemoryStore.get_or_build_map"


def build_store(work_dir: str | Path):
    from services.file_memory import FileMemoryStore
    return FileMemoryStore(str(work_dir))


# ── Parsing ─────────────────────────────────────────────────────────────────


@dataclass
class Symbol:
    kind: str
    name: str
    a: int
    b: int
    params: str | None = None
    ret: str | None = None
    is_async: bool = False
    legacy_type: str | None = None

    @property
    def bare(self) -> str:
        return _bare_name(self.kind, self.name)


@dataclass
class ParsedMap:
    grammar: str  # canonical | legacy | none
    symbols: list[Symbol]
    imports: int | None = None
    header: dict = field(default_factory=dict)


def normalize_ws(text: str | None) -> str | None:
    """Whitespace-normalize a params/ret fragment: single spaces, no space
    before a comma or closing bracket, no trailing comma."""
    if text is None:
        return None
    s = re.sub(r"\s+", " ", text).strip()
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*", ", ", s)
    s = re.sub(r"\s+([)\]}])", r"\1", s)
    s = re.sub(r"([(\[{])\s+", r"\1", s)
    s = s.strip()
    return s.rstrip(",").strip()


def parse_canonical(text: str) -> ParsedMap | None:
    symbols: list[Symbol] = []
    imports: int | None = None
    header: dict = {}
    matched = False
    for line in text.splitlines():
        if not header:
            hm = CANON_HEADER_RE.match(line)
            if hm:
                header = {"path": hm["path"], "lines": int(hm["lines"]), "lang": hm["lang"]}
                matched = True
                continue
        im = CANON_IMPORTS_RE.match(line)
        if im:
            imports = int(im["n"])
            matched = True
            continue
        m = CANON_LINE_RE.match(line)
        if not m:
            continue
        matched = True
        kind = m["kind"]
        if kind == "I":
            imports = (imports or 0) + 1
            continue
        name, params, ret = m["name"], m["params"], m["ret"]
        if kind not in ("C", "M", "F", "P") and params is not None:
            # Parens inside a heading / selector / key are part of the name.
            name = f"{name}({params})"
            params = None
        symbols.append(Symbol(kind=kind, name=name, a=int(m["a"]), b=int(m["b"]),
                              params=normalize_ws(params), ret=normalize_ws(ret),
                              is_async=bool(m["async"])))
    if not matched:
        return None
    return ParsedMap("canonical", symbols, imports, header)


def _split_trailing_params(rest: str) -> tuple[str, str | None]:
    """``name(params)`` → (name, params) using the LAST balanced paren group;
    anything else → (rest, None)."""
    if not rest.endswith(")"):
        return rest, None
    depth = 0
    for i in range(len(rest) - 1, -1, -1):
        ch = rest[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                return rest[:i], rest[i + 1:-1]
    return rest, None


def _legacy_label(label: str, is_child: bool) -> tuple[str, str, str | None, bool, str | None]:
    """Decode a legacy label into (legacy_type, name, params, is_async, access)."""
    icon_chars = []
    i = 0
    while i < len(label) and (EMOJI_RE.match(label[i]) or label[i] == " "):
        if label[i] != " ":
            icon_chars.append(label[i])
        i += 1
    rest = label[i:].strip()
    icon_type = None
    for ch in icon_chars:
        if ch in LEGACY_ICON_TO_TYPE:
            icon_type = LEGACY_ICON_TO_TYPE[ch]
            break

    words = rest.split(" ")
    access = None
    is_async = False
    if words and words[0] in LEGACY_ACCESS_WORDS:
        access = words.pop(0)
    if words and words[0] == "async":
        is_async = True
        words.pop(0)
    rest = " ".join(words)

    ltype = icon_type
    if words and words[0] in LEGACY_TYPE_WORDS and (icon_type is None or icon_type not in ("method",)
                                                     or not is_child):
        # Top-level typed labels and non-method children carry the type word.
        if not (is_child and icon_type == "method"):
            ltype = words[0]
            rest = " ".join(words[1:])
    if ltype is None:
        ltype = "method" if is_child else "content"

    params = None
    if ltype in ("function", "method"):
        rest, params = _split_trailing_params(rest)
    name = rest.strip()
    # Legacy decorations that are formatting, not identity.
    if ltype == "heading":
        name = re.sub(r"^h[1-6]: ", "", name)
    elif ltype == "section":
        name = re.sub(r" \([a-zA-Z][\w-]*\)$", "", name)
    elif ltype == "impl" and name.startswith("impl "):
        name = name[5:]
    return ltype, name, params, is_async, access


def parse_legacy(text: str) -> ParsedMap | None:
    lines = text.splitlines()
    # Join physical continuation lines: a multi-line signature leaves an open
    # paren on a symbol line and its remainder on the following lines.
    merged: list[str] = []
    for line in lines:
        if merged and (merged[-1].count("(") > merged[-1].count(")")) \
                and not (LEGACY_TOP_RE.match(line) or LEGACY_CHILD_RE.match(line)):
            merged[-1] += " " + line.strip()
        else:
            merged.append(line)

    symbols: list[Symbol] = []
    imports: int | None = None
    header: dict = {}
    matched = False
    parent: Symbol | None = None
    for line in merged:
        if not header:
            hm = LEGACY_HEADER_RE.match(line)
            if hm:
                header = {"path": hm["path"], "lines": int(hm["lines"]), "lang": hm["lang"]}
                matched = True
                continue
        im = LEGACY_IMPORTS_RE.match(line)
        if im:
            imports = int(im["n"])
            matched = True
            continue
        cm = LEGACY_CHILD_RE.match(line)
        tm = None if cm else LEGACY_TOP_RE.match(line)
        m = cm or tm
        if not m:
            continue
        matched = True
        ltype, name, params, is_async, _access = _legacy_label(m["label"], is_child=bool(cm))
        if ltype == "import":
            imports = (imports or 0) + 1
            continue
        if ltype in ("comment", "decorator", "content"):
            continue
        kind = LEGACY_TYPE_TO_KIND.get(ltype)
        if kind is None:
            continue
        sym = Symbol(kind=kind, name=name, a=int(m["a"]), b=int(m["b"]),
                     params=normalize_ws(params), ret=None, is_async=is_async, legacy_type=ltype)
        if cm and parent is not None:
            sym.name = f"{parent.name}.{name}"
        elif not cm:
            parent = sym if ltype in ("class", "interface", "enum", "struct", "trait", "impl") else None
        symbols.append(sym)
    if not matched:
        return None
    return ParsedMap("legacy", symbols, imports, header)


def parse_map(text: str) -> ParsedMap:
    """Canonical first, legacy fallback, else an empty parse."""
    head = text.split("\n", 1)[0]
    if LEGACY_HEADER_RE.match(head) and not CANON_HEADER_RE.match(head):
        return parse_legacy(text) or ParsedMap("none", [])
    return parse_canonical(text) or parse_legacy(text) or ParsedMap("none", [])


# ── Metrics ─────────────────────────────────────────────────────────────────


def _key(sym, legacy: bool):
    if legacy:
        return sym.bare
    return (sym.kind, sym.name)


def match_symbols(expected: list[ExpectedSymbol], rendered: list[Symbol], *, legacy: bool):
    """Greedy multiset match. Returns (pairs, missing, extra). Within a key
    the rendered candidate with the exact range wins, then exact start."""
    pool: dict = defaultdict(list)
    for s in rendered:
        pool[_key(s, legacy)].append(s)
    picked: dict[int, Symbol] = {}
    # Pass 1: exact range wins, so two same-named symbols (``__init__`` in
    # two classes, ``run`` in two classes) pair with their own line.
    for e in expected:
        cands = pool.get(_key(e, legacy)) or []
        hit = next((c for c in cands if c.a == e.a and c.b == e.b), None)
        if hit is not None:
            cands.remove(hit)
            picked[id(e)] = hit
    # Pass 2: same start, else whatever is left under the key.
    for e in expected:
        if id(e) in picked:
            continue
        cands = pool.get(_key(e, legacy)) or []
        hit = next((c for c in cands if c.a == e.a), None) or (cands[0] if cands else None)
        if hit is not None:
            cands.remove(hit)
            picked[id(e)] = hit
    pairs = [(e, picked[id(e)]) for e in expected if id(e) in picked]
    missing = [e for e in expected if id(e) not in picked]
    extra = [s for cands in pool.values() for s in cands]
    return pairs, missing, extra


def chrome_share(text: str) -> float:
    """Share of map characters that are emoji / variation selectors or runs
    of 2+ spaces after a non-space (column padding). Info only."""
    if not text:
        return 0.0
    chrome = len(EMOJI_RE.findall(text))
    for line in text.splitlines():
        for m in PADDING_RE.finditer(line):
            chrome += len(m.group(1))
    return round(chrome / len(text), 4)


def _fmt_expected(e: ExpectedSymbol) -> str:
    return f"{e.kind} {e.name} [L{e.a}-L{e.b}]"


def _fmt_symbol(s: Symbol) -> str:
    return f"{s.kind} {s.name} [L{s.a}-L{s.b}]"


# ── Execution ───────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    id: str
    file: str
    gate: str
    phase: str | None
    status: str  # pass | fail | error
    reason: str = ""
    checks_failed: list[str] = field(default_factory=list)
    grammar: str = "none"
    raw_bytes: int = 0
    raw_lines: int = 0
    raw_tokens: int = 0
    map_tokens: int = 0
    map_bytes: int = 0
    ratio: float | None = None
    n_expected: int = 0
    n_rendered: int = 0
    symbol_recall: float | None = None
    symbol_precision: float | None = None
    signature_completeness: float | None = None
    range_accuracy: float | None = None
    determinism: bool | None = None
    render_ms: float = 0.0
    chrome_share: float = 0.0
    record_bytes: int = 0
    imports_reported: int | None = None
    must_not_contain_hits: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    signature_misses: list[str] = field(default_factory=list)
    range_misses: list[str] = field(default_factory=list)


def materialize(case: MapCase, work_dir: Path, generators=None) -> tuple[str, list[ExpectedSymbol], list[str]]:
    """Copy the fixture (or write the generated file) into ``work_dir``.
    Returns (rel_path, expected, must_not_contain)."""
    if case.file:
        src = FIXTURES_DIR / case.file
        if not src.exists():
            raise FileNotFoundError(f"fixture not found: {src}")
        rel = case.file
        shutil.copyfile(src, work_dir / rel)
        return rel, list(case.expected), list(case.must_not_contain)
    gens = generators or load_generators()
    gen = gens.generate(case.generator, case.params, case_id=case.id)
    rel = gen.filename
    (work_dir / rel).write_text(gen.content, encoding="utf-8", newline="\n")
    expected = [ExpectedSymbol.from_dict(e, case.id) for e in gen.expected] + list(case.expected)
    forbidden = list(gen.must_not_contain) + list(case.must_not_contain)
    return rel, expected, forbidden


def grade(case: MapCase, expected: list[ExpectedSymbol], forbidden: list[str],
          parsed: ParsedMap, result: CaseResult) -> list[str]:
    """Fill the symbol metrics on ``result`` and return the gate failures."""
    legacy = parsed.grammar != "canonical"
    rendered = parsed.symbols
    pairs, missing, extra = match_symbols(expected, rendered, legacy=legacy)

    result.n_expected = len(expected)
    result.n_rendered = len(rendered)
    result.symbol_recall = round(len(pairs) / len(expected), 4) if expected else 1.0
    result.symbol_precision = (round(len(pairs) / len(rendered), 4) if rendered else None)
    result.missing = [_fmt_expected(e) for e in missing[:8]]
    result.extra = [_fmt_symbol(s) for s in extra[:8]]

    with_sig = [e for e in expected if e.params is not None]
    if with_sig:
        got = {id(e): s for e, s in pairs}
        ok = 0
        for e in with_sig:
            s = got.get(id(e))
            if s is None:
                result.signature_misses.append(f"{e.name}: not rendered")
                continue
            want_p, have_p = normalize_ws(e.params), normalize_ws(s.params)
            if want_p != have_p:
                result.signature_misses.append(f"{e.name}: params {have_p!r} != {want_p!r}")
                continue
            if e.ret is not None and normalize_ws(e.ret) != normalize_ws(s.ret):
                result.signature_misses.append(f"{e.name}: ret {s.ret!r} != {e.ret!r}")
                continue
            ok += 1
        result.signature_completeness = round(ok / len(with_sig), 4)
        result.signature_misses = result.signature_misses[:8]
    if pairs:
        exact = 0
        for e, s in pairs:
            if s.a == e.a and s.b == e.b:
                exact += 1
            elif len(result.range_misses) < 8:
                result.range_misses.append(f"{e.name}: L{s.a}-L{s.b} != L{e.a}-L{e.b}")
        result.range_accuracy = round(exact / len(pairs), 4)

    names = {s.name for s in rendered} | {s.bare for s in rendered}
    result.must_not_contain_hits = [f for f in forbidden if f in names]

    failures: list[str] = []
    if expected and result.symbol_recall < 1.0:
        failures.append(f"symbol_recall {result.symbol_recall} < 1.0 (missing: {', '.join(result.missing[:4])})")
    if result.must_not_contain_hits:
        failures.append(f"must_not_contain: {result.must_not_contain_hits} rendered as symbols")
    if result.determinism is False:
        failures.append("determinism: two cold renders differ")
    checks = case.checks
    if "signature_completeness" in checks and result.signature_completeness is not None:
        if result.signature_completeness < float(checks["signature_completeness"]):
            failures.append(f"signature_completeness {result.signature_completeness} < "
                            f"{checks['signature_completeness']} ({result.signature_misses[0] if result.signature_misses else ''})")
    if "range_accuracy" in checks and result.range_accuracy is not None:
        if result.range_accuracy < float(checks["range_accuracy"]):
            failures.append(f"range_accuracy {result.range_accuracy} < {checks['range_accuracy']} "
                            f"({result.range_misses[0] if result.range_misses else ''})")
    if "max_map_tokens" in checks and result.map_tokens > int(checks["max_map_tokens"]):
        failures.append(f"max_map_tokens: {result.map_tokens} > {checks['max_map_tokens']}")
    return failures


def run_case(case: MapCase, work_dir: str | Path, store, renderer, generators=None) -> CaseResult:
    from core import count_tokens

    work_dir = Path(work_dir)
    result = CaseResult(id=case.id, file=case.file or case.generator or "", gate=case.gate,
                        phase=case.phase, status="error")
    try:
        rel, expected, forbidden = materialize(case, work_dir, generators)
    except Exception as exc:
        result.reason = f"fixture: {type(exc).__name__}: {exc}"
        return result
    result.file = rel

    raw = (work_dir / rel).read_text(encoding="utf-8", errors="replace")
    result.raw_bytes = len(raw.encode("utf-8"))
    result.raw_lines = raw.count("\n") + (0 if raw.endswith("\n") or not raw else 1)
    result.raw_tokens = count_tokens(raw)

    t0 = time.perf_counter()
    try:
        rendered = renderer(rel)
    except Exception as exc:
        result.reason = f"renderer: {type(exc).__name__}: {exc}"
        result.render_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result
    result.render_ms = round((time.perf_counter() - t0) * 1000, 1)
    if not isinstance(rendered, str):
        result.reason = f"renderer returned {type(rendered).__name__}, not str"
        return result

    try:
        record_path = store._store_path(rel)
        result.record_bytes = record_path.stat().st_size if record_path.exists() else 0
    except Exception:
        result.record_bytes = 0

    # Determinism: drop the record and render again from a cold store.
    try:
        store.drop(rel)
        cold = build_store(work_dir)
        cold_renderer, _ = resolve_renderer(cold)
        again = cold_renderer(rel)
        result.determinism = (again == rendered)
    except Exception as exc:
        result.determinism = None
        result.reason = f"determinism probe: {type(exc).__name__}: {exc}"

    result.map_bytes = len(rendered.encode("utf-8"))
    result.map_tokens = count_tokens(rendered)
    result.ratio = round(result.map_tokens / result.raw_tokens, 4) if result.raw_tokens else None
    result.chrome_share = chrome_share(rendered)

    parsed = parse_map(rendered)
    result.grammar = parsed.grammar
    result.imports_reported = parsed.imports

    failures = grade(case, expected, forbidden, parsed, result)
    result.checks_failed = failures
    result.status = "fail" if failures else "pass"
    result.reason = "; ".join(failures)[:240]
    return result


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 4)
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    idx = max(0, min(99, int(round(q * 100)) - 1))
    return round(quantiles[idx], 4)


def _rate(results: list[CaseResult]) -> float | None:
    if not results:
        return None
    return round(sum(1 for r in results if r.status == "pass") / len(results), 4)


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 4) if vals else None


def aggregate(results: list[CaseResult]) -> dict:
    executed = [r for r in results if r.status != "error"]
    must = [r for r in executed if r.gate == "must_pass"]
    agg = {
        "n_cases": len(results),
        "n_executed": len(executed),
        "n_errors": sum(1 for r in results if r.status == "error"),
        "pass_rate_must_pass": _rate([r for r in results if r.gate == "must_pass"]),
        "pass_rate_xfail": _rate([r for r in results if r.gate == "xfail"]),
        "pass_rate_info": _rate([r for r in results if r.gate == "info"]),
        "symbol_recall_mean": _mean([r.symbol_recall for r in executed]),
        "symbol_recall_mean_must_pass": _mean([r.symbol_recall for r in must]),
        "symbol_precision_mean": _mean([r.symbol_precision for r in executed]),
        "signature_completeness_mean": _mean([r.signature_completeness for r in executed]),
        "range_accuracy_mean": _mean([r.range_accuracy for r in executed]),
        "determinism_rate": _mean([1.0 if r.determinism else 0.0 for r in executed
                                   if r.determinism is not None]),
        "chrome_share_mean": _mean([r.chrome_share for r in executed]),
        "ratio_mean": _mean([r.ratio for r in executed]),
        "ratio_p50": _pct([r.ratio for r in executed if r.ratio is not None], 0.50),
        "ratio_p95": _pct([r.ratio for r in executed if r.ratio is not None], 0.95),
        "tokens_p50": _pct([float(r.map_tokens) for r in executed], 0.50),
        "tokens_p95": _pct([float(r.map_tokens) for r in executed], 0.95),
        "tokens_total": sum(r.map_tokens for r in executed),
        "raw_tokens_total": sum(r.raw_tokens for r in executed),
        "render_ms_p50": _pct([r.render_ms for r in executed], 0.50),
        "render_ms_p95": _pct([r.render_ms for r in executed], 0.95),
        "grammars": dict(Counter(r.grammar for r in executed)),
        "must_pass_failed": sorted(r.id for r in results if r.gate == "must_pass" and r.status != "pass"),
        "xfail_passing": sorted(r.id for r in results if r.gate == "xfail" and r.status == "pass"),
        "errors": sorted(r.id for r in results if r.status == "error"),
        "by_phase": {},
    }
    for phase in PHASES:
        sub = [r for r in results if r.phase == phase]
        if sub:
            agg["by_phase"][phase] = {"n": len(sub), "passing": sum(1 for r in sub if r.status == "pass")}
    return agg


@dataclass
class EvalReport:
    suite: str
    suite_path: str
    renderer: str
    header: dict
    results: list[CaseResult]
    aggregates: dict
    c3_version: str = ""
    baseline_violations: list[str] = field(default_factory=list)
    baseline_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "suite": self.suite,
            "suite_path": self.suite_path,
            "renderer": self.renderer,
            "c3_version": self.c3_version,
            "aggregates": self.aggregates,
            "results": [asdict(r) for r in self.results],
            "baseline_violations": self.baseline_violations,
            "baseline_warnings": self.baseline_warnings,
        }

    def render(self) -> str:
        def f(v, spec="{:.2f}"):
            return "-" if v is None else spec.format(v)

        lines = [f"c3 map-eval — suite={self.suite} renderer={self.renderer}"]
        lines.append("")
        lines.append(f"{'id':<20} {'gate':<9} {'ph':<3} {'status':<6} {'gram':<6} {'raw':>7} {'map':>6} "
                     f"{'ratio':>6} {'rec':>5} {'prec':>5} {'sig':>5} {'rng':>5} {'det':>3} {'ms':>7}  note")
        for r in self.results:
            note = r.reason
            if r.status == "pass" and r.gate == "xfail":
                note = "XFAIL PASSING — promote it and refresh the baseline"
            det = "-" if r.determinism is None else ("y" if r.determinism else "N")
            gram = {"canonical": "canon", "legacy": "legacy", "none": "none"}.get(r.grammar, r.grammar)
            lines.append(
                f"{r.id:<20} {r.gate:<9} {(r.phase or '-'):<3} {r.status:<6} {gram:<6} {r.raw_tokens:>7} "
                f"{r.map_tokens:>6} {f(r.ratio, '{:.3f}'):>6} {f(r.symbol_recall):>5} "
                f"{f(r.symbol_precision):>5} {f(r.signature_completeness):>5} {f(r.range_accuracy):>5} "
                f"{det:>3} {r.render_ms:>7.1f}  {note}")
        a = self.aggregates
        lines.append("")
        lines.append(
            f"pass rate: must_pass={a['pass_rate_must_pass']} xfail={a['pass_rate_xfail']} "
            f"info={a['pass_rate_info']} | recall mean={a['symbol_recall_mean']} "
            f"(must_pass {a['symbol_recall_mean_must_pass']}) | precision mean={a['symbol_precision_mean']} "
            f"| sig mean={a['signature_completeness_mean']} | range mean={a['range_accuracy_mean']} "
            f"| determinism={a['determinism_rate']}")
        lines.append(
            f"ratio mean={a['ratio_mean']} p50={a['ratio_p50']} p95={a['ratio_p95']} | map tokens "
            f"p50={a['tokens_p50']} p95={a['tokens_p95']} total={a['tokens_total']} "
            f"(raw {a['raw_tokens_total']}) | chrome share={a['chrome_share_mean']} | render ms "
            f"p50={a['render_ms_p50']} p95={a['render_ms_p95']} | grammars={a['grammars']}")
        for phase, sub in a["by_phase"].items():
            lines.append(f"  {phase}: {sub['passing']}/{sub['n']} passing")
        if a["must_pass_failed"]:
            lines.append("MUST-PASS FAILURES: " + ", ".join(a["must_pass_failed"]))
        if a["errors"]:
            lines.append("ERRORS: " + ", ".join(a["errors"]))
        if a["xfail_passing"]:
            lines.append("xfail now passing: " + ", ".join(a["xfail_passing"]))
        for v in self.baseline_violations:
            lines.append(f"VIOLATION: {v}")
        for w in self.baseline_warnings:
            lines.append(f"warning: {w}")
        if self.baseline_violations:
            lines.append("verdict: FAIL")
        elif self.baseline_warnings or a["xfail_passing"]:
            lines.append("verdict: PASS (with warnings)")
        else:
            lines.append("verdict: PASS")
        return "\n".join(lines)


def run_suite(suite: str | Path = "fixture", *, work_dir: str | Path | None = None,
              baseline: str | Path | None = None, generators_path: str | Path | None = None) -> EvalReport:
    """Load a suite, materialize every fixture under ``work_dir`` (a temp dir
    when None), render each through the resolved renderer, compare to a
    baseline."""
    suite_path = resolve_suite(str(suite))
    header, cases = load_suite(suite_path)
    suite_name = header.get("suite") or suite_path.stem

    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="c3-map-eval-")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    store = build_store(work_dir)
    renderer, renderer_name = resolve_renderer(store)
    gens = load_generators(generators_path) if generators_path else load_generators()

    results = [run_case(c, work_dir, store, renderer, gens) for c in cases]
    try:
        from cli.c3 import __version__ as c3_version
    except Exception:
        c3_version = ""
    report = EvalReport(suite=suite_name, suite_path=str(suite_path), renderer=renderer_name,
                        header=header, results=results, aggregates=aggregate(results),
                        c3_version=c3_version)

    baseline_path = Path(baseline) if baseline else BUNDLED_BASELINES.get(suite_name)
    if baseline_path and Path(baseline_path).exists():
        violations, warnings = compare_to_baseline(report, load_baseline(baseline_path))
        report.baseline_violations = violations
        report.baseline_warnings = warnings
    return report


# ── Baseline ────────────────────────────────────────────────────────────────

FLOOR_METRICS = ("pass_rate_must_pass", "symbol_recall_mean", "symbol_recall_mean_must_pass",
                 "determinism_rate", "signature_completeness_mean", "range_accuracy_mean")
CEILING_METRICS = ("tokens_p95", "ratio_p95", "ratio_mean", "chrome_share_mean", "render_ms_p95")


def load_baseline(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def compare_to_baseline(report: EvalReport, baseline: dict) -> tuple[list[str], list[str]]:
    """Return (violations, warnings). Violations fail CI: a must_pass case
    failing or erroring, an aggregate under its floor or over its ceiling.
    Warnings inform: an info/xfail case that passed in the baseline and
    fails now, or an xfail that now passes."""
    violations: list[str] = []
    warnings: list[str] = []
    a = report.aggregates
    for cid in a["must_pass_failed"]:
        r = next(x for x in report.results if x.id == cid)
        violations.append(f"must_pass case {cid} {r.status}: {r.reason}")
    for cid in a["errors"]:
        if cid not in a["must_pass_failed"]:
            r = next(x for x in report.results if x.id == cid)
            warnings.append(f"case {cid} errored: {r.reason}")
    for metric, floor in (baseline.get("floors") or {}).items():
        value = a.get(metric)
        if value is None or floor is None:
            continue
        if value < floor:
            violations.append(f"{metric}={value} below floor {floor}")
    for metric, ceiling in (baseline.get("ceilings") or {}).items():
        value = a.get(metric)
        if value is None or ceiling is None:
            continue
        if value > ceiling:
            violations.append(f"{metric}={value} above ceiling {ceiling}")
    prev = baseline.get("per_case") or {}
    for r in report.results:
        before = prev.get(r.id)
        if not before:
            continue
        if before.get("status") == "pass" and r.status != "pass" and r.gate != "must_pass":
            warnings.append(f"{r.id} passed in baseline, {r.status}s now: {r.reason}")
        if before.get("status") != "pass" and r.status == "pass" and r.gate == "xfail":
            warnings.append(f"{r.id} is xfail but now passes — promote it and refresh the baseline")
        # A case that lost symbols since the baseline is a regression even
        # while it stays xfail — the campaign only ever moves recall up.
        prev_recall = before.get("symbol_recall")
        if (prev_recall is not None and r.symbol_recall is not None and r.status != "error"
                and r.symbol_recall < prev_recall):
            msg = f"{r.id} symbol_recall {r.symbol_recall} < baseline {prev_recall}"
            (violations if r.gate == "must_pass" else warnings).append(msg)
        # The campaign's token gate: never more tokens than today's map for
        # the same file — judged only where today's map was complete (recall
        # 1.0), a map that missed symbols is no bar to clear.
        prev_tokens = before.get("map_tokens")
        if (prev_tokens and prev_recall == 1.0 and r.status != "error"
                and r.map_tokens > int(prev_tokens)):
            msg = f"{r.id} map_tokens {r.map_tokens} > baseline {prev_tokens} (recall was 1.0)"
            (violations if r.gate == "must_pass" else warnings).append(msg)
    return violations, warnings


def write_baseline(report: EvalReport, path: str | Path, *, floors: dict | None = None,
                   ceilings: dict | None = None, keep_limits: bool = True) -> dict:
    """Persist aggregates + per-case status. Floors and ceilings are hand-set:
    kept from the existing file unless given explicitly."""
    path = Path(path)
    existing = load_baseline(path) if path.exists() else {}
    if floors is None and keep_limits:
        floors = existing.get("floors") or {}
    if ceilings is None and keep_limits:
        ceilings = existing.get("ceilings") or {}
    data = {
        "suite": report.suite,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "c3_version": report.c3_version,
        "renderer": report.renderer,
        "aggregates": {k: v for k, v in report.aggregates.items() if k != "by_phase"},
        "by_phase": report.aggregates.get("by_phase", {}),
        "floors": floors or {},
        "ceilings": ceilings or {},
        "per_case": {r.id: {"status": r.status, "gate": r.gate, "phase": r.phase, "grammar": r.grammar,
                            "map_tokens": r.map_tokens, "ratio": r.ratio,
                            "symbol_recall": r.symbol_recall, "symbol_precision": r.symbol_precision,
                            "signature_completeness": r.signature_completeness,
                            "range_accuracy": r.range_accuracy, "determinism": r.determinism}
                     for r in report.results},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return data
