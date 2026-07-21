from __future__ import annotations

import inspect
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import app
from scripts import update_daily


class EquityOnlyPro:
    def daily(self, trade_date):
        return pd.DataFrame([
            {
                "ts_code": "300308.SZ", "trade_date": trade_date,
                "open": 1210.0, "high": 1218.0, "low": 1093.98,
                "close": 1093.98, "vol": 423262.42, "pct_chg": -8.4459,
            }
        ])

    def adj_factor(self, trade_date):
        return pd.DataFrame([
            {"ts_code": "300308.SZ", "trade_date": trade_date, "adj_factor": 6.6039}
        ])


def test_stock_daily_fetch_does_not_depend_on_index_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "PARQUET_DIR", tmp_path)
    monkeypatch.setattr(app, "_tushare_api", lambda: EquityOnlyPro())

    assert app._fetch_one_day_parquet("2026-07-10") is True
    stored = pd.read_parquet(tmp_path / "20260710.parquet")
    assert stored["ts_code"].tolist() == ["300308.SZ"]
    assert stored["adj_factor"].tolist() == [6.6039]


def test_sparse_benchmark_gap_never_uses_equity_parquet_rebuild(tmp_path, monkeypatch):
    class Weekend(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 11, 12, 0)

    (tmp_path / "20260710.parquet").touch()
    monkeypatch.setattr(app, "datetime", Weekend)
    monkeypatch.setattr(app, "PARQUET_DIR", tmp_path)
    monkeypatch.setattr(app, "_read_calendar", lambda: ["2026-07-09", "2026-07-10"])
    monkeypatch.setattr(app, "_read_bin", lambda code, field: (0, np.array([4000.0], dtype=np.float32)))
    monkeypatch.setattr(app, "_is_trading_day", lambda value: False)
    monkeypatch.setattr(app, "_append_dates_to_stock_bin", lambda code, dates: -1)

    with patch.object(app, "_full_rebuild_one_stock") as rebuild:
        result = app._ensure_freshness_inner("sh000300")

    assert result["status"] == "benchmark_refresh_required"
    rebuild.assert_not_called()


def test_refresh_benchmark_indices_builds_full_independent_bins(tmp_path, monkeypatch):
    calendars = tmp_path / "calendars"
    instruments = tmp_path / "instruments"
    features = tmp_path / "features"
    calendars.mkdir()
    instruments.mkdir()
    dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    (calendars / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
    (instruments / "all.txt").write_text("sz300308\t2026-01-02\t2026-01-08\n", encoding="utf-8")
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)

    class IndexPro:
        def __init__(self):
            self.calls = []

        def index_daily(self, ts_code, start_date, end_date):
            self.calls.append((ts_code, start_date, end_date))
            rows = []
            for index, date_iso in enumerate(dates):
                ymd = date_iso.replace("-", "")
                if start_date <= ymd <= end_date:
                    base = 4000.0 + index
                    rows.append({
                        "ts_code": ts_code, "trade_date": ymd,
                        "open": base, "close": base + 1,
                        "high": base + 2, "low": base - 1,
                        "vol": 1000.0 + index, "pct_chg": 0.1,
                    })
            return pd.DataFrame(rows[::-1])

    pro = IndexPro()
    update_daily.refresh_benchmark_index_bins(pro, end="20260108", sleep=0)

    assert len(pro.calls) == 12
    for ts_code in update_daily.BENCHMARK_INDEX_TS_CODES:
        code = update_daily._ts_code_to_qlib(ts_code)
        close = np.fromfile(features / code / "close.day.bin", dtype="<f4")
        adj = np.fromfile(features / code / "adj.day.bin", dtype="<f4")
        assert int(close[0]) == 0
        assert np.allclose(close[1:], [4001, 4002, 4003, 4004, 4005])
        assert np.allclose(adj[1:], 1.0)

    lines = (instruments / "all.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("sz300308\t") for line in lines)
    assert all(any(line.startswith(f"{code}\t") for line in lines) for code in app.BENCHMARK_INDEX_CODES)


@pytest.mark.parametrize(
    ("dates", "available_dates", "error"),
    [
        (
            ["2007-12-28", "2010-01-04", "2016-01-04", "2026-07-13"],
            {"2007-12-28", "2016-01-04", "2026-07-13"},
            "calendar gaps",
        ),
        (
            ["2023-12-29", "2026-07-13"],
            {"2023-12-29"},
            "tail is stale",
        ),
    ],
)
def test_benchmark_empty_chunk_fails_closed(
    tmp_path, monkeypatch, dates, available_dates, error,
):
    calendars = tmp_path / "calendars"
    instruments = tmp_path / "instruments"
    features = tmp_path / "features"
    calendars.mkdir()
    instruments.mkdir()
    old_calendar = "2026-07-10\n"
    old_instruments = "sz300308\t2020-01-02\t2026-07-10\n"
    (calendars / "day.txt").write_text(old_calendar, encoding="utf-8")
    (instruments / "all.txt").write_text(old_instruments, encoding="utf-8")
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)

    class GapPro:
        def index_daily(self, ts_code, start_date, end_date):
            rows = []
            for date_iso in sorted(available_dates):
                ymd = date_iso.replace("-", "")
                if start_date <= ymd <= end_date:
                    rows.append({
                        "ts_code": ts_code, "trade_date": ymd,
                        "open": 10.0, "close": 10.5, "high": 11.0, "low": 9.5,
                        "vol": 100.0, "pct_chg": 1.0,
                    })
            return pd.DataFrame(rows)

    with pytest.raises(RuntimeError, match=error):
        update_daily.refresh_benchmark_index_bins(
            GapPro(), end="20260713", sleep=0, calendars=dates,
        )

    assert not features.exists()
    assert (calendars / "day.txt").read_text(encoding="utf-8") == old_calendar
    assert (instruments / "all.txt").read_text(encoding="utf-8") == old_instruments


