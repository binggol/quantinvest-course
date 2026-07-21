from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from app import app
import app as app_module


class _FakeResp:
    def __init__(self, rows):
        self.text = "var _=(" + json.dumps(rows) + ")"


class _FakeJsonResp:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_intraday_requested_date_uses_eastmoney_history_minutes():
    def fake_get(url, params=None, **kwargs):
        params = params or {}
        if "push2his.eastmoney.com" in url and params.get("klt") == "5":
            return _FakeJsonResp({"data": {"klines": [
                "2026-01-16 09:35,100,101,102,99,123,123000",
                "2026-01-16 09:40,101,103,104,100,88,89700",
            ]}})
        if "push2his.eastmoney.com" in url and params.get("klt") == "101":
            return _FakeJsonResp({"data": {"klines": [
                "2026-01-15,94,98,99,93,1000,98000",
                "2026-01-16,100,103,104,99,211,212700",
            ]}})
        return _FakeResp([])

    old_get = requests.get
    requests.get = fake_get
    try:
        response = app.test_client().get("/api/intraday?code=sz301308&date=20260116")
    finally:
        requests.get = old_get

    assert response.status_code == 200
    data = response.get_json()
    assert data["source"] == "eastmoney_history"
    assert data["date"] == "2026-01-16"
    assert data["times"] == ["09:35", "09:40"]
    assert data["close"] == [101.0, 103.0]
    assert data["pre_close"] == 98.0


class _FakeTusharePro:
    def stk_mins(self, ts_code, freq, start_date, end_date):
        assert ts_code == "301308.SZ"
        assert freq == "5min"
        return app_module.pd.DataFrame([
            {"trade_time": "2026-01-16 09:35:00", "open": 100, "high": 102, "low": 99, "close": 101, "vol": 123, "amount": 1230},
            {"trade_time": "2026-01-16 09:40:00", "open": 101, "high": 104, "low": 100, "close": 103, "vol": 88, "amount": 897},
        ])

    def daily(self, ts_code, start_date, end_date):
        return app_module.pd.DataFrame([
            {"trade_date": "20260115", "close": 98.0},
        ])


def test_tushare_history_intraday_builds_requested_date_minutes():
    data = app_module._tushare_history_intraday("sz301308", "20260116", "5min", _FakeTusharePro())

    assert data["source"] == "tushare_stk_mins"
    assert data["date"] == "2026-01-16"
    assert data["times"] == ["09:35", "09:40"]
    assert data["close"] == [101.0, 103.0]
    assert data["pre_close"] == 98.0


def test_intraday_requested_date_does_not_fallback_to_latest(monkeypatch=None):
    rows = [
        {"day": "2026-07-06 09:35:00", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "10", "amount": "1000"},
        {"day": "2026-07-06 09:40:00", "open": "100", "high": "102", "low": "99", "close": "101", "volume": "10", "amount": "1010"},
    ]
    old_get = requests.get
    requests.get = lambda *args, **kwargs: _FakeResp(rows)
    try:
        response = app.test_client().get("/api/intraday?code=sz301308&date=20260116")
    finally:
        requests.get = old_get

    assert response.status_code == 200
    data = response.get_json()
    assert data["date"] == "2026-01-16"
    assert data["times"] == []
    assert "没有分时数据" in data["message"]


if __name__ == "__main__":
    test_intraday_requested_date_uses_eastmoney_history_minutes()
    test_tushare_history_intraday_builds_requested_date_minutes()
    test_intraday_requested_date_does_not_fallback_to_latest()
    print("intraday api tests ok")
