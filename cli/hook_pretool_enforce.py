#!/usr/bin/env python3
"""PreToolUse hook: two-mode enforcement for native tools.

Read-class tools (Read/Grep/Glob/FindFiles/SearchText) are **ADVISORY** —
if no c3_* tool was used first, the call proceeds with a selection-time
hint injected via additionalContext. Drift is still cheap to recover from
for read-only operations.

Write-class tools (Edit/Write) are **BLOCKED** — file mutations must go
through c3_edit so the ledger captures every change. Hard-deny with
redirect message.

This replaces the previous all-blocking behavior. Rationale: blocking read
tools treats Claude adversarially and creates cliffs at every edge case
(new tool variants, Windows quirks). Advisory read + blocked write keeps
the ledger intact without strangling the model's own good judgment.

User-tunable (v2.66+): the write-block above is the ``strict`` mode of
services.enforcement_policy. ``advisory`` downgrades it to a nudge (the
ledger still captures the write — hook_edit_ledger runs PostToolUse and is
independent of this setting); ``off`` disables tool-discipline nudging
entirely. This is LAYER C only. Two things are deliberately NOT affected by
the mode, because they are security boundaries rather than workflow
preferences: the credential-vault write guard below, and Access Guard path
policy in hook_access_guard (which the dispatcher runs FIRST, and whose deny
wins the merge regardless of what this module returns).

State (v2.42+): reads/writes the consolidated .c3/enforcement_state.json via
cli/_hook_utils (single writer module, atomic writes, session-scoped). The
legacy last_c3_call.json / unlocked_files.json pair is still READ as a
fallback for one release; only the new file is written. State recorded by a
different Claude Code session is treated as stale — reads fall back to the
advisory path instead of granting stale unlocks.

Supports both Claude Code and Gemini CLI via _hook_utils.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Same bootstrap hook_access_guard uses: `python hook_pretool_enforce.py`
# invoked directly has no project root on sys.path, so `services` would not
# import. Via hook_dispatch the root is already there and this is a no-op.
_CLI_DIR = Path(__file__).resolve().parent
for _p in (str(_CLI_DIR.parent), str(_CLI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _hook_utils import (  # noqa: E402
    canonical_key,
    load_enforcement_state,
    log_hook_error,
    normalize_tool_name,
    record_unlocked_files,
    response_text_failed,
)

try:
    from _shell_writes import shell_write_targets
except Exception:  # pragma: no cover - package-style import (tests, dispatcher)
    try:
        from cli._shell_writes import shell_write_targets
    except Exception:
        shell_write_targets = None

try:
    from services import access_telemetry, enforcement_policy
except Exception:  # pragma: no cover — degrade to hardcoded strict behavior
    access_telemetry = None
    enforcement_policy = None

# How many activity-log lines to scan backwards
LOOKBACK = 20  # Fix 1: increased from 3 — activity log only has c3_* entries

# Fallback signal age when enforcement_policy cannot be imported. The live
# value is policy.signal_ttl_s (config: enforcement.signal_ttl_s).
_SIGNAL_MAX_AGE_SECS = 600  # 10 minutes

# Which unlock category each native tool requires
_TOOL_CATEGORY = {
    "Read": "read", "Grep": "read", "Glob": "read",
    "FindFiles": "read", "SearchText": "read", "Edit": "edit",
    "Write": "edit",
}

# Which unlock category each c3 tool grants
_C3_GRANTS = {
    "c3_search": "read", "c3_compress": "read", "c3_read": "read",
    "c3_filter": "read", "c3_validate": "read", "c3_impact": "read",
    "c3_edit": "edit", "c3_edits": "edit", "c3_agent": "both",
    "c3_delegate": "read", "c3_session": "read", "c3_memory": "read",
    "c3_status": "read",
}

# c3 tools that satisfy the "used c3 first" requirement per native tool
_PREREQS = {
    "Read":       {"c3_search", "c3_compress", "c3_read", "c3_filter",
                   "c3_validate", "c3_impact", "c3_edit", "c3_agent", "c3_delegate"},
    "Grep":       {"c3_search", "c3_compress", "c3_filter", "c3_validate",
                   "c3_impact", "c3_agent", "c3_delegate"},
    "Glob":       {"c3_search", "c3_filter", "c3_agent", "c3_delegate"},
    "FindFiles":  {"c3_search", "c3_filter", "c3_agent", "c3_delegate"},
    "SearchText": {"c3_search", "c3_compress", "c3_read", "c3_filter",
                   "c3_impact", "c3_agent", "c3_delegate"},
    "Edit":       {"c3_edit", "c3_edits", "c3_agent"},
    "Write":      {"c3_edit", "c3_edits", "c3_agent"},
    "MultiEdit":  {"c3_edit", "c3_edits", "c3_agent"},
}

# Read-class tools: advisory (allow + nudge when no c3 used first).
# Write-class tools: blocked (ledger integrity).
_ADVISORY_TOOLS = {"Read", "Grep", "Glob", "FindFiles", "SearchText"}
_BLOCKED_TOOLS = {"Edit", "Write", "MultiEdit"}

# Write-class tools as a FIXED fact about the tool, independent of policy.
# The vault guard keys off this rather than `blocked_tools` so that neither an
# enforcement mode nor a config override can open a native write path to the
# credential vault. Keep in sync with _BLOCKED_TOOLS' default membership.
_WRITE_CLASS_ALWAYS = frozenset({"Edit", "Write", "MultiEdit"})

# Vault files no native write may touch, regardless of unlock state.
# Mirrors services.credential_store.VAULT_PROTECTED_FILES (parity-tested);
# duplicated because hooks must stay import-light.
_VAULT_FILES = frozenset({"config.json", "secrets.enc", "cred_state.json",
                          "cred_usage.jsonl", "cred_usage.jsonl.1"})

# Redirect messages per native tool
_REDIRECTS = {
    "Read": (
        "Use c3_compress(file_path='...', mode='map') to map the file first, "
        "then c3_read(file_path='...', symbols=['...']) for surgical extraction."
    ),
    "Grep": (
        "Use c3_search(query='...', action='code') for pattern matching, "
        "or c3_search(query='...', action='semantic') for concept search."
    ),
    "Glob": (
        "Use c3_search(query='...', action='files') for file discovery."
    ),
    "FindFiles": (
        "Use c3_search(query='...', action='files') for file discovery."
    ),
    "SearchText": (
        "Use c3_search(query='...', action='code') for code search."
    ),
    "Edit": (
        "Use c3_edit(file_path='...', old_string='...', new_string='...', summary='...') "
        "for file edits — it reads, patches, writes, and logs in one step."
    ),
    "Write": (
        "Use c3_edit(file_path='...', old_string='...', new_string='...', summary='...') "
        "for file modifications. For a NEW file, c3_edit(file_path='...', old_string='', "
        "new_string='<content>') creates it and logs it."
    ),
}


def _tail_lines(path: Path, n: int) -> list[str]:
    """Read last n lines of a file without loading the whole file.

    Activity logs grow to megabytes over a session; the enforcer only inspects
    the tail window, so reading the whole file on every native tool call was
    pure overhead.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            block = 4096
            chunks = []
            seen_newlines = 0
            pos = size
            while pos > 0 and seen_newlines <= n:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size)
                seen_newlines += data.count(b"\n")
                chunks.append(data)
        blob = b"".join(reversed(chunks))
        text = blob.decode("utf-8", errors="replace")
        return text.splitlines()[-n:]
    except Exception:
        return []


