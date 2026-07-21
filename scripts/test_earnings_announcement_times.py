from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


exporter = load_module("scripts/export_earnings_announcement_times.py", "earnings_ann_exporter")
backtest = load_module("scripts/backtest_rolling_earnings.py", "rolling_backtest")
cninfo_helper = load_module("scripts/cninfo_query.py", "earnings_cninfo_helper")
event_backfill = load_module("scripts/backfill_earnings_event_times.py", "earnings_event_backfill")


def test_cninfo_announcement_time_is_kept_as_datetime():
    ms = int(datetime(2026, 7, 3, 18, 40, 5).timestamp() * 1000)
    date, dt = exporter.announcement_datetime({"announcementTime": ms})
    assert date == "2026-07-03"
    assert dt == "2026-07-03 18:40:05"


def test_earnings_title_filter_keeps_real_reports_and_skips_abstracts():
    assert exporter.is_earnings_title("2026年半年度业绩预告")
    assert exporter.is_earnings_title("2026年半年度业绩预告更正公告")
    assert exporter.is_earnings_title("2026年半年度业绩预告（修订版）")
    assert exporter.is_earnings_title("2026年半年度业绩预告补充公告")
    assert exporter.is_earnings_title("2026年半年度报告")
    assert not exporter.is_earnings_title("2026年半年度报告摘要")
    assert exporter.is_earnings_title("重庆水务集团股份有限公司2022年年报")
    assert not exporter.is_earnings_title("重庆水务集团股份有限公司2022年年报摘要")
    assert exporter.is_earnings_title("中国平安2022年中期报告")
    assert not exporter.is_earnings_title("中国平安2022年中期报告摘要")
    assert exporter.is_earnings_title("H股公告-2021年半年度报告")
    assert not exporter.is_earnings_title("关于延期披露《2025年年度报告》的公告")
    assert not exporter.is_earnings_title("关于调整2026年一季度报告的公告")


def test_code_market_maps_bse_920_codes_to_bj_column():
    assert exporter.code_market("920819") == "bj"
    assert exporter.code_market("688001") == "sse"
    assert exporter.code_market("301308") == "szse"


def test_collect_for_code_uses_name_fallback_for_bse_920_legacy_codes():
    old_query = exporter.cninfo_query
    old_fulltext = exporter.cninfo_fulltext_query
    calls = []

    def fake_query(code, start, end, page=1):
        return {"announcements": [], "totalAnnouncement": 0}

    def fake_fulltext(searchkey, start, end, page=1, column="szse"):
        calls.append((searchkey, column))
        if searchkey == "颖泰生物 半年度报告" and column == "bj":
            return {
                "totalAnnouncement": 1,
                "announcements": [{
                    "secCode": "833819",
                    "secName": "颖泰生物",
                    "announcementTitle": "2022年半年度报告",
                    "announcementTime": int(datetime(2022, 8, 10).timestamp() * 1000),
                    "adjunctUrl": "finalpage/report.pdf",
                }],
            }
        return {"announcements": [], "totalAnnouncement": 0}

    try:
        exporter.cninfo_query = fake_query
        exporter.cninfo_fulltext_query = fake_fulltext
        rows = exporter.collect_for_code("920819", "2022-08-01", "2022-08-31", sleep_s=0.0, max_pages=1, name="颖泰生物")
    finally:
        exporter.cninfo_query = old_query
        exporter.cninfo_fulltext_query = old_fulltext

    assert rows
    assert rows[0]["code"] == "920819"
    assert rows[0]["title"] == "2022年半年度报告"
    assert ("颖泰生物 半年度报告", "bj") in calls


def test_earnings_pagination_fails_closed_when_max_pages_is_saturated():
    page = {
        "announcements": [
            {
                "secCode": "000001",
                "announcementTitle": "业绩预告",
                "announcementTime": 1_700_000_000_000 + index,
            }
            for index in range(exporter.PAGE_SIZE)
        ],
        "totalAnnouncement": exporter.PAGE_SIZE + 1,
    }
    with pytest.raises(RuntimeError, match="pagination saturated"):
        exporter.validated_announcement_page(
            page,
            page=1,
            max_pages=1,
            context="test",
        )


