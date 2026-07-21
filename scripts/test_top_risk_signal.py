from __future__ import annotations

import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import _toprisk_latest_record


def test_crowded_without_weakness_is_watch_not_high_risk():
    dates = pd.date_range("2024-01-01", periods=12, freq="D")
    panel = pd.DataFrame(
        {
            "trade_date": dates,
            "share": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 130, 132],
            "close": [10, 10.1, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 12.0, 12.3],
        }
    )

    record = _toprisk_latest_record("X", "测试板块", panel, lookback=5, benchmark=None)

    assert record["crowding"] is True
    assert record["weakness"] is False
    assert record["level"] == "watch"


def test_crowded_overheated_and_weak_is_high_top_risk():
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    panel = pd.DataFrame(
        {
            "trade_date": dates,
            "share": [
                100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                110, 111, 112, 113, 114, 115, 116, 117, 118, 119,
                120, 121, 122, 123, 124, 125, 126, 127, 150, 152,
            ],
            "close": [
                10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9,
                11.0, 11.2, 11.4, 11.6, 11.8, 12.0, 12.2, 12.4, 12.6, 12.8,
                13.0, 13.2, 13.4, 13.6, 13.8, 14.0, 13.9, 13.85, 13.82, 13.8,
            ],
        }
    )

    record = _toprisk_latest_record("X", "测试板块", panel, lookback=5, benchmark=None)

    assert record["crowding"] is True
    assert record["overheat"] is True
    assert record["weakness"] is True
    assert record["level"] == "high"


if __name__ == "__main__":
    test_crowded_without_weakness_is_watch_not_high_risk()
    test_crowded_overheated_and_weak_is_high_top_risk()
    print("ok")
