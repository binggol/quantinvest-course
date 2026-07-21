from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


def test_runup_ignores_cninfo_row_without_parseable_time(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "_runup_load_cninfo_earnings_times",
        lambda: {
            ("301308", "20260703"): {
                "dt": app_module.datetime(2026, 7, 3, 18, 47, 7),
                "ann_date": "20260703",
                "ann_datetime": "2026-07-03 18:47:07",
            }
        },
    )
    payload = {
        "events": {
            "forecast": [
                {"code": "301165.SZ", "name": "Ruijie", "ann_date": "2026-07-03", "period": "20260630"}
            ]
        }
    }

    app_module._attach_cninfo_earnings_time_to_runup(payload)

    row = payload["events"]["forecast"][0]
    assert row["raw_ann_date"] == "2026-07-03"
    assert row["cninfo_effective_ann_date"] == "2026-07-03"
    assert row["cninfo_ann_datetime"] == ""
    assert row["cninfo_ann_date_match"] == "missing"


def test_runup_load_cninfo_cache_requires_datetime(monkeypatch):
    fake = {
        "items": [
            {
                "code": "301165",
                "ann_date": "2026-07-03",
                "ann_datetime": "",
                "title": "no time",
            },
            {
                "code": "000408",
                "ann_date": "2026-07-07",
                "ann_datetime": "2026-07-07 00:00:00",
                "title": "midnight means no specific cninfo time",
            },
            {
                "code": "301308",
                "ann_date": "2026-07-03",
                "ann_datetime": "2026-07-03 18:47:07",
                "title": "has time",
            },
        ]
    }
    monkeypatch.setattr(app_module, "_read_json", lambda path: fake)

    cache = app_module._runup_load_cninfo_earnings_times()

    assert ("301165", "20260703") not in cache
    assert ("000408", "20260707") not in cache
    assert ("301308", "20260703") in cache


if __name__ == "__main__":
    class MonkeyPatch:
        def __init__(self):
            self._old = []

        def setattr(self, obj, name, value):
            self._old.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._old):
                setattr(obj, name, value)

    mp = MonkeyPatch()
    test_runup_ignores_cninfo_row_without_parseable_time(mp)
    mp.undo()
    mp = MonkeyPatch()
    test_runup_load_cninfo_cache_requires_datetime(mp)
    mp.undo()
    print("runup cninfo time tests ok")
