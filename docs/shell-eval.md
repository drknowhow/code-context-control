# Shell output evaluation — `c3 shell-eval`

A harness for what `c3_shell` hands back to the agent. It exists so the
remediation phases (budget and spill in S1, content-aware keep in S2) can be
argued with numbers, and so a regression in the response body has somewhere
to fail. Until this suite the shell tests covered wiring (blocked patterns,
credential injection, ledger logging); a change that dropped the failing
test's name from a 5,000-line pytest run passed every one of them.

## What it measures

Every case is a synthetic command output — `(stdout, stderr, exit_code,
timed_out)` from a seeded generator in `tests/shell_eval/generators.py` —
rendered through `cli.tools.shell.render_shell_response`, the pure function
`handle_shell` calls after the subprocess returns. No process is spawned and
nothing large is committed: 2 MB of binary is a seed and a few lines of
code, identical on every run and every machine.

Per case:

| Metric | Meaning |
|---|---|
| `response_bytes`, `response_tokens` | size of the body the agent reads (UTF-8 bytes; cl100k tokens) |
| `retention` | share of the case's `must_contain` fragments present in the body (the must-keep set) |
| `render_ms` | wall time of the renderer, filter included |
| `stdout_bytes`, `stderr_bytes`, `longest_line` | what came in, before any filtering (from the renderer's stats) |
| `filtered`, `spilled`, `output_id` | what the renderer did (`[F]` / `[S]` in the table) |

Aggregates:

| Metric | Meaning |
|---|---|
| `pass_rate_must_pass` / `pass_rate_xfail` / `pass_rate_info` | share of cases passing, per gate |
| `under_budget_rate`, `budget_violations` | share of cases at or under their `max_bytes`, and the ids over it |
| `must_keep_retention` | mean retention over cases that name must-keep fragments; `_must_pass` restricts it to the must_pass gate |
| `bytes_p50`, `bytes_p95`, `tokens_p50`, `tokens_p95` | response size distribution |
| `render_ms_p50`, `render_ms_p95` | renderer cost (informational; never gated) |
| `by_phase` | per S1 / S2: how many of the cases that phase owns pass today |

Sizes and retention are deterministic (seeded input, deterministic
renderer); render time is not and carries no limit.

## Budget

The S1 response budget is **18 KiB** (`DEFAULT_MAX_BYTES = 18432`), the
byte size of the whole body — header, command echo, both streams, every
trailing section. A case may set `checks.max_bytes` below it, never above
the **22 KiB ceiling** (`CEILING_MAX_BYTES = 22528`); the loader refuses a
case that tries. The suite header records the default so a reader of the
JSONL sees the number without opening the module.

At 2.111.0 six of twenty cases exceeded the budget, five of them by two
orders of magnitude: a 900 KB grep over minified lines, 2 MB of JSONL hits,
1 MB of stderr, a 3.8 MB no-newline blob, a 160 KB progress bar. Since
2.112.0 (S1: byte budget + spill, `cli/tools/shell_render.py`,
`services/shell_output.py`) none does: the five S1 cases are `must_pass`
and bytes p95 went from 1,186,398 to 4,750. The remaining `xfail` cases were
S2's (content-aware keep: CR progress bars, ANSI, a buried jest failure, a
middle line the legacy filter drops); 2.113.0 closed them.

## S2 — content-aware keep (2.113.0)

The rule, from the Cod review of 2026-09-04: *always strip ANSI/control
sequences and collapse carriage-return progress updates; otherwise preserve
complete under-budget output; run deterministic parsers always to identify
priority regions, but only omit content when over budget; pytest/unittest,
cargo/rustc, tsc and Jest/Vitest first, everything else generic error
anchors plus head/tail; collapse only consecutive normalised duplicates.*
`cli/tools/shell_parsers.py` implements it; `shape_stream` in
`cli/tools/shell_render.py` consumes it; the legacy `>30 newlines →
handle_filter` call is gone from `render_shell_response` (the `c3_filter`
tool is untouched).

