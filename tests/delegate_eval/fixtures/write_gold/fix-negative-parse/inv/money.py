"""Money helpers. Amounts are integer cents."""


def parse_amount(text: str) -> int:
    """'12.34' -> 1234. Accepts an optional leading '$' and thousands commas; '-' before or after '$'."""
    cleaned = text.strip()
    negative = False
    if cleaned.startswith("-"):
        negative, cleaned = True, cleaned[1:]
    cleaned = cleaned.lstrip("$")
    if cleaned.startswith("-"):
        negative, cleaned = True, cleaned[1:]
    cleaned = cleaned.replace(",", "")
    whole, _, frac = cleaned.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole or "0") * 100 + int(frac)
    return -cents if negative else cents


def format_money(cents: int) -> str:
    """1234 -> '$12.34'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def percent_of(cents: int, percent: float) -> int:
    """The given percent of an amount, in cents."""
    return int(cents * percent / 100)
