"""Compare Qlib and vn.py replay outputs after both engines have finished."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ComparisonTolerance:
    account: float = 1.0
    daily_return: float = 1e-8
    hedged_return: float = 2e-7
    exposure: float = 1e-8
    trade_raw_shares: float = 1e-4
    trade_price: float = 1e-8
    trade_cost: float = 0.01
    metric: float = 1e-5
    sharpe: float = 0.001


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _max_abs(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return math.inf
    return max((abs(float(a) - float(b)) for a, b in zip(left, right)), default=0.0)


def _daily_compare(qlib_result: dict, vnpy_result: dict, tolerance: ComparisonTolerance) -> dict:
    left = qlib_result.get("daily_path") or []
    right = vnpy_result.get("daily_path") or []
    dates_left = [item.get("date") for item in left]
    dates_right = [item.get("date") for item in right]
    fields = {
        "account": tolerance.account,
        "net_return": tolerance.daily_return,
        "stock_exposure": tolerance.exposure,
        # The primary Qlib path performs benchmark arithmetic in float32 while
        # the canonical CSV is replayed in float64.  Two float32 ULPs are an
        # explicit representation tolerance, not an economic-value tolerance.
        "hedged_return": tolerance.hedged_return,
    }
    max_errors = {
        field: _max_abs(
            [item.get(field, math.nan) for item in left],
            [item.get(field, math.nan) for item in right],
        )
        for field in fields
    }
    return {
        "date_path_equal": dates_left == dates_right,
        "qlib_days": len(left),
        "vnpy_days": len(right),
        "max_abs_error": max_errors,
        "passed": dates_left == dates_right
        and all(_finite_error(max_errors[field], limit) for field, limit in fields.items()),
    }


def _finite_error(value: float, tolerance: float) -> bool:
    return math.isfinite(value) and value <= tolerance


def _order_compare(qlib_result: dict, vnpy_result: dict, tolerance: ComparisonTolerance) -> dict:
    left = qlib_result.get("execution_audit") or []
    right = vnpy_result.get("execution_audit") or []
    identity_fields = ("trade_date", "instrument", "side", "reason")
    identity_mismatches: list[dict[str, Any]] = []
    numeric_errors = {
        "requested_raw_shares": 0.0,
        "deal_raw_shares": 0.0,
        "trade_price": 0.0,
        "trade_cost": 0.0,
    }
    for index, (qlib_order, vnpy_order) in enumerate(zip(left, right)):
        different = {
            field: (qlib_order.get(field), vnpy_order.get(field))
            for field in identity_fields
            if qlib_order.get(field) != vnpy_order.get(field)
        }
        if different and len(identity_mismatches) < 20:
            identity_mismatches.append({"index": index, "fields": different})
        for field in numeric_errors:
            qlib_value = qlib_order.get(field)
            vnpy_value = vnpy_order.get(field)
            if qlib_value is None and vnpy_value is None:
                error = 0.0
            elif qlib_value is None or vnpy_value is None:
                error = math.inf
            else:
                error = abs(float(qlib_value) - float(vnpy_value))
            numeric_errors[field] = max(numeric_errors[field], error)
    limits = {
        "requested_raw_shares": tolerance.trade_raw_shares,
        "deal_raw_shares": tolerance.trade_raw_shares,
        "trade_price": tolerance.trade_price,
        "trade_cost": tolerance.trade_cost,
    }
    return {
        "count_equal": len(left) == len(right),
        "qlib_attempts": len(left),
        "vnpy_attempts": len(right),
        "identity_mismatch_count": len(identity_mismatches),
        "identity_mismatches": identity_mismatches,
        "max_abs_error": numeric_errors,
        "passed": len(left) == len(right)
        and not identity_mismatches
        and all(_finite_error(numeric_errors[field], limit) for field, limit in limits.items()),
    }


def _metric_compare(qlib_result: dict, vnpy_result: dict, tolerance: ComparisonTolerance) -> dict:
    sections = (
        "long_only_full",
        "long_only_2022_plus",
        "exposure_matched_hedged_full",
        "exposure_matched_hedged_2022_plus",
    )
    errors: dict[str, dict[str, float]] = {}
    passed = True
    for section in sections:
        qlib_metrics = (qlib_result.get("execution_metrics") or {}).get(section) or {}
        vnpy_metrics = (vnpy_result.get("execution_metrics") or {}).get(section) or {}
        section_errors: dict[str, float] = {}
        for field in ("total_return", "annualized_return", "max_drawdown", "sharpe"):
            if field not in qlib_metrics or field not in vnpy_metrics:
                error = math.inf
            else:
                error = abs(float(qlib_metrics[field]) - float(vnpy_metrics[field]))
            section_errors[field] = error
            limit = tolerance.sharpe if field == "sharpe" else tolerance.metric
            passed = passed and _finite_error(error, limit)
        errors[section] = section_errors
    return {"max_abs_error": errors, "passed": passed}


def compare_results(
    qlib_result: dict,
    vnpy_result: dict,
    *,
    qlib_result_sha256: str | None = None,
    expected_source_sha256: str | None = None,
    tolerance: ComparisonTolerance | None = None,
) -> dict:
    """Return reproduction and publication gates without mutating either result."""

    tolerance = tolerance or ComparisonTolerance()
    source_hash_equal = (
        True
        if expected_source_sha256 is None or qlib_result_sha256 is None
        else expected_source_sha256 == qlib_result_sha256
    )
    daily = _daily_compare(qlib_result, vnpy_result, tolerance)
    orders = _order_compare(qlib_result, vnpy_result, tolerance)
    metrics = _metric_compare(qlib_result, vnpy_result, tolerance)
    reproduction_passed = source_hash_equal and daily["passed"] and orders["passed"] and metrics["passed"]
    attempted = vnpy_result.get("execution_audit") or []
    fallback_attempts = sum(item.get("market_rule_source") == "board_fallback" for item in attempted)
    final_flat = int((vnpy_result.get("final_position") or {}).get("holding_count", -1)) == 0
    return {
        "schema_version": 1,
        "input_identity": {
            "expected_qlib_sha256": expected_source_sha256,
            "actual_qlib_sha256": qlib_result_sha256,
            "matched": source_hash_equal,
        },
        "daily": daily,
        "orders": orders,
        "metrics": metrics,
        "execution_reproduction_passed": reproduction_passed,
        "publication_gate": {
            "passed": reproduction_passed and fallback_attempts == 0 and final_flat,
            "blocking_checks": [
                item
                for item, blocked in (
                    ("independent engine reproduction", not reproduction_passed),
                    ("point-in-time ST/IPO market-rule coverage", fallback_attempts > 0),
                    ("ending position is flat", not final_flat),
                )
                if blocked
            ],
            "board_fallback_attempts": fallback_attempts,
            "final_position_flat": final_flat,
        },
        "tolerance": tolerance.__dict__,
    }


__all__ = ["ComparisonTolerance", "compare_results", "sha256_file"]
