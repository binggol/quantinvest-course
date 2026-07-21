"""Backtest fresh weekly Advisor Pro targets through the audited Qlib layer.

Weekly signals are formed after the last trading close of each calendar week
and execute at the next trading open.  Historical valuation snapshots are
monthly, so EP, BP, and market value are carried forward strictly as-of and
updated by the price ratio between the snapshot and the weekly signal date.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import pickle
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

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
    CorporateAction,
    EventTargetWeightStrategy,
    HistoricalFeeSchedule,
    _qlib_code,
)
try:
    from scripts.rdagent_backup.live_topk_dropout import (
        capped_topk_dropout as _shared_capped_topk_dropout,
    )
except ImportError:  # Direct execution with only ``scripts`` on sys.path.
    from rdagent_backup.live_topk_dropout import (
        capped_topk_dropout as _shared_capped_topk_dropout,
    )
from scripts.replay_advisor_pro_qlib import load_adjustment_max_factors, performance_metrics


DEFAULT_CACHE = Path(r"C:\rdagent\_combo_cache_300_long.pkl")
DEFAULT_GROWTH_FINA = Path(r"C:\rdagent\_fina_rich.pkl")
DEFAULT_FINA_VIP = Path(r"C:\rdagent\_fina_vip.pkl")
DEFAULT_REPORT_RC = Path(r"C:\rdagent\_report_rc.pkl")
DEFAULT_MINED = Path(r"C:\rdagent\_mined_composite.pkl")
DEFAULT_INDUSTRY = Path(r"C:\rdagent\_industry_map.pkl")
DEFAULT_TURNOVER = Path(r"C:\rdagent\_turnover.pkl")
DEFAULT_OUT = Path("data/advisor_pro_frequency_backtest.json")
SCORING_SCHEMA_VERSION = "advisor_pro_weekly_scores_v3_portfolio_independent"
SIGNAL_CACHE_SIGNATURE_VERSION = 3
SCORING_INPUT_ARGUMENTS = (
    "cache",
    "growth_fina",
    "fina_vip",
    "report_rc",
    "mined",
    "industry",
    "turnover",
)
QLIB_SCORING_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class PortfolioSpec:
    portfolio_topn: int
    max_replacements: int | None
    rebalance_mode: str
    account: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "portfolio_topn", int(self.portfolio_topn))
        if self.portfolio_topn < 1:
            raise ValueError("portfolio_topn must be positive")
        if self.max_replacements is not None:
            object.__setattr__(self, "max_replacements", int(self.max_replacements))
            if not 0 <= self.max_replacements <= self.portfolio_topn:
                raise ValueError("max_replacements must be between 0 and portfolio_topn")
        if self.rebalance_mode not in ("target_weight", "replace_only"):
            raise ValueError("rebalance_mode must be 'target_weight' or 'replace_only'")
        if self.account is not None:
            object.__setattr__(self, "account", float(self.account))
            if not np.isfinite(self.account) or self.account <= 0:
                raise ValueError("account must be finite and positive")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "PortfolioSpec":
        return cls(
            portfolio_topn=(
                args.portfolio_topn if args.portfolio_topn is not None else args.topn
            ),
            max_replacements=args.max_replacements,
            rebalance_mode=args.rebalance_mode,
            account=args.account,
        )

    def as_dict(self, *, default_account: float) -> dict[str, Any]:
        return {
            "portfolio_topn": self.portfolio_topn,
            "max_replacements": self.max_replacements,
            "rebalance_mode": self.rebalance_mode,
            "account": float(self.account if self.account is not None else default_account),
        }


ADVISOR_PRO_CORPORATE_ACTION_SPECS = (
    {
        "from_code": "SH600837",
        "to_code": "SH601211",
        "effective_date": "2025-03-17",
        "raw_share_ratio": 0.62,
        "source_urls": [
            "https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250226_10773005.shtml",
            "https://static.cninfo.com.cn/finalpage/2025-03-08/1222744373.PDF",
            "https://static.cninfo.com.cn/finalpage/2025-03-14/1222789136.PDF",
        ],
    },
    {
        "from_code": "SH601989",
        "to_code": "SH600150",
        "effective_date": "2025-09-16",
        "raw_share_ratio": 0.1339,
        "source_urls": [
            "https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250829_10790128.shtml",
            "https://static.cninfo.com.cn/finalpage/2025-09-04/1224636691.PDF",
            "https://static.cninfo.com.cn/finalpage/2025-09-12/1224653316.PDF",
        ],
    },
)


def ts_to_qlib(code: str) -> str:
    digits, market = str(code).upper().split(".", 1)
    return ({"SH": "sh", "SZ": "sz", "BJ": "bj"}[market] + digits).lower()


def week_end_dates(
    calendar: Iterable[Any], *, start: str | pd.Timestamp, end: str | pd.Timestamp
) -> list[pd.Timestamp]:
    dates = pd.DatetimeIndex(pd.to_datetime(list(calendar))).normalize().sort_values().unique()
    dates = dates[(dates >= pd.Timestamp(start).normalize()) & (dates <= pd.Timestamp(end).normalize())]
    if dates.empty:
        return []
    frame = pd.DataFrame({"date": dates})
    frame["week"] = frame["date"].dt.to_period("W-FRI")
    return [pd.Timestamp(value).normalize() for value in frame.groupby("week")["date"].max()]


def latest_asof(values: Iterable[Any], target: Any) -> Any | None:
    ordered = sorted(values)
    index = bisect_right(ordered, target) - 1
    return ordered[index] if index >= 0 else None


def adjust_fundamentals_by_price(
    pe_ttm: Any,
    pb: Any,
    total_mv: Any,
    snapshot_price: Any,
    signal_price: Any,
) -> tuple[float, float, float]:
    values = [pd.to_numeric(value, errors="coerce") for value in (pe_ttm, pb, total_mv, snapshot_price, signal_price)]
    pe, price_to_book, market_value, old_price, new_price = [float(value) if pd.notna(value) else np.nan for value in values]
    if not np.isfinite(old_price) or not np.isfinite(new_price) or old_price <= 0 or new_price <= 0:
        return np.nan, np.nan, np.nan
    price_ratio = old_price / new_price
    ep = price_ratio / pe if np.isfinite(pe) and abs(pe) > 1e-12 else np.nan
    bp = price_ratio / price_to_book if np.isfinite(price_to_book) and abs(price_to_book) > 1e-12 else np.nan
    current_mv = market_value / price_ratio if np.isfinite(market_value) and market_value > 0 else np.nan
    return ep, bp, current_mv


def is_valid_signal_price(value: Any) -> bool:
    numeric = pd.to_numeric(value, errors="coerce")
    return bool(pd.notna(numeric) and np.isfinite(float(numeric)) and float(numeric) > 0)


def zscore(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() < 2:
        return numeric * 0.0
    clipped = numeric.clip(numeric.quantile(0.01), numeric.quantile(0.99))
    std = float(clipped.std())
    return (clipped - clipped.mean()) / std if std > 0 else clipped * 0.0


def rank_codes_by_score(scores: pd.Series) -> list[str]:
    """Return a complete, deterministic descending score ranking."""

    frame = pd.DataFrame(
        {
            "code": [_qlib_code(code) for code in scores.index],
            "score": pd.to_numeric(scores, errors="coerce").to_numpy(),
        }
    ).dropna(subset=["score"])
    frame = frame.sort_values(
        ["score", "code"], ascending=[False, True], kind="mergesort"
    ).drop_duplicates("code", keep="first")
    return frame["code"].tolist()


def capped_topk_dropout(
    previous_holdings: Iterable[str],
    ranked_codes: Iterable[str],
    *,
    topn: int,
    max_replacements: int,
) -> list[str]:
    """Normalize Qlib codes, then use the shared deterministic transition."""

    ranking = list(dict.fromkeys(_qlib_code(code) for code in ranked_codes))
    current = list(dict.fromkeys(_qlib_code(code) for code in previous_holdings))
    return _shared_capped_topk_dropout(
        current,
        ranking,
        topn=topn,
        max_replacements=max_replacements,
    )


def choose_regime(
    history: list[dict[str, Any]],
    *,
    signal_date: str,
    trend: float,
    value_spread: float,
    completed_lookback: int = 16,
) -> tuple[str, dict[str, Any]]:
    signal = pd.Timestamp(signal_date).normalize()
    completed = [
        item
        for item in history
        if item.get("completion_date")
        and pd.Timestamp(item["completion_date"]).normalize() <= signal
        and np.isfinite(float(item.get("base_m_return", np.nan)))
        and np.isfinite(float(item.get("base_v_return", np.nan)))
    ]
    votes: list[str] = []
    fm_vote = None
    if len(completed) >= completed_lookback:
        recent = completed[-completed_lookback:]
        mean_m = float(np.mean([item["base_m_return"] for item in recent]))
        mean_v = float(np.mean([item["base_v_return"] for item in recent]))
        fm_vote = "V" if mean_v > mean_m else "M"
        votes.append(fm_vote)
    trend_vote = None
    if np.isfinite(trend):
        trend_vote = "M" if trend > 0 else "V"
        votes.append(trend_vote)
    spread_vote = None
    past_spreads = [
        float(item["value_spread"])
        for item in history
        if np.isfinite(float(item.get("value_spread", np.nan)))
    ]
    if np.isfinite(value_spread) and len(past_spreads) >= 8:
        spread_vote = "V" if value_spread > float(np.median(past_spreads)) else "M"
        votes.append(spread_vote)
    regime = "V" if votes and votes.count("V") * 2 > len(votes) else "M"
    return regime, {
        "fm": fm_vote,
        "trend": trend_vote,
        "value_spread": spread_vote,
        "votes": votes,
        "completed_legs_available": len(completed),
        "completed_lookback": completed_lookback,
    }


def detailed_metrics(returns: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    base = dict(performance_metrics(values))
    if values.empty:
        return base
    nav = (1.0 + values).cumprod()
    nav_with_start = pd.concat([pd.Series([1.0]), nav.reset_index(drop=True)], ignore_index=True)
    drawdown = nav_with_start / nav_with_start.cummax() - 1.0
    annual_vol = float(values.std(ddof=1) * math.sqrt(252.0)) if len(values) > 1 else 0.0
    downside = values.where(values < 0, 0.0)
    downside_std = float(downside.std(ddof=1)) if len(values) > 1 else 0.0
    sortino = float(values.mean() / downside_std * math.sqrt(252.0)) if downside_std > 0 else 0.0
    max_drawdown = float(drawdown.min())
    annualized = float(base.get("annualized_return", 0.0))
    underwater = drawdown < 0
    max_duration = duration = 0
    for flag in underwater:
        duration = duration + 1 if flag else 0
        max_duration = max(max_duration, duration)
    dated = values.copy()
    if not isinstance(dated.index, pd.DatetimeIndex):
        dated.index = pd.date_range("2000-01-01", periods=len(dated), freq="D")
    monthly = (1.0 + dated).resample("ME").prod() - 1.0
    quarterly = (1.0 + dated).resample("QE").prod() - 1.0
    annual = (1.0 + dated).resample("YE").prod() - 1.0
    rolling_20 = (1.0 + dated).rolling(20).apply(np.prod, raw=True) - 1.0
    rolling_60 = (1.0 + dated).rolling(60).apply(np.prod, raw=True) - 1.0
    rolling_mean = dated.rolling(252).mean()
    rolling_std = dated.rolling(252).std(ddof=1)
    rolling_sharpe = rolling_mean / rolling_std.replace(0, np.nan) * math.sqrt(252.0)
    rolling_ann = (1.0 + dated).rolling(252).apply(np.prod, raw=True) - 1.0
    base.update(
        {
            "annualized_volatility": round(annual_vol, 6),
            "sortino": round(sortino, 4),
            "calmar": round(annualized / abs(max_drawdown), 4) if max_drawdown < 0 else 0.0,
            "max_drawdown_duration_days": int(max_duration),
            "worst_20d": round(float(rolling_20.min()), 6) if rolling_20.notna().any() else None,
            "worst_60d": round(float(rolling_60.min()), 6) if rolling_60.notna().any() else None,
            "monthly_win_rate": round(float((monthly > 0).mean()), 4) if len(monthly) else None,
            "quarterly_win_rate": round(float((quarterly > 0).mean()), 4) if len(quarterly) else None,
            "rolling_252d_sharpe_median": round(float(rolling_sharpe.median()), 4) if rolling_sharpe.notna().any() else None,
            "rolling_252d_sharpe_p10": round(float(rolling_sharpe.quantile(0.10)), 4) if rolling_sharpe.notna().any() else None,
            "rolling_252d_return_median": round(float(rolling_ann.median()), 6) if rolling_ann.notna().any() else None,
            "rolling_252d_return_p10": round(float(rolling_ann.quantile(0.10)), 6) if rolling_ann.notna().any() else None,
            "annual_returns": {str(index.year): round(float(value), 6) for index, value in annual.items()},
        }
    )
    return base


def compact_period_metrics(
    returns: pd.Series, extra_cost_stress: pd.Series
) -> dict[str, Any]:
    """Return stable, compact search metrics for one fixed evaluation period."""

    values = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    extra_cost = pd.to_numeric(extra_cost_stress, errors="coerce").fillna(0.0)
    extra_cost = extra_cost.reindex(values.index, fill_value=0.0)
    base = detailed_metrics(values)
    stressed = detailed_metrics(values - extra_cost)
    annualized = base.get("annualized_return")
    stressed_annualized = stressed.get("annualized_return")
    return {
        "n": int(base.get("n", 0)),
        "annualized_return": annualized,
        "sharpe": base.get("sharpe"),
        "calmar": base.get("calmar"),
        "max_drawdown": base.get("max_drawdown"),
        "rolling_252d_sharpe_p10": base.get("rolling_252d_sharpe_p10"),
        "rolling_252d_return_p10": base.get("rolling_252d_return_p10"),
        "worst_60d": base.get("worst_60d"),
        "double_cost_annualized_return": stressed_annualized,
        "annualized_cost_drag": (
            round(float(annualized) - float(stressed_annualized), 6)
            if annualized is not None and stressed_annualized is not None
            else None
        ),
    }


def evaluation_period_masks(index: pd.Index) -> dict[str, np.ndarray]:
    dates = pd.DatetimeIndex(pd.to_datetime(index)).normalize()
    return {
        "development_2017_2021": (
            (dates >= pd.Timestamp("2017-01-01")) & (dates < pd.Timestamp("2022-01-01"))
        ),
        "validation_2022_2024": (
            (dates >= pd.Timestamp("2022-01-01")) & (dates < pd.Timestamp("2025-01-01"))
        ),
        "recent_2025_plus": dates >= pd.Timestamp("2025-01-01"),
    }


def build_growth_records(
    source: Mapping[str, pd.DataFrame | None],
) -> tuple[dict[str, tuple[list[str], np.ndarray]], dict[str, Any]]:
    """Build announcement-time growth steps using the latest visible report period."""

    records: dict[str, tuple[list[str], np.ndarray]] = {}
    audit: dict[str, Any] = {
        "selection_rule": "ann_date<=signal; max end_date; latest visible revision",
        "source_codes": len(source),
        "raw_rows": 0,
        "invalid_date_rows": 0,
        "missing_value_rows": 0,
        "exact_duplicate_rows": 0,
        "conflicting_duplicate_keys": 0,
        "conflicting_duplicate_rows": 0,
        "same_announcement_multi_period_groups": 0,
        "clean_rows": 0,
        "asof_steps": 0,
        "codes_with_records": 0,
    }

    for code, frame in source.items():
        if frame is None or frame.empty:
            continue
        required = {"ann_date", "end_date", "netprofit_yoy"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"growth data for {code} is missing columns: {sorted(missing)}")

        audit["raw_rows"] += len(frame)
        clean = pd.DataFrame(
            {
                "_ann": frame["ann_date"].astype("string").str.replace(r"\.0$", "", regex=True),
                "_end": frame["end_date"].astype("string").str.replace(r"\.0$", "", regex=True),
                "_value": pd.to_numeric(frame["netprofit_yoy"], errors="coerce"),
            }
        )
        ann_parsed = pd.to_datetime(clean["_ann"], format="%Y%m%d", errors="coerce")
        end_parsed = pd.to_datetime(clean["_end"], format="%Y%m%d", errors="coerce")
        valid = ann_parsed.notna() & end_parsed.notna()
        audit["invalid_date_rows"] += int((~valid).sum())
        clean = clean.loc[valid].copy()
        clean["_ann"] = ann_parsed.loc[valid].dt.strftime("%Y%m%d")
        clean["_end"] = end_parsed.loc[valid].dt.strftime("%Y%m%d")
        audit["missing_value_rows"] += int(clean["_value"].isna().sum())

        before = len(clean)
        clean = clean.drop_duplicates(["_ann", "_end", "_value"])
        audit["exact_duplicate_rows"] += before - len(clean)

        key_sizes = clean.groupby(["_ann", "_end"], dropna=False).size()
        conflicts = key_sizes[key_sizes > 1]
        if len(conflicts):
            keys = pd.MultiIndex.from_tuples(conflicts.index.tolist(), names=["_ann", "_end"])
            conflict_mask = clean.set_index(["_ann", "_end"]).index.isin(keys)
            audit["conflicting_duplicate_keys"] += len(conflicts)
            audit["conflicting_duplicate_rows"] += int(conflict_mask.sum())
            # No revision timestamp exists for a same-announcement conflict, so
            # excluding it is the only deterministic point-in-time choice.
            clean = clean.loc[~conflict_mask]

        periods_per_announcement = clean.groupby("_ann")["_end"].nunique()
        audit["same_announcement_multi_period_groups"] += int(
            (periods_per_announcement > 1).sum()
        )
        clean = clean.sort_values(["_ann", "_end"], kind="mergesort")
        audit["clean_rows"] += len(clean)
        if clean.empty:
            continue

        latest_by_period: dict[str, float] = {}
        dates: list[str] = []
        values: list[float] = []
        for ann_date, announced in clean.groupby("_ann", sort=True):
            for end_date, value in announced[["_end", "_value"]].itertuples(
                index=False, name=None
            ):
                latest_by_period[str(end_date)] = float(value)
            latest_period = max(latest_by_period)
            dates.append(str(ann_date))
            values.append(latest_by_period[latest_period])
        records[str(code)] = (dates, np.asarray(values, dtype=float))
        audit["asof_steps"] += len(dates)

    audit["codes_with_records"] = len(records)
    return records, audit


@dataclass
class ResearchContext:
    cache: dict[str, Any]
    growth_fina: dict[str, pd.DataFrame | None]
    fina_vip: dict[str, Any]
    report_rc: dict[str, pd.DataFrame]
    mined: pd.DataFrame
    industry: dict[str, Any]
    turnover: dict[str, pd.DataFrame | None]
    open_price: pd.DataFrame
    high_price: pd.DataFrame
    low_price: pd.DataFrame
    close_price: pd.DataFrame
    benchmark_open: pd.Series
    benchmark_close: pd.Series

    @property
    def calendar(self) -> list[pd.Timestamp]:
        return [pd.Timestamp(value).normalize() for value in self.close_price.index]


def _load_pickle(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("rb") as handle:
        return pickle.load(handle)


def stat_fingerprint(path: str | Path) -> dict[str, Any]:
    """Return the stable metadata used to invalidate a cached score input."""

    resolved = Path(path).expanduser().resolve()
    metadata = resolved.stat()
    return {
        "path": str(resolved),
        "size": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
    }


def _flat_directory_fingerprint(path: Path, pattern: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    directory = stat_fingerprint(resolved)
    files = [stat_fingerprint(item) for item in sorted(resolved.glob(pattern)) if item.is_file()]
    if not files:
        raise FileNotFoundError(f"no {pattern!r} files under Qlib component: {resolved}")
    return {"directory": directory, "files": files}


def _qlib_feature_fingerprint(path: Path) -> dict[str, Any]:
    """Hash bounded-depth stat records for only the fields used by scoring."""

    resolved = path.expanduser().resolve()
    directory = stat_fingerprint(resolved)
    digest = hashlib.sha256()
    file_count = 0
    total_size = 0
    maximum_mtime_ns = 0
    for instrument_dir in sorted(item for item in resolved.iterdir() if item.is_dir()):
        for field in QLIB_SCORING_FIELDS:
            feature_path = instrument_dir / f"{field}.day.bin"
            if not feature_path.is_file():
                continue
            metadata = feature_path.stat()
            relative_path = feature_path.relative_to(resolved).as_posix()
            size = int(metadata.st_size)
            mtime_ns = int(metadata.st_mtime_ns)
            digest.update(f"{relative_path}\0{size}\0{mtime_ns}\n".encode("utf-8"))
            file_count += 1
            total_size += size
            maximum_mtime_ns = max(maximum_mtime_ns, mtime_ns)
    if file_count == 0:
        raise FileNotFoundError(f"no scoring feature files under Qlib component: {resolved}")
    return {
        "directory": directory,
        "fields": list(QLIB_SCORING_FIELDS),
        "file_count": file_count,
        "total_size": total_size,
        "max_mtime_ns": maximum_mtime_ns,
        "stat_sha256": digest.hexdigest(),
    }


def qlib_data_fingerprint(path: str | Path) -> dict[str, Any]:
    """Fingerprint Qlib scoring data without hashing feature file contents."""

    root = Path(path).expanduser().resolve()
    return {
        "root": stat_fingerprint(root),
        "calendars": _flat_directory_fingerprint(root / "calendars", "*.txt"),
        "instruments": _flat_directory_fingerprint(root / "instruments", "*.txt"),
        "features": _qlib_feature_fingerprint(root / "features"),
    }


def build_signal_cache_signature(
    args: argparse.Namespace,
    *,
    scoring_schema: str = SCORING_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Build a complete point-in-time score-cache dependency signature."""

    return {
        "signature_version": SIGNAL_CACHE_SIGNATURE_VERSION,
        "scoring_schema": str(scoring_schema),
        "signal_start": args.signal_start,
        "signal_end": args.signal_end,
        "topn": int(args.topn),
        "completed_lookback": int(args.completed_lookback),
        "ranking_schema": "complete_score_desc_code_asc_v1",
        "growth_selection": "latest_visible_end_date_v1",
        "scoring_inputs": {
            name: stat_fingerprint(getattr(args, name)) for name in SCORING_INPUT_ARGUMENTS
        },
        "qlib_data": qlib_data_fingerprint(args.qlib_data),
    }


