import json

import pytest

from inv.stock import Stock


def test_to_json_sorted_and_nonzero():
    s = Stock()
    s.add("B", 2)
    s.add("A", 1)
    s.add("C", 3)
    s.remove("C", 3)
    text = s.to_json()
    assert json.loads(text) == {"A": 1, "B": 2}
    assert text.index('"A"') < text.index('"B"')


def test_round_trip():
    s = Stock()
    s.add("X", 5)
    t = Stock.from_json(s.to_json())
    assert isinstance(t, Stock) and t.level("X") == 5


@pytest.mark.parametrize("text", ["[1, 2]", '{"A": 1.5}', '{"A": "3"}', '{"A": true}', "not json"])
def test_from_json_rejects(text):
    with pytest.raises(ValueError):
        Stock.from_json(text)
