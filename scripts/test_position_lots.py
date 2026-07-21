"""持仓批次接口回归测试。运行：python scripts/test_position_lots.py"""
import tempfile
import unittest
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class PositionLotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.client = app.app.test_client()
        now = datetime.now()
        self.today = now.strftime("%Y-%m-%d")
        self.yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        self.older = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        self.patches = [
            patch.dict(os.environ, {"QI_AUTH_ENABLED": "0"}),
            patch.dict(app.app.config, {"TESTING": True}),
            patch.object(app, "MEMBERS_DB", root / "members.db"),
            patch.object(app, "POSITIONS_JSON", root / "positions.json"),
            patch.object(app, "SELLS_HISTORY_JSON", root / "sells.json"),
            patch.object(app, "_resolve_to_tscode", lambda raw: "300408.SZ"),
            patch.object(app, "_meta_for_codes", lambda codes: {"300408.SZ": {"name": "三环集团"}}),
            patch.object(app, "_rt_quotes", lambda codes: {}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def add(self, cost, qty, date):
        result = self.client.post("/api/positions/add", json={
            "code": "300408", "cost": cost, "qty": qty, "date": date,
        }).get_json()
        self.assertTrue(result["ok"])

    def position(self):
        return self.client.get("/api/positions").get_json()["positions"][0]

    def test_repeated_buys_append_and_weight_cost(self):
        self.add(100, 1000, self.yesterday)
        self.add(110, 500, self.today)
        p = self.position()
        self.assertEqual(len(p["lots"]), 2)
        self.assertEqual(p["qty"], 1500)
        self.assertAlmostEqual(p["cost"], 103.3333, places=4)
        self.assertEqual(p["available_qty"], 1000)
        self.assertEqual(p["locked_qty"], 500)

    def test_lifo_matches_today_lot_but_caps_old_share_quantity(self):
        self.add(90, 1000, self.older)
        self.add(100, 500, self.yesterday)
        self.add(110, 500, self.today)
        sold = self.client.post("/api/positions/sell", json={
            "code": "300408", "qty": 600, "sell_price": 115,
            "sell_date": self.today,
        }).get_json()
        self.assertTrue(sold["ok"])
        self.assertAlmostEqual(sold["record"]["cost"], (110 * 500 + 100 * 100) / 600)
        self.assertTrue(sold["record"]["allocations"][0]["same_day_match"])
        p = self.position()
        self.assertEqual(p["qty"], 1400)
        self.assertEqual(p["available_qty"], 900)
        self.assertEqual(p["locked_qty"], 500)
        rejected = self.client.post("/api/positions/sell", json={
            "code": "300408", "qty": 901, "sell_price": 115,
            "sell_date": self.today,
        }).get_json()
        self.assertFalse(rejected["ok"])

    def test_legacy_record_migrates_without_data_loss(self):
        app.POSITIONS_JSON.write_text(
            f'[{{"code":"300408.SZ","cost":100,"qty":800,"date":"{self.yesterday}"}}]',
            encoding="utf-8",
        )
        p = self.position()
        self.assertEqual(p["qty"], 800)
        self.assertEqual(len(p["lots"]), 1)
        self.assertTrue(p["lots"][0]["legacy"])


if __name__ == "__main__":
    unittest.main()
