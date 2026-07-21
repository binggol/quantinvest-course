from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app
from scripts.access_policy import NAV_GROUPS


ROOT = Path(__file__).resolve().parents[1]


def _fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def _date_event_stats(path: str) -> dict:
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    groups = defaultdict(list)
    for event in data.get("events") or []:
        key = (str(event["trade_date"])[:10], str(event["next_trade_date"])[:10])
        groups[key].append(event)
    rows = []
    for events in groups.values():
        n = len(events)
        rows.append({
            "o2c": sum(float(e["next_open_to_close_pct"]) for e in events) / n,
            "low": sum(float(e["next_low_from_open_pct"]) for e in events) / n,
        })
    total = len(rows)
    assert total > 0
    return {
        "n": total,
        "close_down": _fmt_pct(sum(r["o2c"] < 0 for r in rows) / total * 100),
        "low15": _fmt_pct(sum(r["low"] <= -1.5 for r in rows) / total * 100),
        "low2": _fmt_pct(sum(r["low"] <= -2.0 for r in rows) / total * 100),
    }


def test_tech_external_api_returns_actionable_signal_shape():
    client = app.test_client()

    response = client.get("/api/tech-external")

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["state"] in {"trade", "watch", "avoid"}
    assert data["action"]
    assert data["anchors"]
    assert data["baskets"]
    assert data["rules"]
    assert "hynix" in data["anchors"]
    assert "semiconductor" in data["baskets"]


def test_tech_external_page_exists_and_loads_api():
    client = app.test_client()

    response = client.get("/tech-external")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "科技外部信号" in html
    assert "/api/tech-external" in html
    assert "doRefresh('chipmap')" in html


def test_tech_flow_gap_exit_prompts_are_visible():
    client = app.test_client()

    tech = client.get("/tech-external").get_data(as_text=True)
    surge = client.get("/surge-t").get_data(as_text=True)

    assert "上午买入拦截" in tech
    assert "上午买入拦截" in surge
    assert "开盘价和VWAP" in tech
    assert "开盘价和VWAP" in surge


def test_tech_flow_morning_guard_uses_backtested_numbers():
    client = app.test_client()
    default = _date_event_stats("data/star_flow_gap_exit_backtest.json")
    strict = _date_event_stats("data/star_flow_gap_exit_backtest_strict.json")

    tech = client.get("/tech-external").get_data(as_text=True)
    surge = client.get("/surge-t").get_data(as_text=True)

    expected = [
        f"{default['n']}个事件日",
        f"收盘回落{default['close_down']}",
        f"盘中跌超1.5%：{default['low15']}",
        f"盘中跌超2%：{default['low2']}",
        f"{strict['n']}个严格事件日",
        f"收盘回落{strict['close_down']}",
        f"盘中跌超1.5%：{strict['low15']}",
        f"盘中跌超2%：{strict['low2']}",
    ]
    for text in expected:
        assert text in tech
        assert text in surge


def test_tech_flow_prompts_include_stock_level_volume_down_evidence():
    client = app.test_client()

    tech = client.get("/tech-external").get_data(as_text=True)
    surge = client.get("/surge-t").get_data(as_text=True)

    expected = [
        "个股放量下跌（历史测算）",
        "981只科技股",
        "23649次",
        "8844次",
        "下跌概率51.3%",
        "同日达到20只",
        "+0.88%",
        "单票不追",
        "板块共振",
    ]
    for text in expected:
        assert text in tech
        assert text in surge


def test_main_navigation_links_to_tech_external():
    html = Path("templates/chipmap.html").read_text(encoding="utf-8")
    links = [link for _, group in NAV_GROUPS for link in group]
    paths = [link.path for link in links]

    assert '{% include "_nav.html" %}' in html
    assert next(link.label for link in links if link.path == "/tech-external") == "科技外部信号"
    assert paths.index("/chipmap") < paths.index("/tech-external") < paths.index("/cross-market")


if __name__ == "__main__":
    test_tech_external_api_returns_actionable_signal_shape()
    test_tech_external_page_exists_and_loads_api()
    test_tech_flow_gap_exit_prompts_are_visible()
    test_tech_flow_morning_guard_uses_backtested_numbers()
    test_tech_flow_prompts_include_stock_level_volume_down_evidence()
    test_main_navigation_links_to_tech_external()
    print("ok")