**Always normalised** (both streams, full text or the head/tail previews of
a stream too large to hold): `\r\n` becomes `\n` first, so a Windows line
ending is never mistaken for a rewrite; ANSI escape and control sequences
(CSI, OSC, single-character escapes, C0 controls other than tab and
newline) are stripped — not a loss, nothing the agent reads is gone, so no
note and no spill; each logical line keeps only the final state of its `\r`
rewrites; runs of three or more identical consecutive lines fold to the
first plus ` [x N]`. The last two ARE losses: the header carries
`[collapsed 1999 cr rewrites, 199 dup lines]`, `stats.filtered` is set and
the raw streams are spilled. Under budget the duplicate test is exact; once
a stream is over its allocation it is widened to lines that differ only in
digits, hex runs or timestamps (`fuzzy_dups`), because omission is happening
anyway. `filter_output=False` skips both collapses (ANSI is still stripped)
and, as in S1, never lifts the byte cap.

**A priority region** is a 0-based inclusive line range with a reason —
`(start, end, why)` — that must survive shaping, listed most important
first. `priority_regions(runner, lines)` runs on every call; when the
stream fits its allocation the regions change nothing. Over budget,
`shape_stream` keeps regions first (a region that does not fit whole is
skipped so a smaller one still can), announces each one that is not
contiguous with what was already kept by a one-line `[L2410-2422: pytest
failure test_x]` note, spends what is left on head and tail as before, and
writes one omission note with the missing line and byte counts and the
output id. Line numbers in notes are RAW numbers (the fold keeps a map), so
`output_action='read', lines='2410-2422'` pages the right window.

**Runners** (`detect_runner`: the output's own signature wins, the command
head decides when the output says nothing):

| runner | priority regions | summary head |
|---|---|---|
| pytest | `= N failed … in Xs =` line; `short test summary info` + its FAILED/ERROR lines; each `___ test ___` block through its assertion and location line (long blocks: header + last 15); verbose `FAILED` progress lines | `pytest: 1 failed, 3366 passed … in 240.12s` + `failed: id — reason` |
| unittest | `Ran N tests` + verdict; each `FAIL:`/`ERROR:` block through the exception line; `… FAIL` progress lines | `unittest: Ran N tests in Xs; FAILED (failures=1)` + one line per header |
| cargo / rustc | verdict lines (`could not compile`, `generated N warnings`, `test result:`); each `error[E…]` block with its `-->`; `test x ... FAILED` and panic blocks; up to five warnings | `cargo: <verdict>` + `error[E0308]: … — file:line:col` |
| tsc | every `file(l,c): error TSnnnn:` line, first and last before the rest; `Found N errors` | `tsc: Found N errors` + first twenty |
| jest / vitest | totals block; each `●` block through Expected/Received and the `>` code frame (vitest: `FAIL file > name` through the `❯` frame); `FAIL suite` lines | `jest: Test Suites: …; Tests: …` + `failed: name — Expected / Received` |
| (none) | lines matching Traceback / ERROR / `ERR!` / Error / error / FAIL / panic / exception / fatal, two lines of context, last and first anchors first, capped at 40, at most 60% of the allocation | no summary |

The `--- summary ---` section is appended whenever a runner is recognised
and the output has more than 30 lines, and is counted inside the budget
(streams are allocated what is left after it). Caps: 20 failure blocks per
runner, 20 lines in the summary list (then `… and N more`).

**The +10% band.** Three `must_pass` cases were shaped by the legacy filter
at 2.112.0 and are shaped by the budget now; Cod's rule preserves complete
output under budget and fills the budget above it, so they grew:
`pytest_buried_failure` 808 → 16,253 B, `tsc_errors` 856 → 14,267 B,
`npm_build_noise` 303 → 16,325 B. The suite's `bytes_p50` ceiling was raised
to 4,096 with the S2 baseline. Whether the live token profile of the
30-line-to-18-KiB band moves by more than +10% is a telemetry question
(`shell_by_class` in `.c3/tool_telemetry.jsonl`, S0), decided after the
overlay — not something the harness can settle.

