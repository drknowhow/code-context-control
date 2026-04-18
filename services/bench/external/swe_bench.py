"""SWE-bench Lite external benchmark adapter.

Wraps SWE-bench (https://www.swebench.com) — 300 real GitHub issues from 12
popular Python repos in the Lite subset. For each instance, an agent reads
the issue and produces a patch; the harness then runs the repo's tests in an
isolated Docker image to decide if the issue was resolved.

Setup (one-time):
  # Dataset:
  pip install datasets
  # Evaluation (optional, requires Docker):
  pip install swebench

  # OR download the Lite dataset as a JSON file once:
  python -c "from datasets import load_dataset; \\
             ds = load_dataset('princeton-nlp/SWE-bench_Lite', split='test'); \\
             ds.to_json('swe_bench_lite.jsonl')"

Run:
  c3 bench external --suite swe-bench-lite --dataset swe_bench_lite.jsonl \\
                    --agent aider --model gpt-4o-mini --max-tasks 5

What it produces:
  1. Predictions JSONL: .c3/external_benchmark/runs/swebench_<ts>_{with_c3,baseline}.jsonl
     Each line: {"instance_id": "...", "model_patch": "diff --git ...",
                 "model_name_or_path": "c3+aider-gpt4o"}
     Directly consumable by the official SWE-bench evaluation harness.
  2. Summary JSON: .c3/external_benchmark/runs/swe_bench_lite_<ts>.json
     Aggregated resolution rate, latency, cost (populated after evaluation).
  3. Instructions to run the Docker-based evaluator if swebench is installed.

Honest caveats:
  - Patch generation is reliable without Docker. Resolution evaluation REQUIRES
    Docker (one image per instance) — absent, we record "unevaluated".
  - Some repos require specific Python versions + deps that only install
    cleanly inside their official instance image. Do not try to run tests
    outside Docker.
  - Real SWE-bench Lite runs are slow (many minutes per task). Start small
    (--max-tasks 2–5) to iterate, then scale up.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class SWEBenchTask:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    test_patch: str = ""
    patch: str = ""  # gold patch (for reference only — do NOT feed to agent)
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    version: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SWEBenchTask":
        def _parse_list(val):
            if isinstance(val, list):
                return val
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except Exception:
                    return [val] if val else []
            return []

        return cls(
            instance_id=d.get("instance_id", ""),
            repo=d.get("repo", ""),
            base_commit=d.get("base_commit", ""),
            problem_statement=d.get("problem_statement", ""),
            hints_text=d.get("hints_text", ""),
            test_patch=d.get("test_patch", ""),
            patch=d.get("patch", ""),
            fail_to_pass=_parse_list(d.get("FAIL_TO_PASS", d.get("fail_to_pass", []))),
            pass_to_pass=_parse_list(d.get("PASS_TO_PASS", d.get("pass_to_pass", []))),
            version=str(d.get("version", "")),
        )


@dataclass
class SWEBenchResult:
    instance_id: str
    repo: str
    mode: str  # "with_c3" | "baseline"
    model_patch: str = ""
    patch_empty: bool = True
    patch_lines: int = 0
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    resolved: Optional[bool] = None  # None = unevaluated (no Docker)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SWEBenchReport:
    timestamp: str
    project_path: str
    agent: str
    model: str
    dataset: str
    tasks_run: int = 0
    evaluation_method: str = "none"  # "swebench-docker" | "none"
    results: list[SWEBenchResult] = field(default_factory=list)
    predictions_with_c3: str = ""
    predictions_baseline: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "project_path": self.project_path,
            "suite": "swe-bench-lite",
            "tier": "external",
            "benchmark_type": "swe_bench_lite",
            "agent": self.agent,
            "model": self.model,
            "dataset": self.dataset,
            "tasks_run": self.tasks_run,
            "evaluation_method": self.evaluation_method,
            "results": [r.to_dict() for r in self.results],
            "predictions_with_c3": self.predictions_with_c3,
            "predictions_baseline": self.predictions_baseline,
            "scorecard": self._scorecard(),
        }

    def _scorecard(self) -> dict:
        with_c3 = [r for r in self.results if r.mode == "with_c3"]
        base = [r for r in self.results if r.mode == "baseline"]

        def pct(hits, total):
            return round(100.0 * hits / total, 1) if total else 0.0

        # Patch-generation metrics (always available)
        with_c3_patched = sum(1 for r in with_c3 if not r.patch_empty)
        base_patched = sum(1 for r in base if not r.patch_empty)

        # Resolution metrics (only if evaluated)
        with_c3_resolved = sum(1 for r in with_c3 if r.resolved is True)
        base_resolved = sum(1 for r in base if r.resolved is True)
        evaluated = any(r.resolved is not None for r in self.results)

        return {
            "evaluated": evaluated,
            "with_c3_patch_rate": pct(with_c3_patched, len(with_c3)),
            "baseline_patch_rate": pct(base_patched, len(base)),
            # Resolution delta — the headline metric (null if unevaluated)
            "with_c3_pass_rate": pct(with_c3_resolved, len(with_c3)) if evaluated else None,
            "baseline_pass_rate": pct(base_resolved, len(base)) if evaluated else None,
            "pass_rate_delta": (pct(with_c3_resolved, len(with_c3))
                                - pct(base_resolved, len(base))) if evaluated else None,
            "with_c3_avg_latency_s": round(
                sum(r.latency_s for r in with_c3) / len(with_c3), 1
            ) if with_c3 else 0,
            "baseline_avg_latency_s": round(
                sum(r.latency_s for r in base) / len(base), 1
            ) if base else 0,
            "with_c3_total_cost_usd": round(sum(r.cost_usd for r in with_c3), 4),
            "baseline_total_cost_usd": round(sum(r.cost_usd for r in base), 4),
            "with_c3_count": len(with_c3),
            "baseline_count": len(base),
        }


def load_tasks(dataset_path: str) -> list[SWEBenchTask]:
    """Load SWE-bench tasks from a JSON, JSONL, or HuggingFace dataset name.

    Accepted forms:
      - "path/to/swe_bench_lite.jsonl" — one JSON object per line
      - "path/to/tasks.json"           — a JSON array
      - "princeton-nlp/SWE-bench_Lite" — HuggingFace dataset id (lazy import)
    """
    p = Path(dataset_path)
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        # JSON array form: starts with '['
        if text.startswith("["):
            data = json.loads(text)
            if isinstance(data, list):
                return [SWEBenchTask.from_dict(r) for r in data]
            raise ValueError(f"Unrecognised dataset format: {dataset_path}")
        # Otherwise JSONL: one JSON object per line
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f"Expected object per JSONL line, got {type(obj).__name__}")
            rows.append(obj)
        if rows:
            return [SWEBenchTask.from_dict(r) for r in rows]
        raise ValueError(f"Empty dataset: {dataset_path}")

    # HuggingFace id (e.g. "princeton-nlp/SWE-bench_Lite")
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            f"Dataset file not found at {dataset_path!r}, and `datasets` package "
            "is not installed. Install with `pip install datasets` or pass a "
            "local JSONL path."
        ) from e
    ds = load_dataset(dataset_path, split="test")
    return [SWEBenchTask.from_dict(r) for r in ds]


def _clone_and_checkout(task: SWEBenchTask, workspace: Path) -> Optional[str]:
    """Shallow-clone + checkout the base commit. Returns error string or None."""
    url = f"https://github.com/{task.repo}.git"
    try:
        subprocess.run(
            ["git", "clone", "--quiet", url, str(workspace)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "fetch", "--quiet", "origin", task.base_commit],
            check=False, capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "checkout", "--quiet", task.base_commit],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        return f"git: {e.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return "git clone/checkout timed out"
    except FileNotFoundError:
        return "git not on PATH"
    return None


def _diff_workspace(workspace: Path) -> str:
    """Return the unified diff of workspace vs base commit (the patch)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "diff", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout
    except Exception:
        return ""


