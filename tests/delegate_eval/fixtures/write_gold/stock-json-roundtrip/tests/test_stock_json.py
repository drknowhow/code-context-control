import pytest

from inv.stock import Stock


def test_round_trip():
    s = Stock()
    s.add("A", 4)
    assert Stock.from_json(s.to_json()).level("A") == 4


def test_zero_levels_are_left_out():
    s = Stock()
    s.add("A", 1)
    s.remove("A", 1)
    assert s.to_json() == "{}"


def test_rejects_non_int_levels():
    with pytest.raises(ValueError):
        Stock.from_json('{"A": 2.5}')
