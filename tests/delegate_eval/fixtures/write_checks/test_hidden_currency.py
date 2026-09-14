import pytest

from inv.money import format_money


def test_default_is_unchanged():
    assert format_money(1234) == "$12.34"
    assert format_money(-5) == "-$0.05"


def test_symbols():
    assert format_money(1234, "EUR") == "€12.34"
    assert format_money(-5, currency="GBP") == "-£0.05"
    assert format_money(123456789, "USD") == "$1,234,567.89"


def test_unknown_currency_names_the_code():
    with pytest.raises(ValueError, match="JPY"):
        format_money(1, "JPY")
