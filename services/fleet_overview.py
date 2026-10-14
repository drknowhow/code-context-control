"""Cross-project read models shared by the Hub and the Oracle mobile gateway.

The Hub's overview routes (Locks, Permissions, Rules, Built-in guards) and the
gateway's (API v7) answer the same question for two audiences: C3 Desk on
loopback and a paired phone over the tailnet. Each function here takes the
project list its caller trusts — the Hub's registry, the gateway's scanner —
and builds the payload, so the two surfaces cannot drift apart field by field.

Every builder isolates rows: one unreadable project reports ``error`` (or
``initialized: false``) instead of blanking the page, and never reads as the
empty/clear state, because "no leases" or "no rules" is a claim about a repo
we could not see.

None of these touch a keyring except ``builtin`` (modes and leases are
attested there), so the bulk loops stay cheap.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

LOCKS_COVERAGE_NOTE = (
    "Leases gate C3 tool surfaces only. A raw shell redirect, a non-Claude "
    "agent, or a human in an editor is not covered.")

ENFORCEMENT_COVERAGE_NOTE = (
    "Tool discipline only governs whether native Edit/Write are pushed "
    "through c3_edit. At every mode — including off — Access Guard path "
    "rules, the credential-vault write guard, and agent locks still enforce. "
    "The edit ledger records native writes either way; strict additionally "
    "gets c3_edit's pre-edit snapshot.")

OVERRIDE_POLICY_NOTE = (
    "Project and global policy merge by tightening only: a layer is "
    "escalatable iff both scopes allow it, and the numeric limits take the "
    "smaller value.")


def _rows(projects: Iterable[dict]) -> list[tuple[str, str]]:
    out = []
    for p in projects or []:
        path = str(p.get("path") or "")
        if path:
            out.append((str(p.get("name") or Path(path).name), path))
    return out


def _initialized(path: str) -> bool:
    return (Path(path) / ".c3").is_dir()


# ── Locks ──────────────────────────────────────────────────────────────────

def locks(projects: Iterable[dict]) -> dict:
    """Every project's agent leases. Read-only; force-release lives elsewhere."""
    from services import agent_locks as al

    rows, total = [], 0
    for name, ppath in _rows(projects):
        row = {"name": name, "path": ppath, "initialized": True, "error": None,
               "enabled": True, "mode": "advisory", "locks": [], "count": 0,
               "fencing": 0}
        try:
            if not _initialized(ppath):
                row["initialized"] = False
            else:
                cfg = al.config(ppath)
                row["enabled"] = cfg["enabled"]
                row["mode"] = cfg["mode"]
                snap = al.store_for(ppath).snapshot()
                row["locks"] = snap["locks"]
                row["count"] = snap["count"]
                row["fencing"] = snap["fencing"]
                total += snap["count"]
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    return {"projects": rows, "total": total,
            "coverage_note": LOCKS_COVERAGE_NOTE}


# ── Access Guard path rules ────────────────────────────────────────────────

def access(projects: Iterable[dict]) -> dict:
    """Each project's own path rules, plus the global scope and the builtins.

    A corrupt scope is reported as corrupt: it evaluates deny-all until
    repaired by hand, the opposite of what an empty list would suggest.
    Builtin rules are read once, from the global realm.
    """
    from services import access_guard

    empty = {"deny": [], "read_only": [], "confirm": [], "mask": 0,
             "corrupt": False}
    rows = []
    for name, ppath in _rows(projects):
        row = {"name": name, "path": ppath, "initialized": True,
               "error": None, "rules": dict(empty)}
        try:
            if not _initialized(ppath):
                row["initialized"] = False
            else:
                row["rules"] = access_guard.scope_rules("project", ppath)
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)

    try:
        global_rules = {**access_guard.scope_rules("global"), "error": None}
    except Exception as e:
        global_rules = {**empty, "error": str(e)}
    try:
        home = access_guard._global_base()
        b = access_guard.list_rules(str(home) if home else ".")["builtin"]
        builtin_rules = {"deny": list(b.get("deny") or []),
                         "read_only": list(b.get("read_only") or []),
                         "confirm": list(b.get("confirm") or []), "error": None}
    except Exception as e:
        builtin_rules = {"deny": [], "read_only": [], "confirm": [],
                         "error": str(e)}

    return {
        "projects": rows,
        "global": global_rules,
        "builtin": builtin_rules,
        # Strictest last. "allow" is the absence of a rule, not a kind.
        "kinds": ["confirm", "read_only", "deny"],
        "coverage_note": access_guard.COVERAGE_MATRIX,
    }


