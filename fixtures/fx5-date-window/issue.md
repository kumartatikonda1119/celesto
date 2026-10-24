# A single-day report reports zero days

`report_days(date(2024, 1, 1), date(2024, 1, 1))` returns `0`, but a report
that covers one calendar day should report `1`.

Multi-day windows are also off by one (2024-01-01 to 2024-01-04 should be 4
days, not 3). The docstring says the range is inclusive. The helper is
`report_days` in `daterange.py`.
