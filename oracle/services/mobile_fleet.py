"""Gateway API v7: the fleet views C3 Desk reads from the Hub, for the phone.

C3 Desk answers "what is going on across every project" by asking the Hub on
loopback. A phone cannot reach the Hub, so these routes serve the same read
models over the authenticated gateway. The payloads come from
``services.fleet_overview``, the module the Hub's own overview routes call,
so Desk and the phone cannot drift apart field by field.

What a phone may CHANGE here is narrower than what Desk may, on purpose:

- Locks: read only. Breaking another agent's lease stays at the machine
  holding the work (the Hub's force-release).
- Access Guard and discipline: the overviews are read-only; per-project rule
  and mode edits stay on the existing ``/access/rule`` and ``/enforcement``
  routes with their limits. ``/enforcement/clear`` and
  ``/overrides/policy/clear`` exist because "inherit again" is not a value
  those routes can write; both ask for the typed word when the result loosens.
- Built-in guards: read, TIGHTEN, and end a lease early. Loosening a
  built-in guard, or minting a lease, is refused here for every token kind —
  it stays with C3 Desk's Hub route, which proves a person with the Desk
  client token. Nothing on this surface can disable a guard.
- Sessions: catalog rows and marks, without transcript text. First and last
  prompts, the turn preview and titles copied from a first prompt stay on
  the machine (docs/sessions.md) unless ``mobile_sessions_transcripts`` is
  switched on. Resuming opens a terminal on the PC, so it is not offered;
  a ``claude.ai/code`` remote link is passed through when the session has
  one.
- Now: live sessions, running jobs, last CI and map freshness. Read only;
  the restart and launch buttons Desk shows act on the PC and are absent.

Imported for its side effect (route registration) at the bottom of
``mobile_api``, so the routes exist before the blueprint is registered.
"""

from __future__ import annotations

from pathlib import Path

from flask import jsonify, request

from oracle.services import mobile_api as m

bp = m.bp

#: Row fields that are verbatim transcript text.
_TRANSCRIPT_FIELDS = ("first_prompt", "last_prompt")
_STALE_FILTERS = {"hide", "only", "likely", "all"}
_RESUME_ELSEWHERE = ("resume opens a terminal on the PC - use C3 Desk or the "
                     "Hub there")


def _discovered() -> list[dict]:
    """``[{name, path}]`` for every project the scanner knows."""
    if m._scanner is None:
        return []
    try:
        entries = m._scanner.discover()
    except Exception:
        return []
    return [{"name": str(p.get("name") or Path(str(p.get("path") or "")).name),
             "path": str(p.get("path") or "")}
            for p in entries if p.get("path")]


def _registered() -> list[dict]:
    """``[{name, path}]`` for scanner projects with a ``.c3`` dir."""
    if m._scanner is None:
        return []
    try:
        entries = m._scanner.discover()
    except Exception:
        return []
    return [{"name": str(p.get("name") or Path(str(p.get("path") or "")).name),
             "path": str(p.get("path") or "")}
            for p in entries if p.get("path") and p.get("has_c3")]


def _name_for(resolved: Path) -> str:
    want = str(resolved)
    for p in _registered():
        try:
            if Path(p["path"]).resolve() == resolved:
                return p["name"]
        except OSError:
            continue
    return Path(want).name


def _cap(name: str) -> bool:
    return name in m._capabilities()


# ── Locks ─────────────────────────────────────────────────

@bp.route("/locks/overview", methods=["GET"])
def mobile_locks_overview():
    """Every project's agent leases in one call. Read-only by design."""
    gate = m._feature_or_404("locks_overview")
    if gate:
        return gate
    from services import fleet_overview
    return jsonify(fleet_overview.locks(_discovered()))


# ── Access Guard ──────────────────────────────────────────

@bp.route("/access/overview", methods=["GET"])
def mobile_access_overview():
    """Each project's own path rules, plus global and builtin, for one glob
    table. ``writable`` says which scopes this gateway will edit, so a client
    shows the rest as read-only instead of collecting 403s."""
    gate = m._feature_or_404("access_overview")
    if gate:
        return gate
    from services import fleet_overview
    body = fleet_overview.access(_discovered())
    body["writable"] = {
        "project": _cap("access_write"),
        "global": _cap("access_write")
        and bool(m._cfg().get("mobile_access_global_scope", False)),
    }
    return jsonify(body)


