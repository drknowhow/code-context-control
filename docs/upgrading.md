# Upgrading C3

```bash
c3 upgrade                          # upgrade the running install in place
c3 upgrade --check                  # just report whether a newer release exists

# equivalently
pipx upgrade code-context-control
pip install -U code-context-control
```

MCP is wired through the `c3-mcp` entry point, so upgrading needs **no
per-project reconfiguration** — existing `.mcp.json` files keep working. C3
also nudges you in-app when a newer release is available.

## Version-specific notes

### From before v2.60.1 — Windows only

Existing projects are **not** repaired by upgrading. Hook registration written
by older Windows installs is inert, which silently disables the edit ledger and
the PreToolUse guards. Re-run init once per project:

```bash
c3 init /path/to/project --force
```

This preserves your config; it rewrites only C3-managed blocks.

### From before v2.52.0 — Gemini CLI profile removed

The `gemini` IDE profile no longer exists. Use `--ide antigravity`, which reads
`AGENTS.md`. An existing `GEMINI.md` is still read if present, but C3 no longer
generates or syncs it.

### Stop the MCP server first

Stop any running `c3-mcp` server or CLI before `c3 upgrade`. A live process can
hold package files open, which leaves pip's `~`-prefixed backup directories
(`~ervices`, `~ools`, …) in `site-packages`. Those are inert and safe to delete
once the upgrade completes.

## Large repositories

The embedding index dominates init time on big trees. Skip it, or cap what gets
indexed:

```bash
c3 init /path/to/huge-repo --force --no-embed
```

```jsonc
// .c3/config.json
{
  "index_max_files": 2000   // default
}
```

Init reports live progress, so a long index build is visible rather than
looking hung.

## From source (contributors)

```bash
git clone https://github.com/drknowhow/code-context-control.git
cd code-context-control
pip install -e ".[dev]"             # tests, linters, build tools
```

Run the suite the way CI does — note it is **two** runners, not one:

```bash
pytest && python smoke_test.py
```

Regenerating the README screenshots is a single command; see
[`scripts/screenshots/README.md`](https://github.com/drknowhow/code-context-control/blob/main/scripts/screenshots/README.md).
