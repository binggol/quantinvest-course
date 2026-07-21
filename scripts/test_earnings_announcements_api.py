from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import app as app_module


def _event(
    code: str,
    name: str,
    category: str,
    ann_date: str,
    *,
    period: str = "20260630",
    ann_datetime: str = "",
    time_precision: str = "missing",
    growth_value: float | None = None,
    growth_candidate: bool = False,
    growth_comparable: bool | None = None,
    title: str = "",
    event_id: str = "",
) -> dict:
    label = app_module._EARNINGS_ANNOUNCEMENT_KIND_LABELS[category]
    return {
        "event_id": event_id or f"{code}:{period}:{category}:{ann_date}",
        "announcement_id": event_id,
        "code": code,
        "name": name,
        "category": category,
        "category_label": label,
        "raw_type": label,
        "ann_date": ann_date,
        "ann_datetime": ann_datetime,
        "time_precision": time_precision,
        "title": title or f"{period[:4]}年半年度{label}",
        "url": f"https://static.cninfo.com.cn/{code}-{category}.pdf",
        "period": period,
        "period_label": app_module._earnings_announcement_period_label(period),
        "source": "test",
        "growth_value": growth_value,
        "growth_label": "" if growth_value is None else f"Q2扣非同比+{growth_value:.1f}%",
        "growth_comparable": growth_candidate if growth_comparable is None else growth_comparable,
        "growth_candidate": growth_candidate,
        "acceleration": True if growth_candidate else None,
        "window_status": "",
    }


def _events_fixture() -> list[dict]:
    return [
        _event(
            "300502",
            "新易盛",
            "forecast",
            "2026-07-17",
            ann_datetime="2026-07-17 18:10:00",
            time_precision="exact",
            growth_value=80.0,
            growth_candidate=True,
            growth_comparable=False,
            event_id="1001",
        ),
        _event(
            "300502",
            "新易盛",
            "express",
            "2026-07-18",
            ann_datetime="2026-07-18 00:00:00",
            time_precision="date_only",
            event_id="1002",
        ),
        _event(
            "300502",
            "新易盛",
            "report",
            "2026-08-20",
            ann_datetime="2026-08-20 00:00:00",
            time_precision="date_only",
            event_id="1003",
        ),
        _event(
            "000001",
            "正基数增长",
            "forecast",
            "2026-07-18",
            ann_datetime="2026-07-18 00:00:00",
            time_precision="date_only",
            growth_value=65.0,
            growth_candidate=True,
            event_id="2001",
        ),
        _event(
            "000002",
            "亏损样本",
            "forecast",
            "2026-07-18",
            ann_datetime="2026-07-18 00:00:00",
            time_precision="date_only",
            growth_value=90.0,
            growth_candidate=False,
            event_id="2002",
        ),
        _event(
            "000003",
            "低基数样本",
            "forecast",
            "2026-07-18",
            ann_datetime="2026-07-18 00:00:00",
            time_precision="date_only",
            growth_value=650.0,
            growth_candidate=False,
            event_id="2003",
        ),
        _event(
            "000005",
            "锚点日时点未知",
            "forecast",
            "2026-07-17",
            ann_datetime="2026-07-17 00:00:00",
            time_precision="date_only",
            growth_value=70.0,
            growth_candidate=True,
            event_id="2005",
        ),
        _event(
            "000004",
            "其他日期快报",
            "express",
            "2026-07-16",
            period="20260331",
            ann_datetime="2026-07-16 17:00:00",
            time_precision="exact",
            event_id="2004",
        ),
    ]