## Gates and phases

Each case declares one of:

- `must_pass` — a failure fails CI. Reserved for what works today and must
  keep working: small outputs render verbatim, `TIMEOUT` / `FAIL(n)` headers,
  git diagnostics exempt from the filter, compiler walls keeping first and
  last error, the pytest-aware collapse keeping the failing id, summary and
  last frame.
- `xfail` — known broken; `phase` names the remediation phase that fixes it.
  A pass is reported as a warning so the case can be promoted and the
  baseline refreshed.
- `info` — measured, never gates.

`phase` maps a case to the work that closes it:

- **S1 — budget and spill.** The body fits `max_bytes` no matter what came
  in; the raw streams are spilled to disk and named by `output_id`; a clip
  keeps the match / the first row / the stderr tail and never lands a marker
  inside a JSON object line. Cases: `minified_grep_hit`, `jsonl_grep`,
  `binary_single_line`, `huge_stderr_empty_stdout`, `sed_long_sql_lines`.
- **S2 — content-aware keep.** What survives is chosen by what the agent
  needs, not by line count: the final state of a `\r` progress bar, the
  failing test's name in a jest report, the middle of 120 plain lines that
  fit the budget with room to spare, ANSI escapes stripped from short
  colored output. Cases: `cr_progress_bar`, `jest_failure`,
  `under_budget_120_lines`, `ansi_colored` — all `must_pass` since 2.113.0,
  joined by `pytest_three_failures`, `cargo_two_errors`, `ansi_cr_progress`,
  `identical_flood`, `tsc_wall_900`, `unittest_failures` (see § S2).

When a phase lands: run `c3 shell-eval`, confirm its cases read `XFAIL
PASSING`, flip them to `must_pass` (drop `phase`), then
`--update-baseline` and raise the floors (`under_budget_rate` to 1.0 after
S1, `must_keep_retention` after S2) and lower `bytes_p95` to the 22 KiB
ceiling — deliberate, reviewed edits, not something a run does on its own.

## Checks vocabulary

```json
"checks": {
  "max_bytes": 18432,
  "must_contain": ["$token", "[c3_shell:OK]"],
  "must_not_contain": ["$mid", "\u001b["],
  "must_contain_regex": ["\\[c3_shell:FAIL\\(2\\)\\]"],
  "no_marker_inside": "json",
  "spill_identical": true
}
```

- `max_bytes` — response budget for this case (default 18432; ceiling 22528).
- `must_contain` — every fragment must appear in the body. This list is the
  case's must-keep set and is what `retention` measures. A value starting
  with `$` refers to a fragment the generator returns in `Streams.keep`
  (`$token`, `$summary`, `$last_frame`), so the suite never pastes generated
  text; anything else is literal.
- `must_not_contain` — none may appear (same `$` resolution).
- `must_contain_regex` — Python regexes, `re.MULTILINE`.
- `no_marker_inside` — `json` or `table`: an omission marker (a bracketed
  note saying `omitted`, `collapsed`, `truncated`, `clipped`, `elided`,
  `spilled`, `repeated` or `retained`; `MARKER_RE`) may not share a line with
  the content it interrupts — object syntax for `json`, a `|` separator for
  `table`. A marker on its own line is fine.
- `spill_identical` — only meaningful once the renderer reports
  `spilled=True`: the spilled text must equal the raw streams. Before S1 it
  evaluates as a failure with the reason `not spilled (pre-S1)`, which is
  why such a case is gated `xfail S1`.

## Case format

```json
{"id": "jsonl_grep", "cmd": "grep corr-9f1e2d3c events.jsonl", "generator": "jsonl_grep",
 "params": {"hits": 40, "line_bytes": 50000, "key": "correlation_id", "value": "corr-9f1e2d3c"},
 "gate": "xfail", "phase": "S1", "filter_output": true,
 "checks": {"must_contain": ["$key"], "no_marker_inside": "json"},
 "tags": ["long-line", "jsonl"], "why": "..."}
```

