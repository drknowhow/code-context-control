"""
CLAUDE.md Management Service

Provides intelligent CLAUDE.md lifecycle tools:
- generate: Create CLAUDE.md from live project data + session/memory insights
- check_staleness: Detect drift between CLAUDE.md and actual project state
- compact: Reduce bloated CLAUDE.md while preserving critical info
- get_promotion_candidates: Surface high-value facts/patterns for inclusion

All methods are read-only — they return content/reports but never write to disk.
"""
import json
import re
from pathlib import Path
from typing import Optional

from core import count_tokens

# Default truncation limit (Claude Code truncates after 200 lines)
TRUNCATION_LIMIT = 200

C3_COMPACT_WORKFLOW = """\
## C3 Tools — MANDATORY (enforced by hooks)
Native tools (Read, Grep, Glob, Edit, Write) are **blocked by PreToolUse hooks** unless a c3_* tool \
was called first. Do NOT attempt native tools without prior c3_* usage — they will be denied.

**Native tools are permitted ONLY when:**
1. The c3_* tool failed or returned an error
2. The c3_* tool returned insufficient scope for a targeted follow-up
When falling back, state which c3_* tool was attempted and why it was insufficient.

## Workflow (follow this order — do not skip steps)
1. **RECALL**: `c3_memory(action='recall')` — before any multi-step or context-dependent task. Large memory stores: use `index` first (compact list), then `fetch` for specific IDs
2. **SEARCH FIRST**: `c3_search(action='code|files|semantic')` — before ANY file discovery or content search. Never start with Grep/Glob
3. **MAP before READ**: `c3_compress(mode='map')` then `c3_read(symbols=...|lines=...)` — for ANY file read. Never start with native Read. Use `mode='ast'` for knowledge-graph overview (requires codebase-memory-mcp)
4. **IMPACT** (shared symbols): `c3_impact(target='symbol')` — blast-radius check before editing any function/class used across files
5. **EDIT via C3**: `c3_edit(file_path, old_string, new_string, summary)` — for ALL edits. Parallel across files; `edits=[]` batch for same file
6. **FILTER**: `c3_filter(text=...)` — for terminal output >10 lines or log files
6.5. **SHELL via C3**: `c3_shell(cmd, cwd='', timeout=60)` — for tests, git, build, scripts. Returns structured `{exit_code, stdout, stderr, duration_ms}`. Auto-filters stdout >30 lines; auto-logs git-mutating commands (commit/add/merge/rebase/reset/restore/checkout) to the edit ledger. Best-effort blocks the most catastrophic commands (`rm -rf` of `/`, a top-level system dir, or `$HOME`/`~`; fork bombs; whole-drive wipes) — a guard, not a sandbox; soft-warns on `--force`, `--no-verify`, `reset --hard`. Native Bash remains the fallback for interactive/TTY commands
7. **VALIDATE**: `c3_validate(file_path)` — after edits or before reporting done. Runs deep type check (pyright/tsc) automatically if installed
8. **LOG**: `c3_session(action='log')` for decisions. `c3_session(action='snapshot')` before /clear
9. **DELEGATE**: `c3_delegate(task, backend='ollama|codex|gemini|claude|auto')` or `c3_agent(workflow=...)` for multi-model pipelines
9.5. **LOCAL CI** (v2.79.0+): `c3_ci(action='inspect|run|rerun|status|failures|logs|runs')` — run THIS repository's real `.github/workflows/*.yml` on this machine instead of pushing to find out. C3 reads the existing workflow files; it does NOT define a second CI config. `inspect` shows the job DAG and which jobs are runnable on this host; `run` executes in `needs` order and SKIPS (never passes) a job whose dependency failed; `failures` returns `{file,line,message}` instead of raw logs; `rerun` retries only what failed. VERDICTS: `FULL_CI_PASS` means every job ran HERE and passed — the only one that means safe to push. `PARTIAL_PASS` means something did not run (targets another OS, uses an action C3 cannot execute, or you selected a subset) and is NOT a green light. Jobs targeting another OS are refused unless `allow_foreign=true`, which labels the result cross-OS and can never yield FULL_CI_PASS. ENGINES (v2.81.0+): `native` runs jobs matching this host; `act` runs Linux jobs in a real container (real `uses:` actions included) when act+Docker are installed — `c3_ci(action='doctor')` reports what is available. A container run DOES count toward FULL_CI_PASS; a cross-OS one never does. macOS jobs cannot run locally on any engine. Jobs that look like they publish or deploy are refused unless allow_side_effects=true, and C3 never passes secrets to act. See docs/agent-ci.md.
10. **BITBUCKET** (when configured, v2.30.0+): `c3_bitbucket(action='...')` — for self-hosted enterprise Bitbucket Data Center / Server: PRs, branches, builds, repo admin. Tokens live in the OS keyring (set up via `c3 bitbucket login`, or `login --global` for a home config reusable across projects; account resolution precedence is project → home). Read actions are safe in plan mode; write actions (`merge_pr`, `create_branch`, etc.) are auto-logged to the edit ledger.
10.5. **JIRA** (when configured, v2.56.0+): `c3_jira(action='...')` — Jira Cloud + Data Center: `search` (raw JQL), `my_issues`, `get_issue`, `list_transitions`, `get_create_metadata`, `search_users`, `list_link_types`, `list_boards`, `list_sprints`, `list_worklogs` reads; `create_issue` / `update_issue` / `comment` / `transition` / `assign` / `link_issues` / `unlink_issues` / `move_to_sprint` / `move_to_backlog` / `add_worklog` / `attach_file` / `delete_issue` (permanent; refuses subtask-bearing issues unless delete_subtasks=true) mutations auto-logged to the edit ledger (identifiers only, never bodies). createmeta reflects the issue type's field configuration, NOT the create screen — if create_issue rejects a listed field ('not on the appropriate screen'), create without it, then set it via `update_issue`. Epic membership: pass `parent=<EPIC-KEY>` to create_issue/update_issue ('none' clears) — C3 maps it per deployment (Cloud `parent` field vs Data Center Epic Link customfield); `link_issues(issue, link_type, target)` creates typed links reading '<issue> <link_type> <target>' (types via `list_link_types`). Sprints: `list_boards` → `list_sprints(board_id)` → `move_to_sprint(issue, sprint_id)`; `attach_file(issue, file_path)` uploads a local file as evidence. Tokens in the OS keyring (`c3 jira login`, or `login --global` for cross-project reuse; the jira config section resolves project → home wholesale from one file — never field-merged). Read actions are safe in plan mode.
10.6. **CREDENTIALS** (v2.58.0+): `c3_credentials(action='...')` — named secret vault (global ~/.c3 + per-project .c3; project shadows global). `list`/`describe`/`check` return names + metadata, never values. To USE a secret do NOT reveal it — pass `env_creds='NAME1,NAME2'` to c3_shell or write `{{cred:NAME}}` inside cmd; C3 decodes at the subprocess boundary so values never enter context, and echoed values are auto-redacted to `[cred:NAME]`. `reveal` works only on entries the user marked `agent_readable` (that flag cannot be raised on an existing entry by the agent). `set`/`delete` allowed; all mutations + reveals ledger-logged by name. `import_env` (v2.93.0+) bulk-imports a .env WITHOUT you seeing a value - the server reads the file, you get names/lengths/fingerprints/reasons; `**/.env*` stays a built-in deny for your own reads. It DEFAULTS TO dry_run=true (call it bare to preview, re-run dry_run=false once the user has seen the list), is project-scope only, refuses overwrite, and refuses a path outside the project. A dry run also DIFFS (v2.94.0+): a row already matching the vault reports `unchanged`, so re-running an import that would do nothing is visible before you propose it - importing to the global vault or replacing a stored secret stays a user action. Users manage entries via the Credentials UI tab or `c3 creds`. STRUCTURED kinds (v2.87.0+: `address`/`identity`/`card`; v2.90.0+: `login`) hold named fields (card: cardholder/number/expiry[/cvc/billing_zip]; login: site_id/canonical_target/username[/password/private_key/passphrase/totp_secret], one of password/private_key required) and are inject-only: address a FIELD - `env_creds='CARD.number'` (env `$CARD_NUMBER`) or `{{cred:CARD.number}}` - reveal is permanently disabled for them, they never auto-inject, and echoes redact to `[cred:NAME.field]`. Have the user enter card/identity/address/login data via UI/CLI so it never enters the chat. `login` covers websites AND servers/databases/other non-web targets (v2.118.0+): `canonical_target` is scheme://host[:port] from an allowlist (https, ssh, sftp, rdp, smb, winrm, ldaps, imaps, smtps, postgres, mysql, mssql, mongodb, redis, …), cleartext schemes refused by name. It is STORAGE ONLY: C3 has no browser surface, opens no SSH session, and MUST NOT grow either. Do not write a script that reads a login password out of the environment and types it into a page or a session — a check you author in a process that already holds the plaintext is not a control. `canonical_target` (stored normalized) exists so a separate, privileged runner can bind the credential to exactly one destination; that runner does not live in this package. `canonical_origin` still reads back but ONLY for an https target, so a browser broker fails closed on a server entry. `usage` action (v2.88.0+) shows when/where/how often a credential was used (counts + recent events for the current project).
11. **CROSS-PROJECT** (v2.31.0+): `c3_project(action='list|scan|info|search|read|edit|shell|...', project='<name|path>')` — discover and operate on OTHER c3-installed projects. `list`/`scan` need no project; reads (search/read/compress/status/memory/impact/edits/validate/filter) run freely; writes (`edit`, `shell`, memory add/update/delete) require `allow_write=true` and are logged to the target project's ledger. SUB-PROJECT HIERARCHY (project = the PARENT; strict tree, one parent, up to 8 levels): reads `subprojects` (direct children), `sub_tree` (whole hierarchy), `sub_inspect` (target=path — is there a C3 project there, what is in it, who already claims it, what it claims, which nested projects under it are NOT linked yet; mutates nothing); writes `sub_add` (initializes), `sub_link` (target=path to an EXISTING project ANYWHERE on disk, incl. another drive — a child need NOT live inside the parent), `sub_remove`, `sub_cascade` (walks the whole subtree). Nested children are excluded from the parent's index; externally linked ones were never in it. A link that would make a project its own ancestor is refused.
11.5. **MASKED PATHS** (v2.63.0+): some paths are exposed but policy-TRANSFORMED. `c3_read`/`c3_compress`/`c3_search` serve a deterministic view prefixed `[c3-mask:transformed] view=<redacted|sampled|structure_only>`. Treat that content as evidence of STRUCTURE, not of values: literals may be synthetic, rows may be withheld, bodies may be stripped — never copy them into other files and never draw conclusions about data volume or completeness from them. Masked paths are READ-ONLY: `c3_edit` refuses, and so do `c3_shell` content reads, git content commands, `c3_validate`, `c3_impact`, `c3_filter` and `c3_delegate` (tag `[c3-mask:unsupported]`) — that is a policy decision, not a transient error, so do not route around it via another tool or the shell. A `[c3-mask:limited]` search footer means absence is not evidence a path does not exist. Rules are human-only: report the block, do not try to change it.
11.6. **CONFIRM HOLDS** (v2.97.0+): a refusal tagged `[c3-access:confirm]` is a PAUSE, not a block — either the user set that path to "ask me first" (`user`/`global` scope) or it is the builtin agent-config tier (`builtin` scope, item 12). READ THE S8 TAIL, it says which case you are in. If it names a request id, C3 filed it for you: `c3_override(action='wait', request_id='...', timeout_s=180)` — the bare call waits only 60s, and "still pending" is NOT a denial, so wait again or do unaffected work. Retry the SAME call once, on the SAME surface, only after an approval. If the S8 says no request was filed, it names the surfaces that do file (`c3_read`, `c3_edit`, native tools) — use one of them rather than asking in chat. If it says a request COULD NOT be filed, obey the reason it gives: a denied-and-muted request means do not ask again, a rate limit means withdraw one or wait; ask in chat only when the reason carries no instruction. Never retry before a decision, never route around the hold via c3_shell or another tool, and never re-file — duplicates collapse into the pending request.
12. **AGENT CONFIG** (v2.46.0+): `c3_artifacts(action='status|list|history|diff|restore')` — version history for the files that shape the agent itself: instruction docs (CLAUDE.md/AGENTS.md/copilot-instructions.md/.cursorrules), settings/hooks, MCP configs (.mcp.json/.vscode/.cursor/.codex), .claude skills/agents/commands/plugins. Out-of-band edits are captured automatically; `diff` any version against live, `restore` writes a prior version back (forward-only, ledger-logged). WRITES to that whole set PAUSE by default (v2.100.0+, widened v2.102.0): a builtin confirm tier, `builtin` scope in the S8 — expect `[c3-access:confirm]` and follow the CONFIRM HOLDS flow above; reads stay open. It holds `c3_edit`, `c3_artifacts restore`, native writes via hooks, AND shell writes (redirects, `sed -i`, `cp`) as of v2.102.0 — the shell scan is best-effort, so route agent-config edits through `c3_edit` rather than a heredoc. The tier is builtin: a user changes it with `c3 access builtin mode`, not `c3 access list`. In an IDE with no PreToolUse hooks (Codex, Antigravity, Copilot) the hold covers c3_* tools only — native writes there are not intercepted, so use c3_edit.

## Plan mode
In plan mode, all c3_* read tools (search, read, compress, filter, validate, status) work normally — skip edit/delegate steps.

## Anti-patterns (DO NOT do these)
- Starting with native file search/read/grep without a prior c3_* call
- Using native Edit when c3_edit is available
- Reading entire files when c3_compress + c3_read would be more surgical
- Skipping c3_validate after making edits"""