def test_invalid_benchmark_ohlc_prevents_all_feature_and_metadata_writes(tmp_path, monkeypatch):
    calendars = tmp_path / "calendars"
    instruments = tmp_path / "instruments"
    features = tmp_path / "features"
    calendars.mkdir()
    instruments.mkdir()
    dates = ["2026-07-10", "2026-07-13"]
    old_calendar = "2026-07-10\n"
    old_instruments = "sz300308\t2020-01-02\t2026-07-10\n"
    (calendars / "day.txt").write_text(old_calendar, encoding="utf-8")
    (instruments / "all.txt").write_text(old_instruments, encoding="utf-8")
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)
    invalid_code = update_daily.BENCHMARK_INDEX_TS_CODES[1]

    class InvalidOhlcPro:
        def index_daily(self, ts_code, start_date, end_date):
            rows = []
            for index, date_iso in enumerate(dates):
                ymd = date_iso.replace("-", "")
                if start_date <= ymd <= end_date:
                    close = 10.5 + index
                    rows.append({
                        "ts_code": ts_code, "trade_date": ymd,
                        "open": 10.0 + index, "close": close,
                        "high": close - 1.0 if ts_code == invalid_code else close + 1.0,
                        "low": 9.5 + index, "vol": 100.0, "pct_chg": 1.0,
                    })
            return pd.DataFrame(rows)

    with pytest.raises(RuntimeError, match="invalid OHLC"):
        update_daily.refresh_benchmark_index_bins(
            InvalidOhlcPro(), end="20260713", sleep=0, calendars=dates,
        )

    assert not features.exists()
    assert (calendars / "day.txt").read_text(encoding="utf-8") == old_calendar
    assert (instruments / "all.txt").read_text(encoding="utf-8") == old_instruments


def test_daily_download_failure_is_not_reported_as_success_and_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(update_daily, "PARQUET_DIR", tmp_path)

    class RecentPro:
        def __init__(self, fail_date=""):
            self.fail_date = fail_date

        def trade_cal(self, **_kwargs):
            return pd.DataFrame({"cal_date": ["20260709", "20260710"]})

        def daily(self, trade_date):
            if trade_date == self.fail_date:
                raise RuntimeError("temporary upstream failure")
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "trade_date": trade_date,
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "vol": 100.0, "pct_chg": 1.0,
            }])

        def adj_factor(self, trade_date):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "trade_date": trade_date, "adj_factor": 2.0,
            }])

    with pytest.raises(RuntimeError, match="daily download incomplete"):
        update_daily.download_recent(
            RecentPro(fail_date="20260709"), end="20260710", lookback_days=5, sleep=0,
        )

    assert not (tmp_path / "20260709.parquet").exists()
    assert (tmp_path / "20260710.parquet").exists()

    result = update_daily.download_recent(
        RecentPro(), end="20260710", lookback_days=5, sleep=0,
    )
    assert result == {"dates": ["20260709", "20260710"], "downloaded": 1, "skipped": 1}
    assert (tmp_path / "20260709.parquet").exists()


def test_daily_download_rejects_incomplete_adjustment_data(tmp_path, monkeypatch):
    monkeypatch.setattr(update_daily, "PARQUET_DIR", tmp_path)

    class MissingAdjustmentPro:
        def trade_cal(self, **_kwargs):
            return pd.DataFrame({"cal_date": ["20260710"]})

        def daily(self, trade_date):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "trade_date": trade_date,
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "vol": 100.0, "pct_chg": 1.0,
            }])

        def adj_factor(self, trade_date):
            return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

    with pytest.raises(RuntimeError, match="adj_factor returned no rows"):
        update_daily.download_recent(
            MissingAdjustmentPro(), end="20260710", lookback_days=5, sleep=0,
        )
    assert not (tmp_path / "20260710.parquet").exists()


def test_existing_parquet_with_nonpositive_adjustment_is_invalid(tmp_path):
    path = tmp_path / "20260710.parquet"
    pd.DataFrame([{
        "ts_code": "300308.SZ", "trade_date": "20260710", "adj_factor": 0.0,
    }]).to_parquet(path, index=False)

    assert update_daily._valid_daily_parquet(path, "20260710") is False


def test_daily_download_catches_up_from_last_file_after_long_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(update_daily, "PARQUET_DIR", tmp_path)
    pd.DataFrame([{
        "ts_code": "300308.SZ", "trade_date": "20260105", "adj_factor": 2.0,
    }]).to_parquet(tmp_path / "20260105.parquet", index=False)
    calls = {}

    class CatchupPro:
        def trade_cal(self, **kwargs):
            calls.update(kwargs)
            return pd.DataFrame({"cal_date": ["20260106", "20260710"]})

        def daily(self, trade_date):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "trade_date": trade_date,
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "vol": 100.0, "pct_chg": 1.0,
            }])

        def adj_factor(self, trade_date):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "trade_date": trade_date, "adj_factor": 2.0,
            }])

    update_daily.download_recent(CatchupPro(), end="20260710", lookback_days=45, sleep=0)

    assert calls["start_date"] <= "20260105"
    assert (tmp_path / "20260106.parquet").exists()
    assert (tmp_path / "20260710.parquet").exists()