def _record_unlock(project_path: Path, file_path: str, category: str, session_id: str = ""):
    """Add a file path to the sticky unlock map for the given category."""
    if not file_path or not category:
        return
    cats = {"read", "edit"} if category == "both" else {category}
    record_unlocked_files(
        [file_path], cats, session_id=session_id, project_path=project_path,
    )


def _is_file_unlocked(state: dict, file_path: str, category: str) -> bool:
    """Check if a file is unlocked for the given operation category."""
    if not file_path:
        return False
    try:
        normalized = canonical_key(file_path)
    except OSError:
        return False
    cats = state.get("unlocked_files", {}).get(normalized, [])
    return category in cats or "both" in cats


def _check_signal(state: dict, ttl_s: int = _SIGNAL_MAX_AGE_SECS) -> tuple[bool, bool, str]:
    """Inspect the last_c3_call section of the consolidated state.

    Returns (recent, read_unlocked, c3_tool):
      recent:        True if a c3_* tool completed within ttl_s
      read_unlocked: True if that tool was c3_search/c3_compress/c3_filter/…
      c3_tool:       short name of the c3 tool that wrote the signal (e.g.
                     "c3_edit"), or "" if recent is False / unparseable.

    Fails closed: on any parse error, returns (False, False, "").
    """
    last_call = state.get("last_c3_call")
    if not isinstance(last_call, dict):
        return False, False, ""
    try:
        ts = datetime.fromisoformat(last_call["ts"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > ttl_s:
            return False, False, ""
        return True, bool(last_call.get("read_unlocked", False)), str(last_call.get("tool", ""))
    except Exception:
        return False, False, ""


def _check_c3_used(
    project_path: Path,
    state: dict,
    tool_name: str,
    tool_input: dict,
    session_id: str = "",
    ttl_s: int = _SIGNAL_MAX_AGE_SECS,
    blocked_tools: frozenset | None = None,
) -> tuple[bool, str]:
    """Check if a qualifying c3 tool was recently used.

    Returns (allowed, via) where via is one of:
      'signal'   -- fresh c3_* signal (within 10 min) — no reminder
      'unlock'   -- sticky file unlock only, no fresh signal — emit reminder
      'activity' -- activity log hit within last LOOKBACK entries — no reminder
      ''         -- not allowed
    """
    allowed = _PREREQS.get(tool_name, set())
    if not allowed:
        return True, "signal"  # No prereqs defined → allow without reminder

    write_class = _BLOCKED_TOOLS if blocked_tools is None else blocked_tools

    native_target = (
        tool_input.get("file_path", "")
        or tool_input.get("path", "")
        or tool_input.get("pattern", "")
        or tool_input.get("query", "")
        or ""
    )
    required_cat = _TOOL_CATEGORY.get(tool_name, "read")

    # ── Fix 4: signal — primary, fast, reliable ──────────────────────────────
    signal_recent, signal_read_unlocked, signal_tool = _check_signal(state, ttl_s)
    if signal_recent:
        # Bypass fix: for write-class tools (Edit/Write/MultiEdit), the signal
        # may only unlock them when the c3 tool that wrote it actually satisfies
        # this tool's prereqs (e.g. c3_edit/c3_edits/c3_agent). A read-class
        # signal (c3_status, c3_search, …) must NOT unlock a native write.
        if tool_name in write_class:
            if signal_tool in allowed:
                if native_target:
                    _record_unlock(project_path, native_target, required_cat, session_id)
                return True, "signal"
            # Fresh signal exists but it's not a write-prereq tool — fall through
        # Fix 5: Grep/Glob without file path needs a read-unlocking tool
        elif not native_target and tool_name in ("Grep", "Glob", "FindFiles", "SearchText"):
            if signal_read_unlocked:
                return True, "signal"
            # Signal exists but not read-unlocking (e.g. c3_memory) — fall through
        else:
            if native_target:
                _record_unlock(project_path, native_target, required_cat, session_id)
            return True, "signal"

    # ── Sticky file unlock (per-file, persists across turns) ─────────────────
    if native_target and _is_file_unlocked(state, native_target, required_cat):
        return True, "unlock"  # allowed but no fresh signal — emit reminder

    # ── Fix 1: activity log scan (LOOKBACK increased to 20) ──────────────────
    log_file = project_path / ".c3" / "activity_log.jsonl"
    if not log_file.exists():
        return False, ""

    # The evidence window counts TOOL_CALL entries, not raw lines: foreign
    # event types (denial audits, session events) appended after a c3 call
    # must not evict it. Tail a larger raw window, examine at most LOOKBACK
    # tool_call entries.
    try:
        lines = _tail_lines(log_file, LOOKBACK * 10)
    except Exception:
        return False, ""

    examined = 0
    for line in reversed(lines):
        if examined >= LOOKBACK:
            break
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if entry.get("type") != "tool_call":
            continue
        examined += 1

        tool = entry.get("tool", "")
        if tool not in allowed:
            continue
        # ISSUE-3: the log used to count a call that returned "Error: File
        # not found" as "c3 was used". Newer entries carry ok=False; older
        # ones are judged by the summary text.
        if entry.get("ok") is False or response_text_failed(entry.get("result_summary") or ""):
            continue

        if native_target:
            grant = _C3_GRANTS.get(tool, required_cat)
            _record_unlock(project_path, native_target, grant, session_id)
        return True, "activity"

    return False, ""


def _resolve_policy(base: Path):
    """Effective tool-discipline policy, or None when unavailable.

    None means "behave exactly as pre-v2.66" — a missing/broken policy module
    must never be a way to end up with LESS enforcement than the default.
    """
    if enforcement_policy is None:
        return None
    try:
        return enforcement_policy.resolve(str(base))
    except Exception as exc:
        log_hook_error("hook_pretool_enforce:policy", exc)
        return None


def _vault_denial(tool_name: str, tool_input: dict) -> dict | None:
    """Credential-vault write guard — unconditional.

    Deliberately gated on a FIXED write-tool set rather than the configurable
    `blocked_tools`, so no enforcement mode (not even `off`) and no config
    override can open a native write path to the vault.
    """
    if tool_name not in _WRITE_CLASS_ALWAYS:
        return None
    target = Path(str(tool_input.get("file_path") or ""))
    if target.name.lower() in _VAULT_FILES and target.parent.name.lower() == ".c3":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[c3:vault-protected] {target.name} belongs to the "
                    "credential vault and cannot be modified by the agent. "
                    "Ask the user to use the Credentials UI or `c3 creds` CLI."
                ),
            }
        }
    return None