# Pointer that replaces the embedded Project Context tree (v2.60.0).
# The live map is machine-owned and auto-refreshed; instruction docs carry
# only this stable pointer so they never go stale and never fight the
# line budget. Works for every consumer (Claude Code, Codex, Antigravity).
MAP_POINTER_BLOCK = """\
Live repo map: `.c3/MAP.md` — tree, commands, entry points, module
one-liners. Read it BEFORE any file discovery. C3 refreshes it
automatically (edit hooks + first tool call); if it is missing or looks
stale, run `c3 map refresh`. Freshness state: `.c3/map.meta.json`."""

# Ultra-compact workflow for nano mode (~250 tokens vs ~800 for full)
C3_NANO_WORKFLOW = """\
## C3 Tools — MANDATORY
Native tools BLOCKED unless c3_* called first. State reason when falling back.
1. c3_search(action='code|files|semantic') — BEFORE any search/grep/glob
2. c3_compress(mode='map') then c3_read(symbols=...|lines=...) — BEFORE any file read
3. c3_edit(file_path, old_string, new_string, summary) — for ALL edits; edits=[{...}] batch
4. c3_filter(text='...') — output >10 lines
5. c3_validate(file_path) — after edits
6. c3_session(action='log'|'snapshot') — decisions / before /clear
7. `[c3-access:confirm]` = PAUSE, not block: a human approves this exact write (agent-config files — .mcp.json, instruction docs, .claude bodies — hold by default). Read the S8 tail: wait on the request it names (c3_override(action='wait', request_id='...', timeout_s=180)), then retry once. Never retry early, never route around it.
Plan mode: all c3_* read tools work normally — skip edit/delegate steps.
DO NOT: start with native Read/Grep/Glob/Edit, skip c3_validate, read full files without c3_compress."""


