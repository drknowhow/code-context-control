"""Terminal Output Filter — Two-pass pipeline for reducing terminal noise.

Pass 1 (always): Strip ANSI, collapse progress bars, deduplicate PASS/OK lines,
                  collapse repeated lines, normalize blanks.
Pass 2 (optional): If pass1 output > threshold tokens, use Ollama LLM for
                   3-5 line summary. Status-aware: success=terse, failure=preserve errors.
"""
import re
import threading
import time
from collections import Counter, defaultdict, deque

from core import count_tokens
from services.ollama_client import OllamaClient

# ── ANSI escape regex ────────────────────────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\(B')

# ── Progress bar patterns ────────────────────────────────
_PROGRESS_RE = re.compile(
    r'[\s]*[\|#=\-\>\.]{5,}[\s]*\d+%'  # ||||||||| 50%
    r'|[\s]*\d+%[\s]*[\|#=\-\>\.]{5,}'  # 50% |||||||||
    r'|[\s]*\d+/\d+[\s]*[\|#=\-\>\.]{3,}'  # 5/10 ||||
    r'|[\s]*[\u2588\u2591\u2592\u2593]{3,}'  # Unicode blocks
    r'|\r[^\n]*\d+%'  # Carriage-return progress
)

# ── Pass/OK line patterns ────────────────────────────────
_PASS_RE = re.compile(
    r'^\s*(PASS|PASSED|OK|ok|\u2713|✓|\.)\s+'
    r'|^\s*test[_\s].*\.\.\.\s*(ok|PASS)',
    re.IGNORECASE,
)

# ── Error/failure patterns (to preserve) ─────────────────
_ERROR_RE = re.compile(
    r'ERROR|FAIL|FAILED|Exception|Traceback|panic|CRITICAL'
    r'|error\[|warning\[|^\s*E\s+'
    r'|assert|AssertionError|TypeError|ValueError|KeyError'
    r'|ModuleNotFoundError|ImportError|FileNotFoundError',
    re.IGNORECASE,
)
_PYTEST_PASS_RE = re.compile(
    r'^(?P<target>[^\s:][^:]*(?:::[^\s:]+)+)\s+(?P<status>PASSED|PASS|OK)\b',
    re.IGNORECASE,
)

# ── Package manager noise (npm, pip, cargo) ──────────────
_PKG_NOISE_RE = re.compile(
    r'^(npm (http fetch|notice|verb|WARN)|'
    r'Requirement already satisfied:|'
    r'\s*(Downloading|Fetching|Compiling|Building|Updating|Installed)\s+'
    r'|.*=> (Resolving|Downloading|Building|installing))',
    re.IGNORECASE,
)
_SUCCESS_SUMMARY_RE = re.compile(
    r'('
    r'collected\s+\d+\s+items?'
    r'|=+.*\b(passed|failed|warnings?)\b.*=+'
    r'|\b\d+\s+passed\b'
    r'|\b\d+\s+failed\b'
    r'|\b\d+\s+warnings?\b'
    r'|\bran\s+\d+\s+tests?\s+in\s+'
    r'|\b(build|built|compile|compiled|bundle|bundled|finish|finished|done)\b'
    r'|\bin\s+\d+(?:\.\d+)?s\b'
    r'|\badded\s+\d+\s+packages?\b'
    r'|\binstalled\b'
    r')',
    re.IGNORECASE,
)
_WARNING_RE = re.compile(r'\b(warning|warn)\b', re.IGNORECASE)

