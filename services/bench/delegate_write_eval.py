"""Write-mode quality/cost harness for ``c3_delegate(write_paths=...)``.

Each case is a change a lead agent has already decided, written the way the
lead would hand it over. Per case and target: a fresh copy of
``fixtures/write_project`` (plus a ``.env`` canary), one call through
``handle_delegate`` with the case's write set, then grading on the files the
worker left behind:

* ``pytest``          paths that must pass; ``@checks/x`` is a hidden check,
                      copied in only after the worker finished
* ``mutants``         each ``{file, old, new}`` applied alone must make
                      ``mutant_pytest`` fail (tests the worker wrote must bite)
* ``min_tests``       ``{path, n}``: at least n ``def test_`` in that file
* ``max_count``       ``[{paths, pattern, max}]``: a regex's occurrences
* ``script``          ``@checks/x.py <work> <fixture>`` must exit 0
* ``unchanged``       paths inside the write set that must stay byte-identical
* ``report_any_match`` a regex the worker's own report must match (it said
                      what it could not do)

Always: every file outside the write set is byte-identical to the fixture.

``gold`` mode overlays ``fixtures/write_gold/<case>`` instead of calling a
model, so CI proves each case's checks pass on a correct change and fail on
the untouched fixture.

Run: ``c3 delegate-eval --suite write --targets claude:small,claude:medium``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from services.bench.delegate_eval import SUITE_DIR, build_eval_svc, parse_target, target_overrides

WRITE_SUITE = SUITE_DIR / "write_suite.jsonl"
FIXTURES = SUITE_DIR / "fixtures"
PROJECT_DIR = FIXTURES / "write_project"
CHECKS_DIR = FIXTURES / "write_checks"
GOLD_DIR = FIXTURES / "write_gold"
ENV_CANARY = "c3-write-eval-canary-51c9"
CHECK_KEYS = ("pytest", "mutants", "mutant_pytest", "min_tests", "max_count", "script",
              "unchanged", "report_any_match")
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".c3", "_c3_checks"}
_PYTEST_TIMEOUT = 120


@dataclass
class WriteCase:
    id: str
    task: str
    write_paths: str
    checks: dict = field(default_factory=dict)
    context: str = ""
    file_path: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "WriteCase":
        cid = str(d.get("id") or "")
        if not cid:
            raise ValueError("case without an id")
        checks = dict(d.get("checks") or {})
        unknown = sorted(set(checks) - set(CHECK_KEYS))
        if unknown:
            raise ValueError(f"case {cid!r}: unknown check(s) {unknown}")
        if not (checks.get("pytest") or checks.get("script")):
            raise ValueError(f"case {cid!r}: needs pytest or script")
        if bool(checks.get("mutants")) != bool(checks.get("mutant_pytest")):
            raise ValueError(f"case {cid!r}: mutants and mutant_pytest go together")
        if not str(d.get("write_paths") or "").strip():
            raise ValueError(f"case {cid!r}: needs write_paths")
        return cls(id=cid, task=str(d.get("task") or ""), write_paths=str(d["write_paths"]),
                   checks=checks, context=str(d.get("context") or ""),
                   file_path=str(d.get("file_path") or ""))


def load_write_suite(path: str | Path = WRITE_SUITE) -> tuple[dict, list[WriteCase]]:
    header: dict = {}
    cases: list[WriteCase] = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if "suite" in row and "id" not in row:
            header = row
            continue
        try:
            cases.append(WriteCase.from_dict(row))
        except ValueError as exc:
            raise ValueError(f"{path}:{n}: {exc}") from exc
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate case ids")
    return header, cases


# ── Grading ─────────────────────────────────────────────────────────────────


def _files(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if p.is_file() and not (_IGNORED_PARTS & set(p.relative_to(root).parts)):
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _pytest(work: Path, paths: list[str]) -> tuple[int, str]:
    args = [str(work / "_c3_checks" / p[len("@checks/"):]) if p.startswith("@checks/") else p
            for p in paths]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                              cwd=work, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=_PYTEST_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return 124, "pytest timed out"
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    return proc.returncode, tail


def worker_report(response: str) -> str:
    """The worker's own words: the rendered section between its markers."""
    text = response or ""
    start = text.find("--- worker report ---")
    if start < 0:
        return ""
    body = text[start + len("--- worker report ---"):]
    end = body.find("\n--- diff ---")
    return (body if end < 0 else body[:end]).strip()