# --- Per-IDE workflow adaptation ----------------------------------------------
# The workflows above are written for Claude Code, which enforces the c3-first
# rule with PreToolUse hooks. Every other IDE (VS Code Copilot, Cursor, Codex,
# Antigravity) has no hooks, so telling its agent that native tools are
# "blocked" is a claim the agent disproves the first time it calls one — and a
# document with one disprovable line invites the agent to discount the rest.
# Same mandate, honest mechanism.
HOOK_HEADER = "## C3 Tools — MANDATORY (enforced by hooks)"
NANO_HEADER = "## C3 Tools — MANDATORY"
NO_HOOK_HEADER = "## C3 Tools — MANDATORY (workflow rule — no hooks in this IDE)"
NO_HOOK_LEDE = (
    "Native tools (read, search, grep, glob, edit, write) must NOT be used before a c3_* "
    "tool. This IDE has no PreToolUse hooks, so nothing blocks them technically — "
    "following the order below is a project requirement regardless."
)
NO_HOOK_LEDE_NANO = (
    "Native tools must NOT precede a c3_* call. No hooks here — this is a workflow rule, "
    "not a technical block. State reason when falling back."
)

# Sentences that are only true where PreToolUse hooks run. Same honesty rule
# as the header/lede above, applied to the body: v2.100.0's agent-config
# confirm tier shipped "WRITES to these files PAUSE by default" into AGENTS.md
# and copilot-instructions.md, where a native write is never intercepted — one
# more line the agent disproves the first time it edits AGENTS.md directly.
# (sentence-needle, hookless-replacement); an empty replacement drops it.
NO_HOOK_REWRITES = (
    ("native writes via hooks, AND shell writes",
     "shell writes"),
    ("In an IDE with no PreToolUse hooks (Codex, Antigravity, Copilot) the "
     "hold covers c3_* tools only — native writes there are not intercepted, "
     "so use c3_edit.",
     "This IDE has no PreToolUse hooks: the hold covers c3_* tools only, and "
     "a native write is not intercepted at all — which is exactly why an "
     "agent-config edit must go through c3_edit."),
)

# VS Code defers MCP tools until the agent searches for them, so a Copilot
# session that follows the workflow verbatim calls tools it has not loaded.
VSCODE_INSTRUCTIONS_FILE = ".github/copilot-instructions.md"
VSCODE_SESSION_INIT = """\
## Session Initialization (VS Code ONLY)
C3 tools are deferred in VS Code — load them before anything below is callable.
1. **LOAD TOOLS**: call `tool_search_tool_regex` with pattern `^mcp_c3_` as the VERY FIRST action of every session.
2. **VERIFY**: confirm tools such as `mcp_c3_c3_search` and `mcp_c3_c3_read` are available before proceeding."""


def _strip_unsupported_clauses(line: str, needles: tuple) -> str:
    """Drop only the sentences of ``line`` that mention ``needles``.

    Dropping the whole line loses the step along with its number, which leaves
    a visible hole in the numbered workflow (``7.`` followed by ``9.``).
    Sentence-level trimming keeps the step and drops just the unsupported part.
    Returns "" when nothing but the list marker survives.
    """
    sentences = re.split(r"(?<=\.)\s+", line)
    kept = [s for s in sentences if not any(n in s.lower() for n in needles)]
    rebuilt = " ".join(kept).strip()
    return "" if re.fullmatch(r"[\d.\s]*", rebuilt) else rebuilt


def adapt_workflow_for_ide(workflow: str, *, supports_hooks: bool = True,
                           supports_clear: bool = True, nano: bool = False) -> str:
    """Tailor a C3 workflow block to one IDE's capabilities.

    ``supports_clear=False`` removes ``/clear`` and snapshot guidance;
    ``supports_hooks=False`` restates the mandate as a workflow rule instead of
    hook enforcement and drops hook-specific lines.
    """
    if not supports_clear:
        adapted = []
        for line in workflow.splitlines():
            if not line.strip():
                adapted.append(line)
                continue
            kept = _strip_unsupported_clauses(line, ("snapshot", "/clear"))
            if kept:
                adapted.append(kept)
        workflow = "\n".join(adapted)

    if not supports_hooks:
        workflow = workflow.replace(NANO_HEADER if nano else HOOK_HEADER,
                                    NO_HOOK_HEADER, 1)
        lede = NO_HOOK_LEDE_NANO if nano else NO_HOOK_LEDE
        adapted = []
        lede_done = False
        for line in workflow.splitlines():
            if not lede_done and line.startswith("Native tools"):
                adapted.append(lede)
                lede_done = True
                continue
            if "PostToolUse" in line or "AfterTool" in line:
                continue
            adapted.append(line)
        workflow = "\n".join(adapted)
        for needle, replacement in NO_HOOK_REWRITES:
            workflow = workflow.replace(needle, replacement)

    return workflow


