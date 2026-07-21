"""Static regressions for critical page failure and empty states."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def template(name: str) -> str:
    return (ROOT / "templates" / name).read_text(encoding="utf-8-sig")


def test_advisor_pro_survives_chart_library_failure() -> None:
    html = template("advisor_pro.html")
    assert "function initAdvisorChart" in html
    assert "if(window.echarts)" in html
    assert "清单和回测指标仍可正常查看" in html
    assert "if(!response.ok)throw new Error" in html


def test_holdings_exposes_primary_api_failure() -> None:
    html = template("holdings.html")
    assert "持仓数据加载失败:" in html
    assert "逐票明细暂时无法加载" in html
    assert "if(!window.echarts)" in html
    assert "if(!m||!m.portfolio)" in html


def test_report_exposes_load_and_generation_failures() -> None:
    html = template("report.html")
    assert "async function jsonFetch" in html
    assert "研报数据暂时无法加载" in html
    assert "生成请求失败:" in html


def test_event_tables_distinguish_empty_data_from_request_failure() -> None:
    placement = template("placement_events.html")
    transfer = template("transfer_events.html")
    assert "暂无定增事件数据" in placement
    assert "定增事件暂时无法加载" in placement
    assert "暂无询价转让事件数据" in transfer
    assert "询价转让事件暂时无法加载" in transfer
    assert 'id="retry"' in placement
    assert 'id="retry"' in transfer


def test_chart_pages_use_vendored_echarts_instead_of_external_cdn() -> None:
    chart_pages = []
    for path in (ROOT / "templates").glob("*.html"):
        html = path.read_text(encoding="utf-8-sig")
        if "echarts" in html.lower():
            chart_pages.append(path.name)
            assert "cdn.jsdelivr.net/npm/echarts" not in html
    assert "index.html" in chart_pages
    assert "backtest.html" in chart_pages
    asset = ROOT / "static" / "echarts.min.js"
    assert asset.stat().st_size > 900_000
    assert (ROOT / "static" / "echarts.LICENSE.txt").exists()