def _configure_api(monkeypatch) -> None:
    events = _events_fixture()
    monkeypatch.setattr(
        app_module,
        "_earnings_announcement_build_events",
        lambda: (
            deepcopy(events),
            ["20260630", "20260331"],
            "20260630",
            {
                "forecast_updated": "2026-07-20 08:00:00",
                "forecast_high_growth_ready": True,
                "cninfo_updated": "2026-07-20 08:00:00",
                "cninfo_incomplete": False,
                "cninfo_complete": True,
            },
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_runup_trade_calendar",
        lambda: ["20260716", "20260717", "20260720"],
    )
    monkeypatch.setattr(
        app_module,
        "_earnings_after_close_window",
        lambda _calendar: {
            "available": True,
            "previous_trade_date": "2026-07-17",
            "start": "2026-07-17 15:00:00",
            "end": "2026-07-20 10:00:00",
            "label": "2026-07-17 15:00 至 2026-07-20 10:00",
        },
    )
    monkeypatch.setattr(
        app_module,
        "_earnings_commentary_pinyin_codes",
        lambda query, limit=200: {"300502"} if query in {"xys", "xy"} else set(),
    )


def _growth_row(**updates) -> dict:
    row = {
        "code": "000001",
        "ts_code": "000001.SZ",
        "name": "增长测试",
        "idx": "other",
        "type": "预增",
        "pos": True,
        "ann_date": "2026-07-18",
        "period": "20260630",
        "p_chg_min": 50,
        "p_chg_max": 80,
        "net_min": 10_000,
        "net_max": 12_000,
        "q1_yoy": 20,
        "q2_yoy": 80,
        "q1_dedt": 120_000_000,
        "q1_prior_dedt": 100_000_000,
        "q2_dedt": 180_000_000,
        "q2_prior_dedt": 100_000_000,
    }
    row.update(updates)
    return row


def test_announcement_classification_period_and_noise() -> None:
    forecast = {"type": "业绩预告", "title": "2026年半年度业绩预告"}
    express = {"title": "2026年前三季度业绩快报"}
    report = {"title": "2025年年度报告"}

    assert app_module._earnings_announcement_category(forecast) == "forecast"
    assert app_module._earnings_announcement_period(forecast) == "20260630"
    assert app_module._earnings_announcement_category(express) == "express"
    assert app_module._earnings_announcement_period(express) == "20260930"
    assert app_module._earnings_announcement_category(report) == "report"
    assert app_module._earnings_announcement_period(report) == "20251231"
    assert app_module._earnings_announcement_period(
        {"title": "博敏电子2026半年度业绩预告公告"}
    ) == "20260630"
    assert app_module._earnings_announcement_period(
        {"title": "临2025-006 中国汽研2024年度业绩快报"}
    ) == "20241231"
    compound = {"title": "2025年度业绩快报暨2026年一季度业绩预告"}
    assert app_module._earnings_announcement_categories(compound) == ["forecast", "express"]
    assert app_module._earnings_announcement_period_for_category(compound, "forecast") == "20260331"
    assert app_module._earnings_announcement_period_for_category(compound, "express") == "20251231"
    assert app_module._earnings_announcement_period(
        {"period": "20260331", "title": "2025年年度报告"}
    ) == "20260331"

    noisy = [
        {"code": "600001", "ann_date": "2026-07-18", "title": "财务顾问持续督导季度报告"},
        {"code": "600002", "ann_date": "2026-07-18", "title": "关于举行2025年年度报告说明会的公告"},
        {"code": "600003", "ann_date": "2026-07-18", "title": "2025年年度报告披露提示性公告"},
    ]
    assert all(app_module._earnings_announcement_cninfo_event(item) is None for item in noisy)


def test_after_close_window_and_time_precision_boundaries() -> None:
    window = app_module._earnings_after_close_window(
        ["20260716", "20260717", "20260720"],
        now=datetime(2026, 7, 20, 10, 0, 0),
    )
    assert window["previous_trade_date"] == "2026-07-17"
    assert window["start"] == "2026-07-17 15:00:00"

    def status(ann_date: str, ann_datetime: str, precision: str) -> str:
        return app_module._earnings_announcement_window_status(
            {
                "ann_date": ann_date,
                "ann_datetime": ann_datetime,
                "time_precision": precision,
            },
            window,
        )

    assert status("2026-07-17", "2026-07-17 14:59:59", "exact") == ""
    assert status("2026-07-17", "2026-07-17 15:00:00", "exact") == "confirmed"
    assert status("2026-07-17", "2026-07-17 00:00:00", "date_only") == "anchor_date_uncertain"
    assert status("2026-07-18", "2026-07-18 00:00:00", "date_only") == "date_only_candidate"
    assert status("2026-07-20", "", "missing") == "missing_time_candidate"
    assert status("2026-07-20", "2026-07-20 10:00:01", "exact") == ""
    assert app_module._earnings_after_close_window([], now=datetime(2026, 7, 20))["available"] is False
    stale = app_module._earnings_after_close_window(
        ["20260716", "20260717"], now=datetime(2026, 7, 25, 10, 0, 0)
    )
    assert stale["available"] is False
    assert stale["reason"] == "calendar_stale"


def test_cninfo_midnight_is_date_only_and_untrusted_exact_time_is_flagged() -> None:
    base = {
        "code": "300502",
        "name": "新易盛",
        "type": "业绩预告",
        "ann_date": "2026-07-18",
        "title": "2026年半年度业绩预告",
    }
    midnight = app_module._earnings_announcement_cninfo_event(
        {**base, "ann_datetime": "2026-07-18 00:00:00"}, trusted_time=True
    )
    exact = app_module._earnings_announcement_cninfo_event(
        {**base, "ann_datetime": "2026-07-18 18:10:00"}, trusted_time=True
    )
    untrusted = app_module._earnings_announcement_cninfo_event(
        {**base, "ann_datetime": "2026-07-18 18:10:00"}, trusted_time=False
    )
    missing = app_module._earnings_announcement_cninfo_event(base, trusted_time=True)

    assert midnight["time_precision"] == "date_only"
    assert exact["time_precision"] == "exact"
    assert untrusted["time_precision"] == "unverified"
    assert missing["time_precision"] == "missing"


def test_build_events_merges_nearby_forecast_by_announcement_id(monkeypatch) -> None:
    announcement_id = "1225430348"
    cninfo = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": [
            {
                "code": "920809",
                "name": "安达科技",
                "type": "业绩预告",
                "ann_date": "2026-07-17",
                "ann_datetime": "2026-07-17 00:00:00",
                "title": "2026年半年度业绩预告公告",
                "announcement_id": announcement_id,
                "url": f"https://static.cninfo.com.cn/{announcement_id}.PDF",
            }
        ],
    }
    row = _growth_row(
        code="920809",
        ts_code="920809.BJ",
        name="安达科技",
        ann_date="2026-07-18",
        type="扭亏",
        _announcement={
            "id": announcement_id,
            "source_date": "2026-07-17",
            "source_datetime": "2026-07-17 00:00:00",
            "time_precision": "date_only",
            "date_match": "nearby",
            "title": "2026年半年度业绩预告公告",
            "url": f"https://static.cninfo.com.cn/{announcement_id}.PDF",
        },
    )
    monkeypatch.setattr(
        app_module,
        "_earnings_commentary_load_rows",
        lambda: ({"updated": cninfo["updated"]}, [row], "forecast_browse", "", cninfo),
    )
    monkeypatch.setattr(app_module, "_read_json", lambda _path: {})

    events, _periods, _current, _coverage = app_module._earnings_announcement_build_events()
    matches = [event for event in events if event.get("announcement_id") == announcement_id]

    assert len(matches) == 1
    assert matches[0]["ann_date"] == "2026-07-17"
    assert matches[0]["forecast_raw_date"] == "2026-07-18"
    assert matches[0]["period"] == "20260630"
    assert matches[0]["date_match"] == "nearby"