class OutputFilter:
    """Two-pass terminal output filter with optional LLM summarization."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        base_url = self.config.get("ollama_base_url", "http://localhost:11434")
        self.ollama = OllamaClient(base_url)
        self.filter_model = self.config.get("filter_model", "gemma3n:latest")
        self.llm_threshold = self.config.get("filter_llm_threshold", 500)
        self._lock = threading.Lock()

        # Pass-2 adaptive backoff: a slow Ollama must not stall every filtered
        # tool output. Each pass-2 call gets a hard per-call timeout; if the
        # last N calls all ran into it, pass-2 is suspended for a cooldown
        # window and the skip is noted once in the filter output.
        self.pass2_timeout = max(0.1, float(self.config.get("filter_pass2_timeout", 2.0)))
        self.pass2_latency_window = max(1, int(self.config.get("filter_pass2_latency_window", 3)))
        self.pass2_suspend_seconds = max(0.0, float(self.config.get("filter_pass2_suspend_seconds", 300.0)))
        self._pass2_latencies: deque[float] = deque(maxlen=self.pass2_latency_window)
        self._pass2_suspended_until = 0.0
        self._pass2_suspend_noted = False

        # Metrics
        self.metrics = {
            "calls": 0,
            "raw_tokens": 0,
            "filtered_tokens": 0,
            "llm_calls": 0,
            "total_savings_pct": 0.0,
            "pass2_calls": 0,
            "pass2_timeouts": 0,
            "pass2_suspended": 0,
        }

    def filter(self, text: str, use_llm: bool = True) -> dict:
        """Run the two-pass filter pipeline.

        Returns dict with: filtered, raw_tokens, filtered_tokens, savings_pct,
                           pass_used (1 or 2), llm_used (bool),
                           pass2_suspended (bool — adaptive backoff active)
        """
        if not text or not text.strip():
            return {
                "filtered": text,
                "raw_tokens": 0,
                "filtered_tokens": 0,
                "savings_pct": 0,
                "pass_used": 0,
                "llm_used": False,
                "pass2_suspended": False,
            }

        raw_tokens = count_tokens(text)

        # Pass 1: deterministic filtering
        pass1 = self._pass1(text)
        pass1_tokens = count_tokens(pass1)
        mode = self._detect_mode(pass1.splitlines())

        result_text = pass1
        llm_used = False
        pass_used = 1

        # Compact verbose output even when LLM summarization is disabled.
        compact = self._summarize_signal_output(pass1, mode=mode)
        if compact and count_tokens(compact) < pass1_tokens:
            result_text = compact
            pass1_tokens = count_tokens(compact)

        # Pass 2: LLM summarization if still too large (skipped while the
        # adaptive backoff has pass-2 suspended after consecutive slow calls).
        if use_llm and pass1_tokens > self.llm_threshold:
            if not self.config.get("HYBRID_DISABLE_TIER1"):
                if self._pass2_is_suspended():
                    note = self._pass2_suspension_note()
                    if note:
                        result_text = f"{note}\n{result_text}"
                else:
                    llm_result = self._pass2(result_text, raw_text=text)
                    if llm_result:
                        result_text = llm_result
                        llm_used = True
                        pass_used = 2

        pass2_suspended = self._pass2_is_suspended()
        filtered_tokens = count_tokens(result_text)
        savings_pct = round((1 - filtered_tokens / raw_tokens) * 100, 1) if raw_tokens > 0 else 0

        # Update metrics
        with self._lock:
            self.metrics["calls"] += 1
            self.metrics["raw_tokens"] += raw_tokens
            self.metrics["filtered_tokens"] += filtered_tokens
            if llm_used:
                self.metrics["llm_calls"] += 1
            total_raw = self.metrics["raw_tokens"]
            total_filt = self.metrics["filtered_tokens"]
            self.metrics["total_savings_pct"] = round(
                (1 - total_filt / total_raw) * 100, 1
            ) if total_raw > 0 else 0

        return {
            "filtered": result_text,
            "raw_tokens": raw_tokens,
            "filtered_tokens": filtered_tokens,
            "savings_pct": savings_pct,
            "pass_used": pass_used,
            "llm_used": llm_used,
            "pass2_suspended": pass2_suspended,
        }

    def get_metrics(self) -> dict:
        """Return accumulated filter metrics."""
        with self._lock:
            return dict(self.metrics)

    # ── Pass 1: Deterministic filtering ──────────────────

    def _pass1(self, text: str) -> str:
        """Strip ANSI, collapse progress, deduplicate PASS lines, collapse repeats."""
        lines = text.splitlines()
        mode = self._detect_mode(lines)

        # Strip ANSI codes
        lines = [_ANSI_RE.sub('', line) for line in lines]

        # Remove pure progress bar lines and package manager noise
        lines = [line for line in lines if not _PROGRESS_RE.fullmatch(line) and not _PKG_NOISE_RE.search(line)]

        # Collapse tracebacks before other summarization passes.
        lines = self._collapse_tracebacks(lines)

        # Collapse PASS/OK lines, with stronger grouping for test output.
        lines = self._collapse_pass_lines(lines, mode=mode)

        # Collapse repeated identical lines, then noisy repeats across the full output.
        lines = self._collapse_repeats(lines)
        lines = self._collapse_global_repeats(lines, mode=mode)

        # Normalize multiple blank lines to single
        lines = self._normalize_blanks(lines)

        return '\n'.join(lines)

    def _detect_mode(self, lines: list[str]) -> str:
        """Infer a coarse output mode so filtering can be more aggressive."""
        sample = "\n".join(lines[:120]).lower()
        if "passed" in sample and ("pytest" in sample or "tests/" in sample or "::test_" in sample):
            return "test"
        if "downloading" in sample or "installing" in sample or "fetching" in sample:
            return "install"
        if "build" in sample or "compil" in sample or "bundl" in sample:
            return "build"
        return "generic"

    def _collapse_pass_lines(self, lines: list[str], mode: str = "generic") -> list[str]:
        """Replace consecutive PASS/OK lines with a summary count."""
        if mode == "test":
            return self._collapse_test_pass_lines(lines)

        result = []
        pass_count = 0
        pass_run_start = -1

        for i, line in enumerate(lines):
            if _PASS_RE.search(line) and not _ERROR_RE.search(line):
                if pass_count == 0:
                    pass_run_start = i
                pass_count += 1
            else:
                if pass_count > 3:
                    result.append(f"[{pass_count} tests passed]")
                elif pass_count > 0:
                    # Keep small groups as-is
                    result.extend(lines[pass_run_start:pass_run_start + pass_count])
                pass_count = 0
                result.append(line)

        # Handle trailing pass lines
        if pass_count > 3:
            result.append(f"[{pass_count} tests passed]")
        elif pass_count > 0:
            result.extend(lines[pass_run_start:pass_run_start + pass_count])

        return result

    def _collapse_test_pass_lines(self, lines: list[str]) -> list[str]:
        """Replace large pytest-style pass output with grouped summaries."""
        result = []
        pending_passes = []

        def flush_pending():
            nonlocal pending_passes
            if not pending_passes:
                return
            if len(pending_passes) <= 3:
                result.extend(pending_passes)
            else:
                grouped = defaultdict(int)
                for line in pending_passes:
                    match = _PYTEST_PASS_RE.match(line.strip())
                    if match:
                        target = match.group("target")
                        key = target.split("::", 1)[0]
                    else:
                        key = "[misc]"
                    grouped[key] += 1
                summary_parts = [f"{count} in {name}" for name, count in sorted(grouped.items())]
                result.append(f"[{len(pending_passes)} tests passed] " + " | ".join(summary_parts[:6]))
                if len(summary_parts) > 6:
                    result.append(f"[{len(summary_parts) - 6} more test groups omitted]")
            pending_passes = []

        for line in lines:
            stripped = line.strip()
            if _PASS_RE.search(stripped) and not _ERROR_RE.search(stripped):
                pending_passes.append(line)
                continue
            flush_pending()
            result.append(line)

        flush_pending()
        return result

    def _collapse_repeats(self, lines: list[str]) -> list[str]:
        """Replace runs of identical lines with [line repeated xN]."""
        if not lines:
            return lines

        result = []
        prev = lines[0]
        count = 1

        for line in lines[1:]:
            stripped = line.strip()
            if stripped == prev.strip() and stripped:
                count += 1
            else:
                if count > 2:
                    result.append(prev)
                    result.append(f"[line repeated x{count}]")
                else:
                    result.extend([prev] * count)
                prev = line
                count = 1

        if count > 2:
            result.append(prev)
            result.append(f"[line repeated x{count}]")
        else:
            result.extend([prev] * count)

        return result

    def _collapse_global_repeats(self, lines: list[str], mode: str = "generic") -> list[str]:
        """Summarize noisy repeated lines even when they are not consecutive."""
        if not lines:
            return lines

        normalized = [line.strip() for line in lines if line.strip()]
        counts = Counter(normalized)
        thresholds = {"test": 2, "install": 2, "build": 3, "generic": 4}
        threshold = thresholds.get(mode, 4)
        noisy = {
            line for line, count in counts.items()
            if count >= threshold and not _ERROR_RE.search(line) and len(line) <= 180
        }
        if not noisy:
            return lines

        result = []
        emitted = set()
        for line in lines:
            stripped = line.strip()
            if stripped in noisy:
                if stripped in emitted:
                    continue
                emitted.add(stripped)
                result.append(line)
                result.append(f"[line repeated x{counts[stripped]} across output]")
            else:
                result.append(line)
        return result

    def _collapse_tracebacks(self, lines: list[str]) -> list[str]:
        """Condense traceback blocks to the signal-bearing lines."""
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if "Traceback" not in line:
                result.append(line)
                i += 1
                continue

            block = [line]
            i += 1
            while i < len(lines):
                current = lines[i]
                if not current.strip():
                    block.append(current)
                    i += 1
                    break
                if _ERROR_RE.search(current) or current.lstrip().startswith("File "):
                    block.append(current)
                    i += 1
                    continue
                if current.startswith("  ") or current.startswith("\t"):
                    block.append(current)
                    i += 1
                    continue
                break

            file_lines = [b.strip() for b in block if b.strip().startswith("File ")]
            tail = next((b.strip() for b in reversed(block) if b.strip() and "Traceback" not in b.strip()), "")
            result.append("[traceback]")
            if file_lines:
                result.append(file_lines[0])
                if len(file_lines) > 1:
                    result.append(f"[{len(file_lines) - 1} more stack frames]")
            if tail:
                result.append(tail)

        return result

    def _normalize_blanks(self, lines: list[str]) -> list[str]:
        """Collapse multiple consecutive blank lines to one."""
        result = []
        prev_blank = False
        for line in lines:
            is_blank = not line.strip()
            if is_blank:
                if not prev_blank:
                    result.append(line)
                prev_blank = True
            else:
                prev_blank = False
                result.append(line)
        return result

    # ── Pass 2: LLM summarization ────────────────────────

    def _summarize_signal_output(self, text: str, mode: str = "generic") -> str:
        """Shrink long output to signal-bearing lines while preserving failures."""
        lines = [line.rstrip() for line in text.splitlines()]
        nonblank = [line for line in lines if line.strip()]
        if len(nonblank) <= 12:
            return text

        max_lines = {"test": 5, "install": 6, "build": 6, "generic": 6}.get(mode, 6)
        kept: list[str] = []
        seen: set[str] = set()
        has_errors = any(_ERROR_RE.search(line) for line in nonblank)

        def add(line: str) -> None:
            stripped = line.strip()
            if not stripped or stripped in seen:
                return
            seen.add(stripped)
            kept.append(line)

        for line in nonblank[:3]:
            stripped = line.strip()
            if _PASS_RE.search(stripped) and not _WARNING_RE.search(stripped) and not _ERROR_RE.search(stripped):
                continue
            add(line)
            if len(kept) >= 2:
                break

        if has_errors:
            warning_lines = [line for line in nonblank if _WARNING_RE.search(line)]
            error_lines = [line for line in nonblank if _ERROR_RE.search(line)]
            repeat_lines = [line for line in nonblank if "[line repeated x" in line]
            final_summary = next((line for line in reversed(nonblank) if _SUCCESS_SUMMARY_RE.search(line)), "")
            for line in repeat_lines[-2:]:
                add(line)
            if warning_lines:
                add(f"[{len(warning_lines)} warning lines retained in mixed output]")
                for line in warning_lines[-2:]:
                    add(line)
            for line in error_lines[-max_lines:]:
                add(line)
            if final_summary:
                add(final_summary)
        else:
            summary_lines = [
                line for line in nonblank
                if _SUCCESS_SUMMARY_RE.search(line) or _WARNING_RE.search(line)
            ]
            for line in summary_lines[-max_lines:]:
                add(line)
        if not has_errors and not any(_SUCCESS_SUMMARY_RE.search(line) for line in kept):
            for line in nonblank[-2:]:
                add(line)

        omitted = len(nonblank) - len(kept)
        if omitted > 0:
            label = "non-error" if has_errors else "successful"
            kept.insert(min(2, len(kept)), f"[{omitted} {label} lines omitted]")

        return "\n".join(kept) if kept else text

    def _pass2(self, filtered_text: str, raw_text: str = "") -> str | None:
        """Use Ollama LLM to generate a 3-5 line summary of terminal output.

        Status-aware: on success, be very terse. On failure, preserve error details.
        """
        has_errors = bool(_ERROR_RE.search(filtered_text))

        if has_errors:
            system = (
                "You are a terminal output summarizer. The output contains errors or failures. "
                "Summarize in 3-5 lines. PRESERVE all error messages, file paths, and line numbers. "
                "Start with the failure count/type."
            )
        else:
            system = (
                "You are a terminal output summarizer. The output shows success. "
                "Summarize in 1-3 lines. Include: what ran, result counts, duration if shown. "
                "Be extremely terse."
            )

        # Smart truncation: keep head (context) and tail (usually contains the final error/summary)
        if len(filtered_text) > 4000:
            smart_context = filtered_text[:1000] + "\n... [TRUNCATED C3 FILTER] ...\n" + filtered_text[-3000:]
        else:
            smart_context = filtered_text

        prompt = f"Summarize this terminal output:\n\n{smart_context}"

        t0 = time.monotonic()
        result = self.ollama.generate(
            prompt=prompt,
            model=self.filter_model,
            system=system,
            temperature=0.1,
            max_tokens=200,
            timeout=self.pass2_timeout,
        )
        self._record_pass2_latency(time.monotonic() - t0)

        if result and count_tokens(result) < count_tokens(filtered_text):
            return f"[c3:filter:llm] {result.strip()}"
        return None

    # ── Pass-2 adaptive backoff ──────────────────────────

    def _pass2_is_suspended(self) -> bool:
        """True while pass-2 is on cooldown after consecutive slow calls."""
        with self._lock:
            return time.monotonic() < self._pass2_suspended_until

    def _pass2_suspension_note(self) -> str:
        """One-shot header note; emitted once per suspension window."""
        with self._lock:
            if self._pass2_suspend_noted:
                return ""
            self._pass2_suspend_noted = True
            return "[filter:fast] pass2 suspended, slow ollama"

    def _record_pass2_latency(self, elapsed: float) -> None:
        """Track a pass-2 call's wall latency and suspend pass-2 if the last
        ``pass2_latency_window`` calls all ran into the per-call timeout."""
        timed_out = elapsed >= self.pass2_timeout
        with self._lock:
            self.metrics["pass2_calls"] += 1
            if timed_out:
                self.metrics["pass2_timeouts"] += 1
            self._pass2_latencies.append(elapsed)
            window_full = len(self._pass2_latencies) == self.pass2_latency_window
            if window_full and all(lat >= self.pass2_timeout for lat in self._pass2_latencies):
                self._pass2_suspended_until = time.monotonic() + self.pass2_suspend_seconds
                self._pass2_suspend_noted = False
                self._pass2_latencies.clear()
                self.metrics["pass2_suspended"] += 1
