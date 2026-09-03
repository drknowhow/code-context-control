from decimal import Decimal

from ledgerlite.billing.invoice import Invoice, LineItem


def test_compute_total_applies_vat():
    inv = Invoice("INV-1", "c1", "DE", [LineItem("x", 2, Decimal("10.00"))])
    assert inv.compute_total() == Decimal("23.80")
