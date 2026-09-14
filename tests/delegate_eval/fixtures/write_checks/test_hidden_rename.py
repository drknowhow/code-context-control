import inv.orders as orders
from inv.orders import Order, order_total
from inv.report import order_summary


def test_renamed_without_alias():
    assert not hasattr(orders, "calc_total")
    order = Order(discount_percent=50)
    order.add_line("A", 100, 1)
    assert order_total(order) == 50
    assert order_summary(order) == "1 line(s), total $0.50"