def _run_aider_on_task(
    task: SWEBenchTask, workspace: Path, model: str, timeout: int,
) -> tuple[float, int, int, float, str]:
    """Invoke aider against the task. Returns (latency_s, input_tok, output_tok, cost, error)."""
    from services.bench.external.aider_polyglot import (
        detect_aider, _parse_aider_tokens_cost,
    )

    aider = detect_aider()
    if not aider:
        return (0.0, 0, 0, 0.0, "aider CLI not on PATH")

    prompt = (
        f"Resolve this GitHub issue in this repository. "
        f"Make minimal focused changes — do not modify tests.\n\n"
        f"=== Issue ===\n{task.problem_statement}\n"
    )
    if task.hints_text:
        prompt += f"\n=== Hints ===\n{task.hints_text[:2000]}\n"

    cmd = [
        aider,
        "--model", model,
        "--yes-always",
        "--no-auto-commits",
        "--no-pretty",
        "--no-stream",
        "--map-tokens", "4096",
        "--message", prompt,
    ]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=workspace, capture_output=True, text=True, timeout=timeout,
        )
        latency = round(time.monotonic() - t0, 1)
        inp, out, cost = _parse_aider_tokens_cost(proc.stdout + proc.stderr)
        return (latency, inp, out, cost, "")
    except subprocess.TimeoutExpired:
        return (float(timeout), 0, 0, 0.0, "aider timed out")


def _write_c3_mcp_config(workspace: Path) -> None:
    (workspace / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "c3": {
                    "command": "python",
                    "args": ["-m", "cli.mcp_server"],
                    "env": {"C3_PROJECT_PATH": str(workspace)},
                }
            }
        }, indent=2),
        encoding="utf-8",
    )


