import pytest

from inv import stock


def test_helper_raises_the_same_error():
    for bad in (0, -1, 1.5, "2"):
        with pytest.raises(ValueError) as exc:
            stock._validate_qty(bad)
        assert str(exc.value) == f"quantity must be a positive integer, got {bad!r}"
    assert stock._validate_qty(3) is None


def test_both_methods_use_it(monkeypatch):
    calls = []
    monkeypatch.setattr(stock, "_validate_qty", lambda qty: calls.append(qty))
    s = stock.Stock()
    s.add("A", 5)
    s.remove("A", 2)
    assert calls == [5, 2]
    assert s.level("A") == 3
