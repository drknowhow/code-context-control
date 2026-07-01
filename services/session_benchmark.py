"""Real-world session benchmark for C3.

Simulates multi-turn AI coding workflows end-to-end, comparing
"with C3" vs "without C3" paths across realistic scenarios like
bug investigation, feature exploration, code review, etc.

The "without C3" baseline models a COMPETENT agent, not a strawman:
one targeted grep (matching lines + context, not full-file dumps) and each
needed file read at most ONCE (partial when very large, mirroring native
Read's line cap). Repeated full re-reads of the same file are not modeled —
an agent keeps already-read content in context. Baseline read steps record
which files they read in StepResult.detail ("reads:a.py;b.py") so tests can
verify the at-most-once property.

Measures cumulative token usage, latency, quality, and session longevity.
Generates a visual HTML report.
"""

import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from core import count_tokens
from services.compressor import CodeCompressor
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex
from services.output_filter import OutputFilter
from services.parser import check_syntax_ast, check_syntax_native
from services.validation_cache import ValidationCache

# ─── Data Classes ──────────────────────────────────────────

@dataclass
class StepResult:
    """Result of a single step within a workflow scenario."""
    name: str
    tool: str  # C3 tool used or "native"
    tokens: int = 0
    latency_ms: float = 0.0
    quality: float = 100.0  # 0-100
    detail: str = ""


@dataclass
class ScenarioResult:
    """Result of a complete workflow scenario."""
    name: str
    description: str
    steps_c3: list = field(default_factory=list)
    steps_baseline: list = field(default_factory=list)

    @property
    def total_tokens_c3(self):
        return sum(s.tokens for s in self.steps_c3)

    @property
    def total_tokens_baseline(self):
        return sum(s.tokens for s in self.steps_baseline)

    @property
    def total_latency_c3(self):
        return sum(s.latency_ms for s in self.steps_c3)

    @property
    def total_latency_baseline(self):
        return sum(s.latency_ms for s in self.steps_baseline)

    @property
    def avg_quality_c3(self):
        if not self.steps_c3:
            return 0.0
        return sum(s.quality for s in self.steps_c3) / len(self.steps_c3)

    @property
    def avg_quality_baseline(self):
        if not self.steps_baseline:
            return 0.0
        return sum(s.quality for s in self.steps_baseline) / len(self.steps_baseline)

    @property
    def token_savings_pct(self):
        if not self.total_tokens_baseline:
            return 0.0
        return round((self.total_tokens_baseline - self.total_tokens_c3) / self.total_tokens_baseline * 100, 1)

    @property
    def budget_multiplier(self):
        if not self.total_tokens_c3:
            return 0.0
        return round(self.total_tokens_baseline / self.total_tokens_c3, 2)

    def to_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "steps_c3": [asdict(s) for s in self.steps_c3],
            "steps_baseline": [asdict(s) for s in self.steps_baseline],
            "total_tokens_c3": self.total_tokens_c3,
            "total_tokens_baseline": self.total_tokens_baseline,
            "total_latency_c3_ms": round(self.total_latency_c3, 2),
            "total_latency_baseline_ms": round(self.total_latency_baseline, 2),
            "avg_quality_c3": round(self.avg_quality_c3, 1),
            "avg_quality_baseline": round(self.avg_quality_baseline, 1),
            "token_savings_pct": self.token_savings_pct,
            "budget_multiplier": self.budget_multiplier,
        }


# ─── Performance Timing Model ─────────────────────────────────
# Estimated model input processing rates (tokens/second).
# These are conservative averages for cloud API calls including network latency.
# Real rates vary by model, provider load, and prompt complexity.
PERF_PROFILES = {
    "fast_model": {
        "label": "Fast Model (Sonnet/Haiku)",
        "input_tps": 150_000,      # tokens/sec input processing
        "output_tps": 120,          # tokens/sec output generation
        "avg_output_tokens": 500,   # typical output per turn
        "network_overhead_ms": 200, # API round-trip overhead
    },
    "capable_model": {
        "label": "Capable Model (Opus/GPT-4)",
        "input_tps": 80_000,
        "output_tps": 60,
        "avg_output_tokens": 800,
        "network_overhead_ms": 300,
    },
}


def _estimate_turn_time_ms(tokens: int, profile: dict) -> float:
    """Estimate total wall-clock time for one AI turn given input token count."""
    input_ms = (tokens / profile["input_tps"]) * 1000
    output_ms = (profile["avg_output_tokens"] / profile["output_tps"]) * 1000
    return input_ms + output_ms + profile["network_overhead_ms"]


# ─── Session Benchmark Engine ────────────────────────────────

