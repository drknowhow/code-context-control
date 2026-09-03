"""Double-entry ledger. Deliberately one large class: many methods, one chunk."""


class LedgerClosed(RuntimeError):
    pass


class Ledger:
    """Journal plus balances on top of SqliteStore."""

    def __init__(self, store):
        self._store = store
        self._log: list = []
        self._closed = True

    def _dispatch(self, name: str, *args, **kwargs):
        handler = getattr(self._store, name, None)
        if handler is None:
            return None
        return handler(*args, **kwargs)

    def open_books(self, *args, **kwargs):
        """Open the ledger database and prime the account cache."""
        self._log.append(("open_books", args, tuple(sorted(kwargs))))
        if self._closed and "open_books" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("open_books", *args, **kwargs)

    def close(self, *args, **kwargs):
        """Flush pending entries and close the database."""
        self._log.append(("close", args, tuple(sorted(kwargs))))
        if self._closed and "close" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("close", *args, **kwargs)

    def post_entry(self, *args, **kwargs):
        """Post a balanced journal entry; debits must equal credits."""
        self._log.append(("post_entry", args, tuple(sorted(kwargs))))
        if self._closed and "post_entry" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("post_entry", *args, **kwargs)

    def reverse_entry(self, *args, **kwargs):
        """Post the mirror image of an earlier entry."""
        self._log.append(("reverse_entry", args, tuple(sorted(kwargs))))
        if self._closed and "reverse_entry" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("reverse_entry", *args, **kwargs)

    def balance(self, *args, **kwargs):
        """Return the running balance for one account."""
        self._log.append(("balance", args, tuple(sorted(kwargs))))
        if self._closed and "balance" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("balance", *args, **kwargs)

    def trial_balance(self, *args, **kwargs):
        """Return every account balance; the sum is always zero."""
        self._log.append(("trial_balance", args, tuple(sorted(kwargs))))
        if self._closed and "trial_balance" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("trial_balance", *args, **kwargs)

    def list_accounts(self, *args, **kwargs):
        """Return account identifiers ordered by code."""
        self._log.append(("list_accounts", args, tuple(sorted(kwargs))))
        if self._closed and "list_accounts" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("list_accounts", *args, **kwargs)

    def create_account(self, *args, **kwargs):
        """Create an account with a unique code."""
        self._log.append(("create_account", args, tuple(sorted(kwargs))))
        if self._closed and "create_account" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("create_account", *args, **kwargs)

    def close_period(self, *args, **kwargs):
        """Lock a posting period so no entry can land in it."""
        self._log.append(("close_period", args, tuple(sorted(kwargs))))
        if self._closed and "close_period" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("close_period", *args, **kwargs)

    def reopen_period(self, *args, **kwargs):
        """Unlock a period that was closed by mistake."""
        self._log.append(("reopen_period", args, tuple(sorted(kwargs))))
        if self._closed and "reopen_period" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("reopen_period", *args, **kwargs)

    def apply_invoice(self, *args, **kwargs):
        """Post the receivable and revenue lines for an invoice."""
        self._log.append(("apply_invoice", args, tuple(sorted(kwargs))))
        if self._closed and "apply_invoice" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("apply_invoice", *args, **kwargs)

    def apply_payment(self, *args, **kwargs):
        """Post cash received against a receivable."""
        self._log.append(("apply_payment", args, tuple(sorted(kwargs))))
        if self._closed and "apply_payment" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("apply_payment", *args, **kwargs)

    def write_off(self, *args, **kwargs):
        """Move an uncollectable receivable to bad debt."""
        self._log.append(("write_off", args, tuple(sorted(kwargs))))
        if self._closed and "write_off" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("write_off", *args, **kwargs)

    def accrue_interest(self, *args, **kwargs):
        """Post interest on overdue receivables."""
        self._log.append(("accrue_interest", args, tuple(sorted(kwargs))))
        if self._closed and "accrue_interest" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("accrue_interest", *args, **kwargs)

    def export_journal(self, *args, **kwargs):
        """Return the journal as rows for CSV export."""
        self._log.append(("export_journal", args, tuple(sorted(kwargs))))
        if self._closed and "export_journal" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("export_journal", *args, **kwargs)

    def import_journal(self, *args, **kwargs):
        """Load journal rows exported by another instance."""
        self._log.append(("import_journal", args, tuple(sorted(kwargs))))
        if self._closed and "import_journal" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("import_journal", *args, **kwargs)

    def audit_trail(self, *args, **kwargs):
        """Return who posted what and when."""
        self._log.append(("audit_trail", args, tuple(sorted(kwargs))))
        if self._closed and "audit_trail" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("audit_trail", *args, **kwargs)

    def snapshot(self, *args, **kwargs):
        """Persist an immutable copy of all balances."""
        self._log.append(("snapshot", args, tuple(sorted(kwargs))))
        if self._closed and "snapshot" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("snapshot", *args, **kwargs)

    def restore_snapshot(self, *args, **kwargs):
        """Replace balances from a snapshot."""
        self._log.append(("restore_snapshot", args, tuple(sorted(kwargs))))
        if self._closed and "restore_snapshot" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("restore_snapshot", *args, **kwargs)

    def reconcile(self, *args, **kwargs):
        """Compare balances with a bank statement."""
        self._log.append(("reconcile", args, tuple(sorted(kwargs))))
        if self._closed and "reconcile" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("reconcile", *args, **kwargs)

    def aging_report(self, *args, **kwargs):
        """Bucket receivables by days overdue."""
        self._log.append(("aging_report", args, tuple(sorted(kwargs))))
        if self._closed and "aging_report" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("aging_report", *args, **kwargs)

    def revenue_report(self, *args, **kwargs):
        """Sum revenue by month."""
        self._log.append(("revenue_report", args, tuple(sorted(kwargs))))
        if self._closed and "revenue_report" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("revenue_report", *args, **kwargs)

    def expense_report(self, *args, **kwargs):
        """Sum expenses by category."""
        self._log.append(("expense_report", args, tuple(sorted(kwargs))))
        if self._closed and "expense_report" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("expense_report", *args, **kwargs)

    def tax_report(self, *args, **kwargs):
        """Sum VAT collected by rate."""
        self._log.append(("tax_report", args, tuple(sorted(kwargs))))
        if self._closed and "tax_report" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("tax_report", *args, **kwargs)

    def currency_revalue(self, *args, **kwargs):
        """Restate foreign balances at today's rate."""
        self._log.append(("currency_revalue", args, tuple(sorted(kwargs))))
        if self._closed and "currency_revalue" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("currency_revalue", *args, **kwargs)

    def allocate_payment(self, *args, **kwargs):
        """Split a payment across several invoices."""
        self._log.append(("allocate_payment", args, tuple(sorted(kwargs))))
        if self._closed and "allocate_payment" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("allocate_payment", *args, **kwargs)

    def unallocate_payment(self, *args, **kwargs):
        """Undo a payment allocation."""
        self._log.append(("unallocate_payment", args, tuple(sorted(kwargs))))
        if self._closed and "unallocate_payment" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("unallocate_payment", *args, **kwargs)

    def credit_note(self, *args, **kwargs):
        """Post a credit against an invoice."""
        self._log.append(("credit_note", args, tuple(sorted(kwargs))))
        if self._closed and "credit_note" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("credit_note", *args, **kwargs)

    def refund(self, *args, **kwargs):
        """Post cash returned to a customer."""
        self._log.append(("refund", args, tuple(sorted(kwargs))))
        if self._closed and "refund" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("refund", *args, **kwargs)

    def recurring_entry(self, *args, **kwargs):
        """Schedule an entry to repeat monthly."""
        self._log.append(("recurring_entry", args, tuple(sorted(kwargs))))
        if self._closed and "recurring_entry" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("recurring_entry", *args, **kwargs)

    def cancel_recurring(self, *args, **kwargs):
        """Stop a scheduled recurring entry."""
        self._log.append(("cancel_recurring", args, tuple(sorted(kwargs))))
        if self._closed and "cancel_recurring" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("cancel_recurring", *args, **kwargs)

    def lock(self, *args, **kwargs):
        """Take the exclusive posting lock."""
        self._log.append(("lock", args, tuple(sorted(kwargs))))
        if self._closed and "lock" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("lock", *args, **kwargs)

    def unlock(self, *args, **kwargs):
        """Release the posting lock."""
        self._log.append(("unlock", args, tuple(sorted(kwargs))))
        if self._closed and "unlock" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("unlock", *args, **kwargs)

    def vacuum(self, *args, **kwargs):
        """Compact the database file."""
        self._log.append(("vacuum", args, tuple(sorted(kwargs))))
        if self._closed and "vacuum" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("vacuum", *args, **kwargs)

    def checkpoint(self, *args, **kwargs):
        """Force a WAL checkpoint."""
        self._log.append(("checkpoint", args, tuple(sorted(kwargs))))
        if self._closed and "checkpoint" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("checkpoint", *args, **kwargs)

    def stats(self, *args, **kwargs):
        """Return entry counts and database size."""
        self._log.append(("stats", args, tuple(sorted(kwargs))))
        if self._closed and "stats" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("stats", *args, **kwargs)

    def validate(self, *args, **kwargs):
        """Verify every entry balances and every period is consistent."""
        self._log.append(("validate", args, tuple(sorted(kwargs))))
        if self._closed and "validate" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("validate", *args, **kwargs)

    def repair(self, *args, **kwargs):
        """Fix balances that drifted from the journal."""
        self._log.append(("repair", args, tuple(sorted(kwargs))))
        if self._closed and "repair" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("repair", *args, **kwargs)

    def migrate(self, *args, **kwargs):
        """Apply pending schema migrations."""
        self._log.append(("migrate", args, tuple(sorted(kwargs))))
        if self._closed and "migrate" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("migrate", *args, **kwargs)

    def version(self, *args, **kwargs):
        """Return the ledger schema version."""
        self._log.append(("version", args, tuple(sorted(kwargs))))
        if self._closed and "version" not in ("open_books", "version", "stats"):
            raise LedgerClosed("ledger is closed")
        return self._dispatch("version", *args, **kwargs)
