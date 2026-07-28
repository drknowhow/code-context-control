"""Seed session history for the demo project.

Sessions cannot be seeded over REST: /api/sessions/start exists, but tool
calls, decisions and file changes are logged by hooks through SessionManager,
with no HTTP surface. So this drives SessionManager directly, and must run
BEFORE the UI server boots — the server reads session history from disk at
request time but holds `current_session` in memory.

Without this the Dashboard hero shot shows SESSIONS 0 and an empty
"Current Session" card, which is half the image.

Usage:
    python -m scripts.screenshots.seed_sessions <project_path>
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.activity_log import ActivityLog  # noqa: E402
from services.session_manager import SessionManager  # noqa: E402

# (description, [tool calls], [decisions], [file changes], summary)
SESSIONS = [
    ("Fix proration rounding on mid-cycle downgrade",
     [("c3_search", {"query": "prorate_downgrade", "action": "code"}, "3 matches in billing/"),
      ("c3_compress", {"file_path": "src/acme/billing/proration.py", "mode": "map"}, "map: 2 functions"),
      ("c3_read", {"symbols": "prorate_downgrade"}, "41 lines"),
      ("c3_impact", {"target": "prorate_downgrade"}, "2 callers, 1 test"),
      ("c3_edit", {"file_path": "src/acme/billing/proration.py"}, "1 edit applied"),
      ("c3_validate", {"file_path": "src/acme/billing/proration.py"}, "clean")],
     [("Round after currency conversion, not before",
       "Rounding pre-conversion loses a cent on odd day counts."),
      ("Keep the Decimal quantize at the call site",
       "Callers already expect cent-quantized output.")],
     [("src/acme/billing/proration.py", "modified", "Round after conversion"),
      ("tests/test_invoice.py", "modified", "Add odd-day-count case")],
     "Fixed the off-by-one-cent downgrade credit; added a regression test."),

    ("Add reverse-charge support for Norway",
     [("c3_search", {"query": "reverse_charge", "action": "semantic"}, "tax.py, 2 hits"),
      ("c3_read", {"symbols": "is_reverse_charge,resolve_rate"}, "58 lines"),
      ("c3_edit", {"file_path": "src/acme/billing/tax.py"}, "jurisdiction table added"),
      ("c3_validate", {"file_path": "src/acme/billing/tax.py"}, "clean")],
     [("Use a jurisdiction override table rather than an EU membership check",
       "Norway and Switzerland both need this; a boolean does not scale.")],
     [("src/acme/billing/tax.py", "modified", "Jurisdiction override table"),
      ("tests/test_tax.py", "modified", "Cover Norway")],
     "Norway B2B now reverse-charges correctly."),

    ("Reconcile July payouts against the ledger",
     [("c3_search", {"query": "reconcile", "action": "code"}, "ledger.py"),
      ("c3_read", {"symbols": "reconcile"}, "22 lines"),
      ("c3_shell", {"cmd": "pytest tests/test_ledger.py -q"}, "12 passed")],
     [("Difference traced to a rounding mismatch on refunds",
       "Refund path quantized at 4dp instead of 2dp.")],
     [("src/acme/refunds/reconcile.py", "modified", "Quantize to 2dp")],
     "Month-end close reconciled; 1,240-cent gap explained."),
]

# /api/sessions/current (cli/server.py:994) does NOT read session_mgr state —
# it reconstructs the live view from the activity log: the most recent
# session_start event, then every tool_call / decision / file_change since.
# So the Current Session card and the Recent Activity feed are both driven by
# these events, and they must be written LAST so nothing displaces them.
LIVE_SESSION = "Cache tax rate lookups per request"
LIVE_EVENTS = [
    ("tool_call", {"tool": "c3_search", "result_summary": "resolve_rate — 4 call sites"}),
    ("tool_call", {"tool": "c3_compress", "result_summary": "tax.py — map: 3 functions"}),
    ("decision", {"decision": "Memoize per-request, not per-process"}),
    ("tool_call", {"tool": "c3_read", "result_summary": "resolve_rate — 19 lines"}),
    ("file_change", {"file": "src/acme/billing/tax.py"}),
    ("tool_call", {"tool": "c3_edit", "result_summary": "tax.py — 1 edit applied"}),
    ("tool_call", {"tool": "c3_validate", "result_summary": "tax.py — clean"}),
]


def seed_current_session(project: str) -> int:
    """Write the live-session activity trail.

    Must run AFTER every other seeding step: the feed shows the 8 most recent
    events, and REST seeding emits access_action / cred_action events that the
    Dashboard renders as raw type names.
    """
    log = ActivityLog(project)
    session_id = "sess_2026_07_28_a1f3"
    log.log("session_start", {"session_id": session_id,
                              "description": LIVE_SESSION,
                              "source_system": "claude",
                              "source_ide": "claude-code"})
    for event_type, data in LIVE_EVENTS:
        log.log(event_type, {**data, "session_id": session_id})
    print(f"    live session + {len(LIVE_EVENTS)} events")
    return 0


def main(project: str) -> int:
    mgr = SessionManager(project)
    for desc, tools, decisions, files, summary in SESSIONS:
        mgr.start_session(desc)
        for name, args, result in tools:
            mgr.log_tool_call(name, args, result)
        for decision, reasoning in decisions:
            mgr.log_decision(decision, reasoning)
        for path, kind, note in files:
            mgr.log_file_change(path, kind, note)
        mgr.save_session(summary)
        print(f"    session: {desc[:52]}")

    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    if "--live-only" in sys.argv:
        sys.exit(seed_current_session(target))
    sys.exit(main(target))