def test_scheduled_daily_update_propagates_critical_update_failure(tmp_path, monkeypatch):
    def fail_update():
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(update_daily, "main", fail_update)
    monkeypatch.setattr(app, "DAILY_UPDATE_STATUS_PATH", tmp_path / "daily_update_status.json")
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        app.run_daily_update()


def test_qlib_calendar_is_published_after_atomic_bin_files(tmp_path, monkeypatch):
    parquet = tmp_path / "parquet"
    calendars = tmp_path / "qlib" / "calendars"
    instruments = tmp_path / "qlib" / "instruments"
    features = tmp_path / "qlib" / "features"
    parquet.mkdir()
    pd.DataFrame([
        {
            "ts_code": "300308.SZ", "trade_date": "20260709",
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "vol": 100.0, "pct_chg": 1.0, "adj_factor": 2.0,
        },
        {
            "ts_code": "300308.SZ", "trade_date": "20260710",
            "open": 10.5, "high": 11.5, "low": 10.0, "close": 11.0,
            "vol": 120.0, "pct_chg": 2.0, "adj_factor": 2.0,
        },
    ]).to_parquet(parquet / "20260710.parquet", index=False)
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)

    events = []
    original_bytes = update_daily._atomic_write_bytes
    original_text = update_daily._atomic_write_text

    def record_bytes(path, payload):
        events.append(("bytes", Path(path)))
        original_bytes(path, payload)

    def record_text(path, payload):
        events.append(("text", Path(path)))
        original_text(path, payload)

    monkeypatch.setattr(update_daily, "_atomic_write_bytes", record_bytes)
    monkeypatch.setattr(update_daily, "_atomic_write_text", record_text)
    update_daily.build_qlib_bin()

    calendar_event = events.index(("text", calendars / "day.txt"))
    bin_events = [index for index, event in enumerate(events) if event[0] == "bytes"]
    assert bin_events
    assert calendar_event > max(bin_events)
    assert (calendars / "day.txt").read_text(encoding="utf-8").splitlines() == [
        "2026-07-09", "2026-07-10",
    ]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("adj_factor", np.nan, "invalid adj_factor"),
        ("adj_factor", np.inf, "invalid adj_factor"),
        ("adj_factor", 0.0, "invalid adj_factor"),
        ("open", np.nan, "invalid OHLC"),
        ("high", 0.0, "invalid OHLC"),
        ("vol", np.inf, "invalid vol"),
    ],
)
def test_full_rebuild_validates_all_history_before_any_write(
    tmp_path, monkeypatch, field, value, error,
):
    parquet = tmp_path / "parquet"
    calendars = tmp_path / "qlib" / "calendars"
    instruments = tmp_path / "qlib" / "instruments"
    features = tmp_path / "qlib" / "features"
    parquet.mkdir()
    good = {
        "ts_code": "300308.SZ", "trade_date": "20260710",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "vol": 100.0, "pct_chg": 1.0, "adj_factor": 2.0,
    }
    bad = {**good, "trade_date": "20260709", field: value}
    pd.DataFrame([bad]).to_parquet(parquet / "20260709.parquet", index=False)
    pd.DataFrame([good]).to_parquet(parquet / "20260710.parquet", index=False)
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)
    writes = []
    monkeypatch.setattr(update_daily, "_atomic_write_bytes", lambda *args: writes.append(args))
    monkeypatch.setattr(update_daily, "_atomic_write_text", lambda *args: writes.append(args))

    with pytest.raises(RuntimeError, match=error):
        update_daily.build_qlib_bin()

    assert writes == []
    assert not features.exists()


def _configure_rebuild_paths(tmp_path, monkeypatch):
    parquet = tmp_path / "parquet"
    calendars = tmp_path / "qlib" / "calendars"
    instruments = tmp_path / "qlib" / "instruments"
    features = tmp_path / "qlib" / "features"
    parquet.mkdir()
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)
    return parquet, features


def _historical_row(trade_date, adj_factor, ts_code="600018.SH"):
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "vol": 100.0,
        "pct_chg": 1.0,
        "adj_factor": adj_factor,
    }


def _undefined_initial_return_row(
    trade_date="20080221", adj_factor=1.0, ts_code="920017.BJ",
):
    return {
        **_historical_row(trade_date, adj_factor, ts_code),
        "pre_close": np.nan,
        "change": np.nan,
        "pct_chg": np.nan,
    }


def test_historical_initial_undefined_pct_chg_is_preserved_and_suspension_is_zero(
    tmp_path, monkeypatch,
):
    parquet, features = _configure_rebuild_paths(tmp_path, monkeypatch)
    first = _undefined_initial_return_row("20260709")
    last = {
        **_historical_row("20260713", 1.0, "920017.BJ"),
        "pre_close": 10.5,
        "change": 0.2,
        "pct_chg": 2.0,
    }
    market_calendar_row = {
        **_historical_row("20260710", 1.0, "000001.SZ"),
        "pre_close": 10.4,
        "change": 0.1,
    }
    for row in (first, market_calendar_row, last):
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}-{row['ts_code'][:6]}.parquet",
            index=False,
        )

    update_daily.build_qlib_bin()

    change = np.fromfile(features / "bj920017" / "change.day.bin", dtype="<f4")
    assert change[0] == 0.0
    assert np.isnan(change[1])
    assert change[2] == 0.0
    assert change[3] == pytest.approx(0.02)


