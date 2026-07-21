from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_backtest_module():
    path = ROOT / "scripts" / "backtest_rolling_earnings.py"
    spec = importlib.util.spec_from_file_location("rolling_backtest_tradeability", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


backtest = load_backtest_module()


def test_market_returns_match_stock_ids_and_entry_price(monkeypatch, tmp_path: Path):
    frames = {
        "20240102": pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "open": [10.0, 20.0],
            "close": [11.0, 18.0],
            "adj_factor": [1.0, 1.0],
        }),
        "20240103": pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "open": [11.0, 18.0, 30.0],
            "close": [12.0, 22.0, 33.0],
            "adj_factor": [1.0, 1.0, 1.0],
        }),
    }

    def fake_read(path, columns):
        return frames[Path(path).stem][columns].copy()

    monkeypatch.setattr(backtest.pd, "read_parquet", fake_read)
    result = backtest.load_market_returns(tmp_path, ["20240102", "20240103"], [1, 2])

    close_to_close = ((12.0 / 11.0 - 1.0) + (22.0 / 18.0 - 1.0)) / 2.0
    intraday = ((11.0 / 10.0 - 1.0) + (18.0 / 20.0 - 1.0)) / 2.0
    assert result[("20240102", 2, "open")] == pytest.approx(
        (1.0 + intraday) * (1.0 + close_to_close) - 1.0
    )
    assert result[("20240102", 2, "close")] == pytest.approx(close_to_close)
    assert result[("20240102", 2)] == result[("20240102", 2, "open")]


def test_missing_benchmark_is_null_not_zero_excess():
    events = pd.DataFrame([{
        "ts_code": "000001.SZ",
        "ann_dt": pd.Timestamp("2024-01-01"),
        "end_date": "20231231",
        "dedt_yoy": 50.0,
        "prev_dedt_yoy": 20.0,
    }])
    by_code = {
        "000001": pd.DataFrame({
            "trade_date": ["20240102"],
            "open_adj": [10.0],
            "close_adj": [11.0],
        })
    }

    trades = backtest.build_trades(
        events, by_code, {}, [1], 50, {}, "conservative"
    )

    assert trades[0]["ret_1"] == pytest.approx(10.0)
    assert trades[0]["benchmark_1"] is None
    assert trades[0]["excess_1"] is None


def test_financial_events_replay_disclosure_versions_without_revision_lookahead(tmp_path: Path):
    db_path = tmp_path / "financials.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE fina_indicator_versions (
                version_id INTEGER PRIMARY KEY,
                ts_code TEXT, ann_date TEXT, end_date TEXT, update_flag TEXT,
                q_dtprofit REAL, ingested_at TEXT, source TEXT, source_vintage TEXT
            )
            """
        )
        rows = [
            (1, "000001.SZ", "20211020", "20210930", "0", 10.0),
            (2, "000001.SZ", "20220320", "20211231", "0", 10.0),
            (3, "000001.SZ", "20221020", "20220930", "0", 15.0),
            (4, "000001.SZ", "20230320", "20221231", "0", 20.0),
            # A later downward revision must not overwrite what was known in March.
            (5, "000001.SZ", "20230501", "20221231", "1", 5.0),
        ]
        conn.executemany(
            "INSERT INTO fina_indicator_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (*row, f"2024-01-0{row[0]}T00:00:00+00:00", "tushare.fina_indicator", "backfill")
                for row in rows
            ],
        )

    events = backtest.load_financial_events(db_path, min_growth=20.0)

    assert len(events) == 1
    assert events.iloc[0]["ann_date"] == "20230320"
    assert events.iloc[0]["q_dtprofit"] == 20.0
    assert events.iloc[0]["dedt_yoy"] == pytest.approx(100.0)
    assert events.iloc[0]["prev_dedt_yoy"] == pytest.approx(50.0)
    assert events.iloc[0]["pit_quality"] == "native_versions"


def test_rolling_curve_charges_turnover_costs():
    trades = [
        {"code": "000001", "entry_date": "20240102", "dedt_yoy": 50.0, "delta": 10.0},
        {"code": "000002", "entry_date": "20240103", "dedt_yoy": 80.0, "delta": 10.0},
    ]
    by_code = {
        "000001": pd.DataFrame({"trade_date": ["20240102", "20240103"], "close_adj": [10.0, 11.0]}),
        "000002": pd.DataFrame({"trade_date": ["20240102", "20240103"], "close_adj": [20.0, 22.0]}),
    }

    curve = backtest.build_rolling_portfolio_curve(
        trades,
        by_code,
        ["20240102", "20240103"],
        topn=1,
        buy_cost_rate=0.01,
        sell_cost_rate=0.02,
    )

    assert curve["nav"] == [0.99, 1.0563]
    assert curve["turnover_pct"] == [100.0, 200.0]
    assert curve["trading_cost_pct"] == [1.0, 3.0]
    assert curve["cumulative_cost_drag_pct"] == pytest.approx(3.97)


def test_research_variant_cannot_become_recommended():
    variants = {
        "base": {
            "selection_eligible": True,
            "summary": {"final_nav": 1.2, "mdd_pct": -10.0, "sharpe": 0.5, "win_rate_pct": 51.0},
        },
        "full_sample_winner": {
            "selection_eligible": False,
            "summary": {"final_nav": 2.0, "mdd_pct": -9.0, "sharpe": 2.0, "win_rate_pct": 60.0},
        },
    }

    assert backtest.choose_rolling_recommended(variants, baseline_key="base") == "base"
    assert backtest.choose_rolling_recommended(
        variants,
        baseline_key="base",
        respect_selection_eligibility=False,
    ) == "full_sample_winner"
