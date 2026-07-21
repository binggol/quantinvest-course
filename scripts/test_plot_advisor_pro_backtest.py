from __future__ import annotations

import pandas as pd
import pytest

from scripts.plot_advisor_pro_backtest import (
    annual_returns,
    combine_equal_capital,
    drawdown_path,
    nav_path,
)


def _payload(cost: bool = True) -> dict:
    def row(date: str, value: float, cost_rate: float) -> dict:
        result = {"date": date, "net_return": value}
        if cost:
            result["cost_rate"] = cost_rate
        return result

    return {
        "runs": [
            {
                "frequency_days": 10,
                "frequency_offset": 0,
                "daily_path": [
                    row("2024-01-02", 0.10, 0.01),
                    row("2024-01-03", 0.00, 0.00),
                ],
            },
            {
                "frequency_days": 10,
                "frequency_offset": 1,
                "daily_path": [
                    row("2024-01-02", 0.00, 0.00),
                    row("2024-01-03", 0.20, 0.02),
                ],
            },
        ]
    }


def test_combine_equal_capital_uses_sleeve_navs() -> None:
    combined = combine_equal_capital(_payload())
    nav = nav_path(combined)
    assert nav.iloc[0] == 1.0
    assert nav.iloc[1] == pytest.approx((1.10 + 1.00) / 2.0)
    assert nav.iloc[2] == pytest.approx((1.10 + 1.20) / 2.0)


def test_combine_equal_capital_double_cost_stresses_each_sleeve() -> None:
    combined = combine_equal_capital(_payload(), double_cost=True)
    nav = nav_path(combined)
    assert nav.iloc[0] == 1.0
    assert nav.iloc[1] == pytest.approx((1.09 + 1.00) / 2.0)
    assert nav.iloc[2] == pytest.approx((1.09 + 1.18) / 2.0)


def test_combine_equal_capital_requires_complete_offsets() -> None:
    payload = _payload()
    payload["runs"].pop()
    with pytest.raises(ValueError, match="offsets must be complete"):
        combine_equal_capital(payload)


def test_double_cost_requires_cost_rate() -> None:
    with pytest.raises(ValueError, match="missing cost_rate"):
        combine_equal_capital(_payload(cost=False), double_cost=True)


def test_drawdown_and_annual_return() -> None:
    index = pd.to_datetime(["2023-12-29", "2024-01-02", "2024-01-03"])
    returns = pd.Series([0.10, -0.20, 0.25], index=index)
    drawdown = drawdown_path(returns)
    assert drawdown.iloc[0] == 0.0
    assert drawdown.iloc[2] == pytest.approx(-0.20)
    annual = annual_returns(returns)
    assert annual.iloc[0] == pytest.approx(0.10)
    assert annual.iloc[1] == pytest.approx(0.0)
