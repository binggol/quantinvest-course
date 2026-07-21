from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_rolling_earnings_status as target


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_summary_reports_cooldown_and_missing_count(tmp_path: Path) -> None:
    write_json(
        tmp_path / "rolling_earnings_backtest_top50.json",
        {"announcement_time_match_counts": {"same": 3085, "nearby": 10, "missing": 3850}},
    )
    write_json(
        tmp_path / "cninfo_earnings_event_backfill_status.json",
        {
            "updated": "2026-07-05 10:31:31",
            "n_done_tasks": 4772,
            "n_items": 10777,
            "aborted": True,
            "errors": [{"error": "403"}, {"error": "403"}],
        },
    )
    write_json(
        tmp_path / "earnings_event_times_auto.json",
        {
            "last_run": "2026-07-05 10:32:11",
            "aborted": True,
            "workers": 1,
            "limit": 20,
            "sleep": 1.2,
            "max_403": 1,
        },
    )

    status = target.load_status(tmp_path)
    text = target.format_summary(status)

    assert status["missing"] == 3850
    assert status["aborted"] is True
    assert status["errors_count"] == 2
    assert "403冷却" in text
    assert "missing=3850" in text
    assert "workers=1 limit=20 sleep=1.2 max_403=1" in text
    assert "下次自动补漏: 2026-07-05 22:32:11" in text


def test_summary_reports_recovered_status(tmp_path: Path) -> None:
    write_json(
        tmp_path / "rolling_earnings_backtest_top50.json",
        {"announcement_time_match_counts": {"same": 3200, "nearby": 11, "missing": 3600}},
    )
    write_json(
        tmp_path / "cninfo_earnings_event_backfill_status.json",
        {"updated": "2026-07-05 23:00:00", "n_done_tasks": 4792, "n_items": 10830, "aborted": False},
    )
    write_json(
        tmp_path / "earnings_event_times_auto.json",
        {"last_run": "2026-07-05 23:00:05", "added": 53, "aborted": False, "workers": 5, "limit": 100},
    )

    text = target.format_summary(target.load_status(tmp_path))

    assert "运行正常" in text
    assert "新增=53" in text
    assert "missing=3600" in text
    assert "下次自动补漏: 2026-07-06 03:00:05" in text


def test_newer_manual_success_overrides_stale_auto_aborted_flag(tmp_path: Path) -> None:
    write_json(
        tmp_path / "rolling_earnings_backtest_top50.json",
        {"announcement_time_match_counts": {"same": 3085, "nearby": 10, "missing": 3850}},
    )
    write_json(
        tmp_path / "cninfo_earnings_event_backfill_status.json",
        {
            "updated": "2026-07-05 11:05:00",
            "n_done_tasks": 4773,
            "n_items": 10779,
            "aborted": False,
            "errors": [],
            "workers": 1,
            "limit": 1,
        },
    )
    write_json(
        tmp_path / "earnings_event_times_auto.json",
        {
            "last_run": "2026-07-05 10:32:11",
            "aborted": True,
            "workers": 1,
            "limit": 20,
        },
    )

    status = target.load_status(tmp_path)

    assert status["aborted"] is False
    assert status["errors_count"] == 0
    assert status["n_done_tasks"] == 4773


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_summary_reports_cooldown_and_missing_count(root / "cooldown")
        test_summary_reports_recovered_status(root / "recovered")
        test_newer_manual_success_overrides_stale_auto_aborted_flag(root / "manual")
    print("ok")
