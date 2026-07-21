from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "backtest_huijin_etf_flow.py"
    spec = importlib.util.spec_from_file_location("backtest_huijin_etf_flow", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_fund_panel_uses_amount_units_and_blocks_mechanical_split():
    module = load_module()
    calendar = pd.date_range("2026-01-05", periods=3, freq="B")
    share = pd.DataFrame({
        "trade_date": ["20260105", "20260106", "20260107"],
        "fd_share": [100.0, 110.0, 220.0],
    })
    daily = pd.DataFrame({
        "trade_date": ["20260105", "20260106", "20260107"],
        "close": [10.0, 10.0, 5.0],
    })

    panel = module.build_fund_panel(
        share,
        daily,
        calendar,
        {"code": "510300.SH"},
    )

    assert panel.loc[calendar[1], "net_creation_yi"] == 0.01
    assert bool(panel.loc[calendar[2], "mechanical_adjustment"]) is True
    assert np.isnan(panel.loc[calendar[2], "net_creation_yi"])


def test_point_in_time_roster_activates_only_on_disclosure_date():
    module = load_module()
    calendar = pd.date_range("2026-03-27", periods=4, freq="B")
    share = pd.DataFrame({
        "trade_date": calendar.strftime("%Y%m%d"),
        "fd_share": [100.0, 101.0, 102.0, 103.0],
    })
    daily = pd.DataFrame({
        "trade_date": calendar.strftime("%Y%m%d"),
        "close": [4.0, 4.0, 4.0, 4.0],
    })
    item = {
        "code": "510300.SH",
        "name": "测试ETF",
        "strategy_eligible": True,
        "disclosed_on": "2026-03-31",
    }
    panel = module.build_fund_panel(share, daily, calendar, item)

    result = module.aggregate_panels(
        {item["code"]: panel}, [item], calendar, point_in_time=True
    )

    assert result.loc[pd.Timestamp("2026-03-30"), "active_count"] == 0
    assert result.loc[pd.Timestamp("2026-03-31"), "active_count"] == 1
    assert result.loc[pd.Timestamp("2026-03-31"), "observed_count"] == 1


def test_strategy_position_is_delayed_two_trading_days_and_charged_cost():
    module = load_module()
    dates = pd.date_range("2026-01-05", periods=8, freq="B")
    aggregate = pd.DataFrame(index=dates)
    aggregate.index.name = "date"
    aggregate["follow_target"] = [1.0, 0.5, 0.0, 0.5, 1.0, 0.5, 0.0, 0.5]
    aggregate["contrarian_target"] = 1.0 - aggregate["follow_target"]
    benchmark = pd.DataFrame({
        "trade_date": dates,
        "close": [100, 101, 102, 103, 104, 105, 106, 107],
    })

    metrics, frame = module.run_strategy_set(
        aggregate,
        benchmark,
        execution_lag=2,
        cost_bps=5.0,
        scope="test",
    )

    assert np.isnan(frame.iloc[0]["follow_position"])
    assert np.isnan(frame.iloc[1]["follow_position"])
    assert frame.iloc[2]["follow_position"] == 1.0
    follow = next(row for row in metrics if row["key"] == "follow")
    assert follow["cost_bps"] == 5.0
    assert follow["n_days"] == 6


def test_roster_keeps_direct_asset_and_sma_holder_types_separate():
    roster = json.loads((ROOT / "data" / "huijin_etf_roster.json").read_text(encoding="utf-8"))
    item = next(row for row in roster["items"] if row["code"] == "159901.SZ")

    assert {holder["type"] for holder in item["holders"]} == {
        "huijin_investment",
        "huijin_asset",
        "huijin_asset_sma",
    }


def test_fund_series_payload_exports_daily_share_history():
    module = load_module()
    calendar = pd.date_range("2026-01-05", periods=3, freq="B")
    share = pd.DataFrame({
        "trade_date": ["20260105", "20260106", "20260107"],
        "fd_share": [100.0, 110.0, 120.0],
    })
    daily = pd.DataFrame({
        "trade_date": ["20260105", "20260106", "20260107"],
        "close": [10.0, 10.0, 10.0],
    })
    item = {"code": "510300.SH", "name": "测试ETF"}
    panel = module.build_fund_panel(share, daily, calendar, item)

    payload = module.fund_series_payload([item], {item["code"]: panel}, calendar[-1])

    fund = payload["funds"]["510300.SH"]
    assert payload["as_of"] == calendar[-1].strftime("%Y-%m-%d")
    assert fund["name"] == "测试ETF"
    assert [row["date"] for row in fund["series"]] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert fund["series"][0]["share_yi"] == 0.01
    assert fund["series"][1]["net_creation_yi"] == 0.01


def test_template_states_proxy_boundary_and_has_both_backtest_scopes():
    html = (ROOT / "templates" / "huijin_etf_flow.html").read_text(encoding="utf-8")

    assert "这不是中央汇金的每日交易记录" in html
    assert "/api/huijin_etf_flow" in html
    assert "回测口径" in html
    assert "汇金披露份额" in html

