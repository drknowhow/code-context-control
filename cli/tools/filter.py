"""c3_filter — Terminal/log noise reduction with 3-depth pipeline.

Depths:
  fast  — regex only (pass-1)
  smart — regex + heuristic collapsing (pass-1 + pass-1.5)  [default]
  deep  — regex + heuristics + LLM summarization (pass-1 + pass-1.5 + pass-2)
"""

import json
import re
from pathlib import Path

from cli.tools._helpers import finalize_with_tokens, show_token_ratios
from core import count_tokens


def handle_filter(file_path: str, text: str, pattern: str, max_lines: int,
                  depth: str, use_llm: bool, svc, finalize) -> str:
    # Backward compat: use_llm=True maps to "deep", use_llm=False maps to "smart"
    if depth == "smart" and use_llm is False:
        depth = "fast"
    elif depth == "smart" and use_llm is True:
        pass  # keep smart as default

    # Text mode
    if text and not file_path:
        return _filter_text(text, depth, svc, finalize)

    # File mode
    full = Path(svc.project_path) / file_path
    if not full.exists():
        full = Path(file_path)
    if not full.exists():
        return finalize("c3_filter", {"file": file_path}, "[filter:error] not found", "error")

    return _filter_file(full, file_path, pattern, max_lines, svc, finalize)


def _filter_text(text: str, depth: str, svc, finalize) -> str:
    """Filter terminal output with configurable depth."""
    if depth == "fast":
        # Pass-1 only (regex)
        res = svc.output_filter.filter(text, use_llm=False)
        # Skip pass-1.5 heuristics
        method = "pass1"
        result_text = res['filtered']
    elif depth == "deep":
        # Full pipeline including LLM
        res = svc.output_filter.filter(text, use_llm=True)
        method = f"pass{res['pass_used']}" + ("+llm" if res['llm_used'] else "")
        result_text = res['filtered']
    else:
        # "smart" — pass-1 + heuristic pass-1.5 (no LLM)
        res = svc.output_filter.filter(text, use_llm=False)
        method = "pass1"
        result_text = res['filtered']

        # Pass-1.5: heuristic collapsing
        enhanced = _heuristic_collapse(result_text)
        if enhanced and count_tokens(enhanced) < count_tokens(result_text):
            result_text = enhanced
            method = "pass1.5"

    filtered_tokens = count_tokens(result_text)
    raw_tokens = res['raw_tokens']

    # The method tag is actionable signal (which pass ran); the token ratio is
    # boilerplate — shown only under the show_token_ratios debug flag. The
    # (raw, filtered) pair still flows to accounting structurally below.
    header = f"[filter:{method}]"
    if show_token_ratios(svc):
        savings_pct = round((1 - filtered_tokens / raw_tokens) * 100, 1) if raw_tokens > 0 else 0
        header = f"[filter:{method}] {raw_tokens}→{filtered_tokens}tok ({savings_pct}%saved)"
    resp = f"{header}\n{result_text}"
    return finalize_with_tokens(
        finalize, svc, "c3_filter", {"depth": depth}, resp, method,
        raw_tokens=raw_tokens, optimized_tokens=filtered_tokens,
        response_tokens=filtered_tokens)


def _heuristic_collapse(text: str) -> str | None:
    """Pass-1.5: Pattern-based collapsing without LLM.

    - Collapse repeated similar lines (e.g., download progress, compilation units)
    - Deduplicate stack traces
    - Smart truncation: preserve first error + last N lines
    - Summarize test pass/fail counts
    """
    lines = text.splitlines()
    if len(lines) <= 15:
        return None  # Too short to benefit

    result = []
    # Track patterns for collapsing
    _download_re = re.compile(
        r'^\s*(downloading|fetching|resolving|compiling|building|installing)\s+',
        re.IGNORECASE)
    _test_result_re = re.compile(
        r'^\s*(PASS|PASSED|OK|FAIL|FAILED|ERROR|SKIP)\s+', re.IGNORECASE)
    _repeated_traceback_re = re.compile(r'^\s*File\s+"')

    # Group consecutive similar lines
    i = 0
    while i < len(lines):
        line = lines[i]

        # Collapse consecutive download/compile lines
        if _download_re.match(line):
            group = [line]
            j = i + 1
            while j < len(lines) and _download_re.match(lines[j]):
                group.append(lines[j])
                j += 1
            if len(group) > 3:
                result.append(group[0])
                result.append(f"[{len(group) - 1} similar lines collapsed]")
            else:
                result.extend(group)
            i = j
            continue

        # Collapse repeated "File" lines in tracebacks (keep first + last)
        if _repeated_traceback_re.match(line):
            group = [line]
            j = i + 1
            while j < len(lines) and (_repeated_traceback_re.match(lines[j])
                                       or lines[j].startswith("    ")):
                group.append(lines[j])
                j += 1
            if len(group) > 6:
                result.extend(group[:2])
                result.append(f"[{len(group) - 4} stack frames collapsed]")
                result.extend(group[-2:])
            else:
                result.extend(group)
            i = j
            continue

        result.append(line)
        i += 1

    # Smart truncation: if still too long, keep first error region + last 20 lines
    error_re = re.compile(
        r'ERROR|FAIL|FAILED|Exception|Traceback|panic|CRITICAL',
        re.IGNORECASE)

    if len(result) > 80:
        # Find first error
        first_error_idx = None
        for idx, line in enumerate(result):
            if error_re.search(line):
                first_error_idx = idx
                break

        if first_error_idx is not None:
            # Keep context around first error + tail
            error_region = result[max(0, first_error_idx - 2):first_error_idx + 10]
            tail = result[-20:]
            omitted = len(result) - len(error_region) - len(tail)
            if omitted > 5:
                result = (error_region
                          + [f"[{omitted} lines omitted]"]
                          + tail)
        else:
            # No errors — keep head + tail
            head = result[:10]
            tail = result[-20:]
            omitted = len(result) - 30
            if omitted > 5:
                result = head + [f"[{omitted} lines omitted]"] + tail

    collapsed = "\n".join(result)
    return collapsed