def test_daily_parquet_does_not_infer_legacy_boundary_from_one_day_file(tmp_path):
    path = tmp_path / "20080221.parquet"
    pd.DataFrame([_undefined_initial_return_row()]).to_parquet(path, index=False)

    assert update_daily._valid_daily_parquet(path, "20080221") is False


def test_historical_midstream_undefined_pct_chg_still_fails_before_bin_write(
    tmp_path, monkeypatch,
):
    parquet, features = _configure_rebuild_paths(tmp_path, monkeypatch)
    first = {
        **_historical_row("20260709", 1.0, "920017.BJ"),
        "pre_close": 5.9,
        "change": 0.1,
    }
    broken = _undefined_initial_return_row("20260710")
    pd.DataFrame([first]).to_parquet(parquet / "20260709.parquet", index=False)
    pd.DataFrame([broken]).to_parquet(parquet / "20260710.parquet", index=False)

    with pytest.raises(RuntimeError, match="invalid pct_chg"):
        update_daily.build_qlib_bin()

    assert not features.exists()


@pytest.mark.parametrize(
    ("pct_chg", "pre_close", "raw_change"),
    [
        (np.inf, np.nan, np.nan),
        ("not-a-number", np.nan, np.nan),
        (np.nan, 6.0, np.nan),
        (np.nan, np.nan, 0.0),
    ],
)
def test_historical_boundary_rejects_non_missing_or_conflicting_return_provenance(
    pct_chg, pre_close, raw_change,
):
    row = _undefined_initial_return_row()
    row.update(pct_chg=pct_chg, pre_close=pre_close, change=raw_change)

    with pytest.raises(RuntimeError, match="invalid pct_chg"):
        update_daily._validate_equity_history(
            pd.DataFrame([row]),
            source="test history",
            allow_legacy_initial_pct_chg=True,
        )


def test_historical_boundary_rejects_duplicate_code_date_key():
    missing = _undefined_initial_return_row()
    duplicate = {
        **_historical_row("20080221", 1.0, "920017.BJ"),
        "pre_close": 5.9,
        "change": 0.1,
    }

    with pytest.raises(RuntimeError, match="invalid pct_chg"):
        update_daily._validate_equity_history(
            pd.DataFrame([missing, duplicate]),
            source="test history",
            allow_legacy_initial_pct_chg=True,
        )


def test_equity_history_requires_pct_chg_column():
    row = _historical_row("20260710", 1.0)
    row.pop("pct_chg")

    with pytest.raises(RuntimeError, match="missing columns.*pct_chg"):
        update_daily._validate_equity_history(pd.DataFrame([row]))


def test_adj_factor_staging_accepts_only_full_history_certified_initial_return(
    tmp_path, monkeypatch,
):
    parquet, _ = _configure_rebuild_paths(tmp_path, monkeypatch)
    first = _undefined_initial_return_row("20080221", np.nan)
    second = {
        **_historical_row("20080222", 1.25, "920017.BJ"),
        "pre_close": 6.0,
        "change": 0.2,
    }
    pd.DataFrame([first]).to_parquet(parquet / "20080221.parquet", index=False)
    pd.DataFrame([second]).to_parquet(parquet / "20080222.parquet", index=False)

    class OriginalSource:
        def stock_basic(self, **_kwargs):
            return pd.DataFrame([{
                "ts_code": "920017.BJ",
                "name": "legacy listing",
                "list_date": "20080221",
            }])

        def adj_factor(self, **_kwargs):
            return pd.DataFrame([
                {"ts_code": "920017.BJ", "trade_date": "20080221", "adj_factor": 1.0},
                {"ts_code": "920017.BJ", "trade_date": "20080222", "adj_factor": 1.25},
            ])

    update_daily.build_qlib_bin(adj_factor_source=OriginalSource())

    staged_result = pd.read_parquet(parquet / "20080221.parquet").iloc[0]
    assert staged_result["adj_factor"] == pytest.approx(1.0)
    assert pd.isna(staged_result["pct_chg"])


