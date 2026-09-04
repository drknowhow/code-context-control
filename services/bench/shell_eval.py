"""Output-shaping evaluation harness for ``c3_shell``.

Runs a suite of synthetic command outputs through the SAME renderer the MCP
tool uses (``cli.tools.shell.render_shell_response``) and grades the body the
agent would have read: does it fit the byte budget, did the lines that matter
survive, did a marker land somewhere it breaks the content. No subprocess is
spawned — every case is a deterministic generator in
``tests/shell_eval/generators.py`` producing ``(stdout, stderr, exit_code,
timed_out)`` from a seed (docs/shell-eval.md).

Metrics per case: response bytes and tokens, must-keep retention (share of
the case's ``must_contain`` fragments present in the body), render time.
Aggregates: pass rate per gate, budget violations, must-keep retention rate,
p50/p95 bytes, p50/p95 render ms. A checked-in baseline carries absolute
FLOORS (rates that must not fall) and CEILINGS (sizes that must not grow);
``compare_to_baseline`` turns those into violations (fail CI) and warnings.

Gates per case: ``must_pass`` (a failure is a CI failure), ``xfail`` (known
broken, ``phase`` names the remediation phase — S1 budget/spill, S2
content-aware keep — that fixes it; a pass is reported so the case can be
promoted), ``info`` (measured, never gates).

The report names the renderer it ran (``renderer=``) so a baseline can
never be mistaken for one produced by a different code path.
"""

from __future__ import annotations

