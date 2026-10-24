from datetime import date


def report_days(start: date, end: date) -> int:
    """Number of days covered by a report from start to end, inclusive."""
    return (end - start).days