def load_context(args: argparse.Namespace) -> ResearchContext:
    cache = _load_pickle(Path(args.cache))
    growth_fina = _load_pickle(Path(args.growth_fina))
    fina_vip = _load_pickle(Path(args.fina_vip))
    report_rc = _load_pickle(Path(args.report_rc))
    mined_payload = _load_pickle(Path(args.mined))
    industry = _load_pickle(Path(args.industry))
    turnover = _load_pickle(Path(args.turnover))
    union = sorted({ts_to_qlib(code) for code in cache["union"]})
    raw = D.features(
        union,
        ["$open", "$high", "$low", "$close"],
        start_time="2015-06-01",
        freq="day",
    )

    def panel(field: str) -> pd.DataFrame:
        frame = raw[field].unstack(level="instrument").sort_index()
        frame.index = pd.to_datetime(frame.index).normalize()
        frame.columns = [str(value).lower() for value in frame.columns]
        return frame

    benchmark = D.features(
        ["SH000300"], ["$open", "$close"], start_time="2015-06-01", freq="day"
    )
    benchmark.index = pd.to_datetime(benchmark.index.get_level_values("datetime")).normalize()
    return ResearchContext(
        cache=cache,
        growth_fina=growth_fina,
        fina_vip=fina_vip,
        report_rc=report_rc,
        mined=mined_payload["comp"],
        industry=industry,
        turnover=turnover,
        open_price=panel("$open"),
        high_price=panel("$high"),
        low_price=panel("$low"),
        close_price=panel("$close"),
        benchmark_open=pd.to_numeric(benchmark["$open"], errors="coerce"),
        benchmark_close=pd.to_numeric(benchmark["$close"], errors="coerce"),
    )