def test_earnings_pagination_rejects_falsey_non_list_rows():
    with pytest.raises(RuntimeError, match="announcements must be a list"):
        exporter.validated_announcement_page(
            {"announcements": {}, "totalAnnouncement": 0},
            page=1,
            max_pages=1,
            context="test",
        )


def test_shared_cninfo_query_rejects_falsey_non_list_rows():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"announcements": {}}

    with pytest.raises(cninfo_helper.CNInfoQueryError, match="announcements must be a list"):
        cninfo_helper.query_announcements(
            "test",
            "2026-07-01~2026-07-18",
            "szse",
            max_pages=1,
            request_post=lambda *_args, **_kwargs: Response(),
            sleep=lambda _seconds: None,
        )


def test_failed_code_is_retried_in_same_incremental_window(tmp_path, monkeypatch):
    calls = []

    def fake_collect(code, start, end, sleep_s, max_pages):
        calls.append(code)
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")
        return [{
            "code": code,
            "ann_date": "2026-07-03",
            "title": "2026年半年度报告",
        }]

    monkeypatch.setattr(exporter, "collect_for_code", fake_collect)
    kwargs = {
        "data_dir": tmp_path,
        "start": "2026-07-01",
        "end": "2026-07-18",
        "codes": ["000001"],
        "incremental": True,
        "overlap_days": 2,
        "sleep_s": 0.0,
        "max_pages": 1,
        "flush_every": 1,
        "workers": 1,
    }

    failed = exporter.export(**kwargs)
    recovered = exporter.export(**kwargs)

    assert failed["errors"]
    assert failed["query"]["processed_codes"] == []
    assert calls == ["000001", "000001"]
    assert recovered["errors"] == []
    assert recovered["query"]["processed_codes"] == ["000001"]


def test_failed_global_full_refresh_preserves_existing_snapshot(tmp_path, monkeypatch):
    out_path = tmp_path / "cninfo_earnings_announcements.json"
    original = b'{"items":[{"code":"000001","ann_date":"2026-07-01","title":"old"}]}\n'
    out_path.write_bytes(original)

    def fail_global(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(exporter, "collect_global", fail_global)
    payload = exporter.export(
        data_dir=tmp_path,
        start="2026-07-01",
        end="2026-07-18",
        codes=[],
        incremental=False,
        overlap_days=2,
        sleep_s=0.0,
        max_pages=1,
        flush_every=1,
        workers=1,
    )

    assert payload["errors"] == [{"code": "GLOBAL", "error": "provider unavailable"}]
    assert out_path.read_bytes() == original


def test_failed_per_code_full_refresh_never_publishes_partial_rows(tmp_path, monkeypatch):
    out_path = tmp_path / "cninfo_earnings_announcements.json"
    original = b'{"items":[{"code":"000009","ann_date":"2026-06-30","title":"old"}]}\n'
    out_path.write_bytes(original)

    def collect(code, *_args, **_kwargs):
        if code == "000002":
            raise RuntimeError("second code failed")
        return [{"code": code, "ann_date": "2026-07-03", "title": "new"}]

    monkeypatch.setattr(exporter, "collect_for_code", collect)
    payload = exporter.export(
        data_dir=tmp_path,
        start="2026-07-01",
        end="2026-07-18",
        codes=["000001", "000002"],
        incremental=False,
        overlap_days=2,
        sleep_s=0.0,
        max_pages=1,
        flush_every=1,
        workers=1,
    )

    assert [row["code"] for row in payload["items"]] == ["000001"]
    assert payload["errors"] == [{"code": "000002", "error": "second code failed"}]
    assert out_path.read_bytes() == original


def test_event_backfill_keeps_out_of_window_bases_for_growth_signal(tmp_path):
    db_path = tmp_path / "financials.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "create table fina_indicators "
            "(ts_code text, ann_date text, end_date text, q_dtprofit real)"
        )
        conn.executemany(
            "insert into fina_indicators values (?, ?, ?, ?)",
            [
                ("000001.SZ", "20210430", "20210331", 100.0),
                ("000001.SZ", "20210830", "20210630", 100.0),
                ("000001.SZ", "20220430", "20220331", 120.0),
                ("000001.SZ", "20220830", "20220630", 150.0),
            ],
        )

    keys = event_backfill.load_financial_event_keys(
        db_path,
        start="2022-08-01",
        end="2022-08-31",
        min_growth=20.0,
    )

    assert keys == {("000001", "20220830")}


