"""Merge, validate, and rank Advisor Pro portfolio-sweep checkpoints."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
from statistics import median
import tempfile
from typing import Any, Iterable, Mapping, Sequence


VALIDATION_PERIOD = "2022-2024"
RECENT_PERIOD = "2025+"
MIN_VALIDATION_OBSERVATIONS = 500
MAX_VALIDATION_DRAWDOWN = -0.25
MAX_NO_FILL_RATE = 0.10
SELECTION_BASES = ("long_only", "exposure_matched_hedged")

SCORE_OBJECTIVES = {
    "annualized_return": "max",
    "sharpe": "max",
    "calmar": "max",
    "rolling_252d_sharpe_p10": "max",
    "max_drawdown": "max",
    "worst_60d": "max",
    "double_cost_annualized_return": "max",
    "annualized_one_way_turnover": "min",
}

STABILITY_METRICS = (
    "sharpe",
    "calmar",
    "rolling_252d_sharpe_p10",
    "max_drawdown",
    "worst_60d",
    "double_cost_annualized_return",
    "annualized_one_way_turnover",
)

REQUIRED_SPEC_FIELDS = (
    "portfolio_topn",
    "frequency_days",
    "frequency_offset",
    "replacement_ratio",
    "max_replacements",
    "rebalance_mode",
    "account",
)

REQUIRED_PERIOD_METRICS = (
    "n",
    "annualized_return",
    "sharpe",
    "calmar",
    "rolling_252d_sharpe_p10",
    "max_drawdown",
    "worst_60d",
)

REQUIRED_EXECUTION_FIELDS = (
    "final_holding_count",
    "settlement_mode",
    "annualized_one_way_turnover",
)


def _location(source: Path, index: int, suffix: str = "") -> str:
    base = f"{source}:runs[{index}]"
    return f"{base}.{suffix}" if suffix else base


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    return value


def _require_string(payload: Mapping[str, Any], key: str, location: str) -> str:
    if key not in payload:
        raise ValueError(f"{location}.{key} is required")
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.{key} must be a non-empty string")
    return value.strip()


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


def _required_number(payload: Mapping[str, Any], key: str, location: str) -> float:
    if key not in payload:
        raise ValueError(f"{location}.{key} is required")
    return _number(payload[key], f"{location}.{key}")


def _optional_number(payload: Mapping[str, Any], key: str, location: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    return _number(value, f"{location}.{key}")


def _normalize_spec(raw: Any, location: str) -> dict[str, Any]:
    spec = _require_mapping(raw, location)
    for key in REQUIRED_SPEC_FIELDS:
        if key not in spec:
            raise ValueError(f"{location}.{key} is required")

    topn = int(_required_number(spec, "portfolio_topn", location))
    frequency_days = int(_required_number(spec, "frequency_days", location))
    frequency_offset = int(_required_number(spec, "frequency_offset", location))
    account = _required_number(spec, "account", location)
    max_replacements_raw = spec.get("max_replacements")
    max_replacements = (
        None
        if max_replacements_raw is None
        else int(_number(max_replacements_raw, f"{location}.max_replacements"))
    )
    replacement_ratio = _optional_number(spec, "replacement_ratio", location)
    rebalance_mode = _require_string(spec, "rebalance_mode", location)

    if topn < 1:
        raise ValueError(f"{location}.portfolio_topn must be positive")
    if frequency_days < 1:
        raise ValueError(f"{location}.frequency_days must be positive")
    if frequency_offset < 0:
        raise ValueError(f"{location}.frequency_offset cannot be negative")
    if account <= 0:
        raise ValueError(f"{location}.account must be positive")
    if max_replacements is not None and not 0 <= max_replacements <= topn:
        raise ValueError(
            f"{location}.max_replacements must be between zero and portfolio_topn"
        )
    if replacement_ratio is not None and not 0 <= replacement_ratio <= 1:
        raise ValueError(f"{location}.replacement_ratio must be between zero and one")

    normalized = dict(spec)
    normalized.update(
        {
            "portfolio_topn": topn,
            "frequency_days": frequency_days,
            "frequency_offset": frequency_offset,
            "replacement_ratio": replacement_ratio,
            "max_replacements": max_replacements,
            "rebalance_mode": rebalance_mode,
            "account": account,
        }
    )
    return normalized


def _basis_keys(basis: str) -> tuple[str, ...]:
    if basis == "long_only":
        return ("long_only",)
    if basis == "exposure_matched_hedged":
        return ("exposure_matched_hedged", "hedged", "hedged_proxy")
    raise ValueError(f"basis must be one of: {', '.join(SELECTION_BASES)}")


def _strategy_periods(
    result: Mapping[str, Any], location: str, *, basis: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    basis_keys = _basis_keys(basis)
    evaluation = result.get("evaluation_periods")
    if evaluation is not None:
        periods = _require_mapping(evaluation, f"{location}.evaluation_periods")
        validation_block = periods.get("validation_2022_2024")
        recent_block = periods.get("recent_2025_plus")
        validation = _require_mapping(
            validation_block,
            f"{location}.evaluation_periods.validation_2022_2024",
        )
        recent = _require_mapping(
            recent_block,
            f"{location}.evaluation_periods.recent_2025_plus",
        )
        for key in basis_keys:
            if key in validation and key in recent:
                return (
                    _require_mapping(
                        validation[key],
                        f"{location}.evaluation_periods.validation_2022_2024.{key}",
                    ),
                    _require_mapping(
                        recent[key],
                        f"{location}.evaluation_periods.recent_2025_plus.{key}",
                    ),
                )
        raise ValueError(
            f"{location}.evaluation_periods validation/recent blocks must contain "
            f"{basis}"
        )

    strategy: Mapping[str, Any] | None = None
    strategy_name = ""
    for key in basis_keys:
        if key in result:
            strategy = _require_mapping(result[key], f"{location}.{key}")
            strategy_name = key
            break
    if strategy is None:
        raise ValueError(f"{location}.{basis} is required for validation scoring")

    validation = strategy.get("validation_2022_2024", strategy.get("2022_2024"))
    recent = strategy.get("recent_2025_plus", strategy.get("2025_plus"))
    return (
        _require_mapping(
            validation,
            f"{location}.{strategy_name}.validation_2022_2024",
        ),
        _require_mapping(
            recent,
            f"{location}.{strategy_name}.recent_2025_plus",
        ),
    )


def _normalize_period(
    raw: Mapping[str, Any],
    location: str,
    *,
    require_double_cost: bool,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in REQUIRED_PERIOD_METRICS:
        value = _required_number(raw, key, location)
        normalized[key] = int(value) if key == "n" else value
    if normalized["n"] < 0:
        raise ValueError(f"{location}.n cannot be negative")

    double_cost = raw.get("double_cost_annualized_return")
    if require_double_cost and double_cost is None:
        raise ValueError(
            f"{location}.double_cost_annualized_return is required; a full-period "
            "double-cost metric cannot be used because 2025+ must not affect screening"
        )
    if double_cost is not None:
        normalized["double_cost_annualized_return"] = _number(
            double_cost,
            f"{location}.double_cost_annualized_return",
        )
    for key in ("rolling_252d_return_p10", "annualized_cost_drag"):
        value = _optional_number(raw, key, location)
        if value is not None:
            normalized[key] = value
    return normalized


def _normalize_execution(raw: Any, location: str) -> dict[str, Any]:
    execution = _require_mapping(raw, location)
    for key in REQUIRED_EXECUTION_FIELDS:
        if key not in execution:
            raise ValueError(f"{location}.{key} is required")

    normalized = dict(execution)
    final_holding_count = int(
        _required_number(execution, "final_holding_count", location)
    )
    settlement_mode = _require_string(execution, "settlement_mode", location)
    turnover = _required_number(execution, "annualized_one_way_turnover", location)
    no_fill_rate = _optional_number(execution, "no_fill_rate", location)
    if no_fill_rate is None:
        attempts = _optional_number(execution, "attempts", location)
        unfilled = _optional_number(execution, "unfilled", location)
        if attempts is None or unfilled is None or attempts <= 0:
            raise ValueError(
                f"{location}.no_fill_rate is required when attempts/unfilled cannot derive it"
            )
        no_fill_rate = unfilled / attempts

    if final_holding_count < 0:
        raise ValueError(f"{location}.final_holding_count cannot be negative")
    if turnover < 0:
        raise ValueError(f"{location}.annualized_one_way_turnover cannot be negative")
    if not 0 <= no_fill_rate <= 1:
        raise ValueError(f"{location}.no_fill_rate must be between zero and one")

    normalized.update(
        {
            "final_holding_count": final_holding_count,
            "settlement_mode": settlement_mode,
            "annualized_one_way_turnover": turnover,
            "no_fill_rate": no_fill_rate,
        }
    )
    for key in ("partial_fill_rate", "annualized_cost_rate"):
        value = _optional_number(execution, key, location)
        if value is not None:
            normalized[key] = value
    return normalized


def _normalize_success(
    checkpoint: Mapping[str, Any], source: Path, index: int, *, basis: str
) -> dict[str, Any]:
    location = _location(source, index)
    run_id = _require_string(checkpoint, "run_id", location)
    spec = _normalize_spec(checkpoint.get("spec"), f"{location}.spec")
    result = _require_mapping(checkpoint.get("result"), f"{location}.result")
    validation_raw, recent_raw = _strategy_periods(
        result, f"{location}.result", basis=basis
    )
    validation = _normalize_period(
        validation_raw,
        f"{location}.result.validation_2022_2024.{basis}",
        require_double_cost=True,
    )
    recent = _normalize_period(
        recent_raw,
        f"{location}.result.recent_2025_plus.{basis}",
        require_double_cost=False,
    )
    execution = _normalize_execution(
        result.get("execution"),
        f"{location}.result.execution",
    )
    return {
        "run_id": run_id,
        "status": "success",
        "spec": spec,
        "validation_2022_2024": validation,
        "recent_2025_plus": recent,
        "execution": execution,
        "sources": [location],
    }


def _normalize_failure(
    checkpoint: Mapping[str, Any], source: Path, index: int
) -> dict[str, Any]:
    location = _location(source, index)
    run_id = _require_string(checkpoint, "run_id", location)
    spec = _normalize_spec(checkpoint.get("spec"), f"{location}.spec")
    error = _require_mapping(checkpoint.get("error"), f"{location}.error")
    error_type = _require_string(error, "type", f"{location}.error")
    message = _require_string(error, "message", f"{location}.error")
    return {
        "run_id": run_id,
        "status": "failed",
        "spec": spec,
        "error": {"type": error_type, "message": message},
        "sources": [location],
    }


def load_checkpoints(
    paths: Sequence[Path], *, basis: str = "long_only"
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not paths:
        raise ValueError("at least one input chunk is required")
    _basis_keys(basis)
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    input_counts: dict[str, int] = {}

    for source in paths:
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise ValueError(f"input chunk does not exist: {source}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {source}: {exc}") from exc
        root = _require_mapping(payload, str(source))
        runs = root.get("runs")
        if not isinstance(runs, list):
            raise ValueError(f"{source}.runs must be an array")
        input_counts[str(source)] = len(runs)

        for index, raw_checkpoint in enumerate(runs):
            checkpoint = _require_mapping(raw_checkpoint, _location(source, index))
            status = _require_string(checkpoint, "status", _location(source, index))
            if status == "success":
                normalized = _normalize_success(
                    checkpoint, source, index, basis=basis
                )
            elif status == "failed":
                normalized = _normalize_failure(checkpoint, source, index)
            else:
                raise ValueError(
                    f"{_location(source, index, 'status')} must be 'success' or 'failed'"
                )

            run_id = normalized["run_id"]
            previous = by_id.get(run_id)
            if previous is None:
                by_id[run_id] = normalized
                continue
            duplicate_ids.add(run_id)
            if previous["spec"] != normalized["spec"]:
                raise ValueError(
                    f"duplicate run_id {run_id!r} has conflicting spec values at "
                    f"{previous['sources'][0]} and {normalized['sources'][0]}"
                )
            sources = previous["sources"] + normalized["sources"]
            if previous["status"] == "failed" and normalized["status"] == "success":
                normalized["sources"] = sources
                by_id[run_id] = normalized
            elif previous["status"] == "success" and normalized["status"] == "failed":
                previous["sources"] = sources
            elif previous["status"] == "failed":
                previous["sources"] = sources
            else:
                comparable_keys = (
                    "validation_2022_2024",
                    "recent_2025_plus",
                    "execution",
                )
                if any(previous[key] != normalized[key] for key in comparable_keys):
                    raise ValueError(
                        f"duplicate run_id {run_id!r} has conflicting successful results at "
                        f"{previous['sources'][0]} and {normalized['sources'][0]}"
                    )
                previous["sources"] = sources

    return list(by_id.values()), {
        "input_run_counts": input_counts,
        "duplicate_run_ids": sorted(duplicate_ids),
        "duplicate_count": len(duplicate_ids),
    }


def evaluate_hard_gates(run: Mapping[str, Any]) -> dict[str, Any]:
    validation = run["validation_2022_2024"]
    execution = run["execution"]
    checks = {
        "minimum_validation_observations": validation["n"] >= MIN_VALIDATION_OBSERVATIONS,
        "positive_validation_return": validation["annualized_return"] > 0,
        "positive_validation_double_cost_return": (
            validation["double_cost_annualized_return"] > 0
        ),
        "validation_drawdown_within_25pct": (
            validation["max_drawdown"] >= MAX_VALIDATION_DRAWDOWN
        ),
        "liquidated": (
            execution["settlement_mode"] == "liquidated"
            and execution["final_holding_count"] == 0
        ),
        "no_fill_rate_at_most_10pct": execution["no_fill_rate"] <= MAX_NO_FILL_RATE,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {"passed": not failed, "checks": checks, "failed_checks": failed}


def _percentile_ranks(values: Sequence[float], *, higher_is_better: bool) -> list[float]:
    count = len(values)
    if count == 0:
        return []
    if count == 1:
        return [1.0]
    ordered = sorted((float(value), index) for index, value in enumerate(values))
    result = [0.0] * count
    cursor = 0
    while cursor < count:
        end = cursor + 1
        while end < count and ordered[end][0] == ordered[cursor][0]:
            end += 1
        average_rank = (cursor + end - 1) / 2.0
        percentile = average_rank / (count - 1)
        if not higher_is_better:
            percentile = 1.0 - percentile
        for _, original_index in ordered[cursor:end]:
            result[original_index] = round(percentile, 6)
        cursor = end
    return result


def score_runs(runs: list[dict[str, Any]]) -> None:
    passing = [run for run in runs if run["hard_gate"]["passed"]]
    for run in runs:
        run["percentiles"] = None
        run["scores"] = None
    if not passing:
        return

    for metric, direction in SCORE_OBJECTIVES.items():
        values = [
            (
                run["execution"][metric]
                if metric == "annualized_one_way_turnover"
                else run["validation_2022_2024"][metric]
            )
            for run in passing
        ]
        ranks = _percentile_ranks(values, higher_is_better=direction == "max")
        for run, rank in zip(passing, ranks):
            if run["percentiles"] is None:
                run["percentiles"] = {}
            run["percentiles"][metric] = rank

    for run in passing:
        percentiles = run["percentiles"]
        stability = sum(percentiles[key] for key in STABILITY_METRICS) / len(
            STABILITY_METRICS
        )
        balanced = sum(percentiles.values()) / len(SCORE_OBJECTIVES)
        run["scores"] = {
            "highest_return": percentiles["annualized_return"],
            "most_stable": round(stability, 6),
            "best_balanced": round(balanced, 6),
        }


def _objective_value(run: Mapping[str, Any], metric: str) -> float:
    if metric == "annualized_one_way_turnover":
        return float(run["execution"][metric])
    return float(run["validation_2022_2024"][metric])


def dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    at_least_one_better = False
    for metric, direction in SCORE_OBJECTIVES.items():
        left_value = _objective_value(left, metric)
        right_value = _objective_value(right, metric)
        if direction == "max":
            if left_value < right_value:
                return False
            at_least_one_better = at_least_one_better or left_value > right_value
        else:
            if left_value > right_value:
                return False
            at_least_one_better = at_least_one_better or left_value < right_value
    return at_least_one_better


def pareto_front(runs: Sequence[dict[str, Any]]) -> list[str]:
    passing = [run for run in runs if run["hard_gate"]["passed"]]
    front = [
        run
        for run in passing
        if not any(other is not run and dominates(other, run) for other in passing)
    ]
    front.sort(
        key=lambda run: (
            -run["scores"]["best_balanced"],
            -run["validation_2022_2024"]["annualized_return"],
            run["run_id"],
        )
    )
    return [run["run_id"] for run in front]


def _effective_replacement_ratio(spec: Mapping[str, Any]) -> float | None:
    ratio = spec.get("replacement_ratio")
    if ratio is not None:
        return float(ratio)
    replacements = spec.get("max_replacements")
    if replacements is None:
        return None
    return float(replacements) / float(spec["portfolio_topn"])


def _structural_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "portfolio_topn": int(spec["portfolio_topn"]),
        "frequency_days": int(spec["frequency_days"]),
        "replacement_ratio": _effective_replacement_ratio(spec),
        "rebalance_mode": spec["rebalance_mode"],
        "account": float(spec["account"]),
        "frequency_offset": int(spec["frequency_offset"]),
    }


def _adjacent_numeric(left: float | None, right: float | None, values: Sequence[float | None]) -> bool:
    if left is None or right is None or left == right:
        return False
    numeric = sorted({float(value) for value in values if value is not None})
    try:
        return abs(numeric.index(float(left)) - numeric.index(float(right))) == 1
    except ValueError:
        return False


def are_parameter_neighbors(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    grids: Mapping[str, Sequence[float | None]],
) -> bool:
    left_spec = _structural_spec(left["spec"])
    right_spec = _structural_spec(right["spec"])
    for key in ("rebalance_mode", "account", "frequency_offset"):
        if left_spec[key] != right_spec[key]:
            return False
    changed = [
        key
        for key in ("portfolio_topn", "frequency_days", "replacement_ratio")
        if left_spec[key] != right_spec[key]
    ]
    if len(changed) != 1:
        return False
    key = changed[0]
    return _adjacent_numeric(left_spec[key], right_spec[key], grids[key])


def _distribution(values: Iterable[float]) -> dict[str, float] | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    return {
        "min": round(clean[0], 6),
        "median": round(float(median(clean)), 6),
        "max": round(clean[-1], 6),
    }


def build_neighborhoods(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    successful = list(runs)
    grids = {
        key: [_structural_spec(run["spec"])[key] for run in successful]
        for key in ("portfolio_topn", "frequency_days", "replacement_ratio")
    }
    result: dict[str, Any] = {}
    for run in successful:
        neighbors = [
            other
            for other in successful
            if other is not run and are_parameter_neighbors(run, other, grids)
        ]
        passing_neighbors = [item for item in neighbors if item["hard_gate"]["passed"]]
        candidate_return = run["validation_2022_2024"]["annualized_return"]
        retention = None
        if candidate_return != 0 and passing_neighbors:
            retention = _distribution(
                item["validation_2022_2024"]["annualized_return"] / candidate_return
                for item in passing_neighbors
            )
        result[run["run_id"]] = {
            "neighbor_run_ids": sorted(item["run_id"] for item in neighbors),
            "passing_neighbor_run_ids": sorted(
                item["run_id"] for item in passing_neighbors
            ),
            "failed_gate_neighbor_run_ids": sorted(
                item["run_id"]
                for item in neighbors
                if not item["hard_gate"]["passed"]
            ),
            "neighbor_count": len(neighbors),
            "validation_annualized_return": _distribution(
                item["validation_2022_2024"]["annualized_return"]
                for item in passing_neighbors
            ),
            "balanced_score": _distribution(
                item["scores"]["best_balanced"] for item in passing_neighbors
            ),
            "annualized_return_retention_vs_candidate": retention,
        }
    return result


def _ranking_key(name: str, run: Mapping[str, Any]) -> tuple[Any, ...]:
    validation = run["validation_2022_2024"]
    scores = run["scores"]
    if name == "highest_return":
        return (
            -validation["annualized_return"],
            -validation["double_cost_annualized_return"],
            -scores["best_balanced"],
            run["run_id"],
        )
    if name == "most_stable":
        return (
            -scores["most_stable"],
            -validation["rolling_252d_sharpe_p10"],
            -validation["max_drawdown"],
            run["run_id"],
        )
    if name == "best_balanced":
        return (
            -scores["best_balanced"],
            -validation["annualized_return"],
            run["run_id"],
        )
    raise ValueError(f"unknown ranking: {name}")


def _rank_entry(
    run: Mapping[str, Any],
    rank: int,
    *,
    is_pareto: bool,
    neighborhood: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "rank": rank,
        "run_id": run["run_id"],
        "spec": run["spec"],
        "scores": run["scores"],
        "validation_2022_2024": run["validation_2022_2024"],
        "recent_2025_plus_report_only": run["recent_2025_plus"],
        "execution": {
            key: run["execution"][key]
            for key in (
                "annualized_one_way_turnover",
                "no_fill_rate",
                "final_holding_count",
                "settlement_mode",
            )
        },
        "pareto": is_pareto,
        "neighborhood": neighborhood,
    }


def summarize_chunks(
    paths: Sequence[Path], *, top_n: int = 20, basis: str = "long_only"
) -> dict[str, Any]:
    if top_n < 1:
        raise ValueError("top_n must be positive")
    _basis_keys(basis)
    checkpoints, duplicate_info = load_checkpoints(paths, basis=basis)
    successful = [item for item in checkpoints if item["status"] == "success"]
    failed_checkpoints = [item for item in checkpoints if item["status"] == "failed"]
    for run in successful:
        run["hard_gate"] = evaluate_hard_gates(run)
    score_runs(successful)
    front_ids = pareto_front(successful)
    front_set = set(front_ids)
    neighborhoods = build_neighborhoods(successful)
    passing = [run for run in successful if run["hard_gate"]["passed"]]

    rankings: dict[str, list[dict[str, Any]]] = {}
    rank_positions: dict[str, dict[str, int]] = {}
    for name in ("highest_return", "most_stable", "best_balanced"):
        ordered = sorted(passing, key=lambda run: _ranking_key(name, run))
        rank_positions[name] = {
            run["run_id"]: index for index, run in enumerate(ordered, start=1)
        }
        rankings[name] = [
            _rank_entry(
                run,
                index,
                is_pareto=run["run_id"] in front_set,
                neighborhood=neighborhoods[run["run_id"]],
            )
            for index, run in enumerate(ordered[:top_n], start=1)
        ]

    compact_runs = []
    for run in sorted(successful, key=lambda item: item["run_id"]):
        compact_runs.append(
            {
                "run_id": run["run_id"],
                "spec": run["spec"],
                "validation_2022_2024": run["validation_2022_2024"],
                "recent_2025_plus_report_only": run["recent_2025_plus"],
                "execution": run["execution"],
                "hard_gate": run["hard_gate"],
                "percentiles": run["percentiles"],
                "scores": run["scores"],
                "pareto": run["run_id"] in front_set,
                "ranks": {
                    name: positions.get(run["run_id"])
                    for name, positions in rank_positions.items()
                },
                "sources": run["sources"],
            }
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "research_sweep_summary_not_published",
        "selection_basis": basis,
        "selection_policy": {
            "selection_basis": basis,
            "screening_period": VALIDATION_PERIOD,
            "recent_period": RECENT_PERIOD,
            "recent_period_role": "report_only_not_used_in_gates_percentiles_pareto_or_ranking",
            "hard_gates": {
                "minimum_validation_observations": MIN_VALIDATION_OBSERVATIONS,
                "minimum_validation_annualized_return_exclusive": 0.0,
                "minimum_validation_double_cost_annualized_return_exclusive": 0.0,
                "minimum_validation_max_drawdown": MAX_VALIDATION_DRAWDOWN,
                "maximum_no_fill_rate": MAX_NO_FILL_RATE,
                "require_liquidated_final_position": True,
            },
            "percentile_method": (
                "average rank scaled to [0,1] across hard-gate passers; higher is better; "
                "turnover direction is reversed"
            ),
            "highest_return": (
                "validation annualized return, then validation double-cost return, then "
                "balanced score"
            ),
            "most_stable": (
                "equal mean percentile of Sharpe, Calmar, rolling-252d Sharpe p10, "
                "max drawdown, worst 60d, double-cost return, and inverse turnover"
            ),
            "best_balanced": "equal mean percentile of all eight declared objectives",
            "pareto_objectives": SCORE_OBJECTIVES,
        },
        "inputs": {
            "paths": [str(Path(path).resolve()) for path in paths],
            **duplicate_info,
        },
        "counts": {
            "unique_checkpoints": len(checkpoints),
            "successful": len(successful),
            "failed_checkpoints": len(failed_checkpoints),
            "hard_gate_passed": len(passing),
            "hard_gate_failed": len(successful) - len(passing),
            "pareto": len(front_ids),
        },
        "pareto_front": front_ids,
        "rankings": rankings,
        "parameter_neighborhoods": neighborhoods,
        "runs": compact_runs,
        "failed_checkpoints": sorted(failed_checkpoints, key=lambda item: item["run_id"]),
    }


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", "--input", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--basis", choices=SELECTION_BASES, default="long_only")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.out)
    summary = summarize_chunks(
        [Path(value) for value in args.inputs], top_n=args.top, basis=args.basis
    )
    atomic_write_json(output, summary)
    print(
        json.dumps(
            {
                "out": str(output.resolve()),
                "selection_basis": summary["selection_basis"],
                "counts": summary["counts"],
                "top_run_ids": {
                    name: [item["run_id"] for item in rows]
                    for name, rows in summary["rankings"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