class WeeklySignalBuilder:
    def __init__(
        self,
        context: ResearchContext,
        *,
        topn: int,
        completed_lookback: int,
        portfolio_topn: int | None = None,
    ) -> None:
        self.ctx = context
        self.topn = int(topn)
        self.portfolio_topn = int(portfolio_topn if portfolio_topn is not None else topn)
        if self.topn < 1 or self.portfolio_topn < 1:
            raise ValueError("topn values must be positive")
        self.completed_lookback = int(completed_lookback)
        self.calendar = context.calendar
        self.calendar_index = {value: index for index, value in enumerate(self.calendar)}
        self.db_dates = sorted(context.cache["db"])
        self.iw = context.cache["iw"]
        self.iw_dates = sorted(str(value) for value in self.iw["trade_date"].unique())
        self.nh52 = context.close_price / context.close_price.rolling(252).max()
        self.on_momentum = (
            context.open_price / context.close_price.shift(1) - 1.0
        ).rolling(20).mean().shift(1)
        self.turnover_frame = self._turnover_frame()
        self.neg_turnover_std = -self.turnover_frame.rolling(20).std()
        self.neg_turnover_mean = -self.turnover_frame.rolling(20).mean()
        typical = (context.high_price + context.low_price + context.close_price) / 3.0
        weights = self.turnover_frame.clip(lower=0).fillna(0.0)
        weighted_cost = (typical * weights).rolling(60, min_periods=20).sum()
        weight_sum = weights.rolling(60, min_periods=20).sum().replace(0, np.nan)
        self.neg_asr = -(context.close_price / (weighted_cost / weight_sum) - 1.0)
        self.cfq_records = self._build_cfq_records()
        self.growth_records = self._build_growth_records()
        self.revision_records = self._build_revision_records()
        self.coverage_rows: list[dict[str, Any]] = []

    def _turnover_frame(self) -> pd.DataFrame:
        rows: dict[pd.Timestamp, pd.Series] = {}
        allowed = set(self.ctx.close_price.columns)
        for raw_date, frame in self.ctx.turnover.items():
            if frame is None or frame.empty:
                continue
            column = "turnover_rate_f" if "turnover_rate_f" in frame else "turnover_rate"
            series = pd.to_numeric(frame[column], errors="coerce")
            series.index = [ts_to_qlib(str(code)) for code in series.index]
            rows[pd.Timestamp(str(raw_date)).normalize()] = series.loc[series.index.intersection(allowed)]
        if not rows:
            return pd.DataFrame(index=self.ctx.close_price.index, columns=self.ctx.close_price.columns, dtype=float)
        result = pd.DataFrame(rows).T.sort_index()
        return result.reindex(index=self.ctx.close_price.index, columns=self.ctx.close_price.columns)

    def _build_cfq_records(self) -> dict[str, list[pd.Series]]:
        result: dict[str, list[pd.Series]] = {}
        for period in sorted(self.ctx.fina_vip):
            frame = self.ctx.fina_vip[period]
            if frame is None:
                continue
            for _, row in frame.iterrows():
                result.setdefault(str(row["ts_code"]), []).append(row)
        for code in result:
            result[code].sort(key=lambda row: str(row.get("ann_date", "")))
        return result

    def _build_growth_records(self) -> dict[str, tuple[list[str], np.ndarray]]:
        result, self.growth_audit = build_growth_records(self.ctx.growth_fina)
        return result

    def _build_revision_records(self) -> dict[str, dict[int, tuple[np.ndarray, np.ndarray]]]:
        result: dict[str, dict[int, tuple[np.ndarray, np.ndarray]]] = {}
        for code, frame in self.ctx.report_rc.items():
            if frame is None or frame.empty:
                continue
            clean = frame.assign(
                _rd=pd.to_numeric(frame["rd"], errors="coerce"),
                _fy=pd.to_numeric(frame["fy"], errors="coerce"),
                _eps=pd.to_numeric(frame["eps"], errors="coerce"),
            ).dropna(subset=["_rd", "_fy", "_eps"])
            yearly: dict[int, tuple[np.ndarray, np.ndarray]] = {}
            for year, group in clean.groupby("_fy"):
                ordered = group.sort_values("_rd")
                yearly[int(year)] = (
                    ordered["_rd"].to_numpy(dtype=np.int64),
                    ordered["_eps"].to_numpy(dtype=float),
                )
            if yearly:
                result[str(code)] = yearly
        return result

    def members_asof(self, signal_date: pd.Timestamp) -> set[str]:
        raw_date = signal_date.strftime("%Y%m%d")
        snapshot = latest_asof(self.iw_dates, raw_date)
        if snapshot is None:
            return set()
        rows = self.iw[self.iw["trade_date"].astype(str) == str(snapshot)]
        return set(rows["con_code"].astype(str))

    def _growth_asof(self, code: str, signal_date: pd.Timestamp) -> float:
        record = self.growth_records.get(code)
        if record is None:
            return np.nan
        cutoff = signal_date.strftime("%Y%m%d")
        dates, values = record
        index = bisect_right(dates, cutoff) - 1
        return float(values[index]) if index >= 0 else np.nan

    def _cfq_asof(self, code: str, signal_date: pd.Timestamp) -> float:
        cutoff = signal_date.strftime("%Y%m%d")
        rows = self.cfq_records.get(code, [])
        dates = [str(row.get("ann_date", "")) for row in rows]
        index = bisect_right(dates, cutoff) - 1
        if index < 0:
            return np.nan
        operating = pd.to_numeric(rows[index].get("ocfps"), errors="coerce")
        earnings = pd.to_numeric(rows[index].get("eps"), errors="coerce")
        return float(operating - earnings) if pd.notna(operating) and pd.notna(earnings) else np.nan

    def _revision_asof(self, code: str, signal_date: pd.Timestamp) -> float:
        yearly = self.revision_records.get(code)
        if not yearly or signal_date.year not in yearly:
            return np.nan
        dates, values = yearly[signal_date.year]
        cutoff = int(signal_date.strftime("%Y%m%d"))
        recent_start = int((signal_date - pd.Timedelta(days=90)).strftime("%Y%m%d"))
        prior_start = int((signal_date - pd.Timedelta(days=180)).strftime("%Y%m%d"))
        recent = values[np.searchsorted(dates, recent_start, side="right") : np.searchsorted(dates, cutoff, side="right")]
        prior = values[np.searchsorted(dates, prior_start, side="right") : np.searchsorted(dates, recent_start, side="right")]
        if len(recent) and len(prior) and abs(float(np.median(prior))) > 1e-6:
            return float((np.median(recent) - np.median(prior)) / abs(np.median(prior)))
        return np.nan

    def _base_leg_return(self, codes: list[str], entry: pd.Timestamp, completion: pd.Timestamp) -> float:
        returns: list[float] = []
        for code in codes:
            if code not in self.ctx.open_price.columns:
                continue
            start_price = self.ctx.open_price.at[entry, code]
            end_price = self.ctx.open_price.at[completion, code]
            if pd.notna(start_price) and pd.notna(end_price) and float(start_price) > 0:
                returns.append(float(end_price) / float(start_price) - 1.0)
        if not returns:
            return np.nan
        benchmark_start = self.ctx.benchmark_open.get(entry, np.nan)
        benchmark_end = self.ctx.benchmark_open.get(completion, np.nan)
        if not np.isfinite(benchmark_start) or not np.isfinite(benchmark_end) or benchmark_start <= 0:
            return np.nan
        return float(np.mean(returns)) - float(benchmark_end / benchmark_start - 1.0)

    def score(self, signal_date: pd.Timestamp) -> tuple[pd.DataFrame, float, dict[str, Any]] | None:
        signal_date = pd.Timestamp(signal_date).normalize()
        if signal_date not in self.calendar_index:
            return None
        signal_index = self.calendar_index[signal_date]
        if signal_index < 126:
            return None
        db_date = latest_asof(self.db_dates, signal_date.strftime("%Y-%m-%d"))
        if db_date is None:
            return None
        snapshot = self.ctx.cache["db"][db_date]
        members = self.members_asof(signal_date)
        dynamic_mv: dict[str, float] = {}
        industries: dict[str, str] = {}
        for code in members:
            qlib_code = ts_to_qlib(code)
            if code not in snapshot.index or qlib_code not in self.ctx.close_price.columns:
                continue
            row = snapshot.loc[code]
            old_price = self.ctx.close_price.at[pd.Timestamp(db_date), qlib_code] if pd.Timestamp(db_date) in self.ctx.close_price.index else np.nan
            new_price = self.ctx.close_price.at[signal_date, qlib_code]
            _, _, market_value = adjust_fundamentals_by_price(
                row.get("pe_ttm"), row.get("pb"), row.get("total_mv"), old_price, new_price
            )
            if np.isfinite(market_value) and market_value > 0:
                dynamic_mv[code] = market_value
                industries[code] = str(self.ctx.industry.get(qlib_code, "UNKNOWN"))
        leader: dict[str, float] = {}
        if dynamic_mv:
            market_frame = pd.DataFrame({"market_value": dynamic_mv, "industry": pd.Series(industries)})
            leader = market_frame.groupby("industry")["market_value"].rank(pct=True).to_dict()

        records: dict[str, dict[str, Any]] = {}
        ep_values: list[float] = []
        for code in members:
            qlib_code = ts_to_qlib(code)
            if code not in snapshot.index or qlib_code not in self.ctx.close_price.columns:
                continue
            row = snapshot.loc[code]
            old_price = self.ctx.close_price.at[pd.Timestamp(db_date), qlib_code] if pd.Timestamp(db_date) in self.ctx.close_price.index else np.nan
            new_price = self.ctx.close_price.at[signal_date, qlib_code]
            if not is_valid_signal_price(new_price):
                continue
            ep, bp, market_value = adjust_fundamentals_by_price(
                row.get("pe_ttm"), row.get("pb"), row.get("total_mv"), old_price, new_price
            )
            if np.isfinite(ep):
                ep_values.append(ep)
            momentum_old = self.ctx.close_price.iloc[signal_index - 120].get(qlib_code, np.nan)
            momentum_lag = self.ctx.close_price.iloc[signal_index - 20].get(qlib_code, np.nan)
            momentum = (
                float(momentum_lag) / float(momentum_old) - 1.0
                if pd.notna(momentum_lag) and pd.notna(momentum_old) and float(momentum_old) > 0
                else np.nan
            )
            signal_key = signal_date.strftime("%Y-%m-%d")
            records[qlib_code] = {
                "momentum_raw": momentum,
                "EP": ep,
                "BP": bp,
                "size": -math.log(market_value) if np.isfinite(market_value) and market_value > 0 else np.nan,
                "growth": self._growth_asof(code, signal_date),
                "CFQ": self._cfq_asof(code, signal_date),
                "REV": self._revision_asof(code, signal_date),
                "MINED": self.ctx.mined.at[signal_key, qlib_code]
                if signal_key in self.ctx.mined.index and qlib_code in self.ctx.mined.columns
                else np.nan,
                "LEAD": leader.get(code, np.nan),
                "NH52": self.nh52.at[signal_date, qlib_code],
                "TOSTD": self.neg_turnover_std.at[signal_date, qlib_code],
                "TURN": self.neg_turnover_mean.at[signal_date, qlib_code],
                "ONM": self.on_momentum.at[signal_date, qlib_code],
                "ASR": self.neg_asr.at[signal_date, qlib_code],
            }
        frame = pd.DataFrame(records).T
        if len(frame) < 50:
            return None
        frame["MOM"] = zscore(frame["momentum_raw"])
        frame["VAL"] = zscore(frame["EP"]) + zscore(frame["BP"]) + zscore(frame["size"]) + zscore(frame["growth"])
        frame["Mscore"] = (
            zscore(frame["MOM"])
            + zscore(frame["MINED"]).fillna(0)
            + zscore(frame["REV"]).fillna(0)
            + zscore(frame["NH52"]).fillna(0)
            + zscore(frame["ONM"]).fillna(0)
        )
        frame["Vscore"] = (
            zscore(frame["VAL"])
            + zscore(frame["CFQ"]).fillna(0)
            + zscore(frame["LEAD"]).fillna(0)
            + zscore(frame["TOSTD"]).fillna(0)
            + zscore(frame["TURN"]).fillna(0)
            + zscore(frame["ASR"]).fillna(0)
        )
        ep_series = pd.Series(ep_values, dtype=float)
        value_spread = (
            float(ep_series.clip(ep_series.quantile(0.05), ep_series.quantile(0.95)).std())
            if len(ep_series) > 10
            else np.nan
        )
        audit = {
            "db_date": db_date,
            "db_age_days": int((signal_date - pd.Timestamp(db_date)).days),
            "member_count": len(members),
            "score_count": len(frame),
            "mined_coverage": round(float(frame["MINED"].notna().mean()), 4),
            "turnover_coverage": round(float(frame["TOSTD"].notna().mean()), 4),
        }
        return frame, value_spread, audit

    def build(self, signal_dates: list[pd.Timestamp]) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for signal_date in signal_dates:
            scored = self.score(signal_date)
            if scored is None:
                continue
            frame, value_spread, coverage = scored
            index = self.calendar_index[signal_date]
            if index + 1 >= len(self.calendar):
                continue
            entry_date = self.calendar[index + 1]
            completion_date = self.calendar[index + 61] if index + 61 < len(self.calendar) else None
            base_m_codes = rank_codes_by_score(frame["MOM"])[: self.topn]
            base_v_codes = rank_codes_by_score(frame["VAL"])[: self.topn]
            trend = (
                float(self.ctx.benchmark_close.loc[signal_date])
                / float(self.ctx.benchmark_close.loc[self.calendar[index - 126]])
                - 1.0
            )
            regime, vote_audit = choose_regime(
                history,
                signal_date=signal_date.strftime("%Y-%m-%d"),
                trend=trend,
                value_spread=value_spread,
                completed_lookback=self.completed_lookback,
            )
            score_field = "Mscore" if regime == "M" else "Vscore"
            ranked = rank_codes_by_score(frame[score_field])
            selected = ranked[: self.portfolio_topn]
            record = {
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "completion_date": completion_date.strftime("%Y-%m-%d") if completion_date is not None else None,
                "base_m_return": self._base_leg_return(base_m_codes, entry_date, completion_date)
                if completion_date is not None
                else np.nan,
                "base_v_return": self._base_leg_return(base_v_codes, entry_date, completion_date)
                if completion_date is not None
                else np.nan,
                "value_spread": value_spread,
                "trend": trend,
                "regime": "MOM" if regime == "M" else "VAL",
                "score_field": score_field,
                "codes": selected,
                "ranked_codes": ranked,
                "vote_audit": vote_audit,
                "coverage": coverage,
            }
            history.append(record)
            self.coverage_rows.append(coverage)
        return history