# ── Tool discipline ────────────────────────────────────────────────────────

def enforcement(projects: Iterable[dict]) -> dict:
    """Each project's effective discipline mode, its source, and denials.

    A project we could not read reports ``error`` rather than a mode —
    claiming 'strict' for a repo we failed to read would be a guess.
    """
    from services import access_telemetry as at
    from services import enforcement_policy as ep

    rows = []
    totals = {"discipline": 0, "access": 0}
    for name, ppath in _rows(projects):
        row = {"name": name, "path": ppath, "initialized": True,
               "error": None, "mode": None, "scope": "", "set_by": "",
               "signal_ttl_s": ep.DEFAULT_SIGNAL_TTL_S, "warnings": [],
               "tier": "", "tier_implies": "", "denials": {}, "denial_total": 0}
        try:
            if not _initialized(ppath):
                row["initialized"] = False
            else:
                policy = ep.resolve(ppath)
                row["mode"] = policy.mode
                row["scope"] = policy.scope
                row["set_by"] = policy.set_by
                row["signal_ttl_s"] = policy.signal_ttl_s
                row["warnings"] = list(policy.warnings)
                row["blocked_tools"] = sorted(policy.blocked_tools)

                cfg_path = Path(ppath) / ".c3" / "config.json"
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    tier = str(cfg.get("permission_tier") or "")
                except Exception:
                    tier = ""
                row["tier"] = tier
                row["tier_implies"] = ep.derive_from_tier(tier) if tier else ""

                agg = at.aggregate(ppath)
                row["denials"] = agg["by_layer"]
                row["denial_total"] = agg["total"]
                row["top_denials"] = [
                    {**r, "fix": at.suggest(r)} for r in agg["rows"][:5]
                ]
                for layer, count in agg["by_layer"].items():
                    totals[layer] = totals.get(layer, 0) + count
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)

    # The global (~/.c3) section. A corrupt global config must not blank the
    # page — report the error inside the object.
    try:
        g = ep.resolve_global()
        global_policy = {
            "configured": g.scope == "global",
            "mode": g.mode or None,
            "set_by": g.set_by,
            "signal_ttl_s": g.signal_ttl_s,
            "blocked_tools": sorted(g.blocked_tools),
            "warnings": list(g.warnings),
        }
    except Exception as e:
        global_policy = {"configured": False, "mode": None, "error": str(e)}

    return {
        "projects": rows,
        "modes": [{"id": m, "help": ep.MODE_HELP[m]} for m in ep.MODES],
        "default_mode": ep.DEFAULT_MODE,
        "tier_map": ep.TIER_TO_MODE,
        "totals": totals,
        "global_policy": global_policy,
        # Stated so a client can never imply that turning discipline down
        # also turns the security boundaries down. Mirrors docs/enforcement.md.
        "coverage_note": ENFORCEMENT_COVERAGE_NOTE,
    }


# ── Override policy ────────────────────────────────────────────────────────