def test_historical_adj_factor_is_repaired_from_exact_original_source(
    tmp_path, monkeypatch,
):
    parquet, _ = _configure_rebuild_paths(tmp_path, monkeypatch)
    rows = [
        _historical_row("20000718", 2.0),
        _historical_row("20000719", np.nan),
        _historical_row("20000720", 2.25),
    ]
    for row in rows:
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}.parquet", index=False,
        )

    class OriginalSource:
        def adj_factor(self, ts_code, start_date, end_date):
            assert ts_code == "600018.SH"
            assert (start_date, end_date) == ("20000718", "20000720")
            return pd.DataFrame([
                {"ts_code": ts_code, "trade_date": "20000718", "adj_factor": 2.0},
                {"ts_code": ts_code, "trade_date": "20000719", "adj_factor": 2.25},
                {"ts_code": ts_code, "trade_date": "20000720", "adj_factor": 2.25},
            ])

    update_daily.build_qlib_bin(adj_factor_source=OriginalSource())

    repaired = pd.read_parquet(parquet / "20000719.parquet")
    assert repaired.loc[0, "adj_factor"] == pytest.approx(2.25)
    status = json.loads(
        (tmp_path / "adj_factor_repair_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "repaired"
    assert status["original_source_rows"] == 1
    assert status["bounded_previous_rows"] == 0
    assert status["parquet_files_rewritten"] == 1


def test_historical_adj_factor_uses_previous_only_between_equal_valid_anchors(
    tmp_path, monkeypatch,
):
    parquet, _ = _configure_rebuild_paths(tmp_path, monkeypatch)
    rows = [
        _historical_row("20000718", 2.0),
        _historical_row("20000719", np.nan),
        _historical_row("20000720", 2.0),
    ]
    for row in rows:
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}.parquet", index=False,
        )

    class AnchorOnlySource:
        def adj_factor(self, ts_code, start_date, end_date):
            return pd.DataFrame([
                {"ts_code": ts_code, "trade_date": start_date, "adj_factor": 2.0},
                {"ts_code": ts_code, "trade_date": end_date, "adj_factor": 2.0},
            ])

    update_daily.build_qlib_bin(adj_factor_source=AnchorOnlySource())

    repaired = pd.read_parquet(parquet / "20000719.parquet")
    assert repaired.loc[0, "adj_factor"] == pytest.approx(2.0)
    status = json.loads(
        (tmp_path / "adj_factor_repair_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "repaired"
    assert status["original_source_rows"] == 0
    assert status["bounded_previous_rows"] == 1


def test_unbounded_adj_factor_gap_fails_closed_without_defaulting_to_one(
    tmp_path, monkeypatch,
):
    parquet, features = _configure_rebuild_paths(tmp_path, monkeypatch)
    first = _historical_row("20000718", 2.0)
    tail = _historical_row("20000719", np.nan)
    pd.DataFrame([first]).to_parquet(parquet / "20000718.parquet", index=False)
    pd.DataFrame([tail]).to_parquet(parquet / "20000719.parquet", index=False)

    class MissingTailSource:
        def adj_factor(self, ts_code, start_date, end_date):
            return pd.DataFrame([
                {"ts_code": ts_code, "trade_date": start_date, "adj_factor": 2.0},
            ])

    with pytest.raises(RuntimeError, match="adj_factor repair unresolved"):
        update_daily.build_qlib_bin(adj_factor_source=MissingTailSource())

    still_invalid = pd.read_parquet(parquet / "20000719.parquet")
    assert pd.isna(still_invalid.loc[0, "adj_factor"])
    assert not features.exists()
    status = json.loads(
        (tmp_path / "adj_factor_repair_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "failed"
    assert status["unresolved_rows"] == 1
    assert status["original_source_rows"] == 0
    assert status["bounded_previous_rows"] == 0


def test_pre_listing_predecessor_rows_are_quarantined_by_stock_basic_boundary(
    tmp_path, monkeypatch,
):
    parquet, _ = _configure_rebuild_paths(tmp_path, monkeypatch)
    rows = [
        _historical_row("20000719", np.nan),
        _historical_row("20060925", np.nan),
        _historical_row("20061026", 2.0),
        _historical_row("20061027", 2.0),
    ]
    for row in rows:
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}.parquet", index=False,
        )

    class IdentityBoundarySource:
        def stock_basic(self, ts_code, fields):
            assert ts_code == "600018.SH"
            assert fields == "ts_code,name,list_date"
            return pd.DataFrame([{
                "ts_code": ts_code,
                "name": "上港集团",
                "list_date": "20061026",
            }])

        def adj_factor(self, **_kwargs):
            pytest.fail("pre-listing predecessor rows must not be repaired as current identity")

    source = IdentityBoundarySource()
    update_daily.build_qlib_bin(adj_factor_source=source)
    # Re-running applies the same evidence boundary without mutating source rows.
    update_daily.build_qlib_bin(adj_factor_source=source)

    assert pd.isna(pd.read_parquet(parquet / "20000719.parquet").loc[0, "adj_factor"])
    assert pd.isna(pd.read_parquet(parquet / "20060925.parquet").loc[0, "adj_factor"])
    instruments = (tmp_path / "qlib" / "instruments" / "all.txt").read_text(
        encoding="utf-8"
    )
    assert instruments.strip() == "sh600018\t2006-10-26\t2006-10-27"
    status = json.loads(
        (tmp_path / "adj_factor_repair_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "identity_quarantined"
    assert status["stage"] == "complete"
    assert status["invalid_rows"] == 2
    assert status["repaired_rows"] == 0
    assert status["unresolved_rows"] == 0
    assert status["identity_quarantined_rows"] == 2
    assert status["identity_quarantined_invalid_rows"] == 2
    assert status["identity_quarantined_codes"] == 1
    assert status["identity_exclusion_persistence"] == "build_filter_source_parquet_unchanged"
    assert status["identity_boundaries"] == [{
        "excluded_invalid_rows": 2,
        "excluded_rows": 2,
        "list_date": "20061026",
        "name": "上港集团",
        "source": "tushare.stock_basic",
        "ts_code": "600018.SH",
    }]


def test_post_listing_unresolved_gap_is_not_hidden_by_identity_quarantine(
    tmp_path, monkeypatch,
):
    parquet, features = _configure_rebuild_paths(tmp_path, monkeypatch)
    rows = [
        _historical_row("20260717", np.nan, "000001.SZ"),
        _historical_row("20260718", 2.0, "000001.SZ"),
    ]
    for row in rows:
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}.parquet", index=False,
        )

    class UnresolvedCurrentIdentitySource:
        def stock_basic(self, ts_code, fields):
            return pd.DataFrame([{
                "ts_code": ts_code,
                "name": "平安银行",
                "list_date": "19910403",
            }])

        def adj_factor(self, **_kwargs):
            return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

    with pytest.raises(RuntimeError, match="adj_factor repair unresolved"):
        update_daily.build_qlib_bin(
            adj_factor_source=UnresolvedCurrentIdentitySource(),
        )

    assert not features.exists()
    assert pd.isna(pd.read_parquet(parquet / "20260717.parquet").loc[0, "adj_factor"])
    status = json.loads(
        (tmp_path / "adj_factor_repair_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "failed"
    assert status["unresolved_rows"] == 1
    assert status["identity_quarantined_rows"] == 0
    assert status["identity_quarantined_invalid_rows"] == 0


def test_identity_status_remains_staged_when_bin_build_fails(tmp_path, monkeypatch):
    parquet, _ = _configure_rebuild_paths(tmp_path, monkeypatch)
    rows = [
        _historical_row("20060925", np.nan),
        _historical_row("20061026", 2.0),
    ]
    for row in rows:
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}.parquet", index=False,
        )

    class IdentityBoundarySource:
        def stock_basic(self, ts_code, fields):
            return pd.DataFrame([{
                "ts_code": ts_code,
                "name": "上港集团",
                "list_date": "20061026",
            }])

        def adj_factor(self, **_kwargs):
            pytest.fail("quarantined rows must not reach adj_factor repair")

    monkeypatch.setattr(
        update_daily,
        "_atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bin write failed")),
    )
    with pytest.raises(RuntimeError, match="bin write failed"):
        update_daily.build_qlib_bin(adj_factor_source=IdentityBoundarySource())

    status = json.loads(
        (tmp_path / "adj_factor_repair_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "staged"
    assert status["stage"] == "history_validated_and_repairs_persisted"
    assert status["intended_outcome"] == "identity_quarantined"


def test_full_rebuild_repairs_legacy_ohlc_envelope(tmp_path, monkeypatch):
    parquet = tmp_path / "parquet"
    calendars = tmp_path / "qlib" / "calendars"
    instruments = tmp_path / "qlib" / "instruments"
    features = tmp_path / "qlib" / "features"
    parquet.mkdir()
    pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": "19920803",
        "open": 10.0, "high": 9.8, "low": 9.5, "close": 9.6,
        "vol": 100.0, "pct_chg": -4.0, "adj_factor": 2.0,
    }]).to_parquet(parquet / "19920803.parquet", index=False)
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)

    update_daily.build_qlib_bin()

    high = np.fromfile(features / "sz000001" / "high.day.bin", dtype="<f4")
    low = np.fromfile(features / "sz000001" / "low.day.bin", dtype="<f4")
    assert high.tolist() == pytest.approx([0.0, 10.0])
    assert low.tolist() == pytest.approx([0.0, 9.5])


