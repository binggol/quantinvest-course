"""Regression tests for the model comparison page."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "backtest.html"


def test_backtest_page_keeps_table_available_without_chart_cdn() -> None:
    html = TEMPLATE.read_text(encoding="utf-8-sig")
    assert "const chart = window.echarts ?" in html
    assert "if (!chart)" in html
    assert "回测指标表仍可正常查看" in html
    assert 'id="load-error"' in html
    assert 'id="retry"' in html


def test_backtest_calmar_uses_the_metric_shown_in_its_header() -> None:
    html = TEMPLATE.read_text(encoding="utf-8-sig")
    assert "c.excess/Math.abs(c.maxdd)" in html
    assert "c.ann/Math.abs(c.maxdd)" not in html


def test_backtest_defaults_to_most_recent_updated_batch() -> None:
    html = TEMPLATE.read_text(encoding="utf-8-sig")
    assert "bTime.localeCompare(aTime)" in html
    assert "$batchsel.value = batches[0]" in html
    assert "c.on = c.batch === batches[0]" in html


def test_backtest_table_has_mobile_overflow_container() -> None:
    html = TEMPLATE.read_text(encoding="utf-8-sig")
    assert 'class="bt-table-scroll"' in html
    assert ".bt-table-scroll { width:100%; overflow-x:auto; }" in html
