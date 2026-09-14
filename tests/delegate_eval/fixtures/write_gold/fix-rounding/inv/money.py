"""Money helpers. Amounts are integer cents."""
from decimal import ROUND_HALF_UP, Decimal


def parse_amount(text: str) -> int:
    """'12.34' -> 1234. Accepts an optional leading '$' and thousands commas."""
    cleaned = text.strip().lstrip("$").replace(",", "")
    whole, _, frac = cleaned.partition(".")
    frac = (frac + "00")[:2]
    return int(whole or "0") * 100 + int(frac)


def format_money(cents: int) -> str:
    """1234 -> '$12.34'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def percent_of(cents: int, percent: float) -> int:
    """The given percent of an amount, in cents, rounded to the nearest cent (halves up)."""
    exact = Decimal(cents) * Decimal(str(percent)) / 100
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))
