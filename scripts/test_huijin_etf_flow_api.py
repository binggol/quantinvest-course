from __future__ import annotations

import json

import app as app_module


def test_huijin_etf_flow_page_and_api(monkeypatch, tmp_path):
    payload = {
        "updated": "2026-07-20T12:00:00",
        "as_of": "2026-07-17",
        "aggregate_series": [{"date": "2026-07-17", "share_index": 100.0}],
        "etfs": [{"code": "510300.SH"}],
        "backtest": {"verdict": "样本不足"},
    }
    path = tmp_path / "huijin_etf_flow.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(app_module, "HUJIN_ETF_FLOW_JSON", path)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    page = client.get("/huijin-etf-flow")
    response = client.get("/api/huijin_etf_flow")

    assert page.status_code == 200
    assert "国家队 ETF 资金代理" in page.get_data(as_text=True)
    assert response.status_code == 200
    assert response.get_json()["etfs"][0]["code"] == "510300.SH"


def test_huijin_etf_flow_api_returns_503_when_payload_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "HUJIN_ETF_FLOW_JSON", tmp_path / "missing.json")
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get("/api/huijin_etf_flow")

    assert response.status_code == 503
    assert response.get_json()["ok"] is False


def test_huijin_etf_flow_fund_series_endpoint(monkeypatch, tmp_path):
    series_payload = {
        "updated": "2026-07-20T12:00:00",
        "as_of": "2026-07-17",
        "funds": {
            "510300.SH": {
                "code": "510300.SH",
                "name": "沪深300ETF",
                "category": "broad",
                "series": [
                    {"date": "2026-07-16", "share_yi": 900.0, "aum_yi": 3600.0, "net_creation_yi": 4.0},
                    {"date": "2026-07-17", "share_yi": 901.0, "aum_yi": 3605.0, "net_creation_yi": 1.0},
                ],
            }
        },
    }
    path = tmp_path / "huijin_etf_share_series.json"
    path.write_text(json.dumps(series_payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(app_module, "HUJIN_ETF_SERIES_JSON", path)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    response = client.get("/api/huijin_etf_flow/etf/510300.SH")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["name"] == "沪深300ETF"
    assert len(body["series"]) == 2
    assert body["series"][0]["share_yi"] == 900.0


def test_huijin_etf_flow_fund_series_unknown_code_returns_404(monkeypatch, tmp_path):
    path = tmp_path / "huijin_etf_share_series.json"
    path.write_text(json.dumps({"funds": {"510300.SH": {"series": []}}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(app_module, "HUJIN_ETF_SERIES_JSON", path)
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get("/api/huijin_etf_flow/etf/999999.SH")

    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_huijin_etf_flow_fund_series_returns_503_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "HUJIN_ETF_SERIES_JSON", tmp_path / "missing.json")
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get("/api/huijin_etf_flow/etf/510300.SH")

    assert response.status_code == 503
    assert response.get_json()["ok"] is False