class SessionBenchmark:
    """Runs real-world workflow scenarios comparing C3 vs native approaches."""

    def __init__(self, project_path: str, sample_size: int = 15, min_tokens: int = 200):
        self.project_path = Path(project_path).resolve()
        self.sample_size = sample_size
        self.min_tokens = min_tokens

        self.indexer = CodeIndex(str(self.project_path), str(self.project_path / ".c3" / "index"))
        self.compressor = CodeCompressor(str(self.project_path / ".c3" / "cache"), project_root=str(self.project_path))
        self.file_memory = FileMemoryStore(str(self.project_path))
        self.validation_cache = ValidationCache(str(self.project_path))
        self.output_filter = OutputFilter({"HYBRID_DISABLE_TIER1": True})

        self.files = self._collect_files()
        self.sample = self._select_sample()
        self.fixtures = self._build_fixtures()

    def _collect_files(self):
        skip_dirs = set(getattr(self.indexer, "skip_dirs", set()))
        code_exts = set(getattr(self.indexer, "code_exts", set()))
        files = []
        for fpath in self.project_path.rglob("*"):
            if not fpath.is_file():
                continue
            if fpath.suffix.lower() not in code_exts:
                continue
            if any(skip in fpath.parts for skip in skip_dirs):
                continue
            if self.compressor.is_protected_file(fpath):
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            files.append((fpath, content, count_tokens(content)))
        return files

    def _select_sample(self):
        sample = sorted(
            [f for f in self.files if f[2] >= self.min_tokens],
            key=lambda x: x[2], reverse=True
        )[:self.sample_size]
        if not sample:
            sample = sorted(self.files, key=lambda x: x[2], reverse=True)[:self.sample_size]
        return sample

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.project_path)).replace("\\", "/")

    # ─── Honest baseline modeling ─────────────────────────────
    # The "without C3" baseline models a COMPETENT agent, not a strawman:
    # one targeted grep (matching lines + context, like ripgrep output — not
    # full-file dumps), then reading each needed file at most ONCE (partial
    # when very large, mirroring native Read's line cap). No repeated full
    # re-reads: an agent keeps already-read content in context.

    # Native Read defaults to ~2000 lines per call; a competent agent reads
    # a very large file partially, not repeatedly.
    BASELINE_READ_MAX_LINES = 2000
    # Cap grep matches surfaced per file (agents skim, they don't ingest
    # unbounded match lists).
    BASELINE_GREP_MAX_MATCHES_PER_FILE = 20

    @classmethod
    def _read_once_tokens(cls, content: str) -> int:
        """Token cost of reading a file ONCE (capped like native Read)."""
        lines = content.splitlines()
        if len(lines) <= cls.BASELINE_READ_MAX_LINES:
            return count_tokens(content)
        return count_tokens("\n".join(lines[:cls.BASELINE_READ_MAX_LINES]))

    def _baseline_grep(self, terms: list, limit_files: int = 5,
                       context: int = 2, scan=None) -> tuple:
        """Model a targeted grep: matching lines with +/- context lines.

        Returns (grep_output_text, matched_rel_paths). This replaces the old
        strawman that concatenated the FULL content of every matching file.
        """
        scan = scan if scan is not None else self.files[:30]
        terms = [t.lower() for t in terms if t]
        out_lines = []
        matched = []
        for fpath, content, _ in scan:
            low = content.lower()
            if not terms or not any(t in low for t in terms):
                continue
            rel = self._rel(fpath)
            matched.append(rel)
            lines = content.splitlines()
            hits = 0
            for i, line in enumerate(lines):
                if any(t in line.lower() for t in terms):
                    lo = max(0, i - context)
                    hi = min(len(lines), i + context + 1)
                    for j in range(lo, hi):
                        out_lines.append(f"{rel}:{j + 1}:{lines[j]}")
                    hits += 1
                    if hits >= self.BASELINE_GREP_MAX_MATCHES_PER_FILE:
                        break
            if len(matched) >= limit_files:
                break
        return "\n".join(out_lines), matched

    @staticmethod
    def _reads_detail(rel_paths) -> str:
        """Machine-checkable record of which files a baseline step read."""
        return "reads:" + ";".join(rel_paths)

    def _build_fixtures(self):
        """Create log, JSONL, and terminal fixtures for log-related scenarios."""
        fixture_dir = self.project_path / ".c3" / "session_benchmark" / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)

        rel_paths = [self._rel(f[0]) for f in self.sample[:8]] or ["cli/c3.py"]

        def stamp(idx):
            return f"2026-03-10T14:{idx % 60:02d}:{(idx * 7) % 60:02d}"

        # Build a realistic application log
        log_lines = []
        for idx in range(120):
            rel = rel_paths[idx % len(rel_paths)]
            log_lines.append(f"{stamp(idx)} INFO  Processing {rel}")
            if idx % 2 == 0:
                log_lines.extend([f"{stamp(idx)} DEBUG heartbeat ok"] * 2)
            if idx % 5 == 0:
                log_lines.append(f"{stamp(idx)} WARN  Slow parse {rel} latency={30 + idx}ms")
            if idx % 8 == 0:
                log_lines.append(f"{stamp(idx)} ERROR Failed to analyze {rel}")
                log_lines.append("Traceback (most recent call last):")
                log_lines.append(f'  File "{rel}", line {10 + idx}, in process')
                log_lines.append("RuntimeError: analysis timeout exceeded")
            if idx % 15 == 0:
                log_lines.append(f"{stamp(idx)} ERROR ConnectionError: upstream service unavailable")

        log_path = fixture_dir / "session_app.log"
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        # Build JSONL event stream
        jsonl_entries = []
        for idx in range(200):
            rel = rel_paths[idx % len(rel_paths)]
            jsonl_entries.append({
                "ts": stamp(idx), "event": ["compress", "search", "read", "validate"][idx % 4],
                "file": rel, "status": "ok" if idx % 13 else "error",
                "tokens": 200 + idx * 3, "latency_ms": 2 + (idx % 20),
                "user": f"user_{idx % 3}",
            })

        jsonl_path = fixture_dir / "session_events.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(e) for e in jsonl_entries) + "\n", encoding="utf-8")

        # Build noisy terminal output
        terminal_lines = ["\x1b[36mRunning test suite...\x1b[0m", ""]
        for idx in range(150):
            rel = rel_paths[idx % len(rel_paths)]
            terminal_lines.append(f"tests/test_{idx:03d}.py::test_{rel.replace('/', '_')} PASSED")
            if idx % 5 == 0:
                terminal_lines.extend(["Installing dependencies..."] * 3)
            if idx % 7 == 0:
                terminal_lines.append("██████████████████████ 100%")
            if idx % 10 == 0:
                terminal_lines.append(f"WARN deprecation in {rel}")
            if idx % 25 == 0:
                terminal_lines.append(f"ERROR compilation failed for {rel}")
                terminal_lines.append(f"FAILED tests/test_{idx:03d}.py - AssertionError")

        terminal_path = fixture_dir / "session_terminal.txt"
        terminal_path.write_text("\n".join(terminal_lines) + "\n", encoding="utf-8")

        return {
            "log_path": str(log_path),
            "jsonl_path": str(jsonl_path),
            "terminal_path": str(terminal_path),
            "log_signals": ["ERROR", "WARN", "Traceback", "RuntimeError", "ConnectionError"],
            "jsonl_fields": list(jsonl_entries[0].keys()) if jsonl_entries else [],
            "terminal_signals": ["WARN", "ERROR", "FAILED"],
        }

    # ─── Workflow Scenarios ───────────────────────────────────

    def run_all(self) -> list:
        """Run all workflow scenarios and return results."""
        scenarios = [
            self._scenario_bug_investigation,
            self._scenario_feature_exploration,
            self._scenario_code_review,
            self._scenario_log_diagnosis,
            self._scenario_refactor_planning,
            self._scenario_onboarding,
        ]
        results = []
        for scenario_fn in scenarios:
            try:
                results.append(scenario_fn())
            except Exception as e:
                results.append(ScenarioResult(
                    name=scenario_fn.__name__.replace("_scenario_", ""),
                    description=f"Error: {e}",
                ))
        return results

    def _scenario_bug_investigation(self) -> ScenarioResult:
        """Simulate: search for error → read suspicious files → narrow to symbol → validate."""
        result = ScenarioResult(
            name="bug_investigation",
            description="Search for an error pattern, read suspicious files, narrow to the relevant symbol, validate syntax.",
        )
        if len(self.sample) < 2:
            return result

        # Pick a target file and a "bug query"
        target_path, target_content, _ = self.sample[0]
        target_rel = self._rel(target_path)
        record = self.file_memory.get(target_rel)
        if not record or self.file_memory.needs_update(target_rel):
            record = self.file_memory.update(target_rel)
        symbols = []
        if record and record.get("sections"):
            symbols = [s["name"] for s in record["sections"] if s.get("type") in ("class", "function", "method")][:3]
        # Use a query that's realistic but likely to hit — reference the filename and a symbol if available
        if symbols:
            query = f"{symbols[0]} bug in {target_path.stem}"
        else:
            query = f"{target_path.stem} {target_path.suffix.lstrip('.')} error"

        # ── WITH C3 ──

        # Step 1: c3_search to find relevant files
        t0 = time.perf_counter()
        search_results = self.indexer.search(query, top_k=5, max_tokens=2000)
        context = self.indexer.get_context(query, top_k=5, max_tokens=2000)
        lat = (time.perf_counter() - t0) * 1000
        ctx_tokens = count_tokens(context)
        hit = any(target_rel in str(r.get("file", "")).replace("\\", "/") for r in search_results)
        # Partial credit: if we got results at all, the search is still useful (found related files)
        search_quality = 100.0 if hit else (80.0 if search_results else 50.0)
        result.steps_c3.append(StepResult("search", "c3_search", ctx_tokens, lat, search_quality))

        # Step 2: c3_compress(map) to understand structure
        t0 = time.perf_counter()
        map_text = self.file_memory.get_or_build_map(target_rel)
        lat = (time.perf_counter() - t0) * 1000
        map_tokens = count_tokens(map_text)
        map_ok = "[file_map] Could not" not in map_text and "[file_map:error]" not in map_text
        result.steps_c3.append(StepResult("map_structure", "c3_compress(map)", map_tokens, lat, 100.0 if map_ok else 60.0))

        # Step 3: c3_read to extract specific symbol.
        # When symbol extraction is unavailable (e.g. unsupported file type),
        # the honest fallback is a single capped read — same model as the
        # baseline — NOT an uncapped full-file ingest.
        t0 = time.perf_counter()
        if symbols and record and record.get("sections"):
            target_sections = [s for s in record["sections"] if s["name"] == symbols[0]]
            if target_sections:
                lines = target_content.splitlines()
                s = target_sections[0]
                extracted = "\n".join(lines[s["line_start"]-1:s["line_end"]])
                read_tokens = count_tokens(extracted)
                read_quality = 100.0
            else:
                read_tokens = self._read_once_tokens(target_content)
                read_quality = 70.0
        else:
            read_tokens = self._read_once_tokens(target_content)
            read_quality = 70.0
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("read_symbol", "c3_read", read_tokens, lat, read_quality))

        # Step 4: c3_validate syntax
        t0 = time.perf_counter()
        errors = check_syntax_ast(target_content, target_path.suffix.lower())
        lat = (time.perf_counter() - t0) * 1000
        err_msg = f"Found {len(errors)} errors" if errors else "No errors"
        result.steps_c3.append(StepResult("validate", "c3_validate", count_tokens(err_msg), lat, 100.0))

        # ── WITHOUT C3 (honest baseline: competent agent) ──
        # One targeted grep (matches + context, not full-file dumps), then
        # the suspect file is read ONCE. Symbol narrowing and eyeball
        # validation happen over content already in context — no re-reads.

        # Step 1: targeted grep — matching lines with context
        t0 = time.perf_counter()
        terms = [t for t in re.findall(r"[A-Za-z_]+", query.lower()) if len(t) > 2]
        grep_text, grep_hits = self._baseline_grep(terms, limit_files=5, scan=self.files[:20])
        lat = (time.perf_counter() - t0) * 1000
        base_tokens = count_tokens(grep_text) if grep_text else 50  # empty-result output still costs a little
        base_hit = target_rel in grep_hits
        base_search_quality = 100.0 if base_hit else (75.0 if grep_hits else 50.0)
        result.steps_baseline.append(StepResult("grep_search", "native", base_tokens, lat,
                                                base_search_quality, detail="grep matches + context"))

        # Step 2: read the suspect file ONCE (partial when very large)
        t0 = time.perf_counter()
        full_content = target_path.read_text(encoding="utf-8", errors="replace")
        read_tokens_once = self._read_once_tokens(full_content)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_target_once", "native", read_tokens_once, lat,
                                                100.0, detail=self._reads_detail([target_rel])))

        # Step 3: narrow to the symbol using content already in context
        # (no extra input tokens)
        result.steps_baseline.append(StepResult("narrow_in_context", "native", 0, 0.1, 100.0,
                                                detail="symbol located in already-read content"))

        # Step 4: eyeball validation from context — free, but no machine
        # syntax check (quality penalty)
        result.steps_baseline.append(StepResult("visual_validation", "native", 0, 0.1, 80.0,
                                                detail="no syntax checker; visual only"))

        return result

    def _scenario_feature_exploration(self) -> ScenarioResult:
        """Simulate: discover related files → map structure → read key symbols → understand deps."""
        result = ScenarioResult(
            name="feature_exploration",
            description="Discover files related to a feature, map their structure, read key symbols, understand dependencies.",
        )
        explore_count = min(5, len(self.sample))
        if explore_count < 2:
            return result

        explore_files = self.sample[:explore_count]
        query = f"how does {explore_files[0][0].stem} work"

        # ── WITH C3 ──

        # Step 1: c3_search to discover related files
        t0 = time.perf_counter()
        context = self.indexer.get_context(query, top_k=5, max_tokens=2000)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("discover_files", "c3_search", count_tokens(context), lat, 100.0))

        # Step 2: c3_compress each file
        total_compressed = 0
        total_lat = 0
        successes = 0
        for fpath, content, _ in explore_files:
            t0 = time.perf_counter()
            comp = self.compressor.compress_file(str(fpath), "smart")
            total_lat += (time.perf_counter() - t0) * 1000
            comp_tokens = int(comp.get("compressed_tokens", count_tokens(content)))
            total_compressed += comp_tokens
            if "error" not in comp:
                successes += 1
        quality = round(successes / explore_count * 100, 1)
        result.steps_c3.append(StepResult("compress_files", "c3_compress", total_compressed, total_lat, quality))

        # Step 3: surgical read of key symbols from top 2 files
        surgical_tokens = 0
        surgical_lat = 0
        for fpath, content, _ in explore_files[:2]:
            rel = self._rel(fpath)
            t0 = time.perf_counter()
            rec = self.file_memory.get(rel)
            if not rec or self.file_memory.needs_update(rel):
                rec = self.file_memory.update(rel)
            if rec and rec.get("sections"):
                target = [s for s in rec["sections"] if s.get("type") in ("class", "function")][:2]
                lines = content.splitlines()
                for s in target:
                    extracted = "\n".join(lines[s["line_start"]-1:s["line_end"]])
                    surgical_tokens += count_tokens(extracted)
            else:
                # Honest fallback: single capped read, same as baseline model
                surgical_tokens += self._read_once_tokens(content)
            surgical_lat += (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("read_symbols", "c3_read", surgical_tokens, surgical_lat, 100.0))

        # ── WITHOUT C3 (honest baseline: competent agent) ──

        # Step 1: targeted grep for the feature name (matches + context)
        t0 = time.perf_counter()
        grep_text, _ = self._baseline_grep([explore_files[0][0].stem.lower()], limit_files=5)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("grep_discover", "native",
                                                count_tokens(grep_text) if grep_text else 50,
                                                lat, 100.0, detail="grep matches + context"))

        # Step 2: read each related file ONCE (partial when very large)
        t0 = time.perf_counter()
        full_tokens = 0
        read_rels = []
        for fpath, content, _ in explore_files:
            fpath.read_text(encoding="utf-8", errors="replace")
            full_tokens += self._read_once_tokens(content)
            read_rels.append(self._rel(fpath))
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_all_files_once", "native", full_tokens, lat,
                                                100.0, detail=self._reads_detail(read_rels)))

        # Step 3: symbol understanding from content already in context —
        # no re-read, no extra input tokens
        result.steps_baseline.append(StepResult("symbols_in_context", "native", 0, 0.1, 100.0,
                                                detail="symbols understood from already-read content"))

        return result

    def _scenario_code_review(self) -> ScenarioResult:
        """Simulate: list changed files → compress each → read flagged sections → validate."""
        result = ScenarioResult(
            name="code_review",
            description="Review changed files by compressing them, reading flagged sections, and validating syntax.",
        )
        review_count = min(6, len(self.sample))
        if review_count < 2:
            return result

        review_files = self.sample[:review_count]

        # ── WITH C3 ──

        # Step 1: compress all files under review
        total_compressed = 0
        total_lat = 0
        for fpath, content, _ in review_files:
            t0 = time.perf_counter()
            comp = self.compressor.compress_file(str(fpath), "smart")
            total_lat += (time.perf_counter() - t0) * 1000
            total_compressed += int(comp.get("compressed_tokens", count_tokens(content)))
        result.steps_c3.append(StepResult("compress_review", "c3_compress", total_compressed, total_lat, 100.0))

        # Step 2: surgical read of flagged sections (top 3 files, 1 symbol each)
        read_tokens = 0
        read_lat = 0
        for fpath, content, _ in review_files[:3]:
            rel = self._rel(fpath)
            t0 = time.perf_counter()
            rec = self.file_memory.get(rel)
            if not rec or self.file_memory.needs_update(rel):
                rec = self.file_memory.update(rel)
            if rec and rec.get("sections"):
                target = [s for s in rec["sections"] if s.get("type") in ("class", "function", "method")][:1]
                if target:
                    lines = content.splitlines()
                    s = target[0]
                    extracted = "\n".join(lines[s["line_start"]-1:s["line_end"]])
                    read_tokens += count_tokens(extracted)
                else:
                    # Honest fallback: single capped read, same as baseline
                    read_tokens += self._read_once_tokens(content)
            else:
                read_tokens += self._read_once_tokens(content)
            read_lat += (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("read_flagged", "c3_read", read_tokens, read_lat, 100.0))

        # Step 3: validate all files (cold — native parser, populates cache)
        val_tokens = 0
        val_lat = 0
        val_ok = 0
        for fpath, content, _ in review_files:
            ext = fpath.suffix.lower()
            t0 = time.perf_counter()
            native_result = check_syntax_native(content, ext)
            val_lat += (time.perf_counter() - t0) * 1000
            status = native_result.get("status", "")
            msg = "ok" if status == "clean" else f"{status}: {native_result.get('detail', '')}"[:60]
            val_tokens += count_tokens(msg)
            val_ok += 1
            # Populate cache for the next step
            try:
                import os
                rel = self._rel(fpath)
                st = os.stat(str(fpath))
                self.validation_cache.put(rel, native_result, st.st_mtime, st.st_size)
            except Exception:
                pass
        result.steps_c3.append(StepResult("validate_cold", "c3_validate", val_tokens, val_lat, round(val_ok / review_count * 100, 1)))

        # Step 4: re-validate same files (warm — cache hit, near-zero latency)
        cache_tokens = 0
        cache_lat = 0
        cache_hits = 0
        for fpath, content, _ in review_files:
            rel = self._rel(fpath)
            t0 = time.perf_counter()
            cached = self.validation_cache.get(rel)
            cache_lat += (time.perf_counter() - t0) * 1000
            if cached is not None:
                status = cached.get("status", "")
                msg = "ok" if status == "clean" else f"{status}"[:30]
                cache_tokens += count_tokens(msg)
                cache_hits += 1
            else:
                cache_tokens += count_tokens("miss")
        quality = round(cache_hits / review_count * 100, 1) if review_count else 0
        result.steps_c3.append(StepResult("validate_cached", "c3_validate", cache_tokens, cache_lat, quality))

        # ── WITHOUT C3 (honest baseline: competent agent) ──

        # Step 1: read each file under review ONCE (partial when very large)
        t0 = time.perf_counter()
        full_tokens = sum(self._read_once_tokens(c) for _, c, _ in review_files)
        read_rels = [self._rel(f) for f, _, _ in review_files]
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_all_review_once", "native", full_tokens, lat,
                                                100.0, detail=self._reads_detail(read_rels)))

        # Step 2: review flagged sections from content already in context
        result.steps_baseline.append(StepResult("review_in_context", "native", 0, 0.1, 100.0,
                                                detail="flagged sections already in context"))

        # Step 3: visual validation from context — free, but no machine
        # syntax check (quality penalty)
        result.steps_baseline.append(StepResult("visual_validate", "native", 0, 0.1, 80.0,
                                                detail="no syntax checker; visual only"))

        # Step 4: second validation pass — still visual, still no checker
        result.steps_baseline.append(StepResult("visual_revalidate", "native", 0, 0.1, 80.0,
                                                detail="no syntax checker; visual only"))

        return result

    def _scenario_log_diagnosis(self) -> ScenarioResult:
        """Simulate: read log → filter noise → extract errors → search for related code."""
        result = ScenarioResult(
            name="log_diagnosis",
            description="Diagnose errors from a log file by filtering noise, extracting key signals, and searching for related code.",
        )

        log_path = Path(self.fixtures["log_path"])
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        log_tokens = count_tokens(log_text)

        terminal_path = Path(self.fixtures["terminal_path"])
        terminal_text = terminal_path.read_text(encoding="utf-8", errors="replace")

        # ── WITH C3 ──

        # Step 1: c3_filter the log file
        t0 = time.perf_counter()
        from cli.c3 import _benchmark_extract_preview
        log_extract = _benchmark_extract_preview(log_path, self.compressor)
        lat = (time.perf_counter() - t0) * 1000
        extract_tokens = count_tokens(log_extract)
        signal_hits = sum(1 for sig in self.fixtures["log_signals"] if sig in log_extract)
        signal_quality = round(signal_hits / len(self.fixtures["log_signals"]) * 100, 1) if self.fixtures["log_signals"] else 100.0
        result.steps_c3.append(StepResult("filter_log", "c3_filter", extract_tokens, lat, signal_quality))

        # Step 2: c3_filter terminal output
        t0 = time.perf_counter()
        filter_result = self.output_filter.filter(terminal_text, use_llm=False)
        lat = (time.perf_counter() - t0) * 1000
        filtered_tokens = filter_result.get("filtered_tokens", count_tokens(terminal_text))
        term_signals = sum(1 for sig in self.fixtures["terminal_signals"] if sig in filter_result.get("filtered", ""))
        term_quality = round(term_signals / len(self.fixtures["terminal_signals"]) * 100, 1) if self.fixtures["terminal_signals"] else 100.0
        result.steps_c3.append(StepResult("filter_terminal", "c3_filter", filtered_tokens, lat, term_quality))

        # Step 3: c3_search for error-related code
        t0 = time.perf_counter()
        context = self.indexer.get_context("error handling RuntimeError", top_k=3, max_tokens=1500)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("search_error_code", "c3_search", count_tokens(context), lat, 100.0))

        # ── WITHOUT C3 (honest baseline: competent agent) ──

        # Step 1: read the log ONCE (an agent without a filter must ingest it)
        t0 = time.perf_counter()
        log_path.read_text(encoding="utf-8", errors="replace")
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_full_log", "native", log_tokens, lat, 100.0,
                                                detail=self._reads_detail([self._rel(log_path)])))

        # Step 2: read the terminal output ONCE
        t0 = time.perf_counter()
        terminal_path.read_text(encoding="utf-8", errors="replace")
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_full_terminal", "native", count_tokens(terminal_text),
                                                lat, 100.0,
                                                detail=self._reads_detail([self._rel(terminal_path)])))

        # Step 3: targeted grep for error-related code (matches + context,
        # not full-file dumps)
        t0 = time.perf_counter()
        grep_text, _ = self._baseline_grep(["error", "exception"], limit_files=3, scan=self.files[:20])
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("grep_error_code", "native",
                                                count_tokens(grep_text) if grep_text else 50,
                                                lat, 100.0, detail="grep matches + context"))

        return result

    def _scenario_refactor_planning(self) -> ScenarioResult:
        """Simulate: search for usage → compress callers → read implementations → map impact."""
        result = ScenarioResult(
            name="refactor_planning",
            description="Plan a refactor by searching for usage patterns, compressing callers, reading implementations, mapping impact.",
        )
        if len(self.sample) < 3:
            return result

        target_path, target_content, _ = self.sample[0]
        target_rel = self._rel(target_path)
        query = f"functions that call {target_path.stem}"
        impact_files = self.sample[1:4]

        # ── WITH C3 ──

        # Step 1: search for callers
        t0 = time.perf_counter()
        context = self.indexer.get_context(query, top_k=5, max_tokens=2000)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("search_callers", "c3_search", count_tokens(context), lat, 100.0))

        # Step 2: compress caller files
        comp_tokens = 0
        comp_lat = 0
        for fpath, content, _ in impact_files:
            t0 = time.perf_counter()
            comp = self.compressor.compress_file(str(fpath), "smart")
            comp_lat += (time.perf_counter() - t0) * 1000
            comp_tokens += int(comp.get("compressed_tokens", count_tokens(content)))
        result.steps_c3.append(StepResult("compress_callers", "c3_compress", comp_tokens, comp_lat, 100.0))

        # Step 3: surgical read of target implementation
        t0 = time.perf_counter()
        rec = self.file_memory.get(target_rel)
        if not rec or self.file_memory.needs_update(target_rel):
            rec = self.file_memory.update(target_rel)
        impl_tokens = 0
        if rec and rec.get("sections"):
            target_sections = [s for s in rec["sections"] if s.get("type") in ("class", "function")][:3]
            lines = target_content.splitlines()
            for s in target_sections:
                extracted = "\n".join(lines[s["line_start"]-1:s["line_end"]])
                impl_tokens += count_tokens(extracted)
        if not impl_tokens:
            # Honest fallback: single capped read, same as baseline model
            impl_tokens = self._read_once_tokens(target_content)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("read_implementation", "c3_read", impl_tokens, lat, 100.0))

        # Step 4: compress target for impact map
        t0 = time.perf_counter()
        map_text = self.file_memory.get_or_build_map(target_rel)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("map_impact", "c3_compress(map)", count_tokens(map_text), lat, 100.0))

        # ── WITHOUT C3 (honest baseline: competent agent) ──

        # Step 1: targeted grep for callers (matches + context)
        t0 = time.perf_counter()
        grep_text, _ = self._baseline_grep([target_path.stem.lower()], limit_files=5)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("grep_callers", "native",
                                                count_tokens(grep_text) if grep_text else 50,
                                                lat, 100.0, detail="grep matches + context"))

        # Step 2: read each caller file ONCE (partial when very large)
        caller_tokens = sum(self._read_once_tokens(c) for _, c, _ in impact_files)
        caller_rels = [self._rel(f) for f, _, _ in impact_files]
        result.steps_baseline.append(StepResult("read_callers_once", "native", caller_tokens, 0.1,
                                                100.0, detail=self._reads_detail(caller_rels)))

        # Step 3: read the target ONCE
        result.steps_baseline.append(StepResult("read_target_once", "native",
                                                self._read_once_tokens(target_content), 0.1, 100.0,
                                                detail=self._reads_detail([target_rel])))

        # Step 4: impact assessment from content already in context —
        # no re-read
        result.steps_baseline.append(StepResult("impact_in_context", "native", 0, 0.1, 100.0,
                                                detail="impact assessed from already-read content"))

        return result

    def _scenario_onboarding(self) -> ScenarioResult:
        """Simulate: explore project → compress key files → read entry points → search patterns."""
        result = ScenarioResult(
            name="onboarding",
            description="New contributor explores project structure, compresses key files, reads entry points, searches for patterns.",
        )
        explore_count = min(8, len(self.sample))
        if explore_count < 2:
            return result

        explore_files = self.sample[:explore_count]

        # ── WITH C3 ──

        # Step 1: c3_search for project structure
        t0 = time.perf_counter()
        context = self.indexer.get_context("main entry point project structure", top_k=5, max_tokens=2000)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("search_structure", "c3_search", count_tokens(context), lat, 100.0))

        # Step 2: compress all key files
        total_compressed = 0
        total_lat = 0
        for fpath, content, _ in explore_files:
            t0 = time.perf_counter()
            comp = self.compressor.compress_file(str(fpath), "smart")
            total_lat += (time.perf_counter() - t0) * 1000
            total_compressed += int(comp.get("compressed_tokens", count_tokens(content)))
        result.steps_c3.append(StepResult("compress_key_files", "c3_compress", total_compressed, total_lat, 100.0))

        # Step 3: surgical read of entry points (top 2 files)
        entry_tokens = 0
        entry_lat = 0
        for fpath, content, _ in explore_files[:2]:
            rel = self._rel(fpath)
            t0 = time.perf_counter()
            rec = self.file_memory.get(rel)
            if not rec or self.file_memory.needs_update(rel):
                rec = self.file_memory.update(rel)
            if rec and rec.get("sections"):
                target = [s for s in rec["sections"] if s.get("type") in ("class", "function")][:3]
                lines = content.splitlines()
                for s in target:
                    entry_tokens += count_tokens("\n".join(lines[s["line_start"]-1:s["line_end"]]))
            else:
                # Honest fallback: single capped read, same as baseline model
                entry_tokens += self._read_once_tokens(content)
            entry_lat += (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("read_entry_points", "c3_read", entry_tokens, entry_lat, 100.0))

        # Step 4: search for common patterns
        t0 = time.perf_counter()
        ctx2 = self.indexer.get_context("configuration and settings", top_k=3, max_tokens=1500)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_c3.append(StepResult("search_patterns", "c3_search", count_tokens(ctx2), lat, 100.0))

        # ── WITHOUT C3 (honest baseline: competent agent) ──

        # Step 1: read project root listing + README ONCE (simulate)
        t0 = time.perf_counter()
        readme_path = self.project_path / "README.md"
        readme_tokens = 0
        readme_rels = []
        if readme_path.exists():
            readme_tokens = self._read_once_tokens(
                readme_path.read_text(encoding="utf-8", errors="replace"))
            readme_rels = [self._rel(readme_path)]
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_readme", "native", max(readme_tokens, 200), lat,
                                                100.0, detail=self._reads_detail(readme_rels)))

        # Step 2: read each key file ONCE (partial when very large)
        t0 = time.perf_counter()
        full_tokens = 0
        key_rels = []
        for fpath, content, _ in explore_files:
            fpath.read_text(encoding="utf-8", errors="replace")
            full_tokens += self._read_once_tokens(content)
            key_rels.append(self._rel(fpath))
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("read_key_files_once", "native", full_tokens, lat,
                                                100.0, detail=self._reads_detail(key_rels)))

        # Step 3: entry points understood from content already in context
        result.steps_baseline.append(StepResult("entries_in_context", "native", 0, 0.1, 100.0,
                                                detail="entry files already in context"))

        # Step 4: targeted grep for config patterns (matches + context)
        t0 = time.perf_counter()
        grep_text, _ = self._baseline_grep(["config", "setting"], limit_files=3)
        lat = (time.perf_counter() - t0) * 1000
        result.steps_baseline.append(StepResult("grep_patterns", "native",
                                                count_tokens(grep_text) if grep_text else 50,
                                                lat, 100.0, detail="grep matches + context"))

        return result


