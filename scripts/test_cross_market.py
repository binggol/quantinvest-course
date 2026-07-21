import unittest
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from cross_market import (
    build_monthly_universe,
    classify_data_health,
    compare_sources,
    evaluate_live_gate,
    match_holdings,
    rank_stocks,
    score_stock,
    validate_result,
    visible_korea_points,
)


class CrossMarketTimeTest(unittest.TestCase):
    def test_0920_decision_uses_only_korea_points_at_or_before_0905(self):
        points = [
            {"market_time": "09:00", "price": 201000},
            {"market_time": "09:05", "price": 202000},
            {"market_time": "09:10", "price": 204000},
        ]

        visible = visible_korea_points(
            points,
            decision_at=datetime(2026, 7, 6, 9, 20),
            delay_minutes=15,
        )

        self.assertEqual(
            [point["market_time"] for point in visible],
            ["09:00", "09:05"],
        )

    def test_source_difference_above_half_percent_is_conflict(self):
        result = compare_sources(100.0, 100.6, tolerance_pct=0.5)

        self.assertFalse(result["ok"])
        self.assertAlmostEqual(result["difference_pct"], 0.6)

    def test_korea_age_above_twenty_minutes_degrades_signal(self):
        health = classify_data_health(
            decision_at="2026-07-06T09:20:00+08:00",
            actual_market_at="2026-07-06T08:59:00+08:00",
            max_age_minutes=20,
        )

        self.assertEqual(health["status"], "stale")
        self.assertEqual(health["age_minutes"], 21.0)

    def test_result_schema_rejects_missing_audit_fields(self):
        with self.assertRaisesRegex(ValueError, "missing result keys"):
            validate_result({"schema_version": 1, "mode": "research"})

    def test_result_schema_rejects_unknown_mode(self):
        result = {
            "schema_version": 1,
            "generated_at": "2026-07-06T09:20:00+08:00",
            "decision_at": "2026-07-06T09:20:00+08:00",
            "sector": "storage",
            "mode": "paper",
            "data_health": {},
            "leaders": [],
            "upside": [],
            "downside": [],
            "holdings": [],
            "gate": {},
            "charts": {},
        }

        with self.assertRaisesRegex(ValueError, "research or live"):
            validate_result(result)


class CrossMarketScoreTest(unittest.TestCase):
    def test_upside_and_downside_are_scored_independently(self):
        row = {
            "code": "301308.SZ",
            "name": "江波龙",
            "business_purity": 90,
            "positive_beta_score": 80,
            "negative_beta_score": 55,
            "stability": 75,
            "liquidity": 85,
        }

        upside = score_stock(
            row, us_score=90, korea_score=80, direction="up"
        )
        downside = score_stock(
            row, us_score=90, korea_score=80, direction="down"
        )

        self.assertGreater(upside["score"], downside["score"])
        self.assertIn("business_purity", upside["contributions"])

    def test_unknown_direction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "up or down"):
            score_stock(
                {
                    "business_purity": 90,
                    "positive_beta_score": 80,
                    "negative_beta_score": 55,
                    "stability": 75,
                    "liquidity": 85,
                },
                us_score=90,
                korea_score=80,
                direction="flat",
            )

    def test_ranking_excludes_st_untradable_and_illiquid_stocks(self):
        rows = [
            {
                "code": "301308.SZ",
                "name": "江波龙",
                "score": 82,
                "is_st": False,
                "tradable": True,
                "amount": 500_000_000,
            },
            {
                "code": "000001.SZ",
                "name": "ST样本",
                "score": 95,
                "is_st": True,
                "tradable": True,
                "amount": 900_000_000,
            },
            {
                "code": "000002.SZ",
                "name": "停牌样本",
                "score": 91,
                "is_st": False,
                "tradable": False,
                "amount": 900_000_000,
            },
            {
                "code": "000003.SZ",
                "name": "低流动性",
                "score": 90,
                "is_st": False,
                "tradable": True,
                "amount": 20_000_000,
            },
        ]

        ranked = rank_stocks(rows, min_amount=100_000_000)

        self.assertEqual([row["code"] for row in ranked], ["301308.SZ"])

    def test_live_gate_requires_backtest_and_thirty_forward_days(self):
        gate = evaluate_live_gate(
            {
                "sample_years": 3.4,
                "win_rate": 0.58,
                "sharpe": 1.2,
                "mean_excess": 0.003,
                "recent_12m_valid": True,
                "forward_days": 29,
                "forward_valid": True,
                "data_ok": True,
            }
        )

        self.assertFalse(gate["allow_live"])
        self.assertIn("forward_days", gate["failed"])
        self.assertEqual(gate["mode"], "research")

    def test_live_gate_opens_only_when_every_check_passes(self):
        gate = evaluate_live_gate(
            {
                "sample_years": 3.4,
                "win_rate": 0.58,
                "sharpe": 1.2,
                "mean_excess": 0.003,
                "recent_12m_valid": True,
                "forward_days": 30,
                "forward_valid": True,
                "data_ok": True,
            }
        )

        self.assertTrue(gate["allow_live"])
        self.assertEqual(gate["failed"], [])
        self.assertEqual(gate["mode"], "live")

    def test_holdings_are_matched_by_six_digit_code(self):
        matched = match_holdings(
            [{"code": "301308.SZ", "name": "江波龙", "score": 82}],
            [{"code": "sz301308", "qty": 1000, "cost": 90.0}],
            direction="up",
        )

        self.assertEqual(matched[0]["code"], "301308.SZ")
        self.assertEqual(matched[0]["qty"], 1000)
        self.assertEqual(matched[0]["direction"], "up")


