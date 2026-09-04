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


def _take_lines(lines: list[tuple[int, str]], limit: int, *, from_end: bool,
                focus, output_id, number_lines: bool = True) -> tuple[list[str], int]:
    """Render lines (with clipping) until ``limit`` bytes are used.

    Returns (rendered lines in reading order, number of logical lines kept).
    """
    out: list[str] = []
    used = 0
    kept = 0
    seq = reversed(lines) if from_end else lines
    for lineno, line in seq:
        rendered = clip_line(line, lineno=lineno if number_lines else None,
                             focus=focus, output_id=output_id)
        cost = sum(_byte_len(r) + 1 for r in rendered)
        if used + cost > limit and kept > 0:
            break
        if used + cost > limit and kept == 0:
            # Even one line does not fit: keep it anyway (the note is what
            # the reader needs) unless the budget is absurdly small.
            if limit <= 0:
                break
        out.append("\n".join(rendered))
        used += cost
        kept += 1
    if from_end:
        out.reverse()
    return out, kept


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
                 number_lines: bool = True) -> tuple[str, dict]:
    """Fit one stream into ``alloc`` bytes.

    Either ``full_text`` (the whole stream) or ``head``/``tail`` previews (a
    stream too large to hold) is given. Under budget the text passes through
    untouched, long lines included. Over budget: lines are clipped, then a
    HEAD_SHARE / (1 - HEAD_SHARE) window of the allocation keeps the first and
    last lines and an omission note stands in for the middle.

    Returns ``(rendered, info)`` where info = {cut, omitted_lines,
    omitted_bytes, clipped_lines, rendered_bytes}.
    """
    info = {"cut": False, "omitted_lines": 0, "omitted_bytes": 0,
            "clipped_lines": 0, "rendered_bytes": 0}
    if full_text is not None and _byte_len(full_text) <= alloc:
        info["rendered_bytes"] = _byte_len(full_text)
        return full_text, info

    if full_text is not None:
        raw_lines = full_text.split("\n")
        if raw_lines and raw_lines[-1] == "":
            raw_lines.pop()
        numbered = list(enumerate(raw_lines, 1))
        head_lines = numbered
        tail_lines = numbered
        n_total = len(numbered)
    else:
        h = head.split("\n")
        if h and h[-1] == "" and head.endswith("\n"):
            h.pop()
        t = tail.split("\n")
        if t and t[-1] == "" and tail.endswith("\n"):
            t.pop()
        # The tail preview starts mid-line more often than not; the first
        # partial piece is dropped so every kept line is a whole line.
        if t and not tail.startswith("\n") and len(t) > 1:
            t = t[1:]
        n_total = max(total_lines, 1)
        head_lines = list(enumerate(h, 1))
        first_tail_no = max(1, n_total - len(t) + 1)
        tail_lines = list(enumerate(t, first_tail_no))

    note_reserve = 240
    usable = max(0, alloc - note_reserve)
    head_budget = int(usable * HEAD_SHARE)
    tail_budget = usable - head_budget

    head_out, head_kept = _take_lines(head_lines, head_budget, from_end=False,
                                      focus=focus, output_id=output_id,
                                      number_lines=number_lines)
    # Everything the head already covers is off limits to the tail.
    remaining_tail = [pair for pair in tail_lines if pair[0] > head_kept]
    tail_out, tail_kept = _take_lines(remaining_tail, tail_budget, from_end=True,
                                      focus=focus, output_id=output_id,
                                      number_lines=number_lines)

    kept_total = head_kept + tail_kept
    omitted_lines = max(0, n_total - kept_total)
    info["clipped_lines"] = sum(1 for chunk in head_out + tail_out if "\n" in chunk)
    if omitted_lines == 0 and (full_text is not None):
        rendered = "\n".join(head_out + tail_out)
        info["cut"] = info["clipped_lines"] > 0
        info["rendered_bytes"] = _byte_len(rendered)
        return rendered, info

    rendered_bytes_kept = sum(_byte_len(c) + 1 for c in head_out + tail_out)
    omitted_bytes = max(0, total_bytes - rendered_bytes_kept)
    note = _omission_note(omitted_lines, omitted_bytes, output_id, focus)
    rendered = "\n".join(head_out + [note] + tail_out)
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
