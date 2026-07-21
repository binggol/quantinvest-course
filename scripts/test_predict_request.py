from __future__ import annotations

import json
from pathlib import Path

import app as app_module


def test_predict_request_is_post_only_and_writes_nontraining_request(
    tmp_path: Path, monkeypatch
) -> None:
    request_path = tmp_path / "predict_request.json"
    monkeypatch.setattr(app_module, "PREDICT_REQUEST", request_path)
    monkeypatch.setattr(app_module, "PREDICT_COMPUTE_HERE", False)
    client = app_module.app.test_client()

    assert client.get("/api/predict/request?retrain=0&update=0").status_code == 405
    response = client.post("/api/predict/request?retrain=0&update=0")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["retrain"] is False
    assert payload["update"] is False


def test_predict_template_posts_refresh_request() -> None:
    source = (Path(__file__).parents[1] / "templates" / "predict.html").read_text(
        encoding="utf-8-sig"
    )
    assert "/api/predict/request?retrain=" in source
    assert '{method: "POST"}' in source


def test_data_health_counts_rdagent_hits(tmp_path: Path, monkeypatch) -> None:
    rdagent_path = tmp_path / "rdagent.json"
    rdagent_path.write_text(
        json.dumps(
            {
                "as_of": "2026-07-09",
                "hits": [{"code": "sz000001"}, {"code": "sh600000"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "RDAGENT_JSON", rdagent_path)

    payload = app_module.app.test_client().get("/api/data_health").get_json()
    prediction = next(
        item for item in payload["items"] if item["label"] == "下一日预测(沪深300)"
    )

    assert prediction["updated"] == "2026-07-09"
    assert prediction["n"] == 2