class CrossMarketCollectorTest(unittest.TestCase):
    def test_us_quote_falls_back_to_yahoo_when_eastmoney_fails(self):
        import scripts.export_cross_market_storage as exporter

        def failed_primary(symbol):
            raise RuntimeError("primary unavailable")

        def backup(symbol, interval="1d", range_="5d"):
            return {
                "meta": {
                    "regularMarketPrice": 110.0,
                    "chartPreviousClose": 100.0,
                }
            }

        quote = exporter.collect_us_quote(
            "MU", primary_fetch=failed_primary, backup_fetch=backup
        )

        self.assertEqual(quote["source"], "yahoo")
        self.assertEqual(quote["return_pct"], 10.0)

    def test_collector_can_run_directly_from_scripts_directory(self):
        script = Path(__file__).with_name("export_cross_market_storage.py")

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parent.parent,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_append_snapshot_keeps_market_and_fetch_times(self):
        import scripts.export_cross_market_storage as exporter

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "korea_storage_intraday.jsonl"
            exporter.append_snapshot(
                path,
                {
                    "symbol": "000660.KS",
                    "market_at": "2026-07-06T09:05:00+09:00",
                    "fetched_at": "2026-07-06T09:20:00+08:00",
                    "price": 202000,
                    "source": "yahoo",
                },
            )

            row = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(row["market_at"], "2026-07-06T09:05:00+09:00")
        self.assertEqual(row["fetched_at"], "2026-07-06T09:20:00+08:00")
        self.assertEqual(row["source"], "yahoo")

    def test_atomic_json_replaces_complete_document(self):
        import scripts.export_cross_market_storage as exporter

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "result.json"
            path.write_text('{"old": true}', encoding="utf-8")

            exporter.atomic_json(path, {"schema_version": 1, "mode": "research"})

            result = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            result, {"schema_version": 1, "mode": "research"}
        )

    def test_yahoo_points_use_exchange_local_market_time(self):
        import scripts.export_cross_market_storage as exporter

        result = {
            "timestamp": [1783296300],
            "meta": {"exchangeTimezoneName": "Asia/Seoul"},
            "indicators": {"quote": [{"close": [202000]}]},
        }

        rows = exporter.yahoo_points(result, "000660.KS")

        self.assertEqual(rows[0]["market_time"], "09:05")
        self.assertTrue(rows[0]["market_at"].endswith("+09:00"))

    def test_research_result_excludes_korea_points_after_0905(self):
        import scripts.export_cross_market_storage as exporter

        result = exporter.build_research_result(
            us_quotes=[{"symbol": "MU", "return_pct": 3.0}],
            korea_points=[
                {"symbol": "000660.KS", "market_time": "09:05", "price": 202000},
                {"symbol": "000660.KS", "market_time": "09:10", "price": 203000},
            ],
            candidates=[
                {"code": "301308.SZ", "name": "江波龙", "business_purity": 90}
            ],
            generated_at="2026-07-06T09:20:00+08:00",
        )

        points = result["charts"]["korea"]["intraday"]["points"]
        self.assertEqual([point["market_time"] for point in points], ["09:05"])
        self.assertEqual(result["mode"], "research")
        self.assertFalse(result["gate"]["allow_live"])


