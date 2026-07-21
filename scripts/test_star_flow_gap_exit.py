from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backtest_star_flow_gap_exit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("star_flow_gap_exit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_signal_requires_flow_spike_strong_close_and_next_gap():
    target = load_module()
    panel = pd.DataFrame({
        "trade_date": pd.date_range("2024-01-01", periods=8, freq="D"),
        "share": [100, 101, 102, 103, 104, 120, 121, 122],
        "pre_close": [1.00, 1.00, 1.01, 1.02, 1.03, 1.04, 1.08, 1.06],
        "open": [1.00, 1.01, 1.02, 1.03, 1.04, 1.05, 1.10, 1.06],
        "high": [1.02, 1.03, 1.04, 1.05, 1.06, 1.09, 1.11, 1.08],
        "low": [0.99, 1.00, 1.01, 1.02, 1.03, 1.04, 1.04, 1.05],
        "close": [1.00, 1.01, 1.02, 1.03, 1.04, 1.08, 1.06, 1.07],
    })

    featured = target.add_late_inflow_features(panel, lookback=3)
    events = target.make_gap_exit_events(
        featured,
        flow_threshold=0.8,
        min_signal_ret_pct=2.0,
        min_close_pos=0.7,
        min_next_gap_pct=1.0,
    )

    assert len(events) == 1
    row = events.iloc[0]
    assert row["trade_date"].strftime("%Y-%m-%d") == "2024-01-06"
    assert round(row["next_gap_pct"], 2) == 1.85
    assert round(row["next_open_to_close_pct"], 2) == -3.64
    assert round(row["sell_open_edge_pct"], 2) == 3.64


def test_summary_reports_open_exit_edge():
    target = load_module()
    events = pd.DataFrame({
        "next_gap_pct": [1.0, 2.0, 3.0],
        "next_open_to_close_pct": [-2.0, 1.0, -1.0],
        "sell_open_edge_pct": [2.0, -1.0, 1.0],
    })

    summary = target.summarize_events(events)

    assert summary["n"] == 3
    assert summary["next_open_to_close_mean_pct"] == -0.67
    assert summary["next_open_to_close_negative_rate_pct"] == 66.7
    assert summary["sell_open_edge_mean_pct"] == 0.67


if __name__ == "__main__":
    test_signal_requires_flow_spike_strong_close_and_next_gap()
    test_summary_reports_open_exit_edge()
    print("star flow gap exit tests ok")