# ─── Report Generation ────────────────────────────────────────

def generate_report(project_path: str, scenarios: list, sample_size: int,
                    file_count: int, sampled_files: Optional[list] = None) -> dict:
    """Generate the full JSON report from scenario results."""
    total_c3 = sum(s.total_tokens_c3 for s in scenarios)
    total_base = sum(s.total_tokens_baseline for s in scenarios)
    total_lat_c3 = sum(s.total_latency_c3 for s in scenarios)
    total_lat_base = sum(s.total_latency_baseline for s in scenarios)

    savings_pct = round((total_base - total_c3) / total_base * 100, 1) if total_base else 0.0
    budget_mult = round(total_base / total_c3, 2) if total_c3 else 0.0

    avg_quality_c3 = round(sum(s.avg_quality_c3 for s in scenarios) / len(scenarios), 1) if scenarios else 0.0
    avg_quality_base = round(sum(s.avg_quality_baseline for s in scenarios) / len(scenarios), 1) if scenarios else 0.0

    # Session longevity projection
    context_limit = 200_000
    avg_tokens_per_turn_c3 = total_c3 / len(scenarios) if scenarios else 1
    avg_tokens_per_turn_base = total_base / len(scenarios) if scenarios else 1
    turns_c3 = round(context_limit / avg_tokens_per_turn_c3, 1) if avg_tokens_per_turn_c3 else 0
    turns_base = round(context_limit / avg_tokens_per_turn_base, 1) if avg_tokens_per_turn_base else 0

    # Cumulative token timeline — always at least 30 points, extend to show C3 range
    max_turn = max(30, int(turns_c3) + 5, int(turns_base) + 5)
    timeline = []
    for turn in range(1, max_turn + 1):
        timeline.append({
            "turn": turn,
            "cumulative_c3": round(avg_tokens_per_turn_c3 * turn),
            "cumulative_baseline": round(min(avg_tokens_per_turn_base * turn, context_limit * 1.5)),
        })

    # Tool contribution heatmap — per-scenario breakdown
    tool_contributions = {}
    tool_scenario_matrix = {}  # tool -> {scenario_name: tokens_saved}
    for s in scenarios:
        for step in s.steps_c3:
            tool = step.tool
            if tool not in tool_contributions:
                tool_contributions[tool] = {"total_tokens_saved": 0, "scenarios": []}
                tool_scenario_matrix[tool] = {}
            idx = s.steps_c3.index(step)
            if idx < len(s.steps_baseline):
                saved = max(0, s.steps_baseline[idx].tokens - step.tokens)
                tool_contributions[tool]["total_tokens_saved"] += saved
                tool_scenario_matrix[tool][s.name] = tool_scenario_matrix[tool].get(s.name, 0) + saved
            if s.name not in tool_contributions[tool]["scenarios"]:
                tool_contributions[tool]["scenarios"].append(s.name)

    # Cost estimation (configurable pricing)
    cost_profiles = {
        "sonnet_4": {"label": "Claude Sonnet 4", "input_per_mtok": 3.0, "output_per_mtok": 15.0},
        "opus_4": {"label": "Claude Opus 4", "input_per_mtok": 15.0, "output_per_mtok": 75.0},
        "gpt4o": {"label": "GPT-4o", "input_per_mtok": 2.5, "output_per_mtok": 10.0},
    }
    cost_estimates = {}
    tokens_saved = total_base - total_c3
    for key, profile in cost_profiles.items():
        saved_cost_per_session = (tokens_saved / 1_000_000) * profile["input_per_mtok"]
        cost_estimates[key] = {
            "label": profile["label"],
            "saved_per_session": round(saved_cost_per_session, 4),
            "saved_per_day_5_sessions": round(saved_cost_per_session * 5, 3),
            "saved_per_month": round(saved_cost_per_session * 5 * 22, 2),
        }

    # Sampled files info
    files_info = []
    if sampled_files:
        pp = Path(project_path).resolve()
        for fpath, _content, tok_count in sampled_files:
            try:
                rel = str(Path(fpath).relative_to(pp)).replace("\\", "/")
            except ValueError:
                rel = str(fpath)
            files_info.append({
                "path": rel,
                "tokens": tok_count,
                "extension": Path(fpath).suffix.lower(),
            })

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_path": str(project_path),
        "files_considered": file_count,
        "sample_size": sample_size,
        "scorecard": {
            "total_tokens_c3": total_c3,
            "total_tokens_baseline": total_base,
            "token_savings_pct": savings_pct,
            "budget_multiplier": budget_mult,
            "total_latency_c3_ms": round(total_lat_c3, 2),
            "total_latency_baseline_ms": round(total_lat_base, 2),
            "avg_quality_c3": avg_quality_c3,
            "avg_quality_baseline": avg_quality_base,
            "quality_delta": round(avg_quality_c3 - avg_quality_base, 1),
        },
        "session_longevity": {
            "context_limit": context_limit,
            "avg_tokens_per_turn_c3": round(avg_tokens_per_turn_c3),
            "avg_tokens_per_turn_baseline": round(avg_tokens_per_turn_base),
            "estimated_turns_c3": turns_c3,
            "estimated_turns_baseline": turns_base,
            "turn_multiplier": round(turns_c3 / turns_base, 2) if turns_base else 0.0,
        },
        "timeline": timeline,
        "scenarios": [s.to_dict() for s in scenarios],
        "tool_contributions": tool_contributions,
        "tool_scenario_matrix": tool_scenario_matrix,
        "cost_estimates": cost_estimates,
        "sampled_files": files_info,
        "performance_timing": _build_performance_timing(scenarios, total_lat_c3, total_lat_base),
    }


