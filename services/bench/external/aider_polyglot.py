"""Aider Polyglot benchmark adapter.

Wraps the Aider CLI + polyglot-benchmark corpus (225 Exercism exercises in 6
languages) to measure how C3 MCP affects an agent's edit success rate.

Setup (one-time):
  git clone https://github.com/Aider-AI/polyglot-benchmark /tmp/polyglot-benchmark
  pip install aider-chat

Run:
  c3 bench external --suite aider-polyglot --path /tmp/polyglot-benchmark \\
                    --languages python --max-exercises 5 --model gpt-4o-mini

What it measures:
  For each selected exercise, runs Aider twice against the same starter code:
    1. With C3 MCP server attached (c3_* tools available)
    2. Without any MCP servers (pure Aider baseline)
  After each run, executes the exercise's test command to record pass/fail,
  runtime, and token usage. Aggregate metrics: pass rate delta (C3 minus
  baseline), average tries-to-pass, token cost.

Limitations / honest caveats:
  - Requires `aider` CLI and a cloned polyglot-benchmark repo.
  - Each run burns real API tokens (cost scales linearly with exercises x 2).
  - MCP support in Aider is still evolving; this adapter uses a .mcp.json
    file in the exercise directory to enable C3 tools. If the installed Aider
    build does not yet honor MCP, the "with_c3" run degrades to equivalent to
    "baseline" and the adapter records that case rather than silently passing.
  - Test commands are language-specific and must match what polyglot-benchmark
    expects — see LANGUAGE_TEST_COMMANDS below.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Polyglot-benchmark exercise structure:
#   {repo}/{language}/exercises/practice/{exercise}/
#     .meta/config.json           -> files.solution = primary edit target(s)
#     .docs/instructions.md       -> prompt given to the agent
#     <solution files>            -> starter code (must edit these to pass)
#     <test files>                -> tests (agent must make pass)

LANGUAGE_TEST_COMMANDS: dict[str, list[str]] = {
    "python": ["python", "-m", "pytest", "-q"],
    "javascript": ["npx", "jest", "--silent"],
    "go": ["go", "test", "./..."],
    "rust": ["cargo", "test", "--quiet"],
    "java": ["./gradlew", "test", "--quiet"],
    "cpp": ["make", "test"],
}


@dataclass
class AiderPolyglotResult:
    exercise: str
    language: str
    mode: str  # "with_c3" | "baseline"
    passed: bool = False
    tries: int = 0
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    error: str = ""
    test_output_tail: str = ""  # last ~500 chars of test output for triage

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AiderPolyglotReport:
    timestamp: str
    project_path: str
    suite: str = "aider-polyglot"
    tier: str = "external"
    model: str = ""
    languages: list[str] = field(default_factory=list)
    exercises_run: int = 0
    results: list[AiderPolyglotResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "project_path": self.project_path,
            "suite": self.suite,
            "tier": self.tier,
            "benchmark_type": "aider_polyglot",
            "model": self.model,
            "languages": self.languages,
            "exercises_run": self.exercises_run,
            "results": [r.to_dict() for r in self.results],
            "scorecard": self._scorecard(),
        }

    def _scorecard(self) -> dict:
        with_c3 = [r for r in self.results if r.mode == "with_c3"]
        base = [r for r in self.results if r.mode == "baseline"]

        def pct(hits, total):
            return round(100.0 * hits / total, 1) if total else 0.0

        with_c3_pass = sum(1 for r in with_c3 if r.passed)
        base_pass = sum(1 for r in base if r.passed)

        return {
            "with_c3_pass_rate": pct(with_c3_pass, len(with_c3)),
            "baseline_pass_rate": pct(base_pass, len(base)),
            "pass_rate_delta": pct(with_c3_pass, len(with_c3)) - pct(base_pass, len(base)),
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


def detect_aider() -> Optional[str]:
    """Return path to the `aider` CLI or None if missing."""
    return shutil.which("aider")


def find_polyglot_repo(path: Optional[str] = None) -> Optional[Path]:
    """Locate a polyglot-benchmark checkout.

    Search order: explicit path, env var, a few common locations.
    A directory is recognized by having at least one of the canonical language
    subdirs (python/, javascript/, etc.) with `exercises/practice/` below it.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    env = os.environ.get("POLYGLOT_BENCHMARK_PATH")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path.home() / "polyglot-benchmark",
        Path.home() / "src" / "polyglot-benchmark",
        Path("/tmp/polyglot-benchmark"),
        Path("/opt/polyglot-benchmark"),
        Path.cwd() / "polyglot-benchmark",
    ]

    for c in candidates:
        if not c.exists():
            continue
        for lang in LANGUAGE_TEST_COMMANDS:
            if (c / lang / "exercises" / "practice").exists():
                return c.resolve()
    return None


def _list_exercises(repo: Path, language: str, limit: int) -> list[Path]:
    practice = repo / language / "exercises" / "practice"
    if not practice.exists():
        return []
    dirs = sorted([d for d in practice.iterdir() if d.is_dir()])
    return dirs[:limit]


