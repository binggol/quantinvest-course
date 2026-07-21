"""新股上市研究接口的轻量回归测试。"""
import unittest
from unittest.mock import patch
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime
import requests

import app


class NewStocksApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.ipo = {
            "updated": "2026-07-02 08:00:00",
            "just_listed": [
                {"code": "001234.SZ", "name": "测试股份",
                 "issue_date": "20260702", "price": 10, "pe": 20},
                {"code": "688999.SH", "name": "近周新股",
                 "issue_date": "20260626", "price": 30},
            ],
            "soon_buy": [
                {"code": "603999.SH", "name": "待上市",
                 "issue_date": "20260705", "price": 12},
            ],
        }
        self.meta = {
            "001234.SZ": {"name": "测试股份", "industry": "电子"},
            "688999.SH": {"name": "近周新股", "industry": "医药"},
            "603999.SH": {"name": "待上市", "industry": "运输"},
        }

    def _patches(self):
        return (
            patch.object(app, "_ipo_data", return_value=self.ipo),
            patch.object(
                app, "_meta_for_codes",
                side_effect=lambda codes: {c: self.meta.get(c, {}) for c in codes},
            ),
            patch.object(app, "STOCK_META_DB", "Z:/missing/stock_meta.db"),
        )

    def test_list_groups_today_week_and_upcoming(self):
        p1, p2, p3 = self._patches()
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 2, 12, 0)

        with p1, p2, p3, patch.object(app, "datetime", FixedDateTime):
            data = self.client.get("/api/new_stocks?days=7").get_json()
        self.assertEqual(data["counts"], {"today": 1, "week": 1, "upcoming": 1})
        self.assertEqual([x["group"] for x in data["items"]],
                         ["today", "week", "upcoming"])
        self.assertEqual(data["items"][0]["industry"], "电子")

    def test_detail_calculates_issue_price_premium(self):
        p1, p2, p3 = self._patches()
        with p1, p2, p3, patch.object(
            app, "_rt_quotes", return_value={"001234.SZ": {"price": 15}}
        ):
            response = self.client.get("/api/new_stocks/detail?code=001234")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["premium_pct"], 50.0)

    def test_detail_rejects_invalid_code(self):
        response = self.client.get("/api/new_stocks/detail?code=bad")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_main_pages_expose_new_stocks_entry(self):
        for path in ("/", "/daily"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('href="/new-stocks"', html)

    def test_kline_accepts_tushare_code_from_new_stocks_page(self):
        captured = []

        def fake_load(code, last_n_days=None, adjust="qfq"):
            captured.append(code)
            return {
                "dates": ["2026-07-02"], "open": [10], "close": [11],
                "low": [9.8], "high": [11.2], "volume": [1000],
            }

        with patch.object(app, "ensure_freshness_for_stock", return_value=None), \
             patch.object(app, "load_ohlcv", side_effect=fake_load), \
             patch.object(app, "STOCK_META_DB", "Z:/missing/stock_meta.db"):
            response = self.client.get("/api/kline?code=001234.SZ&refresh=0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, ["sz001234"])

    def test_search_falls_back_to_today_ipo_and_accepts_n_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "stock_meta.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE stock_meta (code TEXT, ts_code TEXT, name TEXT, "
                "industry TEXT, list_status TEXT, pinyin_initials TEXT)"
            )
            conn.commit()
            conn.close()
            ipo = {
                "just_listed": [{
                    "code": "001248.SZ", "name": "华润新能源",
                    "issue_date": "20260702", "board": "主板",
                }]
            }
            with patch.object(app, "STOCK_META_DB", str(db)), \
                 patch.object(app, "_ipo_data", return_value=ipo):
                response = self.client.get("/api/search?q=N华润")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["hits"][0]["code"], "sz001248")

    def test_search_uses_ipo_when_stock_meta_table_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            ipo = {"just_listed": [{"code": "001248.SZ", "name": "华润新能源"}]}
            with patch.object(app, "STOCK_META_DB", str(Path(td) / "empty.db")), \
                 patch.object(app, "_ipo_data", return_value=ipo):
                response = self.client.get("/api/search?q=001248")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["hits"][0]["ts_code"], "001248.SZ")

    def test_first_day_stock_creates_bin_instead_of_exiting(self):
        with patch.object(app, "_read_calendar", return_value=["2026-07-01"]), \
             patch.object(app, "_read_bin", return_value=(-1, app.np.array([]))), \
             patch.object(app, "_is_trading_day", return_value=False), \
             patch.object(app, "_full_rebuild_one_stock",
                          return_value={"ok": True, "n_days": 1,
                                        "first": "2026-07-02", "last": "2026-07-02"}):
            status = app._ensure_freshness_inner("sz001248")
        self.assertEqual(status["status"], "rebuilt")

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
            helper = getattr(
                app, "_eastmoney_daily_ohlcv",
                lambda code: {
                    "dates": [], "open": [], "close": [], "high": [],
                    "low": [], "volume": [], "source": "",
                },
            )
            data = helper("sz001248")

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
             patch.object(app, "_eastmoney_daily_ohlcv", return_value=fallback,
                          create=True) as em, \
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
