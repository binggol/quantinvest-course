from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import requests

import app


PRICE_FIELDS = ("open", "close", "high", "low")


def _write_price_fixture(
    root: Path,
    raw: dict[str, np.ndarray],
    adj: np.ndarray,
    code: str = "sz300308",
    write_adj: bool = True,
) -> None:
    calendar = root / "calendars"
    calendar.mkdir(parents=True)
    dates = pd.bdate_range("2026-01-02", periods=len(adj)).strftime("%Y-%m-%d")
    (calendar / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
    stored_base = float(np.max(adj))
    for field in PRICE_FIELDS:
        app._write_bin(code, field, 0, raw[field] * adj / stored_base)
    app._write_bin(code, "volume", 0, np.arange(len(adj), dtype=float) + 100)
    if write_adj:
        app._write_bin(code, "adj", 0, adj)


def test_load_ohlcv_converts_internal_max_normalization_to_all_adjustments(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QLIB_DATA_PATH", tmp_path)
    adj = np.array([1.0, 8.0, 2.0], dtype=float)
    close = np.array([80.0, 10.0, 40.0])
    raw = {
        "open": close - 1.0,
        "close": close,
        "high": close + 2.0,
        "low": close - 2.0,
    }
    _write_price_fixture(tmp_path, raw, adj)

    qfq = app.load_ohlcv("sz300308", adjust="qfq")
    hfq = app.load_ohlcv("sz300308", adjust="hfq")
    unadjusted = app.load_ohlcv("sz300308", adjust="none")
    raw_alias = app.load_ohlcv("sz300308", adjust="raw")

    assert qfq["close"] == [40.0, 40.0, 40.0]
    assert hfq["close"] == [80.0, 80.0, 80.0]
    assert unadjusted["close"] == [80.0, 10.0, 40.0]
    assert raw_alias["close"] == unadjusted["close"]
    assert qfq["adjust"] == "qfq"
    assert hfq["adjust"] == "hfq"
    assert unadjusted["adjust"] == "none"


def test_load_ohlcv_rejects_bin_range_outside_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QLIB_DATA_PATH", tmp_path)
    adj = np.array([1.0, 1.0, 1.0], dtype=float)
    raw = {field: np.ones(3, dtype=float) * 10 for field in PRICE_FIELDS}
    _write_price_fixture(tmp_path, raw, adj)
    (tmp_path / "calendars" / "day.txt").write_text("2026-01-02\n", encoding="utf-8")

    result = app.load_ohlcv("sz300308", adjust="qfq")

    assert result["dates"] == []


def test_load_ohlcv_repairs_source_envelope_and_reports_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QLIB_DATA_PATH", tmp_path)
    adj = np.ones(3, dtype=float)
    raw = {
        "open": np.array([10.0, 10.0, 10.0]),
        "close": np.array([11.0, 9.0, 10.0]),
        "high": np.array([10.5, 11.0, 11.0]),
        "low": np.array([9.0, 9.5, 9.0]),
    }
    _write_price_fixture(tmp_path, raw, adj)

    result = app.load_ohlcv("sz300308", adjust="qfq")

    assert result["open"] == [10.0, 10.0, 10.0]
    assert result["close"] == [11.0, 9.0, 10.0]
    assert result["high"] == [11.0, 11.0, 11.0]
    assert result["low"] == [9.0, 9.0, 9.0]
    assert result["quality"] == {"ohlc_envelope_repairs": 2}


@pytest.mark.parametrize("adjust", ["qfq", "hfq", "none", "raw"])
def test_benchmark_indices_treat_adjustment_as_not_applicable(tmp_path, monkeypatch, adjust):
    monkeypatch.setattr(app, "QLIB_DATA_PATH", tmp_path)
    adj = np.ones(3, dtype=float)
    raw = {
        "open": np.array([4000.0, 4010.0, 4020.0]),
        "close": np.array([4010.0, 4020.0, 4030.0]),
        "high": np.array([4020.0, 4030.0, 4040.0]),
        "low": np.array([3990.0, 4000.0, 4010.0]),
    }
    _write_price_fixture(tmp_path, raw, adj, code="sh000300", write_adj=False)

    result = app.load_ohlcv("sh000300", adjust=adjust)

    assert result["close"] == [4010.0, 4020.0, 4030.0]
    assert result["adjust"] == adjust
    assert result["adjustment_applicable"] is False


def test_daily_and_weekly_fast_paths_return_true_qfq_and_daily_open(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QLIB_DATA_PATH", tmp_path)
    monkeypatch.setattr(app, "_WEEK_CACHE", None)
    n = 130
    adj = np.r_[np.ones(50), np.full(50, 8.0), np.full(30, 2.0)]
    close = np.full(n, 20.0)
    raw = {
        "open": np.full(n, 19.0),
        "close": close,
        "high": np.full(n, 21.0),
        "low": np.full(n, 18.0),
    }
    raw["high"][-1] = 18.5
    raw["low"][-2] = 20.5
    _write_price_fixture(tmp_path, raw, adj)

    daily = app._daily_ohlc("sz300308", days=5)
    weekly = app._weekly_ohlc("sz300308", days=130)

    assert daily is not None
    assert np.allclose(daily["open"], 19.0)
    assert np.allclose(daily["close"], 20.0)
    assert daily["high"][-1] == 20.0
    assert daily["low"][-2] == 19.0
    assert daily["quality"] == {"ohlc_envelope_repairs": 2}
    assert weekly is not None
    assert np.isclose(weekly["close"][-1], 20.0)
    assert np.isclose(weekly["high"][-1], 21.0)
    assert np.isclose(weekly["low"][-1], 18.0)
    assert weekly["quality"] == {"ohlc_envelope_repairs": 2}


def test_eventstop_candles_preserve_daily_open():
    dates = pd.bdate_range("2026-01-02", periods=130).strftime("%Y-%m-%d").tolist()
    daily = {
        "dates": dates,
        "open": np.full(130, 10.5),
        "high": np.full(130, 12.0),
        "low": np.full(130, 9.0),
        "close": np.full(130, 11.0),
        "volume": np.full(130, 1000.0),
        "quality": {"ohlc_envelope_repairs": 3},
    }
    with patch.object(app, "_resolve_to_tscode", return_value="300308.SZ"), patch.object(
        app, "_daily_ohlc", return_value=daily
    ), patch.object(app, "_meta_for_codes", return_value={}):
        result = app._eventstop_calc("300308", dates[0], 11.0)

    assert result["ok"] is True
    assert result["k"]["ohlc"][0] == [10.5, 11.0, 9.0, 12.0]
    assert result["k"]["adjust"] == "qfq"
    assert result["k"]["source"] == "qlib"
    assert result["k"]["quality"] == {"ohlc_envelope_repairs": 3}


@pytest.mark.parametrize(
    ("requested", "expected_adjust", "expected_fqt"),
    [
        ("qfq", "qfq", "1"),
        ("hfq", "hfq", "2"),
        ("none", "none", "0"),
        ("raw", "none", "0"),
    ],
)
def test_eastmoney_fallback_honors_adjustment(requested, expected_adjust, expected_fqt):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "name": "中际旭创",
                    "klines": ["2026-07-10,1210,1093.98,1218,1093.98,423262.42"],
                }
            }

    with patch.object(requests, "get", return_value=Response()) as get:
        result = app._eastmoney_daily_ohlcv("sz300308", adjust=requested)

    assert get.call_args.kwargs["params"]["fqt"] == expected_fqt
    assert result["adjust"] == expected_adjust
    assert result["adjust_requested"] == requested
    assert result["close"] == [1093.98]
