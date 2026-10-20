"""JSON replies for c3_jira / c3_bitbucket ``format="json"``.

Programs (dashboards, the Work Agents Work page) read these tools too, and
the text replies are capped at a few thousand tokens for agents. JSON mode
returns the normalized data instead, under a much larger character cap that
trims list and string fields so the reply always stays valid JSON:

    {"ok": true, "action": "get_issue", "data": {...}, "truncated": ["comments"]}
    {"ok": false, "action": "get_issue", "error": "...", "status": 404}
"""
from __future__ import annotations

import json
from typing import Any

JSON_CHAR_CAP = 400_000

# Lists ordered oldest first: trimming keeps their newest entries.
_KEEP_TAIL = {"comments", "activities"}

# A string shorter than this is not worth cutting; the next field is.
_MIN_CUT = 1000


def wants_json(kwargs: dict) -> bool:
    return str(kwargs.get("format") or "").strip().lower() == "json"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _fit(data: dict, cap: int, overhead: int) -> list[str]:
    """Shrink the largest top-level fields of *data* in place until the
    serialized dict fits in ``cap - overhead`` characters. Returns the names
    of the fields that were cut."""
    truncated: list[str] = []
    budget = max(0, cap - overhead)
    for _ in range(200):
        if len(_dumps(data)) <= budget:
            break
        sizes = {k: len(_dumps(v)) for k, v in data.items()}
        key = max(sizes, key=sizes.get)
        value = data[key]
        if isinstance(value, list) and len(value) > 1:
            keep = max(1, len(value) * 3 // 4)
            data[key] = value[-keep:] if key in _KEEP_TAIL else value[:keep]
        elif isinstance(value, str) and len(value) > _MIN_CUT:
            data[key] = value[: len(value) * 3 // 4]
        elif isinstance(value, (list, dict)) and value:
            data[key] = [] if isinstance(value, list) else {}
        else:
            data[key] = None
        if key not in truncated:
            truncated.append(key)
    return truncated


def ok(action: str, data: Any, *, cap: int = JSON_CHAR_CAP) -> str:
    """Serialize a successful read. A non-dict *data* is wrapped as
    ``{"values": data}`` so every reply trims the same way."""
    if not isinstance(data, dict):
        data = {"values": data}
    data = dict(data)
    head = {"ok": True, "action": action}
    truncated = _fit(data, cap, len(_dumps(head)) + 64)
    out = {**head, "data": data}
    if truncated:
        out["truncated"] = truncated
    return _dumps(out)


def error(action: str, message: str, *, status: int = 0, code: str = "") -> str:
    out: dict[str, Any] = {"ok": False, "action": action, "error": message}
    if status:
        out["status"] = status
    if code:
        out["code"] = code
    return _dumps(out)


def text(action: str, resp: str) -> str:
    """An action with no structured form (writes, diffs) in JSON mode: the
    text reply rides in ``data.text`` so the caller still gets JSON."""
    return ok(action, {"text": resp})


def is_json_reply(response: str) -> bool:
    """True for a reply built here; callers must not append text to it."""
    return response.startswith('{"ok": ')
