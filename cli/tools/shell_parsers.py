"""Content-aware shaping for c3_shell (shell remediation S2).

Why
---
S1 gave the response a byte budget and made what it drops recoverable. What
it kept was still chosen by position: a head, a tail, an omission note in
between. That is wrong for the outputs an agent runs most — test and build
runs — where the lines that matter (the failing test's id, its assertion,
the summary) sit anywhere, and for the outputs that are not big at all but
noisy: a progress bar rewritten 2,000 times with ``\\r``, colour escapes,
the same log line 200 times.

What (the Cod review of 2026-09-04, verbatim)
--------------------------------------------
"Always strip ANSI/control sequences and collapse carriage-return progress
updates. Otherwise preserve complete under-budget output. Run deterministic
parsers always to identify priority regions, but only omit content when
over budget. Start with pytest/unittest, cargo/rustc, tsc, and Jest/Vitest;
everything else uses generic error anchors plus head/tail. Collapse only
consecutive normalized duplicates. Drain3 is unjustified."

Three pure functions, stdlib only, called by ``render_shell_response``:

``normalize_stream``
    ``\\r\\n`` becomes ``\\n`` first (Windows), then ANSI escape and control
    sequences are stripped (never a loss: nothing an agent reads is gone),
    then every logical line keeps only the final state of its ``\\r``
    rewrites, then runs of three or more consecutive duplicate lines
    collapse to the first plus `` [x N]``. The last two ARE a loss and the
    caller marks the stream cut / spilled. Duplicates are exact by default;
    ``fuzzy_dups=True`` compares lines with digits, hex runs and timestamps
    replaced by a placeholder — the renderer asks for that only once a
    stream is over budget, so under-budget output stays complete.

``detect_runner`` / ``priority_regions`` / ``structured_tail``
    Recognise pytest, unittest, cargo/rustc, tsc, jest and vitest from the
    command head and the output's own signatures; name the 0-based inclusive
    line ranges that must survive shaping, most important first; and build a
    compact ``--- summary ---`` section (totals + one line per failing test,
    capped at 20) that is appended whenever a runner is recognised and the
    output is longer than 30 lines. Unrecognised output gets generic error
    anchors (Traceback / Error / FAIL / panic / exception / fatal) with two
    lines of context, capped at 40 anchors.

The parsers only *identify*; ``shape_stream`` in shell_render.py decides
what to keep, and it keeps everything when the stream fits.
"""
from __future__ import annotations

import re

__all__ = [
    "normalize_stream", "detect_runner", "priority_regions", "structured_tail",
    "RUNNERS", "DUP_MIN_RUN", "SUMMARY_MIN_LINES", "SUMMARY_FAIL_CAP",
    "GENERIC_ANCHOR_CAP", "GENERIC_CONTEXT",
]

RUNNERS = ("pytest", "unittest", "cargo", "tsc", "jest", "vitest")
TSC_ERRORS_HEAD = 20    # priority error lines kept from the front of a tsc wall
TSC_ERRORS_TAIL = 5     # and from the back; the totals line is always kept
DUP_MIN_RUN = 3               # never collapse fewer than three consecutive duplicates
SUMMARY_MIN_LINES = 30        # a structured tail is added past this many lines
SUMMARY_FAIL_CAP = 20         # failing tests / errors listed in the summary
BLOCK_CAP = 20                # failure blocks marked per runner
GENERIC_ANCHOR_CAP = 40
GENERIC_CONTEXT = 2
_SIG_HEAD = 4096              # bytes of each stream scanned for a runner signature
_SIG_TAIL = 8192


# ── Normalisation ───────────────────────────────────────────────────────────

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_ESC = re.compile(r"\x1b[ -/]*[0-~]")          # ESC + intermediates + final: charset selects, ESC 7 / ESC M / ESC c
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # keeps \t \n \r
_HEX_RUN = re.compile(r"\b(?:0x)?[0-9a-fA-F]{7,}\b")
_DIGITS = re.compile(r"\d+")


def _strip_ansi(text: str) -> tuple[str, int]:
    total = 0
    if "\x1b" in text:
        for rx in (_OSC, _CSI, _ESC):
            text, n = rx.subn("", text)
            total += n
    text, n = _CONTROL.subn("", text)
    return text, total + n