def test_entry_rule_uses_intraday_close_and_after_close_next_open():
    dates = ["20260703", "20260706"]
    intraday = {"ann_dt": backtest.pd.Timestamp("2026-07-03 13:20:00")}
    after_close = {"ann_dt": backtest.pd.Timestamp("2026-07-03 18:20:00")}
    weekend = {"ann_dt": backtest.pd.Timestamp("2026-07-04 09:00:00")}

    assert backtest.choose_entry_index(dates, "20260703", intraday, "timed") == (
        0, "same_day_close_intraday_cninfo_time", "close_adj")
    assert backtest.choose_entry_index(dates, "20260703", after_close, "timed") == (
        1, "next_open_after_close", "open_adj")
    assert backtest.choose_entry_index(dates, "20260703", weekend, "timed") == (
        1, "next_open_non_trading_announcement_day", "open_adj")


def test_entry_lag_uses_nth_trading_day_after_announcement_open():
    dates = ["20260703", "20260706", "20260707", "20260708", "20260709"]

    assert backtest.choose_entry_index_after_announcement_lag(dates, "20260703", 1) == (
        1, "ann_plus_1_trading_day_open", "open_adj")
    assert backtest.choose_entry_index_after_announcement_lag(dates, "20260703", 3) == (
        3, "ann_plus_3_trading_day_open", "open_adj")


def test_build_trades_can_enter_on_third_trading_day_after_announcement():
    events = backtest.pd.DataFrame([{
        "ts_code": "000001.SZ",
        "ann_dt": backtest.pd.Timestamp("2026-07-03"),
        "end_date": "20260630",
        "dedt_yoy": 80.0,
        "prev_dedt_yoy": 30.0,
    }])
    by_code = {
        "000001": backtest.pd.DataFrame({
            "trade_date": ["20260703", "20260706", "20260707", "20260708", "20260709"],
            "open_adj": [10.0, 11.0, 12.0, 13.0, 13.5],
            "close_adj": [10.5, 11.5, 12.5, 13.2, 14.3],
        })
    }

    trades = backtest.build_trades(
        events,
        by_code,
        market={},
        horizons=[2],
        topn=50,
        ann_times={},
        entry_mode="ann_plus_3_trading_day",
        entry_lag_days=3,
    )

    assert trades[0]["entry_date"] == "20260708"
    assert trades[0]["entry_rule"] == "ann_plus_3_trading_day_open"
    assert trades[0]["entry_price_type"] == "open"
    assert trades[0]["ret_2"] == 10.0


def test_entry_lag_backtest_can_filter_interim_period_suffix():
    old_load_events = backtest.load_financial_events
    old_load_prices = backtest.load_price_panel

    def fake_load_events(db_path, min_growth):
        return backtest.pd.DataFrame([
            {
                "ts_code": "000001.SZ",
                "ann_date": "20260703",
                "ann_dt": backtest.pd.Timestamp("2026-07-03"),
                "end_date": "20260630",
                "dedt_yoy": 80.0,
                "prev_dedt_yoy": 30.0,
            },
            {
                "ts_code": "000002.SZ",
                "ann_date": "20260430",
                "ann_dt": backtest.pd.Timestamp("2026-04-30"),
                "end_date": "20260331",
                "dedt_yoy": 70.0,
                "prev_dedt_yoy": 25.0,
            },
        ])

    def fake_load_prices(parquet_dir, start, end, codes):
        assert codes == {"000001"}
        return backtest.pd.DataFrame({
            "c6": ["000001", "000001"],
            "trade_date": ["20260706", "20260707"],
            "open_adj": [10.0, 11.0],
            "close_adj": [10.5, 11.5],
        }), ["20260706", "20260707"]

    try:
        backtest.load_financial_events = fake_load_events
        backtest.load_price_panel = fake_load_prices
        result = backtest.run_entry_lag_backtest(
            Path("financials.db"),
            Path("daily"),
            horizons=[1],
            lags=[1],
            period_suffix="0630",
        )
    finally:
        backtest.load_financial_events = old_load_events
        backtest.load_price_panel = old_load_prices

    assert result["n_source_events"] == 1
    assert result["n_codes"] == 1
    assert result["params"]["period_suffix"] == "0630"
    assert result["entry_lag_analysis"]["1"]["summary"]["1"]["mean_pct"] == 5.0


