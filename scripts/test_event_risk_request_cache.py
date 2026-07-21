from __future__ import annotations

from collections import Counter
import os

import app as app_module


def test_unlock_enrichment_reuses_event_data_and_code_summary_within_request(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    reads = Counter()
    report_calls = Counter()
    original_report = app_module._build_event_risk_report

    def fake_read(path):
        reads[path.name] += 1
        if path.name == "cninfo_unlock.json":
            return {
                "items": [
                    {"code": "300001", "unlock_date": "2026-08-01", "unlock_ratio": 8.5},
                    {"code": "600001", "unlock_date": "2026-09-01", "unlock_ratio": 2.0},
                ]
            }
        return {}

    def counted_report(code, industry=""):
        report_calls[app_module._code6(code)] += 1
        return original_report(code, industry=industry)

    monkeypatch.setattr(app_module, "_read_json", fake_read)
    monkeypatch.setattr(app_module, "_build_event_risk_report", counted_report)
    monkeypatch.setattr(
        app_module,
        "_money_outflow_for_code",
        lambda code: {"status": "none", "label": "无资金流数据"},
    )

    first = [
        {"code": "300001.SZ", "industry": "半导体"},
        {"code": "sz300001", "industry": "电子"},
        {"code": "600001.SH", "industry": "银行"},
    ]
    second = [{"code": "300001", "industry": "其他"}]
    with app_module.app.test_request_context("/cache-test"):
        app_module._attach_unlock_info(first)
        app_module._attach_unlock_info(second)

    assert reads
    assert max(reads.values()) == 1
    assert report_calls == {"300001": 1, "600001": 1}
    assert first[0]["unlock_info"] == first[1]["unlock_info"] == second[0]["unlock_info"]
    assert first[0]["unlock_info"]["other"][0]["ratio"] == 8.5
    assert first[2]["unlock_info"]["other"][0]["ratio"] == 2.0
    assert set(first[0]) >= {"code", "industry", "unlock_info", "money_outflow"}


def test_event_json_cache_is_request_scoped(monkeypatch):
    calls = Counter()

    def fake_read(path):
        calls[path.name] += 1
        return {"version": calls[path.name]}

    monkeypatch.setattr(app_module, "_read_json", fake_read)

    with app_module.app.test_request_context("/first"):
        first = app_module._eventrisk_load_json("cache_probe.json")
        again = app_module._eventrisk_load_json("cache_probe.json")
        assert first is again
        assert first["version"] == 1

    with app_module.app.test_request_context("/second"):
        refreshed = app_module._eventrisk_load_json("cache_probe.json")

    assert refreshed["version"] == 2
    assert calls["cache_probe.json"] == 2


def test_event_json_loader_prefers_the_newer_local_or_shared_artifact(monkeypatch, tmp_path):
    shared_dir = tmp_path / "shared"
    local_root = tmp_path / "project"
    shared_dir.mkdir()
    (local_root / "data").mkdir(parents=True)
    shared = shared_dir / "freshness.json"
    local = local_root / "data" / "freshness.json"
    shared.write_text('{"source":"shared"}', encoding="utf-8")
    local.write_text('{"source":"local"}', encoding="utf-8")
    os.utime(shared, (100, 100))
    os.utime(local, (200, 200))
    monkeypatch.setattr(app_module, "PREDICT_JSON", shared_dir / "predictions.json")
    monkeypatch.setattr(app_module, "__file__", str(local_root / "app.py"))

    with app_module.app.test_request_context("/newer-local"):
        assert app_module._eventrisk_load_json("freshness.json")["source"] == "local"

    os.utime(shared, (300, 300))
    with app_module.app.test_request_context("/newer-shared"):
        assert app_module._eventrisk_load_json("freshness.json")["source"] == "shared"


def test_company_index_preserves_source_order_and_deduplicates_multi_key_rows(monkeypatch):
    payload = {
        "items": [
            {"code": "300001", "ts_code": "300001.SZ", "title": "first"},
            {"code": "600001", "title": "other"},
            {"symbol": "sz300001", "title": "second"},
        ]
    }
    monkeypatch.setattr(app_module, "_eventrisk_load_json", lambda name: payload)

    with app_module.app.test_request_context("/index-test"):
        rows = app_module._eventrisk_pick_company_rows(
            ["probe.json"], app_module._eventrisk_code_keys("300001.SZ")
        )
        rows_again = app_module._eventrisk_pick_company_rows(
            ["probe.json"], app_module._eventrisk_code_keys("300001.SZ")
        )

    assert [row["title"] for row in rows] == ["first", "second"]
    assert [row["title"] for row in rows_again] == ["first", "second"]
    assert all(row["dataset"] == "probe.json" for row in rows)


def test_money_outflow_map_avoids_repeated_path_checks_within_request(monkeypatch, tmp_path):
    money_file = tmp_path / "money_outflow_signal.json"
    money_file.write_text(
        '{"latest_stock_outflow":[{"code":"300001.SZ","outflow_rank_pct":91}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "MONEY_OUTFLOW_JSON", money_file)
    monkeypatch.setitem(app_module._MONEY_OUTFLOW_CACHE, "mtime", None)
    monkeypatch.setitem(app_module._MONEY_OUTFLOW_CACHE, "data", {})
    monkeypatch.setitem(app_module._MONEY_OUTFLOW_CACHE, "payload", None)

    original_stat = type(money_file).stat
    calls = Counter()

    def counted_stat(path, *args, **kwargs):
        if path == money_file:
            calls["stat"] += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(money_file), "stat", counted_stat)
    with app_module.app.test_request_context("/money-cache"):
        first = app_module._money_outflow_latest_map()
        after_first = calls["stat"]
        second = app_module._money_outflow_latest_map()

    assert first is second
    assert first["300001"]["outflow_rank_pct"] == 91
    assert calls["stat"] == after_first
