"""Invoice model and totals."""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from ledgerlite.billing.tax import apply_vat


@dataclass
class LineItem:
    description: str
    quantity: int
    unit_price: Decimal


@dataclass
class Invoice:
    number: str
    customer_id: str
    country: str
    items: list[LineItem] = field(default_factory=list)

    def subtotal(self) -> Decimal:
        return sum((item.quantity * item.unit_price for item in self.items), Decimal("0"))

    def compute_total(self) -> Decimal:
        """Subtotal plus VAT for the customer's country, rounded once."""
        total = apply_vat(self.subtotal(), self.country)
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def export_csv(invoices: list[Invoice]) -> str:
    rows = ["number,customer_id,total"]
    for inv in invoices:
        rows.append(f"{inv.number},{inv.customer_id},{inv.compute_total()}")
    return "\n".join(rows)
