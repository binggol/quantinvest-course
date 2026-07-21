from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app


def test_cross_market_api_uses_workspace_payload_and_enriches_charts():
    client = app.test_client()

    response = client.get("/api/cross_market?sector=storage")

    assert response.status_code == 200
    data = response.get_json()
    charts = data.get("charts", {})
    assert charts.get("us", {}).get("intraday", {}).get("points")
    assert charts.get("us", {}).get("daily", {}).get("points")
    assert charts.get("korea", {}).get("intraday", {}).get("points")
    assert charts.get("korea", {}).get("daily", {}).get("points")

    selected_code = data.get("upside", [{}])[0].get("code")
    selected_chart = charts.get("cn", {}).get(selected_code, {})
    assert selected_chart.get("intraday", {}).get("points")
    assert selected_chart.get("daily", {}).get("points")


def test_cross_market_page_has_refresh_button():
    client = app.test_client()

    response = client.get("/cross-market")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "id=\"rfbtn\"" in html
    assert "id=\"rfst\"" in html
    assert "doRefresh('cross_market')" in html
    assert "/static/refresh.js" in html


def test_cross_market_page_loads_cn_intraday_for_selected_stock():
    html = Path("templates/cross_market.html").read_text(encoding="utf-8")
    assert "function cnIntraday" in html
    assert "/api/intraday?code=" in html
    assert "A股分时" in html
    assert "codeForIntraday" in html
    assert "function yValue" in html
    assert "row.pct" in html
    assert "formatter:v=>pctMode" in html
    assert 'cache:"no-store"' in html
    assert "Date.now()" in html


def test_cross_market_exporter_has_cn_intraday_fallback_and_pct():
    script = Path("scripts/export_cross_market_storage.py").read_text(encoding="utf-8")
    assert "quotes.sina.cn" in script
    assert "_cn_intraday_points" in script
    assert "_pct_points" in script
    assert 'row["pct"]' in script


if __name__ == "__main__":
    test_cross_market_api_uses_workspace_payload_and_enriches_charts()
    test_cross_market_page_has_refresh_button()
    test_cross_market_page_loads_cn_intraday_for_selected_stock()
    test_cross_market_exporter_has_cn_intraday_fallback_and_pct()
    print("ok")
