from __future__ import annotations

import pandas as pd

from backtest_etf_flow_signal import add_flow_features, forward_outcomes, make_signal_events


def test_make_signal_events_uses_rolling_percentile_without_lookahead():
    dates = pd.date_range("2024-01-01", periods=8, freq="D")
    panel = pd.DataFrame(
        {
            "trade_date": dates,
            "share": [100, 101, 102, 103, 104, 105, 120, 121],
            "close": [1.0] * 8,
        }
    )

    featured = add_flow_features(panel, lookback=3)
    events = make_signal_events(featured, threshold=0.8)

    assert events["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-07"]
    assert events["share_chg_5d"].round(4).tolist() == [0.1765]


def test_make_signal_events_can_select_large_share_decreases():
    dates = pd.date_range("2024-01-01", periods=8, freq="D")
    panel = pd.DataFrame(
        {
            "trade_date": dates,
            "share": [121, 120, 119, 118, 117, 116, 100, 99],
            "close": [1.0] * 8,
        }
    )

    featured = add_flow_features(panel, lookback=3)
    events = make_signal_events(featured, threshold=0.34, direction="decrease")

    assert events["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-07"]
    assert events["share_chg_5d"].round(4).tolist() == [-0.1597]


def test_forward_outcomes_reports_returns_drawdown_and_top_hit():
    index = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=8, freq="D"),
            "close": [100, 103, 105, 104, 101, 99, 98, 100],
        }
    )
    events = pd.DataFrame({"trade_date": [pd.Timestamp("2024-01-03")]})

    out = forward_outcomes(events, index, horizons=(3, 5), top_window=2)

    assert round(out.loc[0, "ret_3d"], 4) == -0.0381
    assert round(out.loc[0, "mdd_5d"], 4) == -0.0667
    assert bool(out.loc[0, "near_top"]) is True


if __name__ == "__main__":
    test_make_signal_events_uses_rolling_percentile_without_lookahead()
    test_make_signal_events_can_select_large_share_decreases()
    test_forward_outcomes_reports_returns_drawdown_and_top_hit()
    print("ok")
