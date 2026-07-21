from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backfill_earnings_event_times as target


class FakeExporter:
    calls: list[tuple[str, str, str, str]] = []

    @staticmethod
    def collect_for_code(code, start, end, sleep_s=0.0, max_pages=1, name=""):
        FakeExporter.calls.append((code, start, end, name))
        return [{"code": code, "ann_date": start.replace("-", ""), "title": "stub"}]

    @staticmethod
    def merge_items(items, rows):
        return list(items) + list(rows)


class BackfillThrottleTests(unittest.TestCase):
    def test_checkpoint_write_is_atomic_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            path.write_text('{"version":"old"}', encoding="utf-8")
            with patch.object(target.os, "replace", side_effect=OSError("publish failed")):
                with self.assertRaises(OSError):
                    target.write_json(path, {"version": "new"})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"version":"old"}')
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_limit_is_applied_after_done_tasks_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            target.write_json(data_dir / "cninfo_earnings_announcements.json", {"items": []})
            target.write_json(data_dir / "cninfo_earnings_event_backfill_status.json", {
                "done_tasks": [["000001", "202401"]]
            })
            FakeExporter.calls = []
            event_keys = {
                ("000001", "20240131"),
                ("000002", "20240229"),
            }
            with patch.object(target, "load_exporter", return_value=FakeExporter), \
                 patch.object(target, "load_financial_event_keys", return_value=event_keys):
                status = target.backfill(
                    data_dir=data_dir,
                    db_path=Path("unused.db"),
                    start="2024-01-01",
                    end="2024-12-31",
                    min_growth=20.0,
                    workers=1,
                    max_pages=1,
                    sleep_s=0.0,
                    limit=1,
                    max_403=1,
                )

            self.assertEqual(FakeExporter.calls, [("000002", "2024-02-01", "2024-02-29", "")])
            self.assertEqual(status["n_done_tasks"], 2)
            self.assertEqual(status["n_tasks_remaining_start"], 1)

    def test_retry_done_rechecks_missing_done_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            target.write_json(data_dir / "cninfo_earnings_announcements.json", {"items": []})
            target.write_json(data_dir / "cninfo_earnings_event_backfill_status.json", {
                "done_tasks": [["000001", "202401"]]
            })
            FakeExporter.calls = []
            event_keys = {("000001", "20240131")}
            with patch.object(target, "load_exporter", return_value=FakeExporter), \
                 patch.object(target, "load_financial_event_keys", return_value=event_keys):
                status = target.backfill(
                    data_dir=data_dir,
                    db_path=Path("unused.db"),
                    start="2024-01-01",
                    end="2024-12-31",
                    min_growth=20.0,
                    workers=1,
                    max_pages=2,
                    sleep_s=0.0,
                    limit=0,
                    max_403=1,
                    retry_done=True,
                )

            self.assertEqual(FakeExporter.calls, [("000001", "2024-01-01", "2024-01-31", "")])
            self.assertEqual(status["n_tasks_remaining_start"], 1)

    def test_stock_name_is_passed_to_exporter_for_legacy_bse_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            target.write_json(data_dir / "cninfo_earnings_announcements.json", {"items": []})
            FakeExporter.calls = []
            event_keys = {("920819", "20220811")}
            with patch.object(target, "load_exporter", return_value=FakeExporter), \
                 patch.object(target, "load_financial_event_keys", return_value=event_keys), \
                 patch.object(target, "load_stock_names", return_value={"920819": "颖泰生物"}):
                target.backfill(
                    data_dir=data_dir,
                    db_path=Path("unused.db"),
                    start="2022-01-01",
                    end="2022-12-31",
                    min_growth=20.0,
                    workers=1,
                    max_pages=1,
                    sleep_s=0.0,
                    limit=0,
                    max_403=1,
                )

            self.assertEqual(FakeExporter.calls, [("920819", "2022-08-01", "2022-08-31", "颖泰生物")])

    def test_403_circuit_breaker_does_not_start_all_queued_tasks(self):
        calls = []

        class FailingExporter:
            @staticmethod
            def collect_for_code(code, start, end, sleep_s=0.0, max_pages=1, name=""):
                calls.append(code)
                raise RuntimeError("403 Forbidden")

            @staticmethod
            def merge_items(items, rows):
                return list(items) + list(rows)

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            target.write_json(data_dir / "cninfo_earnings_announcements.json", {"items": []})
            event_keys = {
                (f"{code:06d}", "20240131")
                for code in range(1, 11)
            }
            with patch.object(target, "load_exporter", return_value=FailingExporter), \
                 patch.object(target, "load_financial_event_keys", return_value=event_keys), \
                 patch.object(target, "load_stock_names", return_value={}):
                status = target.backfill(
                    data_dir=data_dir,
                    db_path=Path("unused.db"),
                    start="2024-01-01",
                    end="2024-12-31",
                    min_growth=20.0,
                    workers=2,
                    max_pages=1,
                    sleep_s=0.0,
                    limit=0,
                    max_403=1,
                )

            self.assertTrue(status["aborted"])
            self.assertGreaterEqual(len(calls), 1)
            self.assertLessEqual(len(calls), 2)
            self.assertLess(len(calls), len(event_keys))


class BackfillMainExitTests(unittest.TestCase):
    def run_main_with_status(self, data_dir: Path, status: dict) -> int:
        argv = [
            "backfill_earnings_event_times.py",
            "--data-dir",
            str(data_dir),
            "--lock-file",
            str(data_dir / "announcements.lock"),
        ]
        with patch.object(target.sys, "argv", argv), \
             patch.object(target, "backfill", return_value=status):
            return target.main()

    def test_main_returns_zero_for_complete_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = self.run_main_with_status(
                Path(tmp),
                {"n_done_tasks": 3, "n_items": 10, "errors": [], "aborted": False},
            )

        self.assertEqual(exit_code, 0)

    def test_main_returns_nonzero_when_queries_report_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = self.run_main_with_status(
                Path(tmp),
                {
                    "n_done_tasks": 2,
                    "n_items": 9,
                    "errors": [{"task": ["000001", "202401"], "error": "timeout"}],
                    "aborted": False,
                },
            )

        self.assertEqual(exit_code, 1)

    def test_main_returns_nonzero_when_circuit_breaker_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = self.run_main_with_status(
                Path(tmp),
                {"n_done_tasks": 1, "n_items": 8, "errors": [], "aborted": True},
            )

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
