import pytest

from inv.stock import Stock


def test_partial_removal_reduces_the_level():
    s = Stock()
    s.add("A", 5)
    s.remove("A", 2)
    assert s.level("A") == 3


@pytest.mark.parametrize("qty", [0, -1, 1.5])
def test_bad_quantities_raise(qty):
    s = Stock()
    s.add("A", 5)
    with pytest.raises(ValueError):
        s.remove("A", qty)


def test_other_skus_are_untouched():
    s = Stock()
    s.add("A", 5)
    s.add("B", 7)
    s.remove("A", 1)
    assert s.level("B") == 7