import importlib.util
import json
import re
import statistics
import sys
import time
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The S1 response budget: 18 KiB by default, 22 KiB ceiling (docs/shell-eval.md
# § Budget). A case may override max_bytes, never above the ceiling.
DEFAULT_MAX_BYTES = 18 * 1024
CEILING_MAX_BYTES = 22 * 1024

GATES = ("must_pass", "xfail", "info")
PHASES = ("S1", "S2")
CHECK_KEYS = ("max_bytes", "must_contain", "must_not_contain", "must_contain_regex",
              "no_marker_inside", "spill_identical")

SUITE_DIR = Path(__file__).resolve().parents[2] / "tests" / "shell_eval"
GENERATORS_PATH = SUITE_DIR / "generators.py"
BUNDLED_SUITES = {"fixture": SUITE_DIR / "fixture_suite.jsonl"}
BUNDLED_BASELINES = {"fixture": SUITE_DIR / "baseline_fixture.json"}

# An omission / truncation marker: a bracketed note that says something was
# left out — today's filter writes ``[90 lines omitted]`` and ``[12 stack
# frames collapsed]``; S1's clip is expected to use the same shape (a word
# from this list inside square brackets). ``no_marker_inside`` checks that
# such a note never shares a line with the content it interrupts.
MARKER_RE = re.compile(
    r"\[[^\[\]\n]*\b(?:omitted|collapsed|truncated|clipped|elided|spilled|repeated|retained)\b[^\[\]\n]*\]"
    r"|…\s*\[[^\[\]\n]*\]\s*…",
    re.IGNORECASE)


# ── Suite model ─────────────────────────────────────────────────────────────


@dataclass
class ShellCase:
    id: str
    cmd: str
    generator: str
    params: dict = field(default_factory=dict)
    gate: str = "info"
    phase: str | None = None
    filter_output: bool = True
    checks: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    why: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ShellCase":
        gate = d.get("gate", "info")
        if gate not in GATES:
            raise ValueError(f"case {d.get('id')!r}: unknown gate {gate!r}")
        phase = d.get("phase")
        if phase is not None and phase not in PHASES:
            raise ValueError(f"case {d.get('id')!r}: unknown phase {phase!r} (expected one of {PHASES})")
        if gate == "xfail" and not phase:
            raise ValueError(f"case {d.get('id')!r}: xfail must name the phase that fixes it")
        checks = dict(d.get("checks") or {})
        unknown = sorted(set(checks) - set(CHECK_KEYS))
        if unknown:
            raise ValueError(f"case {d.get('id')!r}: unknown check(s) {unknown}")
        if int(checks.get("max_bytes", DEFAULT_MAX_BYTES)) > CEILING_MAX_BYTES:
            raise ValueError(
                f"case {d.get('id')!r}: max_bytes above the {CEILING_MAX_BYTES} ceiling")
        return cls(
            id=str(d["id"]),
            cmd=str(d["cmd"]),
            generator=str(d["generator"]),
            params=dict(d.get("params") or {}),
            gate=gate,
            phase=phase,
            filter_output=bool(d.get("filter_output", True)),
            checks=checks,
            tags=list(d.get("tags", [])),
            why=d.get("why", ""),
        )

    @property
    def max_bytes(self) -> int:
        return int(self.checks.get("max_bytes", DEFAULT_MAX_BYTES))


def load_suite(path: str | Path) -> tuple[dict, list[ShellCase]]:
    """Read a JSONL suite: first object is the header, the rest are cases."""
    path = Path(path)
    header: dict = {}
    cases: list[ShellCase] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            obj = json.loads(raw)
            if lineno == 1 or (not cases and "suite" in obj and "cmd" not in obj):
                header = obj
                continue
            case = ShellCase.from_dict(obj)
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
    """Import ``tests/shell_eval/generators.py`` by path.

    ``tests`` is not a package the wheel ships, so the module is loaded the
    way the search harness locates its fixture repo: relative to this file.
    """
    global _generators_module
    if path is None and _generators_module is not None:
        return _generators_module
    p = Path(path) if path else GENERATORS_PATH
    if not p.exists():
        raise FileNotFoundError(f"generators not found: {p} — run from a C3 source checkout")
    name = "c3_shell_eval_generators"
    spec = importlib.util.spec_from_file_location(name, p)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules[cls.__module__]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if path is None:
        _generators_module = module
    return module


# ── Renderer ────────────────────────────────────────────────────────────────


def build_eval_svc(project_path: str | Path | None = None):
    """The minimal ``svc`` the renderer needs: a project path (where an S1
    spill would land) and the real OutputFilter, pass-2 (LLM) disabled so the
    harness never reaches for Ollama. Mirrors ``services.session_benchmark``."""
    from services.output_filter import OutputFilter

    return types.SimpleNamespace(
        project_path=str(Path(project_path or Path.cwd()).resolve()),
        output_filter=OutputFilter({"HYBRID_DISABLE_TIER1": True}),
        hybrid_config={},
        activity_log=None,
        edit_ledger=None,
        session_mgr=None,
    )


def resolve_renderer():
    """Return ``(callable, name)`` — the renderer the live tool uses."""
    from cli.tools.shell import render_shell_response
    return render_shell_response, "cli.tools.shell.render_shell_response"


# ── Checks ──────────────────────────────────────────────────────────────────


def resolve_fragment(value: str, keep: dict) -> str:
    """``$name`` refers to a generator-provided fragment; anything else is literal."""
    if isinstance(value, str) and value.startswith("$") and value[1:] in keep:
        return keep[value[1:]]
    return value


def marker_inside(body: str, mode: str) -> list[str]:
    """Lines where an omission marker shares the line with the content it
    interrupts. ``json``: the rest of the line still carries object syntax;
    ``table``: the rest still carries a column separator."""
    bad: list[str] = []
    for line in body.splitlines():
        if not MARKER_RE.search(line):
            continue
        rest = MARKER_RE.sub("", line).strip()
        if not rest:
            continue
        if mode == "json" and any(ch in rest for ch in "{}\""):
            bad.append(line[:120])
        elif mode == "table" and "|" in rest:
            bad.append(line[:120])
    return bad


def _read_spill(stats: dict, svc) -> str | None:
    """Best effort at reading what S1 spilled. The storage contract is not
    fixed yet: try a path in the stats, then a reader on the shell module,
    then ``.c3/shell_outputs/<output_id>*`` under the project."""
    path = stats.get("spill_path") or stats.get("output_path")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8", errors="replace")
    output_id = stats.get("output_id")
    if not output_id:
        return None
    try:
        from cli.tools import shell as shell_mod
        reader = getattr(shell_mod, "read_spilled_output", None)
        if reader is not None:
            text = reader(output_id, svc)
            if isinstance(text, str):
                return text
    except Exception:
        pass
    root = Path(getattr(svc, "project_path", ".")) / ".c3" / "shell_outputs"
    if root.is_dir():
        for p in sorted(root.glob(f"{output_id}*")):
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
    return None


def run_checks(case: ShellCase, body: str, stats: dict, streams, svc) -> tuple[list[str], float | None]:
    """Evaluate every check in the case. Returns ``(failures, retention)`` —
    retention is the share of ``must_contain`` fragments present, or None
    when the case names none."""
    failures: list[str] = []
    checks = case.checks
    keep = getattr(streams, "keep", {}) or {}

    if stats.get("response_bytes", 0) > case.max_bytes:
        failures.append(f"max_bytes: {stats['response_bytes']} > {case.max_bytes}")

    retention: float | None = None
    wanted = [resolve_fragment(v, keep) for v in checks.get("must_contain", [])]
    if wanted:
        present = [w for w in wanted if w in body]
        retention = round(len(present) / len(wanted), 4)
        for w in wanted:
            if w not in body:
                failures.append(f"must_contain: {w[:60]!r} missing")

    for v in checks.get("must_not_contain", []):
        v = resolve_fragment(v, keep)
        if v in body:
            failures.append(f"must_not_contain: {v!r} present")

    for pattern in checks.get("must_contain_regex", []):
        if not re.search(pattern, body, re.MULTILINE):
            failures.append(f"must_contain_regex: /{pattern}/ no match")

    mode = checks.get("no_marker_inside")
    if mode:
        if mode not in ("json", "table"):
            failures.append(f"no_marker_inside: unknown mode {mode!r}")
        else:
            bad = marker_inside(body, mode)
            if bad:
                failures.append(f"no_marker_inside({mode}): {len(bad)} line(s), first: {bad[0][:80]!r}")

    if checks.get("spill_identical"):
        if not stats.get("spilled"):
            failures.append("spill_identical: output was not spilled (pre-S1)")
        else:
            spilled = _read_spill(stats, svc)
            raw_out = getattr(streams, "stdout", "") or ""
            raw_err = getattr(streams, "stderr", "") or ""
            if spilled is None:
                failures.append("spill_identical: spill unreadable (no path/reader for output_id)")
            elif not (spilled == raw_out or spilled == raw_out + raw_err
                      or (raw_out in spilled and raw_err in spilled)):
                failures.append("spill_identical: spilled text differs from the raw streams")
    return failures, retention


# ── Execution ───────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    id: str
    cmd: str
    generator: str
    gate: str
    phase: str | None
    status: str  # pass | fail | error
    reason: str = ""
    checks_failed: list[str] = field(default_factory=list)
    response_bytes: int = 0
    response_tokens: int = 0
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    longest_line: int = 0
    filtered: bool = False
    spilled: bool = False
    output_id: str | None = None
    retention: float | None = None
    render_ms: float = 0.0
    over_budget: bool = False


def run_case(case: ShellCase, svc, renderer, generators=None) -> CaseResult:
    gens = generators or load_generators()
    result = CaseResult(id=case.id, cmd=case.cmd, generator=case.generator,
                        gate=case.gate, phase=case.phase, status="error")
    try:
        streams = gens.generate(case.generator, case.params, case_id=case.id)
    except Exception as exc:
        result.reason = f"generator: {type(exc).__name__}: {exc}"
        return result

    t0 = time.perf_counter()
    try:
        body, stats = renderer(case.cmd, streams.as_result(), svc,
                               filter_output=case.filter_output)
    except Exception as exc:
        result.reason = f"renderer: {type(exc).__name__}: {exc}"
        result.render_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result
    result.render_ms = round((time.perf_counter() - t0) * 1000, 1)

    result.response_bytes = int(stats.get("response_bytes", len(body.encode("utf-8", errors="replace"))))
    result.response_tokens = int(stats.get("response_tokens", 0))
    result.stdout_bytes = int(stats.get("stdout_bytes", 0))
    result.stderr_bytes = int(stats.get("stderr_bytes", 0))
    result.longest_line = int(stats.get("longest_line", 0))
    result.filtered = bool(stats.get("filtered"))
    result.spilled = bool(stats.get("spilled"))
    result.output_id = stats.get("output_id")
    result.over_budget = result.response_bytes > case.max_bytes

    failures, retention = run_checks(case, body, stats, streams, svc)
    result.retention = retention
    result.checks_failed = failures
    result.status = "fail" if failures else "pass"
    result.reason = "; ".join(failures)[:200]
    return result


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    idx = max(0, min(99, int(round(q * 100)) - 1))
    return round(quantiles[idx], 1)


def _rate(results: list[CaseResult]) -> float | None:
    if not results:
        return None
    return round(sum(1 for r in results if r.status == "pass") / len(results), 4)


def aggregate(results: list[CaseResult]) -> dict:
    executed = [r for r in results if r.status != "error"]
    kept = [r for r in executed if r.retention is not None]
    agg = {
        "n_cases": len(results),
        "n_executed": len(executed),
        "n_errors": sum(1 for r in results if r.status == "error"),
        "pass_rate_must_pass": _rate([r for r in results if r.gate == "must_pass"]),
        "pass_rate_xfail": _rate([r for r in results if r.gate == "xfail"]),
        "pass_rate_info": _rate([r for r in results if r.gate == "info"]),
        "under_budget_rate": (round(sum(1 for r in executed if not r.over_budget) / len(executed), 4)
                              if executed else None),
        "budget_violations": sorted(r.id for r in executed if r.over_budget),
        "must_keep_retention": (round(statistics.fmean(r.retention for r in kept), 4) if kept else None),
        "must_keep_retention_must_pass": (
            round(statistics.fmean(r.retention for r in kept if r.gate == "must_pass"), 4)
            if any(r.gate == "must_pass" for r in kept) else None),
        "bytes_p50": _pct([float(r.response_bytes) for r in executed], 0.50),
        "bytes_p95": _pct([float(r.response_bytes) for r in executed], 0.95),
        "tokens_p50": _pct([float(r.response_tokens) for r in executed], 0.50),
        "tokens_p95": _pct([float(r.response_tokens) for r in executed], 0.95),
        "render_ms_p50": _pct([r.render_ms for r in executed], 0.50),
        "render_ms_p95": _pct([r.render_ms for r in executed], 0.95),
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
            "default_max_bytes": DEFAULT_MAX_BYTES,
            "aggregates": self.aggregates,
            "results": [asdict(r) for r in self.results],
            "baseline_violations": self.baseline_violations,
            "baseline_warnings": self.baseline_warnings,
        }

    def render(self) -> str:
        lines = [f"c3 shell-eval — suite={self.suite} renderer={self.renderer} "
                 f"budget={DEFAULT_MAX_BYTES}B"]
        lines.append("")
        lines.append(f"{'id':<26} {'gate':<9} {'ph':<3} {'status':<6} {'bytes':>9} {'tok':>7} "
                     f"{'keep':>5} {'ms':>7}  note")
        for r in self.results:
            keep = "-" if r.retention is None else f"{r.retention:.2f}"
            note = r.reason
            if r.status == "pass" and r.gate == "xfail":
                note = "XFAIL PASSING — promote it and refresh the baseline"
            flags = ("F" if r.filtered else "") + ("S" if r.spilled else "")
            if flags:
                note = f"[{flags}] {note}"
            lines.append(f"{r.id:<26} {r.gate:<9} {(r.phase or '-'):<3} {r.status:<6} "
                         f"{r.response_bytes:>9} {r.response_tokens:>7} {keep:>5} {r.render_ms:>7.1f}  {note}")
        a = self.aggregates
        lines.append("")
        lines.append(
            f"pass rate: must_pass={a['pass_rate_must_pass']} xfail={a['pass_rate_xfail']} "
            f"info={a['pass_rate_info']} | under_budget={a['under_budget_rate']} "
            f"({len(a['budget_violations'])} over) | must_keep_retention={a['must_keep_retention']}")
        lines.append(
            f"bytes p50={a['bytes_p50']} p95={a['bytes_p95']} | tokens p50={a['tokens_p50']} "
            f"p95={a['tokens_p95']} | render ms p50={a['render_ms_p50']} p95={a['render_ms_p95']}")
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
    """Load a suite, render every case through the resolved renderer, compare
    to a baseline. ``work_dir`` (a temp dir when None) is the project path the
    renderer sees, so an S1 spill never lands in a real ``.c3``."""
    import tempfile

    suite_path = resolve_suite(str(suite))
    header, cases = load_suite(suite_path)
    suite_name = header.get("suite") or suite_path.stem

    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="c3-shell-eval-")
    svc = build_eval_svc(work_dir)
    renderer, renderer_name = resolve_renderer()
    gens = load_generators(generators_path) if generators_path else load_generators()

    results = [run_case(c, svc, renderer, gens) for c in cases]
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

FLOOR_METRICS = ("pass_rate_must_pass", "under_budget_rate", "must_keep_retention",
                 "must_keep_retention_must_pass")
CEILING_METRICS = ("bytes_p50", "bytes_p95", "tokens_p95")


def load_baseline(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def compare_to_baseline(report: EvalReport, baseline: dict) -> tuple[list[str], list[str]]:
    """Return (violations, warnings).

    Violations fail CI: a must_pass case failing or erroring, an aggregate
    under its floor, an aggregate over its ceiling. Warnings inform: an
    info/xfail case that passed in the baseline and fails now, or an xfail
    that now passes.
    """
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
        "default_max_bytes": DEFAULT_MAX_BYTES,
        "aggregates": {k: v for k, v in report.aggregates.items() if k != "by_phase"},
        "by_phase": report.aggregates.get("by_phase", {}),
        "floors": floors or {},
        "ceilings": ceilings or {},
        "per_case": {r.id: {"status": r.status, "gate": r.gate, "phase": r.phase,
                            "response_bytes": r.response_bytes, "retention": r.retention}
                     for r in report.results},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return data
