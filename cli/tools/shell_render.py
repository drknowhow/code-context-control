"""Response budget for c3_shell (shell remediation S1).

Why
---
Measured 2026-09-04 across 59 projects: 21 c3_shell calls a month returned
more than 25k tokens and carried 55% of the tool's whole token volume; the
client discards any MCP result over MAX_MCP_OUTPUT_TOKENS (25k), so every one
of them came back as an error and was re-run. They were all grep/sed/find
over minified bundles or JSONL logs: two or three lines, hundreds of KB each,
invisible to a filter that triggers on newline count.

What
----
One combined BYTE budget on the final rendered response — headers, command
line, stdout, stderr, hints — enforced always (``filter_output=False`` never
lifts it). Defaults per the Cod review of 2026-09-04: 18 KiB, ceiling 22 KiB,
a per-call override may only lower it; 2 KiB reserved for the envelope;
stderr guaranteed 40% of the remainder when non-empty, unused space
redistributed; 35% head / 65% tail within a stream; logical lines clipped at
512 chars keeping prefix + suffix (or a window around the grep match when the
command is a grep). Bytes are enforced, tokens are reported: token counting
is model-specific.

A clipped line renders as TWO lines — a bracketed note on its own line, then
the fragment — so a note never shares a line with the content it interrupts
(a marker inside a JSON line makes the line unparseable; docs/shell-eval.md
``no_marker_inside``). Every omission note names the output id the full bytes
can be paged from (services/shell_output.py), or says the output was not
spilled when there is nothing to page.

S2 (content-aware keep, cli/tools/shell_parsers.py): streams are normalised
before shaping — ANSI stripped, ``\\r`` rewrites and duplicate runs
collapsed — and ``shape_stream`` keeps the parser's priority regions (a
failing test's block, a compiler error, the totals) before it spends what
is left on head and tail. Under budget nothing is omitted.

Pure functions only; ``render_shell_response`` in cli/tools/shell.py calls
them. The shell-eval harness grades the result.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex

BUDGET_DEFAULT = 18 * 1024
BUDGET_CEILING = 22 * 1024
BUDGET_FLOOR = 2 * 1024            # below this a response cannot carry its own envelope
META_RESERVE = 2 * 1024
STDERR_SHARE = 0.40
HEAD_SHARE = 0.35
LINE_CLIP = 512
CLIP_PREFIX = 384
CLIP_SUFFIX = 128
CMD_DISPLAY_MAX = 240
_TOKEN_BYTES = 3                   # conservative bytes-per-token when a token limit is the bound

_GREP_HEAD = re.compile(r"(?:^|[;&|(]\s*)(?:\S*/)?(?:e?grep|rg)\s+")
_SHELL_OPERATORS = {"|", "||", "&&", ";", ">", ">>", "<", "2>", "2>&1", "&"}
_GREP_FLAG_WITH_ARG = {"-e", "--regexp", "-f", "--file", "-A", "-B", "-C", "-m", "-d",
                       "--include", "--exclude", "--exclude-dir", "-t", "--type", "-g", "--glob"}


# ── Budget ──────────────────────────────────────────────────────────────────

def effective_budget(requested=None, *, config_default=None, env=None) -> int:
    """The byte budget for one response.

    ``requested`` (a per-call ``max_bytes``) and ``config_default``
    (``hybrid.shell_budget_bytes`` in .c3/config.json) may only LOWER the
    ceiling; the client's MAX_MCP_OUTPUT_TOKENS, when set, bounds it too
    (tokens x 3 bytes, conservative). Never below BUDGET_FLOOR.
    """
    env = os.environ if env is None else env
    budget = BUDGET_DEFAULT
    for candidate in (config_default, requested):
        try:
            value = int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            budget = min(budget, value)
    raw = (env.get("MAX_MCP_OUTPUT_TOKENS") or "").strip()
    if raw:
        try:
            tokens = int(float(raw))
            if tokens > 0:
                budget = min(budget, tokens * _TOKEN_BYTES)
        except (TypeError, ValueError):
            pass
    return max(BUDGET_FLOOR, min(budget, BUDGET_CEILING))


def allocate(budget: int, out_bytes: int, err_bytes: int) -> tuple[int, int]:
    """Split ``budget - META_RESERVE`` between stdout and stderr.

    stderr, when present, is guaranteed STDERR_SHARE of the body; whatever a
    stream does not need goes to the other one.
    """
    body = max(0, budget - META_RESERVE)
    if err_bytes <= 0:
        return body, 0
    if out_bytes <= 0:
        return 0, body
    err_guarantee = int(body * STDERR_SHARE)
    err_alloc = min(err_bytes, err_guarantee)
    out_alloc = min(out_bytes, body - err_alloc)
    spare = body - err_alloc - out_alloc
    if spare > 0 and err_bytes > err_alloc:
        extra = min(spare, err_bytes - err_alloc)
        err_alloc += extra
        spare -= extra
    if spare > 0 and out_bytes > out_alloc:
        out_alloc += min(spare, out_bytes - out_alloc)
    return out_alloc, err_alloc


# ── Envelope ────────────────────────────────────────────────────────────────

def cmd_display(cmd: str) -> str:
    """The command as echoed: its first logical line, capped at 240 chars.

    A multi-line or over-long command carries ``(N lines, M chars, sha256:x)``
    instead of its body — the caller already owns the text it submitted, and a
    60-line heredoc echoed back was paid for twice (measured ~6% of in-context
    shell tokens). The display form is captured BEFORE credential expansion.
    """
    text = cmd or ""
    lines = text.splitlines() or [""]
    first = lines[0].rstrip()
    n_lines = len([ln for ln in lines if ln.strip()]) or 1
    if n_lines == 1 and len(first) <= CMD_DISPLAY_MAX:
        return first
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]
    head = first if len(first) <= CMD_DISPLAY_MAX else first[:CMD_DISPLAY_MAX - 1] + "…"
    return f"{head} ({n_lines} lines, {len(text)} chars, sha256:{digest})"


def grep_pattern(cmd: str) -> str | None:
    """Best-effort: the pattern a grep/rg in ``cmd`` searches for, else None.

    Used only to centre a clipped line on its match. Never to rewrite the
    command.
    """
    m = _GREP_HEAD.search(cmd or "")
    if not m:
        return None
    rest = (cmd or "")[m.end():]
    try:
        args = shlex.split(rest, posix=True)
    except ValueError:
        args = rest.split()
    skip_next = False
    for arg in args:
        if arg in _SHELL_OPERATORS:
            break
        if skip_next:
            skip_next = False
            if arg and not arg.startswith("-"):
                return arg  # the argument of -e / --regexp
            continue
        if arg in ("-e", "--regexp"):
            skip_next = True
            continue
        if arg.startswith("--regexp="):
            return arg.split("=", 1)[1] or None
        if arg in _GREP_FLAG_WITH_ARG:
            skip_next = True
            continue
        if arg.startswith("-") and len(arg) > 1:
            continue
        return arg or None
    return None


# ── Line clipping ───────────────────────────────────────────────────────────

def _find_focus(line: str, focus) -> int:
    if not focus:
        return -1
    if isinstance(focus, re.Pattern):
        m = focus.search(line)
        return m.start() if m else -1
    idx = line.find(str(focus))
    if idx < 0:
        try:
            m = re.search(str(focus), line)
            idx = m.start() if m else -1
        except re.error:
            idx = -1
    return idx


def clip_line(line: str, *, lineno: int | None = None, focus=None,
              output_id: str | None = None) -> list[str]:
    """Return the rendered lines for one logical line.

    Short lines pass through unchanged (one element). A line longer than
    LINE_CLIP renders as a note line followed by the fragment: prefix +
    suffix by default, or a window around the first ``focus`` match (a grep
    pattern) when one is found. The note carries the line number, the full
    length, and where the whole line lives.
    """
    if len(line) <= LINE_CLIP:
        return [line]
    at = _find_focus(line, focus)
    if at >= 0:
        start = max(0, at - CLIP_PREFIX)
        end = min(len(line), at + CLIP_SUFFIX)
        # keep the window CLIP_PREFIX + CLIP_SUFFIX wide when the match sits near an edge
        if end - start < CLIP_PREFIX + CLIP_SUFFIX:
            if start == 0:
                end = min(len(line), CLIP_PREFIX + CLIP_SUFFIX)
            else:
                start = max(0, end - (CLIP_PREFIX + CLIP_SUFFIX))
        fragment = ("…" if start > 0 else "") + line[start:end] + ("…" if end < len(line) else "")
        how = "around the match"
    else:
        fragment = line[:CLIP_PREFIX] + "…" + line[-CLIP_SUFFIX:]
        how = "prefix and suffix"
    where = f"; full line via output_id {output_id}" if output_id else "; not spilled"
    label = f"L{lineno}" if lineno is not None else "line"
    note = f"[{label} clipped: {len(line)} chars, {CLIP_PREFIX + CLIP_SUFFIX} shown {how}{where}]"
    return [note, fragment]


# ── Stream shaping ──────────────────────────────────────────────────────────

def _byte_len(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


def _fill(entries: list[tuple[int, str]], order, limit: int, kept: dict[int, str], *,
          focus, output_id, number_lines: bool = True, allow_one: bool = True) -> int:
    """Render entries in ``order`` (indices) into ``kept`` until ``limit`` bytes
    are used. Already-kept indices cost nothing and are skipped. Returns the
    bytes used. ``allow_one``: even one line that does not fit is kept (the
    note is what the reader needs) unless the limit is absurdly small."""
    used = 0
    took = 0
    for idx in order:
        if idx in kept:
            continue
        lineno, line = entries[idx]
        rendered = clip_line(line, lineno=lineno if number_lines else None,
                             focus=focus, output_id=output_id)
        cost = sum(_byte_len(r) + 1 for r in rendered)
        if used + cost > limit:
            if took > 0 or not allow_one or limit <= 0:
                break
        kept[idx] = "\n".join(rendered)
        used += cost
        took += 1
    return used


def split_lines(text: str) -> list[str]:
    """Logical lines of a stream: no trailing empty element for a final newline."""
    lines = text.split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    return lines


def split_preview(head: str, tail: str) -> tuple[list[str], list[str], bool]:
    """Lines of the head and tail previews of a stream too large to hold.

    The tail preview starts mid-line more often than not; its first partial
    piece is dropped so every kept line is a whole line — the third element
    says whether that happened, so a caller mapping line numbers can shift
    its tail map by one. Parsers and ``shape_stream`` both use this, so a
    region index computed over ``head_lines + tail_lines`` is the index the
    shaper sees.
    """
    h = split_lines(head)
    t = split_lines(tail)
    torn = bool(t) and not tail.startswith("\n") and len(t) > 1
    if torn:
        t = t[1:]
    return h, t, torn


def _region_note(a: int, b: int, why: str) -> str:
    label = f"L{a}" if a == b else f"L{a}-{b}"
    return f"[{label}: {why}]"


def _merge_regions(priority, n: int) -> list[tuple[int, int, str]]:
    """Clamp, drop empties, and trim each region to the lines not already
    covered by an earlier (more important) one — order is kept."""
    out: list[tuple[int, int, str]] = []
    covered: set[int] = set()
    for a, b, why in priority or ():
        a, b = max(0, int(a)), min(n - 1, int(b))
        if a > b:
            continue
        idx = [i for i in range(a, b + 1) if i not in covered]
        if not idx:
            continue
        covered.update(idx)
        out.append((idx[0], idx[-1], str(why)))
    return out


def _omission_note(omitted_lines: int, omitted_bytes: int, output_id: str | None,
                   focus) -> str:
    if output_id:
        hint = (f"c3_shell(output_id='{output_id}', output_action='search', pattern='…') "
                f"or output_action='read', lines='a-b'")
        return (f"[… {omitted_lines} lines / {omitted_bytes} bytes omitted; full output via "
                f"{hint} …]")
    return (f"[… {omitted_lines} lines / {omitted_bytes} bytes omitted; not spilled, "
            f"re-run with a narrower command …]")


def shape_stream(*, full_text: str | None = None, head: str = "", tail: str = "",
                 total_bytes: int, total_lines: int, alloc: int,
                 output_id: str | None = None, focus=None,
                 number_lines: bool = True, priority=None, priority_share: float = 1.0,
                 line_numbers: list[int] | None = None,
                 tail_line_numbers: list[int] | None = None) -> tuple[str, dict]:
    """Fit one stream into ``alloc`` bytes.

    Either ``full_text`` (the whole stream) or ``head``/``tail`` previews (a
    stream too large to hold) is given. Under budget the text passes through
    untouched, long lines included. Over budget: lines are clipped; then
    ``priority`` regions — 0-based inclusive ``(start, end, why)`` ranges,
    most important first (cli/tools/shell_parsers.py) — are kept FIRST, each
    prefixed by a one-line ``[La-b: why]`` note when it is not contiguous
    with what was already kept; then the remaining allocation keeps the
    first and last lines HEAD_SHARE / (1 - HEAD_SHARE) as before, and one
    omission note names how many lines and bytes are missing and the output
    id they can be paged from. In the previews path, region indices refer to
    the head lines followed by the tail lines (``split_preview``).
    ``priority_share`` caps what regions may take of the allocation (the
    renderer passes 0.6 for generic error anchors, so head and tail always
    keep something; a recognised runner's regions may take it all).

    ``line_numbers`` (and ``tail_line_numbers`` for the tail preview) map
    each line to its number in the raw stream when normalisation folded
    lines away (S2), so every ``L…`` in a note is a number ``output_action=
    'read'`` understands.

    Returns ``(rendered, info)`` where info = {cut, omitted_lines,
    omitted_bytes, clipped_lines, rendered_bytes, priority_kept}.
    """
    info = {"cut": False, "omitted_lines": 0, "omitted_bytes": 0,
            "clipped_lines": 0, "rendered_bytes": 0, "priority_kept": 0}
    if full_text is not None and _byte_len(full_text) <= alloc:
        info["rendered_bytes"] = _byte_len(full_text)
        return full_text, info

    gap_after: int | None = None          # previews: the hole between head and tail
    if full_text is not None:
        raw_lines = split_lines(full_text)
        numbers = list(line_numbers) if line_numbers and len(line_numbers) == len(raw_lines) \
            else list(range(1, len(raw_lines) + 1))
        entries = list(zip(numbers, raw_lines))
        n_total = len(entries)
    else:
        h, t, torn = split_preview(head, tail)
        t_numbers = list(tail_line_numbers) if tail_line_numbers and len(tail_line_numbers) == len(t) + int(torn) \
            else list(range(1, len(t) + 1 + int(torn)))
        if torn:
            t_numbers = t_numbers[1:]
        n_total = max(total_lines, 1)
        h_numbers = list(line_numbers) if line_numbers and len(line_numbers) == len(h) \
            else list(range(1, len(h) + 1))
        first_tail_no = max(1, n_total - len(t) + 1)
        entries = list(zip(h_numbers, h)) + [(first_tail_no + (no - 1), line) for no, line in zip(t_numbers, t)]
        gap_after = len(h) - 1 if h and t else None

    n = len(entries)
    note_reserve = 240
    usable = max(0, alloc - note_reserve)
    kept: dict[int, str] = {}
    used = 0

    # 1. Priority regions, most important first; a region that does not fit
    #    whole is skipped so a smaller one further down the list still can.
    regions = _merge_regions(priority, n)
    region_at: dict[int, tuple[int, int, str]] = {}
    prio_limit = int(usable * max(0.0, min(1.0, priority_share)))
    for a, b, why in regions:
        note_cost = _byte_len(_region_note(entries[a][0], entries[b][0], why)) + 1
        trial: dict[int, str] = {}
        cost = _fill(entries, range(a, b + 1), prio_limit, trial, focus=focus, output_id=output_id,
                     number_lines=number_lines, allow_one=False)
        if len(trial) != b - a + 1 or used + cost + note_cost > prio_limit:
            continue
        kept.update(trial)
        used += cost + note_cost
        region_at[a] = (entries[a][0], entries[b][0], why)
    info["priority_kept"] = len(kept)

    # 2. Head / tail on what is left.
    remaining = max(0, usable - used)
    head_budget = int(remaining * HEAD_SHARE)
    tail_budget = remaining - head_budget
    _fill(entries, range(n), head_budget, kept, focus=focus, output_id=output_id,
          number_lines=number_lines, allow_one=not kept)
    _fill(entries, range(n - 1, -1, -1), tail_budget, kept, focus=focus, output_id=output_id,
          number_lines=number_lines, allow_one=not kept)

    def _render(kept_now: dict[int, str]) -> tuple[str, int, int]:
        order = sorted(kept_now)
        omitted = max(0, n_total - len(order))
        kept_bytes = sum(_byte_len(kept_now[i]) + 1 for i in order)
        omitted_b = max(0, total_bytes - kept_bytes)
        chunks: list[str] = []
        prev = -1
        total_note_done = False
        for idx in order:
            is_gap = idx != prev + 1 or (gap_after is not None and prev == gap_after)
            if is_gap and omitted > 0:
                if not total_note_done:
                    chunks.append(_omission_note(omitted, omitted_b, output_id, focus))
                    total_note_done = True
                elif idx not in region_at:
                    lo = entries[prev][0] + 1 if prev >= 0 else 1
                    hi = entries[idx][0] - 1
                    chunks.append(f"[… L{lo}-L{hi} omitted …]" if hi > lo else f"[… L{lo} omitted …]")
            if is_gap and idx in region_at:
                chunks.append(_region_note(*region_at[idx]))
            chunks.append(kept_now[idx])
            prev = idx
        if omitted > 0 and not total_note_done:
            chunks.append(_omission_note(omitted, omitted_b, output_id, focus))
        return "\n".join(chunks), omitted, omitted_b

    rendered, omitted_lines, omitted_bytes = _render(kept)
    # Notes were reserved for approximately; if the stream still overflows,
    # thin the head window from its end, then the tail window from its
    # start — never a priority line.
    prio = {i for a, b, _ in regions for i in range(a, b + 1)}
    while _byte_len(rendered) > alloc:
        head_run: list[int] = []
        i = 0
        while i in kept:
            head_run.append(i)
            i += 1
        tail_run: list[int] = []
        i = n - 1
        while i in kept and i not in head_run:
            tail_run.append(i)
            i -= 1
        head_cands = [i for i in head_run if i not in prio]
        tail_cands = [i for i in tail_run if i not in prio]
        if head_cands:
            victim = head_cands[-1]
        elif tail_cands:
            victim = tail_cands[-1]
        else:
            break
        kept.pop(victim)
        rendered, omitted_lines, omitted_bytes = _render(kept)

    info["clipped_lines"] = sum(1 for chunk in kept.values() if "\n" in chunk)
    if omitted_lines == 0 and full_text is not None:
        info["cut"] = info["clipped_lines"] > 0
        info["rendered_bytes"] = _byte_len(rendered)
        return rendered, info
    info.update(cut=True, omitted_lines=omitted_lines, omitted_bytes=omitted_bytes,
                rendered_bytes=_byte_len(rendered))
    return rendered, info


def human_bytes(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
