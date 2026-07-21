from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from datetime import datetime

import pytest
from apscheduler.triggers.date import DateTrigger

import app
from scripts import build_stock_meta, update_daily


class RecordingScheduler:
    def __init__(self) -> None:
        self.jobs = []

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))


def test_daily_update_persists_success_and_preserves_it_on_later_failure(tmp_path, monkeypatch):
    status_path = tmp_path / "daily_update_status.json"
    monkeypatch.setattr(app, "DAILY_UPDATE_STATUS_PATH", status_path)
    monkeypatch.setattr(app, "PREDICT_COMPUTE_HERE", False)
    monkeypatch.setattr(update_daily, "main", lambda: None)
    monkeypatch.setattr(build_stock_meta, "main", lambda force=False: None)
    monkeypatch.setitem(sys.modules, "scripts.export_hot_avoid", SimpleNamespace(main=lambda: None))

    app.run_daily_update(attempt=1)

    succeeded = json.loads(status_path.read_text(encoding="utf-8"))
    assert succeeded["state"] == "succeeded"
    assert succeeded["attempt"] == 1
    assert succeeded["last_success_at"] == succeeded["finished_at"]
    assert not list(tmp_path.glob("*.tmp"))

    def fail_update():
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(update_daily, "main", fail_update)
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        app.run_daily_update(attempt=2)

    failed = json.loads(status_path.read_text(encoding="utf-8"))
    assert failed["state"] == "failed"
    assert failed["attempt"] == 2
    assert failed["last_success_at"] == succeeded["last_success_at"]
    assert "upstream unavailable" in failed["error"]


def test_daily_update_retry_uses_bounded_same_evening_date_jobs(tmp_path, monkeypatch):
    scheduler = RecordingScheduler()
    monkeypatch.setattr(app, "DAILY_UPDATE_STATUS_PATH", tmp_path / "daily_update_status.json")
    monkeypatch.setattr(app, "DAILY_UPDATE_MAX_RETRIES", 2)
    monkeypatch.setattr(app, "DAILY_UPDATE_RETRY_BASE_MINUTES", 10)
    monkeypatch.setattr(app, "DAILY_UPDATE_RETRY_CUTOFF_HOUR", 23)
    now = datetime(2026, 7, 13, 21, 0, 0)

    retry_at = app._schedule_daily_update_retry(
        scheduler, attempt=1, error=RuntimeError("temporary"), now=now,
    )

    assert retry_at == datetime(2026, 7, 13, 21, 10, 0)
    assert len(scheduler.jobs) == 1
    args, kwargs = scheduler.jobs[0]
    assert args[0] is app._run_scheduled_daily_update
    assert isinstance(args[1], DateTrigger)
    assert kwargs["id"] == "daily_update_retry"
    assert kwargs["replace_existing"] is True
    assert kwargs["args"] == [scheduler, 2]

    # Two configured retries means attempts 2 and 3 only; attempt 3 is final.
    assert app._schedule_daily_update_retry(
        scheduler, attempt=3, error=RuntimeError("still down"), now=now,
    ) is None
    # A retry that would cross midnight/cutoff is not queued for the next day.
    assert app._schedule_daily_update_retry(
        scheduler,
        attempt=1,
        error=RuntimeError("late failure"),
        now=datetime(2026, 7, 13, 23, 55, 0),
    ) is None
    assert len(scheduler.jobs) == 1


