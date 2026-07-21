#!/usr/bin/env python3
"""Read-only structural and numerical audit for a Qlib daily OHLCV store.

Qlib daily ``.bin`` files contain one float32 calendar start index followed by
float32 values.  This script never opens source data for writing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


FIELDS = ("open", "high", "low", "close", "volume", "adj")
PRICE_FIELDS = ("open", "high", "low", "close")
MODES = ("qfq", "hfq", "raw")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Qlib cn_data root")
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Randomly audit N feature directories; 0 audits all directories",
    )
    parser.add_argument("--seed", type=int, default=300308)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--include-all-features",
        action="store_true",
        help="Also scan feature-only directories absent from instruments/all.txt",
    )
    parser.add_argument("--focus", action="append", default=["sz300308"])
    parser.add_argument("--max-issue-records", type=int, default=10000)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def read_calendar(path: Path) -> list[str]:
    dates = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return [date for date in dates if date]


def read_instruments(path: Path) -> tuple[dict[str, tuple[str, str]], list[str]]:
    instruments: dict[str, tuple[str, str]] = {}
    malformed: list[str] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.strip().split()
        if len(parts) < 3:
            malformed.append(f"line {line_no}: {raw[:120]}")
            continue
        instruments[parts[0].lower()] = (parts[1], parts[2])
    return instruments, malformed


def load_bin(path: Path) -> tuple[dict[str, Any], np.ndarray | None]:
    detail: dict[str, Any] = {
        "exists": path.is_file(),
        "bytes": None,
        "byte_remainder": None,
        "start_value": None,
        "start_idx": None,
        "length": 0,
        "nan": 0,
        "inf": 0,
        "negative": 0,
        "zero": 0,
        "nonpositive": 0,
        "min": None,
        "max": None,
    }
    if not detail["exists"]:
        return detail, None

    raw = path.read_bytes()
    detail["bytes"] = len(raw)
    detail["byte_remainder"] = len(raw) % np.dtype("<f4").itemsize
    if detail["byte_remainder"] or len(raw) < 8:
        return detail, None

    packed = np.frombuffer(raw, dtype="<f4")
    start_value = float(packed[0])
    values = packed[1:]
    detail["start_value"] = start_value
    detail["length"] = int(values.size)
    if math.isfinite(start_value) and abs(start_value - round(start_value)) <= 1e-4:
        detail["start_idx"] = int(round(start_value))

    nan_mask = np.isnan(values)
    inf_mask = np.isinf(values)
    finite = np.isfinite(values)
    detail["nan"] = int(nan_mask.sum())
    detail["inf"] = int(inf_mask.sum())
    detail["negative"] = int((finite & (values < 0)).sum())
    detail["zero"] = int((finite & (values == 0)).sum())
    detail["nonpositive"] = int((finite & (values <= 0)).sum())
    if finite.any():
        finite_values = values[finite]
        detail["min"] = float(finite_values.min())
        detail["max"] = float(finite_values.max())
    return detail, values


def _ohlc_stats(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> dict[str, int]:
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    scale = np.maximum.reduce((np.abs(o), np.abs(h), np.abs(l), np.abs(c), np.ones(o.size)))
    tolerance = 2e-6 * scale
    high_bad = finite & (h + tolerance < np.maximum.reduce((o, c, l)))
    low_bad = finite & (l - tolerance > np.minimum.reduce((o, c, h)))
    bad = high_bad | low_bad
    return {
        "rows": int(o.size),
        "finite_rows": int(finite.sum()),
        "nonfinite_rows": int((~finite).sum()),
        "high_bad_rows": int(high_bad.sum()),
        "low_bad_rows": int(low_bad.sum()),
        "ohlc_bad_rows": int(bad.sum()),
        "nonpositive_cells": int(sum((finite & (a <= 0)).sum() for a in (o, h, l, c))),
    }


def _ohlc_anomaly_events(
    values: dict[str, np.ndarray], calendar: list[str], start_idx: int, limit: int = 100
) -> list[dict[str, Any]]:
    o, h, l, c = (values[field] for field in PRICE_FIELDS)
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    scale = np.maximum.reduce((np.abs(o), np.abs(h), np.abs(l), np.abs(c), np.ones(o.size)))
    tolerance = 2e-6 * scale
    high_gap = np.maximum.reduce((o, c, l)) - h
    low_gap = l - np.minimum.reduce((o, c, h))
    bad = finite & ((high_gap > tolerance) | (low_gap > tolerance))
    events: list[dict[str, Any]] = []
    for raw_index in np.flatnonzero(bad)[:limit]:
        i = int(raw_index)
        cal_idx = start_idx + i
        types: list[str] = []
        if high_gap[i] > tolerance[i]:
            types.append("high_below_ohlc")
        if low_gap[i] > tolerance[i]:
            types.append("low_above_ohlc")
        events.append(
            {
                "date": calendar[cal_idx] if 0 <= cal_idx < len(calendar) else None,
                "types": types,
                "qfq": {field: float(values[field][i]) for field in PRICE_FIELDS},
                "high_gap": float(max(high_gap[i], 0.0)),
                "low_gap": float(max(low_gap[i], 0.0)),
                "relative_gap": float(max(high_gap[i], low_gap[i], 0.0) / scale[i]),
            }
        )
    return events


def _mode_stats(arrays: dict[str, np.ndarray], adj: np.ndarray) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    valid_adj = adj[np.isfinite(adj) & (adj > 0)]
    max_adj = float(valid_adj.max()) if valid_adj.size else float("nan")
    latest_adj = (
        float(adj[-1]) if adj.size and math.isfinite(float(adj[-1])) and float(adj[-1]) > 0 else float("nan")
    )
    transformed: dict[str, dict[str, np.ndarray]] = {}
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        # Stored bin = raw * adj / historical_max_adj.  Standard qfq is
        # normalized to the latest factor, which differs when max_adj > latest_adj.
        transformed["qfq"] = {
            field: arrays[field].astype(np.float64) * max_adj / latest_adj
            for field in PRICE_FIELDS
        }
        transformed["hfq"] = {
            field: arrays[field].astype(np.float64) * max_adj for field in PRICE_FIELDS
        }
        transformed["raw"] = {
            field: arrays[field].astype(np.float64) * max_adj / adj.astype(np.float64)
            for field in PRICE_FIELDS
        }

    stats: dict[str, Any] = {}
    for mode in MODES:
        values = transformed[mode]
        result = _ohlc_stats(values["open"], values["high"], values["low"], values["close"])
        result["finite_cells"] = int(sum(np.isfinite(values[field]).sum() for field in PRICE_FIELDS))
        result["nonfinite_cells"] = int(
            sum((~np.isfinite(values[field])).sum() for field in PRICE_FIELDS)
        )
        result["min_price"] = None
        result["max_price"] = None
        finite_chunks = [values[field][np.isfinite(values[field])] for field in PRICE_FIELDS]
        finite_chunks = [chunk for chunk in finite_chunks if chunk.size]
        if finite_chunks:
            result["min_price"] = float(min(chunk.min() for chunk in finite_chunks))
            result["max_price"] = float(max(chunk.max() for chunk in finite_chunks))
        stats[mode] = result

    relations = {
        "rows": int(adj.size),
        "qfq_times_latest_to_hfq_fail": 0,
        "raw_times_adj_to_hfq_fail": 0,
    }
    for field in PRICE_FIELDS:
        qfq = transformed["qfq"][field]
        hfq = transformed["hfq"][field]
        raw = transformed["raw"][field]
        expected_hfq = qfq * latest_adj
        finite = np.isfinite(hfq) & np.isfinite(expected_hfq)
        relations["qfq_times_latest_to_hfq_fail"] += int(
            (finite & ~np.isclose(hfq, expected_hfq, rtol=2e-12, atol=1e-12)).sum()
        )
        expected_hfq_from_raw = raw * adj
        finite = np.isfinite(hfq) & np.isfinite(expected_hfq_from_raw)
        relations["raw_times_adj_to_hfq_fail"] += int(
            (finite & ~np.isclose(hfq, expected_hfq_from_raw, rtol=2e-6, atol=1e-8)).sum()
        )
    stats["relations"] = relations
    stats["max_adj"] = max_adj if math.isfinite(max_adj) else None
    stats["latest_adj"] = latest_adj if math.isfinite(latest_adj) else None
    stats["standard_qfq_over_stored_bin"] = (
        max_adj / latest_adj if math.isfinite(max_adj) and math.isfinite(latest_adj) else None
    )
    return stats, transformed


def _focus_detail(
    code: str,
    calendar: list[str],
    start_idx: int,
    arrays: dict[str, np.ndarray],
    modes: dict[str, dict[str, np.ndarray]],
) -> dict[str, Any]:
    n = arrays["close"].size
    adj = arrays["adj"].astype(np.float64)
    close = arrays["close"].astype(np.float64)
    volume = arrays["volume"].astype(np.float64)
    flat = np.zeros(n, dtype=bool)
    if n > 1:
        prev = close[:-1]
        flat[1:] = (
            np.isfinite(prev)
            & np.isclose(arrays["open"][1:], prev, rtol=2e-6, atol=1e-7)
            & np.isclose(arrays["high"][1:], prev, rtol=2e-6, atol=1e-7)
            & np.isclose(arrays["low"][1:], prev, rtol=2e-6, atol=1e-7)
            & np.isclose(arrays["close"][1:], prev, rtol=2e-6, atol=1e-7)
            & (volume[1:] == 0)
        )

    returns = np.full(n, np.nan, dtype=np.float64)
    if n > 1:
        good = np.isfinite(close[1:]) & np.isfinite(close[:-1]) & (close[:-1] > 0)
        returns[1:][good] = close[1:][good] / close[:-1][good] - 1.0
    return_order = np.argsort(np.nan_to_num(np.abs(returns), nan=-1.0))[::-1][:15]

    adj_changes = np.flatnonzero(
        np.r_[False, np.isfinite(adj[1:]) & np.isfinite(adj[:-1]) & (np.abs(adj[1:] - adj[:-1]) > 1e-7)]
    )

    def row_at(i: int) -> dict[str, Any]:
        row: dict[str, Any] = {
            "index": int(i),
            "calendar_idx": int(start_idx + i),
            "date": calendar[start_idx + i] if 0 <= start_idx + i < len(calendar) else None,
            "volume": float(volume[i]),
            "adj": float(adj[i]),
        }
        for mode in MODES:
            row[mode] = {field: float(modes[mode][field][i]) for field in PRICE_FIELDS}
        return row

    selected = sorted(set(range(min(3, n))) | set(range(max(0, n - 10), n)))
    return {
        "code": code,
        "rows": int(n),
        "start_date": calendar[start_idx] if 0 <= start_idx < len(calendar) else None,
        "end_date": calendar[start_idx + n - 1] if n and 0 <= start_idx + n - 1 < len(calendar) else None,
        "zero_volume_rows": int((np.isfinite(volume) & (volume == 0)).sum()),
        "suspension_fill_rows": int(flat.sum()),
        "adj_change_count": int(adj_changes.size),
        "adj_change_dates": [
            calendar[start_idx + int(i)]
            for i in adj_changes
            if 0 <= start_idx + int(i) < len(calendar)
        ],
        "selected_rows": [row_at(i) for i in selected],
        "largest_abs_qfq_returns": [
            {
                "date": calendar[start_idx + int(i)]
                if 0 <= start_idx + int(i) < len(calendar)
                else None,
                "return": float(returns[i]),
                "qfq_close": float(close[i]),
            }
            for i in return_order
            if i > 0 and math.isfinite(float(returns[i]))
        ],
    }


def audit_stock(
    stock_dir: Path,
    calendar: list[str],
    calendar_index: dict[str, int],
    instrument: tuple[str, str] | None,
    focus: set[str],
) -> dict[str, Any]:
    code = stock_dir.name.lower()
    issues: list[str] = []
    field_details: dict[str, dict[str, Any]] = {}
    arrays: dict[str, np.ndarray] = {}

    for field in FIELDS:
        detail, values = load_bin(stock_dir / f"{field}.day.bin")
        field_details[field] = detail
        if not detail["exists"]:
            issues.append(f"missing:{field}")
        elif detail["byte_remainder"] or detail["bytes"] is None or detail["bytes"] < 8:
            issues.append(f"malformed:{field}")
        elif detail["start_idx"] is None:
            issues.append(f"bad_start:{field}")
        elif values is not None:
            arrays[field] = values
            if detail["nan"]:
                issues.append(f"nan:{field}")
            if detail["inf"]:
                issues.append(f"inf:{field}")
            if detail["negative"]:
                issues.append(f"negative:{field}")
            if field in PRICE_FIELDS and detail["nonpositive"]:
                issues.append(f"nonpositive:{field}")
            if field == "adj" and detail["nonpositive"]:
                issues.append("nonpositive:adj")

    lengths = {field: field_details[field]["length"] for field in FIELDS if field in arrays}
    starts = {field: field_details[field]["start_idx"] for field in FIELDS if field in arrays}
    if len(set(lengths.values())) > 1:
        issues.append("length_mismatch")
    if len(set(starts.values())) > 1:
        issues.append("start_mismatch")

    canonical_start = next(iter(starts.values()), None)
    canonical_length = min(lengths.values()) if lengths else 0
    actual_start_date = None
    actual_end_date = None
    if canonical_start is not None and canonical_length:
        end_idx = canonical_start + canonical_length - 1
        if canonical_start < 0 or canonical_start >= len(calendar):
            issues.append("calendar_start_oob")
        else:
            actual_start_date = calendar[canonical_start]
        if end_idx < 0 or end_idx >= len(calendar):
            issues.append("calendar_end_oob")
        else:
            actual_end_date = calendar[end_idx]

    expected_start_date = instrument[0] if instrument else None
    expected_end_date = instrument[1] if instrument else None
    if instrument is None:
        issues.append("missing_instrument")
    else:
        if expected_start_date not in calendar_index:
            issues.append("instrument_start_not_calendar")
        if expected_end_date not in calendar_index:
            issues.append("instrument_end_not_calendar")
        if actual_start_date != expected_start_date:
            issues.append("expected_start_mismatch")
        if actual_end_date != expected_end_date:
            issues.append("expected_end_mismatch")

    mode_stats: dict[str, Any] = {}
    focus_detail = None
    return_anomalies: list[dict[str, Any]] = []
    ohlc_anomalies: list[dict[str, Any]] = []
    adj_detail: dict[str, Any] = {}
    can_compute = all(field in arrays for field in FIELDS) and len(set(starts.values())) == 1
    if can_compute:
        n = min(arrays[field].size for field in FIELDS)
        aligned = {field: arrays[field][:n] for field in FIELDS}
        mode_stats, transformed = _mode_stats(aligned, aligned["adj"])
        for mode in MODES:
            if mode_stats[mode]["nonfinite_cells"]:
                issues.append(f"nonfinite_mode:{mode}")
            if mode_stats[mode]["ohlc_bad_rows"]:
                issues.append(f"ohlc:{mode}")
            if mode_stats[mode]["nonpositive_cells"]:
                issues.append(f"nonpositive_mode:{mode}")
        ohlc_anomalies = _ohlc_anomaly_events(
            transformed["qfq"], calendar, int(canonical_start)
        )

        adj64 = aligned["adj"].astype(np.float64)
        finite_adj = np.isfinite(adj64)
        changes = finite_adj[1:] & finite_adj[:-1] & (np.abs(adj64[1:] - adj64[:-1]) > 1e-7)
        decreases = finite_adj[1:] & finite_adj[:-1] & (adj64[1:] < adj64[:-1] - 1e-7)
        positive_adj = adj64[finite_adj & (adj64 > 0)]
        last_adj = float(adj64[-1]) if adj64.size and math.isfinite(float(adj64[-1])) else None
        max_adj = float(positive_adj.max()) if positive_adj.size else None
        adj_detail = {
            "last": last_adj,
            "max": max_adj,
            "change_rows": int(changes.sum()),
            "decrease_rows": int(decreases.sum()),
            "last_equals_max": bool(
                last_adj is not None
                and max_adj is not None
                and math.isclose(last_adj, max_adj, rel_tol=5e-7, abs_tol=1e-7)
            ),
            "max_gt_last": bool(
                last_adj is not None
                and max_adj is not None
                and max_adj > last_adj + max(1e-7, abs(max_adj) * 5e-7)
            ),
            "strict_max_gt_last": bool(
                last_adj is not None and max_adj is not None and max_adj > last_adj
            ),
            "standard_qfq_over_stored_bin": (
                max_adj / last_adj
                if last_adj is not None and last_adj > 0 and max_adj is not None
                else None
            ),
            "stored_bin_understates_standard_qfq_pct": (
                (1.0 - last_adj / max_adj) * 100.0
                if last_adj is not None and last_adj > 0 and max_adj is not None and max_adj > 0
                else None
            ),
        }

        close = aligned["close"].astype(np.float64)
        if close.size > 1:
            valid = np.isfinite(close[1:]) & np.isfinite(close[:-1]) & (close[:-1] > 0)
            returns = np.full(close.size - 1, np.nan)
            returns[valid] = close[1:][valid] / close[:-1][valid] - 1.0
            large = np.flatnonzero(np.isfinite(returns) & (np.abs(returns) > 0.5))
            for j in large:
                i = int(j + 1)
                cal_idx = int(canonical_start + i)
                return_anomalies.append(
                    {
                        "date": calendar[cal_idx] if 0 <= cal_idx < len(calendar) else None,
                        "return": float(returns[j]),
                    }
                )

        if code in focus and canonical_start is not None:
            focus_detail = _focus_detail(code, calendar, canonical_start, aligned, transformed)

    issue_tags = sorted(set(issues))
    return {
        "code": code,
        "issues": issue_tags,
        "fields": field_details,
        "starts": starts,
        "lengths": lengths,
        "canonical_start": canonical_start,
        "canonical_length": canonical_length,
        "actual_start_date": actual_start_date,
        "actual_end_date": actual_end_date,
        "expected_start_date": expected_start_date,
        "expected_end_date": expected_end_date,
        "expected_active": expected_end_date == calendar[-1] if expected_end_date else None,
        "modes": mode_stats,
        "adj": adj_detail,
        "ohlc_anomalies": ohlc_anomalies,
        "return_anomalies_over_50pct": return_anomalies,
        "focus": focus_detail,
    }


def _sum_numeric(target: dict[str, Any], source: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        target[key] = int(target.get(key, 0)) + int(source.get(key, 0) or 0)


def aggregate(
    results: list[dict[str, Any]],
    feature_codes: set[str],
    instruments: dict[str, tuple[str, str]],
    calendar: list[str],
    malformed_instruments: list[str],
    full_scan: bool,
    max_issue_records: int,
) -> dict[str, Any]:
    issue_counts: Counter[str] = Counter()
    per_field: dict[str, dict[str, Any]] = {}
    per_mode: dict[str, dict[str, Any]] = {}
    coverage_hist: Counter[str] = Counter()
    focus: dict[str, Any] = {}
    issue_records: list[dict[str, Any]] = []
    return_records: list[dict[str, Any]] = []
    mode_extremes: dict[str, dict[str, Any]] = {
        mode: {"min_price": None, "min_code": None, "max_price": None, "max_code": None}
        for mode in MODES
    }

    for field in FIELDS:
        per_field[field] = {
            "files": 0,
            "total_values": 0,
            "nan": 0,
            "inf": 0,
            "negative": 0,
            "zero": 0,
            "nonpositive": 0,
            "stocks_with_nan": 0,
            "stocks_with_inf": 0,
            "stocks_with_negative": 0,
        }
    for mode in MODES:
        per_mode[mode] = {
            "stocks_computed": 0,
            "rows": 0,
            "finite_rows": 0,
            "nonfinite_rows": 0,
            "finite_cells": 0,
            "nonfinite_cells": 0,
            "high_bad_rows": 0,
            "low_bad_rows": 0,
            "ohlc_bad_rows": 0,
            "nonpositive_cells": 0,
            "stocks_with_nonfinite": 0,
            "stocks_with_ohlc_bad": 0,
            "stocks_with_nonpositive": 0,
        }

    active_total = active_exact = 0
    expected_date_exact = 0
    last_adj_not_max = 0
    strict_max_adj_gt_latest = 0
    max_adj_gt_latest = 0
    adj_decrease_stocks = 0
    qfq_scale_deviations: list[dict[str, Any]] = []
    formula_relation_failures = Counter()
    calendar_index = {date: i for i, date in enumerate(calendar)}

    for result in results:
        issue_counts.update(result["issues"])
        if result["issues"] and len(issue_records) < max_issue_records:
            issue_records.append(
                {
                    "code": result["code"],
                    "issues": result["issues"],
                    "actual": [result["actual_start_date"], result["actual_end_date"]],
                    "expected": [result["expected_start_date"], result["expected_end_date"]],
                    "starts": result["starts"],
                    "lengths": result["lengths"],
                    "ohlc_anomalies": result["ohlc_anomalies"],
                }
            )
        if result["return_anomalies_over_50pct"]:
            return_records.append(
                {"code": result["code"], "events": result["return_anomalies_over_50pct"]}
            )
        if result["focus"] is not None:
            focus[result["code"]] = {
                "summary": {
                    "issues": result["issues"],
                    "fields": result["fields"],
                    "actual_start_date": result["actual_start_date"],
                    "actual_end_date": result["actual_end_date"],
                    "expected_start_date": result["expected_start_date"],
                    "expected_end_date": result["expected_end_date"],
                    "modes": result["modes"],
                    "adj": result["adj"],
                },
                "detail": result["focus"],
            }

        for field, detail in result["fields"].items():
            dest = per_field[field]
            dest["files"] += int(detail["exists"])
            dest["total_values"] += int(detail["length"])
            for key in ("nan", "inf", "negative", "zero", "nonpositive"):
                dest[key] += int(detail[key])
            dest["stocks_with_nan"] += int(detail["nan"] > 0)
            dest["stocks_with_inf"] += int(detail["inf"] > 0)
            dest["stocks_with_negative"] += int(detail["negative"] > 0)

        for mode in MODES:
            if mode not in result["modes"]:
                continue
            source = result["modes"][mode]
            dest = per_mode[mode]
            dest["stocks_computed"] += 1
            _sum_numeric(
                dest,
                source,
                (
                    "rows",
                    "finite_rows",
                    "nonfinite_rows",
                    "finite_cells",
                    "nonfinite_cells",
                    "high_bad_rows",
                    "low_bad_rows",
                    "ohlc_bad_rows",
                    "nonpositive_cells",
                ),
            )
            dest["stocks_with_nonfinite"] += int(source["nonfinite_cells"] > 0)
            dest["stocks_with_ohlc_bad"] += int(source["ohlc_bad_rows"] > 0)
            dest["stocks_with_nonpositive"] += int(source["nonpositive_cells"] > 0)
            for direction in ("min", "max"):
                value = source[f"{direction}_price"]
                current = mode_extremes[mode][f"{direction}_price"]
                if value is not None and (
                    current is None or (value < current if direction == "min" else value > current)
                ):
                    mode_extremes[mode][f"{direction}_price"] = value
                    mode_extremes[mode][f"{direction}_code"] = result["code"]

        relations = result["modes"].get("relations", {})
        formula_relation_failures["qfq_times_latest_to_hfq_fail"] += int(
            relations.get("qfq_times_latest_to_hfq_fail", 0)
        )
        formula_relation_failures["raw_times_adj_to_hfq_fail"] += int(
            relations.get("raw_times_adj_to_hfq_fail", 0)
        )

        if result["expected_active"]:
            active_total += 1
            if result["actual_end_date"] == calendar[-1]:
                active_exact += 1
        if result["actual_end_date"] and result["expected_end_date"]:
            expected_idx = calendar_index.get(result["expected_end_date"])
            actual_idx = calendar_index.get(result["actual_end_date"])
            if expected_idx is not None and actual_idx is not None:
                lag = expected_idx - actual_idx
                coverage_hist[str(lag)] += 1
                expected_date_exact += int(lag == 0)

        if result["adj"]:
            last_adj_not_max += int(not result["adj"].get("last_equals_max", False))
            strict_max_adj_gt_latest += int(result["adj"].get("strict_max_gt_last", False))
            max_adj_gt_latest += int(result["adj"].get("max_gt_last", False))
            adj_decrease_stocks += int(result["adj"].get("decrease_rows", 0) > 0)
            if result["adj"].get("max_gt_last", False):
                qfq_scale_deviations.append(
                    {
                        "code": result["code"],
                        "latest_adj": result["adj"].get("last"),
                        "max_adj": result["adj"].get("max"),
                        "standard_qfq_over_stored_bin": result["adj"].get(
                            "standard_qfq_over_stored_bin"
                        ),
                        "stored_bin_understates_standard_qfq_pct": result["adj"].get(
                            "stored_bin_understates_standard_qfq_pct"
                        ),
                    }
                )

    return_records.sort(
        key=lambda item: max(abs(event["return"]) for event in item["events"]), reverse=True
    )
    qfq_scale_deviations.sort(
        key=lambda item: item["standard_qfq_over_stored_bin"] or 0.0, reverse=True
    )
    instrument_codes = set(instruments)
    feature_only = sorted(feature_codes - instrument_codes)
    instrument_only = sorted(instrument_codes - feature_codes)
    instrument_results = sum(result["expected_start_date"] is not None for result in results)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "calendar": {
            "count": len(calendar),
            "first": calendar[0],
            "last": calendar[-1],
            "strictly_sorted_unique": calendar == sorted(set(calendar)),
        },
        "universe": {
            "feature_directories": len(feature_codes),
            "instrument_rows": len(instruments),
            "directories_scanned": len(results),
            "instrument_stock_directories_scanned": instrument_results,
            "feature_only_directories_scanned": len(results) - instrument_results,
            "full_scan": full_scan,
            "non_stock_or_orphan_feature_directories": len(feature_only),
            "non_stock_or_orphan_feature_examples": feature_only[:100] if full_scan else None,
            "instrument_codes_missing_features_count": len(instrument_only),
            "instrument_codes_missing_features": instrument_only if full_scan else None,
            "malformed_instrument_rows": malformed_instruments,
        },
        "issue_stock_counts": dict(sorted(issue_counts.items())),
        "stocks_with_any_issue": sum(bool(result["issues"]) for result in results),
        "field_totals": per_field,
        "mode_totals": per_mode,
        "mode_price_extremes": mode_extremes,
        "formula_relation_failures": dict(formula_relation_failures),
        "coverage": {
            "expected_end_exact": expected_date_exact,
            "expected_end_lag_histogram_trade_days": dict(
                sorted(coverage_hist.items(), key=lambda item: int(item[0]))
            ),
            "active_instruments_scanned": active_total,
            "active_ending_on_calendar_last": active_exact,
            "active_not_ending_on_calendar_last": active_total - active_exact,
        },
        "adjustment_factor": {
            "stocks_last_adj_not_historical_max": last_adj_not_max,
            "stocks_strict_max_adj_gt_latest_adj": strict_max_adj_gt_latest,
            "stocks_material_max_adj_gt_latest_adj": max_adj_gt_latest,
            "material_threshold_relative": 5e-7,
            "stocks_with_adj_decrease": adj_decrease_stocks,
            "qfq_scale_deviations": qfq_scale_deviations,
        },
        "focus": focus,
        "issue_records": issue_records,
        "large_qfq_return_diagnostics_over_50pct": return_records,
    }


def main() -> int:
    args = parse_args()
    root = args.root
    calendar = read_calendar(root / "calendars" / "day.txt")
    if not calendar:
        raise RuntimeError("calendar is empty")
    instruments, malformed_instruments = read_instruments(root / "instruments" / "all.txt")
    features = root / "features"
    all_feature_dirs = sorted(
        (Path(entry.path) for entry in os.scandir(features) if entry.is_dir()),
        key=lambda path: path.name.lower(),
    )
    feature_codes = {path.name.lower() for path in all_feature_dirs}
    # all.txt is the authoritative equity universe.  features also contains
    # bond, ETF and auxiliary-factor-only directories without OHLCV files.
    instrument_stock_dirs = [path for path in all_feature_dirs if path.name.lower() in instruments]
    stock_dirs = all_feature_dirs if args.include_all_features else instrument_stock_dirs
    focus = {code.lower() for code in args.focus}

    selected = stock_dirs
    full_scan = args.sample <= 0 or args.sample >= len(stock_dirs)
    if not full_scan:
        selected = random.Random(args.seed).sample(stock_dirs, args.sample)
        selected_by_code = {path.name.lower(): path for path in selected}
        for path in stock_dirs:
            if path.name.lower() in focus:
                selected_by_code[path.name.lower()] = path
        selected = sorted(selected_by_code.values(), key=lambda path: path.name.lower())

    print(
        f"calendar={len(calendar)} instruments={len(instruments)} "
        f"feature_dirs={len(all_feature_dirs)} stock_feature_dirs={len(instrument_stock_dirs)} "
        f"selected={len(selected)} include_all_features={args.include_all_features} "
        f"workers={args.workers}",
        file=sys.stderr,
        flush=True,
    )
    calendar_index = {date: i for i, date in enumerate(calendar)}
    results: list[dict[str, Any]] = []
    workers = max(1, min(args.workers, 32))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                audit_stock,
                path,
                calendar,
                calendar_index,
                instruments.get(path.name.lower()),
                focus,
            ): path.name.lower()
            for path in selected
        }
        for completed, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # Keep a full-universe audit running after one unreadable stock.
                results.append(
                    {
                        "code": code,
                        "issues": [f"scan_exception:{type(exc).__name__}"],
                        "fields": {},
                        "starts": {},
                        "lengths": {},
                        "canonical_start": None,
                        "canonical_length": 0,
                        "actual_start_date": None,
                        "actual_end_date": None,
                        "expected_start_date": instruments.get(code, (None, None))[0],
                        "expected_end_date": instruments.get(code, (None, None))[1],
                        "expected_active": instruments.get(code, (None, None))[1] == calendar[-1],
                        "modes": {},
                        "adj": {},
                        "ohlc_anomalies": [],
                        "return_anomalies_over_50pct": [],
                        "focus": {"exception": repr(exc)} if code in focus else None,
                    }
                )
            if completed % 500 == 0 or completed == len(futures):
                print(f"scanned {completed}/{len(futures)}", file=sys.stderr, flush=True)

    results.sort(key=lambda result: result["code"])
    report = aggregate(
        results,
        feature_codes,
        instruments,
        calendar,
        malformed_instruments,
        full_scan,
        args.max_issue_records,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"report={args.output}", file=sys.stderr, flush=True)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
