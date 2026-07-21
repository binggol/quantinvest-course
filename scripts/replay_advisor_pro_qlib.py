"""Replay Advisor Pro's fixed historical baskets through the audited Qlib layer.

This script does not select parameters or change the model.  It reconstructs
the 33 published periods as a control and, by default, recursively rebuilds
the regime path with T+1 tradability and retry-aware historical leg returns.
Every selected-path execution is then recorded through the A-share adapter.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import qlib
from qlib.backtest import backtest
from qlib.backtest.executor import SimulatorExecutor
from qlib.data import D

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_engine.qlib_adapter import (
    ChinaAExchange,
    EventTargetWeightStrategy,
    HistoricalFeeSchedule,
    _qlib_code,
    apply_adjustment_factors,
    derive_market_states,
)
from scripts.backtest_engine.historical_universe import HistoricalUniverse, UniverseCoverageError


DEFAULT_RECS = Path(r"C:\rdagent\_recs_dump.pkl")
DEFAULT_SCORES = Path(r"C:\rdagent\_mscore_dump.parquet")
DEFAULT_UNIVERSE = Path(r"C:\rdagent\_combo_cache_300_long.pkl")
DEFAULT_LEDGER = Path("data/regime_advisor_pro.json")
DEFAULT_OUT = Path("data/advisor_pro_execution_audit.json")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def load_adjustment_max_factors(codes: list[str]) -> tuple[dict[str, float], dict[str, Any]]:
    values = D.features(codes, ["$adj"], start_time="2000-01-01", freq="day")["$adj"]
    if values.empty:
        raise ValueError("Qlib adjustment-factor data is empty")
    normalized_index = pd.MultiIndex.from_arrays(
        [
            [_qlib_code(value) for value in values.index.get_level_values("instrument")],
            pd.to_datetime(values.index.get_level_values("datetime")).normalize(),
        ],
        names=["instrument", "datetime"],
    )
    values = pd.Series(pd.to_numeric(values, errors="coerce").to_numpy(), index=normalized_index)
    maxima = values.groupby(level="instrument").max()
    result = {code: float(maxima.get(code, np.nan)) for code in codes}
    invalid = sorted(code for code, value in result.items() if not np.isfinite(value) or value <= 0)
    if invalid:
        raise ValueError(f"missing valid Qlib adjustment maxima for: {invalid[:10]}")
    return result, {
        "field": "$adj",
        "formula": "factor = daily_adj / instrument_max_adj",
        "instrument_count": len(result),
        "missing_count": 0,
        "min_factor_max": round(min(result.values()), 6),
        "max_factor_max": round(max(result.values()), 6),
    }


def validate_period_membership(
    periods: list[dict],
    scores: pd.DataFrame,
    universe: HistoricalUniverse,
) -> dict[str, Any]:
    candidate_violations: list[dict[str, str]] = []
    selected_violations: list[dict[str, str]] = []
    member_omissions: list[dict[str, str]] = []
    uncovered: list[dict[str, str]] = []
    checked_candidates = 0
    checked_selected = 0
    for period in periods:
        snapshot = period["snapshot_date"]
        try:
            members = {_qlib_code(code) for code in universe.members_on(snapshot)}
        except UniverseCoverageError as exc:
            uncovered.append({"snapshot_date": snapshot, "error": str(exc)})
            continue
        subset = scores[
            (scores["de"].astype(str) == period["signal_date"])
            & (scores["dx"].astype(str) == period["legacy_exit_date"])
        ]
        candidates = sorted({_qlib_code(code) for code in subset["inst"].dropna()})
        selected = sorted({_qlib_code(code) for code in period["codes"]})
        checked_candidates += len(candidates)
        checked_selected += len(selected)
        candidate_violations.extend(
            {"snapshot_date": snapshot, "instrument": code}
            for code in candidates
            if code not in members
        )
        member_omissions.extend(
            {"snapshot_date": snapshot, "instrument": code}
            for code in sorted(members - set(candidates))
        )
        selected_violations.extend(
            {"snapshot_date": snapshot, "instrument": code}
            for code in selected
            if code not in members
        )
    passed = (
        not uncovered
        and not candidate_violations
        and not selected_violations
        and not member_omissions
    )
    return {
        "passed": passed,
        "periods": len(periods),
        "checked_candidates": checked_candidates,
        "checked_selected": checked_selected,
        "uncovered_period_count": len(uncovered),
        "uncovered_periods": uncovered[:100],
        "candidate_violation_count": len(candidate_violations),
        "candidate_violations": candidate_violations[:100],
        "member_omission_count": len(member_omissions),
        "member_omissions": member_omissions[:100],
        "selected_violation_count": len(selected_violations),
        "selected_violations": selected_violations[:100],
    }


def _enhanced_leg(record: dict, records: list[dict], index: int, lookback: int) -> str:
    past = [
        records[j]
        for j in range(index)
        if records[j].get("vote_available_date", records[j]["dx"]) <= record["de"]
    ]
    mom = [float(item["e"]["M"][0]) for item in past][-lookback:]
    val = [float(item["e"]["V"][0]) for item in past][-lookback:]
    fundamental = None
    if len(mom) >= lookback and len(val) >= lookback:
        fundamental = "V" if np.mean(val) > np.mean(mom) else "M"

    trend_value = float(record.get("trend", np.nan))
    trend = None if not np.isfinite(trend_value) else ("M" if trend_value > 0 else "V")
    spread_value = float(record.get("vspread", np.nan))
    spread_history = [
        float(records[j].get("vspread", np.nan))
        for j in range(index)
        if np.isfinite(float(records[j].get("vspread", np.nan)))
    ]
    spread = None
    if np.isfinite(spread_value) and len(spread_history) >= 8:
        spread = "V" if spread_value > float(np.median(spread_history)) else "M"
    votes = [value for value in (fundamental, trend, spread) if value]
    if not votes:
        return "V"
    return "V" if votes.count("V") * 2 > len(votes) else "M"


def select_non_overlapping_periods(records: list[dict], *, lookback: int = 4) -> list[dict]:
    """Reproduce the published regime vote and 60-trading-day non-overlap gate."""

    ordered = sorted(records, key=lambda item: item["d"])
    selected: list[dict] = []
    last_exit: str | None = None
    for index, record in enumerate(ordered):
        required = {"d", "de", "dx", "e", "trend", "vspread"}
        if required - set(record):
            raise ValueError(f"record {index} is missing {sorted(required - set(record))}")
        leg = _enhanced_leg(record, ordered, index, lookback)
        if last_exit is not None and record["de"] < last_exit:
            continue
        enhanced_key = leg + "e"
        if enhanced_key not in record["e"]:
            raise ValueError(f"record {record['d']} has no {enhanced_key} leg")
        gate_exit = str(
            (record.get("leg_completion_dates") or {}).get(enhanced_key, record["dx"])
        )
        selected.append(
            {
                "snapshot_date": str(record["d"]),
                "signal_date": str(record["de"]),
                "legacy_exit_date": str(record.get("legacy_dx", record["dx"])),
                "selection_gate_exit_date": gate_exit,
                "pick": "VAL" if leg == "V" else "MOM",
                "score_field": "Vscore" if leg == "V" else "Mscore",
            }
        )
        last_exit = gate_exit
    return selected


def attach_fixed_baskets(periods: list[dict], scores: pd.DataFrame, *, topn: int) -> list[dict]:
    required = {"inst", "de", "dx", "Mscore", "Vscore"}
    if required - set(scores.columns):
        raise ValueError(f"score dump missing columns: {sorted(required - set(scores.columns))}")
    result: list[dict] = []
    for period in periods:
        subset = scores[
            (scores["de"].astype(str) == period["signal_date"])
            & (scores["dx"].astype(str) == period["legacy_exit_date"])
        ][["inst", period["score_field"]]].copy()
        subset[period["score_field"]] = pd.to_numeric(subset[period["score_field"]], errors="coerce")
        subset = subset.dropna().sort_values(
            [period["score_field"], "inst"], ascending=[False, True], kind="mergesort"
        )
        if len(subset) < topn:
            raise ValueError(f"period {period['snapshot_date']} only has {len(subset)} usable scores")
        codes = [str(value).upper() for value in subset.head(topn)["inst"]]
        result.append({**period, "codes": codes, "basket_n": len(codes)})
    return result


def trading_attempt_dates(
    calendar: list[pd.Timestamp],
    value: str | pd.Timestamp,
    attempts: int,
) -> list[pd.Timestamp]:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    timestamp = pd.Timestamp(value).normalize()
    start = bisect_right(calendar, timestamp)
    result = calendar[start : start + attempts]
    if len(result) != attempts:
        raise ValueError(f"calendar has fewer than {attempts} sessions after {value}")
    return result


def find_executable_open(
    quote_df: pd.DataFrame,
    market_states: pd.DataFrame,
    attempts: list[pd.Timestamp],
    code: str,
    side: str,
) -> dict[str, Any]:
    if side not in {"buy", "sell"}:
        raise ValueError(f"unsupported side: {side}")
    instrument = _qlib_code(code)
    blocked_field = "limit_buy" if side == "buy" else "limit_sell"
    attempt_audit: list[dict[str, str]] = []
    for trade_date in attempts:
        key = (instrument, pd.Timestamp(trade_date).normalize())
        if key not in quote_df.index or key not in market_states.index:
            attempt_audit.append({"date": trade_date.strftime("%Y-%m-%d"), "reason": "missing_quote"})
            continue
        state = market_states.loc[key]
        price = pd.to_numeric(pd.Series([quote_df.loc[key, "$open"]]), errors="coerce").iloc[0]
        if bool(state["suspended"]):
            reason = "suspended"
        elif bool(state[blocked_field]):
            reason = blocked_field
        elif not np.isfinite(price) or price <= 0:
            reason = "invalid_open"
        else:
            attempt_audit.append({"date": trade_date.strftime("%Y-%m-%d"), "reason": "filled"})
            return {
                "filled": True,
                "date": trade_date.strftime("%Y-%m-%d"),
                "price": float(price),
                "attempts": attempt_audit,
            }
        attempt_audit.append({"date": trade_date.strftime("%Y-%m-%d"), "reason": reason})
    return {"filled": False, "date": None, "price": None, "attempts": attempt_audit}


def latest_known_close(
    quote_df: pd.DataFrame,
    code: str,
    start_date: str,
    as_of_date: str,
    fallback_price: float,
) -> tuple[str, float, bool]:
    instrument = _qlib_code(code)
    try:
        rows = quote_df.xs(instrument, level="instrument")
    except KeyError:
        return start_date, float(fallback_price), True
    values = pd.to_numeric(
        rows.loc[pd.Timestamp(start_date) : pd.Timestamp(as_of_date), "$close"], errors="coerce"
    )
    values = values[np.isfinite(values) & values.gt(0)]
    if values.empty:
        return start_date, float(fallback_price), True
    mark_date = pd.Timestamp(values.index[-1]).strftime("%Y-%m-%d")
    return mark_date, float(values.iloc[-1]), mark_date != as_of_date


def recompute_executable_leg_returns(
    records: list[dict],
    scores: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    topn: int,
    retry_days: int,
    adjustment_max_factors: dict[str, float],
) -> tuple[list[dict], dict[str, Any]]:
    """Recalculate regime legs using frozen ranks and executable T+1 opens."""

    required = {"inst", "de", "dx", "MOM", "VAL", "Mscore", "Vscore"}
    if required - set(scores.columns):
        raise ValueError(f"score dump missing columns: {sorted(required - set(scores.columns))}")
    instruments = sorted({_qlib_code(value) for value in scores["inst"].dropna()})
    first_entry = trading_attempt_dates(calendar, min(str(item["de"]) for item in records), retry_days)[0]
    final_exit_attempts = trading_attempt_dates(
        calendar, max(str(item["dx"]) for item in records), retry_days
    )
    final_date = final_exit_attempts[-1]
    fields = ["$open", "$high", "$low", "$close", "$change", "$volume", "$factor", "$adj"]
    quote_df = D.features(
        instruments,
        fields,
        start_time=first_entry,
        end_time=final_date,
        freq="day",
    )
    quote_df.columns = fields
    quote_df = apply_adjustment_factors(quote_df, adjustment_max_factors)
    market_states = derive_market_states(
        quote_df,
        buy_price_field="$open",
        sell_price_field="$open",
    )
    benchmark = D.features(
        ["SH000300"],
        ["$open", "$close"],
        start_time=first_entry,
        end_time=final_date,
        freq="day",
    )
    benchmark.index = pd.to_datetime(benchmark.index.get_level_values("datetime")).normalize()

    reason_counts: Counter[str] = Counter()
    exceptional: list[dict[str, Any]] = []
    rebuilt: list[dict] = []
    candidate_round_trips = 0
    for record in sorted(records, key=lambda item: item["d"]):
        de, dx = str(record["de"]), str(record["dx"])
        entry_attempts = trading_attempt_dates(calendar, de, retry_days)
        exit_attempts = trading_attempt_dates(calendar, dx, retry_days)
        nominal_exit = exit_attempts[0].strftime("%Y-%m-%d")
        subset = scores[(scores["de"].astype(str) == de) & (scores["dx"].astype(str) == dx)].copy()
        if subset.empty:
            raise ValueError(f"score dump has no row for {de} -> {dx}")
        legs: dict[str, tuple[float, set[str], float]] = {}
        completion_dates: dict[str, str] = {}
        for key, score_field in (("M", "MOM"), ("V", "VAL"), ("Me", "Mscore"), ("Ve", "Vscore")):
            ranked = subset[["inst", score_field]].copy()
            ranked[score_field] = pd.to_numeric(ranked[score_field], errors="coerce")
            ranked = ranked.dropna().sort_values(
                [score_field, "inst"], ascending=[False, True], kind="mergesort"
            )
            if len(ranked) < topn:
                raise ValueError(f"period {record['d']} only has {len(ranked)} {score_field} scores")
            codes = [_qlib_code(value) for value in ranked.head(topn)["inst"]]
            gross_return = 0.0
            benchmark_return = 0.0
            entered: set[str] = set()
            code_completions = [nominal_exit]
            for code in codes:
                candidate_round_trips += 1
                entry = find_executable_open(quote_df, market_states, entry_attempts, code, "buy")
                reason_counts.update(f"entry_{item['reason']}" for item in entry["attempts"])
                if not entry["filled"]:
                    reason_counts["entry_exhausted"] += 1
                    exceptional.append(
                        {
                            "snapshot_date": str(record["d"]),
                            "leg": key,
                            "instrument": code,
                            "status": "entry_exhausted_cash",
                            "entry_attempts": entry["attempts"],
                            "completion_date": nominal_exit,
                        }
                    )
                    continue
                entered.add(code)
                exit_fill = find_executable_open(
                    quote_df, market_states, exit_attempts, code, "sell"
                )
                reason_counts.update(f"exit_{item['reason']}" for item in exit_fill["attempts"])
                if exit_fill["filled"]:
                    end_date = str(exit_fill["date"])
                    end_price = float(exit_fill["price"])
                    benchmark_end_field = "$open"
                    status = "filled"
                    stale_mark = False
                    mark_date = None
                else:
                    reason_counts["exit_exhausted"] += 1
                    as_of = exit_attempts[-1].strftime("%Y-%m-%d")
                    mark_date, end_price, stale_mark = latest_known_close(
                        quote_df, code, str(entry["date"]), as_of, float(entry["price"])
                    )
                    end_date = as_of
                    benchmark_end_field = "$close"
                    status = "exit_exhausted_marked"
                entry_ts = pd.Timestamp(entry["date"])
                end_ts = pd.Timestamp(end_date)
                stock_return = end_price / float(entry["price"]) - 1.0
                benchmark_leg = (
                    float(benchmark.loc[end_ts, benchmark_end_field])
                    / float(benchmark.loc[entry_ts, "$open"])
                    - 1.0
                )
                gross_return += stock_return / topn
                benchmark_return += benchmark_leg / topn
                code_completions.append(end_date)
                if len(entry["attempts"]) > 1 or len(exit_fill["attempts"]) > 1 or not exit_fill["filled"]:
                    exceptional.append(
                        {
                            "snapshot_date": str(record["d"]),
                            "leg": key,
                            "instrument": code,
                            "status": status,
                            "entry_date": entry["date"],
                            "entry_attempts": entry["attempts"],
                            "exit_date": exit_fill["date"],
                            "exit_attempts": exit_fill["attempts"],
                            "mark_date": mark_date,
                            "mark_stale": stale_mark,
                            "completion_date": end_date,
                        }
                    )
            legs[key] = (gross_return - benchmark_return, entered, benchmark_return)
            completion_dates[key] = max(code_completions)
        rebuilt.append(
            {
                **record,
                "legacy_dx": dx,
                "e": legs,
                "entry_date": entry_attempts[0].strftime("%Y-%m-%d"),
                "corrected_exit_date": nominal_exit,
                "leg_completion_dates": completion_dates,
                "vote_available_date": max(completion_dates["M"], completion_dates["V"]),
            }
        )
    return rebuilt, {
        "retry_days": retry_days,
        "candidate_round_trips": candidate_round_trips,
        "reason_counts": dict(sorted(reason_counts.items())),
        "exceptional_count": len(exceptional),
        "exceptional": exceptional,
        "data_start": first_entry.strftime("%Y-%m-%d"),
        "data_end": final_date.strftime("%Y-%m-%d"),
    }


def validate_published_ledger(periods: list[dict], published: dict) -> dict:
    rows = ((published.get("track") or {}).get("ledger") or [])
    expected = [
        (item["snapshot_date"], item["legacy_exit_date"], item["pick"]) for item in periods
    ]
    actual = [(str(item.get("d")), str(item.get("dx")), str(item.get("pick"))) for item in rows]
    return {
        "matched": expected == actual,
        "expected_periods": len(expected),
        "published_periods": len(actual),
        "first_mismatch": next(
            (
                {"index": i, "reconstructed": left, "published": right}
                for i, (left, right) in enumerate(zip(expected, actual))
                if left != right
            ),
            None,
        ),
    }


def next_trading_day(calendar: list[pd.Timestamp], value: str) -> str:
    timestamp = pd.Timestamp(value).normalize()
    for item in calendar:
        if item > timestamp:
            return item.strftime("%Y-%m-%d")
    raise ValueError(f"calendar has no trading day after {value}")


def build_execution_schedule(periods: list[dict], calendar: list[pd.Timestamp]) -> dict[str, dict[str, float]]:
    for period in periods:
        period["entry_date"] = next_trading_day(calendar, period["signal_date"])
        period["exit_date"] = next_trading_day(calendar, period["legacy_exit_date"])

    targets: dict[str, dict[str, float]] = {period["exit_date"]: {} for period in periods}
    # An entry overrides a same-day prior exit, producing one direct rebalance.
    for period in periods:
        weight = 1.0 / period["basket_n"]
        targets[period["entry_date"]] = {code: weight for code in period["codes"]}
    return dict(sorted(targets.items()))


def performance_metrics(returns: pd.Series) -> dict[str, float | int]:
    values = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    if values.empty:
        return {"n": 0}
    nav = pd.concat([pd.Series([1.0]), (1.0 + values).cumprod().reset_index(drop=True)])
    years = len(values) / 252.0
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    return {
        "n": int(len(values)),
        "total_return": round(float(nav.iloc[-1] - 1.0), 6),
        "annualized_return": round(float(nav.iloc[-1] ** (1.0 / years) - 1.0), 6),
        "sharpe": round(float(values.mean() / std * math.sqrt(252.0)) if std > 0 else 0.0, 4),
        "max_drawdown": round(float((nav / nav.cummax() - 1.0).min()), 6),
    }


def cycle_metrics(values: list[float], *, hold_days: int = 60) -> dict[str, float | int]:
    series = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if series.empty:
        return {"n": 0}
    periods_per_year = 252.0 / hold_days
    nav = pd.concat([pd.Series([1.0]), (1.0 + series).cumprod().reset_index(drop=True)])
    std = float(series.std(ddof=1)) if len(series) > 1 else 0.0
    return {
        "n": int(len(series)),
        "total_return": round(float(nav.iloc[-1] - 1.0), 6),
        "annualized_return": round(float(nav.iloc[-1] ** (periods_per_year / len(series)) - 1.0), 6),
        "sharpe": round(float(series.mean() / std * math.sqrt(periods_per_year)) if std > 0 else 0.0, 4),
        "win_rate": round(float((series > 0).mean()), 4),
        "max_drawdown": round(float((nav / nav.cummax() - 1.0).min()), 6),
    }


def account_before(report: pd.DataFrame, trade_date: str, initial_account: float) -> float:
    prior = report.loc[report.index < pd.Timestamp(trade_date), "account"]
    return float(prior.iloc[-1]) if len(prior) else float(initial_account)


def add_cycle_results(
    periods: list[dict],
    report: pd.DataFrame,
    initial_account: float,
    benchmark_open: pd.Series,
    hedged_daily: pd.Series,
) -> None:
    for period in periods:
        before = account_before(report, period["entry_date"], initial_account)
        exit_rows = report.loc[report.index <= pd.Timestamp(period["exit_date"]), "account"]
        after = float(exit_rows.iloc[-1])
        portfolio_return = after / before - 1.0
        entry_open = float(benchmark_open.loc[pd.Timestamp(period["entry_date"])])
        exit_open = float(benchmark_open.loc[pd.Timestamp(period["exit_date"])])
        benchmark_return = exit_open / entry_open - 1.0
        cycle_hedge = hedged_daily.loc[
            pd.Timestamp(period["entry_date"]) : pd.Timestamp(period["exit_date"])
        ]
        hedged_return = float((1.0 + cycle_hedge).prod() - 1.0)
        period["portfolio_return"] = round(portfolio_return, 6)
        period["benchmark_return"] = round(benchmark_return, 6)
        period["hedged_return"] = round(hedged_return, 6)


def run_replay(args) -> dict:
    recs_path = Path(args.recs)
    scores_path = Path(args.scores)
    universe_path = Path(args.universe_cache)
    ledger_path = Path(args.ledger)
    for path in (recs_path, scores_path, universe_path, ledger_path):
        if not path.exists():
            raise FileNotFoundError(path)

    with recs_path.open("rb") as handle:
        records = pickle.load(handle)
    published_periods = select_non_overlapping_periods(records)
    scores = pd.read_parquet(scores_path)
    with universe_path.open("rb") as handle:
        universe_payload = pickle.load(handle)
    if not isinstance(universe_payload, dict) or not isinstance(
        universe_payload.get("iw"), pd.DataFrame
    ):
        raise ValueError("historical-universe cache has no index-weight frame")
    index_weight = universe_payload["iw"]
    published_periods = attach_fixed_baskets(published_periods, scores, topn=args.topn)
    published = read_json(ledger_path)
    ledger_validation = validate_published_ledger(published_periods, published)
    if not ledger_validation["matched"]:
        raise ValueError(f"reconstructed periods do not match the published ledger: {ledger_validation}")

    qlib.init(provider_uri=str(Path(args.qlib_data)), region="cn")
    calendar = [pd.Timestamp(value).normalize() for value in D.calendar(start_time="2017-01-01")]
    score_codes = sorted({_qlib_code(value) for value in scores["inst"].dropna()})
    adjustment_max_factors, adjustment_audit = load_adjustment_max_factors(score_codes)
    universe_dates = pd.to_datetime(
        index_weight["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True),
        format="%Y%m%d",
        errors="coerce",
    ).dropna()
    universe_calendar = D.calendar(
        start_time=universe_dates.min(), end_time=universe_dates.max(), freq="day"
    )
    historical_universe = HistoricalUniverse.from_index_weight(
        index_weight,
        trading_calendar=universe_calendar,
        expected_snapshot_size=300,
        incomplete_policy="barrier",
    )
    universe_audit = historical_universe.audit_report()
    all_score_periods = [
        {
            "snapshot_date": str(record["d"]),
            "signal_date": str(record["de"]),
            "legacy_exit_date": str(record["dx"]),
            "codes": [],
        }
        for record in records
    ]
    all_score_universe_validation = validate_period_membership(
        all_score_periods, scores, historical_universe
    )
    regime_recompute_audit: dict[str, Any] = {"mode": "published_control_not_recomputed"}
    if args.regime_mode == "corrected":
        corrected_records, regime_recompute_audit = recompute_executable_leg_returns(
            records,
            scores,
            calendar,
            topn=args.topn,
            retry_days=args.retry_days,
            adjustment_max_factors=adjustment_max_factors,
        )
        periods = attach_fixed_baskets(
            select_non_overlapping_periods(corrected_records), scores, topn=args.topn
        )
    else:
        periods = published_periods
    published_universe_validation = validate_period_membership(
        published_periods, scores, historical_universe
    )
    executed_universe_validation = validate_period_membership(periods, scores, historical_universe)
    if (
        not all_score_universe_validation["passed"]
        or not published_universe_validation["passed"]
        or not executed_universe_validation["passed"]
    ):
        raise ValueError(
            "point-in-time universe validation failed: "
            f"all_scores={all_score_universe_validation}, "
            f"published={published_universe_validation}, executed={executed_universe_validation}"
        )
    targets = build_execution_schedule(periods, calendar)
    start_time = min(targets)
    last_target = pd.Timestamp(max(targets)).normalize()
    last_target_index = bisect_left(calendar, last_target)
    final_index = last_target_index + args.retry_days - 1
    if (
        last_target_index >= len(calendar)
        or calendar[last_target_index] != last_target
        or final_index >= len(calendar)
    ):
        raise ValueError("calendar cannot cover the final execution retry window")
    end_time = calendar[final_index].strftime("%Y-%m-%d")
    codes = sorted({code for period in periods for code in period["codes"]})

    fee_schedule = HistoricalFeeSchedule.mainland_a_default(commission=args.commission)
    exchange = ChinaAExchange(
        freq="day",
        start_time=start_time,
        end_time=end_time,
        codes=codes,
        deal_price="open",
        limit_threshold=0.095,
        volume_threshold=("current", f"{args.max_volume_participation} * $volume * 100"),
        open_cost=0.0,
        close_cost=0.0,
        min_cost=0.0,
        impact_cost=args.impact_cost,
        trade_unit=100,
        fee_schedule=fee_schedule,
        adjustment_max_factors={code: adjustment_max_factors[code] for code in codes},
    )
    strategy = EventTargetWeightStrategy(
        target_weights=targets,
        risk_degree=args.risk_degree,
        retry_days=args.retry_days,
    )
    executor = SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True)
    portfolio_metrics, _ = backtest(
        start_time=start_time,
        end_time=end_time,
        strategy=strategy,
        executor=executor,
        benchmark="SH000300",
        account=args.account,
        exchange_kwargs={"exchange": exchange},
    )
    report, positions = portfolio_metrics["1day"]
    report.index = pd.to_datetime(report.index).normalize()
    net_return = report["return"] - report["cost"]
    exposure = (report["value"] / report["account"].replace(0, np.nan)).fillna(0.0).clip(0.0, 1.0)
    benchmark_frame = D.features(
        ["SH000300"], ["$open", "$close"], start_time=start_time, end_time=end_time, freq="day"
    )
    benchmark_frame.index = pd.to_datetime(
        benchmark_frame.index.get_level_values("datetime")
    ).normalize()
    benchmark_frame = benchmark_frame.reindex(report.index)
    benchmark_overnight = (benchmark_frame["$open"] / benchmark_frame["$close"].shift(1) - 1.0).fillna(0.0)
    benchmark_intraday = (benchmark_frame["$close"] / benchmark_frame["$open"] - 1.0).fillna(0.0)
    prior_exposure = exposure.shift(1).fillna(0.0)
    hedge_proxy_return = prior_exposure * benchmark_overnight + exposure * benchmark_intraday
    hedge_notional = (prior_exposure + exposure) / 2.0
    hedge_cost_daily = args.hedge_yearly_cost / 252.0 * hedge_notional
    hedged_daily = net_return - hedge_proxy_return - hedge_cost_daily

    add_cycle_results(periods, report, args.account, benchmark_frame["$open"], hedged_daily)

    final_position = positions[max(positions)]
    final_amounts = {
        _qlib_code(code): float(amount)
        for code, amount in final_position.get_stock_amount_dict().items()
        if float(amount) > 1e-8
    }

    oos_mask = report.index >= pd.Timestamp("2022-01-01")
    old_track = (published.get("track") or {}).get("summary") or {}
    old_long = (published.get("longonly") or {}).get("summary") or {}
    reasons = Counter(item["reason"] for item in exchange.execution_audit)
    published_by_snapshot = {item["snapshot_date"]: item["pick"] for item in published_periods}
    executed_by_snapshot = {item["snapshot_date"]: item["pick"] for item in periods}
    hedged_nav = (1.0 + hedged_daily).cumprod()
    daily_path = [
        {
            "date": trade_date.strftime("%Y-%m-%d"),
            "account": round(float(report.at[trade_date, "account"]), 2),
            "net_return": round(float(net_return.at[trade_date]), 12),
            "stock_exposure": round(float(exposure.at[trade_date]), 10),
            "benchmark_overnight": round(float(benchmark_overnight.at[trade_date]), 12),
            "benchmark_intraday": round(float(benchmark_intraday.at[trade_date]), 12),
            "hedge_proxy_return": round(float(hedge_proxy_return.at[trade_date]), 12),
            "hedge_cost": round(float(hedge_cost_daily.at[trade_date]), 12),
            "hedged_return": round(float(hedged_daily.at[trade_date]), 12),
            "hedged_nav": round(float(hedged_nav.at[trade_date]), 10),
        }
        for trade_date in report.index
    ]
    output = {
        "schema_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "research_audit_not_published",
        "publication_gate": {
            "passed": False,
            "checks_passed": [
                "published ledger reconstruction",
                "all frozen score candidates match point-in-time index membership",
                "adjusted price/share/volume units are internally consistent",
                "final retry window executed and ending position is flat",
                "chronological daily path is persisted for independent metric recomputation",
            ],
            "blocking_checks": [
                "regenerate recs and scores from a current frozen as-of snapshot",
                "populate point-in-time ST and IPO daily market-rule overrides",
                "reproduce the chronological result with a second independent engine",
            ],
        },
        "inputs": {
            "recs": file_fingerprint(recs_path),
            "scores": file_fingerprint(scores_path),
            "historical_universe": file_fingerprint(universe_path),
            "published_ledger": file_fingerprint(ledger_path),
            "qlib_data": str(Path(args.qlib_data).resolve()),
            "snapshot_kind": "frozen_research_inputs",
        },
        "validation": ledger_validation,
        "universe_validation": {
            "all_score_snapshots": all_score_universe_validation,
            "published": published_universe_validation,
            "executed": executed_universe_validation,
            "source_summary": {
                key: universe_audit[key]
                for key in (
                    "snapshot_count",
                    "incomplete_snapshot_count",
                    "coverage_gap_count",
                    "constituent_count",
                    "interval_count",
                    "multi_interval_code_count",
                    "future_interval_count",
                    "swallowed_gap_count",
                )
            },
        },
        "adjustment_factor_audit": adjustment_audit,
        "regime_recompute_audit": regime_recompute_audit,
        "regime_path": {
            "mode": args.regime_mode,
            "published_picks": [item["pick"] for item in published_periods],
            "executed_picks": [item["pick"] for item in periods],
            "changed_periods": sum(
                published_by_snapshot[snapshot] != pick
                for snapshot, pick in executed_by_snapshot.items()
                if snapshot in published_by_snapshot
            ),
            "omitted_snapshots": sorted(set(published_by_snapshot) - set(executed_by_snapshot)),
            "new_snapshots": sorted(set(executed_by_snapshot) - set(published_by_snapshot)),
        },
        "config": {
            "topn": args.topn,
            "regime_mode": args.regime_mode,
            "account": args.account,
            "risk_degree": args.risk_degree,
            "entry": "T+1 open after T-close signal",
            "exit": "planned T+61 open relative to the T-close signal; delayed entries keep the planned exit",
            "retry_days": args.retry_days,
            "commission": args.commission,
            "max_volume_participation": args.max_volume_participation,
            "impact_cost": args.impact_cost,
            "hedge_yearly_cost": args.hedge_yearly_cost,
            "trade_unit": 100,
            "volume_unit_multiplier": 100,
            "share_unit": "normalized_shares = raw_shares / (daily_adj / max_adj)",
            "backtest_end_after_final_retry": end_time,
        },
        "reporting_guidance": {
            "primary_full_metric": "execution_metrics.exposure_matched_hedged_full",
            "primary_2022_plus_metric": "execution_metrics.exposure_matched_hedged_2022_plus",
            "reason": "daily chronological NAV includes cash gaps, delayed fills, costs, and intracycle drawdowns",
            "cycle_metrics_role": "holding-period diagnostics and win rate only; not headline elapsed-time annualization",
        },
        "old_research_metrics": {"hedged": old_track, "long_only": old_long},
        "execution_metrics": {
            "long_only_full": performance_metrics(net_return),
            "long_only_2022_plus": performance_metrics(net_return.loc[oos_mask]),
            "exposure_matched_hedged_full": performance_metrics(hedged_daily),
            "exposure_matched_hedged_2022_plus": performance_metrics(hedged_daily.loc[oos_mask]),
            "cycle_long_only_full": cycle_metrics([p["portfolio_return"] for p in periods]),
            "cycle_long_only_2022_plus": cycle_metrics(
                [p["portfolio_return"] for p in periods if p["snapshot_date"] >= "2022-01-01"]
            ),
            "cycle_hedged_full": cycle_metrics([p["hedged_return"] for p in periods]),
            "cycle_hedged_2022_plus": cycle_metrics(
                [p["hedged_return"] for p in periods if p["snapshot_date"] >= "2022-01-01"]
            ),
            "final_account": round(float(report["account"].iloc[-1]), 2),
            "total_cost": round(float(sum(item["trade_cost"] for item in exchange.execution_audit)), 2),
        },
        "final_position": {
            "date": pd.Timestamp(max(positions)).strftime("%Y-%m-%d"),
            "holding_count": len(final_amounts),
            "adjusted_amounts": final_amounts,
            "stock_value": round(float(final_position.calculate_stock_value()), 2),
            "cash": round(float(final_position.get_cash()), 2),
        },
        "orders": {
            "attempts": len(exchange.execution_audit),
            "reason_counts": dict(sorted(reasons.items())),
            "unfilled": sum(item["deal_amount"] <= 0 for item in exchange.execution_audit),
            "potential_st_locked_attempts": sum(
                bool(item.get("potential_st_locked")) for item in exchange.execution_audit
            ),
        },
        "periods": periods,
        "daily_path": daily_path,
        "execution_audit": exchange.execution_audit,
        "strategy_audit": strategy.strategy_audit,
        "caveats": [
            "Historical ST flags and IPO no-limit windows are not yet available as daily point-in-time fields; board rules are fallback values.",
            "Historical regime-leg reconstruction freezes each top-N rank and applies directional tradability plus five attempts, but it does not replay capacity and transaction costs for all 8,800 hypothetical leg trades.",
            "Qlib min_cost is applied to aggregate transaction cost rather than commission alone.",
            "The primary hedge proxy splits CSI300 overnight and intraday returns by held exposure; it does not simulate individual futures contracts, basis, margin, or margin calls.",
            "Cycle metrics omit cash gaps and are retained only for comparison and win-rate diagnostics; headline return, Sharpe, and drawdown must use the chronological daily metric.",
            "The recs and score files are frozen research snapshots; generated_at is not their data date.",
            "Results remain hidden from the member page until order-level review passes.",
        ],
    }
    return output


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recs", default=str(DEFAULT_RECS))
    parser.add_argument("--scores", default=str(DEFAULT_SCORES))
    parser.add_argument("--universe-cache", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--qlib-data", default=r"C:\qlib_data\cn_data")
    parser.add_argument("--topn", type=int, default=20)
    parser.add_argument("--regime-mode", choices=("corrected", "published"), default="corrected")
    parser.add_argument("--account", type=float, default=100_000_000)
    parser.add_argument("--risk-degree", type=float, default=0.95)
    parser.add_argument("--retry-days", type=int, default=5)
    parser.add_argument("--commission", type=float, default=0.0003)
    parser.add_argument("--max-volume-participation", type=float, default=0.10)
    parser.add_argument("--impact-cost", type=float, default=0.10)
    parser.add_argument("--hedge-yearly-cost", type=float, default=0.01)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_replay(args)
    write_json(Path(args.out), result)
    metrics = result["execution_metrics"]
    print(
        json.dumps(
            {
                "out": str(Path(args.out).resolve()),
                "periods": len(result["periods"]),
                "orders": result["orders"],
                "cycle_hedged_full": metrics["cycle_hedged_full"],
                "cycle_hedged_2022_plus": metrics["cycle_hedged_2022_plus"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