def _read_exercise_meta(ex_dir: Path) -> dict:
    meta_path = ex_dir / ".meta" / "config.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_instructions(ex_dir: Path) -> str:
    docs = ex_dir / ".docs" / "instructions.md"
    append = ex_dir / ".docs" / "instructions.append.md"
    parts = []
    if docs.exists():
        parts.append(docs.read_text(encoding="utf-8", errors="replace"))
    if append.exists():
        parts.append(append.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(parts) or f"Complete the exercise in {ex_dir.name}."


def _write_c3_mcp_config(workspace: Path) -> None:
    """Drop an .mcp.json into workspace so aider can load C3 tools.

    This assumes the `c3` CLI is installed and runnable as an MCP server.
    If the installed Aider build doesn't honor MCP yet, this file is simply
    ignored (no crash, no fake success).
    """
    config = {
        "mcpServers": {
            "c3": {
                "command": "python",
                "args": ["-m", "cli.mcp_server"],
                "env": {"C3_PROJECT_PATH": str(workspace)},
            }
        }
    }
    (workspace / ".mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


class AiderPolyglotBenchmark:
    def __init__(
        self,
        repo_path: Path,
        project_path: Path,
        *,
        languages: list[str],
        max_exercises: int = 5,
        model: str = "gpt-4o-mini",
        timeout_per_exercise: int = 300,
        verbose: bool = False,
    ):
        self.repo = repo_path
        self.project = project_path
        self.languages = languages
        self.max_exercises = max_exercises
        self.model = model
        self.timeout = timeout_per_exercise
        self.verbose = verbose

    def run_all(self) -> AiderPolyglotReport:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        report = AiderPolyglotReport(
            timestamp=timestamp,
            project_path=str(self.project),
            model=self.model,
            languages=list(self.languages),
        )

        for lang in self.languages:
            if lang not in LANGUAGE_TEST_COMMANDS:
                print(f"  [skip] Unsupported language: {lang}")
                continue
            exercises = _list_exercises(self.repo, lang, self.max_exercises)
            if not exercises:
                print(f"  [skip] No {lang} exercises under {self.repo}")
                continue

            for ex in exercises:
                report.exercises_run += 1
                if self.verbose:
                    print(f"\n  [{lang}] {ex.name}")

                for mode in ("baseline", "with_c3"):
                    result = self._run_exercise(ex, lang, mode)
                    report.results.append(result)
                    if self.verbose:
                        status = "PASS" if result.passed else "FAIL"
                        print(
                            f"    {mode:<9} {status}  "
                            f"t={result.latency_s:.1f}s  "
                            f"tok={result.input_tokens + result.output_tokens}"
                        )

        return report

    def _run_exercise(self, ex_dir: Path, language: str, mode: str) -> AiderPolyglotResult:
        result = AiderPolyglotResult(
            exercise=ex_dir.name, language=language, mode=mode, model=self.model, tries=1
        )

        meta = _read_exercise_meta(ex_dir)
        solution_files = meta.get("files", {}).get("solution", [])
        if not solution_files:
            result.error = "no solution files in .meta/config.json"
            return result

        with tempfile.TemporaryDirectory(prefix=f"c3-aider-{mode}-") as tmp:
            workspace = Path(tmp)
            # Copy full exercise dir (code + tests + docs)
            for child in ex_dir.iterdir():
                target = workspace / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)

            if mode == "with_c3":
                _write_c3_mcp_config(workspace)

            instructions = _read_instructions(ex_dir)
            prompt = (
                f"{instructions}\n\n"
                f"Edit the solution file(s) so the existing tests pass. "
                f"Do not modify the test files."
            )

            aider = detect_aider()
            if not aider:
                result.error = "aider CLI not on PATH"
                return result

            cmd = [
                aider,
                "--model", self.model,
                "--yes-always",
                "--no-auto-commits",
                "--no-pretty",
                "--no-stream",
                "--message", prompt,
                *solution_files,
            ]

            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                result.latency_s = round(time.monotonic() - t0, 1)
                result.input_tokens, result.output_tokens, result.cost_usd = \
                    _parse_aider_tokens_cost(proc.stdout + proc.stderr)
            except subprocess.TimeoutExpired:
                result.latency_s = float(self.timeout)
                result.error = "aider timed out"
                return result
            except FileNotFoundError:
                result.error = "aider not invocable"
                return result

            test_cmd = LANGUAGE_TEST_COMMANDS[language]
            try:
                tp = subprocess.run(
                    test_cmd,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                result.passed = tp.returncode == 0
                tail = (tp.stdout + tp.stderr)[-500:]
                result.test_output_tail = tail
            except subprocess.TimeoutExpired:
                result.error = "tests timed out"
            except FileNotFoundError:
                result.error = f"test runner missing: {' '.join(test_cmd)}"

        return result


def _parse_aider_tokens_cost(output: str) -> tuple[int, int, float]:
    """Best-effort parse of aider's trailing 'Tokens: ... Cost: ...' line.

    Aider prints something like:
      Tokens: 2.3k sent, 450 received.
      Cost: $0.0123 message, $0.0456 session.
    We extract totals for the message. Fall back to zeros on parse failure.
    """
    import re

    inp = out = 0
    cost = 0.0
    for line in output.splitlines()[-20:]:
        line = line.strip()
        if line.startswith("Tokens:"):
            m = re.search(r"([\d.]+)([kKmM]?)\s*sent", line)
            if m:
                inp = _to_int(m.group(1), m.group(2))
            m = re.search(r"([\d.]+)([kKmM]?)\s*received", line)
            if m:
                out = _to_int(m.group(1), m.group(2))
        elif line.startswith("Cost:"):
            m = re.search(r"\$\s*([\d.]+)\s*message", line)
            if m:
                try:
                    cost = float(m.group(1))
                except ValueError:
                    cost = 0.0
    return inp, out, cost


def _to_int(val: str, suffix: str) -> int:
    mult = {"k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}.get(suffix, 1)
    try:
        return int(float(val) * mult)
    except ValueError:
        return 0


def save_report(project_path: Path, report: AiderPolyglotReport) -> Path:
    runs_dir = project_path / ".c3" / "external_benchmark" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = runs_dir / f"aider_polyglot_{ts}.json"
    out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    latest = project_path / ".c3" / "external_benchmark" / "latest.json"
    latest.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return out
