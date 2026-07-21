from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_template(name: str) -> str:
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def test_internal_data_pages_render_retryable_load_errors():
    for name in ("fund_mining.html", "fund_predict_compare.html", "mine_pool.html"):
        source = read_template(name)
        assert "if(!response.ok)" in source
        assert "数据加载失败" in source
        assert 'onclick="load()"' in source


def test_index_inclusion_distinguishes_missing_data_from_empty_window():
    source = read_template("index_inclusion_pro.html")
    assert 'd.source_state!=="ok"' in source
    assert 'd.source_state==="missing"' in source
    assert "指数纳入数据尚未生成" in source
    assert "当前非指数调仓窗口期" in source
