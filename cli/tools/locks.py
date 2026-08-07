"""c3_locks — agent leases on files (docs/agent-locks.md Layer B).

Acquisition is normally IMPLICIT: c3_edit takes a lease on the file it is
editing, with an intent derived from the edit summary. That was a deliberate
choice (spec §14) — an explicit step produces better intent strings, but
agents forget explicit steps, and a lock nobody takes protects nothing.

This tool exists for the cases implicit cannot cover: declaring a lease over
several files BEFORE starting a multi-file refactor, seeing who holds what,
and letting go early instead of waiting out the TTL.

force_release is deliberately absent: it is a human override that bumps the
fencing counter, and it lives in `c3 locks force-release` / the Hub tab.
"""
from cli.tools import _grants
from services import agent_locks as al


def _session_id(svc) -> str:
    """A lease taken by c3_edit has to be recognised as ours by c3_locks, and
    vice versa — so both read the one definition in `_grants`."""
    return _grants.session_id(svc)


def _split(paths: str) -> list:
    return [p.strip() for p in (paths or "").split(",") if p.strip()]


def _fmt_lease(row: dict) -> str:
    left = int(max(0, row.get("expires_in_s", 0)))
    mins, secs = divmod(left, 60)
    intent = (row.get("intent") or "").strip()
    tail = f'  "{intent}"' if intent else ""
    return (f"  {row.get('relpath','?'):<44} {row.get('agent_id','?'):<22} "
            f"{mins}m{secs:02d}s{tail}")


def handle_locks(action: str, paths: str, intent: str, ttl_s: int,
                 svc, finalize) -> str:
    """Route c3_locks actions."""
    project = str(svc.project_path)
    session = _session_id(svc)
    agent = al.agent_id_for(session)
    args = {"action": action}

    cfg = al.config(project)
    if not cfg["enabled"]:
        return finalize("c3_locks", args,
                        "Agent Locks are disabled for this project "
                        "(.c3/config.json → locks.enabled=false).", "disabled")

    store = al.store_for(project)

    if action in ("", "list", "status"):
        snap = store.snapshot()
        if not snap["locks"]:
            return finalize("c3_locks", args,
                            f"No active leases in {project} "
                            f"(mode={cfg['mode']}).", "0 leases")
        head = (f"{snap['count']} lease(s) — mode={cfg['mode']}, "
                f"you are {agent}\n"
                f"  {'file':<44} {'holder':<22} left")
        body = "\n".join(_fmt_lease(r) for r in snap["locks"])
        return finalize("c3_locks", args, head + "\n" + body,
                        f"{snap['count']} leases")

    if action == "acquire":
        targets = _split(paths)
        if not targets:
            return finalize("c3_locks", args,
                            "paths is required (comma-separated)", "missing paths")
        res = store.acquire(targets, agent_id=agent, session_id=session,
                            intent=intent, ttl_s=ttl_s or None)
        if res.get("error") == "unsupported_path":
            return finalize("c3_locks", args,
                            f"{al.TAG_UNAVAILABLE} {res['detail']}\n"
                            "  Paths must live inside this project; UNC and "
                            "outside-repo forms are refused rather than guessed.",
                            "unsupported path")
        if not res.get("granted"):
            lines = "\n".join(
                f"  {c['relpath']} — held by {c['owner']}"
                + (f' ("{c["intent"]}")' if c.get("intent") else "")
                + f", {int(c['expires_in_s'])}s left"
                for c in res.get("conflicts", []))
            return finalize(
                "c3_locks", args,
                f"{al.TAG_HELD} nothing was acquired — acquisition is "
                f"all-or-nothing, so a partial grab never happens.\n{lines}\n"
                "  This is a policy block, not a transient error. Do not route "
                "around it. Work elsewhere, or ask the holder to release.",
                f"denied ({len(res.get('conflicts', []))} conflicts)")
        held = ", ".join(x["relpath"] for x in res["locks"])
        return finalize("c3_locks", args,
                        f"Leased {len(res['locks'])} file(s) for {agent}: {held}"
                        + (f'\n  intent: "{intent}"' if intent else ""),
                        f"{len(res['locks'])} leased")

    if action == "release":
        res = store.release(_split(paths) or None, session_id=session)
        if not res["count"]:
            return finalize("c3_locks", args, "Nothing to release.", "0 released")
        return finalize("c3_locks", args,
                        f"Released {res['count']}: {', '.join(res['released'])}",
                        f"{res['count']} released")

    if action == "renew":
        res = store.renew(_split(paths), session_id=session, ttl_s=ttl_s or None)
        if not res["ok"]:
            bad = ", ".join(f"{r['relpath']} ({r['reason']})"
                            for r in res["rejected"])
            return finalize("c3_locks", args,
                            f"Renewed {len(res['renewed'])}; rejected: {bad}",
                            "partial renew")
        return finalize("c3_locks", args,
                        f"Renewed {len(res['renewed'])} lease(s).",
                        f"{len(res['renewed'])} renewed")

    if action == "sweep":
        res = store.sweep()
        return finalize("c3_locks", args,
                        f"Swept {res['count']} expired lease(s)."
                        + (f" {', '.join(res['expired'])}" if res["expired"] else ""),
                        f"{res['count']} swept")

    return finalize("c3_locks", args,
                    f"Unknown action '{action}'. "
                    "Use: list | acquire | release | renew | sweep",
                    "unknown action")
