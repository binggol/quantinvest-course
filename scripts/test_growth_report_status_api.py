import json

import app as app_module
from scripts import build_growth_report_queue


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_growth_status_ignores_an_unrelated_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "BATCH_GEN_STATUS", tmp_path / "batch_gen_status.json")
    _write(tmp_path / "growth_report_queue.json", {
        "job_id": "growth-new", "n": 2, "items": [], "updated": "2026-07-12 16:23:07",
    })
    _write(tmp_path / "batch_gen_status.json", {
        "job_id": "advisor-old", "source": "advisor_pro", "state": "done", "n": 8,
    })

    app_module.app.config.update(TESTING=True, CSRF_TESTING=False)
    payload = app_module.app.test_client().get("/api/report/growth_after_close/status").get_json()

    assert payload["batch_status"]["state"] == "queued"
    assert payload["batch_status"]["job_id"] == "growth-new"
    assert payload["batch_status"]["n"] == 2


def test_growth_status_keeps_matching_error_details(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "BATCH_GEN_STATUS", tmp_path / "batch_gen_status.json")
    _write(tmp_path / "growth_report_queue.json", {"job_id": "growth-same", "n": 1, "items": []})
    _write(tmp_path / "batch_gen_status.json", {
        "job_id": "growth-same", "source": "growth_after_close", "state": "done",
        "n": 1, "ok_r": 0, "fail": ["000001.SZ"],
        "errors": [{"code": "000001.SZ", "reason": "LLM网关不可用"}],
    })

    app_module.app.config.update(TESTING=True, CSRF_TESTING=False)
    payload = app_module.app.test_client().get("/api/report/growth_after_close/status").get_json()

    assert payload["batch_status"]["fail"] == ["000001.SZ"]
    assert payload["batch_status"]["errors"][0]["reason"] == "LLM网关不可用"


def test_growth_status_empty_queue_is_not_queued(tmp_path, monkeypatch):
    """n=0 的空队列没有任务可领, 不应显示"已排队 等待PC研报监听器领取任务"."""
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "BATCH_GEN_STATUS", tmp_path / "batch_gen_status.json")
    _write(tmp_path / "growth_report_queue.json", {
        "job_id": "growth-empty", "n": 0, "items": [], "updated": "2026-07-21 19:20:00",
    })

    app_module.app.config.update(TESTING=True, CSRF_TESTING=False)
    payload = app_module.app.test_client().get("/api/report/growth_after_close/status").get_json()

    assert payload["batch_status"]["state"] != "queued"
    assert payload["batch_status"]["n"] == 0


def test_growth_post_returns_queued_status(monkeypatch):
    monkeypatch.setattr(build_growth_report_queue, "build_queue", lambda *args, **kwargs: {
        "job_id": "growth-post", "updated": "2026-07-12 16:23:07", "n": 1,
        "items": [{"code": "000001.SZ"}], "window": {"start": "s", "end": "e"},
    })
    app_module.app.config.update(TESTING=True, CSRF_TESTING=False)

    response = app_module.app.test_client().post("/api/report/growth_after_close")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["batch_status"]["state"] == "queued"
    assert payload["batch_status"]["job_id"] == "growth-post"
