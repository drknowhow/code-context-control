"""Price calculation for the demo shop."""
from dataclasses import dataclass


@dataclass
class LineItem:
    sku: str
    unit_price: float
    quantity: int


def apply_discount(price: float, discount_percent: int) -> float:
    """Return price after a percentage discount, e.g. 15 for 15% off."""
    return price * (1 - discount_percent)


def order_total(items: list[LineItem], discount_percent: int = 0) -> float:
    subtotal = sum(item.unit_price * item.quantity for item in items)
    return round(apply_discount(subtotal, discount_percent), 2)
