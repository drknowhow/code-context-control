"""Checkout flow."""
from app.payments import charge_card
from app.pricing import order_total


def checkout(cart, token):
    total = order_total(cart.items, cart.discount_percent)
    return charge_card(token, int(total * 100))
