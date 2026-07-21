from __future__ import annotations

import json
from pathlib import Path

import app as app_module


def _configure_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "RDAGENT_REQUEST", tmp_path / "rdagent_request.json")
    monkeypatch.setattr(app_module, "RDAGENT_STATUS", tmp_path / "rdagent_status.json")
    monkeypatch.setattr(app_module, "RDAGENT_JSON", tmp_path / "rdagent.json")


def test_rdagent_request_is_published_complete_and_never_overwritten(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_paths(tmp_path, monkeypatch)

    first = app_module._queue_rdagent_request({"mine": True, "loop_n": 2})
    assert first is not None
    stored = json.loads(app_module.RDAGENT_REQUEST.read_text(encoding="utf-8"))
    assert stored == first
    assert len(stored["request_id"]) == 32
    assert not list(tmp_path.glob(".rdagent_request.json.*.tmp"))

    second = app_module._queue_rdagent_request({"mine": True, "loop_n": 3})
    assert second is None
    assert json.loads(app_module.RDAGENT_REQUEST.read_text(encoding="utf-8")) == first


def test_rdagent_submit_rejects_a_second_job_with_http_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_paths(tmp_path, monkeypatch)
    client = app_module.app.test_client()

    first_response = client.post("/api/rdagent/mine?loop_n=2")
    assert first_response.status_code == 200
    first = first_response.get_json()
    assert first["ok"] is True
    assert first["request_id"]
    stored_before = app_module.RDAGENT_REQUEST.read_bytes()

    duplicate_response = client.post("/api/rdagent/request?model=lgb")
    assert duplicate_response.status_code == 409
    assert duplicate_response.get_json()["ok"] is False
    assert app_module.RDAGENT_REQUEST.read_bytes() == stored_before


def test_rdagent_status_is_correlated_with_the_current_request(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_paths(tmp_path, monkeypatch)
    request_payload = {
        "mine": True,
        "request_id": "a" * 32,
        "requested_at": "2026-07-14 13:00:00",
    }
    app_module.RDAGENT_REQUEST.write_text(
        json.dumps(request_payload), encoding="utf-8"
    )
    app_module.RDAGENT_STATUS.write_text(
        json.dumps({
            "state": "error",
            "msg": "factor_analysis exit 1",
            "updated_at": "2026-07-14 12:11:00",
        }),
        encoding="utf-8",
    )
    client = app_module.app.test_client()

    queued = client.get("/api/rdagent").get_json()
    assert queued["rd_pending"] is True
    assert queued["rd_status"]["state"] == "queued"
    assert queued["rd_status"]["request_id"] == request_payload["request_id"]
    assert "factor_analysis" not in queued["rd_status"]["msg"]

    app_module.RDAGENT_STATUS.write_text(
        json.dumps({
            "state": "running",
            "msg": "progress without identity",
            "updated_at": "2026-07-14 13:00:10",
        }),
        encoding="utf-8",
    )
    missing_identity = client.get("/api/rdagent").get_json()
    assert missing_identity["rd_status"]["state"] == "queued"
    assert missing_identity["rd_status"]["request_id"] == request_payload["request_id"]

    app_module.RDAGENT_STATUS.write_text(
        json.dumps({
            "state": "running",
            "msg": "progress for another request",
            "request_id": "b" * 32,
            "updated_at": "2026-07-14 13:00:12",
        }),
        encoding="utf-8",
    )
    wrong_identity = client.get("/api/rdagent").get_json()
    assert wrong_identity["rd_status"]["state"] == "queued"

    app_module.RDAGENT_STATUS.write_text(
        json.dumps({
            "state": "running",
            "msg": "mine: 检查模型网关",
            "request_id": request_payload["request_id"],
            "requested_at": request_payload["requested_at"],
            "updated_at": "2026-07-14 13:00:15",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    running = client.get("/api/rdagent").get_json()
    assert running["rd_status"]["state"] == "running"

    app_module.RDAGENT_REQUEST.unlink()
    orphaned = client.get("/api/rdagent").get_json()
    assert orphaned["rd_pending"] is False
    assert orphaned["rd_status"]["state"] == "error"
    assert "失去对应请求" in orphaned["rd_status"]["msg"]


def test_rdagent_page_checks_post_result_and_disables_fetch_cache() -> None:
    source = (app_module.APP_ROOT / "templates" / "rdagent.html").read_text(
        encoding="utf-8"
    )
    assert "!response.ok || !data.ok" in source
    assert 'fetch("/api/rdagent", {cache:"no-store"})' in source
