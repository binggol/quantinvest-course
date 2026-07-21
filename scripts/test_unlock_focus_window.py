from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_unlock_focus_marks_recent_eight_months_without_dropping_one_year_items(tmp_path):
    old_predict_json = app_module.PREDICT_JSON
    app_module.PREDICT_JSON = tmp_path / "predictions.json"
    data_dir = app_module.PREDICT_JSON.parent

    write_json(
        data_dir / "cninfo_transfer.json",
        {
            "items": [
                {
                    "code": "301308",
                    "date": "2026-01-16",
                    "ann_date": "2026-01-16",
                    "title": "股东询价转让计划书",
                },
                {
                    "code": "300394",
                    "date": "2025-08-08",
                    "ann_date": "2025-08-08",
                    "title": "股东询价转让计划书",
                },
            ]
        },
    )

    try:
        items = app_module._build_unlock_focus_items()
    finally:
        app_module.PREDICT_JSON = old_predict_json

    by_code = {x["code"]: x for x in items}
    assert len(items) == 2
    assert by_code["301308"]["focus_window"] is True
    assert by_code["301308"]["focus_label"] == "最近8个月重点"
    assert by_code["300394"]["focus_window"] is False


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_unlock_focus_marks_recent_eight_months_without_dropping_one_year_items(Path(tmp))
    print("ok")
