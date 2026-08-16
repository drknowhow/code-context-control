"""c3_credentials tool — named credential vault, injection-first.

Values NEVER appear in list/describe/check responses. ``reveal`` is gated by
the per-entry ``agent_readable`` flag, which only the user can enable (CLI /
Credentials UI) — ``set`` refuses to raise it on an existing entry. The
``value`` argument is stripped from all logging, and every mutation/reveal is
ledger-logged with identifiers only. The normal way for an agent to USE a
credential is c3_shell: ``env_creds='NAME1,NAME2'`` (env injection) or
``{{cred:NAME}}`` in the command (server-side expansion) — the decoded value
never enters model context.

Structured kinds (address/identity/card) go further: reveal is permanently
disabled regardless of flags, and only individual FIELDS are addressable —
``env_creds='CARD.number'`` or ``{{cred:CARD.number}}``.
"""
from __future__ import annotations

from typing import Any

from services import credential_store as cs

_TOOL = "c3_credentials"

_MUTATING_ACTIONS = {"set", "delete"}
_AUDITED_ACTIONS = _MUTATING_ACTIONS | {"reveal"}
_VALID_ACTIONS = sorted(["list", "describe", "check", "reveal", "set", "delete"])

_USAGE_FOOTER = (
    "use: c3_shell(env_creds='NAME1,NAME2') or {{cred:NAME}} inside cmd — "
    "values are decoded server-side at the subprocess boundary and never "
    "enter model context."
)


def _log_access(svc, action: str, name: str, scope: str, response: str) -> None:
    """Ledger + activity audit for mutations and reveals. Never raises.
    Identifiers only — values are never logged."""
    if not getattr(svc, "edit_ledger", None):
        return
    detail: dict[str, Any] = {
        "kind": "creds", "action": action, "name": name, "scope": scope,
    }
    try:
        svc.edit_ledger.log_edit(
            file=f"cred://{name}",
            change_type=f"cred_{action}",
            summary=response.splitlines()[0][:200] if response else f"cred {action}",
            tags=["creds", action],
            detail=detail,
        )
        if getattr(svc, "activity_log", None):
            svc.activity_log.log("cred_action", detail)
    except Exception:
        pass


def _format_entry_line(name: str, entry: dict, usage: dict) -> str:
    flags = []
    if entry.get("inject"):
        flags.append("inject")
    if entry.get("agent_readable"):
        flags.append("agent_readable")
    last_used = (usage.get(name) or {}).get("last_used", "")
    parts = [
        f"{name} [{entry.get('scope', '?')}/{entry.get('type', 'token')}]",
        f"len={entry.get('value_len', '?')}",
    ]
    display = entry.get("display") or {}
    if display:
        parts.append(" ".join(str(v) for v in display.values() if v))
    if entry.get("env_var"):
        parts.append(f"env_var={entry['env_var']}")
    if flags:
        parts.append("flags=" + ",".join(flags))
    if last_used:
        parts.append(f"last_used={last_used}")
    if entry.get("description"):
        parts.append(f"— {entry['description']}")
    return "  ".join(parts)


def _act_list(project_path: str) -> str:
    entries = cs.list_entries(project_path)
    if not entries:
        return (
            "[creds] no credentials registered.\n"
            "The user manages them via the Credentials UI tab or `c3 creds set`."
        )
    usage = cs.read_usage_state(project_path)
    lines = [f"[creds] {len(entries)} entries (project scope shadows global):"]
    lines += [_format_entry_line(n, e, usage) for n, e in entries.items()]
    lines.append(_USAGE_FOOTER)
    return "\n".join(lines)


