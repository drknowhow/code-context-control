from inv.orders import Order, line_count, order_total
from inv.report import order_summary


def test_total_with_discount():
    order = Order(discount_percent=10)
    order.add_line("A", 500, 2)
    assert order_total(order) == 900
    assert line_count(order) == 2


def test_summary():
    order = Order()
    order.add_line("A", 250, 4)
    assert order_summary(order) == "1 line(s), total $10.00"