def _outside_project(base: Path, tool_input: dict) -> bool:
    """True when the call targets a path outside the project root.

    Tool discipline exists to keep THIS project's edit ledger complete and
    its index warm. A scratch file in /tmp or a file in some other checkout
    has no ledger here, so a block buys nothing and costs a workaround, and
    a hint is noise (ISSUE-2 of the 2026-08 field report). Relative paths
    resolve against the project root, which is also the hook's cwd. A path
    that cannot be resolved counts as inside — the existing behaviour.
    """
    raw = str(tool_input.get("file_path") or tool_input.get("notebook_path")
              or tool_input.get("path") or "")
    if not raw:
        return False
    try:
        target = Path(raw)
        if not target.is_absolute():
            target = base / target
        # canonical_key: resolved, forward-slash, casefolded — the one path
        # identity the unlock map and Access Guard already agree on.
        target_key = canonical_key(target)
        root_key = canonical_key(base).rstrip("/")
    except (OSError, RuntimeError, ValueError):
        return False
    return not (target_key == root_key or target_key.startswith(root_key + "/"))


# ── Sub-agent tool grants ──────────────────────────────────────────────────
# Claude Code sends `agent_type` (plus `agent_id` inside a sub-agent) with
# every hook payload. A sub-agent whose definition lists `tools:` gets ONLY
# those tools; if none of them reaches the c3 MCP server, the agent has no
# c3_edit to be redirected to, and a strict deny just pushes the write onto
# a shell heredoc that the ledger never sees (field report 2026-08-22,
# ISSUE-1: four self-reports in one session). For that agent only, the
# block degrades to the advisory nudge. An agent with no `tools:` line
# inherits every tool and stays strict; an agent we cannot find stays strict
# but the deny names the fix.

