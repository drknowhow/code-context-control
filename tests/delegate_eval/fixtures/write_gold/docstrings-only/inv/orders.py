"""Orders and their totals."""
from dataclasses import dataclass, field

from inv.money import percent_of


@dataclass
class Line:
    sku: str
    unit_cents: int
    qty: int


@dataclass
class Order:
    lines: list[Line] = field(default_factory=list)
    discount_percent: float = 0

    def add_line(self, sku: str, unit_cents: int, qty: int) -> None:
        """Append a line for qty units of sku at unit_cents each."""
        self.lines.append(Line(sku, unit_cents, qty))


def calc_total(order: Order) -> int:
    """The order's subtotal minus its percentage discount, in cents."""
    subtotal = sum(line.unit_cents * line.qty for line in order.lines)
    return subtotal - percent_of(subtotal, order.discount_percent)


def line_count(order: Order) -> int:
    """Total units across all lines."""
    return sum(line.qty for line in order.lines)