def test_high_growth_requires_positive_comparable_base_and_rejects_low_base() -> None:
    good = app_module._earnings_announcement_forecast_growth(_growth_row())
    assert good["growth_candidate"] is True
    assert good["growth_comparable"] is True
    assert good["growth_value"] == 80.0

    loss = app_module._earnings_announcement_forecast_growth(
        _growth_row(q2_dedt=-20_000_000, q2_prior_dedt=-50_000_000)
    )
    assert loss["growth_candidate"] is False

    turnaround = app_module._earnings_announcement_forecast_growth(
        _growth_row(q2_dedt=20_000_000, q2_prior_dedt=-50_000_000)
    )
    assert turnaround["growth_comparable"] is False
    assert turnaround["growth_candidate"] is False

    # 线上 forecast_browse 暂未持久化同期金额基数。信息候选允许进入，
    # 但必须明确标成不可比/待核验；只有已知负基数时才应硬排除。
    missing_prior = app_module._earnings_announcement_forecast_growth(
        _growth_row(q1_prior_dedt=None, q2_prior_dedt=None)
    )
    assert missing_prior["growth_candidate"] is True
    assert missing_prior["growth_comparable"] is False

    low_base = app_module._earnings_announcement_forecast_growth(_growth_row(q2_yoy=550))
    assert low_base["growth_candidate"] is False


