"""Plain-text reports."""
from inv.money import format_money
from inv.orders import Order, calc_total


def order_summary(order: Order) -> str:
    return f"{len(order.lines)} line(s), total {format_money(calc_total(order))}"


def low_stock_lines(stock, skus, threshold: int = 5) -> list[str]:
    return [f"{sku}: {stock.level(sku)}" for sku in skus if stock.level(sku) < threshold]
