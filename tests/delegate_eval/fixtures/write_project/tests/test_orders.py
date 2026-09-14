from inv.orders import Order, calc_total, line_count
from inv.report import order_summary


def test_total_with_discount():
    order = Order(discount_percent=10)
    order.add_line("A", 500, 2)
    assert calc_total(order) == 900
    assert line_count(order) == 2


def test_summary():
    order = Order()
    order.add_line("A", 250, 4)
    assert order_summary(order) == "1 line(s), total $10.00"