def _build_performance_timing(scenarios: list, total_local_c3_ms: float, total_local_base_ms: float) -> dict:
    """Estimate end-to-end AI turn time: local processing + model inference + network."""
    profiles = {}
    for key, profile in PERF_PROFILES.items():
        per_scenario = []
        total_e2e_c3 = 0
        total_e2e_base = 0
        for s in scenarios:
            c3_inference_ms = _estimate_turn_time_ms(s.total_tokens_c3, profile)
            base_inference_ms = _estimate_turn_time_ms(s.total_tokens_baseline, profile)
            # End-to-end = local C3 overhead + model inference
            e2e_c3 = s.total_latency_c3 + c3_inference_ms
            e2e_base = s.total_latency_baseline + base_inference_ms
            total_e2e_c3 += e2e_c3
            total_e2e_base += e2e_base
            time_saved_pct = round((e2e_base - e2e_c3) / e2e_base * 100, 1) if e2e_base else 0
            per_scenario.append({
                "name": s.name,
                "e2e_c3_ms": round(e2e_c3, 1),
                "e2e_baseline_ms": round(e2e_base, 1),
                "inference_c3_ms": round(c3_inference_ms, 1),
                "inference_baseline_ms": round(base_inference_ms, 1),
                "time_saved_pct": time_saved_pct,
            })

        time_saved_total_pct = round((total_e2e_base - total_e2e_c3) / total_e2e_base * 100, 1) if total_e2e_base else 0
        speedup = round(total_e2e_base / total_e2e_c3, 2) if total_e2e_c3 else 0

        profiles[key] = {
            "label": profile["label"],
            "total_e2e_c3_ms": round(total_e2e_c3, 1),
            "total_e2e_baseline_ms": round(total_e2e_base, 1),
            "time_saved_pct": time_saved_total_pct,
            "speedup": speedup,
            "local_overhead_c3_ms": round(total_local_c3_ms, 1),
            "local_overhead_baseline_ms": round(total_local_base_ms, 1),
            "per_scenario": per_scenario,
        }

    return {"profiles": profiles, "note": "End-to-end = local processing + model inference + network overhead. Model inference time scales with input token count."}


def _humanize(name: str) -> str:
    """Convert snake_case to Title Case."""
    return name.replace("_", " ").title()


# ─── Benchmark History ─────────────────────────────────────────────


def load_session_benchmark_history(project_path: str) -> list:
    """Load all saved session benchmark runs, sorted by timestamp ascending."""
    runs_dir = Path(project_path).resolve() / ".c3" / "session_benchmark" / "runs"
    if not runs_dir.exists():
        return []
    reports = []
    for f in runs_dir.glob("session_*.json"):
        try:
            reports.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    reports.sort(key=lambda r: r.get("timestamp", ""))
    return reports


def _build_history_data(history: list) -> dict:
    """Extract trend data from a list of benchmark reports."""
    if not history:
        return {}
    labels = []
    savings = []
    multipliers = []
    turns_c3 = []
    turns_base = []
    quality_c3 = []
    latency_c3 = []
    # Per-scenario savings over time
    scenario_trends: dict = {}  # scenario_name -> list of savings%

    for r in history:
        ts = r.get("timestamp", "")
        # Short label: "Mar 11 14:30" format
        try:
            dt = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            label = time.strftime("%b %d %H:%M", dt)
        except Exception:
            label = ts[:16]
        labels.append(label)

        sc = r.get("scorecard", {})
        savings.append(sc.get("token_savings_pct", 0))
        multipliers.append(sc.get("budget_multiplier", 0))
        quality_c3.append(sc.get("avg_quality_c3", 0))
        latency_c3.append(sc.get("total_latency_c3_ms", 0))

        lon = r.get("session_longevity", {})
        turns_c3.append(lon.get("estimated_turns_c3", 0))
        turns_base.append(lon.get("estimated_turns_baseline", 0))

        for s in r.get("scenarios", []):
            name = s.get("name", "")
            if name not in scenario_trends:
                scenario_trends[name] = []
            scenario_trends[name].append(s.get("token_savings_pct", 0))

    return {
        "labels": labels,
        "savings": savings,
        "multipliers": multipliers,
        "turns_c3": turns_c3,
        "turns_base": turns_base,
        "quality_c3": quality_c3,
        "latency_c3": latency_c3,
        "scenario_trends": scenario_trends,
        "run_count": len(history),
    }


def _render_history_section(hist: dict) -> str:
    """Render the HTML section for benchmark history trends."""
    if not hist or hist.get("run_count", 0) < 2:
        return '<div class="info-section"><p style="color:var(--text-dim)">Run the benchmark at least twice to see trend data here. Previous runs are saved automatically.</p></div>'

    n = hist["run_count"]
    latest_savings = hist["savings"][-1] if hist["savings"] else 0
    first_savings = hist["savings"][0] if hist["savings"] else 0
    delta = round(latest_savings - first_savings, 1)
    delta_sign = "+" if delta >= 0 else ""
    latest_mult = hist["multipliers"][-1] if hist["multipliers"] else 0

    # History summary table
    rows = ""
    for i in range(n):
        label = hist["labels"][i]
        sav = hist["savings"][i]
        mult = hist["multipliers"][i]
        qual = hist["quality_c3"][i]
        turns = hist["turns_c3"][i]
        lat = hist["latency_c3"][i]
        rows += f'<tr><td>{html.escape(label)}</td><td style="text-align:right">{sav:.1f}%</td><td style="text-align:right">{mult:.2f}x</td><td style="text-align:right">{qual:.1f}%</td><td style="text-align:right">{turns:.1f}</td><td style="text-align:right">{lat:.0f}ms</td></tr>'

    return f"""
<div class="info-section">
    <div class="stat-grid" style="margin-bottom:1.5rem">
        <div class="stat-card"><div class="stat-label">Total Runs</div><div class="stat-value">{n}</div></div>
        <div class="stat-card"><div class="stat-label">Latest Savings</div><div class="stat-value">{latest_savings:.1f}%</div></div>
        <div class="stat-card"><div class="stat-label">Trend (vs First)</div><div class="stat-value" style="color:{'var(--ok)' if delta >= 0 else 'var(--warn)'}">{delta_sign}{delta}%</div></div>
        <div class="stat-card"><div class="stat-label">Latest Multiplier</div><div class="stat-value">{latest_mult:.2f}x</div></div>
    </div>
    <div class="chart-row"><div class="chart-box"><h3>Token Savings Over Time</h3><canvas id="historySavingsChart"></canvas></div><div class="chart-box"><h3>Budget Multiplier Over Time</h3><canvas id="historyMultChart"></canvas></div></div>
    <div class="chart-row"><div class="chart-box"><h3>Session Turns Over Time</h3><canvas id="historyTurnsChart"></canvas></div><div class="chart-box"><h3>Per-Scenario Savings Trend</h3><canvas id="historyScenarioChart"></canvas></div></div>
    <h3 class="collapsible-toggle" onclick="this.classList.toggle('open'); this.nextElementSibling.classList.toggle('open')">Run History Table ({n} runs)</h3>
    <div class="collapsible-content">
        <table class="files-table"><thead><tr><th>Run</th><th style="text-align:right">Savings</th><th style="text-align:right">Multiplier</th><th style="text-align:right">Quality</th><th style="text-align:right">Turns (C3)</th><th style="text-align:right">Latency</th></tr></thead><tbody>{rows}</tbody></table>
    </div>
</div>"""


