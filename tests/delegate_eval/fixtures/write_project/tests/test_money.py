from inv.money import format_money, parse_amount, percent_of


def test_parse_amount():
    assert parse_amount("12.34") == 1234
    assert parse_amount("$1,000.5") == 100050
    assert parse_amount("7") == 700


def test_format_money():
    assert format_money(1234) == "$12.34"
    assert format_money(-5) == "-$0.05"
    assert format_money(123456789) == "$1,234,567.89"


def test_percent_of_whole_cents():
    assert percent_of(1000, 10) == 100
