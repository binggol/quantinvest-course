from __future__ import annotations

import json
from pathlib import Path

import app as app_module
from scripts import enrich_transfer_terms as module


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_parse_inquiry_result_prefers_executed_terms():
    text = (
        "\u8be2\u4ef7\u8f6c\u8ba9\u4ef7\u683c\u4e0b\u9650\u4e3a100.00\u5143/\u80a1\u3002"
        "\u672c\u6b21\u8be2\u4ef7\u8f6c\u8ba9\u80a1\u4efd\u6570\u91cf\u4e3a7,681,000\u80a1\uff0c"
        "\u5360\u516c\u53f8\u603b\u80a1\u672c285,234,456\u80a1\u76842.6929%\uff1b"
        "\u8be2\u4ef7\u8f6c\u8ba9\u7684\u4ef7\u683c\u4e3a146.08\u5143/\u80a1\u3002"
    )

    result = module.parse_transfer_terms([text])

    assert result["transfer_price"] == 146.08
    assert result["transfer_ratio"] == 2.6929
    assert result["confidence"] == "high"


def test_parse_agreement_terms_and_derived_ratio():
    direct = module.parse_transfer_terms([
        "\u7ea6\u5b9a\u5c06\u5176\u6301\u6709\u7684\u516c\u53f823,149,900\u80a1\uff08"
        "\u5360\u516c\u53f8\u603b\u80a1\u672c\u768412.6481%\uff09\u8f6c\u8ba9\u7ed9\u53d7\u8ba9\u65b9\uff0c"
        "\u8f6c\u8ba9\u4ef7\u683c\u4e3a12.80\u5143/\u80a1\u3002"
    ])
    derived = module.parse_transfer_terms([
        "\u672c\u6b21\u534f\u8bae\u8f6c\u8ba9\u80a1\u4efd\u6570\u91cf\u4e3a2,000,000\u80a1\uff0c"
        "\u516c\u53f8\u603b\u80a1\u672c\u4e3a100,000,000\u80a1\uff0c"
        "\u8f6c\u8ba9\u4ef7\u683c\u4e3a8.50\u5143/\u80a1\u3002"
    ])

    assert direct["transfer_price"] == 12.8
    assert direct["transfer_ratio"] == 12.6481
    assert derived["transfer_ratio"] == 2.0
    assert derived["ratio_method"] == "derived_shares_over_total"


def test_parser_does_not_promote_floor_or_market_price():
    result = module.parse_transfer_terms([
        "\u8be2\u4ef7\u8f6c\u8ba9\u4ef7\u683c\u4e0b\u9650\u4e3a80.00\u5143/\u80a1\uff0c"
        "\u524d20\u4e2a\u4ea4\u6613\u65e5\u80a1\u7968\u4ea4\u6613\u5747\u4ef7\u4e3a100.00\u5143/\u80a1\u3002"
    ])

    assert "transfer_price" not in result


def test_only_official_cninfo_pdf_urls_are_allowed():
    valid = "https://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF"
    assert module.validate_cninfo_pdf_url(valid) == valid

    for invalid in (
        "http://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF",
        "https://evil.example/finalpage/2026-07-10/1225419815.PDF",
        "https://static.cninfo.com.cn/other/1225419815.PDF",
        "https://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF?next=x",
    ):
        try:
            module.validate_cninfo_pdf_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {invalid}")


def test_run_is_incremental_and_keeps_canonical_untouched(tmp_path, monkeypatch):
    source = tmp_path / "cninfo_transfer.json"
    output = tmp_path / "transfer_terms_overlay.json"
    row = {
        "code": "300776",
        "ann_date": "2026-07-10",
        "title": "\u5173\u4e8e\u80a1\u4e1c\u8be2\u4ef7\u8f6c\u8ba9\u7ed3\u679c\u62a5\u544a\u4e66",
        "announcement_id": "1225419815",
        "url": "https://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF",
    }
    _write_json(source, {"items": [row]})
    original = source.read_bytes()
    calls = []

    def fake_enrich(source_row, timeout, session):
        calls.append(source_row["announcement_id"])
        return {
            **source_row,
            "key": module.overlay_key(source_row),
            "status": "parsed",
            "parser_version": module.PARSER_VERSION,
            "transfer_price": 146.08,
            "transfer_ratio": 2.6929,
        }

    monkeypatch.setattr(module, "enrich_row", fake_enrich)
    module.run(source, output, limit=30, sleep_seconds=0)
    module.run(source, output, limit=30, sleep_seconds=0)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert calls == ["1225419815"]
    assert payload["items"][0]["transfer_price"] == 146.08
    assert source.read_bytes() == original


def test_transfer_overlay_is_not_applied_to_placement(monkeypatch):
    announcement_id = "sample-1"
    payloads = {
        "cninfo_transfer.json": {
            "items": [{
                "code": "300776",
                "ann_date": "2026-07-10",
                "title": "transfer result",
                "announcement_id": announcement_id,
                "url": "https://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF",
            }]
        },
        "placement_status.json": {
            "items": [{
                "code": "600000",
                "ann_date": "2026-07-10",
                "title": "placement",
                "announcement_id": announcement_id,
                "issue_price": 21.0,
            }]
        },
        "transfer_terms_overlay.json": {
            "items": [{
                "status": "parsed",
                "announcement_id": announcement_id,
                "url": "https://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF",
                "transfer_price": 146.08,
                "transfer_ratio": 2.6929,
                "confidence": "high",
                "price_page": 1,
                "ratio_page": 1,
                "parser_version": 1,
            }]
        },
    }
    monkeypatch.setattr(app_module, "_eventrisk_load_json", lambda name: payloads.get(name, {}))
    monkeypatch.setattr(app_module, "_meta_for_codes", lambda codes: {})

    response = app_module.app.test_client().get("/api/placement_transfer")

    assert response.status_code == 200
    data = response.get_json()
    transfer = next(row for row in data["transfer"] if row["code"] == "300776")
    placement = next(row for row in data["placement"] if row["code"] == "600000")
    assert transfer["transfer_price"] == 146.08
    assert transfer["transfer_ratio"] == 2.6929
    assert transfer["transfer_terms_confidence"] == "high"
    assert placement["transfer_price"] == 21.0
    assert placement["transfer_ratio"] == ""


def test_watcher_uses_two_daily_slots_and_template_has_no_manual_refresh():
    watcher = Path("scripts/watch_predict_pc.ps1").read_text(encoding="utf-8")
    template = Path("templates/transfer_events.html").read_text(encoding="utf-8")

    assert "enrich_transfer_terms.py" in watcher
    assert "transfer_terms_overlay.json" in watcher
    assert "-preopen" in watcher
    assert "-afterclose" in watcher
    assert "$minutes -ge (6 * 60)" in watcher
    assert "last_slot" in watcher
    assert "refreshData" not in template
    assert "/api/refresh/transfer_events" not in template