def _render_history_charts_js(hist: dict) -> str:
    """Render Chart.js code for benchmark history trends."""
    if not hist or hist.get("run_count", 0) < 2:
        return ""

    labels = json.dumps(hist["labels"])
    savings = json.dumps(hist["savings"])
    multipliers = json.dumps(hist["multipliers"])
    turns_c3 = json.dumps(hist["turns_c3"])
    turns_base = json.dumps(hist["turns_base"])

    # Per-scenario trend datasets
    colors = ['#818cf8', '#34d399', '#fbbf24', '#f87171', '#a78bfa', '#38bdf8', '#fb923c', '#e879f9']
    scenario_datasets = []
    for i, (name, values) in enumerate(hist.get("scenario_trends", {}).items()):
        color = colors[i % len(colors)]
        scenario_datasets.append({
            "label": _humanize(name),
            "data": values,
            "borderColor": color,
            "backgroundColor": color + "33",
            "tension": 0.3,
            "fill": False,
        })
    scenario_datasets_json = json.dumps(scenario_datasets)

    return f"""
// ── History Charts ──
new Chart(document.getElementById('historySavingsChart'), {{
    type: 'line',
    data: {{
        labels: {labels},
        datasets: [{{
            label: 'Token Savings %',
            data: {savings},
            borderColor: '#818cf8',
            backgroundColor: '#818cf833',
            tension: 0.3,
            fill: true,
            pointRadius: 4,
            pointHoverRadius: 6,
        }}]
    }},
    options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true, max: 100, title: {{ display: true, text: '%' }} }} }} }}
}});

new Chart(document.getElementById('historyMultChart'), {{
    type: 'line',
    data: {{
        labels: {labels},
        datasets: [{{
            label: 'Budget Multiplier',
            data: {multipliers},
            borderColor: '#34d399',
            backgroundColor: '#34d39933',
            tension: 0.3,
            fill: true,
            pointRadius: 4,
            pointHoverRadius: 6,
        }}]
    }},
    options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: 'x' }} }} }} }}
}});

new Chart(document.getElementById('historyTurnsChart'), {{
    type: 'line',
    data: {{
        labels: {labels},
        datasets: [
            {{ label: 'With C3', data: {turns_c3}, borderColor: '#818cf8', tension: 0.3, pointRadius: 4 }},
            {{ label: 'Baseline', data: {turns_base}, borderColor: '#f87171', tension: 0.3, pointRadius: 4 }}
        ]
    }},
    options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }}, scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: 'Turns' }} }} }} }}
}});

new Chart(document.getElementById('historyScenarioChart'), {{
    type: 'line',
    data: {{
        labels: {labels},
        datasets: {scenario_datasets_json}
    }},
    options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }}, scales: {{ y: {{ beginAtZero: true, max: 100, title: {{ display: true, text: 'Savings %' }} }} }} }}
}});
"""


