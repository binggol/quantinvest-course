from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


module = load_module("scripts/build_advisor_pro_plus.py", "advisor_pro_plus")


def test_build_plus_classifies_overlap_candidate_and_conflict():
    advisor = {
        "current": {"as_of": "2026-07-03", "regime_label": "MOM"},
        "track": {
            "summary": {"net_ann": 0.24, "sharpe": 2.0, "winrate": 0.85, "final_nav": 5.5, "years": 7.9, "n": 33},
            "cycles": [{"name": "all", "net_ann": 0.24, "sharpe": 2.0, "n": 33}],
            "curve": {"dates": ["2025Q1"], "nav": [1.1]},
        },
        "trade": {
            "items": [
                {"code": "000001", "name": "A", "action": "买入"},
                {"code": "000002", "name": "B", "action": "持有"},
                {"code": "000003", "name": "C", "action": "卖出"},
                {"code": "000004", "name": "D", "action": "买入"},
            ]
        },
    }
    rolling = {
        "rolling": {
            "items": [
                {"code": "000001", "name": "A", "dedt_yoy": 45, "delta": 12, "ann_date": "2026-07-03"},
                {"code": "000002", "name": "B", "dedt_yoy": 35, "delta": 7, "ann_date": "2026-07-03"},
                {"code": "000003", "name": "C", "dedt_yoy": 60, "delta": 20, "ann_date": "2026-07-03"},
                {"code": "000005", "name": "E", "dedt_yoy": 80, "delta": 50, "ann_date": "2026-07-03"},
            ]
        }
    }

    result = module.build_plus(
        advisor,
        rolling,
        backtest={
            "timed": {
                "summary": {"10": {"mean_pct": 2.3, "ann_pct": 58.49, "win_rate_pct": 58.8, "sharpe": 1.1, "n": 100}},
                "by_year": {
                    "2024": {"10": {"ann_pct": 20.0}},
                    "2025": {"10": {"ann_pct": -10.0}},
                },
                "curves": {
                    "10": {
                        "dates": ["2024-01-02", "2024-01-10"],
                        "nav": [1.04, 1.092],
                        "daily_return_pct": [4.0, 5.0],
                        "n_events": [2, 1],
                    }
                },
                "rolling_portfolio_curve": {
                    "dates": ["2024-01-02", "2024-01-03"],
                    "nav": [1.0, 1.1],
                    "holding_count": [1, 1],
                },
            }
        },
    )

    assert [x["code"] for x in result["enhanced_buy"]] == ["000001"]
    assert [x["code"] for x in result["enhanced_hold"]] == ["000002"]
    assert [x["code"] for x in result["conflicts"]] == ["000003"]
    assert [x["code"] for x in result["event_candidates"]] == ["000005"]
    assert [x["code"] for x in result["base_buy"]] == ["000004"]
    assert result["summary"]["n_enhanced_buy"] == 1
    assert result["summary"]["n_event_candidates"] == 1
    assert result["backtest"]["timed"]["summary"]["10"]["mean_pct"] == 2.3
    assert result["advisor_track"]["summary"]["net_ann"] == 0.24
    assert result["performance_compare"]["advisor_pro"]["ann_pct"] == 24.0
    assert result["performance_compare"]["rolling_earnings_10d"]["ann_pct"] == 58.49
    assert result["performance_compare"]["diff"]["ann_pct"] == 34.49
    assert result["rolling_10d_curve"]["dates"] == ["2024-01-02", "2024-01-03"]
    assert result["rolling_10d_curve"]["nav"] == [1.0, 1.1]
    assert result["rolling_10d_curve"]["source"] == "timed.rolling_portfolio_curve"


if __name__ == "__main__":
    test_build_plus_classifies_overlap_candidate_and_conflict()
    print("advisor pro plus tests ok")
