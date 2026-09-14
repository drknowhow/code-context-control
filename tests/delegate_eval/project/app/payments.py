"""Payment gateway adapter."""


class PaymentDeclined(Exception):
    pass


def charge_card(token: str, amount_cents: int) -> str:
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    if token.startswith("tok_declined"):
        raise PaymentDeclined(token)
    return f"ch_{token[-6:]}_{amount_cents}"