class SWEBenchAdapter:
    def __init__(
        self,
        project_path: Path,
        tasks: list[SWEBenchTask],
        *,
        agent: str = "aider",
        model: str = "gpt-4o-mini",
        timeout_per_task: int = 600,
        verbose: bool = False,
    ):
        self.project_path = project_path
        self.tasks = tasks
        self.agent = agent
        self.model = model
        self.timeout = timeout_per_task
        self.verbose = verbose

    def run_all(self, dataset_label: str = "") -> SWEBenchReport:
        report = SWEBenchReport(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            project_path=str(self.project_path),
            agent=self.agent, model=self.model,
            dataset=dataset_label,
            tasks_run=len(self.tasks),
        )

        predictions_c3: list[dict] = []
        predictions_base: list[dict] = []

        for task in self.tasks:
            if self.verbose:
                print(f"\n  [{task.repo}] {task.instance_id}")
            for mode in ("baseline", "with_c3"):
                result = self._run_one(task, mode)
                report.results.append(result)
                pred = {
                    "instance_id": task.instance_id,
                    "model_patch": result.model_patch,
                    "model_name_or_path": f"{'c3+' if mode == 'with_c3' else ''}{self.agent}-{self.model}",
                }
                (predictions_c3 if mode == "with_c3" else predictions_base).append(pred)
                if self.verbose:
                    status = "patched" if not result.patch_empty else "EMPTY"
                    print(f"    {mode:<9} {status}  t={result.latency_s:.1f}s  "
                          f"tok={result.input_tokens + result.output_tokens}")

        # Save predictions JSONL for both modes
        runs_dir = self.project_path / ".c3" / "external_benchmark" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        p_c3 = runs_dir / f"swebench_{ts}_with_c3.jsonl"
        p_bs = runs_dir / f"swebench_{ts}_baseline.jsonl"
        p_c3.write_text("\n".join(json.dumps(p) for p in predictions_c3), encoding="utf-8")
        p_bs.write_text("\n".join(json.dumps(p) for p in predictions_base), encoding="utf-8")
        report.predictions_with_c3 = str(p_c3)
        report.predictions_baseline = str(p_bs)

        return report

    def _run_one(self, task: SWEBenchTask, mode: str) -> SWEBenchResult:
        result = SWEBenchResult(
            instance_id=task.instance_id, repo=task.repo, mode=mode,
        )
        with tempfile.TemporaryDirectory(prefix=f"c3-swe-{mode}-") as tmp:
            workspace = Path(tmp)
            err = _clone_and_checkout(task, workspace)
            if err:
                result.error = err
                return result
            if mode == "with_c3":
                _write_c3_mcp_config(workspace)

            if self.agent == "aider":
                latency, inp, out, cost, err = _run_aider_on_task(
                    task, workspace, self.model, self.timeout,
                )
                result.latency_s = latency
                result.input_tokens = inp
                result.output_tokens = out
                result.cost_usd = cost
                if err:
                    result.error = err
                    return result
            else:
                result.error = f"agent not supported yet: {self.agent}"
                return result

            patch = _diff_workspace(workspace)
            result.model_patch = patch
            result.patch_empty = not patch.strip()
            result.patch_lines = patch.count("\n") if patch else 0

        return result


def evaluate_with_docker(
    predictions_path: Path,
    dataset_path: str,
    run_id: str = "c3-bench",
    max_workers: int = 1,
    timeout: int = 1800,
) -> Optional[dict]:
    """Run the official SWE-bench evaluation harness if swebench + Docker are available.

    Returns the parsed results JSON or None if the harness isn't installed/usable.
    """
    try:
        import swebench.harness.run_evaluation  # noqa: F401
    except ImportError:
        return None
    # Docker check
    try:
        subprocess.run(
            ["docker", "version"], check=True, capture_output=True, timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None

    cmd = [
        "python", "-m", "swebench.harness.run_evaluation",
        "--predictions_path", str(predictions_path),
        "--dataset_name", dataset_path,
        "--max_workers", str(max_workers),
        "--run_id", run_id,
        "--timeout", str(timeout),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=timeout * len(list(predictions_path.read_text().splitlines())))
    except Exception as e:
        return {"error": str(e)}

    # Parse the harness-generated results report
    candidates = list(Path.cwd().glob(f"*{run_id}*results.json")) + \
                 list(Path.cwd().glob(f"results-*{run_id}.json"))
    for c in candidates:
        try:
            return json.loads(c.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def apply_resolution_results(
    report: SWEBenchReport, eval_result: dict, mode: str,
) -> None:
    """Merge resolved/unresolved sets from an evaluation into the report results."""
    resolved = set(eval_result.get("resolved_ids", []) or eval_result.get("resolved", []))
    unresolved = set(eval_result.get("unresolved_ids", []) or eval_result.get("unresolved", []))
    for r in report.results:
        if r.mode != mode:
            continue
        if r.instance_id in resolved:
            r.resolved = True
        elif r.instance_id in unresolved:
            r.resolved = False


def save_report(project_path: Path, report: SWEBenchReport) -> Path:
    runs_dir = project_path / ".c3" / "external_benchmark" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = runs_dir / f"swe_bench_lite_{ts}.json"
    out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    latest = project_path / ".c3" / "external_benchmark" / "latest.json"
    latest.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return out