def _act_describe(name: str, project_path: str) -> str:
    entry = cs.get_entry(name, project_path=project_path)
    if not entry:
        return f"[creds:unknown] no credential named {name!r}"
    usage = cs.read_usage_state(project_path)
    stype = cs.structured_type(name, project_path=project_path)
    if stype:
        fields = entry.get("fields") or []
        first = fields[0] if fields else "field"
        env_base = entry.get("env_var") or name
        return "\n".join([
            f"[creds] {_format_entry_line(name, entry, usage)}",
            f"storage={entry.get('storage', 'keyring')}  "
            f"created={entry.get('created', '?')}  "
            f"updated={entry.get('updated', '?')}",
            f"fields: {', '.join(fields) or 'none recorded'}",
            f"inject a field: c3_shell(cmd=..., env_creds='{name}.{first}') "
            f"→ ${env_base}_{first.upper()}",
            f"inline expansion:  c3_shell(cmd='... "
            f"{{{{cred:{name}.{first}}}}} ...')",
            f"{stype} entries are inject-only: reveal is permanently "
            "disabled; field values decode only at the subprocess boundary.",
        ])
    env_name = entry.get("env_var") or name
    lines = [
        f"[creds] {_format_entry_line(name, entry, usage)}",
        f"storage={entry.get('storage', 'keyring')}  "
        f"created={entry.get('created', '?')}  updated={entry.get('updated', '?')}",
        f"fingerprint={cs.fingerprint(name, project_path=project_path) or 'unresolvable'}",
        f"inject as env var: c3_shell(cmd=..., env_creds='{name}') → ${env_name}",
        f"inline expansion:  c3_shell(cmd='... {{{{cred:{name}}}}} ...')",
    ]
    if not entry.get("agent_readable"):
        lines.append(
            "value is injection-only (agent_readable=false) — reveal is disabled "
            "for this entry."
        )
    return "\n".join(lines)


def _act_check(name: str, project_path: str) -> str:
    entry = cs.get_entry(name, project_path=project_path)
    if not entry:
        return f"[creds:unknown] no credential named {name!r}"
    # is_resolvable, not get_value: a structured entry never resolves whole,
    # but its payload being decodable is exactly what "check" asks.
    resolvable = cs.is_resolvable(name, project_path=project_path)
    return (
        f"[creds:check] {name} scope={entry['scope']} "
        f"storage={entry.get('storage', 'keyring')} resolvable={str(resolvable).lower()}"
    )


def _act_reveal(name: str, svc, project_path: str) -> str:
    entry = cs.get_entry(name, project_path=project_path)
    if not entry:
        return f"[creds:unknown] no credential named {name!r}"
    stype = cs.structured_type(name, project_path=project_path)
    if stype:
        fields = ", ".join(entry.get("fields") or []) or "none recorded"
        return (
            f"[creds:structured] {name!r} is a {stype} entry — reveal is "
            "permanently disabled for structured kinds, for every caller. "
            f"Use a field at the subprocess boundary instead: "
            f"c3_shell(env_creds='{name}.<field>') or "
            f"{{{{cred:{name}.<field>}}}} in cmd. Fields: {fields}."
        )
    if not entry.get("agent_readable"):
        return (
            f"[creds:not-readable] {name!r} is injection-only — use "
            f"c3_shell env_creds='{name}' or {{{{cred:{name}}}}} in the command, "
            "or ask the user to enable agent_readable in the Credentials UI / "
            f"`c3 creds set {name} --agent-readable`."
        )
    if not cs.verify_agent_readable(name, scope=entry["scope"], project_path=project_path):
        return (
            f"[creds:integrity] {name!r} is marked agent_readable in the registry, "
            "but the flag's keyring attestation is missing or disagrees — the "
            "registry may have been modified outside the credentials API. Ask the "
            "user to re-confirm the flag (toggle it in the Credentials UI or "
            "`c3 creds`); re-saving it restores the attestation."
        )
    value = cs.get_value(name, project_path=project_path, scope=entry["scope"])
    if value is None:
        return (
            f"[creds:no-value] {name!r} is registered in {entry['scope']} scope "
            "but its value is missing from that realm's store."
        )
    cs.register_active_secret(name, value)
    cs.touch_last_used([name], project_path)
    return (
        f"[creds:reveal] {name} (scope={entry['scope']}) — value follows; "
        "it is now part of the conversation context.\n" + value
    )