# --- C3-managed instruction block ---------------------------------------------
# C3-generated content for project instruction docs (CLAUDE.md / AGENTS.md /
# GEMINI.md / copilot-instructions.md) is wrapped in these sentinels so that
# regenerating the docs never clobbers user-written content. Mirrors the
# non-destructive merge used for the global ~/.claude/CLAUDE.md.
C3_BLOCK_BEGIN = (
    "<!-- C3:BEGIN — auto-generated by C3. Do NOT edit inside this block; it is "
    "regenerated on every `c3 install-mcp` / `c3 init`. Your content OUTSIDE the "
    "block is preserved. -->"
)
C3_BLOCK_END = "<!-- C3:END -->"
C3_BLOCK_HEADING = "# C3 — Managed Instructions"

# First line of every legacy (marker-less) C3 instruction doc. Used to detect
# and replace pre-marker C3 content instead of leaving it duplicated above the
# new block. Both the compact and nano workflows start with this exact heading,
# so the full string (not just "## C3 Tools") is required to avoid mistaking a
# genuine user file that merely opens with a "## C3 Tools" heading for a
# replaceable legacy doc.
C3_LEGACY_FIRST_LINE = "## C3 Tools — MANDATORY"


def wrap_c3_block(content: str) -> str:
    """Wrap C3-generated instruction ``content`` in the managed-section markers."""
    body = content.strip()
    return f"{C3_BLOCK_BEGIN}\n{C3_BLOCK_HEADING}\n\n{body}\n{C3_BLOCK_END}"


def merge_c3_block(existing: str, new_block: str) -> str:
    """Merge a freshly wrapped C3 block into ``existing`` file content.

    Non-destructive, mirroring the global ~/.claude/CLAUDE.md behaviour:

    1. If the C3 markers are already present, replace only the marked region and
       preserve everything the user wrote before and after it.
    2. Legacy (marker-less) C3 docs are recognised by their full leading
       ``## C3 Tools — MANDATORY`` heading (and the absence of markers) and
       replaced wholesale, while any trailing ``# User Notes`` section (the
       pre-marker convention) is preserved.
    3. A genuine, user-authored file with neither markers nor the legacy
       signature is never overwritten — the C3 block is appended below it.
    """
    new_block = new_block.strip()

    # 1. Markers present → surgical in-place replacement.
    #
    #    Use the FIRST BEGIN and the LAST END so a region containing duplicated
    #    or nested markers collapses to a single clean block. Guard against
    #    out-of-order markers (an END that precedes the BEGIN): in that corrupt
    #    case the slice would overlap, so fall through to the append/rewrite
    #    paths below instead of producing garbage.
    if C3_BLOCK_BEGIN in existing and C3_BLOCK_END in existing:
        start = existing.index(C3_BLOCK_BEGIN)
        end = existing.rindex(C3_BLOCK_END) + len(C3_BLOCK_END)
        if end > start:
            before = existing[:start].rstrip()
            after = existing[end:].lstrip()
            parts = [p for p in (before, new_block, after) if p]
            return "\n\n".join(parts) + "\n"

    # 2. Legacy marker-less C3 doc → replace head, keep trailing user notes.
    #    Require BOTH the full legacy signature AND the absence of the managed
    #    markers, so a marker-bearing file (handled above, or one with corrupt
    #    out-of-order markers) is never wholesale-replaced here.
    has_markers = C3_BLOCK_BEGIN in existing or C3_BLOCK_END in existing
    if not has_markers and existing.lstrip().startswith(C3_LEGACY_FIRST_LINE):
        tail = ""
        if "# User Notes" in existing:
            tail = existing[existing.index("# User Notes"):].strip()
        parts = [new_block] + ([tail] if tail else [])
        return "\n\n".join(parts) + "\n"

    # 3. Genuine user-authored file → preserve fully, append the C3 block.
    head = existing.rstrip()
    parts = [head, new_block] if head else [new_block]
    return "\n\n".join(parts) + "\n"


def strip_c3_block(existing: str) -> Optional[str]:
    """Remove C3's managed block from ``existing`` and return what is left.

    The inverse of :func:`merge_c3_block`, for the uninstall paths
    (``c3 init --clear`` / Wipe). Same three cases, same ownership rule —
    C3 deletes only what it wrote:

    1. Markers present → cut the FIRST BEGIN … LAST END span and keep
       everything before and after it: the user's own notes, or another
       tool's managed block (``<!-- YEP:BEGIN -->``) sharing the file.
    2. Legacy marker-less C3 doc (full ``## C3 Tools — MANDATORY`` head) →
       the head was ours; keep only a trailing ``# User Notes`` section.
    3. No markers and no legacy signature → not a C3 document at all.
       Return ``None``: the caller must leave the file alone.

    Returns the remaining text — ``""`` when the block was the whole file,
    so the caller may delete it — or ``None`` for case 3. Out-of-order
    (corrupt) markers fall to case 3: when the span cannot be trusted,
    nothing is cut.
    """
    if C3_BLOCK_BEGIN in existing and C3_BLOCK_END in existing:
        start = existing.index(C3_BLOCK_BEGIN)
        end = existing.rindex(C3_BLOCK_END) + len(C3_BLOCK_END)
        if end > start:
            before = existing[:start].rstrip()
            after = existing[end:].lstrip()
            parts = [p for p in (before, after) if p]
            return ("\n\n".join(parts) + "\n") if parts else ""

    has_markers = C3_BLOCK_BEGIN in existing or C3_BLOCK_END in existing
    if not has_markers and existing.lstrip().startswith(C3_LEGACY_FIRST_LINE):
        if "# User Notes" in existing:
            return existing[existing.index("# User Notes"):].strip() + "\n"
        return ""

    return None


