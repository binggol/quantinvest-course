from __future__ import annotations

import json
from datetime import datetime

import numpy as np

import app


def _set_event_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "PREDICT_JSON", tmp_path / "predictions.json")
    local_root = tmp_path / "app_root"
    local_root.mkdir()
    monkeypatch.setattr(app, "__file__", str(local_root / "app.py"))
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")


def test_health_requires_a_readable_benchmark_close_bin(tmp_path, monkeypatch):
    qlib = tmp_path / "qlib"
    calendar = qlib / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("2026-07-13\n", encoding="utf-8")
    (qlib / "features").mkdir()
    stock_db = tmp_path / "stock_meta.db"
    stock_db.write_bytes(b"ready")

    monkeypatch.setattr(app, "QLIB_DATA_PATH", qlib)
    monkeypatch.setattr(app, "STOCK_META_DB", str(stock_db))
    monkeypatch.setattr(app, "DAILY_UPDATE_STATUS_PATH", tmp_path / "daily_status.json")
    monkeypatch.setattr(app, "WEEKLY_FINANCIALS_STATUS_PATH", tmp_path / "weekly_status.json")
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")

    response = app.app.test_client().get("/api/health")
    assert response.status_code == 503
    missing = response.get_json()
    assert missing["calendar_days"] == 1
    assert missing["qlib_features"] is True
    assert missing["benchmark_close"] is False
    assert missing["ok"] is False

    benchmark = qlib / "features" / "sh000905" / "close.day.bin"
    benchmark.parent.mkdir()
    np.asarray([0, 100.0], dtype="<f4").tofile(benchmark)

    response = app.app.test_client().get("/api/health")
    assert response.status_code == 200
    ready = response.get_json()
    assert ready["ok"] is True
    assert ready["benchmark_close"] is True
    assert ready["benchmark_code"] == "sh000905"
    assert ready["daily_update"]["stale"] is True


def test_transfer_and_combined_apis_report_real_source_state_and_time(tmp_path, monkeypatch):
    _set_event_data_root(tmp_path, monkeypatch)
    client = app.app.test_client()

    missing = client.get("/api/transfer_events").get_json()
    assert missing["source_state"] == "missing"
    assert missing["updated"] == ""
    combined_missing = client.get("/api/placement_transfer").get_json()
    assert combined_missing["source_state"] == "missing"
    assert combined_missing["updated"] == ""

    (tmp_path / "cninfo_transfer.json").write_text(json.dumps({
        "updated": "2026-07-13 21:05:00",
        "items": [],
    }), encoding="utf-8")
    (tmp_path / "cninfo_placement.json").write_text(json.dumps({
        "updated": "2026-07-14 06:10:00",
        "items": [],
    }), encoding="utf-8")

    transfer = client.get("/api/transfer_events").get_json()
    assert transfer["source_state"] == "ok"
    assert transfer["updated"] == "2026-07-13 21:05:00"
    combined = client.get("/api/placement_transfer").get_json()
    assert combined["source_state"] == "ok"
    assert combined["source_states"] == {"transfer": "ok", "placement": "ok"}
    assert combined["updated"] == "2026-07-14 06:10:00"

    (tmp_path / "cninfo_transfer.json").write_text("{broken", encoding="utf-8")
    invalid = client.get("/api/transfer_events").get_json()
    assert invalid["source_state"] == "invalid"
    assert invalid["updated"] == ""


def test_data_health_includes_transfer_placement_and_rolling_sources(tmp_path, monkeypatch):
    _set_event_data_root(tmp_path, monkeypatch)
    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (tmp_path / "cninfo_transfer.json").write_text(json.dumps({
        "updated": updated,
        "items": [{"code": "000001"}],
    }), encoding="utf-8")
    (tmp_path / "cninfo_placement.json").write_text(json.dumps({
        "updated": updated,
        "items": [{"code": "000002"}],
    }), encoding="utf-8")
    (tmp_path / "rolling_earnings.json").write_text(json.dumps({
        "updated": updated,
        "rolling": {"items": [{"code": "000003"}]},
    }), encoding="utf-8")
    (tmp_path / "index_inclusion.json").write_text(json.dumps({
        "updated_at": updated,
        "details": [{"code": "000004"}, {"code": "000005"}],
        "stats": {"沪深300": {"count": 2}},
    }), encoding="utf-8")
    (tmp_path / "cross_market_storage.json").write_text(json.dumps({
        "generated_at": updated,
        "leaders": [{"code": "000006"}, {"code": "000007"}],
        "upside": [{"code": "000006"}, {"code": "000007"}],
        "downside": [{"code": "000007"}, {"code": "000006"}],
    }), encoding="utf-8")

    payload = app.app.test_client().get("/api/data_health").get_json()
    items = {item["label"]: item for item in payload["items"]}

    assert items["询价转让事件"]["status"] == "fresh"
    assert items["询价转让事件"]["n"] == 1
    assert items["定增事件"]["status"] == "fresh"
    assert items["定增事件"]["n"] == 1
    assert items["滚动业绩"]["status"] == "fresh"
    assert items["滚动业绩"]["n"] == 1
    assert items["纳入研究(inclusion)"]["n"] == 2
    assert items["跨市场存储映射"]["n"] == 2


