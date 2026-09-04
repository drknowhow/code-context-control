"""Deterministic stream generators for the ``c3 shell-eval`` fixture suite.

Each generator returns a :class:`Streams` — the ``(stdout, stderr, exit_code,
timed_out)`` a real command would have produced — from a ``params`` dict and a
seed. Nothing here touches a subprocess, and nothing large is committed: a
2 MB blob is a few lines of code and a seed, so the suite stays small and
every run sees byte-identical input.

Determinism rules (a case that flips between runs is not a gate):

* All randomness goes through ``random.Random(seed)``. The seed is the case's
  own ``params.seed`` or a CRC32 of its id — never ``hash()``, which varies
  per process.
* Line endings are ``\\n`` unless the case asks for ``crlf`` (Windows tools
  emit ``\\r\\n``; the renderer must cope with both).
* Text is what a UTF-8 console would show: bytes are decoded with
  ``errors="replace"`` exactly as ``_run_sync`` does.

``Streams.keep`` names the fragments a case expects to survive rendering, so
a suite line can say ``"must_contain": ["$token"]`` instead of pasting a
generated string into JSON. The evaluator resolves ``$name`` against it.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field

_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu invoice ledger session token router index chunk filter "
    "render budget stream buffer socket handler worker queue cache store"
).split()


@dataclass
class Streams:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    keep: dict[str, str] = field(default_factory=dict)

    def as_result(self, duration_ms: int = 1234, shell: str = "git-bash") -> dict:
        """The dict ``_run_sync`` returns, ready for the renderer."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": duration_ms,
            "timed_out": self.timed_out,
            "shell": shell,
        }


def seed_for(case_id: str, params: dict | None = None) -> int:
    params = params or {}
    if "seed" in params:
        return int(params["seed"])
    return zlib.crc32(case_id.encode("utf-8")) & 0xFFFFFFFF


def _nl(params: dict) -> str:
    return "\r\n" if params.get("crlf") else "\n"


def _words(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(n))


def _ident(rng: random.Random, n: int = 8) -> str:
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


# ── generators ──────────────────────────────────────────────────────────────


def minified_grep_hit(params: dict, rng: random.Random) -> Streams:
    """``grep -n needle dist/bundle.js``: a few enormous minified lines.

    The match sits mid-line; a newline-count trigger never fires (2 newlines)
    and a naive head clip would drop the token.
    """
    lines = int(params.get("lines", 3))
    line_bytes = int(params.get("line_bytes", 300_000))
    token = str(params.get("token", "__c3_needle_7f3a__"))
    token_line = int(params.get("token_line", 1))
    token_pos = float(params.get("token_pos", 0.5))
    out = []
    for i in range(lines):
        body = _minified(rng, line_bytes)
        if i == token_line:
            at = int(len(body) * token_pos)
            body = body[:at] + f"if(window.{token}){{return 1}}" + body[at:]
        out.append(f"{i * 1000 + 17}:{body}")
    return Streams(stdout=_nl(params).join(out) + _nl(params), keep={"token": token})


def _minified(rng: random.Random, size: int) -> str:
    parts = []
    total = 0
    while total < size:
        chunk = (f"function {_ident(rng, 2)}({_ident(rng, 1)},{_ident(rng, 1)}){{"
                 f"return {_ident(rng, 1)}.{_ident(rng, 5)}({rng.randint(0, 9999)})}};")
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)[:size]


