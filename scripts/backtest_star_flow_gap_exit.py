"""Backtest a tech/STAR ETF inflow day followed by next-day gap-up fade."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "etf_flow_cache"
OUT = DATA / "star_flow_gap_exit_backtest.json"

STAR_TECH_ETFS = [
    {"ts_code": "588000.SH", "name": "STAR50 ETF", "daily_prefix": "fund_daily", "share_prefix": "share"},
    {"ts_code": "512480.SH", "name": "Semiconductor ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "159995.SZ", "name": "Chip ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "512760.SH", "name": "Semiconductor 50 ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "515260.SH", "name": "Electronics ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "159997.SZ", "name": "Electronics ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "512720.SH", "name": "Computer ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "159998.SZ", "name": "Computer ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "515880.SH", "name": "Communication ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "515050.SH", "name": "5G ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "159819.SZ", "name": "AI ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "512930.SH", "name": "AI ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
    {"ts_code": "159770.SZ", "name": "Robotics ETF", "daily_prefix": "sector_daily", "share_prefix": "sector_share"},
]


def _parse_trade_date(values: pd.Series) -> pd.Series:
    raw = values.astype(str)
    parsed = pd.to_datetime(raw, format="%Y%m%d", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(raw.loc[missing], errors="coerce")
    return parsed


def _numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def add_late_inflow_features(panel: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    df = panel.copy().sort_values("trade_date").reset_index(drop=True)
    if "trade_date" not in df.columns or "share" not in df.columns or "close" not in df.columns:
        raise ValueError("panel must contain trade_date, share, and close")

    df["trade_date"] = _parse_trade_date(df["trade_date"])
    price_cols = ["share", "pre_close", "open", "high", "low", "close"]
    _numeric(df, price_cols)
    df = df.dropna(subset=["trade_date", "share", "open", "high", "low", "close"]).reset_index(drop=True)

    df["share_chg_5d"] = df["share"].pct_change(4)

    def percentile(values: np.ndarray) -> float:
        current = values[-1]
        if not np.isfinite(current):
            return np.nan
        hist = values[np.isfinite(values)]
        if len(hist) < 2:
            return np.nan
        return float((hist <= current).mean())

    df["flow_pctile"] = (
        df["share_chg_5d"]
        .rolling(max(int(lookback), 2), min_periods=2)
        .apply(percentile, raw=True)
    )

    if "pre_close" in df.columns:
        base_close = df["pre_close"]
    else:
        base_close = df["close"].shift(1)
    df["day_ret_pct"] = (df["close"] / base_close - 1.0) * 100.0

    intraday_range = df["high"] - df["low"]
    df["close_pos"] = np.where(intraday_range > 0, (df["close"] - df["low"]) / intraday_range, 0.5)

    df["next_trade_date"] = df["trade_date"].shift(-1)
    df["next_open"] = df["open"].shift(-1)
    df["next_high"] = df["high"].shift(-1)
    df["next_low"] = df["low"].shift(-1)
    df["next_close"] = df["close"].shift(-1)
    df["next_gap_pct"] = (df["next_open"] / df["close"] - 1.0) * 100.0
    df["next_open_to_close_pct"] = (df["next_close"] / df["next_open"] - 1.0) * 100.0
    df["next_high_from_open_pct"] = (df["next_high"] / df["next_open"] - 1.0) * 100.0
    df["next_low_from_open_pct"] = (df["next_low"] / df["next_open"] - 1.0) * 100.0
    df["next_close_from_signal_pct"] = (df["next_close"] / df["close"] - 1.0) * 100.0
    df["sell_open_edge_pct"] = -df["next_open_to_close_pct"]
    return df


def make_gap_exit_events(
    featured: pd.DataFrame,
    flow_threshold: float = 0.9,
    min_signal_ret_pct: float = 1.0,
    min_close_pos: float = 0.65,
    min_next_gap_pct: float = 0.5,
) -> pd.DataFrame:
    df = featured.copy()
    mask = (
        (df["flow_pctile"] >= flow_threshold)
        & (df["day_ret_pct"] >= min_signal_ret_pct)
        & (df["close_pos"] >= min_close_pos)
        & (df["next_gap_pct"] >= min_next_gap_pct)
        & df["next_open"].gt(0)
        & df["next_close"].gt(0)
    )
    cols = [
        "trade_date",
        "next_trade_date",
        "share",
        "share_chg_5d",
        "flow_pctile",
        "day_ret_pct",
        "close_pos",
        "close",
        "next_open",
        "next_high",
        "next_low",
        "next_close",
        "next_gap_pct",
        "next_open_to_close_pct",
        "next_high_from_open_pct",
        "next_low_from_open_pct",
        "next_close_from_signal_pct",
        "sell_open_edge_pct",
    ]
    return df.loc[mask, cols].reset_index(drop=True)


def _clean_number(value: float | int | None, ndigits: int = 2) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), ndigits)


def _series_summary(values: pd.Series, ndigits: int = 2) -> dict:
    s = pd.to_numeric(values, errors="coerce").dropna()
    if s.empty:
        return {"mean": None, "median": None, "positive_rate_pct": None, "negative_rate_pct": None}
    return {
        "mean": _clean_number(s.mean(), ndigits),
        "median": _clean_number(s.median(), ndigits),
        "positive_rate_pct": round(float((s > 0).mean() * 100.0), 1),
        "negative_rate_pct": round(float((s < 0).mean() * 100.0), 1),
    }


def summarize_events(events: pd.DataFrame) -> dict:
    result = {"n": int(len(events))}
    if events.empty:
        return result
    next_gap = _series_summary(events["next_gap_pct"])
    fade = _series_summary(events["next_open_to_close_pct"])
    edge = _series_summary(events["sell_open_edge_pct"])
    result.update({
        "next_gap_mean_pct": next_gap["mean"],
        "next_gap_median_pct": next_gap["median"],
        "next_open_to_close_mean_pct": fade["mean"],
        "next_open_to_close_median_pct": fade["median"],
        "next_open_to_close_negative_rate_pct": fade["negative_rate_pct"],
        "sell_open_edge_mean_pct": edge["mean"],
        "sell_open_edge_median_pct": edge["median"],
        "sell_open_edge_positive_rate_pct": edge["positive_rate_pct"],
    })
    if "next_low_from_open_pct" in events.columns:
        low = _series_summary(events["next_low_from_open_pct"])
        result["next_low_from_open_mean_pct"] = low["mean"]
        result["next_low_from_open_median_pct"] = low["median"]
    if "next_close_from_signal_pct" in events.columns:
        hold = _series_summary(events["next_close_from_signal_pct"])
        result["next_close_from_signal_mean_pct"] = hold["mean"]
        result["next_close_from_signal_median_pct"] = hold["median"]
    return result


def _cache_code(ts_code: str) -> str:
    return ts_code.replace(".", "_")


def _cache_sort_key(path: Path) -> tuple[str, str, float]:
    match = re.search(r"_(\d{8})_(\d{8})\.csv\.gz$", path.name)
    if match:
        return match.group(2), match.group(1), path.stat().st_mtime
    return "", "", path.stat().st_mtime


def latest_cache_file(cache_dir: Path, prefix: str, ts_code: str) -> Path | None:
    matches = list(cache_dir.glob(f"{prefix}_{_cache_code(ts_code)}_*.csv.gz"))
    if not matches:
        return None
    return max(matches, key=_cache_sort_key)


def load_cached_etf_panel(cache_dir: Path, spec: dict) -> tuple[pd.DataFrame, dict]:
    daily_path = latest_cache_file(cache_dir, spec["daily_prefix"], spec["ts_code"])
    share_path = latest_cache_file(cache_dir, spec["share_prefix"], spec["ts_code"])
    meta = {
        "ts_code": spec["ts_code"],
        "name": spec["name"],
        "daily_file": str(daily_path) if daily_path else None,
        "share_file": str(share_path) if share_path else None,
    }
    if daily_path is None or share_path is None:
        return pd.DataFrame(), meta

    daily = pd.read_csv(daily_path, dtype={"trade_date": str, "ts_code": str})
    share = pd.read_csv(share_path, dtype={"trade_date": str, "ts_code": str})
    share_col = next((col for col in ("fd_share", "fund_share", "share") if col in share.columns), None)
    if share_col is None or daily.empty or share.empty:
        return pd.DataFrame(), meta

    daily_cols = [col for col in ("trade_date", "pre_close", "open", "high", "low", "close") if col in daily.columns]
    panel = share[["trade_date", share_col]].rename(columns={share_col: "share"})
    panel = panel.merge(daily[daily_cols], on="trade_date", how="inner")
    panel["trade_date"] = _parse_trade_date(panel["trade_date"])
    meta["rows"] = int(len(panel))
    if not panel.empty:
        meta["min_trade_date"] = panel["trade_date"].min().strftime("%Y-%m-%d")
        meta["max_trade_date"] = panel["trade_date"].max().strftime("%Y-%m-%d")
    return panel.sort_values("trade_date").reset_index(drop=True), meta


def backtest_one_etf(
    spec: dict,
    panel: pd.DataFrame,
    lookback: int,
    flow_threshold: float,
    min_signal_ret_pct: float,
    min_close_pos: float,
    min_next_gap_pct: float,
) -> pd.DataFrame:
    featured = add_late_inflow_features(panel, lookback=lookback)
    events = make_gap_exit_events(
        featured,
        flow_threshold=flow_threshold,
        min_signal_ret_pct=min_signal_ret_pct,
        min_close_pos=min_close_pos,
        min_next_gap_pct=min_next_gap_pct,
    )
    if events.empty:
        return events
    events.insert(0, "name", spec["name"])
    events.insert(0, "ts_code", spec["ts_code"])
    return events


def summarize_by_etf(events: pd.DataFrame) -> list[dict]:
    if events.empty:
        return []
    rows = []
    for (ts_code, name), group in events.groupby(["ts_code", "name"], dropna=False):
        item = {"ts_code": ts_code, "name": name}
        item.update(summarize_events(group))
        rows.append(item)
    return sorted(rows, key=lambda item: (item.get("n") or 0), reverse=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default=str(CACHE))
    parser.add_argument("--output", default=str(OUT))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--lookback", type=int, default=60)
    parser.add_argument("--flow-threshold", type=float, default=0.9)
    parser.add_argument("--min-signal-ret", type=float, default=1.0)
    parser.add_argument("--min-close-pos", type=float, default=0.65)
    parser.add_argument("--min-next-gap", type=float, default=0.5)
    return parser.parse_args()


def _filter_period(panel: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    out = panel.copy()
    if start:
        out = out[out["trade_date"] >= pd.to_datetime(start)]
    if end:
        out = out[out["trade_date"] <= pd.to_datetime(end)]
    return out.reset_index(drop=True)


def main() -> None:
    cfg = parse_args()
    cache_dir = Path(cfg.cache_dir)
    frames = []
    loaded = []
    missing = []

    for spec in STAR_TECH_ETFS:
        panel, meta = load_cached_etf_panel(cache_dir, spec)
        if panel.empty:
            missing.append(meta)
            continue
        panel = _filter_period(panel, cfg.start, cfg.end)
        if panel.empty:
            missing.append(meta)
            continue
        loaded.append(meta)
        events = backtest_one_etf(
            spec,
            panel,
            lookback=cfg.lookback,
            flow_threshold=cfg.flow_threshold,
            min_signal_ret_pct=cfg.min_signal_ret,
            min_close_pos=cfg.min_close_pos,
            min_next_gap_pct=cfg.min_next_gap,
        )
        if not events.empty:
            frames.append(events)

    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not events.empty:
        events = events.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    result = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "source": "local etf_flow_cache fund_share/fund_daily",
        "strategy": {
            "share_change_window": "5 trading days inclusive",
            "rolling_lookback": cfg.lookback,
            "flow_percentile_threshold": cfg.flow_threshold,
            "min_signal_day_return_pct": cfg.min_signal_ret,
            "min_signal_day_close_position": cfg.min_close_pos,
            "min_next_day_gap_pct": cfg.min_next_gap,
        },
        "summary_all": summarize_events(events),
        "summary_by_etf": summarize_by_etf(events),
        "loaded": loaded,
        "missing": missing,
        "recent_events": json.loads(
            events.tail(80).to_json(orient="records", date_format="iso", force_ascii=False)
        )
        if not events.empty
        else [],
        "events": json.loads(events.to_json(orient="records", date_format="iso", force_ascii=False))
        if not events.empty
        else [],
    }

    out_path = Path(cfg.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary_all": result["summary_all"], "summary_by_etf": result["summary_by_etf"]}, ensure_ascii=False, indent=2))
    print(f"written: {out_path}")


if __name__ == "__main__":
    main()