def test_health_reports_failed_stale_update_without_failing_liveness(tmp_path, monkeypatch):
    stock_db = tmp_path / "stock_meta.db"
    stock_db.write_bytes(b"ready")
    status_path = tmp_path / "daily_update_status.json"
    status_path.write_text(json.dumps({
        "state": "failed",
        "attempt": 3,
        "updated_at": "2026-07-13T21:45:00",
        "last_success_at": "2026-07-10T21:00:00",
        "error": "private internal detail",
    }), encoding="utf-8")
    monkeypatch.setattr(app, "STOCK_META_DB", str(stock_db))
    monkeypatch.setattr(app, "DAILY_UPDATE_STATUS_PATH", status_path)
    monkeypatch.setattr(app, "DAILY_UPDATE_STALE_HOURS", 1)
    monkeypatch.setattr(app, "_read_calendar", lambda: ["2026-07-10"])
    monkeypatch.setattr(app, "_qlib_feature_readiness", lambda _calendar: {
        "features": True,
        "benchmark_close": True,
        "benchmark_code": "sh000300",
    })

    response = app.app.test_client().get("/api/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["daily_update"]["state"] == "failed"
    assert payload["daily_update"]["stale"] is True
    assert "error" not in payload["daily_update"]


def test_instrument_metadata_max_end_ignores_malformed_rows(tmp_path):
    path = tmp_path / "all.txt"
    path.write_text(
        "sz000001\t1991-04-03\t2026-07-10\n"
        "bad-row\n"
        "sh600000\t1999-11-10\tnot-a-date\n"
        "sh000300\t2005-04-08\t2026-07-13\textra\n",
        encoding="utf-8",
    )

    assert app._instrument_metadata_max_end(path) == "2026-07-13"
    assert app._instrument_metadata_max_end(tmp_path / "missing.txt") is None


def test_startup_current_verification_clears_unknown_stale_status(tmp_path, monkeypatch):
    status_path = tmp_path / "daily_update_status.json"
    monkeypatch.setattr(app, "DAILY_UPDATE_STATUS_PATH", status_path)

    payload = app._record_current_daily_update(
        "2026-07-13",
        "startup_freshness_check",
    )

    assert payload["state"] == "current"
    assert payload["attempt"] == 0
    assert payload["verified_through"] == "2026-07-13"
    assert payload["last_success_at"] == payload["finished_at"]
    health = app._daily_update_health()
    assert health["state"] == "current"
    assert health["verified_through"] == "2026-07-13"
    assert health["stale"] is False


def test_weekly_financials_persists_last_success_across_failure(tmp_path, monkeypatch):
    from scripts import fetch_financials

    status_path = tmp_path / "weekly_financials_status.json"
    monkeypatch.setattr(app, "WEEKLY_FINANCIALS_STATUS_PATH", status_path)
    monkeypatch.setattr(fetch_financials, "fetch_all", lambda: None)

    app.run_weekly_financials_update()

    succeeded = json.loads(status_path.read_text(encoding="utf-8"))
    assert succeeded["state"] == "succeeded"
    assert succeeded["last_success_at"] == succeeded["finished_at"]
    assert not list(tmp_path.glob("*.tmp"))

    def fail_fetch():
        raise RuntimeError("financial upstream unavailable")

    monkeypatch.setattr(fetch_financials, "fetch_all", fail_fetch)
    with pytest.raises(RuntimeError, match="financial upstream unavailable"):
        app.run_weekly_financials_update()

    failed = json.loads(status_path.read_text(encoding="utf-8"))
    assert failed["state"] == "failed"
    assert failed["last_success_at"] == succeeded["last_success_at"]
    assert "financial upstream unavailable" in failed["error"]


def test_weekly_financials_startup_catchup_runs_once_per_due_week(tmp_path, monkeypatch):
    status_path = tmp_path / "weekly_financials_status.json"
    monkeypatch.setattr(app, "WEEKLY_FINANCIALS_STATUS_PATH", status_path)
    scheduler = RecordingScheduler()
    now = datetime(2026, 7, 13, 3, 0, 0)  # Monday, after the 02:00 run time.

    run_at = app._schedule_weekly_financials_catchup(scheduler, now=now)

    assert run_at == datetime(2026, 7, 13, 3, 0, 5)
    assert len(scheduler.jobs) == 1
    args, kwargs = scheduler.jobs[0]
    assert args[0] is app.run_weekly_financials_update
    assert isinstance(args[1], DateTrigger)
    assert kwargs["id"] == "weekly_financials_catchup"
    assert kwargs["replace_existing"] is True
    scheduled = json.loads(status_path.read_text(encoding="utf-8"))
    assert scheduled["state"] == "scheduled"
    assert scheduled["reason"] == "startup_catchup"

    status_path.write_text(json.dumps({
        "state": "succeeded",
        "last_success_at": "2026-07-13T02:30:00",
    }), encoding="utf-8")
    assert app._schedule_weekly_financials_catchup(
        RecordingScheduler(), now=datetime(2026, 7, 14, 8, 0, 0),
    ) is None

    status_path.unlink()
    assert app._weekly_financials_catchup_due(datetime(2026, 7, 13, 1, 59, 59)) is False
