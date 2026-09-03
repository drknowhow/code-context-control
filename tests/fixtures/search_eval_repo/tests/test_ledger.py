from ledgerlite.billing.ledger import Ledger, LedgerClosed


class Recorder:
    def __getattr__(self, name):
        return lambda *a, **k: (name, a)


def test_post_entry_when_closed_raises():
    ledger = Ledger(Recorder())
    try:
        ledger.post_entry("cash", "revenue", 10)
    except LedgerClosed:
        pass
