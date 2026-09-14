from inv.money import parse_amount


def test_negative_amounts():
    assert parse_amount("-1.50") == -150
    assert parse_amount("-$2") == -200
    assert parse_amount("$-0.05") == -5
    assert parse_amount("-0.5") == -50


def test_existing_values_keep_parsing():
    assert parse_amount("12.34") == 1234
    assert parse_amount("$1,000.5") == 100050
    assert parse_amount("7") == 700
    assert parse_amount(" .5 ") == 50
    assert parse_amount("0.07") == 7
