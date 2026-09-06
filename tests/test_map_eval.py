"""File-map gold gate (``c3 map-eval``).

Renders the hand-annotated fixture suite (tests/map_eval/fixture_suite.jsonl)
through the renderer the MCP tool uses and enforces the checked-in baseline:

* every ``must_pass`` case passes (recall 1.0, nothing forbidden, deterministic,
  its switched-on checks met),
* every aggregate stays at or above its floor and at or below its ceiling,
* no complete map (baseline recall 1.0) grew in tokens,
* the baseline knows every case in the suite (add a case -> refresh it),
* an ``xfail`` that unexpectedly passes is reported, not failed.

Both map grammars (canonical C1 and today's legacy) are parser-tested here so
the same fixtures grade either renderer. See docs/map-eval.md.
"""

import json
import re
import sys
import types
import warnings
from pathlib import Path

import pytest

from services.bench import map_eval as me

SUITE = me.BUNDLED_SUITES["fixture"]
BASELINE = me.BUNDLED_BASELINES["fixture"]

# The spec's minimum case list. A rename here is a rename in the suite.
REQUIRED_CASES = {
    "py_basic", "py_few_imports", "py_malformed", "ts_basic", "js_basic", "go_basic", "rs_basic",
    "md_basic", "yaml_basic", "json_basic", "css_basic", "html_basic", "r_regex_fallback",
    "java_generic", "js_minified", "py_large_generated",
}

CANONICAL_SAMPLE = """# services/x.py (72L python)
I 9 imports
K MAX_RETRIES [L12-L12]
V settings [L14-L14]
C Worker [L23-L36]
  M Worker.__init__(self, name: str, timeout: float = DEFAULT_TIMEOUT) [L26-L28]
  P Worker.label [L31-L32]
  M Worker.run(self, job: dict) -> bool [L34-L36]
F async gather_all(urls: list) -> list [L60-L63]
F render(template: str,   context: dict, strict: bool = False) -> str [L66-L72]
F main() [L80-L82]
H Usage (advanced) [L18-L26]
SEC .card .title, .card h2 [L17-L20]
C Queue(EventEmitter) [L20-L45]
  M async Queue.drain(handler: JobHandler) -> Promise<number> [L36-L44]
M Server.Start(ctx context.Context) -> error [L26-L33]
IM Point [L22-L30]
  M Point.new(x: f64, y: f64) -> Self [L23-L25]
TR Area [L17-L20]
… 3 more symbols
"""