def override_policy(projects: Iterable[dict]) -> dict:
    """Each project's effective override policy, plus the global scope.

    ``project_keys`` names what the project pins itself, so a screen can tell
    an inherited value from a chosen one. The global scope is READ-ONLY on
    every surface: ``write_section`` is project-scope by design.
    """
    from services import override_policy as opol

    rows = []
    for name, ppath in _rows(projects):
        row = {"name": name, "path": ppath, "initialized": True,
               "error": None, "policy": None, "configured": False,
               "corrupt": False, "project_keys": []}
        try:
            if not _initialized(ppath):
                row["initialized"] = False
            else:
                keys, present, corrupt = opol.scope_keys(ppath)
                row["project_keys"] = keys
                row["configured"] = present
                row["corrupt"] = corrupt
                row["policy"] = opol.resolve(ppath).as_dict()
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)

    try:
        keys, present, corrupt = opol.global_scope_keys()
        global_policy = {"configured": present, "corrupt": corrupt,
                         "keys": keys, "error": None,
                         "policy": opol.resolve_global().as_dict()}
    except Exception as e:
        global_policy = {"configured": False, "corrupt": False, "keys": [],
                         "error": str(e), "policy": None}

    return {
        "projects": rows,
        "global_policy": global_policy,
        "defaults": {k: v for k, v in opol.DEFAULTS.items()
                     if k != opol.WAKE_KEY},
        "layers": list(opol.LAYER_KEYS),
        "typed_confirm_layers": sorted(opol.TYPED_CONFIRM_LAYERS),
        "hard_max_ttl_s": opol.HARD_MAX_TTL_S,
        "coverage_note": OVERRIDE_POLICY_NOTE,
        # What the surface can do beyond the per-project GET/POST, so a client
        # built against a newer C3 can hide what an older one cannot serve.
        "features": ["clear"],
    }


# ── Built-in (Tier-1) guards ───────────────────────────────────────────────

def builtin(projects: Iterable[dict]) -> dict:
    """Every Tier-1 builtin with its live global mode, each project's own
    modes, and the live temporary leases.

    Modes are read through the keyring (config and attestation must agree),
    so a mode listed here is one evaluation honours. ``projects`` holds only
    projects that set a mode of their own, or could not be read.
    ``strictness`` ranks each mode 0 (allow) to 3 (full deny), so a client
    can tell loosening from tightening without knowing the tiers.

    Raises when the global realm cannot be read — the caller answers 500
    rather than presenting every guard at its default.
    """
    from services import access_guard, builtin_leases

    global_modes = access_guard._scope_modes("global")
    home = access_guard._global_base()
    guards = []
    for glob in access_guard.DISABLEABLE_BUILTINS:
        canon = access_guard._norm_builtin(glob)
        guards.append({
            "glob": canon,
            "global": global_modes.get(canon, "default"),
            "strictness": {m: access_guard.builtin_strictness(canon, m)
                           for m in (*access_guard.BUILTIN_MODES, "default")},
        })
    rows = []
    for name, ppath in _rows(projects):
        row = {"name": name, "path": ppath, "modes": {}, "error": None}
        try:
            base = Path(ppath).resolve()
            if (base / ".c3").is_dir() and base != home:
                row["modes"] = access_guard._scope_modes("project", ppath)
        except Exception as e:
            row["error"] = str(e)
        if row["modes"] or row["error"]:
            rows.append(row)
    return {"guards": guards, "projects": rows,
            "leases": builtin_leases.active()}


# ── Now: what is running across the fleet ──────────────────────────────────

_STATS_TAIL_BYTES = 256 * 1024
_CI_MAX_PROJECTS = 8


