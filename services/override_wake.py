"""Wake the asking session when a human decides its override request.

WHY THIS EXISTS — a measured failure, not a hypothetical
--------------------------------------------------------
2026-08-08, live end-to-end run. An agent asked for an override at 00:36Z,
ended its turn, and the user approved from the phone at 00:42:32Z. The grant
was minted correctly and expired unused at 00:57:32Z, because *nothing told
the agent*. Before this module there were exactly two ways an agent could
learn its request had been answered: block in ``c3_override(action='wait')``
— capped at 180s per call, and useless once the turn ends — or call
``action='status'`` later, which requires something to have already woken it.
A decision was a write to disk that nobody was listening for. Grants are
capped at 900s, so an idle agent misses the whole window by construction.

WHAT IT DOES
------------
On every decision (approve *and* deny), run one configured command. That is
the whole feature. The command is how the local orchestrator pokes whatever
runs the agent — a chat message that re-invokes it, a queue write, a webhook
via ``curl``. C3 does not know or care which; wiring an agent runtime in here
would make this file grow a backend per orchestrator and rot.

THREE THINGS THAT ARE DELIBERATE
--------------------------------
**argv, never a shell string.** ``command`` must be a list. There is no
``shell=True`` anywhere in this file and no string form to fall back to, so a
placeholder carrying a quote or a semicolon is an argument, not syntax.
Placeholders are substituted per-element after the list is fixed, so no
substitution can ever add an argument.

**Config-only, and only from a file a human owns.** The spec is read from the
``override.wake`` section of ``.c3/config.json``. Agents cannot write that
file — the builtin ``read_only **/.c3/**`` rule denies it — and
``override_policy.forbidden_target`` refuses to let any grant cover it, so
this cannot be self-approved either. The mobile policy route rejects the
``wake`` key outright (``mobile_api._WAKE_KEY``): a bearer token is not
permission to choose what command this machine runs.

**Synchronous, with a short timeout.** Backgrounding it looks kinder to the
caller and silently loses the wake: ``c3 override approve`` exits the instant
``decide()`` returns, taking any daemon thread with it. So the decision waits
— bounded by ``timeout_s`` (default 10, hard max 60) — and the wake command's
job is to hand off fast, not to do work.

A wake that fails never unwinds a decision. The approval already happened;
the agent falling back to ``action='status'`` is a slower path, not a wrong
one. Every outcome, success or failure, lands in ``.c3/overrides.jsonl``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from services import override_grants as og

#: Lifecycle events, appended to the same audit the rest of the feature uses.
EV_WOKE = "woke"
EV_WAKE_FAILED = "wake_failed"

DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 60

#: Statuses a wake may be requested for. ``expired`` is absent on purpose:
#: nothing decided it, so there is no news to deliver.
VALID_ON = ("approved", "denied")

_VALID_KEYS = frozenset({"command", "cwd", "timeout_s", "on"})


def validate_spec(spec) -> bool:
    """True iff *spec* is a usable ``override.wake`` section.

    Called from ``override_policy._validate``, which treats the whole
    ``override`` section as corrupt when this returns False — the same
    fail-closed reading every other key gets. A wake we cannot parse must not
    degrade into "no wake, carry on quietly": that is the bug this feature
    exists to fix, reintroduced as a typo.
    """
    if spec is None:
        return True
    if not isinstance(spec, dict) or set(spec) - _VALID_KEYS:
        return False
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        return False
    if any(not isinstance(a, str) or not a for a in command):
        return False
    if "cwd" in spec and not isinstance(spec["cwd"], str):
        return False
    if "timeout_s" in spec:
        t = spec["timeout_s"]
        if isinstance(t, bool) or not isinstance(t, int) or t < 1:
            return False
    on = spec.get("on")
    if on is not None:
        if not isinstance(on, list) or not on:
            return False
        if set(on) - set(VALID_ON):
            return False
    return True


def wake_message(row: dict, grant=None) -> str:
    """The line the woken agent reads. Written for a machine, not a human.

    It names the decision, the identifiers needed to act on it, and — the part
    that matters — exactly one next step. An agent woken with "something
    happened" burns a turn rediscovering what; an agent woken with "retry the
    same call once" does the thing the user tapped approve to make happen.
    """
    name = Path(str(row.get("path", ""))).name
    call = f"{row.get('tool', '')} {row.get('op', '')} on {name}"
    rid = row.get("id", "")
    if row.get("status") == "approved":
        g = grant or {}
        return (
            f"[c3-override] APPROVED {rid} — {call}. "
            f"Grant {g.get('id', '')}, {g.get('uses_remaining', '?')} use(s), "
            f"expires {g.get('expires_at', '')}. "
            f"Retry the SAME call once, now, then report the outcome. "
            f"The grant is single-use and session-bound; it dies unused if you wait."
        )
    muted = " The user also muted it: do not ask again this session." \
        if row.get("muted") else ""
    return (
        f"[c3-override] DENIED {rid} — {call}."
        f"{muted} Do not retry and do not re-ask: mark the step blocked and "
        f"tell the user what you needed it for."
    )


def _fields(project_path: str, row: dict, grant=None) -> dict:
    g = grant or {}
    return {
        "request_id": str(row.get("id", "")),
        "session_id": str(row.get("session_id", "")),
        "status": str(row.get("status", "")),
        "decided_by": str(row.get("decided_by", "")),
        "tool": str(row.get("tool", "")),
        "op": str(row.get("op", "")),
        "path": str(row.get("path", "")),
        "path_key": str(row.get("path_key", "")),
        "rule": str(row.get("rule", "")),
        "rule_class": str(row.get("rule_class", "")),
        "layer": str(row.get("layer", "")),
        "grant_id": str(g.get("id", "")),
        "project": str(project_path),
        "project_name": Path(str(project_path)).name,
        "message": wake_message(row, grant),
    }


def _subst(arg: str, fields: dict) -> str:
    """``{name}`` → value, by explicit replace.

    Not ``str.format``: the message and the rule glob both routinely contain
    braces, and a format call would raise KeyError on ``{'a': 1}`` in a
    justification — turning a wake into a silent no-op for the one request
    whose refusal quoted a dict.
    """
    out = arg
    for key, value in fields.items():
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, value)
    return out


def fire(project_path: str, row: dict, grant=None, *, policy=None) -> dict:
    """Run the configured wake command. Returns a small result dict.

    ``{"fired": False, "reason": ...}`` when there is nothing to do — no spec,
    or a status this spec does not subscribe to. Never raises.
    """
    try:
        spec = getattr(policy, "wake", None)
        if spec is None:
            from services import override_policy as opol  # noqa: PLC0415
            spec = getattr(opol.resolve(str(project_path)), "wake", None)
        if not spec:
            return {"fired": False, "reason": "no wake configured"}
        if not validate_spec(spec):
            return {"fired": False, "reason": "invalid wake spec"}

        status = str(row.get("status", ""))
        on = spec.get("on") or list(VALID_ON)
        if status not in on:
            return {"fired": False, "reason": f"status {status!r} not in on={on}"}

        fields = _fields(project_path, row, grant)
        argv = [_subst(a, fields) for a in spec["command"]]
        timeout = min(int(spec.get("timeout_s", DEFAULT_TIMEOUT_S)),
                      MAX_TIMEOUT_S)
        cwd = spec.get("cwd") or str(project_path)
        if not Path(cwd).is_dir():
            og.audit(project_path, EV_WAKE_FAILED, {
                "request_id": fields["request_id"], "reason": "cwd missing",
                "cwd": cwd,
            })
            return {"fired": False, "reason": f"cwd does not exist: {cwd}"}
    except Exception as exc:  # config reading must never break a decision
        return {"fired": False, "reason": f"wake setup failed: {exc}"}

    try:
        proc = subprocess.run(  # noqa: S603 — argv list, shell=False, from a
            argv,               # human-owned config file (see module docstring)
            cwd=cwd,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        og.audit(project_path, EV_WAKE_FAILED, {
            "request_id": fields["request_id"],
            "session_id": fields["session_id"],
            "reason": "timeout", "timeout_s": timeout, "argv0": argv[0],
        })
        return {"fired": True, "ok": False, "reason": f"timed out after {timeout}s"}
    except Exception as exc:
        og.audit(project_path, EV_WAKE_FAILED, {
            "request_id": fields["request_id"],
            "session_id": fields["session_id"],
            "reason": type(exc).__name__, "detail": str(exc)[:200],
            "argv0": argv[0],
        })
        return {"fired": True, "ok": False, "reason": str(exc)}

    ok = proc.returncode == 0
    og.audit(project_path, EV_WOKE if ok else EV_WAKE_FAILED, {
        "request_id": fields["request_id"],
        "session_id": fields["session_id"],
        "status": status,
        "exit_code": proc.returncode,
        # argv0 only. The rest can carry a conversation id or a token-shaped
        # argument, and the audit log is read by more eyes than the config is.
        "argv0": argv[0],
        **({"stderr": (proc.stderr or b"").decode("utf-8", "replace")[:300]}
           if not ok else {}),
    })
    return {"fired": True, "ok": ok, "exit_code": proc.returncode}