def grade(case: WriteCase, work: Path, report: str = "") -> list[str]:
    """Failure reasons for the tree in ``work``; empty means pass."""
    from services import delegate_write as dw

    reasons: list[str] = []
    checks = case.checks
    globs, problem = dw.parse_write_paths(case.write_paths)
    if problem:
        return [f"bad write_paths: {problem}"]
    before, after = _files(PROJECT_DIR), _files(work)
    before[".env"] = f"PAYMENT_API_KEY={ENV_CANARY}\n".encode()
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) != after.get(rel) and not dw.in_write_set(rel, globs):
            reasons.append(f"changed outside the write set: {rel}")
    for rel in checks.get("unchanged", []):
        if before.get(rel) != after.get(rel):
            reasons.append(f"must stay unchanged: {rel}")

    checks_dst = work / "_c3_checks"
    shutil.rmtree(checks_dst, ignore_errors=True)
    shutil.copytree(CHECKS_DIR, checks_dst)
    try:
        if checks.get("pytest"):
            code, tail = _pytest(work, checks["pytest"])
            if code != 0:
                reasons.append(f"pytest exit {code}: {tail}")
        spec = checks.get("min_tests")
        if spec:
            p = work / spec["path"]
            n = len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.M)) if p.is_file() else 0
            if n < int(spec["n"]):
                reasons.append(f"{spec['path']}: {n} test(s), need {spec['n']}")
        for rule in checks.get("max_count", []):
            count = 0
            for rel in rule["paths"]:
                p = work / rel
                if p.is_file():
                    count += len(re.findall(rule["pattern"], p.read_text(encoding="utf-8")))
            if count > int(rule["max"]):
                reasons.append(f"/{rule['pattern']}/ occurs {count}x (max {rule['max']})")
        if checks.get("script"):
            script = CHECKS_DIR / checks["script"][len("@checks/"):]
            proc = subprocess.run([sys.executable, str(script), str(work), str(PROJECT_DIR)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=_PYTEST_TIMEOUT)
            if proc.returncode != 0:
                reasons.append(f"script: {(proc.stderr or proc.stdout).strip()[-300:]}")
        for i, mutant in enumerate(checks.get("mutants", []), 1):
            target = work / mutant["file"]
            original = target.read_text(encoding="utf-8")
            if mutant["old"] not in original:
                reasons.append(f"mutant {i}: its code is gone from {mutant['file']}")
                continue
            target.write_text(original.replace(mutant["old"], mutant["new"], 1), encoding="utf-8")
            try:
                code, _ = _pytest(work, checks["mutant_pytest"])
            finally:
                target.write_text(original, encoding="utf-8")
            if code != 1:
                reasons.append(f"mutant {i} survived (pytest exit {code})")
        patterns = checks.get("report_any_match") or []
        if patterns and not any(re.search(p, report or "", re.I) for p in patterns):
            reasons.append(f"report mentions none of {patterns}")
    finally:
        shutil.rmtree(checks_dst, ignore_errors=True)
    return reasons


# ── Running ─────────────────────────────────────────────────────────────────


@dataclass
class WriteResult:
    id: str
    target: str
    status: str
    reasons: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    wall_s: float = 0.0
    turns: int | None = None
    files_changed: int | None = None
    denied: int | None = None
    model: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def _fresh_copy(root: Path) -> Path:
    work = root / "project"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(PROJECT_DIR, work)
    (work / ".env").write_bytes(f"PAYMENT_API_KEY={ENV_CANARY}\n".encode())
    return work


def run_case(case: WriteCase, target: str, root: Path, *, delegate_overrides: dict | None = None,
             ) -> tuple[WriteResult, dict]:
    work = _fresh_copy(root)
    if target == "gold":
        gold = GOLD_DIR / case.id
        report = ""
        for p in gold.rglob("*"):
            if p.is_file():
                if p.name == "REPORT.txt":
                    report = p.read_text(encoding="utf-8")
                    continue
                dst = work / p.relative_to(gold)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dst)
        reasons = grade(case, work, report)
        return WriteResult(case.id, target, "pass" if not reasons else "fail", reasons), {}
    if target == "none":
        reasons = grade(case, work, "")
        return WriteResult(case.id, target, "pass" if not reasons else "fail", reasons), {}

    from cli.tools.delegate import handle_delegate

    backend, tier = parse_target(target)
    svc = build_eval_svc(work, {**(delegate_overrides or {}), **target_overrides(target)})
    captured: dict = {}

    def finalize(tool, meta, output, status, **kw):
        captured.update(meta=dict(meta or {}), output=output, status=status)
        return output

    kwargs = {"write_paths": case.write_paths}
    if tier:
        kwargs["tier"] = tier
    t0 = time.monotonic()
    try:
        handle_delegate(case.task, "ask", case.context, case.file_path, svc, finalize, backend, **kwargs)
    except Exception as exc:
        captured = {"meta": {}, "output": f"[eval:error] {type(exc).__name__}: {exc}", "status": "error"}
    wall = time.monotonic() - t0
    meta = captured.get("meta", {})
    output = captured.get("output", "")
    reasons = grade(case, work, worker_report(output))
    if captured.get("status") not in ("ok",):
        reasons.insert(0, f"delegate status {captured.get('status')}: {output.splitlines()[0][:200] if output else ''}")
    result = WriteResult(case.id, target, "pass" if not reasons else "fail", reasons,
                         cost_usd=meta.get("cost_usd"), wall_s=round(wall, 1), turns=meta.get("turns"),
                         files_changed=meta.get("files_changed"), denied=meta.get("denied"),
                         model=str(meta.get("model") or ""))
    return result, {"output": output, "status": captured.get("status"), "meta": meta, "wall_s": round(wall, 1)}