class CrossMarketUniverseTest(unittest.TestCase):
    def test_monthly_universe_uses_only_history_before_month(self):
        rows = [
            {
                "date": "2026-05-29",
                "code": "301308.SZ",
                "positive_beta_score": 80,
                "negative_beta_score": 70,
                "stability": 75,
                "liquidity": 90,
                "amount": 500_000_000,
            },
            {
                "date": "2026-06-01",
                "code": "301308.SZ",
                "positive_beta_score": 100,
                "negative_beta_score": 100,
                "stability": 100,
                "liquidity": 100,
                "amount": 900_000_000,
            },
        ]
        candidates = {
            "301308.SZ": {"name": "江波龙", "business_purity": 90}
        }

        result = build_monthly_universe(
            rows, month_start="2026-06-01", candidates=candidates
        )

        self.assertEqual(result[0]["positive_beta_score"], 80)
        self.assertEqual(result[0]["last_history_date"], "2026-05-29")
        self.assertEqual(result[0]["universe_month"], "2026-06")

    def test_monthly_universe_excludes_illiquid_candidates(self):
        rows = [
            {
                "date": "2026-05-29",
                "code": "688525.SH",
                "positive_beta_score": 80,
                "negative_beta_score": 70,
                "stability": 75,
                "liquidity": 60,
                "amount": 20_000_000,
            }
        ]

        result = build_monthly_universe(
            rows,
            month_start="2026-06-01",
            candidates={
                "688525.SH": {
                    "name": "佰维存储",
                    "business_purity": 90,
                }
            },
        )

        self.assertEqual(result, [])


class CrossMarketBacktestTest(unittest.TestCase):
    def test_friday_us_close_maps_to_monday_a_share_session(self):
        from scripts.backtest_cross_market_storage import (
            align_us_to_a_share_day,
        )

        mapped = align_us_to_a_share_day(
            us_date="2026-07-03",
            a_share_days=["2026-07-03", "2026-07-06"],
        )

        self.assertEqual(mapped, "2026-07-06")

    def test_walk_forward_training_always_precedes_test(self):
        from scripts.backtest_cross_market_storage import walk_forward_splits

        splits = walk_forward_splits(
            list(range(800)), train_size=500, test_size=60
        )

        self.assertTrue(splits)
        for train, test in splits:
            self.assertLess(max(train), min(test))

    def test_metrics_include_costs_and_sample_count(self):
        from scripts.backtest_cross_market_storage import calculate_metrics

        result = calculate_metrics(
            [0.01, -0.004, 0.008], round_trip_cost=0.001
        )

        self.assertEqual(result["samples"], 3)
        self.assertAlmostEqual(result["mean_excess"], 0.0036667, places=6)

    def test_maximum_drawdown_uses_compounded_equity(self):
        from scripts.backtest_cross_market_storage import maximum_drawdown

        drawdown = maximum_drawdown([0.10, -0.20, 0.05])

        self.assertAlmostEqual(drawdown, -0.20)


if __name__ == "__main__":
    unittest.main()