# ── Tool discipline ───────────────────────────────────────

@bp.route("/enforcement/overview", methods=["GET"])
def mobile_enforcement_overview():
    """Every project's discipline mode, its source and its denials."""
    gate = m._feature_or_404("enforcement_overview")
    if gate:
        return gate
    from services import fleet_overview
    body = fleet_overview.enforcement(_discovered())
    # Global-scope discipline is machine-wide and stays a desktop edit.
    body["writable"] = {"project": _cap("enforcement_write"), "global": False}
    return jsonify(body)


@bp.route("/enforcement/clear", methods=["POST"])
def mobile_enforcement_clear():
    """Drop a project's own discipline mode so it inherits global again.

    Body: ``{project, confirm?}``. Project scope only. When what it would
    inherit is ``off`` (and it is not off already), this is the loosening
    direction and needs ``confirm: "off"`` like setting off directly.
    """
    gate = (m._feature_or_404("enforcement_clear")
            or m._feature_or_404("enforcement_write"))
    if gate:
        return gate
    limited = m._security_gate()
    if limited:
        return limited
    from services import enforcement_policy as ep
    data = request.get_json(silent=True) or {}
    resolved, err = m._project_or_404(data.get("project") or "")
    if err:
        return err
    try:
        current = ep.resolve(str(resolved)).mode or ep.DEFAULT_MODE
        inherited = ep.resolve_global().mode or ep.DEFAULT_MODE
    except (ValueError, TypeError, OSError) as exc:
        return m._svc_error(exc)
    if (inherited == ep.MODE_OFF and current != ep.MODE_OFF
            and not m._confirmed(data, "off")):
        return jsonify({
            "error": "this project would inherit discipline off",
            "needs_confirmation": True, "confirm_with": "off",
        }), 400
    try:
        result = ep.clear(str(resolved), scope="project")
    except (ValueError, TypeError) as exc:
        return m._svc_error(exc)
    if result.get("cleared"):
        detail = {"kind": "enforcement", "action": "clear",
                  "previous": result.get("previous", ""),
                  "mode": result.get("mode", ""),
                  "scope": result.get("scope", ""), "via": "oracle-mobile",
                  "caller": m._caller()}
        try:
            from services.activity_log import ActivityLog
            ActivityLog(str(resolved)).log("access_action", dict(detail))
        except Exception:
            pass
        try:
            from services.edit_ledger import EditLedger
            EditLedger(str(resolved)).log_edit(
                file="enforcement://project",
                change_type="enforcement_clear",
                summary=(f"tool discipline {result.get('previous') or 'default'}"
                         f" -> inherit ({result.get('mode')} from "
                         f"{result.get('scope')}) via mobile"),
                tags=["enforcement", "access"], detail=dict(detail))
        except Exception:
            pass
        m._gw_audit("mobile.enforcement.clear", {"mode": result.get("mode")})
    return jsonify(result)


# ── Override policy ───────────────────────────────────────

@bp.route("/overrides/policy/overview", methods=["GET"])
def mobile_override_policy_overview():
    """Every project's effective override policy, plus the global scope
    (read-only on every surface)."""
    gate = m._feature_or_404("override_policy_overview")
    if gate:
        return gate
    from services import fleet_overview
    body = fleet_overview.override_policy(_discovered())
    body["writable"] = {"project": _cap("override_write"), "global": False}
    return jsonify(body)


@bp.route("/overrides/policy/clear", methods=["POST"])
def mobile_override_policy_clear():
    """Drop a project's own ``override`` opinions so it inherits global.

    Body: ``{project, confirm?}``. Clearing can LOOSEN (the project may have
    been tighter than global), so it needs ``confirm: "widen"`` when it does.
    ``wake`` survives the clear. A corrupt section is a 409.
    """
    gate = (m._feature_or_404("override_policy_clear")
            or m._override_write_gate())
    if gate:
        return gate
    from services import override_policy as opol
    data = request.get_json(silent=True) or {}
    resolved, err = m._project_or_404(data.get("project") or "")
    if err:
        return err
    try:
        kept, widens = opol.clear_section(
            str(resolved), confirmed=m._confirmed(data, "widen"))
    except opol.PolicyEditError as exc:
        return jsonify(exc.payload), exc.status
    except (OSError, ValueError) as exc:
        return m._svc_error(exc)
    cleared = kept is not None
    if cleared:
        m._override_audit("policy_clear",
                          {"id": "", "project_path": str(resolved), "rule": ""},
                          confirmed=bool(widens),
                          detail_extra={"kept": sorted(kept),
                                        "widened": list(widens)})
        m._gw_audit("mobile.override.policy_clear", {"widens": list(widens)})
    return jsonify({"path": str(resolved), "cleared": cleared,
                    "policy": opol.resolve(str(resolved)).as_dict(),
                    "widened": widens})


