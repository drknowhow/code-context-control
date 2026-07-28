"""Build a synthetic demo world for README screenshots.

Screenshots must never be captured against a real project: the Hub enumerates
every project name and absolute path on the machine, Chat renders verbatim AI
conversations, Memory renders stored facts, and Instructions renders the full
CLAUDE.md. This module generates a fictional "Acme" world instead.

Creates one richly-seeded primary project (acme-invoicing) plus four lighter
siblings so the Hub has a plausible multi-project registry.

Usage:
    python -m scripts.screenshots.demo_world build
    python -m scripts.screenshots.demo_world clean
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path


def _force_rmtree(path: Path) -> None:
    """rmtree that survives Windows.

    Git marks objects under .git/objects read-only; on Windows that makes
    os.unlink raise PermissionError instead of silently succeeding as it
    would on POSIX. Clear the read-only bit and retry.
    """
    def on_error(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=on_error)

def _default_root() -> Path:
    """Pick a demo root whose absolute path is safe to publish.

    The per-project UI renders the project's full path in the header, so a
    root under the home directory would put the developer's username into
    every screenshot. Prefer a short neutral path.
    """
    env = os.environ.get("C3_DEMO_ROOT")
    if env:
        return Path(env)
    if os.name == "nt":
        candidate = Path("C:/c3-demo")
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            pass  # fall through to home
    else:
        return Path("/tmp/c3-demo")
    return Path.home() / ".c3-screenshot-demo"


ROOT = _default_root()

PRIMARY = "acme-invoicing"
SIBLINGS = ["acme-web", "acme-mobile", "acme-infra", "acme-docs"]


# --------------------------------------------------------------------------
# File content
# --------------------------------------------------------------------------

_BODIES = [
    ["if not {arg}:", "    raise ValueError('{fn}: missing input')",
     "result = _resolve({arg})", "return result"],
    ["ctx = _context()", "with ctx.begin():",
     "    rows = ctx.fetch({arg})", "    return [_row_to_dict(r) for r in rows]"],
    ["total = Decimal('0')", "for item in _iter({arg}):",
     "    total += Decimal(str(item.get('amount', 0)))",
     "return total.quantize(Decimal('0.01'))"],
    ["cached = _CACHE.get({arg})", "if cached is not None:", "    return cached",
     "value = _compute({arg})", "_CACHE[{arg}] = value", "return value"],
    ["try:", "    return _client().call({arg})", "except TimeoutError:",
     "    _log.warning('retrying %s', {arg})", "    return _client().call({arg})"],
]


def _py_module(name: str, summary: str, funcs: list[tuple[str, str, str]]) -> str:
    """Render a plausible Python module: docstring, imports, real-ish bodies.

    Bodies matter: a file full of `raise NotImplementedError` compresses to
    almost nothing, which makes the dashboard's token-savings figures look
    artificially small in the screenshots.
    """
    out = [f'"""{summary}"""', "", "from __future__ import annotations", "",
           "import logging", "from decimal import Decimal",
           "from typing import Any, Iterable", "", "_log = logging.getLogger(__name__)",
           "_CACHE: dict[str, Any] = {}", "", ""]
    for idx, (fname, sig, body) in enumerate(funcs):
        arg = sig.split(":")[0].split(",")[0].strip() or "None"
        out.append(f"def {fname}({sig}):")
        out.append(f'    """{body}"""')
        for line in _BODIES[idx % len(_BODIES)]:
            out.append("    " + line.format(arg=arg, fn=fname))
        out.append("")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# Extra domains so the demo repo is a believable size rather than a toy.
_EXTRA_DOMAINS = {
    "subscriptions": ["create", "cancel", "pause", "resume", "change_plan"],
    "coupons": ["validate", "redeem", "expire"],
    "refunds": ["issue", "partial", "reconcile"],
    "usage": ["record_event", "aggregate_period", "overage_charge"],
    "reports": ["monthly_revenue", "churn_cohort", "aging_receivables"],
    "notifications": ["send_invoice", "send_receipt", "send_dunning"],
    "exports": ["to_csv", "to_parquet", "upload"],
    "plans": ["load_catalog", "resolve_price", "is_grandfathered"],
}


def _generate_extra_modules() -> dict[str, str]:
    """Round the demo project out to a realistic file/line count."""
    files: dict[str, str] = {}
    for domain, fns in _EXTRA_DOMAINS.items():
        files[f"src/acme/{domain}/__init__.py"] = f'"""{domain.capitalize()} domain."""\n'
        for fn in fns:
            funcs = [
                (fn, f"{domain[:-1] if domain.endswith('s') else domain}_id: str",
                 f"{fn.replace('_', ' ').capitalize()} for a {domain[:-1]}."),
                (f"{fn}_batch", "ids: Iterable[str]",
                 f"Batch variant of {fn}()."),
                (f"_{fn}_guard", "payload: dict",
                 f"Validate preconditions for {fn}()."),
            ]
            files[f"src/acme/{domain}/{fn}.py"] = _py_module(fn, f"{domain}.{fn}", funcs)
        files[f"tests/test_{domain}.py"] = _py_module(
            f"test_{domain}", f"Tests for the {domain} domain.",
            [(f"test_{fn}_happy_path", "", f"{fn}() succeeds on valid input.")
             for fn in fns])
    return files


PRIMARY_FILES: dict[str, str] = {
    "pyproject.toml": textwrap.dedent("""\
        [build-system]
        requires = ["setuptools>=68"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "acme-invoicing"
        version = "1.4.2"
        description = "Billing and invoicing service for Acme SaaS"
        requires-python = ">=3.11"
        dependencies = [
            "fastapi>=0.110",
            "sqlalchemy>=2.0",
            "pydantic>=2.6",
            "stripe>=8.0",
        ]
        """),

    "README.md": textwrap.dedent("""\
        # acme-invoicing

        Billing and invoicing service for Acme SaaS. Handles subscription
        proration, tax calculation, dunning, and Stripe reconciliation.

        ## Quick start

        ```bash
        uv sync
        uvicorn acme.api.routes:app --reload
        ```

        ## Architecture

        - `src/acme/api/` — HTTP surface (FastAPI), auth, webhook receivers
        - `src/acme/billing/` — invoice assembly, tax, proration, dunning
        - `src/acme/db/` — SQLAlchemy models and migrations
        - `src/acme/integrations/` — Stripe client, ledger export
        """),

    "CLAUDE.md": textwrap.dedent("""\
        # acme-invoicing — agent instructions

        ## Conventions
        - Money is always `Decimal`, never `float`. Cents as integers at the
          storage boundary.
        - All monetary functions take an explicit `currency` argument.
        - Migrations are forward-only. Never edit a shipped migration.

        ## Before editing billing/
        Run `pytest tests/test_invoice.py tests/test_tax.py` — these cover the
        proration edge cases that regressed twice in Q1.
        """),

    ".env.example": textwrap.dedent("""\
        STRIPE_API_KEY=sk_test_replace_me
        DATABASE_URL=postgresql://localhost/acme_dev
        LEDGER_EXPORT_TOKEN=replace_me
        """),

    "src/acme/__init__.py": '"""Acme invoicing service."""\n\n__version__ = "1.4.2"\n',

    "src/acme/api/__init__.py": '"""HTTP surface."""\n',
    "src/acme/api/routes.py": _py_module(
        "routes", "FastAPI route definitions for the invoicing service.",
        [("list_invoices", "customer_id: str, limit: int = 50",
          "Return a customer's invoices, newest first."),
         ("get_invoice", "invoice_id: str", "Fetch a single invoice by id."),
         ("create_invoice", "customer_id: str, lines: Iterable[dict]",
          "Assemble and persist a new draft invoice."),
         ("void_invoice", "invoice_id: str, reason: str",
          "Void an issued invoice and emit a credit note.")]),
    "src/acme/api/auth.py": _py_module(
        "auth", "API-key and JWT authentication for the invoicing API.",
        [("verify_api_key", "key: str", "Validate an API key against the vault."),
         ("issue_token", "customer_id: str, ttl: int = 3600",
          "Mint a short-lived JWT for the customer portal."),
         ("require_scope", "scope: str", "Dependency enforcing a token scope.")]),
    "src/acme/api/webhooks.py": _py_module(
        "webhooks", "Inbound webhook receivers (Stripe, ledger).",
        [("handle_stripe_event", "payload: dict, signature: str",
          "Verify and dispatch a Stripe webhook event."),
         ("handle_payment_failed", "event: dict",
          "Kick off the dunning sequence for a failed payment.")]),

    "src/acme/billing/__init__.py": '"""Billing domain logic."""\n',
    "src/acme/billing/invoice.py": _py_module(
        "invoice", "Invoice assembly: line items, totals, credit notes.",
        [("build_invoice", "customer_id: str, lines: Iterable[dict], currency: str",
          "Assemble a draft invoice from raw line items."),
         ("apply_credits", "invoice: dict, credits: Iterable[Decimal]",
          "Apply outstanding credit balance against an invoice total."),
         ("finalize", "invoice: dict", "Freeze an invoice and assign a number."),
         ("total_cents", "invoice: dict", "Return the invoice total in integer cents.")]),
    "src/acme/billing/tax.py": _py_module(
        "tax", "Tax rate resolution and line-level tax application.",
        [("resolve_rate", "country: str, region: str, product_class: str",
          "Look up the applicable tax rate for a jurisdiction."),
         ("apply_tax", "amount: Decimal, rate: Decimal",
          "Apply a tax rate, rounding half-up at the cent."),
         ("is_reverse_charge", "buyer_country: str, seller_country: str",
          "Return True when EU reverse-charge applies.")]),
    "src/acme/billing/dunning.py": _py_module(
        "dunning", "Failed-payment retry and escalation schedule.",
        [("next_attempt", "attempt: int", "Return the delay before the next retry."),
         ("escalate", "customer_id: str, attempt: int",
          "Advance the dunning stage and notify the customer.")]),
    "src/acme/billing/proration.py": _py_module(
        "proration", "Mid-cycle plan change proration.",
        [("prorate_upgrade", "old_plan: dict, new_plan: dict, days_left: int",
          "Compute the prorated charge for a mid-cycle upgrade."),
         ("prorate_downgrade", "old_plan: dict, new_plan: dict, days_left: int",
          "Compute the prorated credit for a mid-cycle downgrade.")]),

    "src/acme/db/__init__.py": '"""Persistence layer."""\n',
    "src/acme/db/models.py": _py_module(
        "models", "SQLAlchemy ORM models.",
        [("customer_table", "", "Customer table definition."),
         ("invoice_table", "", "Invoice table definition."),
         ("line_item_table", "", "Invoice line-item table definition."),
         ("payment_table", "", "Payment attempt table definition.")]),
    "src/acme/db/session.py": _py_module(
        "session", "Engine and session factory.",
        [("get_engine", "url: str", "Build a pooled SQLAlchemy engine."),
         ("session_scope", "", "Transactional session context manager.")]),
    "src/acme/db/migrations.py": _py_module(
        "migrations", "Forward-only schema migrations.",
        [("upgrade", "revision: str", "Apply migrations up to a revision."),
         ("current_revision", "", "Return the applied schema revision.")]),

    "src/acme/integrations/stripe_client.py": _py_module(
        "stripe_client", "Thin Stripe API wrapper with retry and idempotency.",
        [("charge", "customer_id: str, amount_cents: int, currency: str",
          "Create a charge with an idempotency key."),
         ("refund", "charge_id: str, amount_cents: int", "Refund part or all of a charge."),
         ("sync_customer", "customer_id: str", "Reconcile local state with Stripe.")]),
    "src/acme/integrations/ledger.py": _py_module(
        "ledger", "Double-entry ledger export for the finance team.",
        [("export_period", "start: str, end: str", "Export ledger entries for a period."),
         ("reconcile", "period: str", "Reconcile ledger totals against Stripe payouts.")]),

    "src/acme/core/config.py": _py_module(
        "config", "Environment-backed settings.",
        [("load_settings", "", "Load and validate settings from the environment."),
         ("database_url", "", "Return the configured database URL.")]),
    "src/acme/core/errors.py": _py_module(
        "errors", "Domain error types.",
        [("as_http_error", "exc: Exception", "Map a domain error to an HTTP response.")]),
    "src/acme/core/logging.py": _py_module(
        "logging", "Structured logging setup.",
        [("configure", "level: str = 'INFO'", "Configure structured JSON logging.")]),

    "web/src/App.tsx": textwrap.dedent("""\
        import React from "react";
        import { InvoiceTable } from "./components/InvoiceTable";
        import { PaymentForm } from "./components/PaymentForm";

        export function App(): JSX.Element {
          const [customerId, setCustomerId] = React.useState<string>("");
          return (
            <main className="portal">
              <h1>Billing portal</h1>
              <InvoiceTable customerId={customerId} />
              <PaymentForm onComplete={() => setCustomerId(customerId)} />
            </main>
          );
        }
        """),
    "web/src/components/InvoiceTable.tsx": textwrap.dedent("""\
        import React from "react";
        import { fetchInvoices } from "../api/client";

        export interface InvoiceTableProps {
          customerId: string;
        }

        export function InvoiceTable({ customerId }: InvoiceTableProps) {
          const [rows, setRows] = React.useState<unknown[]>([]);
          React.useEffect(() => {
            fetchInvoices(customerId).then(setRows);
          }, [customerId]);
          return <table className="invoices">{/* rows */}</table>;
        }
        """),
    "web/src/components/PaymentForm.tsx": textwrap.dedent("""\
        import React from "react";

        export function PaymentForm({ onComplete }: { onComplete: () => void }) {
          const [busy, setBusy] = React.useState(false);
          return <form onSubmit={() => setBusy(true)}>{/* fields */}</form>;
        }
        """),
    "web/src/api/client.ts": textwrap.dedent("""\
        const BASE = process.env.API_BASE ?? "/api";

        export async function fetchInvoices(customerId: string) {
          const res = await fetch(`${BASE}/invoices?customer=${customerId}`);
          if (!res.ok) throw new Error(`invoices: ${res.status}`);
          return res.json();
        }

        export async function submitPayment(invoiceId: string, token: string) {
          const res = await fetch(`${BASE}/payments`, {
            method: "POST",
            body: JSON.stringify({ invoiceId, token }),
          });
          return res.json();
        }
        """),

    "tests/test_invoice.py": _py_module(
        "test_invoice", "Invoice assembly tests.",
        [("test_build_invoice_sums_lines", "", "Totals equal the sum of line items."),
         ("test_credits_never_exceed_total", "", "Applied credits are clamped at the total."),
         ("test_finalize_assigns_sequential_number", "", "Numbers are gapless per tenant.")]),
    "tests/test_tax.py": _py_module(
        "test_tax", "Tax resolution tests.",
        [("test_reverse_charge_eu_b2b", "", "EU B2B cross-border is reverse-charged."),
         ("test_rounding_half_up_at_cent", "", "Tax rounds half-up at the cent.")]),
    "tests/test_auth.py": _py_module(
        "test_auth", "Authentication tests.",
        [("test_expired_token_rejected", "", "Expired JWTs are rejected."),
         ("test_scope_enforced", "", "Missing scopes produce a 403.")]),

    # Realistic targets for the Access Guard / Mask Guard demo.
    "data/customers_sample.csv": (
        "customer_id,company,contact_email,country,mrr_cents,plan\n"
        "cus_10241,Northwind Trading,ana.reyes@northwind.example,US,249900,scale\n"
        "cus_10242,Fabrikam GmbH,j.keller@fabrikam.example,DE,89900,growth\n"
        "cus_10243,Tailspin Toys,m.osei@tailspin.example,GB,45000,growth\n"
        "cus_10244,Contoso Health,r.silva@contoso.example,BR,320000,scale\n"
        "cus_10245,Litware Inc,k.tanaka@litware.example,JP,120000,growth\n"
    ),
    "data/transactions.csv": (
        "txn_id,customer_id,amount_cents,currency,status,captured_at\n"
        "txn_88301,cus_10241,249900,USD,captured,2026-07-01T09:14:22Z\n"
        "txn_88302,cus_10242,89900,EUR,captured,2026-07-01T09:31:05Z\n"
        "txn_88303,cus_10243,45000,GBP,failed,2026-07-01T10:02:47Z\n"
        "txn_88304,cus_10244,320000,BRL,captured,2026-07-02T08:15:33Z\n"
    ),
    "secrets/production.env": (
        "# Never read by agents - denied via Access Guard\n"
        "STRIPE_LIVE_KEY=sk_live_REDACTED\n"
        "LEDGER_SIGNING_KEY=REDACTED\n"
    ),
}


SIBLING_FILES: dict[str, dict[str, str]] = {
    "acme-web": {
        "package.json": json.dumps(
            {"name": "acme-web", "version": "3.1.0", "private": True,
             "dependencies": {"react": "^18.3.0", "vite": "^5.2.0"}}, indent=2) + "\n",
        "src/main.tsx": 'import { createRoot } from "react-dom/client";\n'
                        'createRoot(document.getElementById("root")!).render(<App />);\n',
        "src/routes/dashboard.tsx": "export function Dashboard() { return null; }\n",
        "src/routes/settings.tsx": "export function Settings() { return null; }\n",
        "src/lib/api.ts": "export const api = { get: async (p: string) => fetch(p) };\n",
    },
    "acme-mobile": {
        "package.json": json.dumps(
            {"name": "acme-mobile", "version": "2.0.4", "private": True,
             "dependencies": {"react-native": "0.74.1"}}, indent=2) + "\n",
        "src/App.tsx": "export default function App() { return null; }\n",
        "src/screens/Invoices.tsx": "export function Invoices() { return null; }\n",
    },
    "acme-infra": {
        "main.tf": 'terraform {\n  required_version = ">= 1.7"\n}\n\n'
                   'module "billing" {\n  source = "./modules/billing"\n}\n',
        "modules/billing/main.tf": 'resource "aws_rds_cluster" "billing" {}\n',
        "scripts/deploy.py": _py_module(
            "deploy", "Deployment orchestration.",
            [("plan", "env: str", "Run terraform plan for an environment."),
             ("apply", "env: str", "Apply a reviewed plan.")]),
    },
    "acme-docs": {
        "README.md": "# Acme engineering docs\n\nRunbooks, ADRs, onboarding.\n",
        "adr/0001-decimal-money.md": "# ADR 0001 — Money is Decimal\n\nStatus: accepted\n",
        "adr/0002-forward-only-migrations.md": "# ADR 0002 — Forward-only migrations\n\nStatus: accepted\n",
        "runbooks/dunning.md": "# Runbook — dunning escalation\n",
    },
}


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def _write_tree(base: Path, files: dict[str, str]) -> int:
    for rel, content in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return len(files)


def _git_init(path: Path) -> None:
    """Give the project a git history so the Edit Ledger has commits to enrich."""
    env_flags = ["-c", "user.email=demo@acme.example", "-c", "user.name=Acme Demo"]
    subprocess.run(["git", "init", "-q"], cwd=path, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", *env_flags, "add", "-A"], cwd=path, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", *env_flags, "commit", "-q", "-m",
                    "chore: initial import"], cwd=path, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _c3_init(path: Path) -> None:
    """Run `c3 init` without embeddings — fast, and screenshots don't need them.

    NOTE: `c3 init` auto-registers the project in ~/.c3/projects.json. The
    caller is responsible for backing that file up first and restoring it
    afterwards (see run.py).
    """
    subprocess.run([sys.executable, "-m", "cli.c3", "init", str(path),
                    "--no-embed", "--force", "--ide", "claude"],
                   cwd=str(Path(__file__).resolve().parents[2]), check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build() -> Path:
    if ROOT.exists():
        _force_rmtree(ROOT)
    ROOT.mkdir(parents=True)

    primary = ROOT / PRIMARY
    n = _write_tree(primary, {**PRIMARY_FILES, **_generate_extra_modules()})
    _git_init(primary)
    _c3_init(primary)
    print(f"  {PRIMARY:<16} {n:>3} files")

    for name in SIBLINGS:
        path = ROOT / name
        n = _write_tree(path, SIBLING_FILES[name])
        _git_init(path)
        _c3_init(path)
        print(f"  {name:<16} {n:>3} files")

    print(f"\ndemo world at {ROOT}")
    return ROOT


def clean() -> None:
    if ROOT.exists():
        _force_rmtree(ROOT)
        print(f"removed {ROOT}")
    else:
        print("nothing to clean")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()
    elif cmd == "clean":
        clean()
    else:
        print(__doc__)
        sys.exit(1)
