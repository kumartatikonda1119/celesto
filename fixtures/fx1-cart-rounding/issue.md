# Discount is wrong for prices that are not whole dollars

A 10% discount on a $19.99 item is charged as $18.09, but the customer expects
$17.99.

Reproduction notes from the bug report:

- `final_price_cents(1999, 10)` returns `1809`
- changing the price to `2000` happens to look correct
- the helper lives in `pricing.py`

The discount appears to be computed in whole dollars and then subtracted from
the cent amount.
