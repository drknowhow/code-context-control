"""Delegation quality/cost harness for ``c3_delegate``.

Runs a suite of bounded tasks through the SAME entry point the MCP tool uses
(``cli.tools.delegate.handle_delegate``) against one or more targets and
grades each answer with regex checks. A target is a backend name, optionally
with a tier (``claude:small``) once the backend understands tiers. The point
is to answer, with numbers, the one question routing defaults depend on: for
each task type, what is the cheapest target whose answers still pass
(docs/delegate-eval.md).

Two execution modes:

* ``live`` — every case is sent to the real backend. Costs whatever the
  backend costs; ``--record PATH`` keeps the answers.
* ``replay`` — answers come from a recorded file. Deterministic and free;
  CI grades the bundled replay fixture so the checks and the report have
  somewhere to fail without a model in the loop.

Checks per case (all optional, all case-insensitive):

* ``must_match``      every regex must match the answer
* ``any_match``       at least one regex must match
* ``must_not_match``  no regex may match
* ``max_words``       the answer is at most this many words

Gates: ``core`` cases carry what they need in the context or ``file_path``;
``lookup`` cases give neither, so only a delegate that reads the project
itself can pass them. They are aggregated apart.
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import tempfile
import time
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path

GATES = ("core", "lookup")
CHECK_KEYS = ("must_match", "any_match", "must_not_match", "max_words")
TASK_TYPES = ("summarize", "explain", "docstring", "review", "ask", "test", "diagnose", "improve")

SUITE_DIR = Path(__file__).resolve().parents[2] / "tests" / "delegate_eval"
CONTEXT_DIR = SUITE_DIR / "contexts"
PROJECT_DIR = SUITE_DIR / "project"
BUNDLED_SUITES = {"gold": SUITE_DIR / "gold_suite.jsonl"}

# A tier is "recommended" for a task type when its pass rate reaches this
# floor AND is within TOLERANCE of the best target of the same backend.
DEFAULT_FLOOR = 0.8
TOLERANCE = 0.1

_WORD_RE = re.compile(r"\S+")


# ── Suite model ─────────────────────────────────────────────────────────────


@dataclass
class DelegateCase:
    id: str
    task_type: str
    task: str
    gate: str = "core"
    context: str = ""
    context_file: str = ""
    file_path: str = ""
    checks: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict, context_dir: Path = CONTEXT_DIR) -> "DelegateCase":
        cid = str(d.get("id") or "")
        if not cid:
            raise ValueError("case without an id")
        gate = d.get("gate", "core")
        if gate not in GATES:
            raise ValueError(f"case {cid!r}: unknown gate {gate!r}")
        task_type = str(d.get("task_type") or "")
        if task_type not in TASK_TYPES:
            raise ValueError(f"case {cid!r}: unknown task_type {task_type!r}")
        checks = dict(d.get("checks") or {})
        unknown = sorted(set(checks) - set(CHECK_KEYS))
        if unknown:
            raise ValueError(f"case {cid!r}: unknown check(s) {unknown}")
        if not any(checks.get(k) for k in ("must_match", "any_match")):
            raise ValueError(f"case {cid!r}: needs must_match or any_match")
        for key in ("must_match", "any_match", "must_not_match"):
            for pattern in checks.get(key, []):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"case {cid!r}: bad {key} regex {pattern!r}: {exc}") from exc
        context = str(d.get("context") or "")
        context_file = str(d.get("context_file") or "")
        if context_file:
            p = context_dir / context_file
            if not p.exists():
                raise ValueError(f"case {cid!r}: context_file {context_file!r} not found")
            context = p.read_text(encoding="utf-8")
        if gate == "lookup" and (context or d.get("file_path")):
            raise ValueError(f"case {cid!r}: a lookup case gives no context and no file_path")
        return cls(id=cid, task_type=task_type, task=str(d.get("task") or ""), gate=gate,
                   context=context, context_file=context_file,
                   file_path=str(d.get("file_path") or ""), checks=checks)


def load_suite(path: str | Path) -> tuple[dict, list[DelegateCase]]:
    """Read a JSONL suite: first object is the header, the rest are cases."""
    path = Path(path)
    header: dict = {}
    cases: list[DelegateCase] = []
    seen: set[str] = set()
    context_dir = path.parent / "contexts"
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            obj = json.loads(raw)
            if not header and "suite" in obj and "task" not in obj:
                header = obj
                continue
            case = DelegateCase.from_dict(obj, context_dir)
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


def parse_target(spec: str) -> tuple[str, str]:
    """``backend`` or ``backend:tier`` -> (backend, tier)."""
    backend, _, tier = str(spec).strip().partition(":")
    if not backend:
        raise ValueError(f"empty target {spec!r}")
    return backend.lower(), tier.lower()


# ── Checks ──────────────────────────────────────────────────────────────────


def grade(case: DelegateCase, answer: str) -> list[str]:
    """Failed checks for ``answer`` (empty list = pass)."""
    failures: list[str] = []
    text = answer or ""
    flags = re.IGNORECASE | re.MULTILINE
    for pattern in case.checks.get("must_match", []):
        if not re.search(pattern, text, flags):
            failures.append(f"must_match /{pattern}/")
    any_of = case.checks.get("any_match", [])
    if any_of and not any(re.search(p, text, flags) for p in any_of):
        failures.append("any_match: none of " + ", ".join(f"/{p}/" for p in any_of[:4]))
    for pattern in case.checks.get("must_not_match", []):
        if re.search(pattern, text, flags):
            failures.append(f"must_not_match /{pattern}/")
    max_words = case.checks.get("max_words")
    if max_words:
        words = len(_WORD_RE.findall(text))
        if words > int(max_words):
            failures.append(f"max_words {words} > {max_words}")
    return failures


# ── Execution ───────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    id: str
    target: str
    task_type: str
    gate: str
    status: str  # pass | fail | error
    outcome: str = ""  # the delegate's own status (ok, error, blocked, ...)
    reason: str = ""
    checks_failed: list[str] = field(default_factory=list)
    model: str = ""
    wall_ms: float = 0.0
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    answer_words: int = 0


# Delegate statuses that mean no answer came back — graded as ``error``, not
# as a wrong answer, so a broken backend never reads as a dumb one.
_NO_ANSWER = frozenset({"error", "timeout", "blocked", "disabled", "unavailable", "degraded"})


def _num(value, cast):
    if value is None or isinstance(value, bool):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def result_from_answer(case: DelegateCase, target: str, output: str, status: str,
                       meta: dict | None, wall_ms: float) -> CaseResult:
    meta = meta if isinstance(meta, dict) else {}
    outcome = str(status or "").strip().lower()
    if outcome in ("high", "medium", "low"):
        outcome = "ok"
    res = CaseResult(id=case.id, target=target, task_type=case.task_type, gate=case.gate,
                     status="error", outcome=outcome, model=str(meta.get("model") or ""),
                     wall_ms=round(float(wall_ms), 1),
                     cost_usd=_num(meta.get("cost_usd"), float),
                     input_tokens=_num(meta.get("input_tokens"), int),
                     output_tokens=_num(meta.get("output_tokens"), int),
                     answer_words=len(_WORD_RE.findall(output or "")))
    if outcome in _NO_ANSWER or not (output or "").strip():
        first = (output or "").strip().splitlines()
        res.reason = (first[0] if first else f"no answer ({outcome or 'empty'})")[:200]
        return res
    res.checks_failed = grade(case, output)
    res.status = "fail" if res.checks_failed else "pass"
    res.reason = "; ".join(res.checks_failed)[:200]
    return res


def build_eval_svc(project_path: str | Path, delegate_overrides: dict | None = None):
    """The ``svc`` handle_delegate needs, without the MCP runtime.

    Real delegate config (defaults merged with the project's), a real
    compressor for ``file_path`` packing, a real Ollama client, no session
    manager (the harness measures the call itself), no notifications.
    """
    from core.config import load_delegate_config, load_hybrid_config
    from services.compressor import CodeCompressor
    from services.ollama_client import OllamaClient

    project = Path(project_path).resolve()
    dcfg = load_delegate_config(str(project))
    dcfg.update(delegate_overrides or {})
    try:
        hybrid = load_hybrid_config(str(project))
    except Exception:
        hybrid = {}
    activity = types.SimpleNamespace(get_recent=lambda limit=8: [])
    return types.SimpleNamespace(
        project_path=str(project),
        delegate_config=dcfg,
        hybrid_config=hybrid,
        compressor=CodeCompressor(str(project / ".c3" / "cache"), project_root=str(project)),
        ollama_client=OllamaClient(hybrid.get("ollama_base_url", "http://localhost:11434")),
        activity_log=activity,
        notifications=None,
        session_mgr=None,
        _agent_progress_cb=None,
    )


def run_live_case(case: DelegateCase, target: str, svc, *,
                  allow_write_delegation: bool = False) -> tuple[CaseResult, dict]:
    """Send one case through handle_delegate. Returns (result, recording)."""
    from cli.tools.delegate import handle_delegate

    backend, tier = parse_target(target)
    captured: dict = {}

    def finalize(tool, meta, output, status, **kw):
        captured.update(meta=dict(meta or {}), output=output, status=status)
        return output

    kwargs = {"allow_write_delegation": allow_write_delegation}
    if tier:
        kwargs["tier"] = tier
    t0 = time.monotonic()
    try:
        handle_delegate(case.task, case.task_type, case.context, case.file_path,
                        svc, finalize, backend, **kwargs)
    except TypeError as exc:
        if tier and "tier" in str(exc):
            captured = {"meta": {}, "output": f"[eval:error] backend has no tiers yet ({exc})",
                        "status": "error"}
        else:
            captured = {"meta": {}, "output": f"[eval:error] {type(exc).__name__}: {exc}",
                        "status": "error"}
    except Exception as exc:
        captured = {"meta": {}, "output": f"[eval:error] {type(exc).__name__}: {exc}",
                    "status": "error"}
    wall_ms = (time.monotonic() - t0) * 1000
    recording = {"output": captured.get("output", ""), "status": captured.get("status", ""),
                 "meta": captured.get("meta", {}), "wall_ms": round(wall_ms, 1)}
    return (result_from_answer(case, target, recording["output"], recording["status"],
                               recording["meta"], wall_ms), recording)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    idx = max(0, min(99, int(round(q * 100)) - 1))
    return round(quantiles[idx], 1)


def _rate(results: list[CaseResult]) -> float | None:
    """Share of cases that passed. An error counts as not passing: a backend
    that cannot answer has not answered well."""
    if not results:
        return None
    return round(sum(1 for r in results if r.status == "pass") / len(results), 4)


def aggregate_target(results: list[CaseResult]) -> dict:
    core = [r for r in results if r.gate == "core"]
    lookup = [r for r in results if r.gate == "lookup"]
    answered = [r for r in results if r.status != "error"]
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    by_type: dict = {}
    for r in core:
        slot = by_type.setdefault(r.task_type, {"n": 0, "passing": 0})
        slot["n"] += 1
        slot["passing"] += 1 if r.status == "pass" else 0
    for slot in by_type.values():
        slot["pass_rate"] = round(slot["passing"] / slot["n"], 4)
    models: dict = {}
    for r in answered:
        if r.model:
            models[r.model] = models.get(r.model, 0) + 1
    return {
        "n_cases": len(results),
        "n_errors": sum(1 for r in results if r.status == "error"),
        "pass_rate_core": _rate(core),
        "pass_rate_lookup": _rate(lookup),
        "by_task_type": {k: by_type[k] for k in sorted(by_type)},
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_usd_mean": round(statistics.fmean(costs), 6) if costs else None,
        "priced_cases": len(costs),
        "input_tokens_total": sum(r.input_tokens or 0 for r in results) or None,
        "output_tokens_total": sum(r.output_tokens or 0 for r in results) or None,
        "wall_ms_p50": _pct([r.wall_ms for r in answered], 0.50),
        "wall_ms_p95": _pct([r.wall_ms for r in answered], 0.95),
        "models": models,
        "errors": sorted(r.id for r in results if r.status == "error"),
        "failing_core": sorted(r.id for r in core if r.status == "fail"),
    }


def recommend(aggregates: dict[str, dict], floor: float = DEFAULT_FLOOR) -> dict:
    """Per backend and task type: the cheapest tier that still passes.

    A tier qualifies when its pass rate reaches ``floor`` and is within
    ``TOLERANCE`` of the best tier of the same backend on that task type.
    Among qualifiers the lowest mean cost wins; unpriced targets sort last,
    ties keep the target order given on the command line. No qualifier
    means no recommendation — never a guess.
    """
    by_backend: dict[str, list[str]] = {}
    for target in aggregates:
        backend, _tier = parse_target(target)
        by_backend.setdefault(backend, []).append(target)
    out: dict = {}
    for backend, targets in by_backend.items():
        types_seen = sorted({t for tg in targets for t in aggregates[tg]["by_task_type"]})
        rows: dict = {}
        for task_type in types_seen:
            rates = {tg: aggregates[tg]["by_task_type"].get(task_type, {}).get("pass_rate")
                     for tg in targets}
            known = [v for v in rates.values() if v is not None]
            if not known:
                continue
            best = max(known)
            qualifiers = [tg for tg, v in rates.items()
                          if v is not None and v >= floor and v >= best - TOLERANCE]
            if not qualifiers:
                rows[task_type] = {"target": None, "best_pass_rate": best}
                continue

            def cost_key(tg):
                mean = aggregates[tg].get("cost_usd_mean")
                return (mean is None, mean if mean is not None else 0.0, targets.index(tg))

            choice = min(qualifiers, key=cost_key)
            rows[task_type] = {"target": choice, "pass_rate": rates[choice], "best_pass_rate": best}
        out[backend] = rows
    return out


@dataclass
class EvalReport:
    suite: str
    suite_path: str
    mode: str
    targets: list[str]
    results: list[CaseResult]
    aggregates: dict
    recommendation: dict
    floor: float = DEFAULT_FLOOR
    c3_version: str = ""

    def to_dict(self) -> dict:
        return {
            "suite": self.suite,
            "suite_path": self.suite_path,
            "mode": self.mode,
            "targets": self.targets,
            "c3_version": self.c3_version,
            "floor": self.floor,
            "aggregates": self.aggregates,
            "recommendation": self.recommendation,
            "results": [asdict(r) for r in self.results],
        }

    def render(self) -> str:
        lines = [f"c3 delegate-eval — suite={self.suite} mode={self.mode} "
                 f"targets={','.join(self.targets)}"]
        lines.append("")
        lines.append(f"{'case':<28} " + " ".join(f"{t[:18]:>18}" for t in self.targets))
        ids = []
        for r in self.results:
            if r.id not in ids:
                ids.append(r.id)
        index = {(r.id, r.target): r for r in self.results}
        for cid in ids:
            cells = []
            for t in self.targets:
                r = index.get((cid, t))
                if r is None:
                    cells.append(f"{'-':>18}")
                    continue
                mark = {"pass": "pass", "fail": "FAIL", "error": "ERR"}[r.status]
                cost = "" if r.cost_usd is None else f" ${r.cost_usd:.4f}"
                cells.append(f"{(mark + cost):>18}")
            gate = next(r.gate for r in self.results if r.id == cid)
            label = cid if gate == "core" else f"{cid} [lookup]"
            lines.append(f"{label[:28]:<28} " + " ".join(cells))
        lines.append("")
        for t in self.targets:
            a = self.aggregates[t]
            cost = "-" if a["cost_usd_total"] is None else f"${a['cost_usd_total']:.4f} (mean ${a['cost_usd_mean']:.4f})"
            lines.append(f"{t}: core pass={a['pass_rate_core']} lookup pass={a['pass_rate_lookup']} "
                         f"errors={a['n_errors']} cost={cost} wall p50={a['wall_ms_p50']}ms "
                         f"p95={a['wall_ms_p95']}ms models={a['models'] or '-'}")
            types_line = ", ".join(f"{k} {v['passing']}/{v['n']}" for k, v in a["by_task_type"].items())
            if types_line:
                lines.append(f"  by type: {types_line}")
            errs = [r for r in self.results if r.target == t and r.status == "error"]
            for r in errs[:3]:
                lines.append(f"  error {r.id}: {r.reason}")
        if any(len(v) > 1 for v in _targets_by_backend(self.targets).values()):
            lines.append("")
            lines.append(f"cheapest passing tier (floor {self.floor}, tolerance {TOLERANCE}):")
            for backend, rows in self.recommendation.items():
                for task_type, row in rows.items():
                    choice = row.get("target") or "none qualifies"
                    lines.append(f"  {backend} {task_type:<10} -> {choice} "
                                 f"(best {row.get('best_pass_rate')})")
        return "\n".join(lines)


def _targets_by_backend(targets: list[str]) -> dict:
    out: dict = {}
    for t in targets:
        out.setdefault(parse_target(t)[0], []).append(t)
    return out


def load_recording(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data.get("targets"), dict):
        raise ValueError(f"{path}: not a delegate-eval recording (no 'targets' object)")
    return data


def run_suite(suite: str | Path = "gold", *, targets: list[str] | None = None,
              replay: str | Path | None = None, record: str | Path | None = None,
              case_ids: list[str] | None = None, floor: float = DEFAULT_FLOOR,
              allow_write_delegation: bool = False, delegate_overrides: dict | None = None,
              progress=None) -> EvalReport:
    """Run the suite live (default) or from a recording (``replay``).

    Live runs work on a throwaway copy of the fixture project, so a delegate
    that reads or writes the project never touches the checkout.
    """
    suite_path = resolve_suite(str(suite))
    header, cases = load_suite(suite_path)
    if case_ids:
        wanted = set(case_ids)
        missing = sorted(wanted - {c.id for c in cases})
        if missing:
            raise ValueError(f"unknown case id(s): {missing}")
        cases = [c for c in cases if c.id in wanted]
    suite_name = header.get("suite") or suite_path.stem

    results: list[CaseResult] = []
    if replay:
        recording = load_recording(replay)
        targets = targets or list(recording["targets"])
        mode = "replay"
        for target in targets:
            answers = recording["targets"].get(target)
            if answers is None:
                raise ValueError(f"recording has no target {target!r}")
            for case in cases:
                rec = answers.get(case.id)
                if rec is None:
                    results.append(CaseResult(id=case.id, target=target, task_type=case.task_type,
                                              gate=case.gate, status="error",
                                              reason="not in recording"))
                    continue
                results.append(result_from_answer(case, target, rec.get("output", ""),
                                                  rec.get("status", ""), rec.get("meta"),
                                                  rec.get("wall_ms", 0.0)))
    else:
        if not targets:
            raise ValueError("a live run needs at least one target (e.g. --targets claude)")
        mode = "live"
        work = Path(tempfile.mkdtemp(prefix="c3-delegate-eval-"))
        recorded: dict = {"suite": suite_name, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "targets": {}}
        try:
            project = work / "project"
            shutil.copytree(PROJECT_DIR, project)
            svc = build_eval_svc(project, delegate_overrides)
            for target in targets:
                answers = recorded["targets"].setdefault(target, {})
                for case in cases:
                    res, rec = run_live_case(case, target, svc,
                                             allow_write_delegation=allow_write_delegation)
                    results.append(res)
                    answers[case.id] = rec
                    if progress:
                        progress(res)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if record:
            Path(record).parent.mkdir(parents=True, exist_ok=True)
            Path(record).write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")

    aggregates = {t: aggregate_target([r for r in results if r.target == t]) for t in targets}
    try:
        from cli.c3 import __version__ as c3_version
    except Exception:
        c3_version = ""
    return EvalReport(suite=suite_name, suite_path=str(suite_path), mode=mode,
                      targets=list(targets), results=results, aggregates=aggregates,
                      recommendation=recommend(aggregates, floor), floor=floor,
                      c3_version=c3_version)
