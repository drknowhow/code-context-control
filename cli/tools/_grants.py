"""Override grants on the MCP content surface (override-requests.md P2a).

The hooks have consulted grants since P1. The `c3_*` content tools have not:
they called the evaluator and refused without ever asking whether the user
had already approved this exact call. §13's coverage matrix recorded that as
"not yet — P2a", which meant an approved request did nothing for any agent
working through MCP — the grant was minted, and then no one looked at it.

This module is the single place those tools ask. It is deliberately thin:
all the policy ordering, TTL clamping, use accounting and audit lives in
`services.override_grants.gate_access`, which is the same call the hooks
make. Two implementations of "may I" is how the two surfaces drift apart.

`session_id` lives here for the same reason. It was duplicated in edit.py,
locks.py and override.py, each carrying a comment saying it must match the
others exactly — and P2a makes that invariant load-bearing rather than
merely tidy: a grant is minted under the session id `c3_override` computed
and consumed under the one `c3_edit` computes. If those two ever disagree,
every approval silently fails to apply.
"""

import os


def session_id(svc) -> str:
    """This agent's identity, for leases and for grants.

    Prefers the HOST session id (``current_session["host_session_id"]``,
    from ``CLAUDE_CODE_SESSION_ID`` — services.session_manager) over C3's
    own timestamp id. The PreToolUse hooks file requests and consume grants
    under the id the host puts in every hook payload, which is that same
    value; until v2.102.0 this surface used C3's own id instead, so every
    hook-filed confirm hold answered ``c3_override(action='wait')`` with
    "belongs to another session" and the flow CLAUDE.md mandates could not
    complete. One id on both surfaces is what makes it true.

    Falls back to C3's session id, then to the process id, never to "". Two
    agents that both resolved to "" would count as ONE session and stop
    blocking each other — the exact opposite of what a lock is for. Each
    Claude Code session runs its own c3-mcp process, so the pid is a
    faithful stand-in.

    It is also the right identity for c3_project: that proxy builds a runtime
    for the TARGET project but runs inside the CALLER's process, so the pid
    keeps the edit attributed to the agent that actually asked for it.
    """
    session = getattr(getattr(svc, "session_mgr", None), "current_session", None) or {}
    host = str(session.get("host_session_id") or "").strip()
    if host:
        return host
    return str(session.get("id", "") or "") or f"pid-{os.getpid()}"


def allow(svc, denial, *, tool: str, op: str, path,
          peek: bool = False) -> str | None:
    """The `[c3-override:granted]` line when a live grant permits this exact
    call, or None to stay denied.

    Consumes a use on success — so callers must invoke this only when they
    are about to proceed, never speculatively; ``peek=True`` is the
    speculative form (a look, no burn) for a caller that has to check
    several targets before it knows whether the call runs at all. Ordering
    inside `gate_access` is policy first, grants second, so a grant is void
    the instant the policy or its layer is switched off (§12.8).

    Fail-closed by construction: any error resolving the grant store leaves
    the caller on its ordinary refusal path. A grant that cannot be read is
    not a grant.
    """
    if denial is None:
        return None
    try:
        from services import override_grants as og  # noqa: PLC0415 — lazy
        return og.gate_access(svc.project_path, denial, tool=tool, op=op,
                              path=str(path), session_id=session_id(svc),
                              peek=peek)
    except Exception:
        return None


def confirm_request(svc, denial, *, tool: str, op: str, path) -> tuple:
    """Auto-file an Override Request for a ``confirm`` denial on the MCP
    surface (docs/confirm-guard.md §3).

    ``(request_id, note)`` — one of them is always ''. Same single-gate
    principle as `allow()`: the filing logic lives in
    `services.override_requests.auto_file`, which the hook surface also
    calls, so the two surfaces cannot drift on dedup, mutes, or rate limits.
    Never raises — a broken request store degrades to S8's "could not be
    filed" tail and the hold stands.
    """
    if denial is None or getattr(denial, "kind", "") != "confirm":
        return "", ""
    try:
        from services import override_requests as orq  # noqa: PLC0415 — lazy
        row, reason = orq.auto_file(svc.project_path, denial=denial, tool=tool,
                                    op=op, path=str(path),
                                    session_id=session_id(svc))
        if row:
            return str(row.get("id") or ""), ""
        return "", reason
    except Exception as exc:
        return "", f"request store unavailable ({exc})"