LEGACY_SAMPLE = """# py_basic.py (72 lines, python)

  imports    9 statements (collapsed)
  12-12     \U0001F48E constant MAX_RETRIES
  16-20     ✨ function retry(times: int)
            Decorator factory; ``wrap`` is an inner function.
  23-36     \U0001F3D7️ class Worker
            A worker that runs jobs.
            26-28   ⚙️ __init__(self, name: str, timeout: float = DEFAULT_TIMEOUT)
            31-32   ⚙️ label(self)
            34-36   ⚙️ run(self, job: dict)
            40-41   \U0001F3D7️ class Entry
            42-42   \U0001F527 private property jobs
            43-44   ⚙️ private async drain(handler: JobHandler)
  60-63     ✨ async function gather_all(urls: list)
  66-72     ✨ function render(template: str,
    context: dict,
    strict: bool = False,)
            Multi-line signature with a return annotation.
  74-76     ✨ function long_one(aaaaaaaaaa: int, bbbbbbbbbb: int, cccccccccc: int, ddddd...)
  1-30      \U0001F516 h1: Map Eval Guide
  8-14      \U0001F4CD #top (header)
  17-20     \U0001F4CD .card .t
  22-30     \U0001F6E0️ impl impl Point
  26-33     ⚙️ method Start(s *Server)
  15-18     \U0001F9F1 struct Point
  1-28      ⚙️ content (full file)
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    work = tmp_path_factory.mktemp("map_eval")
    return me.run_suite("fixture", work_dir=work, baseline=BASELINE)


def _raw_cases():
    with open(SUITE, encoding="utf-8") as fh:
        objs = [json.loads(line) for line in fh if line.strip()]
    return objs[0], objs[1:]


class TestSuiteShape:
    def test_header_and_size(self):
        header, cases = me.load_suite(SUITE)
        assert header.get("fixture") is True
        assert set(header.get("kinds", [])) == set(me.KINDS)
        assert len(cases) >= 16
        assert REQUIRED_CASES <= {c.id for c in cases}

    def test_every_fixture_exists_and_is_hand_sized(self):
        _, cases = me.load_suite(SUITE)
        for c in cases:
            if not c.file:
                continue
            p = me.FIXTURES_DIR / c.file
            assert p.exists(), f"{c.id}: fixture {p} missing"
            n = len(p.read_text(encoding="utf-8").splitlines())
            assert 20 <= n <= 80, f"{c.id}: {n} lines, fixtures are 20-80 lines"
            for e in c.expected:
                assert e.b <= n, f"{c.id}: {e.name} ends at L{e.b} past EOF ({n})"

    def test_every_generator_exists(self):
        _, cases = me.load_suite(SUITE)
        gens = me.load_generators()
        missing = sorted({c.generator for c in cases if c.generator} - set(gens.GENERATORS))
        assert not missing, f"cases name unknown generators: {missing}"

    def test_gates_are_declared_and_xfails_name_a_phase(self):
        _, cases = me.load_suite(SUITE)
        assert all(c.gate in me.GATES for c in cases)
        bad = [c.id for c in cases if c.gate == "xfail" and c.phase not in me.PHASES]
        assert not bad, f"xfail cases must name a phase: {bad}"
        assert any(c.gate == "must_pass" for c in cases)
        # C1 shipped in 2.121.0 and its cases were promoted; C4 is still open.
        assert not any(c.gate == "xfail" and c.phase == "C1" for c in cases)
        assert any(c.gate == "xfail" and c.phase == "C4" for c in cases)

    def test_checks_use_known_vocabulary(self):
        _, raw = _raw_cases()
        for d in raw:
            unknown = set(d.get("checks") or {}) - set(me.CHECK_KEYS)
            assert not unknown, f"{d['id']}: unknown checks {sorted(unknown)}"
        with pytest.raises(ValueError):
            me.MapCase.from_dict({"id": "x", "file": "a.py", "expected": [], "gate": "xfail"})  # no phase
        with pytest.raises(ValueError):
            me.MapCase.from_dict({"id": "x", "file": "a.py", "expected": [],
                                  "checks": {"budget": 1}})
        with pytest.raises(ValueError):
            me.MapCase.from_dict({"id": "x", "file": "a.py"})  # fixture without annotation
        with pytest.raises(ValueError):
            me.MapCase.from_dict({"id": "x", "file": "a.py", "generator": "g", "expected": []})
        with pytest.raises(ValueError):
            me.ExpectedSymbol.from_dict({"kind": "FN", "name": "f", "line_start": 1, "line_end": 1}, "x")
        with pytest.raises(ValueError):
            me.ExpectedSymbol.from_dict({"kind": "F", "name": "f", "line_start": 5, "line_end": 4}, "x")

    def test_expected_names_are_qualified_for_methods(self):
        _, cases = me.load_suite(SUITE)
        for c in cases:
            for e in c.expected:
                if e.kind in ("M", "P"):
                    assert "." in e.name, f"{c.id}: {e.kind} {e.name} must be Class.member"

    def test_baseline_covers_every_case(self):
        _, cases = me.load_suite(SUITE)
        baseline = me.load_baseline(BASELINE)
        missing = sorted({c.id for c in cases} - set(baseline["per_case"]))
        extra = sorted(set(baseline["per_case"]) - {c.id for c in cases})
        assert not missing and not extra, (
            f"baseline out of date (missing={missing}, extra={extra}); run "
            "`c3 map-eval --update-baseline`")


class TestGenerators:
    def test_deterministic_across_calls(self):
        gens = me.load_generators()
        for name in gens.GENERATORS:
            a = gens.generate(name, case_id=name)
            b = gens.generate(name, case_id=name)
            assert (a.content, a.expected, a.must_not_contain) == (b.content, b.expected, b.must_not_contain), name
        assert gens.seed_for("a") != gens.seed_for("b")
        assert gens.seed_for("a", {"seed": 7}) == 7

    def test_js_minified_is_one_long_line_with_planted_functions(self):
        gens = me.load_generators()
        g = gens.generate("js_minified", {"named": 6, "min_bytes": 20480}, case_id="x")
        assert g.content.count("\n") == 1 and len(g.content) >= 20480
        assert len(g.expected) == 6
        for e in g.expected:
            assert f"function {e['name']}(a,b)" in g.content
            assert (e["line_start"], e["line_end"]) == (1, 1)
        assert g.must_not_contain and all(f"var {v}=" in g.content for v in g.must_not_contain)

    def test_py_large_gold_matches_its_own_layout(self):
        gens = me.load_generators()
        g = gens.generate("py_large_generated", {"functions": 400, "min_bytes": 256000}, case_id="x")
        assert len(g.content.encode("utf-8")) >= 256000
        lines = g.content.splitlines()
        funcs = [e for e in g.expected if e["kind"] == "F"]
        assert len(funcs) == 400
        for e in funcs:
            assert lines[e["line_start"] - 1].startswith(f"def {e['name']}(a: int, b: int) -> int:")
            assert lines[e["line_end"] - 1] == "    return total"
        assert g.must_not_contain and all(f"def {h}(" in g.content for h in g.must_not_contain)


class TestCanonicalParser:
    def test_parses_every_kind_and_shape(self):
        parsed = me.parse_canonical(CANONICAL_SAMPLE)
        assert parsed is not None and parsed.grammar == "canonical"
        assert parsed.imports == 9
        assert parsed.header == {"path": "services/x.py", "lines": 72, "lang": "python"}
        by = {(s.kind, s.name): s for s in parsed.symbols}
        assert ("K", "MAX_RETRIES") in by and by[("K", "MAX_RETRIES")].a == 12
        assert ("V", "settings") in by
        init = by[("M", "Worker.__init__")]
        assert init.params == "self, name: str, timeout: float = DEFAULT_TIMEOUT" and init.ret is None
        assert init.bare == "__init__" and (init.a, init.b) == (26, 28)
        run = by[("M", "Worker.run")]
        assert run.params == "self, job: dict" and run.ret == "bool"
        assert by[("P", "Worker.label")].params is None
        ga = by[("F", "gather_all")]
        assert ga.is_async and ga.ret == "list"
        # whitespace inside params is normalized
        assert by[("F", "render")].params == "template: str, context: dict, strict: bool = False"
        assert by[("F", "main")].params == ""
        # parens in a heading are part of the name, not params
        assert ("H", "Usage (advanced)") in by and by[("H", "Usage (advanced)")].params is None
        assert ("SEC", ".card .title, .card h2") in by
        assert by[("C", "Queue")].params == "EventEmitter"
        drain = by[("M", "Queue.drain")]
        assert drain.is_async and drain.ret == "Promise<number>"
        assert by[("M", "Server.Start")].ret == "error"
        assert ("IM", "Point") in by and by[("M", "Point.new")].ret == "Self"
        assert ("TR", "Area") in by
        # the `… k more symbols` tail and the I line are not symbols
        assert not any(s.name.startswith("…") for s in parsed.symbols)
        assert all(s.kind != "I" for s in parsed.symbols)

    def test_regex_is_the_spec_regex(self):
        line = "  M Worker.run(self, job: dict) -> bool [L34-L36]"
        m = me.CANON_LINE_RE.match(line)
        assert m and m["kind"] == "M" and m["name"] == "Worker.run"
        assert m["params"] == "self, job: dict" and m["ret"] == "bool"
        assert (m["a"], m["b"]) == ("34", "36")
        assert me.CANON_LINE_RE.match("F async fetch(url) [L1-L1]")["async"] == "async "
        assert not me.CANON_LINE_RE.match("  12-12     constant MAX_RETRIES")

    def test_parse_map_picks_canonical(self):
        parsed = me.parse_map(CANONICAL_SAMPLE)
        assert parsed.grammar == "canonical" and len(parsed.symbols) == 17


class TestLegacyParser:
    def test_parses_todays_shape(self):
        parsed = me.parse_legacy(LEGACY_SAMPLE)
        assert parsed is not None and parsed.grammar == "legacy"
        assert parsed.imports == 9
        assert parsed.header["lines"] == 72
        by = {}
        for s in parsed.symbols:
            by.setdefault(s.name, s)
        assert by["MAX_RETRIES"].kind == "K"
        assert by["retry"].kind == "F" and by["retry"].params == "times: int"
        assert by["Worker"].kind == "C" and (by["Worker"].a, by["Worker"].b) == (23, 36)
        # children are qualified with the parent and mapped to M / C / P
        init = by["Worker.__init__"]
        assert init.kind == "M" and init.bare == "__init__" and init.legacy_type == "method"
        assert init.params == "self, name: str, timeout: float = DEFAULT_TIMEOUT"
        assert by["Worker.Entry"].kind == "C"
        assert by["Worker.jobs"].kind == "P"
        drain = by["Worker.drain"]
        assert drain.is_async and drain.params == "handler: JobHandler"
        assert by["gather_all"].is_async and by["gather_all"].kind == "F"
        # a multi-line signature is joined back into one symbol
        assert by["render"].params == "template: str, context: dict, strict: bool = False"
        assert (by["render"].a, by["render"].b) == (66, 72)
        # the 60-char truncation is kept verbatim (it must fail completeness)
        assert by["long_one"].params.endswith("...")
        # headings / html sections / impl lose their legacy decoration
        assert by["Map Eval Guide"].kind == "H"
        assert by["#top"].kind == "SEC"
        assert by[".card .t"].kind == "SEC"
        assert by["Point"].kind in ("IM", "S")
        kinds = {(s.kind, s.name) for s in parsed.symbols}
        assert ("IM", "Point") in kinds and ("S", "Point") in kinds
        go = by["Start"]
        assert go.kind == "M" and go.params == "s *Server"
        # docstring lines, the (full file) content line and I lines are not symbols
        assert not any("Decorator factory" in s.name for s in parsed.symbols)
        assert not any("full file" in s.name for s in parsed.symbols)
        assert all(s.ret is None for s in parsed.symbols)

    def test_parse_map_picks_legacy_by_header(self):
        parsed = me.parse_map(LEGACY_SAMPLE)
        assert parsed.grammar == "legacy"
        assert me.parse_map("[file_map] Could not build map for x — file not found.").grammar == "none"


class TestMetrics:
    def _exp(self, kind, name, a, b, **kw):
        return me.ExpectedSymbol.from_dict({"kind": kind, "name": name, "line_start": a, "line_end": b, **kw}, "t")

    def test_normalize_ws(self):
        assert me.normalize_ws(" a: int ,\n   b :  str , ") == "a: int, b : str"
        assert me.normalize_ws("template: str,\n    context: dict,") == "template: str, context: dict"
        assert me.normalize_ws("( a , b )") == "(a, b)"
        assert me.normalize_ws(None) is None

    def test_multiset_match_pairs_duplicates_by_range(self):
        expected = [self._exp("M", "Worker.run", 34, 36), self._exp("M", "Scheduler.run", 45, 47),
                    self._exp("M", "Registry.Entry.__init__", 29, 31), self._exp("M", "Registry.__init__", 36, 37)]
        rendered = [me.Symbol("M", "Scheduler.run", 45, 47), me.Symbol("M", "Worker.run", 34, 36),
                    me.Symbol("M", "Registry.__init__", 36, 37)]
        pairs, missing, extra = me.match_symbols(expected, rendered, legacy=True)
        assert {(e.name, s.a) for e, s in pairs} == {("Worker.run", 34), ("Scheduler.run", 45), ("Registry.__init__", 36)}
        assert [e.name for e in missing] == ["Registry.Entry.__init__"]
        assert extra == []
        # canonical matching is on (kind, name): a P rendered as M is a miss
        pairs, missing, _ = me.match_symbols([self._exp("P", "Worker.label", 31, 32)],
                                             [me.Symbol("M", "Worker.label", 31, 32)], legacy=False)
        assert not pairs and len(missing) == 1

    def test_grade_metrics_and_gates(self):
        case = me.MapCase(id="t", file="x.py", gate="must_pass",
                          checks={"signature_completeness": 1.0, "range_accuracy": 1.0, "max_map_tokens": 50})
        expected = [self._exp("F", "fetch", 55, 57, params="url: str", ret="str"),
                    self._exp("F", "render", 66, 72, params="a, b"),
                    self._exp("K", "MAX", 1, 1)]
        rendered = [me.Symbol("F", "fetch", 55, 57, params="url: str", ret="str"),
                    me.Symbol("F", "render", 66, 70, params="a, b, c"),
                    me.Symbol("F", "wrap", 18, 19)]
        parsed = me.ParsedMap("canonical", rendered)
        result = me.CaseResult(id="t", file="x.py", gate="must_pass", phase=None, status="error")
        result.map_tokens = 80
        result.determinism = False
        fails = me.grade(case, expected, ["wrap"], parsed, result)
        assert result.symbol_recall == round(2 / 3, 4)
        assert result.symbol_precision == round(2 / 3, 4)
        assert result.signature_completeness == 0.5
        assert result.range_accuracy == 0.5
        assert result.must_not_contain_hits == ["wrap"]
        assert result.missing == ["K MAX [L1-L1]"] and result.extra == ["F wrap [L18-L19]"]
        heads = {f.split(" ")[0].rstrip(":") for f in fails}
        assert heads == {"symbol_recall", "must_not_contain", "determinism", "signature_completeness",
                         "range_accuracy", "max_map_tokens"}

    def test_chrome_share_counts_emoji_and_padding_only(self):
        assert me.chrome_share("F main() [L1-L2]\n") == 0.0
        text = "  12-12     \U0001F48E constant MAX\n"
        share = me.chrome_share(text)
        # 5 padding spaces after "12-12" + 1 emoji, over the line's characters
        assert share == round(6 / len(text), 4)
        # leading indentation is not chrome
        assert me.chrome_share("    M A.b() [L1-L1]") == 0.0


class TestRenderer:
    def test_resolves_todays_renderer_and_renders_a_fixture(self, tmp_path):
        store = me.build_store(tmp_path)
        renderer, name = me.resolve_renderer(store)
        assert name in ("services.file_memory.FileMemoryStore.get_or_build_map", "services.file_map.render_map")
        (tmp_path / "py_basic.py").write_bytes((me.FIXTURES_DIR / "py_basic.py").read_bytes())
        text = renderer("py_basic.py")
        parsed = me.parse_map(text)
        assert parsed.grammar in ("legacy", "canonical")
        names = {s.bare for s in parsed.symbols}
        assert {"Worker", "Scheduler", "fetch", "gather_all", "render"} <= names

    def test_prefers_file_map_render_map_when_it_exists(self, tmp_path, monkeypatch):
        """Phase C1 ships services/file_map.py; the harness must pick it up
        by import and report it as the renderer, with the canonical grammar
        graded from the same fixtures."""
        import services

        def render_map(record, *, include_docs=False, max_tokens=None):
            lines = [f"# {record['path']} ({record.get('lines', 0)}L {record.get('language', '')})"]
            kinds = {"class": "C", "function": "F", "method": "M", "constant": "K"}

            def sig(sec):
                m = re.search(r"\(([^()]*)\)", sec.get("signature", ""), re.S)
                return f"({me.normalize_ws(m.group(1))})" if m else ""
            for sec in record["sections"]:
                t = sec["type"]
                if t == "import":
                    continue
                k = kinds.get(t, "V")
                params = sig(sec) if k in ("F", "M") else ""
                lines.append(f"{k} {sec['name']}{params} [L{sec['line_start']}-L{sec['line_end']}]")
                for ch in sec.get("children", []):
                    ck = kinds.get(ch["type"], "M")
                    lines.append(f"  {ck} {sec['name']}.{ch['name']}{sig(ch)} [L{ch['line_start']}-L{ch['line_end']}]")
            return "\n".join(lines)

        fake = types.ModuleType("services.file_map")
        fake.render_map = render_map
        monkeypatch.setitem(sys.modules, "services.file_map", fake)
        monkeypatch.setattr(services, "file_map", fake, raising=False)

        store = me.build_store(tmp_path)
        renderer, name = me.resolve_renderer(store)
        assert name == "services.file_map.render_map"
        (tmp_path / "js_basic.js").write_bytes((me.FIXTURES_DIR / "js_basic.js").read_bytes())
        text = renderer("js_basic.js")
        parsed = me.parse_map(text)
        assert parsed.grammar == "canonical"
        _, cases = me.load_suite(SUITE)
        case = next(c for c in cases if c.id == "js_basic")
        result = me.CaseResult(id=case.id, file=case.file, gate=case.gate, phase=None, status="error")
        result.determinism = True
        fails = me.grade(case, case.expected, case.must_not_contain, parsed, result)
        assert result.symbol_recall == 1.0 and result.range_accuracy == 1.0, fails
        assert result.signature_completeness == 1.0, result.signature_misses


class TestFixtureGate:
    def test_every_case_executed(self, report):
        assert report.aggregates["n_errors"] == 0, report.aggregates["errors"]
        assert report.aggregates["n_executed"] == report.aggregates["n_cases"]
        assert all(r.determinism is True for r in report.results), [
            r.id for r in report.results if r.determinism is not True]

    def test_must_pass_cases_pass(self, report):
        failed = report.aggregates["must_pass_failed"]
        detail = "\n".join(f"{r.id}: {r.reason}" for r in report.results if r.id in failed)
        assert not failed, f"must_pass regressions:\n{detail}\n\n{report.render()}"

    def test_aggregates_meet_floors_and_ceilings(self, report):
        limits = [v for v in report.baseline_violations if "floor" in v or "ceiling" in v]
        assert not limits, "\n".join(limits) + "\n\n" + report.render()

    def test_no_violations(self, report):
        assert not report.baseline_violations, "\n".join(report.baseline_violations) + "\n\n" + report.render()

    def test_xfail_and_regressions_are_visible(self, report):
        for w in report.baseline_warnings:
            warnings.warn(w, stacklevel=1)
        for cid in report.aggregates["xfail_passing"]:
            warnings.warn(f"{cid} is xfail but passes; promote it", stacklevel=1)

    def test_report_renders_and_serialises(self, report):
        text = report.render()
        assert "verdict:" in text and "renderer=" in text and "grammars=" in text
        assert report.suite == "fixture"
        data = report.to_dict()
        assert len(data["results"]) == report.aggregates["n_cases"]
        json.dumps(data)  # must be JSON-serialisable for --json
        assert Path(report.suite_path).name == "fixture_suite.jsonl"

    def test_baseline_roundtrip_and_token_gate(self, report, tmp_path):
        path = tmp_path / "baseline.json"
        me.write_baseline(report, path, floors={"pass_rate_must_pass": 1.0}, ceilings={"tokens_p50": 10})
        data = me.load_baseline(path)
        assert data["floors"] == {"pass_rate_must_pass": 1.0}
        violations, _ = me.compare_to_baseline(report, data)
        assert any("above ceiling" in v for v in violations)
        # keep_limits: a refresh without explicit limits keeps the hand-set ones
        me.write_baseline(report, path)
        assert me.load_baseline(path)["ceilings"] == {"tokens_p50": 10}
        # a complete must_pass map that grows is a violation; a lost symbol too
        data = me.load_baseline(path)
        data["ceilings"] = {}
        pc = data["per_case"]["js_basic"]
        pc["map_tokens"] = 1
        pc["symbol_recall"] = 1.0
        violations, warnings_ = me.compare_to_baseline(report, data)
        assert any("js_basic map_tokens" in v for v in violations)
        pc = data["per_case"]["py_basic"]
        pc["symbol_recall"] = 1.0
        r = next(x for x in report.results if x.id == "py_basic")
        r.symbol_recall = 0.5
        try:
            violations, warnings_ = me.compare_to_baseline(report, data)
            # py_basic is must_pass since 2.121.0: a lost symbol is a violation.
            assert any("py_basic symbol_recall 0.5" in v for v in violations + warnings_)
        finally:
            r.symbol_recall = 1.0
