"""Override costs — the "this rule is costing you" nudge (spec §11 Threat 5).

The request store answers "what is waiting?"; nothing answered "which rule
keeps asking?". On 2026-09-06 one session filed six holds on
``**/.claude/skills/**`` for ONE file in 65 minutes — four approved by hand,
two lost to the TTL — and every surface rendered each card as if it were the
first. Approval fatigue is the threat §11 calls "the real one", and its
mitigation is a number: how often a rule held, and how the human answered.

This module is that number. It is a READ-ONLY fold over
``~/.c3/oracle/override_requests.json`` grouped by ``(project_path, rule)``
over a trailing window — never a decision, never a mutation, not even the
store's own lazy expiry flip (a lapsed pending row is *counted* as expired
against the supplied clock; flipping it on disk stays with the readers that
already do so). A corrupt or missing store reads as no rows, which is the
honest answer for a nudge: silence, not a crash in a route or a tray.

Consumers: ``GET /api/mobile/overrides/costs`` (desktop tray nudge),
``GET /api/hub/overrides/costs`` (Hub "Costing you" strip),
``c3 override costs``. The desktop shows ``suggestion`` verbatim, so its
three values are part of the wire contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from services import override_grants as og
from services import override_requests as orq

#: Window defaults. ``MAX_DAYS`` is the remote ceiling (the gateway clamps
#: to it); the CLI may ask for more, because a person at the keyboard asking
#: for a quarter's history is not a phone hammering the store.
DEFAULT_DAYS = 7
MAX_DAYS = 30

#: How many holds per rule per window before a surface should say something.
#: Shared by the desktop client (hard-coded there to the same value) and the
#: Hub strip; a rule that held twice is a coincidence, three times is a cost.
NUDGE_THRESHOLD = 3

#: The one-line verdicts. The desktop renders them verbatim — change the
#: wording here and the tray changes with it, so treat them as contract.
SUGGEST_ALLOW = "convert to allow"
SUGGEST_TIGHTEN = "tighten or deny"
SUGGEST_REVIEW = "review"

#: Output row shape, in order. ``count`` is EVERY request in the window
#: (withdrawn included); the four buckets are the human-visible outcomes and
#: deliberately leave ``withdrawn`` out — the agent cancelling its own
#: question says nothing about whether the rule costs the user anything.
FIELDS = (
    "project_path", "rule", "rule_class", "layer",
    "count", "approved", "denied", "expired", "pending",
    "last_at", "suggestion",
)


def suggestion(approved: int, denied: int) -> str:
    """The verdict for one rule's window.

    ``convert to allow`` — the user keeps saying yes and has never said no;
    the rule is friction, not protection, for this project. ``tighten or
    deny`` — the user has said no at least twice; the rule is doing its job
    and the agent should stop asking, or the rule should be narrower so the
    legitimate case stops tripping it. Anything else is ``review``.
    """
    if denied >= 2:
        return SUGGEST_TIGHTEN
    if approved >= 3 and denied == 0:
        return SUGGEST_ALLOW
    return SUGGEST_REVIEW


def _effective_status(row: dict, at: datetime) -> str:
    """A pending row past its ``expires_at`` IS expired, whether or not a
    writer has flipped it yet. Evaluated against the caller's clock so a
    fixed ``now`` in a test sees the same answer the live store would."""
    status = str(row.get("status") or "")
    if status == orq.STATUS_PENDING:
        exp = og.parse_ts(row.get("expires_at"))
        if exp is None or at >= exp:
            return orq.STATUS_EXPIRED
    return status


def _project_key(raw: str, cache: dict) -> str:
    """Grouping identity for a project path — same canonicaliser
    ``list_requests`` filters with, so a row spelled ``Y:/Projects/X`` and a
    filter spelled ``y:\\projects\\x`` land in one group. Empty stays empty:
    ``path_key('')`` would resolve to the CWD and silently adopt orphan rows."""
    raw = str(raw or "")
    if not raw:
        return ""
    if raw not in cache:
        try:
            cache[raw] = og.path_key(raw) or raw.replace("\\", "/").lower()
        except Exception:
            cache[raw] = raw.replace("\\", "/").lower()
    return cache[raw]


def _load_rows() -> list:
    """Every request row, or ``[]`` — a nudge must never raise."""
    try:
        rows = orq.load()
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)]


def rule_costs(project_path: str | None = None, days: int = DEFAULT_DAYS,
               now: datetime | None = None) -> list:
    """Per-``(project_path, rule)`` request counts over the trailing window.

    ``project_path`` narrows to one project (any spelling); ``None`` / ``""``
    means every project in the store — callers serving a token MUST filter
    the result to what that token may see (the store is one file for the
    whole machine). ``days`` is the window length in days, floor 1; the
    window is ``[now - days, now]`` on ``created_at``. ``now`` defaults to
    the wall clock.

    Returns a list of dicts with exactly :data:`FIELDS`, sorted by ``count``
    descending then ``last_at`` descending. Never raises on store trouble.
    """
    at = now or og.now()
    try:
        span = max(1, int(days))
    except (TypeError, ValueError):
        span = DEFAULT_DAYS
    since = at - timedelta(days=span)

    cache: dict = {}
    want = _project_key(project_path or "", cache) if project_path else ""

    groups: dict = {}
    for row in _load_rows():
        created = og.parse_ts(row.get("created_at"))
        if created is None or created < since or created > at:
            continue
        proj_key = _project_key(row.get("project_path", ""), cache)
        if want and proj_key != want:
            continue
        rule = str(row.get("rule") or "")
        key = (proj_key, rule)
        grp = groups.get(key)
        if grp is None:
            grp = groups[key] = {
                "project_path": str(row.get("project_path") or ""),
                "rule": rule,
                "rule_class": str(row.get("rule_class") or ""),
                "layer": str(row.get("layer") or ""),
                "count": 0, "approved": 0, "denied": 0,
                "expired": 0, "pending": 0,
                "last_at": "", "_last_dt": None,
            }
        grp["count"] += 1
        status = _effective_status(row, at)
        if status in ("approved", "denied", "expired", "pending"):
            grp[status] += 1
        if grp["_last_dt"] is None or created > grp["_last_dt"]:
            grp["_last_dt"] = created
            grp["last_at"] = str(row.get("created_at") or "")
            # The newest row is the most honest source for the labels — a
            # rule reclassified mid-window reports what it is NOW.
            grp["rule_class"] = str(row.get("rule_class") or grp["rule_class"])
            grp["layer"] = str(row.get("layer") or grp["layer"])
            grp["project_path"] = str(row.get("project_path") or grp["project_path"])

    out = []
    for grp in groups.values():
        grp["suggestion"] = suggestion(grp["approved"], grp["denied"])
        out.append(grp)
    # count desc, then most recent first. Every group has a `_last_dt` (the
    # first row that opened it set one), so the fallback never fires; it is
    # there so a sort key can never be None.
    out.sort(key=lambda g: (-g["count"],
                            -(g["_last_dt"] or since).timestamp()))
    return [{k: grp[k] for k in FIELDS} for grp in out]


def costing(rules: list, threshold: int = NUDGE_THRESHOLD) -> list:
    """The subset a surface should actually nudge about."""
    return [r for r in rules if int(r.get("count") or 0) >= threshold]