def render_html(report: dict, history: Optional[list] = None) -> str:
    """Render a comprehensive visual HTML report with charts and detailed breakdowns."""
    sc = report["scorecard"]
    longevity = report["session_longevity"]
    scenarios = report["scenarios"]
    timeline = report["timeline"]
    tool_contribs = report.get("tool_contributions", {})
    tool_matrix = report.get("tool_scenario_matrix", {})
    cost_estimates = report.get("cost_estimates", {})
    sampled_files = report.get("sampled_files", [])
    perf_timing = report.get("performance_timing", {}).get("profiles", {})
    hist = _build_history_data(history or [])

    def esc(v):
        return html.escape(str(v))

    # ── Chart data ──
    timeline_labels = json.dumps([t["turn"] for t in timeline])
    timeline_c3 = json.dumps([t["cumulative_c3"] for t in timeline])
    timeline_base = json.dumps([t["cumulative_baseline"] for t in timeline])

    scenario_names = json.dumps([_humanize(s["name"]) for s in scenarios])
    scenario_c3_tokens = json.dumps([s["total_tokens_c3"] for s in scenarios])
    scenario_base_tokens = json.dumps([s["total_tokens_baseline"] for s in scenarios])
    scenario_savings = json.dumps([s["token_savings_pct"] for s in scenarios])
    scenario_lat_c3 = json.dumps([round(s["total_latency_c3_ms"], 1) for s in scenarios])
    scenario_lat_base = json.dumps([round(s["total_latency_baseline_ms"], 1) for s in scenarios])

    # Tool heatmap: stacked bar data — one dataset per tool, values per scenario
    all_tools = sorted(tool_matrix.keys())
    all_scenario_names = [s["name"] for s in scenarios]
    heatmap_datasets = []
    tool_colors = ['#818cf8', '#34d399', '#fbbf24', '#f87171', '#a78bfa', '#38bdf8', '#fb923c', '#e879f9']
    for i, tool in enumerate(all_tools):
        data = [tool_matrix.get(tool, {}).get(sn, 0) for sn in all_scenario_names]
        color = tool_colors[i % len(tool_colors)]
        heatmap_datasets.append({"label": tool, "data": data, "backgroundColor": color})
    heatmap_datasets_json = json.dumps(heatmap_datasets)

    # Performance timing chart data
    perf_cards_html = ""
    perf_e2e_c3_data = {}  # profile_key -> [ms per scenario]
    perf_e2e_base_data = {}
    for pkey, pdata in perf_timing.items():
        speedup = pdata.get("speedup", 0)
        saved_pct = pdata.get("time_saved_pct", 0)
        total_c3_s = pdata.get("total_e2e_c3_ms", 0) / 1000
        total_base_s = pdata.get("total_e2e_baseline_ms", 0) / 1000
        local_c3 = pdata.get("local_overhead_c3_ms", 0)
        local_base = pdata.get("local_overhead_baseline_ms", 0)
        perf_e2e_c3_data[pkey] = [s["e2e_c3_ms"] for s in pdata.get("per_scenario", [])]
        perf_e2e_base_data[pkey] = [s["e2e_baseline_ms"] for s in pdata.get("per_scenario", [])]
        perf_cards_html += f"""<div class="cost-card">
            <div class="cost-model">{esc(pdata['label'])}</div>
            <div class="cost-row"><span class="cost-label">Total E2E (C3)</span><span class="cost-val">{total_c3_s:.1f}s</span></div>
            <div class="cost-row"><span class="cost-label">Total E2E (Base)</span><span class="cost-val" style="color:var(--warn)">{total_base_s:.1f}s</span></div>
            <div class="cost-row"><span class="cost-label">Time Saved</span><span class="cost-val">{saved_pct}%</span></div>
            <div class="cost-row"><span class="cost-label">Speedup</span><span class="cost-val">{speedup}x faster</span></div>
            <div class="cost-row" style="margin-top:0.4rem;padding-top:0.4rem;border-top:1px solid var(--surface2)"><span class="cost-label">Local C3 overhead</span><span class="cost-val" style="color:var(--text-dim)">{local_c3:.0f}ms</span></div>
            <div class="cost-row"><span class="cost-label">Local base overhead</span><span class="cost-val" style="color:var(--text-dim)">{local_base:.0f}ms</span></div>
        </div>"""
    # Use the first profile for the per-scenario chart
    first_profile_key = list(perf_timing.keys())[0] if perf_timing else None
    perf_chart_c3 = json.dumps(perf_e2e_c3_data.get(first_profile_key, []))
    perf_chart_base = json.dumps(perf_e2e_base_data.get(first_profile_key, []))
    first_profile_label = perf_timing.get(first_profile_key, {}).get("label", "") if first_profile_key else ""

    # ── Summary table ──
    summary_rows = ""
    for s in scenarios:
        sav = s["token_savings_pct"]
        sav_color = "#10b981" if sav > 50 else ("#f59e0b" if sav > 20 else "#ef4444")
        q_c3 = s.get("avg_quality_c3", 0)
        q_base = s.get("avg_quality_baseline", 0)
        q_icon = "" if q_c3 >= 99.9 else (' <span class="q-warn" title="Quality below 100%: some steps had imperfect retrieval/extraction">&#9888;</span>' if q_c3 < 100 else "")
        summary_rows += f"""<tr>
            <td class="td-name">{esc(_humanize(s['name']))}</td>
            <td class="num">{s['total_tokens_c3']:,}</td>
            <td class="num">{s['total_tokens_baseline']:,}</td>
            <td class="num" style="color:{sav_color};font-weight:700">{sav}%</td>
            <td class="num">{s['budget_multiplier']}x</td>
            <td class="num">{s['total_latency_c3_ms']:.0f}</td>
            <td class="num">{s['total_latency_baseline_ms']:.0f}</td>
            <td class="num">{q_c3:.0f}%{q_icon}</td>
            <td class="num">{q_base:.0f}%</td>
        </tr>"""
    # Totals row
    t_c3 = sc["total_tokens_c3"]
    t_base = sc["total_tokens_baseline"]
    summary_rows += f"""<tr class="totals-row">
        <td class="td-name"><strong>TOTAL</strong></td>
        <td class="num"><strong>{t_c3:,}</strong></td>
        <td class="num"><strong>{t_base:,}</strong></td>
        <td class="num" style="color:#10b981;font-weight:700"><strong>{sc['token_savings_pct']}%</strong></td>
        <td class="num"><strong>{sc['budget_multiplier']}x</strong></td>
        <td class="num"><strong>{sc['total_latency_c3_ms']:.0f}</strong></td>
        <td class="num"><strong>{sc['total_latency_baseline_ms']:.0f}</strong></td>
        <td class="num"><strong>{sc['avg_quality_c3']:.0f}%</strong></td>
        <td class="num"><strong>{sc['avg_quality_baseline']:.0f}%</strong></td>
    </tr>"""

    # ── Cost cards ──
    cost_cards = ""
    for key, est in cost_estimates.items():
        cost_cards += f"""<div class="cost-card">
            <div class="cost-model">{esc(est['label'])}</div>
            <div class="cost-row"><span class="cost-label">Per session</span><span class="cost-val">${est['saved_per_session']:.4f}</span></div>
            <div class="cost-row"><span class="cost-label">Per day (5 sessions)</span><span class="cost-val">${est['saved_per_day_5_sessions']:.3f}</span></div>
            <div class="cost-row"><span class="cost-label">Per month (22 days)</span><span class="cost-val">${est['saved_per_month']:.2f}</span></div>
        </div>"""

    # ── Sampled files ──
    files_rows = ""
    for f in sampled_files:
        files_rows += f"""<tr><td class="td-path">{esc(f['path'])}</td><td class="num">{f['tokens']:,}</td><td>{esc(f['extension'])}</td></tr>"""

    # ── Scenario detail cards ──
    scenario_cards = []
    for s in scenarios:
        steps_c3 = s.get("steps_c3", [])
        steps_base = s.get("steps_baseline", [])
        max_step_tokens = max(
            max((st["tokens"] for st in steps_c3), default=1),
            max((st["tokens"] for st in steps_base), default=1),
            1
        )

        def _step_rows(steps, badge_cls):
            rows = ""
            for i, step in enumerate(steps):
                bar_pct = min(100, step["tokens"] / max_step_tokens * 100)
                # Find matching step in other path for delta
                delta_html = ""
                if badge_cls == "badge-c3" and i < len(steps_base):
                    saved = steps_base[i]["tokens"] - step["tokens"]
                    if saved > 0:
                        delta_html = f'<span class="step-delta">-{saved:,}</span>'
                elif badge_cls == "badge-base" and i < len(steps_c3):
                    extra = step["tokens"] - steps_c3[i]["tokens"]
                    if extra > 0:
                        delta_html = f'<span class="step-extra">+{extra:,}</span>'
                q_html = ""
                if step.get("quality", 100) < 100:
                    q_html = f' <span class="q-warn" title="Quality: {step["quality"]:.0f}%">&#9888; {step["quality"]:.0f}%</span>'
                rows += f"""<div class="step-row">
                    <span class="step-name">{esc(_humanize(step['name']))}{q_html}</span>
                    <span class="step-tool {badge_cls}">{esc(step['tool'])}</span>
                    <span class="step-tokens">{step['tokens']:,} tok {delta_html}</span>
                    <span class="step-latency">{step['latency_ms']:.1f}ms</span>
                    <div class="step-bar-track"><div class="step-bar {badge_cls}" style="width:{bar_pct:.1f}%"></div></div>
                </div>"""
            return rows

        sav_color = "#10b981" if s["token_savings_pct"] > 50 else ("#f59e0b" if s["token_savings_pct"] > 20 else "#ef4444")

        scenario_cards.append(f"""<div class="scenario-card" id="scenario-{s['name']}">
            <div class="scenario-header">
                <h3>{esc(_humanize(s['name']))}</h3>
                <div class="scenario-savings" style="color:{sav_color}">{s['token_savings_pct']}% saved</div>
            </div>
            <p class="scenario-desc">{esc(s['description'])}</p>
            <div class="scenario-metrics">
                <div class="metric-pill"><span class="metric-label">C3</span><span class="metric-value">{s['total_tokens_c3']:,} tok</span></div>
                <div class="metric-pill"><span class="metric-label">Base</span><span class="metric-value">{s['total_tokens_baseline']:,} tok</span></div>
                <div class="metric-pill"><span class="metric-label">Budget</span><span class="metric-value">{s['budget_multiplier']}x</span></div>
                <div class="metric-pill"><span class="metric-label">Quality C3</span><span class="metric-value">{s['avg_quality_c3']:.0f}%</span></div>
                <div class="metric-pill"><span class="metric-label">Latency C3</span><span class="metric-value">{s['total_latency_c3_ms']:.0f}ms</span></div>
            </div>
            <div class="steps-comparison">
                <div class="steps-col">
                    <h4>With C3</h4>
                    {_step_rows(steps_c3, "badge-c3")}
                    <div class="steps-total">Total: {s['total_tokens_c3']:,} tokens &middot; {s['total_latency_c3_ms']:.0f}ms</div>
                </div>
                <div class="steps-col">
                    <h4>Without C3 (Baseline)</h4>
                    {_step_rows(steps_base, "badge-base")}
                    <div class="steps-total">Total: {s['total_tokens_baseline']:,} tokens &middot; {s['total_latency_baseline_ms']:.0f}ms</div>
                </div>
            </div>
        </div>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C3 Session Benchmark Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3"></script>
<style>
:root {{
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155; --border: #475569;
    --text: #e2e8f0; --text-dim: #94a3b8; --accent: #818cf8; --accent2: #34d399;
    --danger: #f87171; --warn: #fbbf24; --c3: #818cf8; --base: #64748b;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--text); font-family:'Inter',-apple-system,sans-serif; padding:0; line-height:1.6; }}
.container {{ max-width:1440px; margin:0 auto; padding:2rem; padding-top:4rem; }}
h1 {{ font-size:2rem; font-weight:700; margin-bottom:0.5rem; }}
h2 {{ font-size:1.4rem; font-weight:600; margin:2.5rem 0 1rem; color:var(--accent); scroll-margin-top:3.5rem; }}
h3 {{ font-size:1.1rem; font-weight:600; }}
h4 {{ font-size:0.85rem; font-weight:600; color:var(--text-dim); margin-bottom:0.5rem; text-transform:uppercase; letter-spacing:0.05em; }}
.subtitle {{ color:var(--text-dim); margin-bottom:2rem; }}

/* Sticky Nav */
.sticky-nav {{ position:fixed; top:0; left:0; right:0; z-index:100; background:rgba(15,23,42,0.92); backdrop-filter:blur(12px); border-bottom:1px solid var(--border); padding:0.5rem 2rem; display:flex; align-items:center; gap:1.5rem; }}
.sticky-nav .nav-brand {{ font-weight:700; color:var(--accent); font-size:0.9rem; white-space:nowrap; }}
.sticky-nav a {{ color:var(--text-dim); text-decoration:none; font-size:0.8rem; white-space:nowrap; transition:color 0.15s; }}
.sticky-nav a:hover {{ color:var(--accent); }}
.sticky-nav .nav-actions {{ margin-left:auto; display:flex; gap:0.75rem; }}
.btn-sm {{ background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:0.25rem 0.75rem; border-radius:6px; cursor:pointer; font-size:0.75rem; }}
.btn-sm:hover {{ background:var(--border); }}

/* Scorecard */
.scorecard {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:1rem; margin-bottom:2rem; }}
.score-card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.25rem; text-align:center; transition:transform 0.15s; }}
.score-card:hover {{ transform:translateY(-2px); }}
.score-card .value {{ font-size:2rem; font-weight:700; color:var(--accent); }}
.score-card .label {{ font-size:0.75rem; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.05em; margin-top:0.25rem; }}
.score-card.green .value {{ color:var(--accent2); }}
.score-card.warn .value {{ color:var(--warn); }}
.score-card.cost .value {{ color:var(--accent2); font-size:1.6rem; }}

/* Charts */
.chart-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(420px, 1fr)); gap:1.5rem; margin-bottom:2rem; }}
.chart-box {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.5rem; }}
.chart-box canvas {{ max-height:320px; }}
.chart-box.wide {{ grid-column: 1 / -1; }}

/* Summary Table */
.summary-table {{ width:100%; border-collapse:collapse; margin-bottom:2rem; font-size:0.85rem; }}
.summary-table th {{ background:var(--surface2); color:var(--text-dim); padding:0.6rem 0.75rem; text-align:left; font-weight:600; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; cursor:pointer; user-select:none; white-space:nowrap; border-bottom:2px solid var(--border); }}
.summary-table th:hover {{ color:var(--accent); }}
.summary-table td {{ padding:0.5rem 0.75rem; border-bottom:1px solid var(--surface2); }}
.summary-table .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.summary-table .td-name {{ font-weight:600; }}
.summary-table .totals-row td {{ border-top:2px solid var(--accent); background:rgba(129,140,248,0.05); }}
.summary-table .q-warn {{ color:var(--warn); cursor:help; }}

/* Cost Estimation */
.cost-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:1rem; margin-bottom:1.5rem; }}
.cost-card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.25rem; }}
.cost-model {{ font-weight:600; font-size:0.9rem; margin-bottom:0.75rem; color:var(--accent); }}
.cost-row {{ display:flex; justify-content:space-between; padding:0.25rem 0; font-size:0.8rem; }}
.cost-label {{ color:var(--text-dim); }}
.cost-val {{ font-weight:600; color:var(--accent2); }}

/* Scenario Cards */
.scenarios-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(620px, 1fr)); gap:1.5rem; }}
.scenario-card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.5rem; scroll-margin-top:3.5rem; }}
.scenario-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem; }}
.scenario-savings {{ font-size:1.3rem; font-weight:700; }}
.scenario-desc {{ color:var(--text-dim); font-size:0.85rem; margin-bottom:1rem; }}
.scenario-metrics {{ display:flex; gap:0.6rem; flex-wrap:wrap; margin-bottom:1rem; }}
.metric-pill {{ background:var(--surface2); border-radius:8px; padding:0.35rem 0.65rem; display:flex; gap:0.4rem; align-items:center; }}
.metric-label {{ font-size:0.65rem; color:var(--text-dim); text-transform:uppercase; }}
.metric-value {{ font-size:0.85rem; font-weight:600; }}

/* Steps */
.steps-comparison {{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; }}
.steps-col {{ background:var(--bg); border-radius:8px; padding:1rem; }}
.step-row {{ padding:0.4rem 0; font-size:0.8rem; border-bottom:1px solid var(--surface2); }}
.step-row > span {{ display:inline-block; vertical-align:middle; }}
.step-name {{ min-width:120px; }}
.step-tool {{ font-size:0.7rem; padding:0.15rem 0.4rem; border-radius:4px; }}
.badge-c3 {{ background:rgba(129,140,248,0.2); color:var(--c3); }}
.badge-base {{ background:rgba(100,116,139,0.2); color:var(--base); }}
.step-tokens {{ color:var(--accent2); min-width:90px; text-align:right; }}
.step-delta {{ color:var(--accent2); font-size:0.7rem; font-weight:600; margin-left:0.3rem; }}
.step-extra {{ color:var(--danger); font-size:0.7rem; font-weight:600; margin-left:0.3rem; }}
.step-latency {{ color:var(--text-dim); min-width:55px; text-align:right; }}
.step-bar-track {{ width:100%; height:4px; background:var(--surface2); border-radius:2px; margin-top:0.3rem; }}
.step-bar {{ height:4px; border-radius:2px; transition:width 0.3s; }}
.step-bar.badge-c3 {{ background:var(--c3); }}
.step-bar.badge-base {{ background:var(--base); }}
.steps-total {{ margin-top:0.5rem; padding-top:0.5rem; border-top:1px solid var(--border); font-size:0.8rem; font-weight:600; }}
.q-warn {{ color:var(--warn); font-size:0.7rem; cursor:help; }}

/* Longevity */
.longevity-box {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.5rem; margin-bottom:2rem; }}
.longevity-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:1rem; text-align:center; }}
.longevity-stat .num {{ font-size:1.8rem; font-weight:700; }}
.longevity-stat .lbl {{ font-size:0.8rem; color:var(--text-dim); }}

/* Sampled Files */
.files-table {{ width:100%; border-collapse:collapse; font-size:0.8rem; margin-top:0.5rem; }}
.files-table th {{ text-align:left; padding:0.4rem 0.6rem; color:var(--text-dim); font-weight:600; font-size:0.7rem; text-transform:uppercase; border-bottom:1px solid var(--border); }}
.files-table td {{ padding:0.3rem 0.6rem; border-bottom:1px solid var(--surface2); }}
.files-table .td-path {{ font-family:monospace; font-size:0.75rem; }}

/* Info Sections */
.info-section {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.5rem 2rem; margin-bottom:1.5rem; }}
.info-section h3 {{ margin-bottom:0.75rem; }}
.info-section p, .info-section li {{ color:var(--text-dim); font-size:0.85rem; line-height:1.7; }}
.info-section ul {{ padding-left:1.25rem; margin:0.5rem 0; }}
.info-section li {{ margin-bottom:0.3rem; }}
.info-section strong {{ color:var(--text); }}
.info-section code {{ background:var(--surface2); padding:0.15rem 0.4rem; border-radius:4px; font-size:0.8rem; color:var(--accent); }}
.info-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(350px, 1fr)); gap:1.5rem; }}
.info-grid .info-section {{ margin-bottom:0; }}
.run-params {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:0.75rem; margin-top:0.75rem; }}
.param-box {{ background:var(--bg); border-radius:8px; padding:0.5rem 0.8rem; }}
.param-box .param-key {{ font-size:0.65rem; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.04em; }}
.param-box .param-val {{ font-size:0.95rem; font-weight:600; color:var(--text); }}
.collapsible-toggle {{ cursor:pointer; user-select:none; display:flex; align-items:center; gap:0.5rem; }}
.collapsible-toggle::before {{ content:'\\25B6'; font-size:0.7rem; transition:transform 0.2s; }}
.collapsible-toggle.open::before {{ transform:rotate(90deg); }}
.collapsible-content {{ max-height:0; overflow:hidden; transition:max-height 0.3s ease; }}
.collapsible-content.open {{ max-height:4000px; }}
.scenario-explainer {{ background:var(--bg); border-radius:8px; padding:1rem; margin-top:0.5rem; }}
.scenario-explainer .ex-title {{ font-weight:600; font-size:0.9rem; color:var(--accent); margin-bottom:0.3rem; }}
.scenario-explainer .ex-flow {{ display:flex; gap:0.4rem; flex-wrap:wrap; align-items:center; margin:0.4rem 0; }}
.flow-step {{ background:var(--surface2); border-radius:6px; padding:0.2rem 0.5rem; font-size:0.75rem; }}
.flow-arrow {{ color:var(--text-dim); font-size:0.7rem; }}
.metric-def {{ display:grid; grid-template-columns:140px 1fr; gap:0.3rem 1rem; margin-top:0.5rem; }}
.metric-def dt {{ font-weight:600; font-size:0.8rem; color:var(--accent); }}
.metric-def dd {{ font-size:0.8rem; color:var(--text-dim); }}

/* Footer */
.footer {{ margin-top:3rem; padding:1.5rem 0; text-align:center; color:var(--text-dim); font-size:0.8rem; border-top:1px solid var(--border); }}

/* Print */
@media print {{
    body {{ background:#fff; color:#1a1a1a; padding:1rem; }}
    .sticky-nav {{ display:none; }}
    .container {{ padding-top:0; }}
    .score-card, .chart-box, .scenario-card, .info-section, .longevity-box, .cost-card {{ border-color:#ccc; background:#fff; }}
    .score-card .value, h2, .scenario-savings, .cost-model {{ color:#333; }}
    .step-bar-track {{ background:#eee; }}
    .step-bar.badge-c3 {{ background:#6366f1; }}
    .step-bar.badge-base {{ background:#9ca3af; }}
}}
</style>
</head>
<body>

<!-- Sticky Navigation -->
<nav class="sticky-nav">
    <span class="nav-brand">C3 Session Benchmark</span>
    <a href="#scorecard">Scorecard</a>
    <a href="#longevity">Longevity</a>
    <a href="#charts">Charts</a>
    <a href="#summary">Summary</a>
    <a href="#cost">Cost</a>
    <a href="#performance">Performance</a>
    <a href="#methodology">Methodology</a>
    <a href="#scenarios">Scenarios</a>
    <a href="#details">Details</a>
    <a href="#files">Files</a>
    <a href="#history">History</a>
    <div class="nav-actions">
        <button class="btn-sm" onclick="window.print()">Export PDF</button>
    </div>
</nav>

<div class="container">

<h1>C3 Session Benchmark <span style="background:#818cf8;color:#0b1020;padding:0.15rem 0.55rem;border-radius:999px;font-size:0.7rem;font-weight:600;margin-left:0.5rem;vertical-align:middle">Synthetic</span> <a href="../benchmarks/index.html" style="color:var(--text-dim,#9aa3c7);font-size:0.8rem;margin-left:0.8rem;text-decoration:none;font-weight:400">← dashboard</a></h1>
<p class="subtitle">Real-world workflow simulation &middot; {esc(report.get('timestamp', '').replace('T', ' '))} &middot; {report['files_considered']} files &middot; {len(scenarios)} scenarios</p>

<!-- About -->
<div class="info-section">
    <h3 class="collapsible-toggle open" onclick="this.classList.toggle('open'); this.nextElementSibling.classList.toggle('open')">About This Benchmark</h3>
    <div class="collapsible-content open">
        <p>
            This benchmark simulates <strong>real-world AI coding session workflows</strong> end-to-end, comparing
            <strong>with C3 tools</strong> (c3_search, c3_compress, c3_read, c3_filter, c3_validate) versus <strong>without C3</strong>
            (native file reads, lexical grep, full-file context loading). Each scenario represents a common developer task broken into
            sequential steps. Both paths perform equivalent work on the same files, but C3 uses intelligent compression, surgical reading,
            and semantic search to minimize tokens loaded into context.
        </p>
    </div>
</div>

<!-- Run Parameters -->
<div class="info-section">
    <h3>Run Parameters</h3>
    <div class="run-params">
        <div class="param-box"><div class="param-key">Project</div><div class="param-val">{esc(Path(report.get('project_path','')).name)}</div></div>
        <div class="param-box"><div class="param-key">Files Eligible</div><div class="param-val">{report['files_considered']}</div></div>
        <div class="param-box"><div class="param-key">Files Sampled</div><div class="param-val">{report['sample_size']}</div></div>
        <div class="param-box"><div class="param-key">Scenarios</div><div class="param-val">{len(scenarios)}</div></div>
        <div class="param-box"><div class="param-key">Timestamp</div><div class="param-val">{esc(report.get('timestamp','').replace('T',' '))}</div></div>
        <div class="param-box"><div class="param-key">Context Limit</div><div class="param-val">{longevity['context_limit']:,} tok</div></div>
    </div>
</div>

<!-- Scorecard -->
<h2 id="scorecard">Scorecard</h2>
<div class="scorecard">
    <div class="score-card green"><div class="value">{sc['token_savings_pct']}%</div><div class="label">Token Savings</div></div>
    <div class="score-card"><div class="value">{sc['budget_multiplier']}x</div><div class="label">Budget Multiplier</div></div>
    <div class="score-card green"><div class="value">{longevity['estimated_turns_c3']}</div><div class="label">Est. Turns (C3)</div></div>
    <div class="score-card warn"><div class="value">{longevity['estimated_turns_baseline']}</div><div class="label">Est. Turns (Base)</div></div>
    <div class="score-card"><div class="value">{longevity['turn_multiplier']}x</div><div class="label">Session Multiplier</div></div>
    <div class="score-card"><div class="value">{sc['avg_quality_c3']:.0f}%</div><div class="label">Avg Quality (C3)</div></div>
    <div class="score-card"><div class="value">{sc['total_latency_c3_ms']:.0f}ms</div><div class="label">Total Latency (C3)</div></div>
    <div class="score-card"><div class="value">{sc['total_tokens_c3']:,}</div><div class="label">Total Tokens (C3)</div></div>
    <div class="score-card green"><div class="value">{list(perf_timing.values())[0].get('speedup', 0) if perf_timing else 0}x</div><div class="label">E2E Speedup</div></div>
</div>

<!-- Longevity -->
<h2 id="longevity">Session Longevity Projection</h2>
<div class="longevity-box" style="margin-top:0">
    <p style="color:var(--text-dim); margin-bottom:1rem; font-size:0.85rem">
        Estimated turns before hitting {longevity['context_limit']:,} token context limit. Each "turn" is one complete workflow scenario.
    </p>
    <div class="longevity-grid">
        <div class="longevity-stat"><div class="num" style="color:var(--accent)">{longevity['avg_tokens_per_turn_c3']:,}</div><div class="lbl">Avg tokens/turn (C3)</div></div>
        <div class="longevity-stat"><div class="num" style="color:var(--base)">{longevity['avg_tokens_per_turn_baseline']:,}</div><div class="lbl">Avg tokens/turn (Base)</div></div>
        <div class="longevity-stat"><div class="num" style="color:var(--accent)">{longevity['estimated_turns_c3']}</div><div class="lbl">Turns before limit (C3)</div></div>
        <div class="longevity-stat"><div class="num" style="color:var(--warn)">{longevity['estimated_turns_baseline']}</div><div class="lbl">Turns before limit (Base)</div></div>
        <div class="longevity-stat"><div class="num" style="color:var(--accent2)">{longevity['turn_multiplier']}x</div><div class="lbl">Session Multiplier</div></div>
    </div>
</div>

<!-- Charts -->
<h2 id="charts">Visual Analysis</h2>
<div class="chart-grid">
    <div class="chart-box wide">
        <h3>Cumulative Token Usage (Turn by Turn)</h3>
        <p style="color:var(--text-dim);font-size:0.8rem;margin:0.3rem 0 0.5rem">Shows how context accumulates over turns. The gap is wasted tokens C3 prevents. Red dashed = context limit. Orange zone = danger (80%+).</p>
        <canvas id="timelineChart" style="max-height:380px"></canvas>
    </div>
    <div class="chart-box">
        <h3>Token Usage by Scenario</h3>
        <p style="color:var(--text-dim);font-size:0.8rem;margin:0.3rem 0 0.5rem">Side-by-side token consumption per workflow.</p>
        <canvas id="scenarioChart"></canvas>
    </div>
    <div class="chart-box">
        <h3>Savings by Scenario (%)</h3>
        <canvas id="savingsChart"></canvas>
    </div>
    <div class="chart-box">
        <h3>Latency Comparison (ms)</h3>
        <p style="color:var(--text-dim);font-size:0.8rem;margin:0.3rem 0 0.5rem">C3 trades local ms for massive token savings.</p>
        <canvas id="latencyChart"></canvas>
    </div>
    <div class="chart-box">
        <h3>Tool Savings by Scenario (Stacked)</h3>
        <p style="color:var(--text-dim);font-size:0.8rem;margin:0.3rem 0 0.5rem">Which C3 tools contributed most savings in each scenario.</p>
        <canvas id="heatmapChart"></canvas>
    </div>
</div>

<!-- Summary Table -->
<h2 id="summary">Summary Comparison</h2>
<div style="overflow-x:auto">
<table class="summary-table" id="summaryTable">
    <thead>
        <tr>
            <th onclick="sortTable(0)">Scenario</th>
            <th onclick="sortTable(1)">C3 Tokens</th>
            <th onclick="sortTable(2)">Base Tokens</th>
            <th onclick="sortTable(3)">Savings %</th>
            <th onclick="sortTable(4)">Budget x</th>
            <th onclick="sortTable(5)">C3 Latency ms</th>
            <th onclick="sortTable(6)">Base Latency ms</th>
            <th onclick="sortTable(7)">C3 Quality</th>
            <th onclick="sortTable(8)">Base Quality</th>
        </tr>
    </thead>
    <tbody>
        {summary_rows}
    </tbody>
</table>
</div>

<!-- Cost Estimation -->
<h2 id="cost">Estimated Cost Savings</h2>
<div class="info-section" style="margin-bottom:1rem">
    <p>Based on {sc['total_tokens_baseline'] - sc['total_tokens_c3']:,} tokens saved per session. Cost = input token pricing only (output tokens are unaffected by C3).
    Assumes 5 sessions/day, 22 working days/month.</p>
</div>
<div class="cost-grid">
    {cost_cards}
</div>

<!-- Performance Timing -->
<h2 id="performance">End-to-End Performance Timing</h2>
<div class="info-section" style="margin-bottom:1rem">
    <p>Estimates total wall-clock time for an AI assistant to process each scenario, including model inference (input tokenization + output generation) and network overhead.
    Fewer input tokens = faster model processing = shorter wait times for the developer.</p>
</div>
<div class="cost-grid">
    {perf_cards_html}
</div>
<div class="chart-card" style="margin-top:1.5rem">
    <h3>Per-Scenario E2E Time &mdash; {esc(first_profile_label)}</h3>
    <canvas id="perfChart"></canvas>
</div>

<!-- Methodology -->
<h2 id="methodology">Methodology</h2>
<div class="info-grid">
    <div class="info-section">
        <h3>How Measurements Work</h3>
        <ul>
            <li><strong>Token counting</strong> uses the same tokenizer as the AI model. Every piece of text loaded into context is counted.</li>
            <li><strong>Latency</strong> measured via <code>time.perf_counter()</code> — real wall-clock time including disk I/O, index lookups, and compression.</li>
            <li><strong>Quality</strong> scored 0&ndash;100% per step: Did search find the target file? Did the file map build? Did surgical reading extract the right symbol? Did filtering retain error signals?</li>
            <li><strong>Both paths do equivalent work.</strong> The baseline represents how a capable AI assistant actually operates without C3: full file reads, lexical grep, complete log loading.</li>
        </ul>
    </div>
    <div class="info-section">
        <h3>Metric Definitions</h3>
        <dl class="metric-def">
            <dt>Token Savings %</dt><dd><code>(baseline - c3) / baseline &times; 100</code></dd>
            <dt>Budget Multiplier</dt><dd><code>baseline_tokens / c3_tokens</code> — how many times more info fits in context.</dd>
            <dt>Est. Turns</dt><dd>Turns before hitting 200K context limit, based on avg tokens/turn.</dd>
            <dt>Session Multiplier</dt><dd><code>turns_c3 / turns_baseline</code></dd>
            <dt>Quality Score</dt><dd>Avg accuracy. 100% = perfect. &lt;100% = some info missed (marked with &#9888;).</dd>
            <dt>Latency</dt><dd>Local wall-clock ms. C3 trades local compute for token savings.</dd>
        </dl>
    </div>
</div>

<!-- Scenario Explanations -->
<h2 id="scenarios">Scenario Descriptions</h2>
<div class="info-section">
    <p style="margin-bottom:1rem">Each scenario simulates a real developer workflow with 3&ndash;4 sequential steps.</p>
    <div class="scenario-explainer">
        <div class="ex-title">1. Bug Investigation</div>
        <p>Find, understand, and validate a fix for an error.</p>
        <div class="ex-flow"><span class="flow-step">Search error</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Map structure</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Read symbol</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Validate</span></div>
    </div>
    <div class="scenario-explainer">
        <div class="ex-title">2. Feature Exploration</div>
        <p>Understand how a feature works across multiple files.</p>
        <div class="ex-flow"><span class="flow-step">Discover files</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Compress each</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Read key symbols</span></div>
    </div>
    <div class="scenario-explainer">
        <div class="ex-title">3. Code Review</div>
        <p>Review changed files for correctness and style.</p>
        <div class="ex-flow"><span class="flow-step">Compress files</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Read flagged</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Validate all</span></div>
    </div>
    <div class="scenario-explainer">
        <div class="ex-title">4. Log Diagnosis</div>
        <p>Triage errors from logs and terminal output.</p>
        <div class="ex-flow"><span class="flow-step">Filter log</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Filter terminal</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Search code</span></div>
    </div>
    <div class="scenario-explainer">
        <div class="ex-title">5. Refactor Planning</div>
        <p>Understand callers, implementations, and impact before refactoring.</p>
        <div class="ex-flow"><span class="flow-step">Search callers</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Compress callers</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Read impl</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Map impact</span></div>
    </div>
    <div class="scenario-explainer">
        <div class="ex-title">6. Onboarding</div>
        <p>New contributor explores project structure and key files.</p>
        <div class="ex-flow"><span class="flow-step">Search structure</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Compress files</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Read entries</span><span class="flow-arrow">&rarr;</span><span class="flow-step">Search patterns</span></div>
    </div>
</div>

<!-- C3 Tools Reference -->
<div class="info-grid" style="margin-top:1rem">
    <div class="info-section">
        <h3>C3 Tools Used</h3>
        <ul>
            <li><code>c3_search</code> &mdash; TF-IDF semantic code search. Returns relevant snippets, not full files.</li>
            <li><code>c3_compress</code> &mdash; Structural summaries (classes, functions, signatures). 40&ndash;90% savings.</li>
            <li><code>c3_compress(map)</code> &mdash; Lightweight file layout map for targeted reads.</li>
            <li><code>c3_read</code> &mdash; Extract specific symbols by name without full-file reads.</li>
            <li><code>c3_filter</code> &mdash; Surface errors/warnings from logs, collapse repetition.</li>
            <li><code>c3_validate</code> &mdash; AST syntax check. Near-zero token cost.</li>
        </ul>
    </div>
    <div class="info-section">
        <h3>Baseline (Without C3)</h3>
        <ul>
            <li><strong>File reads:</strong> Full content loaded. Every byte enters the prompt.</li>
            <li><strong>Search:</strong> Lexical term matching + full-file loading.</li>
            <li><strong>Logs:</strong> Entire log loaded. AI scans visually for errors.</li>
            <li><strong>Validation:</strong> Re-reads full file. Scored at 80% (misses subtle errors).</li>
            <li><strong>Multi-file:</strong> Each file read in full, often multiple times across steps.</li>
        </ul>
    </div>
</div>

<!-- Scenario Details -->
<h2 id="details">Workflow Scenario Details</h2>
<div class="scenarios-grid">
    {"".join(scenario_cards)}
</div>

<!-- Sampled Files -->
<h2 id="files">Sampled Files</h2>
<div class="info-section">
    <h3 class="collapsible-toggle" onclick="this.classList.toggle('open'); this.nextElementSibling.classList.toggle('open')">Files Used in This Benchmark ({len(sampled_files)} files)</h3>
    <div class="collapsible-content">
        <table class="files-table">
            <thead><tr><th>Path</th><th style="text-align:right">Tokens</th><th>Type</th></tr></thead>
            <tbody>{files_rows}</tbody>
        </table>
    </div>
</div>

<!-- How to Read -->
<h2>How to Read This Report</h2>
<div class="info-section">
    <ul>
        <li><strong>Scorecard</strong> &mdash; headline numbers. Token savings directly determines session longevity and available reasoning context.</li>
        <li><strong>Timeline chart</strong> &mdash; cumulative tokens turn-by-turn. The gap between lines is wasted tokens C3 prevents. Orange zone = danger. Red dashed = limit.</li>
        <li><strong>Summary table</strong> &mdash; sortable comparison of all scenarios. Click column headers to sort.</li>
        <li><strong>Cost estimation</strong> &mdash; translates token savings to dollar amounts based on common model pricing.</li>
        <li><strong>Scenario cards</strong> &mdash; step-by-step breakdown with per-step token bars, delta indicators, and quality warnings.</li>
        <li><strong>Tool heatmap</strong> &mdash; stacked bar showing which C3 tools saved the most tokens in each scenario.</li>
    </ul>
    <p style="margin-top:0.5rem"><strong>Note on latency:</strong> C3 is slightly slower than raw reads (ms of compression vs. &micro;s of disk I/O).
    This is intentional &mdash; spending a few ms locally prevents thousands of tokens from entering context, saving significant AI inference time and cost.</p>
</div>

<!-- Benchmark History / Trends -->
<h2 id="history">Benchmark History</h2>
{_render_history_section(hist)}

<div class="footer">
    Generated by C3 Session Benchmark &middot; {esc(report.get('project_path', ''))} &middot;
    <code>c3 session-benchmark</code> to regenerate
</div>

</div>

<script>
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#475569';

// Timeline with annotation
const dangerZone = {longevity['context_limit']} * 0.8;
new Chart(document.getElementById('timelineChart'), {{
    type: 'line',
    data: {{
        labels: {timeline_labels},
        datasets: [
            {{ label: 'With C3', data: {timeline_c3}, borderColor: '#818cf8', backgroundColor: 'rgba(129,140,248,0.08)', fill: true, tension: 0.3, pointRadius: 1 }},
            {{ label: 'Without C3', data: {timeline_base}, borderColor: '#64748b', backgroundColor: 'rgba(100,116,139,0.08)', fill: true, tension: 0.3, pointRadius: 1 }},
            {{ label: 'Context Limit ({longevity["context_limit"]:,})', data: {json.dumps([longevity['context_limit']] * len(timeline))}, borderColor: '#f87171', borderDash: [6,3], pointRadius: 0, fill: false, borderWidth: 2 }}
        ]
    }},
    options: {{
        responsive: true,
        plugins: {{
            legend: {{ position: 'bottom' }},
            annotation: {{
                annotations: {{
                    dangerZone: {{
                        type: 'box', yMin: dangerZone, yMax: {longevity['context_limit']},
                        backgroundColor: 'rgba(251,191,36,0.06)', borderWidth: 0,
                        label: {{ display: true, content: 'Danger Zone (80%+)', position: 'start', color: '#fbbf24', font: {{ size: 10 }} }}
                    }},
                    baseExhausted: {{
                        type: 'line', xMin: {longevity['estimated_turns_baseline']}, xMax: {longevity['estimated_turns_baseline']},
                        borderColor: '#f87171', borderDash: [3,3], borderWidth: 1,
                        label: {{ display: true, content: 'Base exhausted', position: 'start', color: '#f87171', font: {{ size: 10 }} }}
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{ title: {{ display: true, text: 'Turn #' }} }},
            y: {{ title: {{ display: true, text: 'Cumulative Tokens' }}, beginAtZero: true }}
        }}
    }}
}});

// Scenario tokens
new Chart(document.getElementById('scenarioChart'), {{
    type: 'bar',
    data: {{
        labels: {scenario_names},
        datasets: [
            {{ label: 'With C3', data: {scenario_c3_tokens}, backgroundColor: '#818cf8' }},
            {{ label: 'Without C3', data: {scenario_base_tokens}, backgroundColor: '#64748b' }}
        ]
    }},
    options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }}, scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: 'Tokens' }} }} }} }}
}});

// Savings
new Chart(document.getElementById('savingsChart'), {{
    type: 'bar',
    data: {{
        labels: {scenario_names},
        datasets: [{{ label: 'Savings %', data: {scenario_savings}, backgroundColor: {scenario_savings}.map(v => v > 80 ? '#10b981' : v > 50 ? '#34d399' : '#f59e0b') }}]
    }},
    options: {{ responsive: true, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ beginAtZero: true, max: 100, title: {{ display: true, text: '%' }} }} }} }}
}});

// Latency comparison (new)
new Chart(document.getElementById('latencyChart'), {{
    type: 'bar',
    data: {{
        labels: {scenario_names},
        datasets: [
            {{ label: 'C3 Latency (ms)', data: {scenario_lat_c3}, backgroundColor: '#818cf8' }},
            {{ label: 'Baseline Latency (ms)', data: {scenario_lat_base}, backgroundColor: '#64748b' }}
        ]
    }},
    options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }}, scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: 'ms' }} }} }} }}
}});

// Tool heatmap (stacked bar)
new Chart(document.getElementById('heatmapChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps([_humanize(n) for n in all_scenario_names])},
        datasets: {heatmap_datasets_json}
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ position: 'bottom' }} }},
        scales: {{ x: {{ stacked: true }}, y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: 'Tokens Saved' }} }} }}
    }}
}});

// Performance timing chart
new Chart(document.getElementById('perfChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps([_humanize(n) for n in all_scenario_names])},
        datasets: [
            {{ label: 'With C3', data: {perf_chart_c3}.map(v => v / 1000), backgroundColor: '#818cf8' }},
            {{ label: 'Baseline', data: {perf_chart_base}.map(v => v / 1000), backgroundColor: '#f87171' }}
        ]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ position: 'bottom' }} }},
        scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: 'Seconds' }} }} }}
    }}
}});

// Sortable table
function sortTable(col) {{
    const table = document.getElementById('summaryTable');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr:not(.totals-row)'));
    const totalsRow = tbody.querySelector('.totals-row');
    const dir = table.dataset.sortDir === 'asc' ? 'desc' : 'asc';
    table.dataset.sortDir = dir;
    rows.sort((a, b) => {{
        let aVal = a.cells[col].textContent.replace(/[,%x$]/g, '').trim();
        let bVal = b.cells[col].textContent.replace(/[,%x$]/g, '').trim();
        let aNum = parseFloat(aVal), bNum = parseFloat(bVal);
        if (!isNaN(aNum) && !isNaN(bNum)) return dir === 'asc' ? aNum - bNum : bNum - aNum;
        return dir === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
    }});
    rows.forEach(r => tbody.appendChild(r));
    if (totalsRow) tbody.appendChild(totalsRow);
}}

// Smooth scroll
document.querySelectorAll('.sticky-nav a[href^="#"]').forEach(a => {{
    a.addEventListener('click', e => {{
        e.preventDefault();
        document.querySelector(a.getAttribute('href')).scrollIntoView({{ behavior: 'smooth' }});
    }});
}});

{_render_history_charts_js(hist)}
</script>
</body>
</html>"""
