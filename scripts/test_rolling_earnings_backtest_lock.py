from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import backtest_rolling_earnings as target


def _base_args(tmp_path: Path) -> list[str]:
    announcement = tmp_path / "cninfo_earnings_announcements.json"
    announcement.write_text(
        json.dumps({"updated": "2026-07-14 08:00:00", "items": []}),
        encoding="utf-8",
    )
    return [
        "--announcement-cache", str(announcement),
        "--out", str(tmp_path / "rolling_earnings_backtest_top50.json"),
        "--lock-file", str(tmp_path / "rolling_earnings_backtest.lock"),
        "--status-file", str(tmp_path / "rolling_earnings_backtest_status.json"),
        "--reason", "test-run",
    ]


def test_busy_lock_does_not_overwrite_active_status(tmp_path: Path) -> None:
    args = _base_args(tmp_path)
    lock_path = tmp_path / "rolling_earnings_backtest.lock"
    status_path = tmp_path / "rolling_earnings_backtest_status.json"
    status_path.write_text('{"state":"running","pid":123}', encoding="utf-8")
    owner = target.acquire_backtest_lock(lock_path, 0, "first-run")
    try:
        assert target.main(args) == target.LOCK_BUSY_EXIT
        assert json.loads(status_path.read_text(encoding="utf-8")) == {
            "state": "running",
            "pid": 123,
        }
    finally:
        target.release_backtest_lock(lock_path, owner)


def test_success_status_binds_output_to_announcement_fingerprint(tmp_path: Path) -> None:
    args = _base_args(tmp_path)
    result = {"n_events": 3, "summary": {"mean": 1.25}}
    with patch.object(target, "run_backtest", return_value=result):
        assert target.main(args) == 0

    status = json.loads(
        (tmp_path / "rolling_earnings_backtest_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "done"
    assert status["stage"] == "complete"
    assert status["reason"] == "test-run"
    assert status["source"]["updated"] == "2026-07-14 08:00:00"
    assert len(status["source"]["sha256"]) == 64
    assert len(status["output"]["sha256"]) == 64
    assert not (tmp_path / "rolling_earnings_backtest.lock").exists()


def test_source_change_prevents_stale_result_from_overwriting_output(tmp_path: Path) -> None:
    args = _base_args(tmp_path)
    announcement = tmp_path / "cninfo_earnings_announcements.json"
    output = tmp_path / "rolling_earnings_backtest_top50.json"
    output.write_text('{"version":"previous"}', encoding="utf-8")

    def change_source(*_args, **_kwargs):
        announcement.write_text(
            json.dumps({"updated": "2026-07-14 08:01:00", "items": [{"code": "000001"}]}),
            encoding="utf-8",
        )
        return {"version": "stale-candidate"}

    with patch.object(target, "run_backtest", side_effect=change_source):
        assert target.main(args) == target.SOURCE_CHANGED_EXIT

    assert json.loads(output.read_text(encoding="utf-8")) == {"version": "previous"}
    status = json.loads(
        (tmp_path / "rolling_earnings_backtest_status.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "stale"
    assert status["stage"] == "source-changed"
    assert status["source_current"]["sha256"] != status["source"]["sha256"]