def test_data_health_includes_extended_page_sources_and_counts(tmp_path, monkeypatch):
    _set_event_data_root(tmp_path, monkeypatch)
    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fixtures = {
        "event_avoid.json": {
            "updated": updated,
            "cats": {
                "management": {"items": [{"code": "000001"}, {"code": "000002"}]},
                "reduction": {"items": [{"code": "000003"}]},
            },
        },
        "inquiry_letter.json": {"updated": updated, "n": 4},
        "investigation_avoid.json": {"updated": updated, "items": [{"code": "000005"}]},
        "lhb_avoid.json": {"updated": updated, "list": [{}, {}, {}, {}, {}]},
        "leverage_avoid.json": {"updated": updated, "n_cand": 2},
        "late_disclosure.json": {"updated": updated, "items": [{"code": "000006"}]},
        "foreign_inclusion.json": {
            "updated": updated,
            "candidates": [{"code": "000007"}, {"code": "000008"}],
            "n_cand": 99,
        },
        "repo_cancel.json": {"updated": updated, "items": [{"code": "000009"}]},
        "commit_nosell.json": {"updated": updated, "n": 3},
        "bigbath.json": {"updated": updated, "count": 4},
        "asset_injection.json": {"updated": updated, "items": [{"code": "000010"}]},
        "cninfo_earnings_announcements.json": {
            "updated": updated,
            "items": [{"code": "000011"}, {"code": "000012"}],
        },
        "etf_flow_top_signal.json": {
            "updated": updated,
            "events": [{"trade_date": "2026-07-17"}, {"trade_date": "2026-07-18"}],
        },
        "sector_etf_flow_signal.json": {
            "updated": updated,
            "events": [{"trade_date": "2026-07-18"}],
        },
        "money_outflow_signal.json": {
            "updated": updated,
            "latest_stock_outflow": [
                {"code": "000013"}, {"code": "000014"}, {"code": "000015"},
            ],
        },
        "hynix_intraday.json": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "points": [{"t": "09:00"}, {"t": "09:01"}],
        },
        "intraday_t.json": {"updated": updated, "n": 2, "rows": [{}, {}]},
    }
    for filename, data in fixtures.items():
        (tmp_path / filename).write_text(json.dumps(data), encoding="utf-8")

    payload = app.app.test_client().get("/api/data_health").get_json()
    items = {item["label"]: item for item in payload["items"]}

    # The original 17 entries remain and the 17 page-level sources are appended.
    assert payload["n"] == 34
    expected_counts = {
        "事件避雷": 3,
        "问询函避雷": 4,
        "立案调查避雷": 1,
        "龙虎榜净卖出": 5,
        "融资透支避雷": 2,
        "年报晚披露": 1,
        "境外指数纳入": 2,
        "注销型回购": 1,
        "承诺不减持": 3,
        "洗大澡反弹": 4,
        "资产注入定增": 1,
        "巨潮业绩公告时间": 2,
        "宽基ETF见顶风险": 2,
        "行业ETF见顶风险": 1,
        "资金流出验证": 3,
        "海力士盘中分时": 2,
        "超短线做T": 2,
    }
    for label, count in expected_counts.items():
        assert items[label]["status"] == "fresh"
        assert items[label]["n"] == count

    for label in expected_counts:
        expected_freshness = 8 if label in {"年报晚披露", "境外指数纳入"} else 2 if label in {
            "海力士盘中分时", "超短线做T",
        } else 3
        assert items[label]["max_fresh"] == expected_freshness


def test_index_inclusion_pro_distinguishes_missing_invalid_and_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    path = tmp_path / "index_inclusion_pro.json"
    client = app.app.test_client()

    missing = client.get("/api/index_inclusion_pro").get_json()
    assert missing["source_state"] == "missing"
    assert missing["buy_today"] == []
    assert missing["updated"] == ""

    path.write_text("{broken", encoding="utf-8")
    invalid_json = client.get("/api/index_inclusion_pro").get_json()
    assert invalid_json["source_state"] == "invalid"

    path.write_text(json.dumps({"updated": "2026-07-14", "buy_today": []}), encoding="utf-8")
    invalid_schema = client.get("/api/index_inclusion_pro").get_json()
    assert invalid_schema["source_state"] == "invalid"

    path.write_text(json.dumps({
        "updated": "2026-07-14 07:00",
        "today": "2026-07-14",
        "buy_today": [],
        "sell_today": [],
        "holdings": [],
        "watch": [],
    }), encoding="utf-8")
    ok = client.get("/api/index_inclusion_pro").get_json()
    assert ok["source_state"] == "ok"
    assert ok["updated"] == "2026-07-14 07:00"
