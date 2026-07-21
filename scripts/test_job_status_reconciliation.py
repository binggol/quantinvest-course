from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import app
from scripts import export_index_inclusion


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_status_with_request_resets_only_orphaned_running_state(tmp_path):
    status_path = tmp_path / "status.json"
    request_path = tmp_path / "request.json"
    _write_json(status_path, {
        "state": "running",
        "msg": "13/30 csi500 / catboost",
        "updated_at": "2026-06-22 23:37:16",
    })

    status, pending = app._status_with_request(status_path, request_path)
    assert pending is False
    assert status["state"] == "error"
    assert status["stale"] is True
    assert "13/30" in status["msg"]

    request_path.write_text("{}", encoding="utf-8")
    status, pending = app._status_with_request(status_path, request_path)
    assert pending is True
    assert status["state"] == "running"


def test_index_inclusion_status_reconciles_newer_research_artifact(tmp_path, monkeypatch):
    status_path = tmp_path / "inclusion_status.json"
    research_path = tmp_path / "index_inclusion.json"
    _write_json(status_path, {
        "state": "done",
        "msg": "纳入数据已更新 (研究表失败)",
        "updated_at": "2026-06-28 08:04:47",
    })
    _write_json(research_path, {"stats": {}, "details": []})
    os.utime(status_path, (1000, 1000))
    os.utime(research_path, (2000, 2000))
    monkeypatch.setattr(app, "INCLUSION_STATUS", status_path)
    monkeypatch.setattr(app, "PREDICT_JSON", tmp_path / "predictions.json")

    payload = app.app.test_client().get("/api/index_inclusion/status").get_json()

    assert payload["state"] == "done"
    assert payload["research_state"] == "done"
    assert payload["reconciled"] is True
    assert "后续任务中补齐" in payload["msg"]


def test_pipeline_status_does_not_label_errors_as_completed(tmp_path, monkeypatch):
    model = tmp_path / "rdagent_status.json"
    report = tmp_path / "batch_gen_status.json"
    thesis = tmp_path / "thesis_status.json"
    _write_json(model, {"state": "error", "msg": "token rejected"})
    _write_json(report, {"state": "done", "msg": "8/8"})
    _write_json(thesis, {"state": "done", "msg": "done"})
    monkeypatch.setattr(app, "RDAGENT_STATUS", model)
    monkeypatch.setattr(app, "BATCH_GEN_STATUS", report)
    monkeypatch.setattr(app, "THESIS_STATUS", thesis)

    payload = app.app.test_client().get("/api/pipeline_status").get_json()

    assert payload["active"] is False
    assert payload["stage"] == "error"
    assert "失败" in payload["label"]


def test_index_inclusion_uses_configured_qlib_path_and_current_end_date(tmp_path, monkeypatch):
    dates = pd.bdate_range("2025-11-03", "2026-03-31")
    close = np.linspace(10.0, 20.0, len(dates))
    features = pd.DataFrame(
        {"$close": close},
        index=pd.MultiIndex.from_product([["SH600000"], dates]),
    )
    calls = {}

    class FakeD:
        @staticmethod
        def features(codes, fields, start_time, end_time):
            calls["features_end"] = end_time
            return features

        @staticmethod
        def calendar(start_time, end_time):
            calls["calendar_end"] = end_time
            return dates

    qlib_module = types.ModuleType("qlib")
    qlib_module.init = lambda **kwargs: calls.setdefault("init", kwargs)
    qlib_data_module = types.ModuleType("qlib.data")
    qlib_data_module.D = FakeD
    monkeypatch.setitem(sys.modules, "qlib", qlib_module)
    monkeypatch.setitem(sys.modules, "qlib.data", qlib_data_module)
    monkeypatch.setattr(export_index_inclusion, "QLIB_DATA_PATH", tmp_path / "cn_data")
    events = pd.DataFrame([{
        "ts_code": "600000.SH",
        "index_name": "沪深300",
        "inclusion_date": "2026-01-15",
        "year_month": "2026-01",
    }])

    result = export_index_inclusion.calculate_returns(events)

    assert not result.empty
    assert calls["init"]["provider_uri"] == str(tmp_path / "cn_data")
    assert calls["features_end"] == calls["calendar_end"]
    assert calls["features_end"] == datetime.now().strftime("%Y-%m-%d")


def test_watcher_resets_arena_states_and_reports_token_rejection():
    source = Path("scripts/watch_predict_pc.ps1").read_text(encoding="utf-8")
    reset_block = source[source.index("function Reset-StaleRunning"):source.index("if (-not (Test-Path $shared))")]
    for variable in ("$arenaStatusFile", "$uarenaStatusFile", "$barenaStatusFile"):
        assert variable in reset_block
    assert "Invalid token|token_rejected" in source
    assert "research_ok = $okR" in source
    assert "$baFailed" in source
    assert "批次擂台部分完成" in source