def test_full_rebuild_groups_rows_once_instead_of_scanning_market_per_stock():
    source = inspect.getsource(update_daily.build_qlib_bin)

    assert 'df.groupby("code"' in source
    assert 'df[df["code"] == code]' not in source


def test_main_keeps_old_calendar_when_benchmark_refresh_fails(tmp_path, monkeypatch):
    qlib_root = tmp_path / "qlib"
    parquet = tmp_path / "parquet"
    calendars = qlib_root / "calendars"
    instruments = qlib_root / "instruments"
    features = qlib_root / "features"
    calendars.mkdir(parents=True)
    instruments.mkdir(parents=True)
    parquet.mkdir()
    calendar = calendars / "day.txt"
    calendar.write_text("2026-07-08\n", encoding="utf-8")
    instrument_file = instruments / "all.txt"
    instrument_file.write_text("sz300308\t2026-07-08\t2026-07-08\n", encoding="utf-8")
    pd.DataFrame([
        {
            "ts_code": "300308.SZ", "trade_date": "20260708",
            "open": 9.5, "high": 10.5, "low": 9.0, "close": 10.0,
            "vol": 90.0, "pct_chg": 0.5, "adj_factor": 2.0,
        },
        {
            "ts_code": "300308.SZ", "trade_date": "20260709",
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "vol": 100.0, "pct_chg": 1.0, "adj_factor": 2.0,
        },
    ]).to_parquet(parquet / "20260709.parquet", index=False)
    monkeypatch.setattr(update_daily, "QLIB_DATA_PATH", qlib_root)
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)
    monkeypatch.setattr(update_daily, "_pro", lambda: object())
    monkeypatch.setattr(update_daily, "download_recent", lambda _pro: None)
    seen = {}

    def fail_benchmarks(_pro, **kwargs):
        seen["calendars"] = kwargs["calendars"]
        raise RuntimeError("benchmark unavailable")

    monkeypatch.setattr(update_daily, "refresh_benchmark_index_bins", fail_benchmarks)

    with pytest.raises(RuntimeError, match="benchmark unavailable"):
        update_daily.main()

    assert seen["calendars"] == ["2026-07-08", "2026-07-09"]
    assert calendar.read_text(encoding="utf-8") == "2026-07-08\n"
    assert instrument_file.read_text(encoding="utf-8") == "sz300308\t2026-07-08\t2026-07-08\n"
    assert not features.exists()


