"""Leakage-safe walk-forward helpers for cross-market storage research."""

import argparse
import json
import math
from pathlib import Path

import numpy as np


ENTRY_RULES = ("open", "09:35")
EXIT_RULES = ("same_close", "next_close")
SIGNAL_VARIANTS = ("us_only", "korea_daily_proxy", "us_plus_korea_proxy")


def align_us_to_a_share_day(us_date, a_share_days):
    """Map a completed US session to the next A-share trading session."""
    return next(day for day in a_share_days if day > us_date)


def walk_forward_splits(index, train_size=500, test_size=60):
    """Yield strictly ordered rolling train/test index slices."""
    splits = []
    end = train_size
    while end + test_size <= len(index):
        splits.append(
            (index[end - train_size : end], index[end : end + test_size])
        )
        end += test_size
    return splits


def maximum_drawdown(returns):
    returns = np.asarray(returns, dtype=float)
    if not len(returns):
        return 0.0
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    return round(float(np.min(equity / peak - 1.0)), 6)


def calculate_metrics(excess_returns, round_trip_cost):
    net = np.asarray(excess_returns, dtype=float) - round_trip_cost
    standard_deviation = float(net.std(ddof=1)) if len(net) > 1 else 0.0
    return {
        "samples": int(len(net)),
        "mean_excess": round(float(net.mean()), 7) if len(net) else 0.0,
        "win_rate": (
            round(float((net > 0).mean()), 4) if len(net) else 0.0
        ),
        "sharpe": (
            round(
                float(net.mean() / standard_deviation * math.sqrt(252)), 4
            )
            if standard_deviation
            else 0.0
        ),
        "max_drawdown": maximum_drawdown(net),
    }


def strategy_matrix(results_by_key, round_trip_cost=0.001):
    """Calculate all predeclared variants without selecting on test results."""
    output = {}
    for signal in SIGNAL_VARIANTS:
        for entry in ENTRY_RULES:
            for exit_rule in EXIT_RULES:
                key = f"{signal}:{entry}:{exit_rule}"
                output[key] = calculate_metrics(
                    results_by_key.get(key, []), round_trip_cost
                )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--round-trip-cost", type=float, default=0.001)
    args = parser.parse_args()
    input_data = json.loads(args.input.read_text(encoding="utf-8"))
    payload = {
        "schema_version": 1,
        "method": "walk_forward",
        "korea_history_mode": "daily_proxy",
        "strategies": strategy_matrix(
            input_data.get("excess_returns", {}), args.round_trip_cost
        ),
        "forward_simulation": input_data.get(
            "forward_simulation", {"days_observed": 0, "valid": False}
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