def pytest_buried_failure(params: dict, rng: random.Random) -> Streams:
    """Verbose pytest run: thousands of PASSED lines, one failure, and a
    summary line that is NOT the last line (40 warning lines follow it)."""
    nl = _nl(params)
    total = int(params.get("lines", 5000))
    passed = int(params.get("passed", 3366))
    failing = str(params.get("failing_test", "tests/test_billing.py::test_invoice_rounding_3366"))
    summary = str(params.get("summary", "1 failed, 3366 passed, 12 warnings in 240.12s"))
    warn_lines = int(params.get("warning_lines", 40))
    last_frame = f"{failing.split('::')[0]}:87: AssertionError"

    lines = ["============================= test session starts =============================",
             "platform win32 -- Python 3.12.4, pytest-8.3.2, pluggy-1.5.0",
             f"collected {passed + 1} items", ""]
    fail_at = int(params.get("fail_at", passed // 2))
    mod_count = 0
    for i in range(passed + 1):
        if i == fail_at:
            lines.append(f"{failing} FAILED [ {int(100 * i / (passed + 1)):2d}%]")
            continue
        mod = f"tests/test_{_WORDS[(i // 40) % len(_WORDS)]}_{mod_count}.py"
        if i % 40 == 0:
            mod_count += 1
        lines.append(f"{mod}::test_{_WORDS[i % len(_WORDS)]}_{i} PASSED [ {int(100 * i / (passed + 1)):2d}%]")
    lines += ["", "=================================== FAILURES ===================================",
              f"_______________________ {failing.split('::')[-1]} _______________________", ""]
    for depth in range(6):
        lines.append(f"{failing.split('::')[0]}:{40 + depth * 7}: in {'test_invoice_rounding_3366' if depth == 0 else 'helper_' + str(depth)}")
        lines.append(f"    total = round_cents(amount * {depth + 1})")
    lines += ["E   AssertionError: assert 1234.57 == 1234.56", "E    +  where 1234.57 = round_cents(1234.565)",
              "", last_frame, "", "=========================== short test summary info ============================",
              f"FAILED {failing} - AssertionError: assert 1234.57 == 1234.56",
              f"======== {summary} ========"]
    for w in range(warn_lines):
        lines.append(f"  {_ident(rng, 6)}.py:{rng.randint(1, 400)}: DeprecationWarning: "
                     f"{_words(rng, 5)} is deprecated (warning {w + 1}/{warn_lines})")
    # Pad / trim to the requested size so the case is exactly what it claims.
    while len(lines) < total:
        lines.insert(4, f"tests/test_pad_{len(lines)}.py::test_pad_{len(lines)} PASSED [  0%]")
    stdout = nl.join(lines) + nl
    return Streams(stdout=stdout, exit_code=1,
                   keep={"failing_test": failing, "summary": summary, "last_frame": last_frame})


def tsc_errors(params: dict, rng: random.Random) -> Streams:
    """``tsc --noEmit``: N identical-shaped error lines, exit 2."""
    nl = _nl(params)
    n = int(params.get("lines", 300))
    lines = []
    for i in range(n):
        f = f"src/components/{_WORDS[i % len(_WORDS)].title()}{i}.ts"
        lines.append(f"{f}({rng.randint(1, 400)},{rng.randint(1, 80)}): error TS2322: "
                     f"Type 'string' is not assignable to type 'number'.")
    lines.append(f"Found {n} errors in {n} files.")
    return Streams(stdout=nl.join(lines) + nl, exit_code=2,
                   keep={"first": lines[0], "last_error": lines[-2], "count": lines[-1]})


def npm_build_noise(params: dict, rng: random.Random) -> Streams:
    """``npm ci && npm run build``: progress noise, exit 0, a final summary."""
    nl = _nl(params)
    n = int(params.get("lines", 400))
    lines = []
    for i in range(n):
        kind = i % 4
        if kind == 0:
            lines.append(f"npm http fetch GET 200 https://registry.npmjs.org/{_ident(rng, 7)} {rng.randint(20, 900)}ms")
        elif kind == 1:
            lines.append(f"npm WARN deprecated {_ident(rng, 6)}@{rng.randint(0, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}: {_words(rng, 4)}")
        elif kind == 2:
            lines.append(f"[{i + 1}/{n}] Compiling module {_ident(rng, 9)}.ts")
        else:
            lines.append(f"  building {'#' * (i % 40)}{'.' * (40 - i % 40)} {int(100 * i / n)}%")
    last = str(params.get("last_line", "added 1234 packages, and audited 1235 packages in 42s"))
    lines += ["", "webpack 5.92.1 compiled successfully in 8123 ms", last]
    return Streams(stdout=nl.join(lines) + nl, exit_code=0, keep={"last_line": last})


def cr_progress_bar(params: dict, rng: random.Random) -> Streams:
    """One logical line rewritten thousands of times with ``\\r``; only the
    final state means anything."""
    steps = int(params.get("steps", 2000))
    parts = []
    for i in range(1, steps + 1):
        filled = int(40 * i / steps)
        parts.append(f"\rDownloading model.bin  {i}/{steps} [{'=' * filled}{' ' * (40 - filled)}] {int(100 * i / steps):3d}%")
    final = f"{steps}/{steps}"
    mid = f"{steps // 2}/{steps}"
    return Streams(stdout="".join(parts) + "\n", exit_code=0, keep={"final": final, "mid": mid})


def jsonl_grep(params: dict, rng: random.Random) -> Streams:
    """``grep key events.jsonl``: N hits, every one a huge single-line JSON
    object. A marker inside a JSON line makes it unparseable."""
    nl = _nl(params)
    n = int(params.get("hits", 40))
    line_bytes = int(params.get("line_bytes", 50_000))
    key = str(params.get("key", "correlation_id"))
    value = str(params.get("value", "corr-9f1e2d3c"))
    lines = []
    for i in range(n):
        fields = [f'"seq":{i}', f'"{key}":"{value}"', '"level":"info"']
        while sum(len(f) for f in fields) < line_bytes:
            fields.append(f'"{_ident(rng, 8)}":"{_words(rng, 12)}"')
        lines.append("{" + ",".join(fields) + "}")
    return Streams(stdout=nl.join(lines) + nl, exit_code=0,
                   keep={"key": f'"{key}":"{value}"'})


def python_traceback(params: dict, rng: random.Random) -> Streams:
    """A 60-line Python traceback on stderr, exit 1."""
    nl = _nl(params)
    frames = int(params.get("frames", 28))
    exc = str(params.get("exception", "KeyError: 'invoice_total'"))
    lines = ["Traceback (most recent call last):"]
    for i in range(frames):
        mod = _WORDS[i % len(_WORDS)]
        lines.append(f'  File "src/ledgerlite/{mod}.py", line {rng.randint(10, 400)}, in {mod}_{i}')
        lines.append(f"    return {mod}_{i + 1}(payload[\"{_ident(rng, 5)}\"])")
    last_frame = '  File "src/ledgerlite/billing/invoice.py", line 87, in compute_total'
    lines += [last_frame, "    return totals[\"invoice_total\"]", exc]
    return Streams(stdout="", stderr=nl.join(lines) + nl, exit_code=1,
                   keep={"last_frame": last_frame, "exception": exc})


def cargo_errors(params: dict, rng: random.Random) -> Streams:
    """``cargo build``: compile progress then several errors, exit 101."""
    nl = _nl(params)
    errors = int(params.get("errors", 12))
    lines = [f"   Compiling {_ident(rng, 6)} v0.{rng.randint(1, 30)}.{rng.randint(0, 9)}" for _ in range(60)]
    lines.append("   Compiling ledgerlite v0.4.2 (/work/ledgerlite)")
    for i in range(errors):
        lines += ["error[E0308]: mismatched types",
                  f"  --> src/billing/invoice.rs:{40 + i * 9}:{rng.randint(5, 30)}",
                  "   |",
                  f"{40 + i * 9:2d} |     let total: u64 = amount * {i + 1}.5;",
                  "   |                      ^^^^^^^^^^^^^^^^ expected `u64`, found `f64`",
                  ""]
    final = f"error: could not compile `ledgerlite` (lib) due to {errors} previous errors"
    lines += ["For more information about this error, try `rustc --explain E0308`.", final]
    locations = [ln.strip() for ln in lines if ln.lstrip().startswith("-->")]
    return Streams(stdout="", stderr=nl.join(lines) + nl, exit_code=101,
                   keep={"e0308": "error[E0308]: mismatched types", "final": final,
                         "loc_first": locations[0], "loc_last": locations[-1]})


def jest_failure(params: dict, rng: random.Random) -> Streams:
    """``jest``: many PASS suites, one failing test, a totals block."""
    nl = _nl(params)
    suites = int(params.get("suites", 60))
    name = str(params.get("test_name", "Invoice › rounds totals to cents"))
    lines = []
    for i in range(suites):
        lines.append(f"PASS src/__tests__/{_WORDS[i % len(_WORDS)]}{i}.test.ts ({rng.randint(1, 900)} ms)")
    lines += ["FAIL src/__tests__/invoice.test.ts", f"  ● {name}", "",
              "    expect(received).toBe(expected) // Object.is equality", "",
              "    Expected: 1234.56", "    Received: 1234.57", "",
              "      41 |   it('rounds totals to cents', () => {",
              "    > 42 |     expect(computeTotal(items)).toBe(1234.56);",
              "         |                                 ^", "",
              "Test Suites: 1 failed, 59 passed, 60 total",
              "Tests:       1 failed, 211 passed, 212 total",
              "Snapshots:   0 total", "Time:        14.2 s"]
    return Streams(stdout=nl.join(lines) + nl, exit_code=1,
                   keep={"test_name": name, "totals": "Tests:       1 failed, 211 passed, 212 total"})


def pytest_three_failures(params: dict, rng: random.Random) -> Streams:
    """Verbose pytest run with THREE failures spread through the run and a
    wall of warning lines after the summary. Every failing id, its assertion
    and the summary line must survive; the run is far over budget."""
    nl = _nl(params)
    passed = int(params.get("passed", 2400))
    warn_lines = int(params.get("warning_lines", 400))
    failing = ["tests/test_billing.py::test_invoice_rounding",
               "tests/test_router.py::test_route_precedence",
               "tests/test_cache.py::test_ttl_expiry"]
    fail_at = {passed // 5: 0, passed // 2: 1, (passed * 4) // 5: 2}
    lines = ["============================= test session starts =============================",
             "platform linux -- Python 3.12.4, pytest-8.3.2, pluggy-1.5.0",
             f"collected {passed + 3} items", ""]
    for i in range(passed + 3):
        if i in fail_at:
            lines.append(f"{failing[fail_at[i]]} FAILED [ {int(100 * i / (passed + 3)):2d}%]")
            continue
        mod = f"tests/test_{_WORDS[(i // 40) % len(_WORDS)]}_{i // 40}.py"
        lines.append(f"{mod}::test_{_WORDS[i % len(_WORDS)]}_{i} PASSED [ {int(100 * i / (passed + 3)):2d}%]")
    lines += ["", "=================================== FAILURES ==================================="]
    asserts = ["E   AssertionError: assert 1234.57 == 1234.56",
               "E   AssertionError: assert '/api/v2' == '/api/v1'",
               "E   AssertionError: assert None is not None"]
    for k, fid in enumerate(failing):
        path, name = fid.split("::")
        lines += [f"_______________________ {name} _______________________", ""]
        for depth in range(4):
            lines.append(f"{path}:{30 + depth * 9}: in {name if depth == 0 else 'helper_' + str(depth)}")
            lines.append(f"    value = compute_{_WORDS[(k * 7 + depth) % len(_WORDS)]}(payload)")
        lines += [asserts[k], "", f"{path}:{70 + k}: AssertionError", ""]
    lines.append("=========================== short test summary info ============================")
    for k, fid in enumerate(failing):
        lines.append(f"FAILED {fid} - {asserts[k][4:].strip()}")
    summary = f"3 failed, {passed} passed, 7 warnings in 188.40s"
    lines.append(f"======== {summary} ========")
    for w in range(warn_lines):
        lines.append(f"  {_ident(rng, 6)}.py:{rng.randint(1, 400)}: DeprecationWarning: "
                     f"{_words(rng, 5)} is deprecated (warning {w + 1}/{warn_lines})")
    keep = {"summary": summary, "assert_1": asserts[0], "assert_2": asserts[1], "assert_3": asserts[2]}
    keep.update({f"fail_{k + 1}": fid for k, fid in enumerate(failing)})
    return Streams(stdout=nl.join(lines) + nl, exit_code=1, keep=keep)


def unittest_failures(params: dict, rng: random.Random) -> Streams:
    """``python -m unittest -v``: a long verbose run on stderr with one FAIL
    and one ERROR, then the ``Ran N tests`` / ``FAILED (...)`` totals."""
    nl = _nl(params)
    n = int(params.get("tests", 1500))
    lines = []
    for i in range(n):
        cls = f"tests.test_{_WORDS[(i // 25) % len(_WORDS)]}.{_WORDS[(i // 25) % len(_WORDS)].title()}Tests"
        name = f"test_{_WORDS[i % len(_WORDS)]}_{i}"
        verdict = "ok"
        if i == n // 3:
            name, verdict = "test_rounding", "FAIL"
        elif i == (2 * n) // 3:
            name, verdict = "test_lookup", "ERROR"
        lines.append(f"{name} ({cls}.{name}) ... {verdict}")
    fail_header = "FAIL: test_rounding (tests.test_billing.BillingTests.test_rounding)"
    error_header = "ERROR: test_lookup (tests.test_cache.CacheTests.test_lookup)"
    assertion = "AssertionError: 1234.57 != 1234.56"
    exception = "KeyError: 'invoice_total'"
    lines += ["", "=" * 70, error_header, "-" * 70, "Traceback (most recent call last):",
              '  File "tests/test_cache.py", line 44, in test_lookup',
              "    self.assertEqual(cache['invoice_total'], 1)", exception, "",
              "=" * 70, fail_header, "-" * 70, "Traceback (most recent call last):",
              '  File "tests/test_billing.py", line 87, in test_rounding',
              "    self.assertEqual(round_cents(1234.565), 1234.56)", assertion, "",
              "-" * 70, f"Ran {n} tests in 12.345s", "", "FAILED (failures=1, errors=1)"]
    return Streams(stdout="", stderr=nl.join(lines) + nl, exit_code=1,
                   keep={"fail_header": fail_header, "error_header": error_header,
                         "assertion": assertion, "exception": exception,
                         "ran": f"Ran {n} tests in 12.345s", "verdict": "FAILED (failures=1, errors=1)"})


def ansi_cr_progress(params: dict, rng: random.Random) -> Streams:
    """A coloured progress bar rewritten with ``\\r`` (pip / npm style): ANSI
    escapes AND carriage returns on the same line, then a plain last line."""
    steps = int(params.get("steps", 500))
    parts = []
    for i in range(1, steps + 1):
        filled = int(30 * i / steps)
        parts.append(f"\r\x1b[2K\x1b[32m{'━' * filled}\x1b[90m{'━' * (30 - filled)}\x1b[0m "
                     f"\x1b[1m{i}/{steps}\x1b[22m {int(100 * i / steps):3d}%")
    done = str(params.get("done", "Successfully installed ledgerlite-0.4.2"))
    stdout = "".join(parts) + "\n" + f"\x1b[32m{done}\x1b[0m\n"
    return Streams(stdout=stdout, exit_code=0,
                   keep={"final": f"{steps}/{steps}", "mid": f"{steps // 2}/{steps}", "done": done})


def identical_flood(params: dict, rng: random.Random) -> Streams:
    """The same line N times (a retry loop, a heartbeat); only the line and
    its count carry information."""
    nl = _nl(params)
    n = int(params.get("lines", 200))
    line = str(params.get("line", "WARNING: retrying connection to broker (timeout)"))
    return Streams(stdout=nl.join([line] * n) + nl, exit_code=0,
                   keep={"line": line, "count": f"[x {n}]"})


def binary_garbage(params: dict, rng: random.Random) -> Streams:
    """``cat model.bin``: random bytes decoded with errors=replace, like the
    console would show them."""
    size = int(params.get("bytes", 2 * 1024 * 1024))
    raw = rng.randbytes(size) if hasattr(rng, "randbytes") else bytes(rng.getrandbits(8) for _ in range(size))
    if params.get("strip_newlines"):
        # A blob with no 0x0A at all (a base64 asset, a packed texture): the
        # newline-count trigger never fires, so nothing stands between the
        # raw bytes and the agent.
        raw = raw.replace(b"\n", b"\x00").replace(b"\r", b"\x00")
    return Streams(stdout=raw.decode("utf-8", errors="replace"), exit_code=0)


def ansi_colored(params: dict, rng: random.Random) -> Streams:
    """``eslint --color``: a short, colored report (under the filter
    threshold, so today nothing strips the escapes)."""
    nl = _nl(params)
    n = int(params.get("lines", 24))
    lines = []
    for i in range(n):
        f = f"src/{_WORDS[i % len(_WORDS)]}.ts"
        lines.append(f"\x1b[4m{f}\x1b[24m")
        lines.append(f"  \x1b[2m{rng.randint(1, 200)}:{rng.randint(1, 60)}\x1b[22m  "
                     f"\x1b[31merror\x1b[39m  Unexpected any  \x1b[2m@typescript-eslint/no-explicit-any\x1b[22m")
    lines.append(f"\x1b[31m\x1b[1m✖ {n} problems ({n} errors, 0 warnings)\x1b[22m\x1b[39m")
    return Streams(stdout=nl.join(lines[:n]) + nl, exit_code=1,
                   keep={"plain_error": "error", "rule": "@typescript-eslint/no-explicit-any"})


def huge_stderr_empty_stdout(params: dict, rng: random.Random) -> Streams:
    """A linker / compiler dumping ~1 MB of diagnostics on stderr, nothing on
    stdout. Stderr was never filtered."""
    nl = _nl(params)
    size = int(params.get("bytes", 1024 * 1024))
    lines = []
    total = 0
    i = 0
    while total < size:
        line = (f"ld.lld: warning: {_ident(rng, 10)}.o: symbol _{_ident(rng, 12)} "
                f"defined in discarded section .text.{_words(rng, 3).replace(' ', '_')} [{i}]")
        lines.append(line)
        total += len(line) + 1
        i += 1
    tail = "ld.lld: error: undefined symbol: compute_total_v2"
    lines.append(tail)
    return Streams(stdout="", stderr=nl.join(lines) + nl, exit_code=1, keep={"tail": tail})


def mixed_streams_small(params: dict, rng: random.Random) -> Streams:
    """A short command with both streams; the response must carry both
    verbatim."""
    nl = _nl(params)
    out = nl.join([f"line {i}: {_words(rng, 4)}" for i in range(int(params.get("stdout_lines", 6)))]) + nl
    err = nl.join([f"warning: {_words(rng, 3)}" for _ in range(int(params.get("stderr_lines", 2)))]) + nl
    return Streams(stdout=out, stderr=err, exit_code=int(params.get("exit_code", 0)),
                   keep={"stdout": out.rstrip(), "stderr": err.rstrip()})


def under_budget_120_lines(params: dict, rng: random.Random) -> Streams:
    """120 plain, distinct lines (~6 KB) — well under any byte budget, yet
    over the 30-line filter threshold."""
    nl = _nl(params)
    n = int(params.get("lines", 120))
    lines = [f"{i + 1:03d} {_words(rng, 7)}" for i in range(n)]
    mid = lines[n // 2]
    return Streams(stdout=nl.join(lines) + nl, exit_code=0,
                   keep={"first": lines[0], "middle": mid, "last": lines[-1]})


def timeout_partial(params: dict, rng: random.Random) -> Streams:
    """A command killed at the deadline with partial stdout."""
    nl = _nl(params)
    n = int(params.get("lines", 12))
    lines = [f"[{i:02d}s] still working on {_words(rng, 3)}" for i in range(n)]
    return Streams(stdout=nl.join(lines) + nl, stderr="", exit_code=-1, timed_out=True,
                   keep={"last_partial": lines[-1]})


def sed_long_sql_lines(params: dict, rng: random.Random) -> Streams:
    """``sed -n 1,120p dump.sql``: long INSERT rows, ~8 KB each."""
    nl = _nl(params)
    n = int(params.get("lines", 120))
    row_bytes = int(params.get("line_bytes", 8192))
    lines = []
    for i in range(n):
        values = []
        while sum(len(v) for v in values) < row_bytes:
            values.append(f"({i},'{_ident(rng, 12)}','{_words(rng, 6)}',{rng.randint(0, 99999)})")
        lines.append(f"INSERT INTO ledger_entries VALUES {','.join(values)};"[:row_bytes])
    prefix = lines[0][:60]
    return Streams(stdout=nl.join(lines) + nl, exit_code=0, keep={"first_prefix": prefix})


def git_status_long(params: dict, rng: random.Random) -> Streams:
    """``git status``: many modified paths. Git diagnostics are exempt from
    the auto-filter, so every line must come through."""
    nl = _nl(params)
    n = int(params.get("files", 60))
    lines = ["On branch main", "Changes not staged for commit:", ""]
    lines += [f"\tmodified:   src/{_WORDS[i % len(_WORDS)]}/{_ident(rng, 6)}.py" for i in range(n)]
    lines += ["", 'no changes added to commit (use "git add" and/or "git commit -a")']
    return Streams(stdout=nl.join(lines) + nl, exit_code=0,
                   keep={"middle": lines[3 + n // 2], "last": lines[-1]})


def empty_output(params: dict, rng: random.Random) -> Streams:
    """A silent success: no stdout, no stderr."""
    return Streams(stdout="", stderr="", exit_code=int(params.get("exit_code", 0)))


def fail_exit_code(params: dict, rng: random.Random) -> Streams:
    """A short failing command whose exit code must be named in the header."""
    nl = _nl(params)
    code = int(params.get("exit_code", 2))
    err = str(params.get("stderr", "make: *** [Makefile:12: build] Error 2"))
    return Streams(stdout=f"building target{nl}", stderr=err + nl, exit_code=code,
                   keep={"stderr": err})


GENERATORS = {
    "minified_grep_hit": minified_grep_hit,
    "pytest_buried_failure": pytest_buried_failure,
    "tsc_errors": tsc_errors,
    "npm_build_noise": npm_build_noise,
    "cr_progress_bar": cr_progress_bar,
    "jsonl_grep": jsonl_grep,
    "python_traceback": python_traceback,
    "cargo_errors": cargo_errors,
    "jest_failure": jest_failure,
    "pytest_three_failures": pytest_three_failures,
    "unittest_failures": unittest_failures,
    "ansi_cr_progress": ansi_cr_progress,
    "identical_flood": identical_flood,
    "binary_garbage": binary_garbage,
    "ansi_colored": ansi_colored,
    "huge_stderr_empty_stdout": huge_stderr_empty_stdout,
    "mixed_streams_small": mixed_streams_small,
    "under_budget_120_lines": under_budget_120_lines,
    "timeout_partial": timeout_partial,
    "sed_long_sql_lines": sed_long_sql_lines,
    "git_status_long": git_status_long,
    "empty_output": empty_output,
    "fail_exit_code": fail_exit_code,
}


def generate(name: str, params: dict | None = None, *, case_id: str = "") -> Streams:
    """Run generator ``name`` with ``params`` under a deterministic seed."""
    try:
        fn = GENERATORS[name]
    except KeyError:
        raise KeyError(f"unknown generator {name!r}; known: {', '.join(sorted(GENERATORS))}")
    params = dict(params or {})
    rng = random.Random(seed_for(case_id or name, params))
    return fn(params, rng)