def _configure_staged_main(tmp_path, monkeypatch):
    qlib_root = tmp_path / "qlib"
    parquet = tmp_path / "parquet"
    calendars = qlib_root / "calendars"
    instruments = qlib_root / "instruments"
    features = qlib_root / "features"
    calendars.mkdir(parents=True)
    instruments.mkdir(parents=True)
    (features / "sz300308").mkdir(parents=True)
    parquet.mkdir()
    (calendars / "day.txt").write_text("2026-07-08\n", encoding="utf-8")
    (instruments / "all.txt").write_text(
        "sz300308\t2026-07-08\t2026-07-08\n", encoding="utf-8",
    )
    (features / "sz300308" / "close.day.bin").write_bytes(b"old-feature")
    monkeypatch.setattr(update_daily, "QLIB_DATA_PATH", qlib_root)
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)
    monkeypatch.setattr(update_daily, "_pro", lambda: object())
    monkeypatch.setattr(update_daily, "download_recent", lambda _pro: None)

    def staged_equities(_publish_calendar=False, **kwargs):
        assert kwargs["published_calendars_dir"] == calendars
        target = Path(kwargs["features_dir"]) / "sz300308" / "close.day.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"new-feature")
        return ["2026-07-08", "2026-07-09"], [
            "sz300308\t2026-07-08\t2026-07-09",
        ]

    def staged_benchmarks(_pro, **kwargs):
        target = Path(kwargs["features_dir"]) / "sh000300" / "close.day.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"new-benchmark")
        return [
            *kwargs["base_instruments"],
            "sh000300\t2026-07-08\t2026-07-09",
        ]

    monkeypatch.setattr(update_daily, "build_qlib_bin", staged_equities)
    monkeypatch.setattr(update_daily, "refresh_benchmark_index_bins", staged_benchmarks)
    return qlib_root, calendars, instruments, features


def test_main_commits_complete_staged_feature_tree_before_metadata(tmp_path, monkeypatch):
    qlib_root, calendars, instruments, features = _configure_staged_main(
        tmp_path, monkeypatch,
    )

    update_daily.main()

    assert (features / "sz300308" / "close.day.bin").read_bytes() == b"new-feature"
    assert (features / "sh000300" / "close.day.bin").read_bytes() == b"new-benchmark"
    assert (instruments / "all.txt").read_text(encoding="utf-8").splitlines() == [
        "sz300308\t2026-07-08\t2026-07-09",
        "sh000300\t2026-07-08\t2026-07-09",
    ]
    assert (calendars / "day.txt").read_text(encoding="utf-8").splitlines() == [
        "2026-07-08", "2026-07-09",
    ]
    assert list(qlib_root.glob(".update_daily.stage.*")) == []
    assert list(qlib_root.glob(".update_daily.rollback.*")) == []


def test_main_restores_old_tree_when_final_calendar_publish_fails(tmp_path, monkeypatch):
    qlib_root, calendars, instruments, features = _configure_staged_main(
        tmp_path, monkeypatch,
    )
    original_replace = update_daily.os.replace

    def fail_final_calendar(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.name == "day.txt"
            and ".update_daily.stage." in source_path.as_posix()
            and destination_path == calendars / "day.txt"
        ):
            raise OSError("calendar publish unavailable")
        return original_replace(source, destination)

    monkeypatch.setattr(update_daily.os, "replace", fail_final_calendar)

    with pytest.raises(OSError, match="calendar publish unavailable"):
        update_daily.main()

    assert (features / "sz300308" / "close.day.bin").read_bytes() == b"old-feature"
    assert not (features / "sh000300").exists()
    assert (instruments / "all.txt").read_text(encoding="utf-8") == (
        "sz300308\t2026-07-08\t2026-07-08\n"
    )
    assert (calendars / "day.txt").read_text(encoding="utf-8") == "2026-07-08\n"
    assert list(qlib_root.glob(".update_daily.stage.*")) == []
    assert list(qlib_root.glob(".update_daily.rollback.*")) == []


def test_startup_recovers_crash_between_backup_rename_and_journal_update(tmp_path):
    qlib_root = tmp_path / "qlib"
    live = qlib_root / "features"
    rollback = qlib_root / ".update_daily.rollback.interrupted"
    stage = qlib_root / ".update_daily.stage.interrupted"
    (live / "sz300308").mkdir(parents=True)
    (live / "sz300308" / "close.day.bin").write_bytes(b"old-feature")
    stage.mkdir(parents=True)
    rollback.mkdir(parents=True)
    # Simulate a crash after live -> backup but before phase became backed_up.
    update_daily.os.replace(live, rollback / "features")
    (rollback / update_daily._QlibStagedPublication.STATE_NAME).write_text(
        json.dumps({
            "state": "committing",
            "stage_root": stage.name,
            "records": [{
                "relative_path": "features",
                "had_live": True,
                "phase": "prepared",
            }],
        }),
        encoding="utf-8",
    )

    recovered = update_daily.recover_incomplete_qlib_publications(qlib_root)

    assert recovered == [rollback.name]
    assert (live / "sz300308" / "close.day.bin").read_bytes() == b"old-feature"
    assert not rollback.exists()
    assert not stage.exists()


def test_historical_calendar_insertion_fails_before_any_bin_write(tmp_path, monkeypatch):
    parquet = tmp_path / "parquet"
    calendars = tmp_path / "qlib" / "calendars"
    instruments = tmp_path / "qlib" / "instruments"
    features = tmp_path / "qlib" / "features"
    parquet.mkdir()
    calendars.mkdir(parents=True)
    (calendars / "day.txt").write_text(
        "2026-07-08\n2026-07-10\n",
        encoding="utf-8",
    )
    rows = []
    for trade_date in ("20260708", "20260709", "20260710"):
        rows.append({
            "ts_code": "300308.SZ", "trade_date": trade_date,
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "vol": 100.0, "pct_chg": 1.0, "adj_factor": 2.0,
        })
    pd.DataFrame(rows).to_parquet(parquet / "20260710.parquet", index=False)
    monkeypatch.setattr(update_daily, "PARQUET_DIR", parquet)
    monkeypatch.setattr(update_daily, "CALENDARS_DIR", calendars)
    monkeypatch.setattr(update_daily, "INSTRUMENTS_DIR", instruments)
    monkeypatch.setattr(update_daily, "FEATURES_DIR", features)
    writes = []
    monkeypatch.setattr(
        update_daily,
        "_atomic_write_bytes",
        lambda *args: writes.append(args),
    )

    with pytest.raises(RuntimeError, match="not a tail-only extension"):
        update_daily.build_qlib_bin(publish_calendar=False)

    assert writes == []
    assert not features.exists()


