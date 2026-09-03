"""Relevance evaluation harness for ``c3_search``.

Runs a suite of queries through the SAME code path the MCP tool uses
(``cli.tools.search.handle_search``) and grades what comes back against
expected files and symbols. Two suite kinds exist (docs/search-eval.md):

* ``fixture`` — the committed synthetic repo under
  ``tests/fixtures/search_eval_repo``. Deterministic, built into a temp dir on
  every run, gated per query in CI.
* ``golden`` — real-repo queries (C3 itself by default). Run from the CLI
  against the live ``.c3`` index; too slow and environment-bound for CI.

Metrics: recall@1/3/10, MRR, symbol recall@3, zero-result accuracy, latency
p50/p95, index build cost. A checked-in baseline carries absolute FLOORS for
the aggregates and a per-query status table; ``compare_to_baseline`` turns
those into violations (fail CI) and warnings (print).

Gates per query: ``must_pass`` (a failure is a CI failure), ``xfail`` (known
broken, names the phase that fixes it; a pass is reported so the baseline can
be updated), ``info`` (measured, never gates).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import time
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The tool caps top_k at 10 and max_tokens at 2400; the agent-facing defaults
# are top_k=3, max_tokens=1200. Ranking metrics need the deepest list the tool
# can return, so top_k=10; the token budget stays at the agent default because
# "a chunk too large for the default budget is never returned" is one of the
# behaviours this suite exists to measure.
DEFAULT_TOP_K = 10
DEFAULT_MAX_TOKENS = 1200
DEFAULT_K = 3

GATES = ("must_pass", "xfail", "info")
SUITE_DIR = Path(__file__).resolve().parents[2] / "tests" / "search_eval"
FIXTURE_REPO = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "search_eval_repo"
BUNDLED_SUITES = {
    "fixture": SUITE_DIR / "fixture_suite.jsonl",
    "golden": SUITE_DIR / "golden_c3.jsonl",
}
BUNDLED_BASELINES = {
    "fixture": SUITE_DIR / "baseline_fixture.json",
}


# ── Suite model ─────────────────────────────────────────────────────────────


@dataclass
class QueryCase:
    id: str
    query: str
    action: str = "code"
    expect_files: list[str] = field(default_factory=list)
    expect_symbols: list[str] = field(default_factory=list)
    expect_none: bool = False
    require_symbol: bool = False
    k: int = DEFAULT_K
    gate: str = "info"
    fixed_by: str = ""
    why: str = ""
    forbid_text: list[str] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    top_k: int = DEFAULT_TOP_K
    max_tokens: int = DEFAULT_MAX_TOKENS
    tags: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)  # extra handle_search kwargs, e.g. ignore_case

    @classmethod
    def from_dict(cls, d: dict) -> "QueryCase":
        expect = d.get("expect") or {}
        gate = d.get("gate", "info")
        if gate not in GATES:
            raise ValueError(f"case {d.get('id')!r}: unknown gate {gate!r}")
        return cls(
            id=str(d["id"]),
            query=str(d["query"]),
            action=d.get("action", "code"),
            expect_files=[_norm_path(p) for p in expect.get("files", [])],
            expect_symbols=list(expect.get("symbols", [])),
            expect_none=bool(expect.get("none", False)),
            require_symbol=bool(d.get("require_symbol", False)),
            k=int(d.get("k", DEFAULT_K)),
            gate=gate,
            fixed_by=d.get("fixed_by", ""),
            why=d.get("why", ""),
            forbid_text=list(d.get("forbid_text", [])),
            filters=dict(d.get("filters") or {}),
            top_k=int(d.get("top_k", DEFAULT_TOP_K)),
            max_tokens=int(d.get("max_tokens", DEFAULT_MAX_TOKENS)),
            tags=list(d.get("tags", [])),
            params=dict(d.get("params") or {}),
        )


@dataclass
class Hit:
    file: str
    lines: str = ""
    name: str = ""
    type: str = ""


@dataclass
class QueryResult:
    id: str
    action: str
    query: str
    gate: str
    status: str  # pass | fail | skip
    rank: int | None = None
    symbol_rank: int | None = None
    latency_ms: float = 0.0
    hits: list[Hit] = field(default_factory=list)
    reason: str = ""
    forbidden_found: list[str] = field(default_factory=list)
    expect_none: bool = False
    has_symbols: bool = False

    @property
    def scored(self) -> bool:
        """Counts toward recall/MRR: executed and had an expected file."""
        return self.status != "skip" and not self.expect_none


def load_suite(path: str | Path) -> tuple[dict, list[QueryCase]]:
    """Read a JSONL suite: first object is the header, the rest are cases."""
    path = Path(path)
    header: dict = {}
    cases: list[QueryCase] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            obj = json.loads(raw)
            if lineno == 1 or (not cases and "suite" in obj and "query" not in obj):
                header = obj
                continue
            case = QueryCase.from_dict(obj)
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


# ── Runtime ─────────────────────────────────────────────────────────────────


@dataclass
class IndexStats:
    files_indexed: int = 0
    chunks: int = 0
    oversize_chunks: int = 0
    build_seconds: float | None = None
    file_memory_seconds: float | None = None
    tracked_files: int = 0
    file_memory_coverage: float | None = None
    index_bytes: int = 0
    semantic: str = "off"  # off | ready | unavailable:<reason>


@dataclass
class EvalRuntime:
    project_path: str
    svc: object
    stats: IndexStats


def prepare_fixture(fixture_src: str | Path, work_dir: str | Path,
                    c3_config: dict | None = None) -> Path:
    """Copy the fixture tree to ``work_dir/repo`` and write its ``.c3/config.json``.

    The fixture is never indexed in place: ``CodeIndex`` and ``FileMemoryStore``
    write under ``<project>/.c3``, which must not land in the source tree.
    """
    src = Path(fixture_src)
    if not src.is_dir():
        raise FileNotFoundError(f"fixture repo not found: {src}")
    dest = Path(work_dir) / "repo"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    # Pin every file to one mtime. The indexer multiplies scores by a recency
    # factor derived from mtime/max_mtime; after a checkout the spread is
    # microseconds, which is invisible in a score but decides EXACT ties. A
    # gate that flips with checkout timing is not a gate.
    pinned = 1_600_000_000
    for p in dest.rglob("*"):
        if p.is_file():
            try:
                os.utime(p, (pinned, pinned))
            except OSError:
                pass
    c3_dir = dest / ".c3"
    c3_dir.mkdir(exist_ok=True)
    (c3_dir / "config.json").write_text(
        json.dumps(c3_config or {}, indent=2), encoding="utf-8")
    return dest


def build_eval_runtime(project_path: str | Path, *, rebuild: bool = True,
                       semantic: str = "off", populate_file_memory: bool = True,
                       max_tokens: int = DEFAULT_MAX_TOKENS) -> EvalRuntime:
    """Build the minimal ``svc`` ``handle_search`` needs, plus index stats.

    ``semantic``: ``off`` never touches Ollama/chromadb; ``auto`` uses the
    embedding index when its backends are ready; ``on`` additionally builds
    the embeddings (slow, needs Ollama) when they are missing or stale.
    """
    from services.file_memory import FileMemoryStore
    from services.indexer import CodeIndex

    project = str(Path(project_path).resolve())
    stats = IndexStats()

    indexer = CodeIndex(project)
    index_file = indexer.index_dir / "index.json"
    if rebuild or not index_file.exists():
        t0 = time.perf_counter()
        indexer.build_index()
        stats.build_seconds = round(time.perf_counter() - t0, 3)
    else:
        indexer._load_index()
    stats.files_indexed = len(indexer.documents)
    stats.chunks = len(indexer.chunks)
    stats.oversize_chunks = sum(
        1 for c in indexer.chunks.values() if int(c.get("tokens") or 0) > max_tokens)
    if index_file.exists():
        stats.index_bytes = index_file.stat().st_size

    file_memory = FileMemoryStore(project)
    if populate_file_memory:
        t0 = time.perf_counter()
        for rel in indexer.documents:
            try:
                file_memory.update(rel.replace("\\", "/"))
            except Exception:
                pass
        stats.file_memory_seconds = round(time.perf_counter() - t0, 3)
    tracked = [t for t in file_memory.list_tracked() if t]
    stats.tracked_files = len(tracked)
    if stats.files_indexed:
        stats.file_memory_coverage = round(len(tracked) / stats.files_indexed, 3)

    embedding_index = None
    if semantic in ("auto", "on"):
        embedding_index, stats.semantic = _embedding_index(project, indexer, build=(semantic == "on"))
    else:
        stats.semantic = "off"

    svc = types.SimpleNamespace(
        project_path=project,
        indexer=indexer,
        file_memory=file_memory,
        embedding_index=embedding_index,
        hybrid_config={},
        compressor=None,
        convo_store=None,
    )
    return EvalRuntime(project_path=project, svc=svc, stats=stats)


def _embedding_index(project: str, indexer, *, build: bool):
    try:
        from core.config import load_hybrid_config
        from services.embedding_index import EmbeddingIndex
        from services.ollama_client import OllamaClient

        cfg = load_hybrid_config(project) or {}
        client = OllamaClient(cfg.get("ollama_base_url", "http://localhost:11434"))
        ei = EmbeddingIndex(project, client, embed_model=cfg.get("embed_model", "nomic-embed-text"))
        probe = ei.probe()
        if not probe.get("ready"):
            return None, f"unavailable:{ei.unavailable_reason() or 'backends not ready'}"
        if build:
            ei.build(indexer)
        if ei.get_stats().get("total_embedded_chunks", 0) == 0:
            return None, "unavailable:no embedded chunks (run with --semantic on to build)"
        return ei, "ready"
    except Exception as exc:  # pragma: no cover - environment dependent
        return None, f"unavailable:{type(exc).__name__}"


# ── Response parsing ────────────────────────────────────────────────────────

_RE_CHUNK = re.compile(
    r"^--- (?P<file>.+?):L(?P<lines>[^ ]+)(?: (?P<name>.+?))? \((?P<type>[^()]+)\)\s*$")
_RE_FILE_ROW = re.compile(
    r"^- (?P<file>.+?) \(L(?P<lines>[^)]+)\)(?:.*?contains (?P<type>\S+) '(?P<name>[^']*)')?")
_RE_EXACT = re.compile(r"^--- (?P<file>.+?) ---\s*$")


def parse_hits(action: str, response: str) -> list[Hit]:
    """Extract ordered hits from a ``handle_search`` response string."""
    hits: list[Hit] = []
    for line in (response or "").splitlines():
        if line.startswith("[c3-access") or line.startswith("[c3-mask"):
            continue
        if action == "exact":
            m = _RE_EXACT.match(line)
            if m:
                hits.append(Hit(file=_norm_path(m.group("file"))))
            continue
        if action == "files":
            m = _RE_FILE_ROW.match(line)
            if m:
                hits.append(Hit(file=_norm_path(m.group("file")), lines=m.group("lines"),
                                name=m.group("name") or "", type=m.group("type") or ""))
            continue
        m = _RE_CHUNK.match(line)
        if m:
            hits.append(Hit(file=_norm_path(m.group("file")), lines=m.group("lines"),
                            name=(m.group("name") or "").strip(), type=m.group("type")))
    return hits


def _norm_path(p: str) -> str:
    p = (p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def _symbol_matches(name: str, symbol: str) -> bool:
    if not name or not symbol:
        return False
    if name == symbol or name.endswith("." + symbol):
        return True
    return name.split(".")[-1] == symbol


# ── Execution ───────────────────────────────────────────────────────────────


def _noop_finalize(_name, _args, resp, _summ="", **_kw):
    return resp


def _noop_facts(*_a, **_kw):
    return ""


def run_case(case: QueryCase, rt: EvalRuntime) -> QueryResult:
    result = QueryResult(id=case.id, action=case.action, query=case.query, gate=case.gate,
                         status="skip", expect_none=case.expect_none,
                         has_symbols=bool(case.expect_symbols))
    if case.filters:
        result.reason = "filters are not a c3_search parameter yet (P2)"
        return result
    if case.action == "transcript":
        result.reason = "transcript search is out of scope for this harness"
        return result
    if case.action == "semantic":
        ei = getattr(rt.svc, "embedding_index", None)
        if ei is None or not getattr(ei, "ready", False):
            result.reason = f"embeddings {rt.stats.semantic}"
            return result

    from cli.tools.search import handle_search

    t0 = time.perf_counter()
    try:
        response = handle_search(case.query, case.action, case.top_k, case.max_tokens,
                                 rt.svc, _noop_finalize, _noop_facts, **case.params)
    except Exception as exc:
        result.status = "fail"
        result.reason = f"{type(exc).__name__}: {exc}"
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result
    result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    result.hits = parse_hits(case.action, response)

    # The tool echoes the query in its headers ("[search:exact:<query>] 0
    # results"), so a canary used AS the query would trip its own check. Judge
    # only what came back from the index.
    body = response.replace(case.query, "")
    result.forbidden_found = [t for t in case.forbid_text if t and t in body]
    if result.forbidden_found:
        result.status = "fail"
        result.reason = "forbidden text present in response"
        return result

    if case.expect_none:
        result.status = "pass" if not result.hits else "fail"
        if result.hits:
            result.reason = f"{len(result.hits)} hit(s) for a query with no valid answer"
        return result

    for i, hit in enumerate(result.hits, 1):
        if result.rank is None and hit.file in case.expect_files:
            result.rank = i
        if result.symbol_rank is None and any(_symbol_matches(hit.name, s) for s in case.expect_symbols):
            result.symbol_rank = i
    decisive = result.symbol_rank if case.require_symbol else result.rank
    if decisive is not None and decisive <= case.k:
        result.status = "pass"
    else:
        result.status = "fail"
        if not result.hits:
            result.reason = "0 results"
        elif decisive is None:
            top = result.hits[0]
            result.reason = f"expected not in top {len(result.hits)}; first hit {top.file}"
            if top.name:
                result.reason += f" {top.name}"
        else:
            result.reason = f"found at rank {decisive} > k={case.k}"
    return result


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    idx = max(0, min(99, int(round(q * 100)) - 1))
    return round(quantiles[idx], 1)


def aggregate(results: list[QueryResult]) -> dict:
    scored = [r for r in results if r.scored]
    none_cases = [r for r in results if r.expect_none and r.status != "skip"]
    executed = [r for r in results if r.status != "skip"]
    symbol_cases = [r for r in scored if r.has_symbols]

    def recall_at(n: int) -> float | None:
        if not scored:
            return None
        return round(sum(1 for r in scored if r.rank is not None and r.rank <= n) / len(scored), 4)

    agg = {
        "n_cases": len(results),
        "n_executed": len(executed),
        "n_scored": len(scored),
        "n_skipped": sum(1 for r in results if r.status == "skip"),
        "recall_at_1": recall_at(1),
        "recall_at_3": recall_at(3),
        "recall_at_10": recall_at(10),
        "mrr": (round(statistics.fmean(1.0 / r.rank if r.rank else 0.0 for r in scored), 4)
                if scored else None),
        "symbol_recall_at_3": (round(sum(1 for r in symbol_cases
                                         if r.symbol_rank is not None and r.symbol_rank <= 3)
                                     / len(symbol_cases), 4) if symbol_cases else None),
        "zero_result_accuracy": (round(sum(1 for r in none_cases if r.status == "pass")
                                       / len(none_cases), 4) if none_cases else None),
        "latency_p50_ms": _pct([r.latency_ms for r in executed], 0.50),
        "latency_p95_ms": _pct([r.latency_ms for r in executed], 0.95),
        "must_pass_failed": sorted(r.id for r in results if r.gate == "must_pass" and r.status == "fail"),
        "xfail_passing": sorted(r.id for r in results if r.gate == "xfail" and r.status == "pass"),
        "forbidden_hits": sorted(r.id for r in results if r.forbidden_found),
        "by_action": {},
    }
    for action in sorted({r.action for r in scored}):
        sub = [r for r in scored if r.action == action]
        agg["by_action"][action] = {
            "n": len(sub),
            "recall_at_3": round(sum(1 for r in sub if r.rank is not None and r.rank <= 3) / len(sub), 4),
        }
    return agg


@dataclass
class EvalReport:
    suite: str
    suite_path: str
    project_path: str
    header: dict
    stats: IndexStats
    results: list[QueryResult]
    aggregates: dict
    c3_version: str = ""
    baseline_violations: list[str] = field(default_factory=list)
    baseline_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "suite": self.suite,
            "suite_path": self.suite_path,
            "project_path": self.project_path,
            "c3_version": self.c3_version,
            "index": asdict(self.stats),
            "aggregates": self.aggregates,
            "results": [asdict(r) for r in self.results],
            "baseline_violations": self.baseline_violations,
            "baseline_warnings": self.baseline_warnings,
        }

    def render(self) -> str:
        lines = [f"c3 search-eval — suite={self.suite} project={self.project_path}"]
        s = self.stats
        lines.append(
            f"index: files={s.files_indexed} chunks={s.chunks} oversize(>{DEFAULT_MAX_TOKENS}tok)="
            f"{s.oversize_chunks} build={s.build_seconds}s file_memory_coverage={s.file_memory_coverage} "
            f"semantic={s.semantic}")
        lines.append("")
        lines.append(f"{'id':<28} {'action':<8} {'gate':<9} {'status':<5} {'rank':>4} {'ms':>7}  note")
        for r in self.results:
            rank = "-" if r.rank is None else str(r.rank)
            note = r.reason
            if r.status == "pass" and r.gate == "xfail":
                note = "XFAIL PASSING — update the baseline"
            lines.append(f"{r.id:<28} {r.action:<8} {r.gate:<9} {r.status:<5} {rank:>4} "
                         f"{r.latency_ms:>7.1f}  {note}")
        a = self.aggregates
        lines.append("")
        lines.append(
            f"recall@1={a['recall_at_1']} recall@3={a['recall_at_3']} recall@10={a['recall_at_10']} "
            f"mrr={a['mrr']} symbol_recall@3={a['symbol_recall_at_3']} "
            f"zero_result_accuracy={a['zero_result_accuracy']}")
        lines.append(
            f"latency p50={a['latency_p50_ms']}ms p95={a['latency_p95_ms']}ms | scored={a['n_scored']} "
            f"skipped={a['n_skipped']} of {a['n_cases']}")
        for action, sub in a["by_action"].items():
            lines.append(f"  {action:<9} n={sub['n']:<3} recall@3={sub['recall_at_3']}")
        if a["must_pass_failed"]:
            lines.append("MUST-PASS FAILURES: " + ", ".join(a["must_pass_failed"]))
        if a["forbidden_hits"]:
            lines.append("FORBIDDEN TEXT LEAKED: " + ", ".join(a["forbidden_hits"]))
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


def run_suite(suite: str | Path = "fixture", *, repo: str | Path | None = None,
              work_dir: str | Path | None = None, rebuild: bool | None = None,
              semantic: str = "off", baseline: str | Path | None = None) -> EvalReport:
    """Load a suite, build its runtime, run every case, compare to a baseline.

    For a fixture suite the repo is copied into ``work_dir`` (a temp dir when
    None) and indexed there. For a real repo (``header['repo']`` or ``repo``)
    the live ``.c3`` index is used unless ``rebuild`` is True, and
    ``file_memory`` is left exactly as the agent sees it.
    """
    import tempfile

    suite_path = resolve_suite(str(suite))
    header, cases = load_suite(suite_path)
    suite_name = header.get("suite") or suite_path.stem
    is_fixture = bool(header.get("fixture", suite_name == "fixture"))

    if repo is not None:
        project = Path(repo)
        is_fixture = False
    else:
        project = Path(header.get("repo", "."))
        if not project.is_absolute():
            project = (suite_path.parent.parent.parent / project).resolve() if is_fixture \
                else Path.cwd() / project

    if is_fixture:
        tmp = None
        if work_dir is None:
            tmp = tempfile.mkdtemp(prefix="c3-search-eval-")
            work_dir = tmp
        project = prepare_fixture(project, work_dir, header.get("c3_config"))
        rt = build_eval_runtime(project, rebuild=True, semantic=semantic,
                                populate_file_memory=True)
    else:
        rt = build_eval_runtime(project, rebuild=bool(rebuild), semantic=semantic,
                                populate_file_memory=False)

    results = [run_case(c, rt) for c in cases]
    try:
        from cli.c3 import __version__ as c3_version
    except Exception:
        c3_version = ""
    report = EvalReport(suite=suite_name, suite_path=str(suite_path), project_path=rt.project_path,
                        header=header, stats=rt.stats, results=results,
                        aggregates=aggregate(results), c3_version=c3_version)

    baseline_path = Path(baseline) if baseline else BUNDLED_BASELINES.get(suite_name)
    if baseline_path and Path(baseline_path).exists():
        violations, warnings = compare_to_baseline(report, load_baseline(baseline_path))
        report.baseline_violations = violations
        report.baseline_warnings = warnings
    return report


# ── Baseline ────────────────────────────────────────────────────────────────

FLOOR_METRICS = ("recall_at_1", "recall_at_3", "recall_at_10", "mrr",
                 "symbol_recall_at_3", "zero_result_accuracy")


def load_baseline(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def compare_to_baseline(report: EvalReport, baseline: dict) -> tuple[list[str], list[str]]:
    """Return (violations, warnings).

    Violations fail CI: a must_pass case failing, forbidden text leaking, or an
    aggregate under its floor. Warnings inform: an info/xfail case that passed
    in the baseline and fails now, or an xfail that now passes.
    """
    violations: list[str] = []
    warnings: list[str] = []
    a = report.aggregates
    for cid in a["must_pass_failed"]:
        r = next(x for x in report.results if x.id == cid)
        violations.append(f"must_pass case {cid} failed: {r.reason}")
    for cid in a["forbidden_hits"]:
        violations.append(f"case {cid} leaked forbidden text")
    for metric, floor in (baseline.get("floors") or {}).items():
        value = a.get(metric)
        if value is None or floor is None:
            continue
        if value < floor:
            violations.append(f"{metric}={value} below floor {floor}")
    prev = baseline.get("per_query") or {}
    for r in report.results:
        before = prev.get(r.id)
        if not before:
            continue
        if before.get("status") == "pass" and r.status == "fail" and r.gate != "must_pass":
            warnings.append(f"{r.id} passed in baseline, fails now: {r.reason}")
        if before.get("status") == "fail" and r.status == "pass" and r.gate == "xfail":
            warnings.append(f"{r.id} is xfail but now passes — promote it and refresh the baseline")
    return violations, warnings


def write_baseline(report: EvalReport, path: str | Path, *, floors: dict | None = None,
                   keep_floors: bool = True) -> dict:
    """Persist aggregates + per-query status. Floors are hand-set: kept from the
    existing file unless ``floors`` is given explicitly."""
    path = Path(path)
    existing = load_baseline(path) if path.exists() else {}
    if floors is None and keep_floors:
        floors = existing.get("floors") or {}
    data = {
        "suite": report.suite,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "c3_version": report.c3_version,
        "index": asdict(report.stats),
        "aggregates": {k: v for k, v in report.aggregates.items() if k != "by_action"},
        "by_action": report.aggregates.get("by_action", {}),
        "floors": floors or {},
        "per_query": {r.id: {"status": r.status, "rank": r.rank, "gate": r.gate}
                      for r in report.results},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return data