# ── Built-in guards ───────────────────────────────────────

@bp.route("/access/builtin", methods=["GET"])
def mobile_builtin_overview():
    """Every Tier-1 builtin, its live global mode, per-project modes, leases.

    ``loosen`` is always false here: a looser mode or a lease is set from
    C3 Desk, never from this gateway.
    """
    gate = m._feature_or_404("builtin")
    if gate:
        return gate
    from services import fleet_overview
    try:
        body = fleet_overview.builtin(_discovered())
    except Exception as exc:
        return m._svc_error(exc)
    body["writable"] = {
        "tighten": _cap("builtin_tighten"),
        "tighten_global": _cap("builtin_tighten")
        and bool(m._cfg().get("mobile_access_global_scope", False)),
        "revoke": _cap("builtin_tighten"),
        "loosen": False,
    }
    return jsonify(body)


def _builtin_target(data: dict):
    """``(scope, project_or_None, error_response_or_None)``."""
    scope = str(data.get("scope") or "project").strip().lower()
    if scope not in ("project", "global"):
        return scope, None, (jsonify(
            {"error": "scope must be 'project' or 'global'"}), 400)
    if scope == "global":
        if not m._cfg().get("mobile_access_global_scope", False):
            return scope, None, (jsonify({
                "error": "global-scope guard changes are disabled on this gateway",
                "detail": "a global mode applies to every project on this "
                          "machine; enable mobile_access_global_scope, or use "
                          "C3 Desk.",
            }), 403)
        return scope, None, None
    resolved, err = m._project_or_404(data.get("project") or "")
    if err:
        return scope, None, err
    return scope, str(resolved), None