def test_update_lock_releases_thread_guard_when_lock_directory_creation_fails(tmp_path, monkeypatch):
    lock_root = tmp_path / "qlib"
    original_mkdir = Path.mkdir
    calls = {"count": 0}

    def fail_once(path, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("disk unavailable")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_once)
    with pytest.raises(OSError, match="disk unavailable"):
        with update_daily.qlib_update_lock(lock_root):
            pass

    with update_daily.qlib_update_lock(lock_root):
        pass


def test_page_stock_append_never_extends_global_calendar(tmp_path, monkeypatch):
    qlib_root = tmp_path / "qlib"
    parquet = tmp_path / "parquet"
    (qlib_root / "calendars").mkdir(parents=True)
    parquet.mkdir()
    calendar = qlib_root / "calendars" / "day.txt"
    calendar.write_text("2026-07-09\n", encoding="utf-8")
    pd.DataFrame([{
        "ts_code": "300308.SZ", "trade_date": "20260710",
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "vol": 100.0, "pct_chg": 1.0, "adj_factor": 2.0,
    }]).to_parquet(parquet / "20260710.parquet", index=False)
    monkeypatch.setattr(app, "QLIB_DATA_PATH", qlib_root)
    monkeypatch.setattr(app, "PARQUET_DIR", parquet)

    appended = app._append_dates_to_stock_bin("sz300308", ["20260710"])

    assert appended == 0
    assert calendar.read_text(encoding="utf-8") == "2026-07-09\n"
    assert not (qlib_root / "features" / "sz300308").exists()


def test_page_single_stock_rebuild_ignores_dates_not_in_global_calendar(tmp_path, monkeypatch):
    qlib_root = tmp_path / "qlib"
    parquet = tmp_path / "parquet"
    (qlib_root / "calendars").mkdir(parents=True)
    parquet.mkdir()
    calendar = qlib_root / "calendars" / "day.txt"
    calendar.write_text("2026-07-09\n", encoding="utf-8")
    for trade_date in ("20260709", "20260710"):
        pd.DataFrame([{
            "ts_code": "300308.SZ", "trade_date": trade_date,
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "vol": 100.0, "pct_chg": 1.0, "adj_factor": 2.0,
        }]).to_parquet(parquet / f"{trade_date}.parquet", index=False)
    monkeypatch.setattr(app, "QLIB_DATA_PATH", qlib_root)
    monkeypatch.setattr(app, "PARQUET_DIR", parquet)

    result = app._full_rebuild_one_stock("sz300308")

    assert result["ok"] is True
    assert result["first"] == result["last"] == "2026-07-09"
    assert calendar.read_text(encoding="utf-8") == "2026-07-09\n"
    close = np.fromfile(
        qlib_root / "features" / "sz300308" / "close.day.bin", dtype="<f4",
    )
    assert close.size == 2  # one header plus one scheduler-published date


def test_page_single_stock_rebuild_preserves_initial_nan_and_zeros_only_suspension(
    tmp_path, monkeypatch,
):
    qlib_root = tmp_path / "qlib"
    parquet = tmp_path / "parquet"
    (qlib_root / "calendars").mkdir(parents=True)
    parquet.mkdir()
    dates = ["2026-07-09", "2026-07-10", "2026-07-13"]
    (qlib_root / "calendars" / "day.txt").write_text(
        "\n".join(dates) + "\n", encoding="utf-8",
    )
    first = _undefined_initial_return_row("20260709")
    last = {
        **_historical_row("20260713", 1.0, "920017.BJ"),
        "pre_close": 10.5,
        "change": 0.2,
        "pct_chg": 2.0,
    }
    for row in (first, last):
        pd.DataFrame([row]).to_parquet(
            parquet / f"{row['trade_date']}.parquet", index=False,
        )
    monkeypatch.setattr(app, "QLIB_DATA_PATH", qlib_root)
    monkeypatch.setattr(app, "PARQUET_DIR", parquet)

    result = app._full_rebuild_one_stock("bj920017")

    assert result["ok"] is True
    change = np.fromfile(
        qlib_root / "features" / "bj920017" / "change.day.bin", dtype="<f4",
    )
    assert change[0] == 0.0
    assert np.isnan(change[1])
    assert change[2] == 0.0
    assert change[3] == pytest.approx(0.02)


def test_page_stock_repair_yields_when_full_market_update_owns_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QLIB_DATA_PATH", tmp_path)
    monkeypatch.setattr(
        app,
        "_ensure_freshness_inner",
        lambda _code: pytest.fail("page repair must not run while scheduler owns the lock"),
    )

    with update_daily.qlib_update_lock(tmp_path):
        result = app.ensure_freshness_for_stock("sz300308")

    assert result["status"] == "update_in_progress"
