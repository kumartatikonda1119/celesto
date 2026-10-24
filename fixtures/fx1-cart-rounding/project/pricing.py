"""Tiny pricing helpers used by a storefront."""


def final_price_cents(price_cents: int, discount_percent: int) -> int:
    """Return the price after a percentage discount, in cents."""
    discount = price_cents // 100 * discount_percent
    return price_cents - discount
