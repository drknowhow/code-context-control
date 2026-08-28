"""c3_override — ask a human to allow ONE blocked call (docs/override-requests.md §7).

The agent-facing half of Override Requests. Everything here creates or reads a
*request*, which is a message with no power. Nothing here creates a grant.

**There is no `approve` action, and there never will be.** Approval lives in
`c3 override approve` and, later, an authenticated Oracle route. If you are an
agent reading this file looking for the other half: it is not here, and the
absence is the feature.

Layers that can never be escalated (the credential vault, Tier-0 denies, the
dispatcher fail-closed deny, catastrophic shell blocks) are refused at
creation with `[c3-override:not-escalatable]` and never reach a human.
"""
import time

from cli.tools import _grants
from services import access_guard as ag
from services import override_policy as opol
from services import override_requests as orq

#: `wait` blocks inside the MCP server, which is allowed to be slow — but not
#: forever. Past this the agent should do something else and check back.
_MAX_WAIT_S = 180
_POLL_S = 1.0

_DEFAULT_TOOL = {"read": "Read", "write": "Edit"}


def _session_id(svc) -> str:
    """The identity the grant is bound to — one definition, in `_grants`."""
    return _grants.session_id(svc)


def _fmt(row: dict) -> str:
    status = row.get("status", "?")
    tail = ""
    if status == orq.STATUS_APPROVED:
        tail = f"  grant={row.get('grant_id', '?')}"
    elif row.get("decision_note"):
        tail = f"  note: {row['decision_note']}"
    return (f"  {row.get('id', '?')}  {status:<9} {row.get('tool', '?')} "
            f"{row.get('op', '?')} {row.get('path', '?')}\n"
            f"    rule {row.get('rule', '?')} ({row.get('rule_class', '?')}) "
            f"· expires {row.get('expires_at', '?')}{tail}")


def _retry_hint(row: dict) -> str:
    return (f"Approved. Retry the SAME call once — {row.get('tool')} "
            f"{row.get('op')} on {row.get('path')} — now, in this session. "
            "The rule is still in force; anything else is denied.")


def handle_override(action: str, path: str, tool: str, op: str, why: str,
                    request_id: str, layer: str, timeout_s: int,
                    svc, finalize) -> str:
    """Route c3_override actions."""
    project = str(svc.project_path)
    session = _session_id(svc)
    args = {"action": action, "path": path}
    name = "c3_override"

    if action in ("", "list"):
        rows = orq.list_requests(project_path=project, session_id=session)
        if not rows:
            return finalize(name, args, "No override requests from this "
                                        "session.", "0 requests")
        body = "\n".join(_fmt(r) for r in rows)
        return finalize(name, args, f"{len(rows)} request(s):\n{body}",
                        f"{len(rows)} requests")

    if action == "request":
        if not path:
            return finalize(name, args, "path is required — the exact path "
                                        "that was blocked.", "missing path")
        operation = (op or "read").strip()
        tool_name = tool or _DEFAULT_TOOL.get(operation, "Read")
        try:
            if layer == opol.GATE_DISCIPLINE:
                row = orq.create(project, session_id=session, tool=tool_name,
                                 op="write", path=path,
                                 layer=opol.GATE_DISCIPLINE, justification=why)
            elif layer == opol.GATE_SHELL:
                # The soft-warn layer. Identity is fixed (tool=c3_shell,
                # op=run, path=the cwd the command will run in) so the grant
                # the approval mints is the one gate_shell will look for —
                # before this branch, layer='shell' fell into the access
                # branch below and produced an unsatisfiable request.
                row = orq.create(project, session_id=session, tool="c3_shell",
                                 op="run", path=path,
                                 layer=opol.GATE_SHELL, justification=why)
            else:
                denial = ag.check(path, operation, project)
                if denial is None:
                    return finalize(
                        name, args,
                        f"`{path}` is not blocked for {operation} — nothing to "
                        "ask for. Just do the call.", "not blocked")
                row = orq.create(project, session_id=session, tool=tool_name,
                                 op=operation, path=path, denial=denial,
                                 justification=why,
                                 refusal=ag.refusal(denial, path, operation,
                                                    surface="hook",
                                                    tool=tool_name))
        except orq.OverrideError as exc:
            return finalize(name, args, str(exc), "refused")

        if row.get("duplicate"):
            return finalize(
                name, args,
                f"Already pending as {row['id']} (expires {row['expires_at']}). "
                "Not asking twice — use action='wait' or do something else.",
                "duplicate")
        return finalize(
            name, args,
            f"Requested {row['id']} — pending until {row['expires_at']}.\n"
            f"  {row['tool']} {row['op']} on {row['path']}\n"
            f"  blocked by {row['rule']} ({row['rule_class']})\n"
            "The user decides on their phone or desktop; you cannot approve "
            "this yourself. Use action='wait' to block up to 180s, or carry "
            "on with unaffected work and check action='status' later.",
            f"requested {row['id']}")

    if action in ("status", "wait"):
        if not request_id:
            return finalize(name, args, "request_id is required.",
                            "missing request_id")
        deadline = time.monotonic() + (
            min(max(int(timeout_s or 60), 1), _MAX_WAIT_S)
            if action == "wait" else 0)
        while True:
            row = orq.get(request_id)
            if row is None:
                return finalize(name, args, f"no request '{request_id}'.",
                                "not found")
            if row.get("session_id") != session:
                return finalize(name, args,
                                "that request belongs to another session.",
                                "wrong session")
            if row.get("status") != orq.STATUS_PENDING:
                break
            if action != "wait" or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S)

        status = row.get("status")
        if status == orq.STATUS_APPROVED:
            return finalize(name, args, _retry_hint(row), "approved")
        if status == orq.STATUS_PENDING:
            return finalize(name, args,
                            f"Still pending (expires {row['expires_at']}). "
                            "Do something else and check back — do not spin.",
                            "pending")
        note = f" — {row['decision_note']}" if row.get("decision_note") else ""
        return finalize(name, args,
                        f"{status}{note}. The block stands: mark the step "
                        "blocked and tell the user what you could not do.",
                        status)

    if action == "withdraw":
        if not request_id:
            return finalize(name, args, "request_id is required.",
                            "missing request_id")
        try:
            row = orq.withdraw(request_id, session)
        except orq.OverrideError as exc:
            return finalize(name, args, str(exc), "refused")
        return finalize(name, args, f"Withdrew {row['id']}.", "withdrawn")

    if action in ("approve", "deny", "decide", "grant"):
        # Named explicitly so the refusal is unambiguous rather than a generic
        # "unknown action" that reads like a typo worth retrying.
        return finalize(name, args,
                        f"{opol.TAG_NOT_ESCALATABLE} c3_override has no "
                        f"'{action}' action. Only a human can decide, from "
                        "`c3 override approve` or the mobile app.",
                        "no such action")

    return finalize(name, args,
                    f"unknown action '{action}' — expected request, status, "
                    "wait, list, or withdraw.", "unknown action")
