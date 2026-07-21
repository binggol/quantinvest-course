import importlib.util
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def load_money_module():
    path = ROOT / "scripts" / "backtest_money_outflow_signal.py"
    spec = importlib.util.spec_from_file_location("money_outflow_signal", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MoneyOutflowUiTest(unittest.TestCase):
    def test_latest_rows_expose_15d_and_recent_5d_outflow_fields(self):
        module = load_money_module()
        dates = pd.date_range("2026-07-01", periods=15, freq="D").strftime("%Y%m%d")
        rows = []
        for d in dates:
            rows.append({
                "ts_code": "000001.SZ",
                "trade_date": d,
                "main_net_amount": -1000.0,
                "amount": 100000.0,
                "main_net_ratio": -0.1,
                "outflow_ratio": 0.1,
                "close": 10.0,
                "ret_1d": 0.01,
                "ret_20d": 0.02,
                "ma20": 9.5,
                "high20": 11.0,
                "drawdown20": -0.05,
                "amount_ratio20": 1.2,
            })
            rows.append({
                "ts_code": "000002.SZ",
                "trade_date": d,
                "main_net_amount": -500.0,
                "amount": 100000.0,
                "main_net_ratio": -0.05,
                "outflow_ratio": 0.05,
                "close": 20.0,
                "ret_1d": 0.0,
                "ret_20d": 0.0,
                "ma20": 19.0,
                "high20": 21.0,
                "drawdown20": -0.01,
                "amount_ratio20": 1.0,
            })
        meta = pd.DataFrame([
            {"ts_code": "000001.SZ", "name": "Ping An", "industry": "Bank"},
            {"ts_code": "000002.SZ", "name": "Vanke", "industry": "Estate"},
        ])

        latest = module.latest_stock_flow_rows(pd.DataFrame(rows), meta=meta, limit=None)
        top = latest[0]

        self.assertEqual(top["code"], "000001.SZ")
        self.assertAlmostEqual(top["outflow_15d_yi"], 1.5)
        self.assertAlmostEqual(top["main_net_15d_yi"], -1.5)
        self.assertEqual(top["n_flow_days_15d"], 15)
        self.assertEqual(len(top["outflow_15d_daily"]), 15)
        self.assertEqual(len(top["outflow_recent_5d"]), 5)
        self.assertEqual(
            [d["date"] for d in top["outflow_recent_5d"]],
            ["2026-07-15", "2026-07-14", "2026-07-13", "2026-07-12", "2026-07-11"],
        )

    def test_template_has_show_all_toggle_and_recent_5d_columns(self):
        html = (ROOT / "templates" / "money_outflow.html").read_text(encoding="utf-8")

        self.assertIn("STOCK_SHOW_ALL=false", html)
        self.assertIn("toggleStockLimit", html)
        self.assertIn("outflow_recent_5d", html)
        self.assertIn("显示全部", html)
        self.assertIn("只看前200", html)


if __name__ == "__main__":
    unittest.main()
