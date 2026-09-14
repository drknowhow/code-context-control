from inv.money import percent_of


def test_halves_round_up():
    assert percent_of(1, 50) == 1
    assert percent_of(3, 50) == 2
    assert percent_of(5, 50) == 3
    assert percent_of(10, 25) == 3


def test_nearest_cent():
    assert percent_of(999, 15) == 150
    assert percent_of(1000, 10) == 100
    assert percent_of(0, 50) == 0
    assert percent_of(101, 12.5) == 13
    assert percent_of(98, 12.5) == 12


def test_returns_int():
    assert isinstance(percent_of(999, 15), int)
