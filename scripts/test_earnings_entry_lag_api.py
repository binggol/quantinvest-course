from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def sample_payload(min_growth: float, mean_20: float = 1.23) -> dict:
    return {
        "updated": "2026-07-10 10:15:00",
        "method": "entry lag test",
        "params": {
            "min_growth": min_growth,
            "topn_per_announcement_day": 50,
            "horizons": [5, 10, 20, 60],
            "entry_lags": [1, 2, 3, 4, 5],
            "period_suffix": "0630",
        },
        "n_source_events": 100,
        "n_codes": 80,
        "date_range": {"ann_start": "20220719", "ann_end": "20250830"},
        "entry_lag_analysis": {
            "1": {
                "label": "公告后第1个交易日开盘买入",
                "n_events": 90,
                "summary": {
                    "5": {"n": 90, "mean_pct": -0.1, "win_rate_pct": 45.0},
                    "10": {"n": 90, "mean_pct": -0.2, "win_rate_pct": 44.0},
                    "20": {"n": 90, "mean_pct": 0.8, "win_rate_pct": 43.0},
                    "60": {"n": 90, "mean_pct": 8.1, "win_rate_pct": 54.0},
                },
            },
            "5": {
                "label": "公告后第5个交易日开盘买入",
                "n_events": 90,
                "summary": {
                    "5": {"n": 90, "mean_pct": 0.2, "win_rate_pct": 47.0},
                    "10": {"n": 90, "mean_pct": -0.6, "win_rate_pct": 41.0},
                    "20": {"n": 90, "mean_pct": mean_20, "win_rate_pct": 45.0},
                    "60": {"n": 90, "mean_pct": 9.3, "win_rate_pct": 56.0},
                },
            },
        },
    }


def test_earnings_entry_lag_api_reads_three_threshold_files(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    data_dir = app_module.PREDICT_JSON.parent
    write_json(data_dir / "rolling_earnings_interim_entry_lag_top50.json", sample_payload(20, 1.2))
    write_json(data_dir / "rolling_earnings_interim_entry_lag_g50_top50.json", sample_payload(50, 1.5))
    write_json(data_dir / "rolling_earnings_interim_entry_lag_g100_top50.json", sample_payload(100, 0.9))

    try:
        response = app_module.app.test_client().get("/api/earnings_entry_lag")
    finally:
        app_module.PREDICT_JSON = old_predict_json

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert [v["key"] for v in data["variants"]] == ["g20", "g50", "g100"]
    assert data["variants"][0]["label"] == "扣非增速 >20%"
    assert data["variants"][1]["params"]["min_growth"] == 50
    assert data["variants"][0]["best_by_horizon"]["20"]["lag"] == "5"
    assert data["variants"][0]["best_by_horizon"]["20"]["mean_pct"] == 1.2


def test_earnings_entry_lag_page_and_template_text() -> None:
    response = app_module.app.test_client().get("/earnings-entry-lag")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "中报入场日回测" in html
    assert "/api/earnings_entry_lag" in html
    assert "重新测算" in html
    assert "扣非增速 >20%" in html


def test_earnings_entry_lag_rebuild_runs_three_thresholds(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    old_runner = getattr(app_module, "_run_earnings_entry_lag_backtest", None)
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    calls = []

    def fake_runner(min_growth: float, out_path: Path) -> dict:
        calls.append((min_growth, out_path.name))
        payload = sample_payload(min_growth)
        write_json(out_path, payload)
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    app_module._run_earnings_entry_lag_backtest = fake_runner
    try:
        response = app_module.app.test_client().post("/api/earnings_entry_lag/rebuild")
    finally:
        app_module.PREDICT_JSON = old_predict_json
        if old_runner is None:
            delattr(app_module, "_run_earnings_entry_lag_backtest")
        else:
            app_module._run_earnings_entry_lag_backtest = old_runner

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert calls == [
        (20.0, "rolling_earnings_interim_entry_lag_top50.json"),
        (50.0, "rolling_earnings_interim_entry_lag_g50_top50.json"),
        (100.0, "rolling_earnings_interim_entry_lag_g100_top50.json"),
    ]
    assert len(data["variants"]) == 3


if __name__ == "__main__":
    with __import__("tempfile").TemporaryDirectory() as tmp:
        test_earnings_entry_lag_api_reads_three_threshold_files(Path(tmp))
        test_earnings_entry_lag_rebuild_runs_three_thresholds(Path(tmp))
    test_earnings_entry_lag_page_and_template_text()
    print("earnings entry lag api tests ok")
