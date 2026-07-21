from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from scripts.access_policy import PAGE_FEATURES


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_advisor_pro_plus_api_builds_overlay_from_shared_files(tmp_path: Path) -> None:
    old_predict_json = app_module.PREDICT_JSON
    old_stock_meta_db = app_module.STOCK_META_DB
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    app_module.STOCK_META_DB = str(tmp_path / "stock_meta.db")
    data_dir = app_module.PREDICT_JSON.parent
    write_json(
        data_dir / "regime_advisor_pro.json",
        {"trade": {"items": [{"code": "000001", "name": "A", "action": "买入"}]}},
    )
    write_json(
        data_dir / "rolling_earnings.json",
        {"rolling": {"items": [{"code": "000001", "name": "A", "dedt_yoy": 40, "delta": 10}]}},
    )
    write_json(data_dir / "rolling_earnings_backtest_top50.json", {"timed": {"summary": {"10": {"mean_pct": 2.3}}}})

    try:
        response = app_module.app.test_client().get("/api/advisor-pro-plus/result")
    finally:
        app_module.PREDICT_JSON = old_predict_json
        app_module.STOCK_META_DB = old_stock_meta_db

    assert response.status_code == 200
    data = response.get_json()
    assert data["summary"]["n_enhanced_buy"] == 1
    assert data["enhanced_buy"][0]["code"] == "000001"
    assert data["backtest"]["timed"]["summary"]["10"]["mean_pct"] == 2.3


def test_advisor_pro_plus_template_keeps_echarts_canvas() -> None:
    html = Path("templates/advisor_pro_plus.html").read_text(encoding="utf-8")
    assert 'id="plus-chart"' in html
    assert "echarts.init(box)" in html
    assert "box.innerHTML='';" not in html
    assert "commonStart" in html
    assert "commonEnd" in html
    assert "forwardAlignRebase" in html
    assert "顾问Pro+滚动业绩" in html
    assert "顾问Pro+增强" in html
    assert '{% include "_nav.html" %}' in html
    assert PAGE_FEATURES["/rolling-earnings"] == "internal_operations"
    assert_no_mojibake(html)


def test_rolling_earnings_template_uses_readable_chinese() -> None:
    html = Path("templates/rolling_earnings.html").read_text(encoding="utf-8")
    assert "滚动业绩调仓" in html
    assert "刷新滚动业绩组合" in html
    assert "历史回测" in html
    assert '{% include "_nav.html" %}' in html
    assert PAGE_FEATURES["/advisor-pro-plus"] == "internal_operations"
    assert_no_mojibake(html)


def assert_no_mojibake(html: str) -> None:
    bad_fragments = [
        "椤鹃棶",
        "婊氬姩",
        "鍔犺浇",
        "璋冧粨",
        "鎵ц",
        "鍥炴祴",
        "鏆傛棤",
        "鍒锋柊",
    ]
    found = [fragment for fragment in bad_fragments if fragment in html]
    assert not found, f"template still contains mojibake fragments: {found}"


if __name__ == "__main__":
    with __import__("tempfile").TemporaryDirectory() as tmp:
        test_advisor_pro_plus_api_builds_overlay_from_shared_files(Path(tmp))
    test_advisor_pro_plus_template_keeps_echarts_canvas()
    test_rolling_earnings_template_uses_readable_chinese()
    print("advisor pro plus api tests ok")
