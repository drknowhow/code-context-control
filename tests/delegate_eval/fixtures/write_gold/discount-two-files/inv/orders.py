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

    def __post_init__(self) -> None:
        if not 0 <= self.discount_percent <= 100:
            raise ValueError(f"discount_percent must be 0..100, got {self.discount_percent!r}")

    def add_line(self, sku: str, unit_cents: int, qty: int) -> None:
        self.lines.append(Line(sku, unit_cents, qty))


def calc_total(order: Order) -> int:
    subtotal = sum(line.unit_cents * line.qty for line in order.lines)
    return subtotal - percent_of(subtotal, order.discount_percent)


def line_count(order: Order) -> int:
    return sum(line.qty for line in order.lines)