_C3_GRANT_PREFIXES = ("mcp__c3__", "c3_")
_C3_GRANT_EXACT = frozenset({"*", "mcp__*", "mcp__c3"})


def _agent_definition(base: Path, agent_type: str) -> Path | None:
    """The agent file for ``agent_type``: project ``.claude/agents`` first,
    then the user's. Plugin-scoped or path-like names are not looked up."""
    name = str(agent_type or "").strip()
    if not name or any(ch in name for ch in r"/\:") or name.startswith("."):
        return None
    roots = [base / ".claude" / "agents"]
    try:
        roots.append(Path.home() / ".claude" / "agents")
    except Exception:
        pass
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / f"{name}.md"
        if direct.is_file():
            return direct
        nested = next((p for p in root.rglob(f"{name}.md") if p.is_file()), None)
        if nested is not None:
            return nested
    return None


def _frontmatter_tools(text: str) -> list | None:
    """The ``tools:`` grant from an agent file's YAML frontmatter.

    ``None`` when there is no frontmatter or no ``tools:`` key — the agent
    inherits every tool. Handles the inline form (``tools: Read, Write`` or
    ``tools: [Read, Write]``) and the list form (``- Read`` lines).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    body = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        body.append(line)
    for i, line in enumerate(body):
        m = re.match(r"^tools\s*:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest and rest not in ("|", ">"):
            rest = rest.strip("[]")
            return [t.strip().strip("'\"") for t in rest.split(",") if t.strip()]
        tools = []
        for nxt in body[i + 1:]:
            lm = re.match(r"^\s+-\s*(.*)$", nxt)
            if not lm:
                break
            tools.append(lm.group(1).strip().strip("'\""))
        return tools
    return None


def _grant_reaches_c3(tools: list) -> bool:
    for t in tools:
        t = str(t).strip()
        if t in _C3_GRANT_EXACT or t.startswith(_C3_GRANT_PREFIXES):
            return True
    return False


def _agent_cannot_comply(payload: dict, base: Path) -> str | None:
    """A one-line reason when the calling agent provably has no c3 tool to
    use instead, else None (keep strict). Any failure → None."""
    agent_type = str(payload.get("agent_type") or "").strip()
    if not agent_type:
        return None
    try:
        definition = _agent_definition(base, agent_type)
        if definition is None:
            return None
        tools = _frontmatter_tools(definition.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if tools is None or _grant_reaches_c3(tools):
        return None
    return (f"agent '{agent_type}' ({definition.name}) lists tools: with no "
            f"mcp__c3__* entry, so it has no c3_edit to use")


def _override_allows(base: Path, tool_name: str, tool_input: dict,
                     session_id: str) -> str | None:
    """A live discipline grant for this exact write, or None (spec §5).

    Runs AFTER `_vault_denial`, which stays unconditional: no grant, no
    config value and no approval can open a native write path to the vault.
    """
    target = str(tool_input.get("file_path")
                 or tool_input.get("notebook_path") or "")
    if not target:
        return None
    try:
        from services import override_grants as og  # noqa: PLC0415 — lazy
        return og.gate_discipline(base, tool=tool_name, path=target,
                                  session_id=session_id)
    except Exception:
        return None  # fail closed: no grant, ordinary block


def run(payload: dict, project_path: Path | None = None) -> dict | None:
    """Core enforcement logic — importable by the dispatcher and tests.

    Returns a hook-output dict ({"additionalContext": ...} or a deny
    {"hookSpecificOutput": ...}) or None when the call passes silently.
    """
    tool_name = normalize_tool_name(payload.get("tool_name", ""))

    if tool_name == "Bash":
        return _shell_advisory(payload, project_path)

    if tool_name not in _PREREQS:
        return None  # Not a tool we enforce — pass through

    tool_input = payload.get("tool_input", {}) or {}
    session_id = str(payload.get("session_id") or "")
    base = project_path if project_path is not None else Path.cwd()

    # Vault write-guard runs BEFORE the mode check: the credential registry
    # may never be modified by the agent, at any discipline level (v2.61.2).
    vault = _vault_denial(tool_name, tool_input)
    if vault:
        return vault

    # Scope: this hook governs the project's own files. Anything outside the
    # root passes through in every mode — nothing here to keep a ledger of.
    if _outside_project(base, tool_input):
        return None

    policy = _resolve_policy(base)
    mode = getattr(policy, "mode", None) or "strict"
    ttl_s = getattr(policy, "signal_ttl_s", _SIGNAL_MAX_AGE_SECS)
    blocked_tools = getattr(policy, "blocked_tools", None) or _BLOCKED_TOOLS

    # `off`: no tool-discipline nudging at all. Access Guard (hook_access_guard,
    # already yielded ahead of this module) and the vault guard above are
    # untouched — this switch governs workflow preference, not security.
    if mode == "off":
        return _policy_warnings(policy)

    # Session-scoped load: state written by a different session comes back
    # empty, so stale unlocks degrade to the advisory path below.
    state = load_enforcement_state(base, session_id=session_id)

    allowed, via = _check_c3_used(
        base, state, tool_name, tool_input, session_id,
        ttl_s=ttl_s, blocked_tools=blocked_tools,
    )

    if allowed:
        # Sticky-unlock only: gentle drift-guard nudge, still allow.
        if via == "unlock":
            return _with_warnings({
                "additionalContext": (
                    f"[c3:drift-guard] {tool_name} allowed via sticky unlock "
                    f"— no recent c3_* call detected. "
                    f"Prefer c3_search/c3_compress to keep the ledger warm."
                )
            }, policy)
        return _policy_warnings(policy)  # satisfied prereq — allow

    # No c3_* prereq met. Advisory vs blocked split.
    redirect = _REDIRECTS.get(tool_name, "Prefer a c3_* tool.")

    # Write-class blocks only in `strict`. In `advisory` a native write is
    # allowed with a nudge — hook_edit_ledger still records it PostToolUse, so
    # the ledger stays complete either way; what is lost is the pre-edit
    # snapshot c3_edit would have taken.
    if tool_name in blocked_tools and mode == "strict":
        granted = _override_allows(base, tool_name, tool_input, session_id)
        if granted:
            return _with_warnings({"additionalContext": granted}, policy)
        cannot = _agent_cannot_comply(payload, base)
        if cannot:
            return _with_warnings({
                "additionalContext": (
                    f"[c3:hint] Native `{tool_name}` allowed: {cannot}. The edit "
                    f"ledger still records this write. To keep strict discipline "
                    f"for this agent, add `mcp__c3__c3_edit` (or `mcp__c3`) to its "
                    f"tools: list, or drop the tools: line so it inherits every tool."
                )
            }, policy)
        _record_block(tool_name, tool_input, session_id, base)
        reason = (
            f"[c3:enforce] Native `{tool_name}` is blocked to preserve the edit "
            f"ledger. {redirect} "
            f"If this is getting in your way, the user can run "
            f"`c3 enforce advisory`."
        )
        agent_type = str(payload.get("agent_type") or "").strip()
        if agent_type:
            reason += (
                f" Running as agent '{agent_type}': if its tools: grant has no "
                f"mcp__c3__* entry it cannot follow this — add `mcp__c3__c3_edit` "
                f"to the grant, or drop the tools: line so it inherits every tool."
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    # Read-class (any mode) and write-class under `advisory`: allow + hint.
    verb = "is running" if tool_name in _ADVISORY_TOOLS else "wrote"
    return _with_warnings({
        "additionalContext": (
            f"[c3:hint] Native `{tool_name}` {verb} without a prior c3_* call. "
            f"For better index awareness next time: {redirect}"
        )
    }, policy)


def _shell_advisory(payload: dict, project_path: Path | None) -> dict | None:
    """Bash is the escape hatch tool discipline never looked at (field
    report 2026-08-22, ISSUE-1's buried finding): ``python -c "open(f,'w')"``
    or a heredoc was never nudged toward c3_edit and never snapshotted. It
    is NOT denied — blocking shell writes outright would break far more
    legitimate work than it protects — it gets the advisory hint that names
    the files, in strict and advisory modes alike, and hook_edit_ledger
    records the writes after the fact. A fresh write-class c3 signal
    (c3_edit just ran) means the agent is already on the c3 path: silent.
    """
    if shell_write_targets is None:
        return None
    tool_input = payload.get("tool_input", {}) or {}
    cmd = str(tool_input.get("command") or "")
    if not cmd.strip():
        return None
    base = project_path if project_path is not None else Path.cwd()
    policy = _resolve_policy(base)
    mode = getattr(policy, "mode", None) or "strict"
    if mode == "off":
        return _policy_warnings(policy)
    targets = [t for t in shell_write_targets(cmd, str(base))
               if not _outside_project(base, {"file_path": t})]
    if not targets:
        return _policy_warnings(policy)
    session_id = str(payload.get("session_id") or "")
    state = load_enforcement_state(base, session_id=session_id)
    ttl_s = getattr(policy, "signal_ttl_s", _SIGNAL_MAX_AGE_SECS)
    recent, _read_unlocked, signal_tool = _check_signal(state, ttl_s)
    if recent and signal_tool in _PREREQS["Write"]:
        return _policy_warnings(policy)
    shown = []
    for t in targets[:3]:
        try:
            shown.append(str(Path(t).resolve().relative_to(base.resolve())).replace("\\", "/"))
        except (OSError, ValueError):
            shown.append(t)
    more = f" (+{len(targets) - 3} more)" if len(targets) > 3 else ""
    return _with_warnings({
        "additionalContext": (
            f"[c3:hint] This shell command looks like it writes {', '.join(shown)}{more}. "
            f"Shell writes bypass c3_edit: no pre-edit snapshot, and the ledger can only "
            f"record them after the fact as `shell` changes. Prefer "
            f"c3_edit(file_path='...', old_string='...', new_string='...') — old_string='' "
            f"creates a file. (advisory: shell is never blocked)"
        )
    }, policy)


def _record_block(tool_name: str, tool_input: dict, session_id: str,
                  base: Path) -> None:
    """Log a discipline block so `c3 access stats` can show what it costs."""
    if access_telemetry is None:
        return
    try:
        access_telemetry.record(
            layer=access_telemetry.LAYER_DISCIPLINE,
            rule="native-write-blocked",
            scope="discipline",
            tool=tool_name,
            operation="write",
            path=str(tool_input.get("file_path") or ""),
            session_id=session_id,
            project_path=str(base),
        )
    except Exception:
        pass


def _policy_warnings(policy) -> dict | None:
    """Surface a malformed `enforcement` section instead of failing silently."""
    warnings = getattr(policy, "warnings", ()) or ()
    if not warnings:
        return None
    return {"additionalContext": "[c3:enforcement-config] " + "; ".join(warnings)}


def _with_warnings(output: dict, policy) -> dict:
    extra = _policy_warnings(policy)
    if extra:
        output["additionalContext"] = (
            output.get("additionalContext", "") + "\n" + extra["additionalContext"]
        ).strip()
    return output


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        output = run(json.loads(raw))
        if output:
            print(json.dumps(output))

    except Exception as _e:
        log_hook_error("hook_pretool_enforce", _e)


if __name__ == "__main__":
    main()
