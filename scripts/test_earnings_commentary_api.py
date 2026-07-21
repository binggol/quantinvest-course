from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import app as app_module
from scripts.access_policy import PAGE_FEATURES


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _forecast_payload() -> dict:
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "periods": ["20260630", "20260930"],
        "items": [
            {
                "code": "000001",
                "ts_code": "000001.SZ",
                "name": "测试银行",
                "idx": "csi300",
                "type": "预增",
                "pos": True,
                "p_chg_min": 20,
                "p_chg_max": 40,
                "net_min": 10000,
                "net_max": 20000,
                "ann_date": "20260703",
                "period": "20260630",
                "summary": "预计净利润增长",
                "reason": "主营改善 <script>alert(1)</script>" + "经营改善" * 30,
                "dedt_lo": 80_000_000,
                "dedt_hi": 120_000_000,
                "dedt_h1_yoy": 32,
                "dedt_src": "东财扣非预告(精确)",
                "q1_yoy": 20,
                "q2_yoy": 50,
                "q2_dedt": 90_000_000,
                "accel": True,
            },
            {
                "code": "000001",
                "name": "测试银行旧预告",
                "idx": "csi300",
                "type": "略增",
                "net_min": 5000,
                "net_max": 6000,
                "ann_date": "20260701",
                "period": "20260630",
            },
            {
                "code": "000002",
                "name": "应被排除的三季报",
                "idx": "csi500",
                "type": "预增",
                "ann_date": "20260703",
                "period": "20260930",
            },
            {
                "code": "000003",
                "name": "测试制造",
                "idx": "other",
                "type": "预减",
                "pos": False,
                "p_chg_min": -40,
                "p_chg_max": -20,
                "net_min": 3000,
                "net_max": 5000,
                "ann_date": "20260703",
                "period": "20260630",
                "reason": "需求阶段性走弱",
            },
        ],
    }


def _cninfo_payload() -> dict:
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": [
            {
                "code": "000001",
                "type": "业绩预告",
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 00:00:00",
                "title": "2026年半年度业绩预告",
                "announcement_id": "date-only-1",
                "url": "https://static.cninfo.com.cn/date-only-1.pdf",
            },
            {
                "code": "000003",
                "type": "业绩预告",
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 14:30:00",
                "title": "2026年半年度业绩预告",
                "announcement_id": "exact-1",
                "url": "https://static.cninfo.com.cn/exact-1.pdf",
            },
        ],
    }


def _configure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "_runup_trade_calendar", lambda: ["20260702", "20260703", "20260706"])
    _write_json(tmp_path / "forecast_browse.json", _forecast_payload())
    _write_json(tmp_path / "cninfo_earnings_announcements.json", _cninfo_payload())
    _write_json(
        tmp_path / "report_000001.json",
        {
            "code": "000001.SZ",
            "name": "测试银行",
            "updated": "2026-07-19 18:00:00",
            "as_of_px": "20260719",
            "overview": {"l1": "金融", "l2": "银行", "mv": 120, "pe": 8, "pb": 0.8},
            "broker_fc": [
                {"date": "20260701", "org": "甲券商", "title": "中报前瞻", "rating": "买入", "np": {"2026": 12, "2027": 15}},
                {"date": "20260701", "org": "甲券商", "title": "中报前瞻", "rating": "买入", "np": {"2026": 12, "2027": 15}},
                {"date": "20260630", "org": "乙券商", "title": "经营跟踪", "rating": "增持", "np": {"2026": 10, "2027": 14}},
            ],
            "peers_val": [{"is_self": True, "pe_fwd": {"2026": 10, "2027": 8}}],
        },
    )


def test_page_route_navigation_and_template() -> None:
    app_module.app.config.update(TESTING=True)
    response = app_module.app.test_client().get("/earnings-commentary")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "中报业绩预告点评" in html
    assert "/api/earnings-commentary" in html
    assert "00:00:00" in html
    assert "查看完整个股研报" in html
    assert "Q2同比口径排序" in html
    assert "Q2原始同比排序" not in html
    assert "可比Q2增速排序" not in html
    assert "esc(item.headline)" in html
    assert PAGE_FEATURES["/earnings-commentary"] == "internal_operations"