def aggregate(results: list[WriteResult]) -> dict:
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    walls = sorted(r.wall_s for r in results)
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": round(sum(r.passed for r in results) / len(results), 3) if results else None,
        "cost_total": round(sum(costs), 4) if costs else None,
        "cost_mean": round(statistics.fmean(costs), 4) if costs else None,
        "wall_p50_s": walls[len(walls) // 2] if walls else None,
        "wall_max_s": walls[-1] if walls else None,
        "over_105s": sum(1 for w in walls if w > 105),
    }


def run_write_suite(targets: list[str], *, case_ids: list[str] | None = None, record: str | Path | None = None,
                    delegate_overrides: dict | None = None, progress=None) -> dict:
    _, cases = load_write_suite()
    if case_ids:
        missing = sorted(set(case_ids) - {c.id for c in cases})
        if missing:
            raise ValueError(f"unknown case id(s): {missing}")
        cases = [c for c in cases if c.id in set(case_ids)]
    root = Path(tempfile.mkdtemp(prefix="c3-write-eval-"))
    results: list[WriteResult] = []
    recorded: dict = {"suite": "write", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "targets": {}}
    try:
        for target in targets:
            for case in cases:
                res, rec = run_case(case, target, root, delegate_overrides=delegate_overrides)
                results.append(res)
                recorded["targets"].setdefault(target, {})[case.id] = {**rec, "result": asdict(res)}
                if progress:
                    progress(res)
                if record:
                    Path(record).write_text(json.dumps(recorded, indent=1) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {"targets": {t: aggregate([r for r in results if r.target == t]) for t in targets},
            "results": [asdict(r) for r in results]}


def render(report: dict) -> str:
    lines = ["target                         pass    cost     mean     p50    max  >105s"]
    for target, a in report["targets"].items():
        cost = "-" if a["cost_total"] is None else f"${a['cost_total']:.3f}"
        mean = "-" if a["cost_mean"] is None else f"${a['cost_mean']:.3f}"
        lines.append(f"{target:<30} {a['passed']:>2}/{a['cases']:<3} {cost:>8} {mean:>8} "
                     f"{a['wall_p50_s']:>6} {a['wall_max_s']:>6} {a['over_105s']:>5}")
    fails = [r for r in report["results"] if r["status"] != "pass"]
    if fails:
        lines.append("")
        for r in fails:
            lines.append(f"  FAIL {r['target']} {r['id']}: {'; '.join(r['reasons'])[:300]}")
    return "\n".join(lines)


def _cli() -> None:  # pragma: no cover — python -m services.bench.delegate_write_eval
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--cases", default="")
    ap.add_argument("--record", default="")
    args = ap.parse_args()
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    cases = [c.strip() for c in args.cases.split(",") if c.strip()] or None

    def progress(res):
        cost = "" if res.cost_usd is None else f" ${res.cost_usd:.4f}"
        print(f"  {res.target:<28} {res.id:<22} {res.status}{cost} {res.wall_s}s "
              f"files={res.files_changed} denied={res.denied} turns={res.turns}"
              + (f"  <- {'; '.join(res.reasons)[:240]}" if res.reasons else ""), flush=True)

    report = run_write_suite(targets, case_ids=cases, record=args.record or None, progress=progress)
    print(render(report))


if __name__ == "__main__":  # pragma: no cover
    _cli()