def _filter_file(full: Path, file_path: str, pattern: str, max_lines: int,
                 svc, finalize) -> str:
    text = full.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    orig_tok = count_tokens(text)
    ext = full.suffix.lower()
    extracted = ""

    if pattern:
        try:
            pat = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"[extract:error] invalid regex: {e}"
        matched = []
        for i, line in enumerate(lines):
            if pat.search(line):
                for j in range(max(0, i - 2), min(len(lines), i + 5)):
                    marker = ">" if j == i else " "
                    entry = f"{marker}L{j+1}: {lines[j][:200]}"
                    if entry not in matched:
                        matched.append(entry)
                if matched and matched[-1] != "---":
                    matched.append("---")
                if len(matched) >= max_lines:
                    break
        extracted = f"[grep:{pattern}] {len(matched)} lines\n" + "\n".join(matched[:max_lines])
    elif ext in ('.log', '.txt'):
        error_keywords = ['error', 'exception', 'traceback', 'fatal', 'critical', 'fail', 'warn']
        errs = {k.upper(): 0 for k in ['ERROR', 'WARN', 'Exception', 'Traceback', 'FATAL', 'CRITICAL']}
        error_line_indices = []
        for i, line in enumerate(lines):
            ll = line.lower()
            for k in errs:
                if k.lower() in ll:
                    errs[k] += 1
            if any(kw in ll for kw in error_keywords):
                error_line_indices.append(i)
        freq = " | ".join(f"{k}:{v}" for k, v in errs.items() if v > 0)
        header = f"[log] {len(lines)} lines | {freq or 'no errors'} | {len(error_line_indices)} error lines"
        if error_line_indices:
            emitted = set()
            context_parts = []
            budget = max_lines - 2
            for idx in error_line_indices:
                for j in range(max(0, idx - 3), min(len(lines), idx + 4)):
                    if j not in emitted:
                        emitted.add(j)
                        marker = ">" if j == idx else " "
                        context_parts.append(f"{marker}L{j+1}: {lines[j][:200]}")
                context_parts.append("---")
                if len(context_parts) >= budget:
                    break
            extracted = header + "\n" + "\n".join(context_parts[:max_lines])
        else:
            extracted = header + "\n" + "\n".join(lines[:max_lines])
    elif ext in ('.jsonl', '.ndjson'):
        records = 0
        schema_keys = set()
        error_records = []
        sample_lines = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            records += 1
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    schema_keys.update(obj.keys())
                    level_val = str(obj.get("level", obj.get("severity",
                                    obj.get("log_level", "")))).lower()
                    msg = str(obj.get("message", obj.get("msg",
                              obj.get("error", "")))).lower()
                    if any(kw in level_val or kw in msg
                           for kw in ("error", "fatal", "exception", "traceback")):
                        if len(error_records) < 10:
                            error_records.append(f"L{i+1}: {stripped[:200]}")
                    if len(sample_lines) < 3:
                        sample_lines.append(f"L{i+1}: {stripped[:200]}")
            except Exception:
                if len(sample_lines) < 3:
                    sample_lines.append(f"L{i+1}: {stripped[:200]}")
        schema_str = ", ".join(sorted(schema_keys)[:20]) if schema_keys else "unknown"
        header = f"[jsonl] {records} records | keys: [{schema_str}] | {len(error_records)} errors"
        parts = [header, "--- sample ---"] + sample_lines
        if error_records:
            parts += ["--- errors ---"] + error_records
        extracted = "\n".join(parts)
    elif ext in ('.csv', '.tsv'):
        sep = "\t" if ext == '.tsv' else ","
        if lines:
            header_line = lines[0]
            columns = [c.strip().strip('"') for c in header_line.split(sep)]
            data_lines = len(lines) - 1
            null_counts = {c: 0 for c in columns}
            for row in lines[1:min(101, len(lines))]:
                cells = row.split(sep)
                for j, col in enumerate(columns):
                    if j < len(cells) and not cells[j].strip():
                        null_counts[col] += 1
            sample_size = min(100, data_lines)
            col_info = " | ".join(
                f"{c}{'('+str(round(null_counts[c]/sample_size*100))+'% null)' if sample_size and null_counts[c] else ''}"
                for c in columns[:15]
            )
            header_str = f"[csv] {data_lines} rows, {len(columns)} cols | {col_info}"
            extracted = header_str + "\n" + "\n".join(lines[:min(max_lines, 20)])
        else:
            extracted = "[csv] empty file"
    else:
        extracted = "\n".join(lines[:max_lines])

    res_tok = count_tokens(extracted)
    header = f"[extract:{ext}]"
    if show_token_ratios(svc):
        saved = round((1 - res_tok / orig_tok) * 100) if orig_tok > 0 else 0
        header = f"[extract:{ext}] {orig_tok}->{res_tok}tok ({saved}% saved)"
    return finalize_with_tokens(
        finalize, svc, "c3_filter", {"file": file_path, "pattern": pattern},
        f"{header}\n{extracted}", "extract",
        raw_tokens=orig_tok, optimized_tokens=res_tok,
        response_tokens=res_tok)
