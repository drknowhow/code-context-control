"""VAT rates and application."""
from decimal import Decimal

VAT_RATES = {
    "DE": Decimal("0.19"),
    "FR": Decimal("0.20"),
    "NL": Decimal("0.21"),
    "US": Decimal("0.00"),
}


def vat_rate(country: str) -> Decimal:
    return VAT_RATES.get(country.upper(), Decimal("0"))


def apply_vat(amount: Decimal, country: str) -> Decimal:
    """Return amount including VAT for country."""
    return amount * (Decimal("1") + vat_rate(country))
