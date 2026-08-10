"""AgentCI — turn raw job logs into structured failures.

The point of the whole module (AgentCI spec §18, PRD 2 "Failure extraction"):
an agent that has to read 4,000 lines of pytest output to find one assertion
is an agent burning context to do `grep`. A structured failure is
`{file, line, message}` — small enough to act on, specific enough to fix.

Two rules keep this honest:

1. **Never claim to have parsed what we did not.** A parser that finds nothing
   returns nothing; the caller then falls back to a bounded log tail and says
   so. Inventing a plausible-looking failure object from an unrecognized
   format would point an agent at the wrong file.
2. **Never report zero failures for a job that failed.** If a job exited
   non-zero and no parser matched, that is itself the finding — callers emit
   an `unparsed` failure carrying the tail, so "0 failures" can only ever mean
   "this job passed".
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# How much raw log to keep on an unparsed failure. Big enough to contain a
# traceback, small enough not to blow an agent's context on one job.
TAIL_LINES = 40


@dataclass
class Failure:
    tool: str                    # pytest | ruff | jest | tsc | generic | unparsed
    message: str
    file: str = ""
    line: int = 0
    rule: str = ""               # e.g. ruff code F401
    test: str = ""               # e.g. tests/test_x.py::test_y
    excerpt: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0)}


@dataclass
class ParseResult:
    failures: list = field(default_factory=list)
    parser: str = ""

    def to_dict(self) -> dict:
        return {"parser": self.parser,
                "failures": [f.to_dict() for f in self.failures]}


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    rows = (text or "").splitlines()
    return "\n".join(rows[-lines:]).strip()


# ── pytest ──────────────────────────────────────────────────────────────────
# The summary block is the reliable surface. Per-assertion detail varies with
# plugins and verbosity, but `FAILED path::test - message` is stable across
# pytest 6-8 and is what `-q` prints.
_PYTEST_SUMMARY = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<test>\S+?)(?:\s+-\s+(?P<msg>.*))?$", re.MULTILINE)
# `path:line: in func` / `path:line: MessageError` from the traceback body.
_PYTEST_LOC = re.compile(r"^(?P<file>[^\s:][^:]*\.py):(?P<line>\d+):", re.MULTILINE)


def parse_pytest(text: str) -> list:
    out: list = []
    for m in _PYTEST_SUMMARY.finditer(text or ""):
        test = m.group("test") or ""
        msg = (m.group("msg") or "").strip()
        file = test.split("::", 1)[0] if "::" in test else ""
        line = 0
        # Find a location line near this test's traceback, if one is present.
        if file:
            for loc in _PYTEST_LOC.finditer(text or ""):
                if loc.group("file").replace("\\", "/").endswith(
                        file.replace("\\", "/")):
                    line = int(loc.group("line"))
                    break
        out.append(Failure(tool="pytest", test=test, file=file, line=line,
                           message=msg or "test failed"))
    return out


# ── ruff ────────────────────────────────────────────────────────────────────
# Default output: `path:line:col: CODE message`
_RUFF = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<code>[A-Z]+\d+)\s+(?P<msg>.*)$", re.MULTILINE)


def parse_ruff(text: str) -> list:
    return [
        Failure(tool="ruff", file=m.group("file"), line=int(m.group("line")),
                rule=m.group("code"), message=m.group("msg").strip())
        for m in _RUFF.finditer(text or "")
    ]


# ── TypeScript / tsc ────────────────────────────────────────────────────────
# `path(line,col): error TS1234: message`
_TSC = re.compile(
    r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),\d+\):\s+error\s+"
    r"(?P<code>TS\d+):\s+(?P<msg>.*)$", re.MULTILINE)


def parse_tsc(text: str) -> list:
    return [
        Failure(tool="tsc", file=m.group("file"), line=int(m.group("line")),
                rule=m.group("code"), message=m.group("msg").strip())
        for m in _TSC.finditer(text or "")
    ]


# ── Jest / Vitest ───────────────────────────────────────────────────────────
# `  ● suite › case` blocks, plus `FAIL path` headers.
_JEST_FAIL = re.compile(r"^\s*●\s+(?P<test>.+?)\s*$", re.MULTILINE)
_JEST_FILE = re.compile(r"^\s*FAIL\s+(?P<file>\S+)", re.MULTILINE)


def parse_jest(text: str) -> list:
    files = [m.group("file") for m in _JEST_FILE.finditer(text or "")]
    out: list = []
    for m in _JEST_FAIL.finditer(text or ""):
        test = m.group("test").strip()
        # Jest repeats the suite header inside summaries; skip the noise rows.
        if not test or test.lower().startswith(("console", "test suite failed")):
            continue
        out.append(Failure(tool="jest", test=test,
                           file=files[0] if files else "",
                           message="test failed"))
    return out


# ── Generic tracebacks ──────────────────────────────────────────────────────
_PY_TRACEBACK = re.compile(
    r'Traceback \(most recent call last\):(?P<body>.*?)'
    r'^(?P<exc>\w+(?:Error|Exception|Exit)[^\n]*)$',
    re.DOTALL | re.MULTILINE)
_PY_TB_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')


def parse_traceback(text: str) -> list:
    out: list = []
    for m in _PY_TRACEBACK.finditer(text or ""):
        frames = _PY_TB_FRAME.findall(m.group("body") or "")
        file, line = (frames[-1] if frames else ("", "0"))
        out.append(Failure(tool="generic", file=file, line=int(line or 0),
                           message=m.group("exc").strip(),
                           excerpt=_tail(m.group(0), 12)))
    return out


# Ordered: the most specific parser that matches wins. pytest before generic
# traceback, because a pytest run full of tracebacks should read as N test
# failures, not N unrelated exceptions.
_PARSERS = (
    ("pytest", parse_pytest),
    ("ruff", parse_ruff),
    ("tsc", parse_tsc),
    ("jest", parse_jest),
    ("traceback", parse_traceback),
)


def parse(text: str, exit_code: int = 1, limit: int = 50) -> ParseResult:
    """Best structured reading of one job's log.

    `exit_code` matters: a job that PASSED may still contain the word
    "FAILED" in a fixture name or a doctest, and reporting failures for a green
    job is its own kind of lie. Parsing only runs for a non-zero exit.
    """
    if exit_code == 0:
        return ParseResult(failures=[], parser="")

    for name, fn in _PARSERS:
        try:
            found = fn(text)
        except Exception:
            # A crashing parser must not swallow the job's failure — fall
            # through to the next one and ultimately to `unparsed`.
            continue
        if found:
            return ParseResult(failures=found[:limit], parser=name)

    # Nothing matched, but the job DID fail. Say exactly that and hand back a
    # bounded tail; never return an empty list for a failed job.
    return ParseResult(
        failures=[Failure(tool="unparsed",
                          message=f"job failed (exit {exit_code}); "
                                  "no known failure format recognised",
                          excerpt=_tail(text))],
        parser="unparsed")