def write_c3_instruction_doc(path, content: str, project_path=None,
                             source: str = "install_mcp") -> str:
    """Write a C3-generated instruction doc without clobbering user content.

    Wraps ``content`` in the C3 managed block and merges it into any existing
    file via :func:`merge_c3_block`. Returns the exact text written to disk.

    Self-reports the write to the agent-artifact tracker under ``source``
    (``install_mcp`` for the installer and CLI, ``claude_md_updater`` for the
    background agent) so regeneration is attributed to C3 instead of
    surfacing as anonymous out-of-band drift. ``project_path`` pins the
    project root; when omitted it is inferred from the doc's location (root
    or one level down, e.g. .github/copilot-instructions.md).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    block = wrap_c3_block(content)
    if p.exists():
        existing = p.read_text(encoding="utf-8", errors="replace")
        final = merge_c3_block(existing, block)
    else:
        final = block.rstrip() + "\n"
    p.write_text(final, encoding="utf-8")
    try:
        from services.artifact_defs import note_pending_write
        root = Path(project_path) if project_path else None
        if root is None:
            root = next((c for c in (p.parent, p.parent.parent)
                         if (c / ".c3").is_dir()), None)
        if root is not None and (root / ".c3").is_dir():
            note_pending_write(root, str(p.resolve().relative_to(root.resolve())),
                               source or "install_mcp")
    except Exception:
        pass
    return final


class ClaudeMdManager:
    """Manages instructions file generation, analysis, compaction, and insight promotion.

    Supports multiple IDEs — instructions_file determines the output filename
    (e.g. CLAUDE.md for Claude Code, .github/copilot-instructions.md for VS Code).
    """

    def __init__(self, project_path: str, session_mgr, indexer, memory,
                 instructions_file: str = "CLAUDE.md", line_limit: int = 200,
                 supports_hooks: bool = True, supports_clear: bool = True,
                 nano_mode: bool = False):
        self.project_path = Path(project_path)
        self.session_mgr = session_mgr
        self.indexer = indexer
        self.memory = memory
        self.instructions_file = instructions_file
        self.line_limit = line_limit
        self.supports_hooks = supports_hooks
        self.supports_clear = supports_clear
        self._nano_mode = nano_mode

    # ── Public API (one per MCP tool) ────────────────────────

    def _build_c3_workflow(self, nano: bool = False) -> str:
        """Build C3 workflow section.

        nano=True: ~250 tokens (vs ~800 full). Use for IDEs where instructions space is limited.
        Filters out hooks/snapshot/transcript lines for IDEs that don't support them.
        """
        if self.instructions_file == "AGENTS.md" and self.supports_hooks:
            from services.codex_integration import CODEX_WORKFLOW
            return CODEX_WORKFLOW

        workflow = C3_NANO_WORKFLOW if nano else C3_COMPACT_WORKFLOW

        # Strip features unsupported by this IDE to reduce irrelevant instruction
        # tokens and avoid claiming enforcement the IDE does not have.
        workflow = adapt_workflow_for_ide(
            workflow,
            supports_hooks=self.supports_hooks,
            supports_clear=self.supports_clear,
            nano=nano,
        )

        # VS Code hides MCP tools until they are searched for, so the generated
        # doc must open with the load step or it describes uncallable tools.
        if self.instructions_file == VSCODE_INSTRUCTIONS_FILE:
            workflow = VSCODE_SESSION_INIT + "\n\n" + workflow

        return workflow

    def _repo_map_enabled(self) -> bool:
        """Live repo map is default-on; map.enabled=false in .c3/config.json
        restores the legacy embedded tree."""
        try:
            with open(self.project_path / ".c3" / "config.json",
                      encoding="utf-8") as f:
                cfg = json.load(f) or {}
            return bool(cfg.get("map", {}).get("enabled", True))
        except (OSError, ValueError):
            return True

    def generate(self, include_sessions: bool = True, mode: str = "compact") -> dict:
        """Generate token-efficient CLAUDE.md from live project data.

        mode='compact' (default): full workflow + project tree + key facts (~2,000 tokens)
        mode='nano': minimal mandate only (~250 tokens) — project tree/facts served via c3_memory

        Optimized for minimal per-turn overhead:
        - Compact C3 tool reference (~7 lines vs ~16)
        - No session history (use c3_memory recall instead)
        - Top 5 learned facts only (rest available via c3_memory)
        - No shortcuts section (low value, costs tokens every turn)
        """
        if mode == "nano":
            self._nano_mode = True

        use_nano = getattr(self, '_nano_mode', False)

        # Nano mode: return minimal mandate only — project tree/facts served via c3_memory on demand
        if use_nano:
            content = self._build_c3_workflow(nano=True)
            metrics = self._count_metrics(content)
            return {
                "content": content,
                "lines": metrics["lines"],
                "tokens": metrics["tokens"],
                "mode": "nano",
                "truncation_warning": None,
            }

        parts = []

        # C3 workflow instructions (compact)
        parts.append(self._build_c3_workflow(nano=False))

        # Project context: pointer to the live repo map when enabled
        # (v2.60.0), legacy embedded tree otherwise. The map is regenerated
        # automatically (hooks + first-tool-call ensure), so the pointer
        # never goes stale the way an embedded tree did.
        map_enabled = self._repo_map_enabled()
        if map_enabled:
            parts.append("\n# Project Context\n")
            parts.append(MAP_POINTER_BLOCK)
        else:
            # Project structure
            parts.append("\n# Project Context\n")
            parts.append(self.session_mgr._scan_project_structure())

            # Tech stack
            parts.append("\n## Tech Stack\n")
            parts.append(self.session_mgr._detect_tech_stack())

            # Key files (compact)
            key_files = self._detect_key_files()
            if key_files:
                parts.append("\n## Key Files\n")
                for kf in key_files[:5]:
                    parts.append(f"- `{kf['file']}` — {kf['reason']}")

        # Top learned facts only (rest available via c3_memory recall)
        promoted_facts = [
            f for f in self.memory.facts
            if f.get("relevance_count", 0) >= 3
        ]
        if promoted_facts:
            parts.append("\n## Key Facts (use c3_memory for more)\n")
            for f in promoted_facts[:5]:
                parts.append(f"- {f['fact'][:120]}")

        content = '\n'.join(parts)
        metrics = self._count_metrics(content)

        # Enforce line budget: progressively prune rather than silently truncate Key Facts
        if self.line_limit and metrics["lines"] > self.line_limit:
            # Pass 1: prune project structure to depth 1
            pruned_parts = []
            for part in parts:
                if part.strip().startswith("```") and "\n" in part:
                    part = self._prune_structure_depth(part, max_depth=1)
                pruned_parts.append(part)
            content = '\n'.join(pruned_parts)
            metrics = self._count_metrics(content)

        if self.line_limit and metrics["lines"] > self.line_limit:
            # Pass 2: drop key facts to 3
            rebuilt = []
            in_facts = False
            facts_shown = 0
            for line in content.splitlines():
                if line.startswith("## Key Facts"):
                    in_facts = True
                    rebuilt.append(line)
                    continue
                if in_facts and line.startswith("- "):
                    if facts_shown < 3:
                        rebuilt.append(line)
                        facts_shown += 1
                    continue
                if in_facts and line.startswith("## "):
                    in_facts = False
                rebuilt.append(line)
            content = '\n'.join(rebuilt)
            metrics = self._count_metrics(content)

        return {
            "content": content,
            "lines": metrics["lines"],
            "tokens": metrics["tokens"],
            "truncation_warning": (
                f"Content is {metrics['lines']} lines — exceeds limit of {self.line_limit}. "
                "Run `c3 claudemd compact` to reduce further."
            ) if self.line_limit and metrics["lines"] > self.line_limit else None,
        }

    def check_staleness(self) -> dict:
        """Check existing CLAUDE.md for staleness and drift."""
        current = self._read_current()
        if current is None:
            return {
                "status": "missing",
                "issues": [{
                    "severity": "error",
                    "message": f"No {self.instructions_file} found. Use CLI `c3 claudemd generate` to create one.",
                }],
            }

        issues = []
        sections = self._parse_sections(current)
        metrics = self._count_metrics(current)

        # Size warning (only if line_limit is set)
        if self.line_limit and metrics["lines"] > self.line_limit:
            issues.append({
                "severity": "warning",
                "message": (
                    f"{self.instructions_file} is {metrics['lines']} lines ({metrics['tokens']} tokens). "
                    f"Truncation may occur after {self.line_limit} lines. "
                    "Use CLI `c3 claudemd compact` to reduce."
                ),
            })

        # Structure and tech-stack drift apply only to the legacy embedded
        # tree. With the live repo map (default since 2.60.0) the doc carries
        # neither — the map does — so there is nothing embedded to drift.
        # Diffing anyway reported every detected technology as "not listed"
        # on every check, and an auto-applying updater rewrote the file each
        # time (2.110.1).
        if not self._repo_map_enabled():
            issues.extend(self._diff_structure(current))
            issues.extend(self._diff_tech_stack(current))

        # Session staleness
        session_files = sorted(
            (self.project_path / ".c3" / "sessions").glob("session_*.json"),
            reverse=True,
        ) if (self.project_path / ".c3" / "sessions").exists() else []

        session_section = sections.get("Session History (Compressed)", "")
        if session_files:
            # Count sessions mentioned in CLAUDE.md
            mentioned_ids = set(re.findall(r'Session:\s*(\d{8}_\d{6})', session_section))
            total_sessions = len(session_files)
            unmentioned = total_sessions - len(mentioned_ids)
            if unmentioned > 3:
                issues.append({
                    "severity": "info",
                    "message": f"{unmentioned} sessions not reflected in CLAUDE.md. Consider regenerating.",
                })

        if not issues:
            issues.append({
                "severity": "info",
                "message": "CLAUDE.md looks up to date.",
            })

        return {
            "status": "ok" if all(i["severity"] == "info" for i in issues) else "stale",
            "lines": metrics["lines"],
            "tokens": metrics["tokens"],
            "issues": issues,
        }

    def compact(self, target_lines: int = 150) -> dict:
        """Compact existing CLAUDE.md to fit within target line count.

        When the file uses the C3 managed-block markers, only the inner C3
        body is compacted and the block is re-wrapped, so the markers, the
        ``# C3`` heading, and any user content outside the block survive.
        """
        current = self._read_current()
        if current is None:
            return {"error": f"No {self.instructions_file} found on disk. Use CLI `c3 claudemd generate` to preview, then `c3 claudemd save` to persist before compacting."}

        original_metrics = self._count_metrics(current)

        # If already under target, no compaction needed
        if original_metrics["lines"] <= target_lines:
            return {
                "content": current,
                "original_lines": original_metrics["lines"],
                "compacted_lines": original_metrics["lines"],
                "original_tokens": original_metrics["tokens"],
                "compacted_tokens": original_metrics["tokens"],
                "actions": ["Already under target — no compaction needed."],
            }

        # Isolate the C3 managed block so its markers, the # C3 heading, and
        # any surrounding user content are preserved verbatim across compaction.
        before = after = ""
        inner = current
        has_block = C3_BLOCK_BEGIN in current and C3_BLOCK_END in current
        if has_block:
            start = current.index(C3_BLOCK_BEGIN)
            end = current.index(C3_BLOCK_END) + len(C3_BLOCK_END)
            before = current[:start]
            after = current[end:]
            inner = current[start + len(C3_BLOCK_BEGIN):end - len(C3_BLOCK_END)].strip()
            if inner.startswith(C3_BLOCK_HEADING):
                inner = inner[len(C3_BLOCK_HEADING):].lstrip("\n")

        compacted_inner, actions = self._compact_sections(inner, target_lines)

        if has_block:
            pieces = []
            if before.strip():
                pieces.append(before.strip())
            pieces.append(wrap_c3_block(compacted_inner))
            if after.strip():
                pieces.append(after.strip())
            content = "\n\n".join(pieces) + "\n"
        else:
            content = compacted_inner

        compacted_metrics = self._count_metrics(content)

        if not actions:
            actions.append("No compaction opportunities found.")

        return {
            "content": content,
            "original_lines": original_metrics["lines"],
            "compacted_lines": compacted_metrics["lines"],
            "original_tokens": original_metrics["tokens"],
            "compacted_tokens": compacted_metrics["tokens"],
            "actions": actions,
        }

    def _compact_sections(self, text: str, target_lines: int) -> tuple:
        """Section-based compaction of an instruction-doc body.

        Returns ``(content, actions)``. Operates purely on ``text``; callers
        handle any C3 managed-block wrapping.
        """
        sections = self._parse_sections(text)
        actions = []

        # Step 1: Compress session history — keep last 3, one-line summaries
        if "Session History (Compressed)" in sections:
            session_text = sections["Session History (Compressed)"]
            compressed = self._compress_sessions(session_text, max_sessions=3)
            if len(compressed.split('\n')) < len(session_text.split('\n')):
                sections["Session History (Compressed)"] = compressed
                actions.append("Trimmed session history to last 3 sessions with one-line summaries")

        # Step 2: Deduplicate — remove exact duplicate lines (excluding blank lines and headers)
        seen_lines = set()
        deduped_sections = {}
        for name, sect_text in sections.items():
            if name in ("User Notes", "C3 — Token-Saving Workflow (MUST FOLLOW)"):
                deduped_sections[name] = sect_text
                continue
            new_lines = []
            for line in sect_text.split('\n'):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    new_lines.append(line)
                elif stripped not in seen_lines:
                    seen_lines.add(stripped)
                    new_lines.append(line)
            deduped_sections[name] = '\n'.join(new_lines)
        dup_removed = sum(
            len(sections[k].split('\n')) - len(deduped_sections[k].split('\n'))
            for k in sections
        )
        if dup_removed > 0:
            actions.append(f"Removed {dup_removed} duplicate lines")
            sections = deduped_sections

        # Step 3: Prune structure tree depth if still over target
        content = self._reassemble_sections(sections)
        current_lines = len(content.split('\n'))
        if current_lines > target_lines and "Project Context (Auto-generated by C3)" in sections:
            ctx_section = sections["Project Context (Auto-generated by C3)"]
            pruned = self._prune_structure_depth(ctx_section, max_depth=2)
            if len(pruned.split('\n')) < len(ctx_section.split('\n')):
                sections["Project Context (Auto-generated by C3)"] = pruned
                actions.append("Reduced project structure tree depth")

        # Reassemble
        return self._reassemble_sections(sections), actions

    def get_promotion_candidates(self, min_relevance: int = 2) -> dict:
        """Find facts and patterns worth promoting into CLAUDE.md."""
        current = self._read_current()
        current_text = current or ""
        candidates = {
            "Code Patterns & Conventions": [],
            "Quick Reference Shortcuts": [],
            "Key Files": [],
            "Project Roadmap & Active Plans": [],
        }

        # High-relevance facts
        for fact in self.memory.facts:
            if fact.get("relevance_count", 0) < min_relevance:
                continue
            # Skip if already in CLAUDE.md
            if fact["fact"] in current_text:
                continue

            category = fact.get("category", "general")
            target = "Code Patterns & Conventions"
            if category in ("shortcut", "reference", "alias"):
                target = "Quick Reference Shortcuts"
            elif category in ("file", "path", "entry_point"):
                target = "Key Files"
            elif category in ("plan", "roadmap", "todo"):
                target = "Project Roadmap & Active Plans"

            candidates[target].append({
                "fact": fact["fact"],
                "category": category,
                "relevance_count": fact["relevance_count"],
                "snippet": f"- [{category}] {fact['fact']}",
            })

        # Recurring decisions and plans from sessions
        session_dir = self.project_path / ".c3" / "sessions"
        if session_dir.exists():
            decision_keywords = {}  # keyword -> [session_ids]
            active_plans = []  # List of unique plan strings
            for sf in sorted(session_dir.glob("session_*.json"), reverse=True)[:20]:
                try:
                    with open(sf, encoding='utf-8') as f:
                        s = json.load(f)
                    sid = s.get("id", "unknown")
                    for d in s.get("decisions", []):
                        text = d.get("decision", "")
                        # Plan detection
                        if "PLAN:" in text.upper():
                            plan_text = text.split("PLAN:", 1)[1].strip()
                            if plan_text and not any(p["fact"] == plan_text for p in active_plans):
                                active_plans.append({
                                    "fact": plan_text,
                                    "category": "active_plan",
                                    "relevance_count": 1,
                                    "snippet": f"- [PLAN] {plan_text}"
                                })

                        # Decision keyword extraction (5+ chars)
                        words = set(re.findall(r'[a-zA-Z]{5,}', text.lower()))
                        for w in words:
                            if w not in decision_keywords:
                                decision_keywords[w] = []
                            if sid not in decision_keywords[w]:
                                decision_keywords[w].append(sid)
                except Exception:
                    continue

            # Add unique plans to roadmap
            for p in active_plans:
                if p["fact"] not in current_text:
                    candidates["Project Roadmap & Active Plans"].append(p)

            # Keywords appearing in 2+ sessions
            recurring = {k: v for k, v in decision_keywords.items() if len(v) >= 2}
            for keyword, session_ids in sorted(recurring.items(), key=lambda x: -len(x[1]))[:5]:
                snippet = f"- Recurring decision keyword: \"{keyword}\" (across {len(session_ids)} sessions)"
                if snippet not in current_text:
                    candidates["Code Patterns & Conventions"].append({
                        "fact": f"Recurring decision keyword: \"{keyword}\"",
                        "category": "recurring_decision",
                        "relevance_count": len(session_ids),
                        "snippet": snippet,
                    })

        # Filter out empty groups
        candidates = {k: v for k, v in candidates.items() if v}

        total = sum(len(v) for v in candidates.values())
        return {
            "total_candidates": total,
            "candidates": candidates,
            "message": (
                f"Found {total} promotion candidates across {len(candidates)} sections."
                if total > 0
                else "No promotion candidates found. Build more session history and facts first."
            ),
        }

    # ── Shared helpers ───────────────────────────────────────

    def _read_current(self) -> Optional[str]:
        """Read existing instructions file from project root."""
        path = self.project_path / self.instructions_file
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _parse_sections(self, content: str) -> dict:
        """Split CLAUDE.md into named sections by # or ## headers."""
        sections = {}
        current_name = "_preamble"
        current_lines = []

        for line in content.split('\n'):
            header_match = re.match(r'^(#{1,3})\s+(.+)', line)
            if header_match:
                # Save previous section
                if current_lines or current_name != "_preamble":
                    sections[current_name] = '\n'.join(current_lines)
                current_name = header_match.group(2).strip()
                current_lines = []
            else:
                current_lines.append(line)

        # Save last section
        if current_lines or current_name != "_preamble":
            sections[current_name] = '\n'.join(current_lines)

        return sections

    def _reassemble_sections(self, sections: dict) -> str:
        """Reassemble sections into CLAUDE.md content."""
        parts = []
        for name, body in sections.items():
            if name == "_preamble":
                if body.strip():
                    parts.append(body)
            else:
                # Determine header level from body context (default ##)
                level = "#"
                if name in ("Project Context (Auto-generated by C3)",
                            "Session History (Compressed)", "User Notes"):
                    level = "#"
                else:
                    level = "##"
                parts.append(f"{level} {name}\n{body}")
        return '\n\n'.join(parts)

    def _count_metrics(self, content: str) -> dict:
        """Count lines and tokens."""
        lines = len(content.split('\n'))
        tokens = count_tokens(content)
        return {"lines": lines, "tokens": tokens}

    # ── Generate helpers ─────────────────────────────────────

    def _detect_enhanced_patterns(self) -> list:
        """Detect patterns beyond what SessionManager finds — linting, test frameworks, monorepo."""
        patterns = []
        p = self.project_path

        # Base patterns from session manager
        base = self.session_mgr._detect_patterns()
        if base and base != "No patterns auto-detected":
            for line in base.split('\n'):
                line = line.strip().lstrip('- ')
                if line:
                    patterns.append(line)

        # Linting / formatting
        linting_indicators = {
            ".eslintrc": "ESLint", ".eslintrc.js": "ESLint", ".eslintrc.json": "ESLint",
            ".eslintrc.yml": "ESLint", "eslint.config.js": "ESLint (flat config)",
            ".prettierrc": "Prettier", ".prettierrc.json": "Prettier",
            "prettier.config.js": "Prettier",
            ".flake8": "Flake8", "setup.cfg": "Python config (setup.cfg)",
            "ruff.toml": "Ruff", ".ruff.toml": "Ruff",
            ".stylelintrc": "Stylelint",
            "biome.json": "Biome",
        }
        for filename, tool in linting_indicators.items():
            if (p / filename).exists():
                patterns.append(f"Uses {tool}")

        # Check pyproject.toml for tool configs
        pyproject = p / "pyproject.toml"
        if pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8")
                if "[tool.ruff" in text:
                    patterns.append("Uses Ruff (via pyproject.toml)")
                if "[tool.black" in text:
                    patterns.append("Uses Black formatter")
                if "[tool.pytest" in text or "[tool.pytest.ini_options" in text:
                    patterns.append("Uses pytest")
                if "[tool.mypy" in text:
                    patterns.append("Uses mypy type checking")
            except Exception:
                pass

        # Test frameworks
        if (p / "jest.config.js").exists() or (p / "jest.config.ts").exists():
            patterns.append("Uses Jest for testing")
        if (p / "vitest.config.ts").exists() or (p / "vitest.config.js").exists():
            patterns.append("Uses Vitest for testing")
        if (p / "pytest.ini").exists() or (p / "conftest.py").exists():
            patterns.append("Uses pytest")

        # Monorepo indicators
        if (p / "lerna.json").exists():
            patterns.append("Monorepo (Lerna)")
        if (p / "pnpm-workspace.yaml").exists():
            patterns.append("Monorepo (pnpm workspaces)")
        if (p / "turbo.json").exists():
            patterns.append("Monorepo (Turborepo)")
        pkg = p / "package.json"
        if pkg.exists():
            try:
                with open(pkg, encoding='utf-8') as f:
                    data = json.load(f)
                if "workspaces" in data:
                    patterns.append("Monorepo (npm/yarn workspaces)")
            except Exception:
                pass

        # Deduplicate
        seen = set()
        unique = []
        for pat in patterns:
            key = pat.lower()
            if key not in seen:
                seen.add(key)
                unique.append(pat)

        return unique

    def _detect_key_files(self) -> list:
        """Identify key files from session history and conventional entry points."""
        key_files = []
        seen = set()

        # Hot files from session history
        session_dir = self.project_path / ".c3" / "sessions"
        if session_dir.exists():
            file_counts = {}
            for sf in sorted(session_dir.glob("session_*.json"), reverse=True)[:20]:
                try:
                    with open(sf, encoding='utf-8') as f:
                        s = json.load(f)
                    for ft in s.get("files_touched", []):
                        fname = ft.get("file", "")
                        if fname:
                            file_counts[fname] = file_counts.get(fname, 0) + 1
                except Exception:
                    continue

            for fname, count in sorted(file_counts.items(), key=lambda x: -x[1])[:5]:
                if count >= 2 and fname not in seen:
                    key_files.append({"file": fname, "reason": f"edited in {count} sessions"})
                    seen.add(fname)

        # Conventional entry points
        entry_points = [
            ("main.py", "Python entry point"),
            ("app.py", "Application entry point"),
            ("index.ts", "TypeScript entry point"),
            ("index.js", "JavaScript entry point"),
            ("src/index.ts", "Source entry point"),
            ("src/index.js", "Source entry point"),
            ("src/main.ts", "Source entry point"),
            ("src/App.tsx", "React app root"),
            ("cli/mcp_server.py", "MCP server entry"),
        ]
        for filepath, reason in entry_points:
            if (self.project_path / filepath).exists() and filepath not in seen:
                key_files.append({"file": filepath, "reason": reason})
                seen.add(filepath)

        return key_files

    # ── Check helpers ────────────────────────────────────────

    def _diff_structure(self, current_content: str) -> list:
        """Find dirs mentioned in CLAUDE.md that don't exist, and new dirs not mentioned."""
        issues = []

        # Extract dir-like references from the code block
        mentioned_dirs = set()
        in_code_block = False
        for line in current_content.split('\n'):
            if line.strip().startswith('```'):
                in_code_block = not in_code_block
                continue
            if in_code_block and line.strip().endswith('/'):
                dirname = line.strip().rstrip('/')
                if dirname:
                    mentioned_dirs.add(dirname)

        # Scan actual top-level dirs with the same pruning rules as the
        # generator, so gitignored dirs are never reported as "new".
        from services.scanner import make_dir_pruner
        pruned = make_dir_pruner(self.project_path, extra_skip=('.claude',))
        actual_dirs = set()
        for item in self.project_path.iterdir():
            if item.is_dir() and not pruned(item.name) and not item.name.startswith('.'):
                actual_dirs.add(item.name)

        # Compare (use base names only)
        mentioned_basenames = {d.split('/')[-1] for d in mentioned_dirs if d}

        missing_in_fs = mentioned_basenames - actual_dirs
        new_in_fs = actual_dirs - mentioned_basenames

        for d in missing_in_fs:
            # Skip the project root name
            if d == self.project_path.name:
                continue
            issues.append({
                "severity": "warning",
                "message": f"Directory '{d}' mentioned in CLAUDE.md but not found on disk.",
            })

        for d in new_in_fs:
            issues.append({
                "severity": "info",
                "message": f"New directory '{d}' exists but is not in CLAUDE.md.",
            })

        return issues

    def _diff_tech_stack(self, current_content: str) -> list:
        """Compare tech stack in CLAUDE.md vs detected."""
        issues = []
        detected = self.session_mgr._detect_tech_stack()

        if detected == "Could not auto-detect":
            return issues

        detected_set = {t.strip().lower() for t in detected.split(',')}

        # Find the tech stack line in CLAUDE.md
        sections = self._parse_sections(current_content)
        claimed_text = sections.get("Tech Stack", "")
        claimed_set = set()
        for line in claimed_text.split('\n'):
            line = line.strip().lstrip('- ')
            if line:
                for item in line.split(','):
                    item = item.strip().lower()
                    if item:
                        claimed_set.add(item)

        new_tech = detected_set - claimed_set
        for tech in new_tech:
            issues.append({
                "severity": "warning",
                "message": f"Detected '{tech}' in project but not listed in CLAUDE.md Tech Stack.",
            })

        return issues

    # ── Compact helpers ──────────────────────────────────────

    def _compress_sessions(self, session_text: str, max_sessions: int = 3) -> str:
        """Trim session history to last N sessions with one-line summaries."""
        # Split into individual session blocks (## Session: ...)
        blocks = re.split(r'(?=## Session:)', session_text)
        blocks = [b.strip() for b in blocks if b.strip()]

        if len(blocks) <= max_sessions:
            return session_text

        # Keep only last max_sessions, compress each to one line
        kept = blocks[:max_sessions]
        compressed_lines = []
        for block in kept:
            lines = block.split('\n')
            header = lines[0] if lines else ""
            # Extract summary if present
            summary = ""
            for line in lines[1:]:
                if line.startswith("**Summary:**"):
                    summary = line.replace("**Summary:**", "").strip()
                    break
                elif line.startswith("**When:**"):
                    date = line.replace("**When:**", "").strip()
                    summary = f"({date}) {summary}"
            if summary:
                compressed_lines.append(f"{header}\n**Summary:** {summary}\n")
            else:
                compressed_lines.append(f"{header}\n")

        return '\n'.join(compressed_lines)

    def _prune_structure_depth(self, section_text: str, max_depth: int = 2) -> str:
        """Reduce project structure tree depth."""
        lines = section_text.split('\n')
        pruned = []
        in_code_block = False

        for line in lines:
            if line.strip().startswith('```'):
                in_code_block = not in_code_block
                pruned.append(line)
                continue

            if in_code_block:
                # Count indent level (2 spaces per level)
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                depth = indent // 2
                if depth <= max_depth:
                    pruned.append(line)
            else:
                pruned.append(line)

        return '\n'.join(pruned)
