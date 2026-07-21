from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_event_risk_api_combines_placement_company_and_industry_risks(tmp_path):
    old_predict_json = app_module.PREDICT_JSON
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    data_dir = app_module.PREDICT_JSON.parent
    code = "300001"

    write_json(
        data_dir / "asset_injection.json",
        {
            "items": [
                {
                    "code": code,
                    "ts_code": f"{code}.SZ",
                    "name": "测试股份",
                    "list_date": "2026-06-20",
                    "lock": "3年",
                    "issue_price": 12.3,
                    "current_price": 10.0,
                }
            ]
        },
    )
    write_json(
        data_dir / "event_risk_news.json",
        {
            "company": [
                {
                    "code": code,
                    "date": "2026-07-01",
                    "title": "测试股份收到交易所问询",
                    "source": "公告",
                    "severity": "high",
                }
            ],
            "industry": [
                {
                    "industry": "半导体",
                    "date": "2026-07-02",
                    "title": "存储价格下行压力扩大",
                    "source": "新闻",
                    "severity": "medium",
                }
            ],
        },
    )
    write_json(
        data_dir / "cninfo_transfer.json",
        {
            "items": [
                {
                    "code": code,
                    "date": "2026-06-30",
                    "title": "Major shareholder share transfer agreement",
                    "transfer_ratio": 7.8,
                    "transfer_price": 9.8,
                    "severity": "medium",
                }
            ]
        },
    )
    write_json(
        data_dir / "cninfo_unlock.json",
        {
            "items": [
                {
                    "code": code,
                    "unlock_date": "2026-07-20",
                    "unlock_ratio": 12.5,
                    "shares": 120000000,
                    "reason": "private placement lock-up expiry",
                }
            ]
        },
    )

    try:
        response = app_module.app.test_client().get(
            f"/api/event_risk?code={code}&industry=半导体"
        )
    finally:
        app_module.PREDICT_JSON = old_predict_json

    assert response.status_code == 200
    data = response.get_json()
    assert data["code"] == code
    assert data["placement"]["status"] == "high"
    assert data["placement"]["items"][0]["price_gap_pct"] < 0
    assert data["placement"]["items"][0]["support_motive"] == "strong"
    placement = data["placement"]["items"][0]
    assert placement["lock_period"] == "3年"
    assert placement["unlock_date"] == ""
    assert placement["unlock_estimated"] is False
    assert placement["unlock_basis"] == "pending_source_evidence"
    assert data["transfer"]["status"] == "watch"
    assert data["transfer"]["items"][0]["transfer_ratio"] == 7.8
    assert data["transfer"]["items"][0]["lock_months"] == 6
    assert data["transfer"]["items"][0]["unlock_date"] == "2026-12-30"
    assert data["unlock"]["status"] == "high"
    assert data["unlock"]["items"][0]["days_to_unlock"] >= 0
    assert data["company_negative"]["status"] == "high"
    assert data["industry_negative"]["status"] == "watch"
    assert data["summary"]["decision"] == "\u6392\u9664"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_event_risk_api_combines_placement_company_and_industry_risks(Path(tmp))
    print("ok")