def handle_credentials(action: str, svc, finalize, **kwargs) -> str:
    """Dispatch a credential-vault action.

    ``svc`` provides ``project_path``, ``edit_ledger``, ``activity_log``.
    ``finalize(tool, args, response, summary)`` logs the call — ``value`` is
    stripped from the logged args before it is ever passed on.
    """
    action = (action or "").strip().lower()
    args_for_log = {k: v for k, v in kwargs.items() if k not in {"value", "token", "secret"}}
    args_for_log["action"] = action
    project_path = getattr(svc, "project_path", ".") or "."

    if not action:
        return finalize(_TOOL, args_for_log, "[creds:error] action is required", "error")
    if action not in _VALID_ACTIONS:
        return finalize(
            _TOOL, args_for_log,
            f"[creds:unknown-action] '{action}'. Valid: {', '.join(_VALID_ACTIONS)}",
            "unknown-action",
        )

    name = (kwargs.get("name") or "").strip()
    if action != "list" and not name:
        return finalize(
            _TOOL, args_for_log,
            f"[creds:error] name is required for {action}", "missing-arg",
        )
    scope = (kwargs.get("scope") or "").strip().lower()

    try:
        if action == "list":
            resp = _act_list(project_path)
        elif action == "describe":
            resp = _act_describe(name, project_path)
        elif action == "check":
            resp = _act_check(name, project_path)
        elif action == "reveal":
            resp = _act_reveal(name, svc, project_path)
        elif action == "set":
            value = kwargs.get("value") or ""
            if not value:
                resp = "[creds:error] value is required for set"
            else:
                scope = scope or "project"
                existing = cs.get_entry(name, project_path=project_path)
                want_readable = bool(kwargs.get("agent_readable"))
                # The agent may set agent_readable only at CREATION time —
                # raising it on an existing entry is a user-only operation.
                if (
                    want_readable
                    and existing
                    and existing.get("scope") == scope
                    and not existing.get("agent_readable")
                ):
                    resp = (
                        f"[creds:not-allowed] agent_readable on existing entry "
                        f"{name!r} can only be enabled by the user "
                        "(Credentials UI or `c3 creds set --agent-readable`)."
                    )
                else:
                    entry = cs.set_credential(
                        name, value,
                        scope=scope,
                        project_path=project_path,
                        description=kwargs.get("description") or "",
                        ctype=kwargs.get("ctype") or "token",
                        env_var=kwargs.get("env_var") or "",
                        agent_readable=want_readable,
                        inject=bool(kwargs.get("inject")),
                    )
                    resp = (
                        f"[creds:set] {name} (scope={scope}, "
                        f"storage={entry['storage']}, len={entry['value_len']})"
                    )
        else:  # delete — action set is closed by the _VALID_ACTIONS gate above
            target = scope or (cs.get_entry(name, project_path=project_path).get("scope") or "")
            if not target:
                resp = f"[creds:unknown] no credential named {name!r}"
            elif cs.delete_credential(name, scope=target, project_path=project_path):
                resp = f"[creds:deleted] {name} (scope={target})"
            else:
                resp = f"[creds:unknown] no credential named {name!r} in {target} scope"
    except cs.CredentialError as exc:
        return finalize(_TOOL, args_for_log, f"[creds:error] {exc}", "error")
    except RuntimeError as exc:  # keyring unavailable
        return finalize(_TOOL, args_for_log, f"[creds:keyring-error] {exc}", "error")

    if action in _AUDITED_ACTIONS and not resp.startswith((
        "[creds:error]", "[creds:unknown", "[creds:not-allowed]",
        "[creds:not-readable]", "[creds:no-value]", "[creds:structured]",
    )):
        _log_access(svc, action, name, scope or "", resp)
    return finalize(_TOOL, args_for_log, resp, action)