- `cmd` is what the renderer sees: it decides the runner hint (when the
  output carries no signature), the grep focus for clipping and the header
  echo, nothing else.
- `generator` names a function in `tests/shell_eval/generators.py`;
  `params` are its knobs (sizes, counts, `crlf: true` for Windows line
  endings, `seed` to pin the RNG instead of the CRC32 of the id).
- `filter_output` is passed straight to the renderer (since 2.113.0:
  `false` skips the CR and duplicate collapses; ANSI is always stripped and
  the byte cap always holds).
- `phase` is `S1`, `S2`, or `null`; required when `gate` is `xfail`.

## Adding a case

1. Add or extend a generator. Randomness goes through the `rng` argument
   only; put every fragment a check will name into `Streams.keep`.
2. Append a line to `tests/shell_eval/fixture_suite.jsonl`. Gate by what the
   renderer does *today*: `must_pass` if it passes and must keep passing,
   `xfail` with the phase that will fix it if it fails, `info` to watch.
3. `c3 shell-eval --update-baseline`, commit both files.
   `test_baseline_covers_every_case` fails until the baseline knows the id;
   `test_must_contain_refs_resolve` fails on a `$name` the generator does
   not provide.

Two cases in the original spec were expected to fail today and did not,
once measured: the pytest-aware collapse keeps the failing id, the summary
and the last frame of a 5,000-line run (`pytest_buried_failure`), and 2 MB
of random bytes has a newline every 256 bytes on average, so the line-count
trigger fires and the filter shrinks it to under a kilobyte
(`binary_garbage`). Both are gated `must_pass` as regression guards; the
no-newline blob that really reaches the agent whole is `binary_single_line`
(`xfail S1`). The jest report went the other way: today's filter drops the
`● name` block as "non-error lines", so `jest_failure` is `xfail S2`.

## Workflow

```
c3 shell-eval                          # fixture suite, table + verdict, exit 1 on violation
c3 shell-eval --json > report.json
c3 shell-eval --update-baseline        # after an intentional change to the renderer
c3 shell-eval --update-baseline --floors '{"under_budget_rate": 1.0}' --ceilings '{"bytes_p95": 22528}'
pytest tests/test_shell_eval.py -q     # what CI runs
```

Floors (`pass_rate_must_pass`, `under_budget_rate`, `must_keep_retention`,
`must_keep_retention_must_pass`) are values that must not fall; ceilings
(`bytes_p50`, `bytes_p95`, `tokens_p95`) are values that must not grow.
Both live in `tests/shell_eval/baseline_fixture.json`, were hand-set when
the suite was created (three points under the measured rates; sizes just
above today's), and are kept as-is by `--update-baseline` unless passed
explicitly.

## The renderer under test

`render_shell_response(cmd, result, svc, *, filter_output=True, warn="",
capped_note="", touched_files=(), cred_names=(), swept_ghosts=())` returns
`(body, stats)`. `result` is the dict `_run_sync` returns; `stats` carries
`stdout_bytes`, `stderr_bytes`, `longest_line` (measured before filtering),
`filtered`, `spilled`, `output_id`, `response_bytes`, `response_tokens`.
The harness builds the `svc` it needs itself (`build_eval_svc`: a temp
project path so a spill never lands in a real `.c3`, and the real
`OutputFilter` with the LLM pass disabled) and reports which renderer ran
in the first line of the table (`renderer=`).

The renderer is `cli.tools.shell.render_shell_response` — the function
`handle_shell` itself calls after the subprocess finishes (same
normalisation, same parsers, same budget, same sections in the same order).
Since 2.113.0 `stats` also carries `runner`, `ansi_stripped`,
`cr_collapsed`, `dup_collapsed` and `priority_lines`. There is no second
implementation to drift.
