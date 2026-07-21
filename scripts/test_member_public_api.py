from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app as app_module


def _member_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    monkeypatch.setenv("QI_DEV_ROLE", "member")
    monkeypatch.setenv("QI_DEV_PLAN", "data_pro")
    monkeypatch.setattr(app_module, "MEMBERS_DB", tmp_path / "members.db")
    app_module.app.config.update(TESTING=True, CSRF_TESTING=False)
    return app_module.app.test_client()


def _empty_ohlcv() -> dict:
    return {
        "dates": [],
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "volume": [],
        "adjust": "qfq",
    }


def test_demo_search_never_turns_a_real_code_into_synthetic_kline(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    monkeypatch.setenv("QI_DEMO_DATA", "1")
    monkeypatch.setattr(app_module, "QLIB_DATA_PATH", tmp_path / "missing-qlib")
    monkeypatch.setattr(app_module, "STOCK_META_DB", str(tmp_path / "missing-meta.db"))

    landing = client.get("/")
    assert landing.status_code == 302
    assert "code=sh600519" in landing.headers["Location"]

    search = client.get("/api/search?q=600519")
    assert search.status_code == 200
    assert search.get_json()["hits"][0]["code"] == "sh600519"

    with patch.object(app_module, "load_ohlcv", return_value=_empty_ohlcv()), patch.object(
        app_module, "_eastmoney_daily_ohlcv", return_value=_empty_ohlcv()
    ) as eastmoney:
        response = client.get("/api/kline?code=600519&days=5")
    payload = response.get_json()
    assert response.status_code == 404
    assert payload == {"error": "no data for sh600519"}
    eastmoney.assert_called_once_with("sh600519", last_n_days=5, adjust="qfq")
    assert not (tmp_path / "missing-meta.db").exists()


def test_kline_rejects_bad_parameters_before_reading_data(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    with patch.object(app_module, "load_ohlcv") as load:
        for url in (
            "/api/kline?code=../../secret",
            "/api/kline?code=600519&days=bad",
            "/api/kline?code=600519&days=-1",
            "/api/kline?code=600519&days=5001",
            "/api/kline?code=600519&adjust=invalid",
            "/api/kline?code=600519&refresh=yes",
        ):
            response = client.get(url)
            assert response.status_code == 400
            assert response.is_json
    load.assert_not_called()


def test_kline_returns_controlled_503_for_invalid_data_contract(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    malformed = {
        "dates": ["2026-07-10"],
        "open": [10.0],
        "high": [11.0],
        "low": [9.0],
        "close": [],
        "volume": [100],
    }
    with patch.object(app_module, "load_ohlcv", return_value=malformed):
        response = client.get("/api/kline?code=sh600519")
    assert response.status_code == 503
    assert response.get_json() == {"error": "invalid market data payload"}


def test_kline_rejects_bad_dates_and_impossible_ohlc(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    base = {
        "dates": ["2026-07-09", "2026-07-10"],
        "open": [10.0, 10.0],
        "high": [11.0, 11.0],
        "low": [9.0, 9.0],
        "close": [10.5, 10.5],
        "volume": [100, 100],
    }
    malformed_payloads = [
        {**base, "dates": ["2026-07-10", "2026-07-09"]},
        {**base, "dates": ["2026-02-30", "2026-07-10"]},
        {**base, "high": [9.5, 11.0]},
        {**base, "low": [10.25, 9.0]},
    ]
    for malformed in malformed_payloads:
        with patch.object(app_module, "load_ohlcv", return_value=malformed):
            response = client.get("/api/kline?code=sh600519")
        assert response.status_code == 503
        assert response.get_json() == {"error": "invalid market data payload"}


def test_screen_missing_financial_table_is_controlled_and_closes_db(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    financials = tmp_path / "financials.db"
    with closing(sqlite3.connect(financials)) as conn:
        pass
    monkeypatch.setattr(app_module, "FINANCIALS_DB", str(financials))

    response = client.get("/api/screen")
    payload = response.get_json()
    assert response.status_code == 503
    assert payload["hits"] == []
    assert payload["total_matched"] == 0
    assert str(financials) not in response.get_data(as_text=True)
    financials.unlink()
    assert not financials.exists()


def test_search_missing_db_does_not_create_sqlite_file(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    missing_db = tmp_path / "missing.db"
    monkeypatch.setattr(app_module, "STOCK_META_DB", str(missing_db))
    monkeypatch.setattr(app_module, "_ipo_data", lambda: {})

    response = client.get("/api/search?q=600519")
    assert response.status_code == 200
    assert response.get_json() == {"hits": []}
    assert not missing_db.exists()


def test_watchlist_rejects_non_object_json(tmp_path, monkeypatch):
    client = _member_client(tmp_path, monkeypatch)
    response = client.post("/api/watchlist/add", json=["sh600519"])
    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object required"}
