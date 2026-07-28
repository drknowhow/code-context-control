# README screenshot rig

Regenerates every screenshot the README embeds, against a synthetic demo
project. One command:

```bash
python -m scripts.screenshots.run --out docs/screenshots/<yyyy-mm>
```

## Why it exists

Screenshots rot silently. The set this replaced was captured 2026-04-27 and
was still in the README at v2.63.0 — 34 releases later. It showed an 8-tab
sidebar (live: 12), a Hub generation that had been retired and moved to
`/legacy`, and an IDE picker offering a Gemini CLI profile that was removed in
v2.52. Nothing failed; the images just quietly described a different product.

## What each step guards against

**Never capture a real project.** The Hub renders every project name and
absolute path in `~/.c3/projects.json`; Chat renders verbatim AI conversations;
Memory renders stored facts; Instructions renders the full CLAUDE.md; Access
Guard rules name your sensitive directories. `demo_world.py` builds a fictional
"Acme" world instead, and `run.py` swaps the Hub registry down to just those
projects.

**Always restore the registry.** The swap is backed up to
`~/.c3/projects.json.PRE-SCREENSHOT-BAK` before anything else and restored in a
`finally` block, so a crash mid-run cannot leave your registry replaced. If a
run is killed hard:

```bash
python -m scripts.screenshots.run --restore-only
```

**Never trust a running server.** `cli/server.py:234` builds the UI bundle on
first request and never invalidates it, and Flask runs with `debug=False` — so
a long-lived server serves the code it booted with, forever. `capture.py`
asserts a symbol from the newest component (`MASK_EMPTY_FORM`, added v2.63.0)
is present in the *served* HTML and aborts if it isn't. This is not
hypothetical: the server running on this machine during development was
serving a pre-Mask-Guard bundle.

**Never assume a port.** `find_free_port()` silently walks 3333 → 3334 → 3335
when a port is taken, and `run_hub()` returns without starting if a hub already
owns the port. `run.py` parses the port from each server's own startup banner.

**Never wait on a timer.** `ui.html` loads React and babel-standalone from CDN
and compiles JSX in the browser. Capture gates on `#root` having children and
`document.fonts.status === 'loaded'`, then on the target tab actually becoming
active.

## Files

| File | Role |
|---|---|
| `run.py` | Orchestrator. Backup → build → swap registry → seed → capture → restore. |
| `demo_world.py` | Generates the fictional Acme projects and `c3 init`s them. |
| `seed_sessions.py` | Session history + the live-session activity trail. Runs partly before the server starts. |
| `seed.py` | Memory facts, tasks, credentials, access/mask rules, edit ledger — over the REST API the UI itself uses. |
| `capture.py` | Playwright capture with the freshness gate. |

## Notes

- Output is 2880×1800 (1440×900 at `deviceScaleFactor: 2`) so it stays crisp
  when the README renders it at `width="900"`. Hub views use a 680px-tall
  viewport because their content is short.
- Dark theme only. The per-project theme is React state with no persistence;
  the Hub's is server-side config (`/api/hub/config`).
- Give changed screenshots **new filenames**. PyPI proxies README images
  through its own camo cache keyed on the URL string, so overwriting a file in
  place can leave the old image on the PyPI page indefinitely. That is why
  output goes in a dated folder.
- The demo world lands in `C:\c3-demo` (or `$C3_DEMO_ROOT`) rather than under
  the home directory, so no developer username appears in a published
  screenshot.
