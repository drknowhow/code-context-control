"""c3_schedule — propose, inspect and run scheduled tasks from an agent session.

Deciding a pending approval is human-only (Hub, Desk, phone, ``c3 schedule
approve``); there is deliberately no ``decide`` action here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

READ_ACTIONS = {"list", "get", "runs", "preview", "approvals", "status"}
ACTIONS = ("list", "get", "create", "update", "delete", "enable", "disable",
           "run_now", "runs", "preview", "approvals", "status")


def _store():
    from services.schedule_store import ScheduleStore
    return ScheduleStore()


def _approvals():
    from services.schedule_approvals import ApprovalStore
    return ApprovalStore()


def _defaults() -> dict:
    from oracle.config import load_config
    from oracle.services.scheduler import defaults_from_config
    return defaults_from_config(load_config())


def build_when(*, cron: str = "", every: int = 0, at: str = "", preset: str = "",
               tz: str = "local", preset_at: str = "09:00", days: str = "") -> dict | None:
    """The ``when`` object the flat tool parameters describe, or None when none was given."""
    if cron:
        return {"type": "cron", "expr": cron, "tz": tz or "local"}
    if preset:
        day_list = [d.strip() for d in (days or "").split(",") if d.strip()]
        return {"type": "preset", "preset": preset, "at": preset_at or "09:00", "days": day_list,
                "tz": tz or "local"}
    if at:
        return {"type": "at", "when": at}
    if every:
        return {"type": "every", "seconds": int(every)}
    return None


def build_target(kind: str, target: str) -> dict:
    text = (target or "").strip()
    if kind == "command":
        return {"cmd": text}
    if kind == "prompt":
        return {"prompt": text}
    skill, _, args = text.partition(" ")
    return {"skill": skill, "args": args.strip()}


def _fmt_schedule(row: dict) -> str:
    when = row.get("when") or {}
    cadence = when.get("expr") or (f"every {when.get('seconds')}s" if when.get("type") == "every"
                                    else when.get("when", ""))
    target = row.get("target") or {}
    what = target.get("skill") or target.get("cmd") or (target.get("prompt") or "")[:40]
    flag = "on " if row.get("enabled") else "off"
    model = f" {row['provider']}" + (f"/{row['model']}" if row.get("model") else "")
    if row.get("kind") == "command":
        model = ""
    return (f"  {row['id']} [{flag}] {row['name']!r} {row['kind']} {what}{model} "
            f"@ {cadence} {row.get('autonomy')} next={row.get('next_run_at') or '-'} "
            f"last={row.get('last_status') or '-'}")


def _fmt_run(run: dict) -> str:
    cost = f" ${run['cost_usd']:.3f}" if run.get("cost_usd") is not None else ""
    secs = f" {run['duration_ms'] / 1000:.1f}s" if run.get("duration_ms") else ""
    err = f" — {str(run['error'])[:80]}" if run.get("error") else ""
    return (f"  {run['run_id']} {run.get('status'):<17} {run.get('trigger')} {run.get('schedule_name')!r} "
            f"{run.get('claimed_at', '')[:19]}{secs}{cost}{err}")


def handle_schedule(action, svc, finalize, *, schedule_id="", name="", kind="skill", target="",
                    cron="", every=0, at="", preset="", preset_at="09:00", days="", tz="local",
                    provider="claude", model="", effort="", autonomy="approval", project="",
                    cwd="", enabled=True, timeout=0, max_budget_usd=0.0, permission_mode="",
                    allowed_tools="", check_command="", overlap="skip", misfire_grace_s=-1,
                    run_id="", status="", limit=20) -> str:
    action = (action or "").strip().lower()

    def done(resp, summ="ok"):
        return finalize("c3_schedule", {"action": action}, resp, summ)

    if action not in ACTIONS:
        return done(f"[schedule:error] action must be one of {', '.join(ACTIONS)}", "error")

    from services.schedule_store import RevConflict, ScheduleError

    project_path = str(Path(project or getattr(svc, "project_path", ".") or ".").resolve())
    store = _store()

    try:
        if action == "list":
            rows = store.list(project_path=project_path if project else None)
            if not rows:
                return done("[schedule:list] no schedules", "0")
            return done(f"[schedule:list] {len(rows)} schedule(s)\n" + "\n".join(_fmt_schedule(r) for r in rows),
                        str(len(rows)))

        if action == "get":
            row = store.get(schedule_id)
            if row is None:
                return done(f"[schedule:error] no schedule {schedule_id}", "error")
            import json
            return done(f"[schedule:get]\n{json.dumps(row, indent=2, ensure_ascii=False)}", schedule_id)

        if action == "create":
            when = build_when(cron=cron, every=every, at=at, preset=preset, tz=tz, preset_at=preset_at, days=days)
            if when is None:
                return done("[schedule:error] create needs one of cron, every, at or preset", "error")
            fields = {
                "name": name, "kind": kind, "target": build_target(kind, target), "provider": provider,
                "model": model, "effort": effort, "autonomy": autonomy or "approval",
                "project_path": project_path, "enabled": bool(enabled), "when": when,
                "overlap": overlap, "check_command": check_command, "allowed_tools": allowed_tools,
            }
            if cwd:
                fields["cwd"] = cwd
            if timeout:
                fields["timeout_s"] = int(timeout)
            if max_budget_usd:
                fields["max_budget_usd"] = float(max_budget_usd)
            if permission_mode:
                fields["permission_mode"] = permission_mode
            if misfire_grace_s >= 0:
                fields["misfire"] = {"policy": "grace", "grace_s": int(misfire_grace_s)}
            row = store.create(fields, defaults=_defaults(), created_by="mcp")
            gate = "fires autonomously" if row["autonomy"] == "autonomous" else "asks for approval before each run"
            return done(f"[schedule:created] {row['id']} {row['name']!r} next={row['next_run_at']} — {gate}",
                        row["id"])

        if action == "update":
            if not schedule_id:
                return done("[schedule:error] update needs schedule_id", "error")
            patch: dict = {}
            when = build_when(cron=cron, every=every, at=at, preset=preset, tz=tz, preset_at=preset_at, days=days)
            if when is not None:
                patch["when"] = when
            if name:
                patch["name"] = name
            if target:
                existing = store.get(schedule_id) or {}
                patch["kind"] = kind or existing.get("kind", "skill")
                patch["target"] = build_target(patch["kind"], target)
            for key, value in (("model", model), ("effort", effort), ("provider", provider if provider != "claude" else ""),
                               ("cwd", cwd), ("permission_mode", permission_mode),
                               ("allowed_tools", allowed_tools), ("check_command", check_command)):
                if value:
                    patch[key] = value
            if autonomy and autonomy != "approval":
                patch["autonomy"] = autonomy
            if timeout:
                patch["timeout_s"] = int(timeout)
            if max_budget_usd:
                patch["max_budget_usd"] = float(max_budget_usd)
            if overlap != "skip":
                patch["overlap"] = overlap
            if misfire_grace_s >= 0:
                patch["misfire"] = {"policy": "grace", "grace_s": int(misfire_grace_s)}
            if not patch:
                return done("[schedule:error] update: nothing to change", "error")
            row = store.update(schedule_id, patch, defaults=_defaults(), by="mcp")
            return done(f"[schedule:updated] {row['id']} fields={sorted(patch)} next={row['next_run_at']}", row["id"])

        if action in ("enable", "disable"):
            row = store.set_enabled(schedule_id, action == "enable", by="mcp")
            return done(f"[schedule:{action}d] {row['id']} next={row['next_run_at'] or '-'}", row["id"])

        if action == "delete":
            if not store.delete(schedule_id, by="mcp"):
                return done(f"[schedule:error] no schedule {schedule_id}", "error")
            return done(f"[schedule:deleted] {schedule_id}", schedule_id)

        if action == "run_now":
            row = store.get(schedule_id)
            if row is None:
                return done(f"[schedule:error] no schedule {schedule_id}", "error")
            run = store.request_run(schedule_id, trigger="manual", by="mcp")
            gate = "" if row.get("autonomy") == "autonomous" else " (needs a human approval first)"
            return done(f"[schedule:queued] run {run['run_id']} for {row['name']!r}; the Oracle dispatches it on "
                        f"its next tick{gate}", run["run_id"])

        if action == "runs":
            rows = store.runs(schedule_id=schedule_id or None, status=status or None,
                              project_path=None if schedule_id else project_path, limit=limit)
            if not rows:
                return done("[schedule:runs] no runs", "0")
            return done(f"[schedule:runs] {len(rows)} run(s)\n" + "\n".join(_fmt_run(r) for r in rows), str(len(rows)))

        if action == "preview":
            from services.cron_expr import preview, validate_when
            when = build_when(cron=cron, every=every, at=at, preset=preset, tz=tz, preset_at=preset_at, days=days)
            if when is None:
                return done("[schedule:error] preview needs one of cron, every, at or preset", "error")
            fires = preview(validate_when(when), datetime.now(timezone.utc), max(1, min(limit, 20)))
            return done("[schedule:preview] " + (", ".join(f.isoformat() for f in fires) or "never"), str(len(fires)))

        if action == "approvals":
            rows = _approvals().list(status=status or "pending", limit=limit)
            if not rows:
                return done(f"[schedule:approvals] none {status or 'pending'}", "0")
            lines = [f"  {r['id']} {r['status']} {r['schedule_name']!r} run={r['run_id']} expires={r['expires_at']}"
                     for r in rows]
            return done(f"[schedule:approvals] {len(rows)} — decide from the Hub, Desk, phone or "
                        f"`c3 schedule approve <id>`\n" + "\n".join(lines), str(len(rows)))

        if action == "status":
            enabled = store.list(enabled=True)
            due = sorted(r["next_run_at"] for r in enabled if r.get("next_run_at"))
            pending = len(_approvals().list(status="pending"))
            return done(f"[schedule:status] enabled={len(enabled)} open_runs={len(store.open_runs())} "
                        f"pending_approvals={pending} next_due={due[0] if due else '-'} "
                        f"(the Oracle's tick dispatches; see /api/schedules/status)", "ok")
    except RevConflict as exc:
        return done(f"[schedule:conflict] {exc}", "conflict")
    except ScheduleError as exc:
        return done(f"[schedule:error] {exc}", "error")
    return done("[schedule:error] unhandled action", "error")
