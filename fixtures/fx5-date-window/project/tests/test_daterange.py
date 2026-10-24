from datetime import date

from daterange import report_days


def test_window_is_positive():
    assert report_days(date(2024, 1, 1), date(2024, 1, 4)) > 0


def test_longer_window_is_larger():
    assert report_days(date(2024, 1, 1), date(2024, 1, 5)) > report_days(
        date(2024, 1, 1), date(2024, 1, 3)
    )
