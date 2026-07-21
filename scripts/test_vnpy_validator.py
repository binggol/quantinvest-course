from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.backtest_engine.vnpy_validator import (
    QuantInvestPortfolioEngine,
    VNPY_AVAILABLE,
    derive_market_state,
)


def quote(**overrides):
    result = {
        "open": 10.0,
        "high": 10.2,
        "low": 9.8,
        "close": 10.0,
        "change": 0.0,
        "volume_lots": 10_000.0,
        "adj": 1.0,
        "max_adj": 1.0,
    }
    result.update(overrides)
    return result


def test_validator_module_has_no_qlib_imports():
    path = Path(__file__).parent / "backtest_engine" / "vnpy_validator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not [name for name in imports if name == "qlib" or name.startswith("qlib.")]


def test_market_state_uses_directional_limit_and_chinext_boundary():
    old = derive_market_state(
        "SZ300001", date(2020, 8, 21), quote(open=11.0, high=11.0, close=11.0, change=0.1)
    )
    new = derive_market_state(
        "SZ300001", date(2020, 8, 24), quote(open=11.0, high=11.0, close=11.0, change=0.1)
    )
    assert old.limit_pct == pytest.approx(0.10)
    assert old.limit_buy
    assert new.limit_pct == pytest.approx(0.20)
    assert not new.limit_buy


def test_market_state_rejects_missing_or_zero_volume_bar():
    state = derive_market_state("SH600000", date(2026, 7, 10), quote(volume_lots=0))
    assert state.suspended
    assert state.limit_buy and state.limit_sell


def test_explicit_no_limit_window_does_not_block_large_move():
    state = derive_market_state(
        "SH688999",
        date(2026, 7, 10),
        quote(open=15, high=16, low=14, close=15, change=0.5, has_price_limit=False),
    )
    assert not state.suspended
    assert not state.limit_buy
    assert not state.limit_sell


def test_explicit_st_flag_uses_five_percent_limit():
    state = derive_market_state(
        "SH600001",
        date(2026, 7, 10),
        quote(open=10.5, high=10.5, low=10.5, close=10.5, change=0.05, is_st=True),
    )
    assert state.limit_pct == pytest.approx(0.05)
    assert state.limit_buy
    assert state.rule_source == "explicit"


@dataclass(frozen=True)
class FakeBundle:
    manifest: dict
    targets: dict
    quotes: pd.DataFrame
    config: dict
    provenance: dict


@pytest.mark.skipif(not VNPY_AVAILABLE, reason="vn.py is installed only in validator environment")
def test_vnpy_engine_executes_target_at_same_day_open_and_finishes_flat():
    rows = []
    for day in ("2026-01-02", "2026-01-05"):
        for code, price in (("SH600000", 10.0), ("SH000300", 100.0)):
            rows.append(
                {
                    "date": day,
                    "instrument": code,
                    **quote(open=price, high=price, low=price, close=price),
                }
            )
    bundle = FakeBundle(
        manifest={"schema_version": 1, "files": {}},
        targets={"2026-01-02": {"SH600000": 1.0}, "2026-01-05": {}},
        quotes=pd.DataFrame(rows),
        config={
            "account": 1_000_000,
            "risk_degree": 0.95,
            "retry_days": 5,
            "commission": 0.0003,
            "max_volume_participation": 0.10,
            "impact_cost": 0.10,
            "hedge_yearly_cost": 0.01,
            "trade_unit": 100,
            "volume_unit_multiplier": 100,
            "backtest_end_after_final_retry": "2026-01-05",
            "benchmark": "SH000300",
        },
        provenance={},
    )

    result = QuantInvestPortfolioEngine(bundle).run_validation()

    assert result["execution_audit"][0]["trade_date"] == "2026-01-02"
    assert result["execution_audit"][0]["side"] == "buy"
    assert result["execution_audit"][-1]["trade_date"] == "2026-01-05"
    assert result["execution_audit"][-1]["side"] == "sell"
    assert result["final_position"]["holding_count"] == 0
    assert result["orders"]["trades"] == 2