@bp.route("/access/builtin/mode", methods=["POST"])
def mobile_builtin_mode():
    """Set a built-in guard's mode, in the TIGHTENING direction only.

    Body: ``{scope: project|global, project?, glob, mode}``. A mode at least
    as strict as the one agents see today is applied. A looser one is a 403
    for every token kind, with the Desk route and CLI command that can do it.
    """
    gate = m._feature_or_404("builtin") or m._feature_or_404("builtin_tighten")
    if gate:
        return gate
    limited = m._security_gate()
    if limited:
        return limited
    from services import access_guard
    data = request.get_json(silent=True) or {}
    scope, project, err = _builtin_target(data)
    if err:
        return err
    mode = str(data.get("mode") or "").strip()
    canon = access_guard._norm_builtin(str(data.get("glob") or ""))
    try:
        new = access_guard.builtin_strictness(canon, mode)
        live = (access_guard._scope_modes("global") if scope == "global"
                else access_guard.effective_builtin_modes(project))
        old = access_guard.builtin_strictness(canon, live.get(canon, "default"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if new < old:
        return jsonify({
            "error": "loosening a built-in guard is done from C3 Desk, "
                     "not the phone",
            "needs_desk": True,
            "command": f'c3 access builtin mode "{canon}" {mode}'
                       + (f' --project --path "{project}"'
                          if scope == "project" else ""),
        }), 403
    try:
        result = access_guard.set_builtin_mode(canon, mode, scope,
                                               project or ".")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if result.get("changed"):
        m._access_audit(f"builtin_mode_{result.get('mode')}",
                        result.get("glob") or canon, "builtin", scope,
                        project)
        m._gw_audit("mobile.access.builtin_mode",
                    {"glob": canon, "mode": mode, "scope": scope})
    return jsonify({**result, "loosened": False})


@bp.route("/access/builtin/lease/revoke", methods=["POST"])
def mobile_builtin_lease_revoke():
    """End a temporary loosening now. Tightening, so no confirmation.

    Body: ``{id}``. A lease on an unregistered project is a 404 here, the
    same answer as an unknown one.
    """
    gate = m._feature_or_404("builtin") or m._feature_or_404("builtin_tighten")
    if gate:
        return gate
    limited = m._security_gate()
    if limited:
        return limited
    from services import builtin_leases
    lease_id = str((request.get_json(silent=True) or {}).get("id") or "")
    match = next((row for row in builtin_leases.active()
                  if str(row.get("id") or "") == lease_id), None) \
        if lease_id else None
    if match is None:
        return jsonify({"error": "unknown or already-expired lease"}), 404
    project = str(match.get("project_path") or "")
    if project and m._resolve_registered(project) is None:
        return jsonify({"error": "unknown or already-expired lease"}), 404
    gone = builtin_leases.revoke(lease_id)
    if gone is None:
        return jsonify({"error": "unknown or already-expired lease"}), 404
    m._access_audit("builtin_lease_revoke", gone.get("glob") or "", "builtin",
                    gone.get("scope") or "", gone.get("project_path") or None)
    m._gw_audit("mobile.access.builtin_lease_revoke", {"id": lease_id})
    return jsonify({"revoked": lease_id})


# ── Sessions (history catalog) ────────────────────────────

def _transcripts_allowed() -> bool:
    return bool(m._cfg().get("mobile_sessions_transcripts", False))


def _session_wire(row: dict, transcripts: bool) -> dict:
    """One catalog row for the network.

    Without ``mobile_sessions_transcripts`` the prompt text is blanked and a
    title that IS the first prompt is withheld; resume is reduced to the
    remote link, because launching opens a terminal on the PC.
    """
    out = dict(row)
    if not transcripts:
        for key in _TRANSCRIPT_FIELDS:
            out[key] = ""
        if out.get("title_source") == "first_prompt":
            out["title"] = "(untitled)"
            out["title_source"] = "withheld"
    resume = row.get("resume") or {}
    out["resume"] = {"remote_url": resume.get("remote_url") or None,
                     "can_launch": False, "why_not": _RESUME_ELSEWHERE}
    return out


def _sessions_project():
    """``({name, path}, None)`` for ``?project=``, else an error response."""
    resolved, err = m._project_or_404(str(request.args.get("project") or ""))
    if err:
        return None, err
    return {"name": _name_for(resolved), "path": str(resolved)}, None


@bp.route("/sessions/overview", methods=["GET"])
def mobile_sessions_overview():
    """Per-project session counts for the project rail. Stats files only."""
    gate = m._feature_or_404("sessions")
    if gate:
        return gate
    from concurrent.futures import ThreadPoolExecutor

    from services import session_catalog as sc
    transcripts = _transcripts_allowed()

    def one(p):
        row = {"name": p["name"], "path": p["path"], "initialized": True,
               "error": None,
               "counts": {"total": 0, "live": 0, "stale": 0, "idle": 0},
               "last_active": "", "newest": None, "transcripts": False}
        try:
            row.update(sc.overview(p["path"], name=p["name"]))
        except Exception as e:
            row["error"] = str(e)
        newest = row.get("newest")
        if (not transcripts and isinstance(newest, dict)
                and newest.get("title_source") == "first_prompt"):
            row["newest"] = {**newest, "title": "(untitled)",
                             "title_source": "withheld"}
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, _registered()))
    rows.sort(key=lambda r: r.get("last_active") or "", reverse=True)
    return jsonify({"projects": rows, "transcripts": transcripts,
                    "features": ["list", "detail", "mark", "note", "remote"]})


@bp.route("/sessions", methods=["GET"])
def mobile_sessions_list():
    """Rows for one project (``?project=``) or every project (none).

    Query: ``stale`` hide|only|likely|all (default hide), ``q``, ``limit``
    (default 50, max 200), ``before`` (ISO cursor from ``next_before``).
    Without ``mobile_sessions_transcripts``, ``q`` never matches prompt text
    or a first-prompt title: a hit on hidden text would reveal it a word at
    a time.
    """
    gate = m._feature_or_404("sessions")
    if gate:
        return gate
    from services import session_catalog as sc
    stale = (request.args.get("stale") or "hide").strip().lower()
    if stale not in _STALE_FILTERS:
        return jsonify({"error": f"stale must be one of {sorted(_STALE_FILTERS)}"}), 400
    limit = m._limit_arg()
    q = (request.args.get("q") or "").strip()[:200]
    before = (request.args.get("before") or "").strip()
    transcripts = _transcripts_allowed()
    try:
        if (request.args.get("project") or "").strip():
            proj, err = _sessions_project()
            if err:
                return err
            res = sc.list_sessions(proj["path"], name=proj["name"], stale=stale,
                                   q=q, limit=limit, before=before,
                                   transcripts=transcripts)
            res["errors"] = []
        else:
            res = sc.list_many(_registered(), stale=stale, q=q, limit=limit,
                               before=before, transcripts=transcripts)
    except Exception as exc:
        return m._svc_error(exc)
    res["sessions"] = [_session_wire(r, transcripts)
                       for r in res.get("sessions") or []]
    res["transcripts"] = transcripts
    return jsonify(res)