def _collapse_cr(line: str) -> tuple[str, int]:
    """The final state of a line rewritten with ``\\r``.

    A trailing ``\\r`` right before the newline leaves the previous content
    on screen, so the kept segment is the last NON-EMPTY one; the count is
    the number of non-empty states that were overwritten.
    """
    if "\r" not in line:
        return line, 0
    states = [s for s in line.split("\r") if s]
    if not states:
        return "", 0
    return states[-1], len(states) - 1


def _dup_key(line: str, fuzzy: bool) -> str:
    if not fuzzy:
        return line
    return _DIGITS.sub("#", _HEX_RUN.sub("#", line))


def normalize_stream(text: str | None, *, collapse: bool = True,
                     fuzzy_dups: bool = False) -> tuple[str, dict]:
    """Return ``(text, info)`` with control noise removed and rewrites collapsed.

    info = {ansi_stripped, cr_collapsed, dup_collapsed, line_numbers}.
    ``ansi_stripped`` counts escape sequences and control characters removed
    (not a loss). ``cr_collapsed`` counts overwritten progress states and
    ``dup_collapsed`` the lines folded into a `` [x N]`` — both are losses
    the caller must report and spill. ``line_numbers`` is the original
    1-based line number of every returned line, or None when nothing was
    folded (the numbering is then the identity).

    ``collapse=False`` (the tool's ``filter_output=False``) strips escapes and
    normalises line endings only. ``fuzzy_dups`` widens the duplicate test
    to lines that differ only in digits, hex runs or timestamps.
    """
    info = {"ansi_stripped": 0, "cr_collapsed": 0, "dup_collapsed": 0, "line_numbers": None}
    if not text:
        return "", info
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    text, info["ansi_stripped"] = _strip_ansi(text)
    if not collapse:
        return text, info

    trailing_nl = text.endswith("\n")
    lines = text.split("\n")
    if trailing_nl:
        lines.pop()

    if "\r" in text:
        cr_total = 0
        for i, line in enumerate(lines):
            if "\r" in line:
                lines[i], n = _collapse_cr(line)
                cr_total += n
        info["cr_collapsed"] = cr_total

    if len(lines) >= DUP_MIN_RUN:
        out: list[str] = []
        numbers: list[int] = []
        folded = 0
        n = len(lines)
        i = 0
        while i < n:
            line = lines[i]
            j = i + 1
            if line.strip():                       # blank runs are never folded
                key = _dup_key(line, fuzzy_dups)
                while j < n and (lines[j] == line or (fuzzy_dups and _dup_key(lines[j], True) == key)):
                    j += 1
            run = j - i
            if run >= DUP_MIN_RUN:
                out.append(f"{line} [x {run}]")
                numbers.append(i + 1)
                folded += run - 1
            else:
                out.extend(lines[i:j])
                numbers.extend(range(i + 1, j + 1))
            i = j
        if folded:
            info["dup_collapsed"] = folded
            info["line_numbers"] = numbers
            lines = out

    return "\n".join(lines) + ("\n" if trailing_nl else ""), info


# ── Runner detection ────────────────────────────────────────────────────────

_CMD_PREFIX = re.compile(
    r"""^(?:\s*(?:cd\s+(?:"[^"]*"|'[^']*'|\S+)\s*(?:&&|;)\s*|\w+=(?:"[^"]*"|'[^']*'|\S*)\s*;?\s*))*""")
_CMD_HINTS = (
    ("pytest", re.compile(r"(?:^|[\s/\\])(?:py\.test|pytest)\b|python[\d.]*\s+-m\s+pytest\b")),
    ("unittest", re.compile(r"python[\d.]*\s+-m\s+unittest\b|(?:^|[\s/\\])unittest\b")),
    ("cargo", re.compile(r"(?:^|[\s/\\])(?:cargo|rustc)\b")),
    ("tsc", re.compile(r"(?:^|[\s/\\])tsc\b")),
    ("vitest", re.compile(r"(?:^|[\s/\\])vitest\b")),
    ("jest", re.compile(r"(?:^|[\s/\\])jest\b")),
)
_OUTPUT_SIGNATURES = (
    ("pytest", re.compile(
        r"={3,} test session starts ={3,}|={3,} short test summary info ={3,}"
        r"|^={3,} .*\b(?:passed|failed|errors?|skipped|no tests ran)\b.* in [\d.]+s?.*={3,}$", re.M)),
    ("unittest", re.compile(r"^Ran \d+ tests? in [\d.]+s$", re.M)),
    ("cargo", re.compile(
        r"^error\[E\d{4}\]|^error: could not compile|^\s+Compiling \S+ v\d|^warning: .*generated \d+ warnings?"
        r"|^test result: (?:ok|FAILED)\. \d+ passed", re.M)),
    ("tsc", re.compile(r"error TS\d{4}:|^Found \d+ errors?\b", re.M)),
    ("vitest", re.compile(r"^\s*Test Files\s{2,}\d|^\s*RUN\s+v\d|⎯{3,} Failed Tests", re.M)),
    ("jest", re.compile(r"^Test Suites:\s|^Tests:\s+\d|^\s*● ", re.M)),
)


