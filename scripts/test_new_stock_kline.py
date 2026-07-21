"""Regression tests for newly listed stock search and K-line fallback."""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

import app


class NewStockKlineTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_search_uses_ipo_when_stock_meta_table_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            ipo_path = Path(td) / "ipo.json"
            ipo_path.write_text(
                '{"just_listed":[{"code":"001248.SZ","name":"华润新能源"}]}',
                encoding="utf-8",
            )
            with patch.object(app, "STOCK_META_DB", str(Path(td) / "empty.db")), \
                 patch.object(app, "IPO_JSON", ipo_path):
                response = self.client.get("/api/search?q=001248")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["hits"][0]["ts_code"], "001248.SZ")
        self.assertEqual(response.get_json()["hits"][0]["code"], "sz001248")

    def test_first_day_stock_refetches_stale_cached_daily_file(self):
        class AfterClose(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 2, 20, 0)

        fetch_calls = []
        cache = {"fresh": False}

        def fake_fetch(date_str, force=False):
            fetch_calls.append(force)
            if force:
                cache["fresh"] = True
            return True

        def fake_rebuild(code):
            if cache["fresh"]:
                return {
                    "ok": True, "n_days": 1,
                    "first": "2026-07-02", "last": "2026-07-02",
                }
            return {"ok": False, "message": "cached parquet lacks this stock"}

        with patch.object(app, "datetime", AfterClose), \
             patch.object(app, "_read_calendar", return_value=["2026-07-01"]), \
             patch.object(app, "_read_bin", return_value=(-1, app.np.array([]))), \
             patch.object(app, "_is_trading_day", return_value=True), \
             patch.object(app, "_fetch_one_day_parquet", side_effect=fake_fetch), \
             patch.object(app, "_full_rebuild_one_stock", side_effect=fake_rebuild):
            status = app._ensure_freshness_inner("sz001248")

        self.assertEqual(status["status"], "rebuilt")
        self.assertEqual(fetch_calls, [False, True])

    def test_eastmoney_daily_normalizes_new_stock_kline(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "name": "华润新能源",
                        "klines": [
                            "2026-07-02,12.50,15.00,15.00,12.50,123456,0",
                        ],
                    },
                }

        with patch.object(requests, "get", return_value=Response()) as get:
            data = app._eastmoney_daily_ohlcv("sz001248")

        self.assertEqual(data["dates"], ["2026-07-02"])
        self.assertEqual(data["open"], [12.5])
        self.assertEqual(data["close"], [15.0])
        self.assertEqual(data["high"], [15.0])
        self.assertEqual(data["low"], [12.5])
        self.assertEqual(data["volume"], [123456])
        self.assertEqual(data["source"], "eastmoney")
        self.assertEqual(get.call_args.kwargs["params"]["secid"], "0.001248")

    def test_kline_uses_eastmoney_only_when_local_history_is_empty(self):
        fallback = {
            "dates": ["2026-07-02"], "open": [12.5], "close": [15.0],
            "high": [15.0], "low": [12.5], "volume": [123456],
            "adjust": "none", "adjust_requested": "qfq",
            "source": "eastmoney", "name": "华润新能源",
        }
        empty = {
            "dates": [], "open": [], "close": [], "high": [], "low": [],
            "volume": [], "adjust": "qfq",
        }
        with patch.object(app, "ensure_freshness_for_stock", return_value={
                 "status": "stock_not_in_db", "message": "no local data",
             }), \
             patch.object(app, "load_ohlcv", return_value=empty), \
             patch.object(app, "_eastmoney_daily_ohlcv", return_value=fallback) as em, \
             patch.object(app, "STOCK_META_DB", "Z:/missing/stock_meta.db"):
            response = self.client.get("/api/kline?code=001248.SZ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["source"], "eastmoney")
        em.assert_called_once_with("sz001248", last_n_days=None, adjust="qfq")

    def test_kline_does_not_call_eastmoney_when_local_history_exists(self):
        local = {
            "dates": ["2026-07-01"], "open": [10], "close": [11],
            "high": [11.2], "low": [9.8], "volume": [1000],
            "adjust": "qfq",
        }
        with patch.object(app, "ensure_freshness_for_stock", return_value=None), \
             patch.object(app, "load_ohlcv", return_value=local), \
             patch.object(app, "_eastmoney_daily_ohlcv") as em, \
             patch.object(app, "STOCK_META_DB", "Z:/missing/stock_meta.db"):
            response = self.client.get("/api/kline?code=001248.SZ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["source"], "qlib")
        em.assert_not_called()


if __name__ == "__main__":
    unittest.main()