def test_list_keeps_only_latest_half_year_event_and_normalizes_units(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    app_module.app.config.update(TESTING=True)
    response = app_module.app.test_client().get("/api/earnings-commentary?page_size=10")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["total"] == 2
    assert {item["code"] for item in data["items"]} == {"000001", "000003"}

    item = next(item for item in data["items"] if item["code"] == "000001")
    assert item["name"] == "测试银行"
    assert item["parent_profit_yi"] == {"min": 1.0, "max": 2.0, "mid": 1.5, "width_pct": 66.67}
    assert item["deduct_profit_yi"]["mid"] == 1.0
    assert item["announcement_time_precision"] == "date_only"
    assert item["acceleration"] is None
    assert item["report_exists"] is True


def test_detail_preserves_date_only_metadata_without_treating_midnight_as_exact(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    app_module.app.config.update(TESTING=True)
    response = app_module.app.test_client().get("/api/earnings-commentary?code=000001")
    assert response.status_code == 200
    item = response.get_json()["item"]
    announcement = item["announcement"]

    assert announcement["id"] == "date-only-1"
    assert announcement["title"] == "2026年半年度业绩预告"
    assert announcement["url"].endswith("date-only-1.pdf")
    assert announcement["source_datetime"] == "2026-07-03 00:00:00"
    assert announcement["published_at"] == ""
    assert announcement["time_precision"] == "date_only"
    assert announcement["effective_trade_date"] == "2026-07-06"
    assert "下一交易日" in announcement["effective_rule"]

    assert "营业收入" in "".join(item["announcement_facts"])
    assert "不直接下“超预期”" in item["expectation_assessment"]
    serialized = json.dumps(item, ensure_ascii=False)
    assert "Q2" in serialized
    assert "Q4" not in serialized
    assert "四季度" not in serialized
    assert item["report_context"]["available"] is True
    assert item["report_context"]["broker_consensus"][0]["sample_size"] == 2
    assert item["report_context"]["broker_consensus"][0]["forward_pe"] == 10.9
    assert item["source_reason_may_be_truncated"] is True


def test_intraday_exact_announcement_is_available_next_trading_day(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    app_module.app.config.update(TESTING=True)
    response = app_module.app.test_client().get("/api/earnings-commentary?code=000003")
    item = response.get_json()["item"]
    assert item["announcement"]["time_precision"] == "exact"
    assert item["announcement"]["published_at"] == "2026-07-03 14:30:00"
    assert item["announcement"]["effective_trade_date"] == "2026-07-06"
    assert item["acceleration"] is None


def test_stale_cninfo_cache_is_not_used_as_exact_time(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    stale_cninfo = _cninfo_payload()
    stale_cninfo["updated"] = "2020-01-01 00:00:00"
    _write_json(tmp_path / "cninfo_earnings_announcements.json", stale_cninfo)
    app_module.app.config.update(TESTING=True)
    response = app_module.app.test_client().get("/api/earnings-commentary?code=000003")
    data = response.get_json()
    assert response.status_code == 200
    assert data["data_health"]["components"]["cninfo"]["status"] == "stale"
    assert data["item"]["announcement"]["time_precision"] == "missing"
    assert data["item"]["announcement"]["published_at"] == ""


def test_growth_display_handles_cross_sign_and_low_base() -> None:
    turnaround = app_module._earnings_commentary_growth_display(300, current=10, prior=-5)
    assert turnaround["label"] == "扭亏"
    assert turnaround["value"] is None

    low_base = app_module._earnings_commentary_growth_display(1200)
    assert low_base["basis"] == "low_base_risk"
    assert low_base["value"] is None
    assert "低基数" in low_base["label"]


def test_acceleration_requires_complete_positive_bases() -> None:
    row = dict(_forecast_payload()["items"][0])
    row.update({
        "q1_dedt": 60_000_000,
        "q1_prior_dedt": 50_000_000,
        "q2_dedt": 90_000_000,
        "q2_prior_dedt": 60_000_000,
        "_announcement": {"time_precision": "missing"},
        "_report_exists": False,
    })
    summary = app_module._earnings_commentary_item_summary(row)
    assert summary["acceleration"] is True


def test_h1_loss_types_use_semantic_labels_end_to_end(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "_runup_trade_calendar", lambda: ["20260703", "20260706"])
    items = [
        {"code": "100001", "name": "扭亏样本", "type": "扭亏", "net_min": 100, "net_max": 200, "p_chg_min": 110, "p_chg_max": 120},
        {"code": "100002", "name": "续亏样本", "type": "续亏", "net_min": -3000, "net_max": -2000, "p_chg_min": 30, "p_chg_max": 40,
         "dedt_lo": -25_000_000, "dedt_hi": -20_000_000, "dedt_h1_yoy": 33, "dedt_src": "东财扣非预告(精确)"},
        {"code": "100003", "name": "首亏样本", "type": "首亏", "net_min": -500, "net_max": -300, "p_chg_min": -130, "p_chg_max": -110},
        {"code": "100004", "name": "续盈样本", "type": "续盈", "net_min": 33800, "net_max": 37800, "p_chg_min": -5, "p_chg_max": 5},
        {"code": "100005", "name": "跨零样本", "type": "不确定", "net_min": -100, "net_max": 100, "p_chg_min": -20, "p_chg_max": 20},
    ]
    for item in items:
        item.update({"idx": "other", "ann_date": "20260703", "period": "20260630"})
    _write_json(tmp_path / "forecast_browse.json", {"updated": "2026-07-20 08:00:00", "items": items})
    _write_json(tmp_path / "cninfo_earnings_announcements.json", {"items": []})

    app_module.app.config.update(TESTING=True)
    data = app_module.app.test_client().get("/api/earnings-commentary?page_size=10").get_json()
    by_code = {item["code"]: item for item in data["items"]}
    assert by_code["100001"]["parent_growth_label"] == "扭亏"
    assert "亏损收窄" in by_code["100002"]["parent_growth_label"]
    assert "亏损收窄" in by_code["100002"]["deduct_growth_label"]
    assert by_code["100003"]["parent_growth_label"] == "转亏"
    assert "同比变动区间跨零" in by_code["100004"]["parent_growth_label"]
    assert by_code["100005"]["parent_growth_label"] == "利润区间跨盈亏平衡点"
    assert by_code["100001"]["parent_profit_yi"]["mid"] == 0.015


def test_nonrecurring_bridge_is_estimated_without_structured_deduct_range() -> None:
    row = {"dedt_src": "披露非经损益(精确)", "q2_dedt": 10_000_000}
    assert app_module._earnings_commentary_source_quality(row) == "estimated"


def test_revision_words_do_not_promote_non_forecast_announcements() -> None:
    assert app_module._earnings_commentary_cninfo_relevance({
        "title": "2026年半年度报告补充公告",
        "type": "半年度报告",
    }) == 0
    assert app_module._earnings_commentary_cninfo_relevance({
        "title": "2026年半年度业绩预告（修订版）",
        "type": "业绩预告",
    }) > 0


def test_nearby_cninfo_match_is_not_used_as_event_time() -> None:
    row = {"code": "100001", "ann_date": "2026-07-03", "period": "20260630"}
    index = {
        ("100001", "20260704"): [{
            "code": "100001",
            "ann_date": "2026-07-04",
            "ann_datetime": "2026-07-04 08:00:00",
            "type": "业绩预告",
            "title": "2026年半年度业绩预告",
            "url": "https://static.cninfo.com.cn/nearby.pdf",
        }]
    }
    announcement = app_module._earnings_commentary_announcement(
        row, index, ["20260703", "20260706"]
    )
    assert announcement["date_match"] == "nearby"
    assert announcement["time_precision"] == "unverified"
    assert announcement["effective_trade_date"] == ""


def test_same_day_revision_uses_final_exact_version() -> None:
    row = {"code": "100001", "ann_date": "2026-07-03", "period": "20260630"}
    index = {
        ("100001", "20260703"): [
            {
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 08:00:00",
                "type": "业绩预告",
                "title": "2026年半年度业绩预告（修订版）",
                "announcement_id": "1001",
            },
            {
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 18:00:00",
                "type": "业绩预告",
                "title": "2026年半年度业绩预告更正公告",
                "announcement_id": "1002",
            },
        ]
    }
    announcement = app_module._earnings_commentary_announcement(
        row, index, ["20260703", "20260706"]
    )
    assert announcement["id"] == "1002"
    assert announcement["published_at"] == "2026-07-03 18:00:00"
    assert announcement["effective_trade_date"] == "2026-07-06"


def test_final_date_only_revision_stays_conservative() -> None:
    row = {"code": "100001", "ann_date": "2026-07-03", "period": "20260630"}
    index = {
        ("100001", "20260703"): [
            {
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 18:00:00",
                "type": "业绩预告",
                "title": "2026年半年度业绩预告更正公告",
                "announcement_id": "1002",
            },
            {
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 00:00:00",
                "type": "业绩预告",
                "title": "2026年半年度业绩预告（修订版）",
                "announcement_id": "1003",
            },
        ]
    }
    announcement = app_module._earnings_commentary_announcement(
        row, index, ["20260703", "20260706"]
    )
    assert announcement["id"] == "1003"
    assert announcement["time_precision"] == "date_only"
    assert announcement["published_at"] == ""
    assert announcement["effective_trade_date"] == "2026-07-06"


def test_calendar_tail_does_not_guess_weekday() -> None:
    row = {"code": "100001", "ann_date": "2026-07-03", "period": "20260630"}
    announcement = app_module._earnings_commentary_announcement(row, {}, ["20260703"])
    assert announcement["effective_trade_date"] == ""
    assert "暂不猜测" in announcement["effective_rule"]


def test_data_health_rejects_future_empty_and_stale_time_sources() -> None:
    now = datetime(2026, 7, 20, 12, 0, 0)
    future = app_module._earnings_commentary_data_health(
        {"updated": "2030-01-01 00:00:00"},
        rows=[{"code": "100001"}],
        cninfo_payload={"updated": "2026-07-20 11:00:00"},
        now=now,
    )
    assert future["stale"] is True
    assert future["status"] == "future_timestamp"

    empty = app_module._earnings_commentary_data_health(
        {"updated": "2026-07-20 11:00:00"},
        rows=[],
        cninfo_payload={"updated": "2026-07-20 11:00:00"},
        now=now,
    )
    assert empty["stale"] is True
    assert empty["status"] == "empty"

    stale_cninfo = app_module._earnings_commentary_data_health(
        {"updated": "2026-07-20 11:00:00"},
        rows=[{"code": "100001"}],
        cninfo_payload={"updated": "2026-07-18 00:00:00"},
        now=now,
    )
    assert stale_cninfo["stale"] is True
    assert stale_cninfo["components"]["forecast"]["status"] == "fresh"
    assert stale_cninfo["components"]["cninfo"]["status"] == "stale"

    empty_cninfo = app_module._earnings_commentary_data_health(
        {"updated": "2026-07-20 11:00:00"},
        rows=[{"code": "100001"}],
        cninfo_payload={"updated": "2026-07-20 11:00:00", "items": []},
        now=now,
    )
    assert empty_cninfo["stale"] is True
    assert empty_cninfo["status"] == "degraded"
    assert "源数据为空" in empty_cninfo["message"]


def test_forecast_top_level_list_is_defensively_normalized(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "_runup_trade_calendar", lambda: ["20260703", "20260706"])
    row = dict(_forecast_payload()["items"][0])
    _write_json(tmp_path / "forecast_browse.json", [row])
    _write_json(tmp_path / "cninfo_earnings_announcements.json", _cninfo_payload())
    app_module.app.config.update(TESTING=True)
    response = app_module.app.test_client().get("/api/earnings-commentary?page_size=10")
    data = response.get_json()
    assert response.status_code == 200
    assert data["total"] == 1
    assert data["items"][0]["code"] == "000001"
    assert "缺少顶层元数据" in data["source_message"]


def test_list_filters_are_server_side(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    positive = client.get("/api/earnings-commentary?positive=1").get_json()
    assert positive["total"] == 1
    assert positive["items"][0]["code"] == "000001"

    missing_report = client.get("/api/earnings-commentary?report_status=missing").get_json()
    assert missing_report["total"] == 1
    assert missing_report["items"][0]["code"] == "000003"


def test_list_search_supports_pinyin_initials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "STOCK_META_DB", str(tmp_path / "stock_meta.db"))
    monkeypatch.setattr(app_module, "_runup_trade_calendar", lambda: ["20260720", "20260721"])
    _write_json(tmp_path / "forecast_browse.json", {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": [{
            "code": "300502",
            "ts_code": "300502.SZ",
            "name": "新易盛",
            "idx": "csi300",
            "type": "预增",
            "pos": True,
            "net_min": 700000,
            "net_max": 800000,
            "p_chg_min": 77.6,
            "p_chg_max": 102.9,
            "ann_date": "20260720",
            "period": "20260630",
        }],
    })
    _write_json(tmp_path / "cninfo_earnings_announcements.json", {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": [{
            "code": "300502",
            "ann_date": "2026-07-20",
            "ann_datetime": "2026-07-20 00:00:00",
            "type": "业绩预告",
            "title": "2026年半年度业绩预告",
        }],
    })
    with sqlite3.connect(tmp_path / "stock_meta.db") as conn:
        conn.execute(
            "CREATE TABLE stock_meta (code TEXT, ts_code TEXT, name TEXT, "
            "pinyin_initials TEXT, list_status TEXT)"
        )
        conn.execute(
            "INSERT INTO stock_meta VALUES (?, ?, ?, ?, ?)",
            ("sz300502", "300502.SZ", "新易盛", "xys", "L"),
        )

    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    by_initials = client.get("/api/earnings-commentary?q=xys").get_json()
    assert by_initials["total"] == 1
    assert by_initials["items"][0]["code"] == "300502"

    by_prefix = client.get("/api/earnings-commentary?q=xy").get_json()
    assert by_prefix["total"] == 1

    wildcard = client.get("/api/earnings-commentary?q=x%25").get_json()
    assert wildcard["total"] == 0
