from __future__ import annotations

import pandas as pd

from backtest_sector_etf_flow_signal import backtest_one_etf


def test_backtest_one_etf_uses_each_etf_own_price_series():
    panel = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=9, freq="D"),
            "share": [100, 101, 102, 103, 104, 105, 125, 126, 127],
            "close": [1.00, 1.02, 1.04, 1.08, 1.10, 1.09, 1.12, 1.05, 1.03],
        }
    )

    out = backtest_one_etf(
        "512480.SH",
        "semi",
        panel,
        direction="increase",
        threshold=0.8,
        lookback=3,
        horizons=(3,),
    )

    assert len(out) == 1
    assert out.loc[0, "ts_code"] == "512480.SH"
    assert out.loc[0, "name"] == "semi"
    assert round(out.loc[0, "ret_3d"], 4) == -0.0804
    assert round(out.loc[0, "mdd_3d"], 4) == -0.0804


if __name__ == "__main__":
    test_backtest_one_etf_uses_each_etf_own_price_series()
    print("ok")