def test_lookup_announcement_time_allows_nearby_cninfo_date():
    ann_times = {
        ("301308", "20260704"): {
            "ann_dt": backtest.pd.Timestamp("2026-07-04 08:00:00"),
            "cninfo_ann_date": "20260704",
        }
    }
    found = backtest.lookup_announcement_time(ann_times, "301308", "20260703")
    assert found["cninfo_ann_date"] == "20260704"


def test_event_curve_uses_all_trades_grouped_by_entry_date():
    trades = [
        {"entry_date": "20240102", "ret_10": 10.0},
        {"entry_date": "20240102", "ret_10": -2.0},
        {"entry_date": "20240110", "ret_10": 5.0},
        {"entry_date": "20240110", "ret_10": None},
    ]

    curves = backtest.build_event_curves(trades, [10])

    assert curves["10"]["dates"] == ["2024-01-02", "2024-01-10"]
    assert curves["10"]["nav"] == [1.04, 1.092]
    assert curves["10"]["daily_return_pct"] == [4.0, 5.0]
    assert curves["10"]["n_events"] == [2, 1]


def test_rolling_portfolio_curve_rebalances_as_new_signals_arrive():
    trades = [
        {"code": "000001", "entry_date": "20240102", "dedt_yoy": 50.0, "delta": 10.0},
        {"code": "000002", "entry_date": "20240103", "dedt_yoy": 80.0, "delta": 5.0},
    ]
    by_code = {
        "000001": backtest.pd.DataFrame({
            "trade_date": ["20240102", "20240103", "20240104"],
            "close_adj": [10.0, 11.0, 12.1],
        }),
        "000002": backtest.pd.DataFrame({
            "trade_date": ["20240102", "20240103", "20240104"],
            "close_adj": [20.0, 20.0, 22.0],
        }),
    }

    curve = backtest.build_rolling_portfolio_curve(trades, by_code, ["20240102", "20240103", "20240104"], topn=1)

    assert curve["dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert curve["nav"] == [1.0, 1.1, 1.21]
    assert curve["holding_count"] == [1, 1, 1]
    assert curve["top_codes"][-1] == ["000002"]


def test_rolling_portfolio_variants_can_reduce_exposure_and_filter_unlocks():
    trades = [
        {"code": "000001", "entry_date": "20240102", "dedt_yoy": 80.0, "delta": 10.0},
        {"code": "000002", "entry_date": "20240102", "dedt_yoy": 60.0, "delta": 8.0},
    ]
    by_code = {
        "000001": backtest.pd.DataFrame({
            "trade_date": ["20240102", "20240103"],
            "close_adj": [10.0, 9.0],
        }),
        "000002": backtest.pd.DataFrame({
            "trade_date": ["20240102", "20240103"],
            "close_adj": [20.0, 22.0],
        }),
    }
    unlocks = {"000001": ["2024-02-15"]}

    base = backtest.build_rolling_portfolio_curve(trades, by_code, ["20240102", "20240103"], topn=2)
    filtered = backtest.build_rolling_portfolio_curve(
        trades, by_code, ["20240102", "20240103"], topn=2, unlocks_by_code=unlocks, avoid_unlock=True
    )

    assert base["nav"][-1] == 1.0
    assert filtered["nav"][-1] == 1.1
    assert filtered["top_codes"][0] == ["000002"]


def test_rolling_portfolio_can_exclude_advisor_and_hot_pre_runup():
    trades = [
        {"code": "000001", "ann_date": "20240104", "entry_date": "20240105", "dedt_yoy": 100.0, "delta": 10.0},
        {"code": "000002", "ann_date": "20240104", "entry_date": "20240105", "dedt_yoy": 90.0, "delta": 8.0},
        {"code": "000003", "ann_date": "20240104", "entry_date": "20240105", "dedt_yoy": 80.0, "delta": 6.0},
    ]
    by_code = {
        "000001": backtest.pd.DataFrame({"trade_date": ["20240101", "20240104", "20240105"], "close_adj": [10.0, 11.0, 11.0]}),
        "000002": backtest.pd.DataFrame({"trade_date": ["20240101", "20240104", "20240105"], "close_adj": [10.0, 16.0, 16.0]}),
        "000003": backtest.pd.DataFrame({"trade_date": ["20240101", "20240104", "20240105"], "close_adj": [10.0, 10.5, 10.5]}),
    }

    curve = backtest.build_rolling_portfolio_curve(
        trades,
        by_code,
        ["20240104", "20240105"],
        topn=2,
        exclude_codes={"000001"},
        max_pre_runup_20=30.0,
    )

    assert curve["top_codes"][-1] == ["000003"]


def test_rolling_portfolio_can_skip_large_entry_gap():
    trades = [
        {"code": "000001", "entry_date": "20240105", "dedt_yoy": 100.0, "delta": 10.0, "entry_gap_pct": 8.0},
        {"code": "000002", "entry_date": "20240105", "dedt_yoy": 90.0, "delta": 8.0, "entry_gap_pct": 2.0},
    ]
    by_code = {
        "000001": backtest.pd.DataFrame({"trade_date": ["20240105"], "close_adj": [11.0]}),
        "000002": backtest.pd.DataFrame({"trade_date": ["20240105"], "close_adj": [10.5]}),
    }

    curve = backtest.build_rolling_portfolio_curve(
        trades, by_code, ["20240105"], topn=2, max_entry_gap_pct=5.0
    )

    assert curve["top_codes"][-1] == ["000002"]


def test_rolling_portfolio_can_filter_growth_and_delta_strength():
    trades = [
        {"code": "000001", "entry_date": "20240105", "dedt_yoy": 120.0, "delta": 5.0},
        {"code": "000002", "entry_date": "20240105", "dedt_yoy": 80.0, "delta": 40.0},
        {"code": "000003", "entry_date": "20240105", "dedt_yoy": 140.0, "delta": 60.0},
    ]
    by_code = {
        "000001": backtest.pd.DataFrame({"trade_date": ["20240105"], "close_adj": [11.0]}),
        "000002": backtest.pd.DataFrame({"trade_date": ["20240105"], "close_adj": [12.0]}),
        "000003": backtest.pd.DataFrame({"trade_date": ["20240105"], "close_adj": [13.0]}),
    }

    curve = backtest.build_rolling_portfolio_curve(
        trades, by_code, ["20240105"], topn=3, min_signal_growth=100.0, min_signal_delta=20.0
    )

    assert curve["top_codes"][-1] == ["000003"]
    assert curve["min_signal_growth"] == 100.0
    assert curve["min_signal_delta"] == 20.0


def test_choose_rolling_recommended_prefers_better_sharpe_without_large_drawdown_slip():
    variants = {
        "advisor_gap_cool": {"summary": {"final_nav": 2.3, "mdd_pct": -30.8, "sharpe": 0.99, "win_rate_pct": 53.9}},
        "strong_delta": {"summary": {"final_nav": 2.6, "mdd_pct": -30.9, "sharpe": 1.13, "win_rate_pct": 54.4}},
        "too_deep": {"summary": {"final_nav": 3.0, "mdd_pct": -45.0, "sharpe": 1.5, "win_rate_pct": 55.0}},
    }

    assert backtest.choose_rolling_recommended(variants) == "strong_delta"


if __name__ == "__main__":
    test_cninfo_announcement_time_is_kept_as_datetime()
    test_earnings_title_filter_keeps_real_reports_and_skips_abstracts()
    test_code_market_maps_bse_920_codes_to_bj_column()
    test_collect_for_code_uses_name_fallback_for_bse_920_legacy_codes()
    test_entry_rule_uses_intraday_close_and_after_close_next_open()
    test_entry_lag_uses_nth_trading_day_after_announcement_open()
    test_build_trades_can_enter_on_third_trading_day_after_announcement()
    test_entry_lag_backtest_can_filter_interim_period_suffix()
    test_lookup_announcement_time_allows_nearby_cninfo_date()
    test_event_curve_uses_all_trades_grouped_by_entry_date()
    test_rolling_portfolio_curve_rebalances_as_new_signals_arrive()
    test_rolling_portfolio_variants_can_reduce_exposure_and_filter_unlocks()
    test_rolling_portfolio_can_exclude_advisor_and_hot_pre_runup()
    test_rolling_portfolio_can_skip_large_entry_gap()
    test_rolling_portfolio_can_filter_growth_and_delta_strength()
    test_choose_rolling_recommended_prefers_better_sharpe_without_large_drawdown_slip()
    print("earnings announcement time tests ok")
