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
        self.lines.append(Line(sku, unit_cents, qty))


def order_total(order: Order) -> int:
    subtotal = sum(line.unit_cents * line.qty for line in order.lines)
    return subtotal - percent_of(subtotal, order.discount_percent)


def line_count(order: Order) -> int:
    return sum(line.qty for line in order.lines)