def _tail_jsonl(path: Path, max_bytes: int = _STATS_TAIL_BYTES) -> list[dict]:
    """The parsed rows of the last ``max_bytes`` of a JSONL file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial first line
            block = fh.read()
    except OSError:
        return []
    out = []
    for raw in block.splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _newest_stats(ppath: str) -> dict[str, dict]:
    """Newest session_stats row per host session id.

    Rows are cumulative per session, so the newest one IS the total — they
    are never summed.
    """
    newest: dict[str, dict] = {}
    for row in _tail_jsonl(Path(ppath) / ".c3" / "session_stats.jsonl"):
        sid = str(row.get("session_id") or "")
        if sid:
            newest[sid] = row
    return newest


def _last_c3_call(ppath: str) -> tuple[str, dict]:
    """``(host_session_id, {ts, tool})`` from enforcement_state.json.

    Only the session id and the last call are read; the file's unlocked-path
    list stays where it is.
    """
    try:
        data = json.loads((Path(ppath) / ".c3" / "enforcement_state.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return "", {}
    call = data.get("last_c3_call") if isinstance(data, dict) else None
    if not isinstance(call, dict):
        return "", {}
    return (str(data.get("session_id") or ""),
            {"ts": str(call.get("ts") or ""), "tool": str(call.get("tool") or "")})


def _map_state(ppath: str, now: float) -> dict:
    c3 = Path(ppath) / ".c3"
    meta = c3 / "map.meta.json"
    if not meta.exists():
        return {"state": "missing", "generated_at": None, "age_s": None}
    generated_at, age = None, None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        generated_at = data.get("generated_at") or None
    except Exception:
        pass
    if generated_at:
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            age = max(0, int(now - ts.timestamp()))
        except Exception:
            age = None
    state = "dirty" if (c3 / "map.dirty").exists() else (
        "fresh" if generated_at else "unknown")
    return {"state": state, "generated_at": generated_at, "age_s": age}


def _session_row(beat: dict, stats: dict[str, dict], last_sid: str,
                 last_call: dict) -> dict:
    host = str(beat.get("host_session_id") or "")
    c3id = str(beat.get("session_id") or "")
    st = stats.get(host) or stats.get(c3id) or {}
    tool = last_call if last_call and last_sid and last_sid in (host, c3id) else {}
    tokens_in = int(st.get("input_tokens") or 0)
    tokens_out = int(st.get("output_tokens") or 0)
    return {
        "session_id": c3id,
        "host_session_id": host,
        "short": (host or c3id)[:8],
        "ide": str(beat.get("ide") or ""),
        "heartbeat_age_s": round(float(beat.get("age_seconds") or 0.0), 1),
        "last_tool": tool.get("tool") or None,
        "last_tool_at": tool.get("ts") or None,
        "model": st.get("model") or None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "cost_usd": st.get("cost_usd"),
    }


def now(projects: Iterable[dict], *, job_rows: list[dict] | None = None,
        ci_runs=None) -> dict:
    """Live sessions, running jobs, last CI and map state, across projects.

    Liveness is the MCP heartbeat under ``.c3/live`` (services.session_live),
    never an inference from activity rows. Only projects with a live session
    or a running job are listed in full; every project still counts toward
    the totals. ``job_rows`` is the caller's already-filtered job list (the
    store is shared machine-wide); ``ci_runs(path, limit)`` is injectable for
    tests and asked for at most eight projects.
    """
    from services import session_live

    if ci_runs is None:
        from services.ci_runner import list_runs as ci_runs

    t = time.time()
    running_by_project: dict[str, list[dict]] = {}
    for job in job_rows or []:
        if job.get("status") in ("queued", "running"):
            key = str(Path(str(job.get("project_path") or "")).resolve()).lower()
            running_by_project.setdefault(key, []).append({
                "id": job.get("id"), "status": job.get("status"),
                "cmd_display": job.get("cmd_display"),
                "started_at": job.get("started_at") or job.get("created_at"),
            })

    rows, errors = [], []
    live_total = running_total = 0
    for name, ppath in _rows(projects):
        if not _initialized(ppath):
            continue
        try:
            beats = session_live.live_sessions(ppath, prune=False)
        except Exception as e:
            errors.append({"path": ppath, "error": str(e)})
            beats = []
        try:
            jobs = running_by_project.get(str(Path(ppath).resolve()).lower(), [])
        except OSError:
            jobs = []
        if not beats and not jobs:
            continue
        stats = _newest_stats(ppath) if beats else {}
        last_sid, last_call = _last_c3_call(ppath) if beats else ("", {})
        sessions = [_session_row(b, stats, last_sid, last_call) for b in beats]
        live_total += len(sessions)
        running_total += len(jobs)
        rows.append({"name": name, "path": ppath, "sessions": sessions,
                     "running_jobs": jobs, "map": _map_state(ppath, t),
                     "ci": None})

    rows.sort(key=lambda r: (-len(r["sessions"]), -len(r["running_jobs"]),
                             r["name"].lower()))
    for row in rows[:_CI_MAX_PROJECTS]:
        try:
            runs = ci_runs(row["path"], limit=1) or []
            row["ci"] = runs[0] if runs else None
        except Exception as e:
            errors.append({"path": row["path"], "error": f"ci: {e}"})

    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
            "projects": rows, "live_sessions": live_total,
            "running_jobs": running_total, "errors": errors}
