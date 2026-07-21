from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import app as app_module


def _basket(code: str) -> dict:
    return {
        "current": {"as_of": "2026-07-13", "regime": "test"},
        "trade": {
            "items": [
                {"code": code, "name": f"name-{code}", "action": "买入"},
            ]
        },
    }


def test_trade_history_snapshot_is_atomic_and_deduplicated(tmp_path, monkeypatch):
    history_path = tmp_path / "trade_history.json"
    history_path.write_text(json.dumps([{"sig": "old"}]), encoding="utf-8")
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")

    original_replace = app_module.os.replace
    observations = []

    def observing_replace(source, target):
        observations.append(
            {
                "source_is_sibling": source.parent == history_path.parent,
                "target_still_valid": json.loads(history_path.read_text(encoding="utf-8")),
            }
        )
        return original_replace(source, target)

    monkeypatch.setattr(app_module.os, "replace", observing_replace)
    app_module._snapshot_trade_basket(_basket("600000.SH"))
    app_module._snapshot_trade_basket(_basket("600000.SH"))

    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert len(history) == 2
    assert history[-1]["buy"] == ["600000.SH"]
    assert len(observations) == 1
    assert observations[0]["source_is_sibling"] is True
    assert observations[0]["target_still_valid"] == [{"sig": "old"}]
    assert not list(tmp_path.glob(".*.tmp"))


def test_trade_history_concurrent_snapshots_do_not_lose_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    codes = [f"{index:06d}.SZ" for index in range(24)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda code: app_module._snapshot_trade_basket(_basket(code)), codes))

    history = json.loads((tmp_path / "trade_history.json").read_text(encoding="utf-8"))
    assert len(history) == len(codes)
    assert {row["buy"][0] for row in history} == set(codes)
