"""Claude Code subagents for same-provider downshift, installed by ``c3 install-mcp``.

Measured 2026-09-14 over ~/.claude/projects transcripts since 2026-07-01: 963
of 991 ``Agent`` calls left ``model`` unset, so 92% of subagent turns ran on
the parent's Opus/Fable-class model. A subagent definition with ``model:`` set
is the one place Claude Code downshifts on its own. C3 ships two:

- ``c3-scout`` (haiku): read-only lookups. On the delegate eval a small tier
  passed every bounded core case at a fifth of the large tier's cost.
- ``c3-worker`` (sonnet): well-specified, bounded changes.

Both run inside the parent session, so C3's hooks — Access Guard, edit
ledger, discipline — apply to them exactly as to the parent. ``c3_delegate``
covers the one-shot calls on every host; these cover multi-step work in
Claude Code.

A file carrying ``MARKER`` is C3's to rewrite; one without it is the user's
and is never touched.
"""

from __future__ import annotations

from pathlib import Path

MARKER = "<!-- c3-managed: delegate-agents -->"

_SCOUT = f"""---
name: c3-scout
description: Use for bounded, read-only lookups on a small, cheap model — where is X defined, who calls Y, what does this file or diff do, which config key controls Z. Returns a few lines with file:line references. Never edits and never runs commands.
tools: Read, Grep, Glob, mcp__c3__c3_search, mcp__c3__c3_read, mcp__c3__c3_filter, mcp__c3__c3_memory
model: haiku
---
{MARKER}

You are c3-scout, a read-only lookup agent working for a larger model. Answer
the one question you were given, then stop.

- Find before you read: `c3_search` (or Grep/Glob), then `c3_read` on the
  file for its map, then only the symbols or lines you need.
- Never edit files and never run commands.
- Answer in a few lines with `path:line` references. Quote code only when the
  question asks for it.
- If a read is refused, that path is off limits: say so, do not work around it.
- If the answer is not in the repository, say that. Do not guess.
"""

_WORKER = f"""---
name: c3-worker
description: Use for well-specified, bounded code changes on a mid-tier model when the parent already knows what to change — apply a described edit across named files, add tests for a given function, fix a lint or type error with a known cause. Give it the files, the intended behaviour and how to verify.
model: sonnet
---
{MARKER}

You are c3-worker, an implementation agent working for a larger model. You get
a change that is already decided: the files, the behaviour and how to check it.

- Stay inside that scope. If the change needs more than you were given, stop
  and report what and why instead of widening it.
- Look with `c3_search` / `c3_read`, change with `c3_edit`, run `c3_validate`
  after every edit, and run tests with `c3_shell`.
- Run the tests named in the task, or the nearest existing ones, before
  reporting.
- Report: files changed, the test command and its result, anything left undone.
"""

AGENTS = {"c3-scout": _SCOUT, "c3-worker": _WORKER}


def install_delegate_agents(project_path) -> list[tuple[str, str]]:
    """Write ``.claude/agents/c3-scout.md`` and ``c3-worker.md``.

    Returns ``[(path, action)]`` with action ``wrote`` (new), ``updated``
    (C3's file, content changed), ``kept`` (C3's file, already current) or
    ``skipped`` (a file without the marker — the user's; never overwritten).
    """
    agents_dir = Path(project_path) / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for name, content in AGENTS.items():
        path = agents_dir / f"{name}.md"
        if path.exists():
            existing = path.read_text(encoding="utf-8", errors="replace")
            if MARKER not in existing:
                results.append((str(path), "skipped"))
                continue
            if existing.strip() == content.strip():
                results.append((str(path), "kept"))
                continue
            path.write_text(content, encoding="utf-8")
            results.append((str(path), "updated"))
        else:
            path.write_text(content, encoding="utf-8")
            results.append((str(path), "wrote"))
        _note_write(project_path, path)
    return results


def _note_write(project_path, path: Path) -> None:
    """Attribute the write to install_mcp so artifact capture does not report
    it as out-of-band drift (same contract as write_c3_instruction_doc)."""
    try:
        from services.artifact_defs import note_pending_write

        root = Path(project_path)
        if (root / ".c3").is_dir():
            note_pending_write(root, str(path.resolve().relative_to(root.resolve())), "install_mcp")
    except Exception:
        pass
