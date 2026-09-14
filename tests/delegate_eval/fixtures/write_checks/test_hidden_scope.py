import pytest

from inv.stock import InsufficientStock, Stock


def test_overdraw_raises_and_keeps_the_level():
    s = Stock()
    s.add("SKU-77", 4)
    with pytest.raises(InsufficientStock) as exc:
        s.remove("SKU-77", 13)
    msg = str(exc.value)
    assert "SKU-77" in msg and "13" in msg and "4" in msg
    assert s.level("SKU-77") == 4
    assert issubclass(InsufficientStock, ValueError)


def test_removing_the_whole_level_is_allowed():
    s = Stock()
    s.add("A", 2)
    s.remove("A", 2)
    assert s.level("A") == 0


def test_bad_quantity_is_still_a_value_error():
    with pytest.raises(ValueError):
        Stock().remove("A", 0)
