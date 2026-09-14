import pytest

from inv.stock import Stock


def test_add_accumulates():
    s = Stock()
    s.add("A", 2)
    s.add("A", 3)
    assert s.level("A") == 5


def test_add_rejects_bad_quantities():
    with pytest.raises(ValueError):
        Stock().add("A", 0)
