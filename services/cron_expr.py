"""5-field cron expressions and the ``when`` shapes a schedule row carries.

A ``when`` is one of::

    {"type": "cron",   "expr": "0 9 * * 1-5", "tz": "local"}
    {"type": "every",  "seconds": 3600}
    {"type": "at",     "when": "2026-10-14T09:00:00+00:00"}
    {"type": "preset", "preset": "weekdays", "at": "09:00", "days": [], "tz": "local"}

``next_fire`` turns any of them into the first aware UTC datetime strictly
after a reference instant, or ``None`` when nothing is left to fire.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WHEN_TYPES = ("cron", "every", "at", "preset")
PRESETS = ("hourly", "daily", "weekdays", "weekly", "monthly")

MIN_EVERY_SECONDS = 60
SEARCH_LIMIT_DAYS = 366

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_DOWS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset
    day_restricted: bool
    weekday_restricted: bool

    def matches_day(self, dt: datetime) -> bool:
        if dt.month not in self.months:
            return False
        dom = dt.day in self.days
        dow = _cron_weekday(dt) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom or dow
        if self.day_restricted:
            return dom
        if self.weekday_restricted:
            return dow
        return True


def _cron_weekday(dt: datetime) -> int:
    return (dt.weekday() + 1) % 7


def _parse_field(text: str, lo: int, hi: int, names: dict, label: str) -> tuple[frozenset, bool]:
    values: set[int] = set()
    restricted = False
    for item in text.split(","):
        item = item.strip().lower()
        if not item:
            raise ValueError(f"{label}: empty list item in {text!r}")
        step = 1
        if "/" in item:
            item, step_text = item.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"{label}: bad step in {text!r}")
            step = int(step_text)
        if item == "*":
            start, end = lo, hi
            if step > 1:
                restricted = True
        elif "-" in item:
            a, b = item.split("-", 1)
            start, end = _atom(a, lo, hi, names, label), _atom(b, lo, hi, names, label)
            if end < start:
                raise ValueError(f"{label}: descending range {item!r}")
            restricted = True
        else:
            start = _atom(item, lo, hi, names, label)
            end = hi if step > 1 else start
            restricted = True
        values.update(range(start, end + 1, step))
    return frozenset(values), restricted


def _atom(text: str, lo: int, hi: int, names: dict, label: str) -> int:
    if text in names:
        return names[text]
    if not text.isdigit():
        raise ValueError(f"{label}: {text!r} is not a number or name")
    value = int(text)
    if label == "weekday" and value == 7:
        value = 0
    if value < lo or value > hi:
        raise ValueError(f"{label}: {value} outside {lo}-{hi}")
    return value


def parse(expr: str) -> CronSpec:
    """Parse a 5-field cron expression (minute hour day month weekday).

    Supports ``*``, lists, ranges, ``/step``, month and weekday names, ``7``
    for Sunday and the ``@daily``-style aliases. Raises ``ValueError``.
    """
    text = (expr or "").strip().lower()
    text = _ALIASES.get(text, text)
    fields = text.split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(fields)}: {expr!r}")
    minutes, _ = _parse_field(fields[0], 0, 59, {}, "minute")
    hours, _ = _parse_field(fields[1], 0, 23, {}, "hour")
    days, day_r = _parse_field(fields[2], 1, 31, {}, "day")
    months, _ = _parse_field(fields[3], 1, 12, _MONTHS, "month")
    weekdays, dow_r = _parse_field(fields[4], 0, 7, _DOWS, "weekday")
    return CronSpec(minutes, hours, days, months, weekdays, day_r, dow_r)


def resolve_tz(name: str) -> tzinfo:
    """``tzinfo`` for a schedule's ``tz``: an IANA name, or ``local``/empty.

    ``local`` honours ``C3_SCHEDULER_TZ`` when set, else the machine's
    current UTC offset. Callers recompute ``next_run_at`` at every fire, so a
    DST change is picked up one fire late at worst. Raises ``ValueError`` for
    an unknown IANA name.
    """
    key = (name or "").strip()
    if not key or key.lower() == "local":
        key = os.environ.get("C3_SCHEDULER_TZ", "").strip()
        if not key:
            return datetime.now().astimezone().tzinfo or timezone.utc
    if key.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def next_after(spec: CronSpec, after: datetime, tz: tzinfo | None = None) -> datetime | None:
    """First instant matching ``spec`` strictly after ``after``, as aware UTC.

    ``after`` must be aware. Wall-clock matching happens in ``tz`` (UTC when
    omitted). A wall time that does not exist on a DST-gap day fires at the
    shifted instant; a repeated wall time fires once. ``None`` when no match
    falls within ``SEARCH_LIMIT_DAYS``.
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    zone = tz or timezone.utc
    local = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    after_utc = after.astimezone(timezone.utc)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)
    day = local.replace(hour=0, minute=0)
    first_day = True
    for _ in range(SEARCH_LIMIT_DAYS):
        if spec.matches_day(day):
            for hour in hours:
                for minute in minutes:
                    if first_day and (hour, minute) < (local.hour, local.minute):
                        continue
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                    candidate_utc = candidate.astimezone(timezone.utc)
                    if candidate_utc > after_utc:
                        return candidate_utc
        first_day = False
        day = (day + timedelta(days=1, hours=2)).replace(hour=0, minute=0)
    return None