@bp.route("/sessions/detail", methods=["GET"])
def mobile_sessions_detail():
    """One session: the row plus decisions, the last snapshot and tasks.

    Query: ``project``, ``id`` (full id or a prefix of 8+ characters). The
    turn-by-turn preview is included only with ``mobile_sessions_transcripts``.
    """
    gate = m._feature_or_404("sessions")
    if gate:
        return gate
    from services import session_catalog as sc
    proj, err = _sessions_project()
    if err:
        return err
    try:
        res = sc.get_session(proj["path"], request.args.get("id") or "",
                             name=proj["name"])
    except Exception as exc:
        return m._svc_error(exc)
    if "error" in res:
        return jsonify({"error": res["error"]}), 404
    transcripts = _transcripts_allowed()
    out = _session_wire(res, transcripts)
    if not transcripts:
        out["preview"] = []
    out["transcripts"] = transcripts
    return jsonify(out)


@bp.route("/sessions/mark", methods=["POST"])
def mobile_sessions_mark():
    """Mark a session stale / unstale, or leave a note on it.

    Body: ``{project, id, op: stale|unstale|note, reason?, successor?,
    summary?, next_steps?}``. Recorded as ``by: user`` — a person tapped it.
    """
    gate = m._feature_or_404("sessions") or m._feature_or_404("sessions_write")
    if gate:
        return gate
    from services import session_catalog as sc
    data = request.get_json(silent=True) or {}
    resolved, err = m._project_or_404(str(data.get("project") or ""))
    if err:
        return err
    op = str(data.get("op") or "")
    res = sc.mark(str(resolved), str(data.get("id") or ""), op,
                  reason=str(data.get("reason") or "")[:400],
                  successor=str(data.get("successor") or ""),
                  summary=str(data.get("summary") or "")[:2000],
                  next_steps=str(data.get("next_steps") or "")[:2000],
                  by="user")
    if "error" in res:
        return jsonify({"error": res["error"]}), int(res.get("status") or 400)
    m._gw_audit("mobile.sessions.mark", {"op": op})
    row = res.get("row")
    if isinstance(row, dict):
        res = {**res, "row": _session_wire(row, _transcripts_allowed())}
    return jsonify(res)


# ── Now ───────────────────────────────────────────────────

@bp.route("/now", methods=["GET"])
def mobile_now():
    """Live sessions, running jobs, last CI and map state across projects,
    plus whether the Hub answers. The Desk's Now view, minus its buttons."""
    gate = m._feature_or_404("now")
    if gate:
        return gate
    from services import fleet_overview
    jobs: list[dict] = []
    try:
        from services.shell_jobs import JobStore
        jobs = [r for r in JobStore().list_all(project_path=None,
                                               limit=m._SOURCE_CAP)
                if r.get("status") in ("queued", "running")
                and m._resolve_registered(str(r.get("project_path") or ""))
                is not None]
    except Exception:
        jobs = []
    body = fleet_overview.now(_registered(), job_rows=jobs)
    hub_available = False
    try:
        import json
        import urllib.request
        hub_url = str(m._cfg().get("hub_url", "http://localhost:3330")).rstrip("/")
        with urllib.request.urlopen(f"{hub_url}/api/health", timeout=2) as r:
            hub_available = json.loads(r.read()).get("service") == "c3-hub"
    except Exception:
        hub_available = False
    body["services"] = {
        "oracle": {"up": True, "version": m._c3_version(),
                   "api_version": m.API_VERSION},
        "hub": {"available": hub_available},
    }
    return jsonify(body)
