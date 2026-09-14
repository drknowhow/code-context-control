"""Money helpers. Amounts are integer cents."""

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def parse_amount(text: str) -> int:
    """'12.34' -> 1234. Accepts an optional leading '$' and thousands commas."""
    cleaned = text.strip().lstrip("$").replace(",", "")
    whole, _, frac = cleaned.partition(".")
    frac = (frac + "00")[:2]
    return int(whole or "0") * 100 + int(frac)


def format_money(cents: int, currency: str = "USD") -> str:
    """1234 -> '$12.34'; 1234, 'EUR' -> '€12.34'."""
    try:
        symbol = _SYMBOLS[currency]
    except KeyError:
        raise ValueError(f"unknown currency {currency!r}") from None
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{symbol}{cents // 100:,}.{cents % 100:02d}"


def percent_of(cents: int, percent: float) -> int:
    """The given percent of an amount, in cents."""
    return int(cents * percent / 100)
