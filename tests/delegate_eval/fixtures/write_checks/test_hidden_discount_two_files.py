import pytest

from inv.orders import Order
from inv.report import order_summary


def test_rejects_out_of_range_discounts():
    for bad in (-1, 100.5, 250):
        with pytest.raises(ValueError):
            Order(discount_percent=bad)
    Order(discount_percent=0)
    Order(discount_percent=100)


def _order(discount):
    order = Order(discount_percent=discount)
    order.add_line("A", 1000, 1)
    return order


def test_summary_names_the_discount():
    assert order_summary(_order(10)) == "1 line(s), total $9.00 (10% off)"
    assert order_summary(_order(10.0)) == "1 line(s), total $9.00 (10% off)"
    assert order_summary(_order(12.5)) == "1 line(s), total $8.75 (12.5% off)"


def test_no_discount_reads_as_before():
    order = Order()
    order.add_line("A", 250, 4)
    assert order_summary(order) == "1 line(s), total $10.00"