def _signature_sample(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= _SIG_HEAD + _SIG_TAIL:
        return text
    return text[:_SIG_HEAD] + "\n" + text[-_SIG_TAIL:]


def detect_runner(cmd: str, stdout: str | None = "", stderr: str | None = "") -> str | None:
    """pytest | unittest | cargo | tsc | jest | vitest, or None.

    The output's own signature wins over the command head (``npm test`` may
    run jest; ``make check`` may run pytest); the command head decides when
    the output says nothing (a run that crashed before printing).
    """
    sample = _signature_sample(stdout) + "\n" + _signature_sample(stderr)
    if sample.strip():
        for name, rx in _OUTPUT_SIGNATURES:
            if rx.search(sample):
                return name
    head = _CMD_PREFIX.sub("", (cmd or "").strip())
    for name, rx in _CMD_HINTS:
        if rx.search(head):
            return name
    return None


# ── Priority regions ────────────────────────────────────────────────────────

Region = tuple[int, int, str]

# pytest
_PT_HEADER = re.compile(r"^_{3,} (.+?) _{3,}$")
_PT_SUMMARY = re.compile(
    r"^={3,} .*\b(?:passed|failed|errors?|skipped|xfailed|xpassed|no tests ran)\b.* in [\d.]+ ?s.*={3,}$")
_PT_SHORT = re.compile(r"^={3,} short test summary info ={3,}$")
_PT_SHORT_LINE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")
_PT_PROGRESS_FAIL = re.compile(r"^(\S+::\S+) (FAILED|ERROR)\b")
_PT_LOCATION = re.compile(r"^\S+\.py:\d+: \S+")
_PT_E = re.compile(r"^E {2,}\S")
_PT_SECTION = re.compile(r"^-{3,} Captured .* -{3,}$|^={3,} .* ={3,}$")
# unittest
_UT_HEADER = re.compile(r"^(FAIL|ERROR): (\S+) \((\S+)\)")
_UT_RAN = re.compile(r"^Ran \d+ tests? in [\d.]+s$")
_UT_VERDICT = re.compile(r"^(OK|FAILED)\b")
_UT_RULE = re.compile(r"^[-=]{10,}$")
_UT_PROGRESS_FAIL = re.compile(r"^\S+ \(\S+\) \.\.\. (FAIL|ERROR)$")
# cargo / rustc
_CG_HEADER = re.compile(r"^(error(?:\[E\d+\])?|warning): (.*)$")
_CG_ARROW = re.compile(r"^\s*-->\s*(\S+)")
_CG_TAIL = re.compile(
    r"^error: could not compile|^warning: .*generated \d+ warnings?|^error: aborting due to"
    r"|^test result: |^error: test failed|^error: \d+ previous errors?")
_CG_TEST_FAIL = re.compile(r"^test (\S+) \.\.\. FAILED$")
_CG_PANIC = re.compile(r"^---- (\S+) stdout ----$")
_CG_PANICKED = re.compile(r"panicked at ")
# tsc
_TS_ERROR = re.compile(r"^(\S.*?)[(:](\d+)[,:](\d+)\)?[: -]+\s*error (TS\d+):\s*(.*)$")
_TS_FOUND = re.compile(r"^Found \d+ errors?\b")
# jest / vitest
_JS_BULLET = re.compile(r"^\s*● (.+?)\s*$")
_JS_RECEIVED = re.compile(r"^\s*(?:Received:|\+ Received|Received\b)")
_JS_FRAME = re.compile(r"^\s*> \d+ \|")
_JS_TOTALS = re.compile(r"^\s*(Test Suites|Test Files|Tests|Snapshots|Time|Duration|Start at|Errors)(?::|\s{2,})")
_JS_SUITE_FAIL = re.compile(r"^\s*FAIL\s+\S+")
_VT_CASE_FAIL = re.compile(r"^\s*(?:FAIL\s+\S+ > |[×✗] )(.+)$")
_VT_FRAME = re.compile(r"^\s*❯ \S+:\d+")
_VT_ASSERT = re.compile(r"^\s*(?:AssertionError|Error|TypeError|\w+Error):")
# generic
_ANCHOR = re.compile(
    r"Traceback \(most recent call last\)|\bERROR\b|\bERR!|Error\b|\berror\b|\bFAIL(?:ED|URE)?\b"
    r"|(?i:\bpanic(?:ked)?\b|\bexception\b|\bfatal\b)")


def _clamp(a: int, b: int, n: int) -> tuple[int, int]:
    return max(0, min(a, n - 1)), max(0, min(b, n - 1))


def _block(lines: list[str], start: int, stop, *, limit: int) -> int:
    """Index of the last line of the block starting at ``start``: the line
    before the first ``stop(line)`` hit, at most ``limit`` lines in."""
    end = start
    for i in range(start + 1, min(len(lines), start + limit + 1)):
        if stop(lines[i]):
            break
        end = i
    return end


def _split_long(start: int, end: int, why: str, *, head: int = 2, tail: int = 15,
                max_len: int = 30) -> list[Region]:
    if end - start + 1 <= max_len:
        return [(start, end, why)]
    return [(start, start + head - 1, why), (max(start + head, end - tail + 1), end, why)]


def _pytest_regions(lines: list[str]) -> list[Region]:
    n = len(lines)
    regions: list[Region] = []
    for i in range(n - 1, -1, -1):
        if _PT_SUMMARY.match(lines[i]):
            regions.append((i, i, "pytest summary"))
            break
    for i, line in enumerate(lines):
        if _PT_SHORT.match(line):
            end = i
            for j in range(i + 1, min(n, i + SUMMARY_FAIL_CAP + 2)):
                if _PT_SHORT_LINE.match(lines[j]):
                    end = j
                else:
                    break
            regions.append((i, end, "short test summary"))
            break
    blocks = 0
    for i, line in enumerate(lines):
        m = _PT_HEADER.match(line)
        if not m or blocks >= BLOCK_CAP:
            continue
        blocks += 1
        name = m.group(1).strip()
        span_end = _block(lines, i, lambda s: bool(_PT_HEADER.match(s) or _PT_SECTION.match(s)), limit=400)
        end = i
        for j in range(i + 1, span_end + 1):
            if _PT_LOCATION.match(lines[j]) or _PT_E.match(lines[j]):
                end = j
        if end == i:
            end = min(span_end, i + 15)
        regions.extend(_split_long(i, end, f"pytest failure {name}"))
    hits = 0
    for i, line in enumerate(lines):
        if _PT_PROGRESS_FAIL.match(line):
            regions.append((i, i, "failed test"))
            hits += 1
            if hits >= BLOCK_CAP:
                break
    return regions


def _unittest_regions(lines: list[str]) -> list[Region]:
    n = len(lines)
    regions: list[Region] = []
    for i, line in enumerate(lines):
        if _UT_RAN.match(line):
            end = i
            for j in range(i + 1, min(n, i + 4)):
                if lines[j].strip():
                    end = j
                    if _UT_VERDICT.match(lines[j]):
                        break
            regions.append((i, end, "unittest totals"))
            break
    blocks = 0
    for i, line in enumerate(lines):
        m = _UT_HEADER.match(line)
        if not m or blocks >= BLOCK_CAP:
            continue
        blocks += 1
        span_end = _block(lines, i, lambda s: bool(_UT_HEADER.match(s) or _UT_RAN.match(s)), limit=400)
        end = span_end
        while end > i and (not lines[end].strip() or _UT_RULE.match(lines[end])):
            end -= 1
        regions.extend(_split_long(i, end, f"unittest {m.group(1)} {m.group(2)}", tail=12))
    hits = 0
    for i, line in enumerate(lines):
        if _UT_PROGRESS_FAIL.match(line):
            regions.append((i, i, "failed test"))
            hits += 1
            if hits >= BLOCK_CAP:
                break
    return regions


def _cargo_regions(lines: list[str]) -> list[Region]:
    n = len(lines)
    regions: list[Region] = []
    for i in range(n - 1, -1, -1):
        if _CG_TAIL.match(lines[i]):
            regions.append((i, i, "cargo verdict"))
    errors: list[Region] = []
    warnings: list[Region] = []
    for i, line in enumerate(lines):
        m = _CG_HEADER.match(line)
        if not m:
            continue
        kind = m.group(1)
        if _CG_TAIL.match(line):
            continue
        end = _block(lines, i, lambda s: not s.strip() or bool(_CG_HEADER.match(s)), limit=8)
        if kind.startswith("error"):
            if len(errors) < BLOCK_CAP:
                errors.append((i, end, f"rustc {kind}"))
        elif len(warnings) < 5:
            arrow = i
            for j in range(i + 1, min(n, i + 4)):
                if _CG_ARROW.match(lines[j]):
                    arrow = j
                    break
            warnings.append((i, arrow, "rustc warning"))
    regions.extend(errors)
    fails = 0
    for i, line in enumerate(lines):
        if _CG_TEST_FAIL.match(line) and fails < BLOCK_CAP:
            regions.append((i, i, "failed test"))
            fails += 1
        elif _CG_PANIC.match(line) and fails < BLOCK_CAP:
            end = _block(lines, i, lambda s: bool(_CG_PANIC.match(s) or s.startswith("failures:")), limit=12)
            for j in range(i + 1, end + 1):
                if _CG_PANICKED.search(lines[j]):
                    end = min(end, j + 1)
                    break
            regions.append((i, end, f"panic {_CG_PANIC.match(line).group(1)}"))
            fails += 1
    regions.extend(warnings)
    return regions


def _tsc_regions(lines: list[str]) -> list[Region]:
    errs = [i for i, line in enumerate(lines) if _TS_ERROR.match(line)]
    regions: list[Region] = []
    if errs:
        regions.append((errs[0], errs[0], "tsc error"))
        if errs[-1] != errs[0]:
            regions.append((errs[-1], errs[-1], "tsc error"))
    for i, line in enumerate(lines):
        if _TS_FOUND.match(line):
            regions.append((i, i, "tsc totals"))
    # A wall of 300 errors is one problem repeated, not 300 problems: keep the
    # first TSC_ERRORS_HEAD and the last TSC_ERRORS_TAIL as priority (14 KB of
    # error lines measured otherwise); the rest pages back by id.
    middle = errs[1:-1]
    if len(middle) > TSC_ERRORS_HEAD + TSC_ERRORS_TAIL:
        middle = middle[:TSC_ERRORS_HEAD] + middle[-TSC_ERRORS_TAIL:]
    regions.extend((i, i, "tsc error") for i in middle)
    return regions


def _js_regions(lines: list[str], runner: str) -> list[Region]:
    n = len(lines)
    regions: list[Region] = []
    i = 0
    while i < n:
        if _JS_TOTALS.match(lines[i]):
            end = i
            while end + 1 < n and _JS_TOTALS.match(lines[end + 1]):
                end += 1
            regions.append((i, end, f"{runner} totals"))
            i = end + 1
            continue
        i += 1
    blocks = 0
    for i, line in enumerate(lines):
        if blocks >= BLOCK_CAP:
            break
        m = _JS_BULLET.match(line)
        if m:
            blocks += 1
            end = i
            for j in range(i + 1, min(n, i + 21)):
                if _JS_BULLET.match(lines[j]) or _JS_TOTALS.match(lines[j]):
                    break
                if _JS_RECEIVED.match(lines[j]) or _JS_FRAME.match(lines[j]):
                    end = j
            if end == i:
                end = _block(lines, i, lambda s: bool(_JS_BULLET.match(s) or _JS_TOTALS.match(s)), limit=6)
            regions.append((i, end, f"{runner} failure {m.group(1)}"))
            continue
        m = _VT_CASE_FAIL.match(line)          # vitest: ' FAIL  file > name' or '× name'
        if m:
            blocks += 1
            end = i
            for j in range(i + 1, min(n, i + 13)):
                if _VT_CASE_FAIL.match(lines[j]) or _JS_TOTALS.match(lines[j]):
                    break
                if _VT_ASSERT.match(lines[j]) or _JS_RECEIVED.match(lines[j]) or _VT_FRAME.match(lines[j]):
                    end = j
                if _VT_FRAME.match(lines[j]):
                    break
            regions.append((i, end, f"{runner} failure {m.group(1).strip()}"))
    suites = 0
    for i, line in enumerate(lines):
        if _JS_SUITE_FAIL.match(line) and " > " not in line and suites < BLOCK_CAP:
            regions.append((i, i, "failed suite"))
            suites += 1
    return regions


def _generic_regions(lines: list[str]) -> list[Region]:
    n = len(lines)
    hits = [i for i, line in enumerate(lines) if _ANCHOR.search(line)]
    if not hits:
        return []
    # The last anchor is usually the verdict, the first the origin; then the
    # rest, latest first, up to the cap.
    order: list[int] = []
    for i in [hits[-1], hits[0]] + hits[-2:0:-1]:
        if i not in order:
            order.append(i)
        if len(order) >= GENERIC_ANCHOR_CAP:
            break
    return [(max(0, i - GENERIC_CONTEXT), min(n - 1, i + GENERIC_CONTEXT), "error anchor") for i in order]


def priority_regions(runner: str | None, lines: list[str]) -> list[Region]:
    """0-based inclusive ``(start, end, why)`` ranges that must survive shaping,
    most important first. Regions may overlap; the shaper merges them."""
    if not lines:
        return []
    if runner == "pytest":
        regions = _pytest_regions(lines)
    elif runner == "unittest":
        regions = _unittest_regions(lines)
    elif runner == "cargo":
        regions = _cargo_regions(lines)
    elif runner == "tsc":
        regions = _tsc_regions(lines)
    elif runner in ("jest", "vitest"):
        regions = _js_regions(lines, runner)
    else:
        regions = _generic_regions(lines)
    n = len(lines)
    return [(a, b, why) for a, b, why in ((*_clamp(a, b, n), why) for a, b, why in regions) if a <= b]


# ── Structured tail ─────────────────────────────────────────────────────────

def _short(s: str, limit: int = 160) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _cap_list(items: list[str], total: int | None = None) -> list[str]:
    shown = items[:SUMMARY_FAIL_CAP]
    more = (total if total is not None else len(items)) - len(shown)
    if more > 0:
        shown.append(f"… and {more} more")
    return shown


def _pytest_tail(lines: list[str], exit_code) -> list[str]:
    totals = ""
    for line in reversed(lines):
        m = _PT_SUMMARY.match(line)
        if m:
            totals = line.strip("= ").strip()
            break
    fails: list[str] = []
    for line in lines:
        m = _PT_SHORT_LINE.match(line)
        if m:
            reason = f" — {_short(m.group(3))}" if m.group(3) else ""
            fails.append(f"{m.group(1).lower()}: {m.group(2)}{reason}")
    if not fails:
        for i, line in enumerate(lines):
            m = _PT_HEADER.match(line)
            if not m:
                continue
            reason = ""
            for j in range(i + 1, min(len(lines), i + 400)):
                if _PT_HEADER.match(lines[j]):
                    break
                if _PT_E.match(lines[j]):
                    reason = f" — {_short(lines[j][1:].strip())}"
                    break
            fails.append(f"failed: {m.group(1).strip()}{reason}")
    head = f"pytest: {totals}" if totals else f"pytest: exit {exit_code}, no summary line found"
    return [head] + _cap_list(fails)


def _unittest_tail(lines: list[str], exit_code) -> list[str]:
    ran = verdict = ""
    for i, line in enumerate(lines):
        if _UT_RAN.match(line):
            ran = line.strip()
            for j in range(i + 1, min(len(lines), i + 4)):
                if _UT_VERDICT.match(lines[j]):
                    verdict = lines[j].strip()
                    break
    fails: list[str] = []
    for i, line in enumerate(lines):
        m = _UT_HEADER.match(line)
        if not m:
            continue
        reason = ""
        end = _block(lines, i, lambda s: bool(_UT_HEADER.match(s) or _UT_RAN.match(s)), limit=400)
        for j in range(end, i, -1):
            if lines[j].strip() and not _UT_RULE.match(lines[j]):
                reason = f" — {_short(lines[j])}"
                break
        fails.append(f"{m.group(1).lower()}: {m.group(2)} ({m.group(3)}){reason}")
    head = "unittest: " + "; ".join(p for p in (ran, verdict) if p) if ran else \
        f"unittest: exit {exit_code}, no 'Ran N tests' line found"
    return [head] + _cap_list(fails)


def _cargo_tail(lines: list[str], exit_code) -> list[str]:
    verdicts = [line.strip() for line in lines if _CG_TAIL.match(line)]
    items: list[str] = []
    for i, line in enumerate(lines):
        m = _CG_HEADER.match(line)
        if m and m.group(1).startswith("error") and not _CG_TAIL.match(line):
            where = ""
            for j in range(i + 1, min(len(lines), i + 4)):
                a = _CG_ARROW.match(lines[j])
                if a:
                    where = f" — {a.group(1)}"
                    break
            items.append(f"{m.group(1)}: {_short(m.group(2))}{where}")
        elif _CG_TEST_FAIL.match(line):
            items.append(f"failed: {_CG_TEST_FAIL.match(line).group(1)}")
    head = "cargo: " + "; ".join(verdicts[-2:]) if verdicts else f"cargo: exit {exit_code}, no verdict line found"
    return [head] + _cap_list(items)


def _tsc_tail(lines: list[str], exit_code) -> list[str]:
    found = ""
    items: list[str] = []
    for line in lines:
        m = _TS_ERROR.match(line)
        if m:
            items.append(f"{m.group(1)}({m.group(2)},{m.group(3)}): {m.group(4)} {_short(m.group(5), 120)}")
        elif _TS_FOUND.match(line):
            found = line.strip()
    if not found:
        found = f"{len(items)} error line(s), exit {exit_code}" if items else f"exit {exit_code}, no error lines found"
    return [f"tsc: {found}"] + _cap_list(items)


def _js_tail(lines: list[str], exit_code, runner: str) -> list[str]:
    totals = [re.sub(r"\s{2,}", " ", line.strip()) for line in lines if _JS_TOTALS.match(line)]
    totals = [t for t in totals if not t.startswith(("Time", "Duration", "Start at", "Snapshots"))]
    items: list[str] = []
    n = len(lines)
    for i, line in enumerate(lines):
        m = _JS_BULLET.match(line)
        if not m:
            m = _VT_CASE_FAIL.match(line)
            if not m:
                continue
        detail = ""
        exp = rec = ""
        for j in range(i + 1, min(n, i + 16)):
            s = lines[j].strip()
            if _JS_BULLET.match(lines[j]) or _JS_TOTALS.match(lines[j]) or _VT_CASE_FAIL.match(lines[j]):
                break
            if s.startswith("Expected:") and not exp:
                exp = s
            elif s.startswith("Received:") and not rec:
                rec = s
            elif _VT_ASSERT.match(lines[j]) and not detail:
                detail = _short(s, 120)
        if exp or rec:
            detail = " / ".join(p for p in (exp, rec) if p)
        items.append(f"failed: {m.group(1).strip()}" + (f" — {detail}" if detail else ""))
    head = f"{runner}: " + "; ".join(totals) if totals else f"{runner}: exit {exit_code}, no totals block found"
    return [head] + _cap_list(items)


def structured_tail(runner: str | None, lines: list[str], exit_code) -> str:
    """A compact ``--- summary ---`` section for a recognised runner, else ``""``.

    One head line (the runner's own totals) and one line per failing test /
    error (capped at SUMMARY_FAIL_CAP). ~200 bytes for a typical run; it is
    what the agent reads first, so the renderer adds it whenever the output
    has more than SUMMARY_MIN_LINES lines and counts it inside the budget.
    """
    if runner not in RUNNERS:
        return ""
    if runner == "pytest":
        rows = _pytest_tail(lines, exit_code)
    elif runner == "unittest":
        rows = _unittest_tail(lines, exit_code)
    elif runner == "cargo":
        rows = _cargo_tail(lines, exit_code)
    elif runner == "tsc":
        rows = _tsc_tail(lines, exit_code)
    else:
        rows = _js_tail(lines, exit_code, runner)
    return "--- summary ---\n" + "\n".join(rows) + "\n"
