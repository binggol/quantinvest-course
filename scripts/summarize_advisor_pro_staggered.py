"""Validate and combine equal-capital Advisor Pro offset sleeves."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_advisor_pro_frequency import detailed_metrics, write_json
from scripts.summarize_advisor_pro_frequency import combine_sleeves, distribution


SUPPORTED_STEPS = frozenset({1, 2, 3, 4, 12})
PERIODS = {
    "full": (None, None),
    "development_2017_2021": ("2017-01-01", "2022-01-01"),
    "validation_2022_2024": ("2022-01-01", "2025-01-01"),
    "recent_2025_plus": ("2025-01-01", None),
}
RETURN_FIELDS = {
    "long_only": "net_return",
    "exposure_matched_hedged": "hedged_return",
}
OFFSET_DISTRIBUTION_METRICS = (
    "annualized_return",
    "sharpe",
    "calmar",
    "max_drawdown",
    "rolling_252d_sharpe_p10",
)
REQUIRED_EXECUTION_PARAMETERS = (
    "rank_buffer",
    "commission",
    "max_volume_participation",
    "impact_cost",
    "risk_degree",
    "retry_days",
    "hedge_yearly_cost",
)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    return value


def _number(value: Any, location: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{location} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{location} must be a finite number")
    return result


def _integer(value: Any, location: str) -> int:
    numeric = _number(value, location)
    result = int(numeric)
    if result != numeric:
        raise ValueError(f"{location} must be an integer")
    return result


def _validate_reported_rate(
    execution: Mapping[str, Any], key: str, expected: float, location: str
) -> None:
    if key not in execution:
        return
    reported = _number(execution.get(key), f"{location}.{key}")
    if not 0 <= reported <= 1:
        raise ValueError(f"{location}.{key} must be between zero and one")
    if not math.isclose(reported, expected, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"{location}.{key} does not match the exact execution counts")


def _normalize_execution_quality(
    execution: Mapping[str, Any], index: int
) -> dict[str, Any] | None:
    location = f"runs[{index}].execution"
    count_keys = ("attempts", "trades", "unfilled")
    present = [key in execution for key in count_keys]
    if not any(present):
        return None
    if not all(present):
        missing = [key for key, is_present in zip(count_keys, present) if not is_present]
        raise ValueError(f"{location} execution counts are incomplete: missing {missing}")

    attempts = _integer(execution.get("attempts"), f"{location}.attempts")
    trades = _integer(execution.get("trades"), f"{location}.trades")
    no_fill_count = _integer(execution.get("unfilled"), f"{location}.unfilled")
    if min(attempts, trades, no_fill_count) < 0:
        raise ValueError(f"{location} execution counts cannot be negative")
    if trades + no_fill_count != attempts:
        raise ValueError(f"{location} attempts must equal trades plus unfilled")

    raw_reasons = execution.get("reason_counts")
    if not isinstance(raw_reasons, Mapping):
        raise ValueError(f"{location}.reason_counts is required with execution counts")
    reason_counts: dict[str, int] = {}
    for reason, raw_count in raw_reasons.items():
        count = _integer(raw_count, f"{location}.reason_counts.{reason}")
        if count < 0:
            raise ValueError(f"{location}.reason_counts.{reason} cannot be negative")
        reason_counts[str(reason)] = count
    if sum(reason_counts.values()) != attempts:
        raise ValueError(f"{location}.reason_counts must sum to attempts")

    partial_count = reason_counts.get("partial", 0)
    if partial_count > trades:
        raise ValueError(f"{location}.reason_counts.partial cannot exceed trades")
    incomplete_count = no_fill_count + partial_count
    if incomplete_count > attempts:
        raise ValueError(f"{location} incomplete count cannot exceed attempts")

    denominator = attempts or 1
    no_fill_rate = no_fill_count / denominator if attempts else 0.0
    partial_rate = partial_count / denominator if attempts else 0.0
    incomplete_rate = incomplete_count / denominator if attempts else 0.0
    _validate_reported_rate(execution, "no_fill_rate", no_fill_rate, location)
    _validate_reported_rate(execution, "partial_fill_rate", partial_rate, location)
    _validate_reported_rate(
        execution, "incomplete_fill_rate", incomplete_rate, location
    )
    return {
        "attempts": attempts,
        "trades": trades,
        "no_fill_count": no_fill_count,
        "no_fill_rate": no_fill_rate,
        "partial_count": partial_count,
        "partial_rate": partial_rate,
        "incomplete_count": incomplete_count,
        "incomplete_rate": incomplete_rate,
    }


def _unwrap_run(raw: Any, index: int) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    wrapper = _mapping(raw, f"runs[{index}]")
    if "result" not in wrapper:
        return wrapper, {}
    status = wrapper.get("status")
    if status != "success":
        raise ValueError(f"runs[{index}] checkpoint status must be 'success'")
    raw_spec = wrapper.get("spec", {})
    return _mapping(wrapper["result"], f"runs[{index}].result"), _mapping(
        raw_spec, f"runs[{index}].spec"
    )


def _normalize_daily_path(run: Mapping[str, Any], index: int) -> list[dict[str, Any]]:
    raw_path = run.get("daily_path")
    if not isinstance(raw_path, list) or not raw_path:
        raise ValueError(f"runs[{index}].daily_path must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    dates: list[pd.Timestamp] = []
    cost_presence: list[bool] = []
    for row_index, raw_row in enumerate(raw_path):
        row = _mapping(raw_row, f"runs[{index}].daily_path[{row_index}]")
        try:
            trade_date = pd.Timestamp(row.get("date")).normalize()
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"runs[{index}].daily_path[{row_index}].date is invalid"
            ) from exc
        if pd.isna(trade_date):
            raise ValueError(f"runs[{index}].daily_path[{row_index}].date is invalid")
        normalized_row = {
            "date": trade_date.strftime("%Y-%m-%d"),
            "net_return": _number(
                row.get("net_return"),
                f"runs[{index}].daily_path[{row_index}].net_return",
            ),
            "hedged_return": _number(
                row.get("hedged_return"),
                f"runs[{index}].daily_path[{row_index}].hedged_return",
            ),
        }
        has_cost_rate = "cost_rate" in row
        cost_presence.append(has_cost_rate)
        if has_cost_rate:
            cost_rate = _number(
                row.get("cost_rate"),
                f"runs[{index}].daily_path[{row_index}].cost_rate",
            )
            if cost_rate < 0:
                raise ValueError(
                    f"runs[{index}].daily_path[{row_index}].cost_rate cannot be negative"
                )
            normalized_row["cost_rate"] = cost_rate
        normalized.append(normalized_row)
        dates.append(trade_date)
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError(f"runs[{index}].daily_path dates must be unique and increasing")
    if any(cost_presence) and not all(cost_presence):
        raise ValueError(
            f"runs[{index}].daily_path cost_rate must be present on every row or omitted"
        )
    return normalized


def _normalize_run(raw: Any, index: int) -> dict[str, Any]:
    run, sweep_spec = _unwrap_run(raw, index)
    portfolio_raw = run.get("portfolio_spec")
    if portfolio_raw is None:
        portfolio_raw = sweep_spec
    portfolio = _mapping(portfolio_raw, f"runs[{index}].portfolio_spec")
    execution = _mapping(run.get("execution"), f"runs[{index}].execution")
    execution_parameters_raw = run.get("execution_parameters")
    if execution_parameters_raw is None:
        execution_parameters_raw = sweep_spec.get("run_parameters")
    execution_parameters = dict(
        _mapping(execution_parameters_raw, f"runs[{index}].execution_parameters")
    )
    for key in REQUIRED_EXECUTION_PARAMETERS:
        if key not in execution_parameters:
            raise ValueError(f"runs[{index}].execution_parameters.{key} is required")

    frequency_days = _integer(
        run.get("frequency_days", sweep_spec.get("frequency_days")),
        f"runs[{index}].frequency_days",
    )
    frequency_step = max(1, round(frequency_days / 5))
    reported_step = run.get("frequency_step")
    if reported_step is not None and _integer(
        reported_step, f"runs[{index}].frequency_step"
    ) != frequency_step:
        raise ValueError(f"runs[{index}].frequency_step does not match frequency_days")
    if frequency_step not in SUPPORTED_STEPS:
        raise ValueError(
            f"runs[{index}] frequency step {frequency_step} is unsupported; "
            f"expected one of {sorted(SUPPORTED_STEPS)}"
        )

    offset = _integer(
        run.get("frequency_offset", sweep_spec.get("frequency_offset")),
        f"runs[{index}].frequency_offset",
    )
    topn = _integer(
        portfolio.get("portfolio_topn", sweep_spec.get("portfolio_topn")),
        f"runs[{index}].portfolio_topn",
    )
    max_replacements_raw = portfolio.get(
        "max_replacements", sweep_spec.get("max_replacements")
    )
    max_replacements = (
        None
        if max_replacements_raw is None
        else _integer(max_replacements_raw, f"runs[{index}].max_replacements")
    )
    rebalance_mode = portfolio.get("rebalance_mode", sweep_spec.get("rebalance_mode"))
    if rebalance_mode not in ("target_weight", "replace_only"):
        raise ValueError(f"runs[{index}].rebalance_mode is invalid")
    account = _number(
        portfolio.get("account", sweep_spec.get("account")),
        f"runs[{index}].account",
    )
    if topn < 1 or account <= 0:
        raise ValueError(f"runs[{index}] portfolio_topn and account must be positive")
    if not 0 <= offset < frequency_step:
        raise ValueError(f"runs[{index}].frequency_offset is outside the frequency step")
    if max_replacements is not None and not 0 <= max_replacements <= topn:
        raise ValueError(f"runs[{index}].max_replacements is outside portfolio_topn")

    settlement_mode = execution.get("settlement_mode")
    final_holding_count = _integer(
        execution.get("final_holding_count"),
        f"runs[{index}].execution.final_holding_count",
    )
    if settlement_mode != "liquidated" or final_holding_count != 0:
        raise ValueError(f"runs[{index}] must be fully liquidated")
    turnover = _number(
        execution.get("annualized_one_way_turnover"),
        f"runs[{index}].execution.annualized_one_way_turnover",
    )
    if turnover < 0:
        raise ValueError(f"runs[{index}] turnover cannot be negative")
    execution_quality = _normalize_execution_quality(execution, index)

    daily_path = _normalize_daily_path(run, index)
    return {
        "frequency_offset": offset,
        "spec": {
            "portfolio_topn": topn,
            "frequency_days": frequency_days,
            "frequency_step": frequency_step,
            "max_replacements": max_replacements,
            "rebalance_mode": rebalance_mode,
            "account": account,
        },
        "execution_parameters": execution_parameters,
        "annualized_one_way_turnover": turnover,
        "execution_quality": execution_quality,
        "daily_path": daily_path,
        "has_daily_cost_rate": "cost_rate" in daily_path[0],
    }


def _period_metrics(returns: pd.Series) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, (start, end) in PERIODS.items():
        mask = pd.Series(True, index=returns.index)
        if start is not None:
            mask &= returns.index >= pd.Timestamp(start)
        if end is not None:
            mask &= returns.index < pd.Timestamp(end)
        result[name] = detailed_metrics(returns.loc[mask])
    return result


def _offset_metric_distributions(
    offset_rows: Sequence[Mapping[str, Any]], basis: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for period in PERIODS:
        distributions: dict[str, Any] = {}
        for metric in OFFSET_DISTRIBUTION_METRICS:
            values = [row[basis][period].get(metric) for row in offset_rows]
            clean = [float(value) for value in values if value is not None]
            distributions[metric] = distribution(clean) if clean else None
        result[period] = distributions
    return result


def _min_max(values: Sequence[int | float]) -> dict[str, int | float]:
    return {"min": min(values), "max": max(values)}


def _execution_quality_summary(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unavailable_offsets = [
        int(run["frequency_offset"])
        for run in runs
        if run["execution_quality"] is None
    ]
    result: dict[str, Any] = {
        "available": not unavailable_offsets,
        "all_liquidated": True,
        "unavailable_offsets": unavailable_offsets,
        "aggregate": None,
        "per_offset_ranges": None,
    }
    if unavailable_offsets:
        return result

    rows = [run["execution_quality"] for run in runs]
    attempts = sum(row["attempts"] for row in rows)
    trades = sum(row["trades"] for row in rows)
    no_fill_count = sum(row["no_fill_count"] for row in rows)
    partial_count = sum(row["partial_count"] for row in rows)
    incomplete_count = no_fill_count + partial_count
    denominator = attempts or 1
    aggregate = {
        "attempts": attempts,
        "trades": trades,
        "no_fill_count": no_fill_count,
        "no_fill_rate": no_fill_count / denominator if attempts else 0.0,
        "partial_count": partial_count,
        "partial_rate": partial_count / denominator if attempts else 0.0,
        "incomplete_count": incomplete_count,
        "incomplete_rate": incomplete_count / denominator if attempts else 0.0,
    }
    range_fields = tuple(aggregate)
    result["aggregate"] = aggregate
    result["per_offset_ranges"] = {
        field: _min_max([row[field] for row in rows]) for field in range_fields
    }
    return result


def summarize_staggered(
    payload: Mapping[str, Any], *, source: str | Path = "<memory>"
) -> dict[str, Any]:
    raw_runs = payload.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("runs must be a non-empty array")
    runs = sorted(
        (_normalize_run(raw, index) for index, raw in enumerate(raw_runs)),
        key=lambda item: item["frequency_offset"],
    )
    base_spec = runs[0]["spec"]
    base_parameters = runs[0]["execution_parameters"]
    for run in runs[1:]:
        if run["spec"] != base_spec:
            raise ValueError("all offset sleeves must have the same portfolio specification")
        if run["execution_parameters"] != base_parameters:
            raise ValueError("all offset sleeves must have the same execution parameters")

    step = int(base_spec["frequency_step"])
    offsets = [int(run["frequency_offset"]) for run in runs]
    expected_offsets = list(range(step))
    if offsets != expected_offsets:
        raise ValueError(
            f"offsets must be complete and unique: expected {expected_offsets}, found {offsets}"
        )

    offset_rows: list[dict[str, Any]] = []
    for run in runs:
        row: dict[str, Any] = {
            "frequency_offset": run["frequency_offset"],
            "annualized_one_way_turnover": run["annualized_one_way_turnover"],
            "execution_quality": run["execution_quality"],
        }
        for basis, return_field in RETURN_FIELDS.items():
            frame = pd.DataFrame(run["daily_path"])
            series = pd.Series(
                frame[return_field].to_numpy(dtype=float),
                index=pd.to_datetime(frame["date"]).dt.normalize(),
                dtype=float,
            )
            row[basis] = _period_metrics(series)
        offset_rows.append(row)

    staggered: dict[str, Any] = {}
    combine_input = [{"daily_path": run["daily_path"]} for run in runs]
    for basis, return_field in RETURN_FIELDS.items():
        staggered[basis] = _period_metrics(
            combine_sleeves(combine_input, return_field=return_field)
        )

    cost_offsets = [
        int(run["frequency_offset"])
        for run in runs
        if not run["has_daily_cost_rate"]
    ]
    staggered_double_cost: dict[str, Any] | None = None
    if not cost_offsets:
        staggered_double_cost = {}
        for basis, return_field in RETURN_FIELDS.items():
            stressed_runs = [
                {
                    "daily_path": [
                        {
                            "date": row["date"],
                            "stressed_return": row[return_field] - row["cost_rate"],
                        }
                        for row in run["daily_path"]
                    ]
                }
                for run in runs
            ]
            staggered_double_cost[basis] = _period_metrics(
                combine_sleeves(stressed_runs, return_field="stressed_return")
            )

    turnover_distribution = distribution(
        run["annualized_one_way_turnover"] for run in runs
    )
    return {
        "schema_version": 3,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "research_staggered_summary_not_published",
        "source": str(Path(source).resolve()) if str(source) != "<memory>" else "<memory>",
        "construction": (
            "equal initial capital per offset sleeve; sleeves retain their own capital; "
            "no capital transfers between sleeves"
        ),
        "portfolio_spec": base_spec,
        "execution_parameters": base_parameters,
        "offsets": {
            "expected": expected_offsets,
            "observed": offsets,
            "count": len(offsets),
        },
        "staggered": staggered,
        "double_cost": {
            "available": staggered_double_cost is not None,
            "definition": (
                "subtract each sleeve's realized daily cost_rate once more from its "
                "already-net return before equal-capital NAV combination"
            ),
            "missing_offsets": cost_offsets,
            "staggered": staggered_double_cost,
        },
        "offset_distribution": {
            basis: _offset_metric_distributions(offset_rows, basis)
            for basis in RETURN_FIELDS
        },
        "turnover": {
            "annualized_one_way_turnover": turnover_distribution,
            "median": turnover_distribution["median"],
        },
        "execution_quality": _execution_quality_summary(runs),
        "offset_runs": offset_rows,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    source = Path(args.input)
    payload = _mapping(
        json.loads(source.read_text(encoding="utf-8-sig")), str(source)
    )
    result = summarize_staggered(payload, source=source)
    write_json(Path(args.out), result)
    print(
        json.dumps(
            {
                "out": str(Path(args.out).resolve()),
                "offset_count": result["offsets"]["count"],
                "portfolio_spec": result["portfolio_spec"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
