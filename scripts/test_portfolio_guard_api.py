from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from scripts.access_policy import PAGE_FEATURES


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def test_portfolio_guard_combines_sources_and_risk_overlays(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    data_dir = app_module.PREDICT_JSON.parent

    write_json(
        data_dir / "combo_holdings.json",
        {
            "updated": "2026-07-06 09:00",
            "holdings": [
                {"code": "000001.SZ", "name": "Good", "weight_pct": 4.0},
                {"code": "000002.SZ", "name": "Unlock", "weight_pct": 4.0},
                {"code": "000003.SZ", "name": "Bad", "weight_pct": 4.0},
            ],
        },
    )
    write_json(data_dir / "hot_avoid.json", {"items": [{"code": "000002.SZ", "name": "Unlock"}]})
    write_json(data_dir / "investigation_avoid.json", {"items": [{"code": "000003.SZ", "name": "Bad"}]})
    write_json(
        data_dir / "cninfo_transfer.json",
        {"items": [{"code": "000002", "ann_date": "2026-01-20", "unlock_date": "2026-07-20", "title": "transfer"}]},
    )
    write_json(
        data_dir / "top_risk_snapshot.json",
        {"market": {"level": "watch", "score": 35, "as_of": "2026-07-06"}},
    )

    try:
        response = app_module.app.test_client().get("/api/portfolio_guard")
    finally:
        app_module.PREDICT_JSON = old_predict_json

    assert response.status_code == 200
    data = response.get_json()
    assert data["summary"]["n_total"] == 3
    assert data["market"]["level"] == "watch"

    rows = {row["code6"]: row for row in data["items"]}
    assert rows["000001"]["target_pct"] == 3.0
    assert rows["000001"]["risk_penalty"] == 25.0
    assert rows["000002"]["target_pct"] < 1.0
    assert rows["000002"]["unlock_info"]["status"] == "watch"
    assert rows["000002"]["reasons"]
    assert rows["000003"]["target_pct"] == 0
    assert rows["000003"]["risk_penalty"] == 100.0
    assert rows["000003"]["reasons"]


def test_portfolio_guard_page_has_endpoint_and_table() -> None:
    html = Path("templates/portfolio_guard.html").read_text(encoding="utf-8")
    assert "/api/portfolio_guard" in html
    assert '{% include "_nav.html" %}' in html
    assert PAGE_FEATURES["/portfolio-guard"] == "internal_operations"
    assert "target_pct" in html


def test_event_avoid_categories_are_flattened_for_risk_consumers() -> None:
    rows = app_module._eventrisk_rows(
        {
            "cats": {
                "management": {
                    "items": [{"code": "000001.SZ", "in_window": True}]
                },
                "supply": {
                    "rows": [{"code": "000002.SZ", "in_window": False}]
                },
            }
        }
    )

    assert [(row["code"], row["category"]) for row in rows] == [
        ("000001.SZ", "management"),
        ("000002.SZ", "supply"),
    ]


def test_portfolio_guard_ignores_explicitly_expired_avoid_rows(tmp_path: Path) -> None:
    payloads = {
        "hot_avoid.json": {
            "items": [
                {"code": "000001.SZ", "in_window": False},
                {"code": "000002.SZ", "in_window": True},
                {"code": "000003.SZ"},
            ]
        },
        "investigation_avoid.json": {
            "items": [
                {"code": "000004.SZ", "in_blacklist": False},
                {"code": "000005.SZ", "in_blacklist": True},
                {"code": "000006.SZ"},
                {"code": "000007.SZ", "in_window": False, "in_blacklist": True},
            ]
        },
        "snowball_avoid.json": {
            "items": [
                {"code": "000008.SZ", "expired": True},
                {"code": "000009.SZ", "expired": False},
                {"code": "000010.SZ"},
            ]
        },
        "event_avoid.json": {
            "cats": {
                "event": {
                    "items": [
                        {"code": "000011.SZ", "in_window": False},
                        {"code": "000012.SZ", "in_window": True},
                        {"code": "000013.SZ"},
                    ]
                }
            }
        },
    }
    for filename in (
        "hot_avoid.json",
        "margin_avoid.json",
        "fraud_avoid.json",
        "investigation_avoid.json",
        "lhb_avoid.json",
        "leverage_avoid.json",
        "snowball_avoid.json",
        "event_avoid.json",
    ):
        write_json(tmp_path / filename, payloads.get(filename, {"items": []}))

    avoid = app_module._pg_avoid_codes(tmp_path)

    assert set(avoid) == {
        "000002",
        "000003",
        "000005",
        "000006",
        "000009",
        "000010",
        "000012",
        "000013",
    }
    assert {flag["key"] for flag in avoid["000012"]} == {"event"}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_portfolio_guard_combines_sources_and_risk_overlays(Path(tmp))
        test_portfolio_guard_ignores_explicitly_expired_avoid_rows(Path(tmp))
    test_portfolio_guard_page_has_endpoint_and_table()
    test_event_avoid_categories_are_flattened_for_risk_consumers()
    print("portfolio guard tests ok")