def preset_to_cron(preset: str, at: str = "09:00", days=()) -> str:
    """Cron expression for a preset at ``HH:MM``; ``days`` names the weekly days."""
    name = (preset or "").strip().lower()
    if name not in PRESETS:
        raise ValueError(f"unknown preset {preset!r} (use one of {', '.join(PRESETS)})")
    m = _HHMM.match((at or "09:00").strip())
    if not m:
        raise ValueError(f"preset time must be HH:MM, got {at!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if name == "hourly":
        return f"{minute} * * * *"
    if name == "daily":
        return f"{minute} {hour} * * *"
    if name == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if name == "monthly":
        return f"{minute} {hour} 1 * *"
    dows = []
    for d in days or ("mon",):
        text = str(d).strip().lower()
        if text in _DOWS:
            dows.append(_DOWS[text])
        elif text.isdigit() and 0 <= int(text) <= 7:
            dows.append(int(text) % 7)
        else:
            raise ValueError(f"weekly preset: unknown day {d!r}")
    return f"{minute} {hour} * * {','.join(str(d) for d in sorted(set(dows)))}"


def validate_when(when: dict) -> dict:
    """Normalised copy of ``when`` or ``ValueError`` naming the first defect."""
    if not isinstance(when, dict):
        raise ValueError("when must be an object")
    kind = str(when.get("type") or "").strip().lower()
    if kind not in WHEN_TYPES:
        raise ValueError(f"when.type must be one of {', '.join(WHEN_TYPES)}")
    out: dict = {"type": kind}
    if kind == "cron":
        expr = str(when.get("expr") or "").strip()
        parse(expr)
        out["expr"] = expr
        out["tz"] = _tz_field(when)
    elif kind == "every":
        try:
            seconds = int(when.get("seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("when.seconds must be an integer") from exc
        if seconds < MIN_EVERY_SECONDS:
            raise ValueError(f"when.seconds must be at least {MIN_EVERY_SECONDS}")
        out["seconds"] = seconds
    elif kind == "at":
        out["when"] = parse_iso(str(when.get("when") or "")).isoformat()
    else:
        preset = str(when.get("preset") or "").strip().lower()
        at = str(when.get("at") or "09:00").strip()
        days = list(when.get("days") or [])
        out.update({"preset": preset, "at": at, "days": days, "tz": _tz_field(when),
                    "expr": preset_to_cron(preset, at, days)})
    return out


def _tz_field(when: dict) -> str:
    tz = str(when.get("tz") or "local").strip()
    resolve_tz(tz)
    return tz


def parse_iso(text: str) -> datetime:
    """Aware UTC datetime from ISO-8601 text; a naive value is taken as UTC."""
    value = (text or "").strip()
    if not value:
        raise ValueError("empty datetime")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 datetime: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_fire(when: dict, after: datetime) -> datetime | None:
    """Next aware-UTC fire of a validated ``when`` strictly after ``after``."""
    kind = when.get("type")
    if kind in ("cron", "preset"):
        return next_after(parse(when["expr"]), after, resolve_tz(when.get("tz", "local")))
    if kind == "every":
        return after.astimezone(timezone.utc) + timedelta(seconds=int(when["seconds"]))
    if kind == "at":
        at = parse_iso(when["when"])
        return at if at > after.astimezone(timezone.utc) else None
    raise ValueError(f"unknown when.type {kind!r}")


def preview(when: dict, after: datetime, count: int = 5) -> list[datetime]:
    """The next ``count`` fires of ``when`` after ``after``."""
    out: list[datetime] = []
    cursor = after
    for _ in range(max(0, count)):
        nxt = next_fire(when, cursor)
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out