def test_api_filters_by_exact_date_and_announcement_type(monkeypatch) -> None:
    _configure_api(monkeypatch)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    payload = client.get(
        "/api/earnings-announcements?ann_date=2026-07-18&kind=express"
    ).get_json()

    assert payload["ok"] is True
    assert payload["ann_date"] == "2026-07-18"
    assert payload["total"] == 1
    assert payload["items"][0]["code"] == "300502"
    assert payload["items"][0]["categories"] == ["express"]


def test_api_searches_by_pinyin_initials_and_stock_code(monkeypatch) -> None:
    _configure_api(monkeypatch)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    by_pinyin = client.get("/api/earnings-announcements?q=xys").get_json()
    assert by_pinyin["total"] == 1
    assert by_pinyin["items"][0]["code"] == "300502"

    by_code = client.get("/api/earnings-announcements?q=300502").get_json()
    assert by_code["total"] == 1
    assert by_code["items"][0]["name"] == "新易盛"


def test_api_returns_same_stock_same_period_disclosure_chain(monkeypatch) -> None:
    _configure_api(monkeypatch)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    payload = client.get(
        "/api/earnings-announcements?code=300502&chain_period=20260630"
    ).get_json()
    chain = payload["chain"]

    assert chain["code"] == "300502"
    assert chain["name"] == "新易盛"
    assert chain["period"] == "20260630"
    assert [item["category"] for item in chain["items"]] == ["forecast", "express", "report"]
    assert [item["announcement_id"] for item in chain["items"]] == ["1001", "1002", "1003"]


def test_after_close_growth_api_excludes_loss_and_low_base(monkeypatch) -> None:
    _configure_api(monkeypatch)
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    payload = client.get(
        "/api/earnings-announcements?preset=after_close_growth&min_growth=50"
    ).get_json()
    codes = {item["code"] for item in payload["items"]}

    assert payload["preset"] == "after_close_growth"
    assert codes == {"300502", "000001"}
    pending = next(item for item in payload["items"] if item["code"] == "300502")
    assert pending["growth_comparable"] is False
    assert payload["window"]["previous_trade_date"] == "2026-07-17"
    assert payload["facets"]["confirmed_after_close"] == 1
    assert payload["facets"]["time_pending"] == 1
    assert payload["facets"]["anchor_date_uncertain_excluded"] == 1


def test_after_close_growth_fails_closed_when_forecast_snapshot_is_stale(monkeypatch) -> None:
    _configure_api(monkeypatch)
    events = _events_fixture()
    monkeypatch.setattr(
        app_module,
        "_earnings_announcement_build_events",
        lambda: (
            deepcopy(events),
            ["20260630"],
            "20260630",
            {
                "forecast_updated": "2026-07-15 08:00:00",
                "forecast_status": "stale",
                "forecast_stale": True,
                "forecast_high_growth_ready": False,
                "cninfo_complete": True,
            },
        ),
    )
    app_module.app.config.update(TESTING=True)

    payload = app_module.app.test_client().get(
        "/api/earnings-announcements?preset=after_close_growth"
    ).get_json()

    assert payload["ok"] is True
    assert payload["high_growth_available"] is False
    assert payload["total"] == 0
    assert "已暂停" in payload["message"]


def test_api_is_get_only_and_does_not_mutate_files(tmp_path: Path, monkeypatch) -> None:
    _configure_api(monkeypatch)
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    marker = tmp_path / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    assert client.get("/api/earnings-announcements").status_code == 200
    assert client.post("/api/earnings-announcements").status_code == 405

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before


def test_template_exposes_announcement_browser_static_contract() -> None:
    template = (
        Path(app_module.__file__).resolve().parent / "templates" / "earnings_commentary.html"
    ).read_text(encoding="utf-8")

    assert "/api/earnings-announcements" in template
    assert "after_close_growth" in template
    assert "ann_date" in template
    assert "chain" in template
    assert "昨收后" in template
    for element_id in (
        "announcement-quick",
        "announcement-panel",
        "ann-date",
        "ann-q",
        "ann-kind",
        "ann-period",
        "ann-min-growth",
        "ann-growth-preset",
        "ann-rows",
        "ann-chain",
        "ann-prev",
        "ann-page",
        "ann-next",
    ):
        assert f'id="{element_id}"' in template
    for function_name in (
        "loadAnnouncementList",
        "loadAnnouncementChain",
        "renderAnnouncementRows",
        "renderAnnouncementChain",
        "safeCninfoUrl",
    ):
        assert function_name in template
