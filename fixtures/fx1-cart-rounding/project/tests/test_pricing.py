from pricing import final_price_cents


def test_whole_dollar_discount():
    assert final_price_cents(2000, 10) == 1800


def test_no_discount():
    assert final_price_cents(1999, 0) == 1999