def build_targets(
    records: list[dict[str, Any]],
    *,
    frequency_days: int,
    topn: int,
    final_clear_date: str,
    rank_buffer: int = 0,
    frequency_offset: int = 0,
    max_replacements: int | None = None,
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    step = max(1, round(int(frequency_days) / 5))
    offset = int(frequency_offset)
    if not 0 <= offset < step:
        raise ValueError(f"frequency_offset must be in [0, {step})")
    if max_replacements is not None and int(rank_buffer) != 0:
        raise ValueError("max_replacements and rank_buffer are mutually exclusive")
    if max_replacements is not None and not 0 <= int(max_replacements) <= int(topn):
        raise ValueError("max_replacements must be between 0 and topn")
    selected_records = [
        dict(record) for index, record in enumerate(records) if index % step == offset
    ]
    selected_records = [record for record in selected_records if record["entry_date"] < final_clear_date]
    targets: dict[str, dict[str, float]] = {}
    holdings: list[str] = []
    for record in selected_records:
        if max_replacements is not None:
            holdings = capped_topk_dropout(
                holdings,
                record["ranked_codes"],
                topn=topn,
                max_replacements=int(max_replacements),
            )
        elif rank_buffer > topn and holdings:
            buffer_set = set(record["ranked_codes"][:rank_buffer])
            kept = [code for code in holdings if code in buffer_set]
            chosen = list(kept)
            for code in record["ranked_codes"]:
                if code not in chosen:
                    chosen.append(code)
                if len(chosen) >= topn:
                    break
            holdings = chosen[:topn]
        else:
            holdings = list(record["ranked_codes"][:topn])
        weight = 1.0 / len(holdings)
        targets[record["entry_date"]] = {code: weight for code in holdings}
        record["executed_codes"] = list(holdings)
    targets[final_clear_date] = {}
    return dict(sorted(targets.items())), selected_records


def _exposure_hedged_returns(
    report: pd.DataFrame,
    benchmark: pd.DataFrame,
    hedge_yearly_cost: float,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    net_return = report["return"] - report["cost"]
    exposure = (report["value"] / report["account"].replace(0, np.nan)).fillna(0.0).clip(0.0, 1.0)
    benchmark_overnight = (benchmark["$open"] / benchmark["$close"].shift(1) - 1.0).fillna(0.0)
    benchmark_intraday = (benchmark["$close"] / benchmark["$open"] - 1.0).fillna(0.0)
    prior_exposure = exposure.shift(1).fillna(0.0)
    hedge_proxy = prior_exposure * benchmark_overnight + exposure * benchmark_intraday
    hedge_cost = hedge_yearly_cost / 252.0 * (prior_exposure + exposure) / 2.0
    return net_return, exposure, net_return - hedge_proxy - hedge_cost


def first_flat_position_date(
    positions: Mapping[Any, Any], *, on_or_after: str | pd.Timestamp
) -> pd.Timestamp | None:
    threshold = pd.Timestamp(on_or_after).normalize()
    for raw_date in sorted(positions, key=pd.Timestamp):
        trade_date = pd.Timestamp(raw_date).normalize()
        if trade_date < threshold:
            continue
        amounts = positions[raw_date].get_stock_amount_dict()
        if not any(float(amount) > 1e-8 for amount in amounts.values()):
            return trade_date
    return None


def marked_residual_holdings(position: Any, quote_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_code, raw_amount in position.get_stock_amount_dict().items():
        amount = float(raw_amount)
        if amount <= 1e-8:
            continue
        code = _qlib_code(raw_code)
        mark_price = float(position.get_stock_price(raw_code))
        last_quote_date = None
        try:
            quotes = quote_df.xs(code, level="instrument")
            valid = pd.to_numeric(quotes["$close"], errors="coerce").dropna()
            if not valid.empty:
                last_quote_date = pd.Timestamp(valid.index[-1]).strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError):
            pass
        result[code] = {
            "amount": amount,
            "mark_price": mark_price,
            "market_value": round(amount * mark_price, 2),
            "last_quote_date": last_quote_date,
        }
    return result


def load_last_adjustment_factor(
    code: str, *, max_adjustment: float, before: str | pd.Timestamp
) -> tuple[float, dict[str, Any]]:
    cutoff = pd.Timestamp(before).normalize() - pd.Timedelta(days=1)
    values = D.features(
        [_qlib_code(code)], ["$adj"], start_time="2000-01-01", end_time=cutoff, freq="day"
    )["$adj"]
    values = pd.to_numeric(values, errors="coerce").dropna().sort_index()
    maximum = float(max_adjustment)
    if values.empty or not np.isfinite(maximum) or maximum <= 0:
        raise ValueError(f"missing valid pre-action adjustment factor for: {_qlib_code(code)}")
    last_date = pd.Timestamp(values.index.get_level_values("datetime")[-1]).normalize()
    last_adjustment = float(values.iloc[-1])
    factor = last_adjustment / maximum
    if not np.isfinite(factor) or factor <= 0:
        raise ValueError(f"invalid pre-action normalized factor for: {_qlib_code(code)}")
    return factor, {
        "code": _qlib_code(code),
        "last_adjustment_date": last_date.strftime("%Y-%m-%d"),
        "last_adjustment": last_adjustment,
        "max_adjustment": maximum,
        "from_factor": factor,
    }


def run_frequency(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    frequency_days: int,
    frequency_offset: int = 0,
    spec: PortfolioSpec | None = None,
) -> dict[str, Any]:
    frequency_step = max(1, round(int(frequency_days) / 5))
    portfolio_spec = spec if spec is not None else PortfolioSpec.from_args(args)
    if not isinstance(portfolio_spec, PortfolioSpec):
        raise TypeError("spec must be a PortfolioSpec")
    portfolio_topn = portfolio_spec.portfolio_topn
    effective_account = float(
        portfolio_spec.account if portfolio_spec.account is not None else args.account
    )
    targets, selected_records = build_targets(
        records,
        frequency_days=frequency_days,
        topn=portfolio_topn,
        final_clear_date=args.backtest_end,
        rank_buffer=args.rank_buffer,
        frequency_offset=frequency_offset,
        max_replacements=portfolio_spec.max_replacements,
    )
    if len(targets) < 3:
        raise ValueError("frequency schedule has too few targets")
    calendar = [pd.Timestamp(value).normalize() for value in D.calendar(start_time=min(targets))]
    final_target = pd.Timestamp(max(targets)).normalize()
    liquidation_retry_days = int(args.liquidation_retry_days)
    final_index = calendar.index(final_target) + liquidation_retry_days - 1
    if final_index >= len(calendar):
        raise ValueError("Qlib calendar cannot cover the final liquidation retry window")
    simulation_end_time = calendar[final_index].strftime("%Y-%m-%d")
    applicable_action_specs = [
        spec
        for spec in ADVISOR_PRO_CORPORATE_ACTION_SPECS
        if pd.Timestamp(spec["effective_date"]).normalize() <= pd.Timestamp(simulation_end_time)
    ]
    codes = sorted(
        {code for weights in targets.values() for code in weights}
        | {spec["from_code"] for spec in applicable_action_specs}
        | {spec["to_code"] for spec in applicable_action_specs}
    )
    maxima, adjustment_audit = load_adjustment_max_factors(codes)
    corporate_actions: list[CorporateAction] = []
    corporate_action_sources: list[dict[str, Any]] = []
    for spec in applicable_action_specs:
        from_factor, source_audit = load_last_adjustment_factor(
            spec["from_code"],
            max_adjustment=maxima[spec["from_code"]],
            before=spec["effective_date"],
        )
        action_fields = {
            key: spec[key]
            for key in ("from_code", "to_code", "effective_date", "raw_share_ratio")
        }
        corporate_actions.append(CorporateAction(**action_fields, from_factor=from_factor))
        corporate_action_sources.append({**spec, **source_audit})
    adjustment_audit["corporate_actions"] = corporate_action_sources
    exchange = ChinaAExchange(
        freq="day",
        start_time=min(targets),
        end_time=simulation_end_time,
        codes=codes,
        deal_price="open",
        limit_threshold=0.095,
        volume_threshold=("current", f"{args.max_volume_participation} * $volume * 100"),
        open_cost=0.0,
        close_cost=0.0,
        min_cost=0.0,
        impact_cost=args.impact_cost,
        trade_unit=100,
        fee_schedule=HistoricalFeeSchedule.mainland_a_default(commission=args.commission),
        adjustment_max_factors={code: maxima[code] for code in codes},
    )
    strategy = EventTargetWeightStrategy(
        target_weights=targets,
        risk_degree=args.risk_degree,
        retry_days=args.retry_days,
        retry_days_by_target={final_target: liquidation_retry_days},
        corporate_actions=corporate_actions,
        rebalance_mode=portfolio_spec.rebalance_mode,
    )
    portfolio_metrics, _ = backtest(
        start_time=min(targets),
        end_time=simulation_end_time,
        strategy=strategy,
        executor=SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True),
        benchmark="SH000300",
        account=effective_account,
        exchange_kwargs={"exchange": exchange},
    )
    report, positions = portfolio_metrics["1day"]
    report.index = pd.to_datetime(report.index).normalize()
    liquidation_date = first_flat_position_date(positions, on_or_after=final_target)
    if liquidation_date is None:
        final_position = positions[max(positions)]
        residual_valuations = marked_residual_holdings(final_position, exchange.quote_df)
        if args.residual_policy == "strict":
            raise RuntimeError(
                "final liquidation did not complete within "
                f"{liquidation_retry_days} trading days; residual holdings: "
                f"{residual_valuations}"
            )
        settlement_mode = "unresolved_mark_to_market"
        end_time = report.index[-1].strftime("%Y-%m-%d")
    else:
        settlement_mode = "liquidated"
        report = report.loc[:liquidation_date].copy()
        end_time = liquidation_date.strftime("%Y-%m-%d")
        final_position = next(
            position
            for raw_date, position in positions.items()
            if pd.Timestamp(raw_date).normalize() == liquidation_date
        )
        residual_valuations = {}
    benchmark = D.features(
        ["SH000300"], ["$open", "$close"], start_time=min(targets), end_time=end_time, freq="day"
    )
    benchmark.index = pd.to_datetime(benchmark.index.get_level_values("datetime")).normalize()
    benchmark = benchmark.reindex(report.index)
    net_return, exposure, hedged_return = _exposure_hedged_returns(
        report, benchmark, args.hedge_yearly_cost
    )
    extra_cost_stress = pd.to_numeric(report["cost"], errors="coerce").fillna(0.0)
    reasons = Counter(item["reason"] for item in exchange.execution_audit)
    attempt_count = len(exchange.execution_audit)
    trade_count = sum(float(item["deal_amount"]) > 0 for item in exchange.execution_audit)
    no_fill_count = sum(float(item["deal_amount"]) <= 0 for item in exchange.execution_audit)
    partial_fill_count = int(reasons.get("partial", 0))
    total_cost = sum(float(item["trade_cost"]) for item in exchange.execution_audit)
    years = len(report) / 252.0
    gross_turnover = sum(float(item["trade_value"]) for item in exchange.execution_audit)
    average_account = float(pd.to_numeric(report["account"], errors="coerce").mean())
    basket_turnovers = []
    for left, right in zip(selected_records, selected_records[1:]):
        before, after = set(left.get("executed_codes", left["codes"])), set(right.get("executed_codes", right["codes"]))
        basket_turnovers.append(1.0 - len(before & after) / max(len(before), len(after), 1))
    oos = report.index >= pd.Timestamp("2022-01-01")
    period_masks = evaluation_period_masks(report.index)
    development = period_masks["development_2017_2021"]
    validation = period_masks["validation_2022_2024"]
    recent = period_masks["recent_2025_plus"]
    evaluation_periods = {
        name: {
            "long_only": compact_period_metrics(
                net_return.loc[mask], extra_cost_stress.loc[mask]
            ),
            "exposure_matched_hedged": compact_period_metrics(
                hedged_return.loc[mask], extra_cost_stress.loc[mask]
            ),
        }
        for name, mask in period_masks.items()
    }
    final_holdings = {
        _qlib_code(code): float(amount)
        for code, amount in final_position.get_stock_amount_dict().items()
        if float(amount) > 1e-8
    }
    return {
        "frequency_days": frequency_days,
        "frequency_label": {5: "weekly", 10: "biweekly", 20: "monthly", 60: "quarterly"}.get(
            frequency_days, f"{frequency_days}d"
        ),
        "frequency_offset": int(frequency_offset),
        "frequency_step": frequency_step,
        "portfolio_topn": portfolio_topn,
        "max_replacements": portfolio_spec.max_replacements,
        "rebalance_mode": portfolio_spec.rebalance_mode,
        "portfolio_spec": portfolio_spec.as_dict(default_account=float(args.account)),
        "execution_parameters": {
            "rank_buffer": int(args.rank_buffer),
            "commission": float(args.commission),
            "max_volume_participation": float(args.max_volume_participation),
            "impact_cost": float(args.impact_cost),
            "risk_degree": float(args.risk_degree),
            "retry_days": int(args.retry_days),
            "hedge_yearly_cost": float(args.hedge_yearly_cost),
        },
        "signal_count": len(selected_records),
        "target_count": len(targets),
        "start_time": min(targets),
        "end_time": end_time,
        "long_only": {
            "full": detailed_metrics(net_return),
            "2022_plus": detailed_metrics(net_return.loc[oos]),
            "2025_plus": detailed_metrics(net_return.loc[recent]),
            "development_2017_2021": detailed_metrics(net_return.loc[development]),
            "validation_2022_2024": detailed_metrics(net_return.loc[validation]),
            "recent_2025_plus": detailed_metrics(net_return.loc[recent]),
            "double_cost_full": detailed_metrics(net_return - extra_cost_stress),
        },
        "exposure_matched_hedged": {
            "full": detailed_metrics(hedged_return),
            "2022_plus": detailed_metrics(hedged_return.loc[oos]),
            "2025_plus": detailed_metrics(hedged_return.loc[recent]),
            "development_2017_2021": detailed_metrics(hedged_return.loc[development]),
            "validation_2022_2024": detailed_metrics(hedged_return.loc[validation]),
            "recent_2025_plus": detailed_metrics(hedged_return.loc[recent]),
            "double_cost_full": detailed_metrics(hedged_return - extra_cost_stress),
        },
        "evaluation_periods": evaluation_periods,
        "execution": {
            "attempts": attempt_count,
            "trades": trade_count,
            "unfilled": no_fill_count,
            "no_fill_rate": round(no_fill_count / attempt_count, 6) if attempt_count else 0.0,
            "partial_fill_rate": (
                round(partial_fill_count / attempt_count, 6) if attempt_count else 0.0
            ),
            "incomplete_fill_rate": (
                round((no_fill_count + partial_fill_count) / attempt_count, 6)
                if attempt_count
                else 0.0
            ),
            "reason_counts": dict(sorted(reasons.items())),
            "total_cost": round(total_cost, 2),
            "annualized_cost_rate": (
                round(total_cost / average_account / years, 6)
                if average_account > 0 and years > 0
                else 0.0
            ),
            "annualized_two_sided_turnover": round(gross_turnover / average_account / years, 4),
            "annualized_one_way_turnover": round(gross_turnover / average_account / years / 2.0, 4),
            "average_signal_basket_turnover": round(float(np.mean(basket_turnovers)), 4) if basket_turnovers else None,
            "average_exposure": round(float(exposure.mean()), 4),
            "final_account": round(float(report["account"].iloc[-1]), 2),
            "final_holding_count": len(final_holdings),
            "settlement_mode": settlement_mode,
            "liquidation_target_date": final_target.strftime("%Y-%m-%d"),
            "liquidation_completion_date": end_time if settlement_mode == "liquidated" else None,
            "liquidation_retry_days": liquidation_retry_days,
            "simulation_end_time": simulation_end_time,
            "valuation_date": end_time,
            "residual_holdings": residual_valuations,
            "residual_market_value": round(
                sum(item["market_value"] for item in residual_valuations.values()), 2
            ),
            "cost_basis": (
                "actual_fills_including_liquidation"
                if settlement_mode == "liquidated"
                else "actual_fills_only_no_hypothetical_exit_cost"
            ),
            "corporate_actions": [
                item
                for item in strategy.strategy_audit
                if str(item.get("status", "")).startswith("corporate_action")
                or item.get("status") == "retired_target_blocked"
            ],
        },
        "adjustment_factor_audit": adjustment_audit,
        "signals": selected_records,
        "daily_path": [
            {
                "date": trade_date.strftime("%Y-%m-%d"),
                "account": round(float(report.at[trade_date, "account"]), 2),
                "net_return": round(float(net_return.at[trade_date]), 12),
                "cost_rate": round(float(extra_cost_stress.at[trade_date]), 12),
                "exposure": round(float(exposure.at[trade_date]), 10),
                "hedged_return": round(float(hedged_return.at[trade_date]), 12),
            }
            for trade_date in report.index
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qlib-data", default=r"C:\qlib_data\cn_data")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--growth-fina", default=str(DEFAULT_GROWTH_FINA))
    parser.add_argument("--fina-vip", default=str(DEFAULT_FINA_VIP))
    parser.add_argument("--report-rc", default=str(DEFAULT_REPORT_RC))
    parser.add_argument("--mined", default=str(DEFAULT_MINED))
    parser.add_argument("--industry", default=str(DEFAULT_INDUSTRY))
    parser.add_argument("--turnover", default=str(DEFAULT_TURNOVER))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--signal-cache", default="data/advisor_pro_weekly_signal_cache.pkl"
    )
    parser.add_argument("--rebuild-signals", action="store_true")
    parser.add_argument("--signal-start", default="2017-02-03")
    parser.add_argument("--signal-end", default="2026-05-15")
    parser.add_argument("--backtest-end", default="2026-05-18")
    parser.add_argument("--frequencies", default="5")
    parser.add_argument(
        "--all-frequency-offsets",
        action="store_true",
        help="Run every weekly start offset for each requested frequency",
    )
    parser.add_argument("--topn", type=int, default=20)
    parser.add_argument(
        "--portfolio-topn",
        type=int,
        default=None,
        help="Portfolio holding count; defaults to --topn",
    )
    parser.add_argument(
        "--max-replacements",
        type=int,
        default=None,
        help="Maximum top-k dropout replacements per rebalance",
    )
    parser.add_argument(
        "--rebalance-mode",
        choices=("target_weight", "replace_only"),
        default="target_weight",
    )
    parser.add_argument("--rank-buffer", type=int, default=0)
    parser.add_argument("--completed-lookback", type=int, default=16)
    parser.add_argument("--account", type=float, default=100_000_000)
    parser.add_argument("--risk-degree", type=float, default=0.95)
    parser.add_argument("--retry-days", type=int, default=5)
    parser.add_argument("--liquidation-retry-days", type=int, default=30)
    parser.add_argument(
        "--residual-policy", choices=("strict", "mark_to_market"), default="strict"
    )
    parser.add_argument("--commission", type=float, default=0.0003)
    parser.add_argument("--max-volume-participation", type=float, default=0.10)
    parser.add_argument("--impact-cost", type=float, default=0.10)
    parser.add_argument("--hedge-yearly-cost", type=float, default=0.01)
    return parser.parse_args()


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    def safe(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        if isinstance(value, (pd.Timestamp, datetime)):
            return value.isoformat()
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(path)


def main() -> None:
    args = parse_args()
    portfolio_spec = PortfolioSpec.from_args(args)
    portfolio_topn = portfolio_spec.portfolio_topn
    if args.topn < 1:
        raise ValueError("topn values must be positive")
    qlib.init(provider_uri=str(Path(args.qlib_data)), region="cn")
    cache_path = Path(args.signal_cache)
    signature = build_signal_cache_signature(args)
    cached = None
    if cache_path.exists() and not args.rebuild_signals:
        with cache_path.open("rb") as handle:
            candidate = pickle.load(handle)
        if isinstance(candidate, dict) and candidate.get("signature") == signature:
            cached = candidate
    if cached is None:
        context = load_context(args)
        signals = week_end_dates(
            context.calendar, start=args.signal_start, end=args.signal_end
        )
        builder = WeeklySignalBuilder(
            context,
            topn=args.topn,
            portfolio_topn=args.topn,
            completed_lookback=args.completed_lookback,
        )
        records = builder.build(signals)
        cached = {
            "signature": signature,
            "signals": [value.strftime("%Y-%m-%d") for value in signals],
            "records": records,
            "coverage_rows": builder.coverage_rows,
            "growth_audit": builder.growth_audit,
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_cache = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with temporary_cache.open("wb") as handle:
            pickle.dump(cached, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary_cache.replace(cache_path)
    signals = [pd.Timestamp(value) for value in cached["signals"]]
    records = cached["records"]
    if not records:
        raise ValueError("weekly score generation produced no records")
    frequencies = sorted({int(value.strip()) for value in args.frequencies.split(",") if value.strip()})
    runs = [
        run_frequency(
            args,
            records,
            frequency,
            frequency_offset=offset,
            spec=portfolio_spec,
        )
        for frequency in frequencies
        for offset in (
            range(max(1, round(int(frequency) / 5)))
            if args.all_frequency_offsets
            else range(1)
        )
    ]
    coverage = pd.DataFrame(cached["coverage_rows"])
    output = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "research_frequency_test_not_published",
        "method": {
            "signal": "last trading close of each W-FRI calendar week",
            "execution": "next trading open via audited Qlib ChinaAExchange",
            "fundamentals": "latest month-end snapshot at or before signal, price-ratio adjusted",
            "growth": "latest report period announced by the signal date",
            "regime": "FM vote uses only 60-trading-day legs completed by the signal date",
            "corporate_actions": "official share conversions applied before trading on the effective date",
            "topn": portfolio_topn,
            "regime_topn": args.topn,
            "portfolio_topn": portfolio_topn,
            "max_replacements": portfolio_spec.max_replacements,
            "rebalance_mode": portfolio_spec.rebalance_mode,
            "rank_buffer": args.rank_buffer,
            "completed_lookback": args.completed_lookback,
        },
        "coverage": {
            "requested_week_signals": len(signals),
            "generated_week_signals": len(records),
            "daily_basic_age_days_mean": round(float(coverage["db_age_days"].mean()), 2),
            "daily_basic_age_days_median": round(float(coverage["db_age_days"].median()), 2),
            "daily_basic_age_days_max": int(coverage["db_age_days"].max()),
            "mined_coverage_mean": round(float(coverage["mined_coverage"].mean()), 4),
            "turnover_coverage_mean": round(float(coverage["turnover_coverage"].mean()), 4),
            "turnover_history_starts": "2021-01-04",
            "growth_data": cached["growth_audit"],
        },
        "runs": runs,
        "caveats": [
            "Historical daily_basic is monthly; the test carries it forward without using future snapshots and price-adjusts EP, BP, and market value.",
            "Turnover factors are unavailable before 2021 and contribute zero through cross-sectional missing-value handling.",
            "The static industry map can introduce historical classification drift in the LEAD feature.",
            "Point-in-time ST and IPO no-price-limit overrides are not complete; board rules remain fallbacks.",
            "The mined factor family was selected on earlier research data, so full-sample figures are not a clean untouched holdout.",
            "Exposure-matched hedged returns are an idealized CSI 300 proxy, not executed index-futures orders with basis, roll, margin, and contract constraints.",
        ],
    }
    write_json(Path(args.out), output)
    summary = {
        "out": str(Path(args.out).resolve()),
        "weekly_signals": len(records),
        "coverage": output["coverage"],
        "runs": [
            {
                "frequency": run["frequency_label"],
                "frequency_offset": run["frequency_offset"],
                "frequency_step": run["frequency_step"],
                "hedged_full": run["exposure_matched_hedged"]["full"],
                "long_only_full": run["long_only"]["full"],
                "execution": run["execution"],
            }
            for run in runs
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
