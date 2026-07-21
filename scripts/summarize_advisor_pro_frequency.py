"""Summarize frequency-offset runs and combine staggered monthly sleeves."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from statistics import median
import sys
from typing import Any, Iterable

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_advisor_pro_frequency import detailed_metrics, write_json


METRICS = {
    "long_annualized_return": ("long_only", "full", "annualized_return"),
    "long_sharpe": ("long_only", "full", "sharpe"),
    "long_max_drawdown": ("long_only", "full", "max_drawdown"),
    "hedged_proxy_annualized_return": (
        "exposure_matched_hedged", "full", "annualized_return",
    ),
    "hedged_proxy_sharpe": ("exposure_matched_hedged", "full", "sharpe"),
    "hedged_proxy_max_drawdown": (
        "exposure_matched_hedged", "full", "max_drawdown",
    ),
    "hedged_proxy_2022_annualized_return": (
        "exposure_matched_hedged", "2022_plus", "annualized_return",
    ),
    "hedged_proxy_2022_sharpe": (
        "exposure_matched_hedged", "2022_plus", "sharpe",
    ),
    "hedged_proxy_2022_max_drawdown": (
        "exposure_matched_hedged", "2022_plus", "max_drawdown",
    ),
    "hedged_proxy_double_cost_annualized_return": (
        "exposure_matched_hedged", "double_cost_full", "annualized_return",
    ),
    "annualized_one_way_turnover": ("execution", "annualized_one_way_turnover"),
}


def nested(payload: dict[str, Any], path: Iterable[str]) -> float:
    value: Any = payload
    for key in path:
        value = value[key]
    return float(value)


def distribution(values: Iterable[float]) -> dict[str, float]:
    clean = sorted(float(value) for value in values if np.isfinite(float(value)))
    if not clean:
        raise ValueError("metric distribution is empty")
    return {
        "min": round(clean[0], 6),
        "median": round(float(median(clean)), 6),
        "max": round(clean[-1], 6),
    }


def summarize_offsets(runs: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        grouped.setdefault(str(run["frequency_label"]), []).append(run)
    result: dict[str, Any] = {}
    for label, items in grouped.items():
        result[label] = {
            "offsets": len(items),
            "all_liquidated": all(
                item["execution"]["settlement_mode"] == "liquidated"
                and int(item["execution"]["final_holding_count"]) == 0
                for item in items
            ),
            "metrics": {
                name: distribution(nested(item, path) for item in items)
                for name, path in METRICS.items()
            },
        }
    return result


def combine_sleeves(
    runs: list[dict[str, Any]], *, return_field: str
) -> pd.Series:
    if not runs:
        raise ValueError("at least one sleeve is required")
    sleeve_returns: list[pd.Series] = []
    all_dates = pd.DatetimeIndex([])
    for run in runs:
        frame = pd.DataFrame(run["daily_path"])
        series = pd.Series(
            pd.to_numeric(frame[return_field], errors="coerce").fillna(0.0).to_numpy(),
            index=pd.to_datetime(frame["date"]).dt.normalize(),
            dtype=float,
        )
        sleeve_returns.append(series)
        all_dates = all_dates.union(series.index)
    all_dates = all_dates.sort_values()
    navs = []
    for series in sleeve_returns:
        aligned = series.reindex(all_dates, fill_value=0.0)
        navs.append((1.0 + aligned).cumprod())
    combined_nav = pd.concat(navs, axis=1).mean(axis=1)
    combined_returns = combined_nav.pct_change()
    combined_returns.iloc[0] = combined_nav.iloc[0] - 1.0
    return combined_returns


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    source = Path(args.input)
    payload = json.loads(source.read_text(encoding="utf-8"))
    runs = list(payload.get("runs") or [])
    monthly = [run for run in runs if run.get("frequency_label") == "monthly"]
    if len(monthly) != 4:
        raise ValueError(f"expected four monthly offsets, found {len(monthly)}")

    long_returns = combine_sleeves(monthly, return_field="net_return")
    hedged_returns = combine_sleeves(monthly, return_field="hedged_return")
    result = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source.resolve()),
        "offset_summary": summarize_offsets(runs),
        "staggered_monthly_four_sleeves": {
            "construction": (
                "equal initial capital in four monthly sleeves, one sleeve per weekly offset; "
                "no capital transfers between sleeves"
            ),
            "long_only": detailed_metrics(long_returns),
            "long_only_2022_plus": detailed_metrics(
                long_returns.loc[long_returns.index >= pd.Timestamp("2022-01-01")]
            ),
            "hedged_proxy": detailed_metrics(hedged_returns),
            "hedged_proxy_2022_plus": detailed_metrics(
                hedged_returns.loc[hedged_returns.index >= pd.Timestamp("2022-01-01")]
            ),
            "hedged_proxy_2025_plus": detailed_metrics(
                hedged_returns.loc[hedged_returns.index >= pd.Timestamp("2025-01-01")]
            ),
            "turnover_estimate": distribution(
                run["execution"]["annualized_one_way_turnover"] for run in monthly
            ),
            "capacity_note": (
                "Each source sleeve was simulated at the full account size, so combining them "
                "is conservative for nonlinear market impact at one-quarter capital per sleeve."
            ),
        },
    }
    write_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
