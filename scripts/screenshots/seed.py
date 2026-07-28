"""Seed the demo project with fictional data via the per-project REST API.

Posting through the same endpoints the UI uses means the seeded data goes
through the same validation as real data, so the screenshots show genuine
rendering rather than hand-forged JSON.

Every value here is invented. Nothing references a real customer, secret,
repository, or path outside the demo world.

Usage:
    python -m scripts.screenshots.seed http://127.0.0.1:3333
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 30


def _post(base: str, path: str, payload: dict) -> tuple[bool, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 # Origin must match, the server enforces an Origin/Referer
                 # check on every request (v2.33.0 CSRF hardening).
                 "Origin": base})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return True, res.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


# --------------------------------------------------------------------------
# Seed data — all fictional
# --------------------------------------------------------------------------

FACTS = [
    ("Money is always Decimal, never float. Cents as integers at the storage boundary.", "convention"),
    ("Migrations are forward-only; a shipped migration is never edited.", "convention"),
    ("Proration rounds half-up at the cent, matching the tax module.", "decision"),
    ("EU B2B cross-border invoices are reverse-charged, no VAT line.", "decision"),
    ("Stripe charges use an idempotency key derived from invoice_id + attempt.", "architecture"),
    ("The dunning schedule is 1d / 3d / 7d / 14d, then manual review.", "architecture"),
    ("Invoice numbers must be gapless per tenant — finance audit requirement.", "constraint"),
    ("web/ is a separate Vite build; it does not share the Python venv.", "architecture"),
    ("test_invoice.py and test_tax.py cover the Q1 proration regressions.", "testing"),
    ("Ledger export runs nightly at 02:00 UTC and reconciles against payouts.", "architecture"),
]

TASKS = [
    ("Fix proration rounding on mid-cycle downgrade",
     "Downgrade credit is off by one cent when days_left is odd. Rounding "
     "happens before the currency conversion instead of after.",
     "in_progress", "p0", ["billing", "bug"]),
    ("Add reverse-charge support for Norway",
     "Norway is not in the EU but has a reverse-charge arrangement for B2B. "
     "resolve_rate() needs a jurisdiction override table.",
     "in_progress", "p1", ["tax", "compliance"]),
    ("Gapless invoice numbering under concurrent finalize",
     "Two workers finalizing simultaneously can allocate the same number. "
     "Needs an advisory lock or a sequence per tenant.",
     "blocked", "p0", ["billing", "correctness"]),
    ("Retry Stripe webhooks with exponential backoff",
     "Currently a failed webhook handler drops the event. Persist and retry.",
     "backlog", "p1", ["integrations"]),
    ("Export ledger in the finance team's CSV dialect",
     "They need semicolon separators and comma decimals for the EU entity.",
     "backlog", "p2", ["ledger"]),
    ("Cache tax rate lookups per request",
     "resolve_rate() hits the database once per line item.",
     "backlog", "p2", ["tax", "performance"]),
    ("Document the dunning escalation runbook",
     "Support has asked three times what happens after attempt 4.",
     "backlog", "p3", ["docs"]),
    ("Migrate payment_table to partitioned storage",
     "Table is 240M rows; queries over 90d are slow.",
     "backlog", "p2", ["db", "performance"]),
    ("Reconcile July payouts against ledger",
     "Month-end close. Difference of 1,240 cents unexplained.",
     "done", "p1", ["ledger", "finance"]),
    ("Upgrade to SQLAlchemy 2.0 typed ORM",
     "Completed — models.py now uses Mapped[] annotations throughout.",
     "done", "p2", ["db"]),
]

# type ∈ (token, env, multiline) — services/credential_store.py
CREDENTIALS = [
    ("STRIPE_TEST_KEY", "sk_test_51QhFakeKeyForScreenshotsOnly000000",
     "token", "Stripe test-mode key for local billing runs", "STRIPE_API_KEY", False, True),
    ("LEDGER_EXPORT_TOKEN", "lex_demo_0000000000000000",
     "token", "Signs nightly ledger exports to the finance bucket", "LEDGER_EXPORT_TOKEN", False, True),
    ("SANDBOX_DB_URL", "postgresql://demo:demo@localhost:5432/acme_sandbox",
     "env", "Throwaway sandbox database — safe for agents to read", "DATABASE_URL", True, True),
    ("SENTRY_DSN", "https://demo@o0.ingest.example/0",
     "env", "Error reporting endpoint for the staging deploy", "SENTRY_DSN", False, False),
]

# kind ∈ {deny, read_only} (services/access_guard.py:25-27).
# `**/.env*` is already a non-overridable builtin, so it is not repeated here.
ACCESS_RULES = [
    ("secrets/**", "deny"),
    ("src/acme/db/migrations.py", "read_only"),
    ("src/acme/core/config.py", "read_only"),
]

# preset ∈ MASK_PRESETS (services/access_guard.py:34-39); params must match
# each preset's declared schema exactly.
MASK_RULES = [
    ("data/customers_sample.csv", "sample_rows", {"count": 3, "strategy": "first"}),
    ("data/transactions.csv", "redact_columns", {"columns": ["amount_cents", "customer_id"]}),
]

EDITS = [
    ("src/acme/billing/proration.py", "modified",
     "Round after currency conversion, not before", [41, 42, 43], ["bug", "billing"]),
    ("src/acme/billing/tax.py", "modified",
     "Add jurisdiction override table for non-EU reverse charge", [88, 89, 90, 91], ["tax"]),
    ("tests/test_tax.py", "modified",
     "Cover Norway reverse-charge case", [22, 23, 24], ["testing"]),
    ("src/acme/db/models.py", "modified",
     "Migrate to SQLAlchemy 2.0 Mapped[] annotations", [12, 30, 55, 71], ["db"]),
    ("src/acme/integrations/stripe_client.py", "modified",
     "Derive idempotency key from invoice_id + attempt", [18, 19], ["integrations"]),
    ("src/acme/api/webhooks.py", "created",
     "Add webhook receiver module", [], ["integrations"]),
    ("src/acme/billing/dunning.py", "modified",
     "Extend schedule to 14d before manual review", [15], ["billing"]),
]


def main(base: str) -> int:
    base = base.rstrip("/")
    ok = fail = 0

    def track(label: str, success: bool, detail: str) -> None:
        nonlocal ok, fail
        if success:
            ok += 1
        else:
            fail += 1
            print(f"    ! {label}: {detail}")

    print("  memory facts")
    for fact, category in FACTS:
        track(fact[:40], *_post(base, "/api/memory/remember",
                                {"fact": fact, "category": category}))

    print("  tasks")
    for title, desc, status, priority, tags in TASKS:
        track(title[:40], *_post(base, "/api/pm/task",
                                 {"title": title, "description": desc,
                                  "status": status, "priority": priority,
                                  "tags": tags}))

    print("  credentials")
    for name, value, ctype, desc, env_var, readable, inject in CREDENTIALS:
        track(name, *_post(base, "/api/credentials",
                           {"name": name, "value": value, "scope": "project",
                            "type": ctype, "description": desc,
                            "env_var": env_var, "agent_readable": readable,
                            "inject": inject}))

    print("  access rules")
    for glob, kind in ACCESS_RULES:
        track(glob, *_post(base, "/api/access",
                           {"glob": glob, "kind": kind, "scope": "project"}))

    print("  mask rules")
    for glob, preset, params in MASK_RULES:
        track(glob, *_post(base, "/api/access/mask",
                           {"glob": glob, "preset": preset, "params": params,
                            "scope": "project"}))

    print("  edit ledger")
    for path, change, summary, lines, tags in EDITS:
        track(path, *_post(base, "/api/edits",
                           {"file": path, "change_type": change,
                            "summary": summary, "lines_changed": lines,
                            "tags": tags}))

    # Mask rules land on disk immediately but are not enforced until the
    # activation transaction purges derived artifacts. Without this the UI
    # shows an amber "configured but not activated" banner.
    print("  activating masks")
    track("mask activation", *_post(base, "/api/access/mask/activate", {}))

    print(f"\n  seeded ok={ok} failed={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3333"))
