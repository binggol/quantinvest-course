from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_backtest_api_includes_event_backfill_status(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    data_dir = app_module.PREDICT_JSON.parent
    write_json(
        data_dir / "rolling_earnings_backtest_top50.json",
        {
            "updated": "2026-07-05 10:28:09",
            "summary": {"10": {"n": 1, "mean_pct": 1.0}},
            "announcement_time_match_counts": {"same": 1, "nearby": 0, "missing": 2},
        },
    )
    write_json(
        data_dir / "cninfo_earnings_event_backfill_status.json",
        {
            "updated": "2026-07-05 10:31:31",
            "n_done_tasks": 4772,
            "n_items": 10777,
            "aborted": True,
            "errors": [{"error": "403 Client Error"}],
            "workers": 20,
            "limit": 100,
        },
    )
    write_json(
        data_dir / "earnings_event_times_auto.json",
        {
            "last_run": "2026-07-05 10:32:11",
            "aborted": True,
            "added": 0,
            "workers": 1,
            "limit": 20,
        },
    )

    try:
        response = app_module.app.test_client().get("/api/rolling_earnings/backtest")
    finally:
        app_module.PREDICT_JSON = old_predict_json

    assert response.status_code == 200
    data = response.get_json()
    assert data["event_backfill_status"]["n_done_tasks"] == 4772
    assert data["event_backfill_status"]["errors_count"] == 1
    assert data["event_backfill_auto"]["last_run"] == "2026-07-05 10:32:11"
    assert data["event_backfill_auto"]["workers"] == 1


def test_backtest_api_falls_back_to_app_data_for_event_status(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    old_file = app_module.__file__
    app_module.PREDICT_JSON = tmp_path / "missing_shared" / "predictions.json"
    app_module.__file__ = str(tmp_path / "app.py")
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "rolling_earnings_backtest_top50.json",
        {
            "updated": "2026-07-05 10:28:09",
            "announcement_time_match_counts": {"same": 2, "nearby": 0, "missing": 3},
        },
    )
    write_json(
        data_dir / "cninfo_earnings_event_backfill_status.json",
        {
            "updated": "2026-07-05 10:31:31",
            "n_done_tasks": 4772,
            "n_items": 10777,
            "aborted": True,
            "errors": [{"error": "403 Client Error"}, {"error": "403 Client Error"}],
            "workers": 1,
            "limit": 20,
        },
    )
    write_json(
        data_dir / "earnings_event_times_auto.json",
        {
            "last_run": "2026-07-05 10:32:11",
            "reason": "cooldown",
            "aborted": True,
            "added": 0,
            "workers": 1,
            "limit": 20,
        },
    )

    try:
        response = app_module.app.test_client().get("/api/rolling_earnings/backtest")
    finally:
        app_module.PREDICT_JSON = old_predict_json
        app_module.__file__ = old_file

    assert response.status_code == 200
    data = response.get_json()
    assert data["announcement_time_match_counts"]["missing"] == 3
    assert data["event_backfill_status"]["n_done_tasks"] == 4772
    assert data["event_backfill_status"]["errors_count"] == 2
    assert data["event_backfill_auto"]["reason"] == "cooldown"


def test_backtest_api_uses_auto_params_when_status_lacks_params(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    data_dir = app_module.PREDICT_JSON.parent
    write_json(
        data_dir / "rolling_earnings_backtest_top50.json",
        {"updated": "2026-07-05", "announcement_time_match_counts": {"missing": 3850}},
    )
    write_json(
        data_dir / "cninfo_earnings_event_backfill_status.json",
        {
            "updated": "2026-07-05 10:31:31",
            "n_done_tasks": 4772,
            "n_items": 10777,
            "aborted": True,
            "errors": [{"error": "403 Client Error"}],
        },
    )
    write_json(
        data_dir / "earnings_event_times_auto.json",
        {
            "last_run": "2026-07-05 10:32:11",
            "aborted": True,
            "added": 0,
            "workers": 1,
            "limit": 20,
            "sleep": 1.2,
            "max_403": 1,
        },
    )

    try:
        response = app_module.app.test_client().get("/api/rolling_earnings/backtest")
    finally:
        app_module.PREDICT_JSON = old_predict_json

    assert response.status_code == 200
    status = response.get_json()["event_backfill_status"]
    assert status["workers"] == 1
    assert status["limit"] == 20
    assert status["sleep"] == 1.2
    assert status["max_403"] == 1


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_backtest_api_includes_event_backfill_status(Path(tmp))
        test_backtest_api_falls_back_to_app_data_for_event_status(Path(tmp))
        test_backtest_api_uses_auto_params_when_status_lacks_params(Path(tmp))
    print("ok")
