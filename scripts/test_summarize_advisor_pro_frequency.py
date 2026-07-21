import pandas as pd

from scripts.summarize_advisor_pro_frequency import combine_sleeves, distribution


def test_distribution_reports_min_median_and_max():
    assert distribution([3, 1, 4, 2]) == {"min": 1.0, "median": 2.5, "max": 4.0}


def test_combine_sleeves_keeps_later_sleeve_in_cash_before_it_starts():
    runs = [
        {"daily_path": [
            {"date": "2026-01-05", "net_return": 0.10},
            {"date": "2026-01-06", "net_return": 0.00},
        ]},
        {"daily_path": [
            {"date": "2026-01-06", "net_return": 0.20},
        ]},
    ]

    result = combine_sleeves(runs, return_field="net_return")

    assert result.index.equals(pd.to_datetime(["2026-01-05", "2026-01-06"]))
    assert round(float(result.iloc[0]), 6) == 0.05
    assert round(float(result.iloc[1]), 6) == round(1.15 / 1.05 - 1.0, 6)
