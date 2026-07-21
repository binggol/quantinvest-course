"""Backtest whether broad ETF share expansion warns of market tops.

The script aggregates selected broad-based A-share ETF shares, marks unusually
large short-term share increases, and checks future index returns/drawdowns.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "etf_flow_cache"
OUT = DATA / "etf_flow_top_signal.json"

BROAD_ETFS = {
    "510300.SH": "沪深300ETF",
    "159919.SZ": "沪深300ETF",
    "510050.SH": "上证50ETF",
    "510500.SH": "中证500ETF",
    "512100.SH": "中证1000ETF",
    "159915.SZ": "创业板ETF",
    "588000.SH": "科创50ETF",
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--index", default="000300.SH")
    p.add_argument("--lookback", type=int, default=60)
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("--direction", choices=("increase", "decrease"), default="increase")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--output", default=str(OUT))
    return p.parse_args()


def token() -> str:
    tok = os.environ.get("TUSHARE_TOKEN", "").strip()
    if tok:
        return tok
    path = DATA / ".tushare_token"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def api():
    tok = token()
    if not tok:
        raise RuntimeError("缺少 TUSHARE_TOKEN 或 data/.tushare_token")
    return ts.pro_api(tok)


def ymd(value: str) -> str:
    return value.replace("-", "")


def cached_call(pro, name: str, cache_key: str, refresh: bool, **kwargs) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{cache_key}.csv.gz"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})
    df = getattr(pro, name)(**kwargs)
    if df is None:
        df = pd.DataFrame()
    df.to_csv(path, index=False, compression="gzip")
    time.sleep(0.2)
    return df


def load_etf_panel(pro, start: str, end: str, refresh: bool) -> pd.DataFrame:
    frames = []
    for code, name in BROAD_ETFS.items():
        share = cached_call(
            pro,
            "fund_share",
            f"share_{code.replace('.', '_')}_{ymd(start)}_{ymd(end)}",
            refresh,
            ts_code=code,
            start_date=ymd(start),
            end_date=ymd(end),
        )
        daily = cached_call(
            pro,
            "fund_daily",
            f"fund_daily_{code.replace('.', '_')}_{ymd(start)}_{ymd(end)}",
            refresh,
            ts_code=code,
            start_date=ymd(start),
            end_date=ymd(end),
        )
        if share.empty:
            continue
        share_col = next((c for c in ("fd_share", "fund_share", "share") if c in share.columns), None)
        if not share_col:
            continue
        keep = share[["trade_date", share_col]].rename(columns={share_col: "share"})
        if not daily.empty and "close" in daily.columns:
            keep = keep.merge(daily[["trade_date", "close"]], on="trade_date", how="left")
        else:
            keep["close"] = np.nan
        keep["ts_code"] = code
        keep["name"] = name
        frames.append(keep)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_index(pro, code: str, start: str, end: str, refresh: bool) -> pd.DataFrame:
    df = cached_call(
        pro,
        "index_daily",
        f"index_{code.replace('.', '_')}_{ymd(start)}_{ymd(end)}",
        refresh,
        ts_code=code,
        start_date=ymd(start),
        end_date=ymd(end),
    )
    if df.empty:
        raise RuntimeError(f"指数行情为空: {code}")
    return df[["trade_date", "close"]]


def aggregate_panel(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        raise RuntimeError("ETF 份额数据为空")
    df = raw.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    df["share"] = pd.to_numeric(df["share"], errors="coerce")
    out = df.dropna(subset=["trade_date", "share"]).groupby("trade_date", as_index=False)["share"].sum()
    out["close"] = np.nan
    return out.sort_values("trade_date").reset_index(drop=True)


def add_flow_features(panel: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    df = panel.copy().sort_values("trade_date").reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["share"] = pd.to_numeric(df["share"], errors="coerce")
    df["share_chg_5d"] = df["share"].pct_change(4)

    def percentile(values: np.ndarray) -> float:
        current = values[-1]
        if not np.isfinite(current):
            return np.nan
        hist = values[np.isfinite(values)]
        if len(hist) < 3:
            return np.nan
        return float((hist <= current).mean())

    df["flow_pctile"] = (
        df["share_chg_5d"]
        .rolling(lookback, min_periods=3)
        .apply(percentile, raw=True)
    )
    return df


def make_signal_events(
    featured: pd.DataFrame,
    threshold: float = 0.9,
    direction: str = "increase",
) -> pd.DataFrame:
    df = featured.copy()
    if direction == "increase":
        hit = df["flow_pctile"] >= threshold
    elif direction == "decrease":
        hit = df["flow_pctile"] <= threshold
    else:
        raise ValueError("direction must be increase or decrease")
    prior_hit = hit.shift(1, fill_value=False)
    events = df[hit & ~prior_hit].copy()
    return events[["trade_date", "share", "share_chg_5d", "flow_pctile"]].reset_index(drop=True)


def forward_outcomes(
    events: pd.DataFrame,
    index: pd.DataFrame,
    horizons: tuple[int, ...] = (5, 10, 20, 60),
    top_window: int = 5,
) -> pd.DataFrame:
    px = index.copy().sort_values("trade_date").reset_index(drop=True)
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    rows = []
    for _, event in events.iterrows():
        event_date = pd.to_datetime(event["trade_date"])
        start_i = px.index[px["trade_date"] >= event_date]
        if len(start_i) == 0:
            continue
        i = int(start_i[0])
        if not np.isfinite(px.loc[i, "close"]) or px.loc[i, "close"] <= 0:
            continue
        rec = event.to_dict()
        rec["trade_date"] = px.loc[i, "trade_date"]
        base = float(px.loc[i, "close"])
        for horizon in horizons:
            j = i + horizon - 1
            if j >= len(px):
                rec[f"ret_{horizon}d"] = None
                rec[f"mdd_{horizon}d"] = None
                continue
            window = px.loc[i:j, "close"].astype(float)
            rec[f"ret_{horizon}d"] = float(window.iloc[-1] / base - 1)
            rec[f"mdd_{horizon}d"] = float((window / window.cummax() - 1).min())
        lo = max(0, i - top_window)
        hi = min(len(px), i + top_window + 1)
        rec["near_top"] = bool(px.loc[i, "close"] >= px.loc[lo:hi - 1, "close"].max() * 0.98)
        rows.append(rec)
    return pd.DataFrame(rows)


def summarize(outcomes: pd.DataFrame) -> dict:
    result = {"n_signals": int(len(outcomes))}
    if outcomes.empty:
        return result
    result["near_top_rate"] = round(float(outcomes["near_top"].mean() * 100), 1)
    for horizon in (5, 10, 20, 60):
        ret = pd.to_numeric(outcomes.get(f"ret_{horizon}d"), errors="coerce").dropna()
        mdd = pd.to_numeric(outcomes.get(f"mdd_{horizon}d"), errors="coerce").dropna()
        result[str(horizon)] = {
            "n": int(len(ret)),
            "mean_ret_pct": round(float(ret.mean() * 100), 2) if len(ret) else None,
            "median_ret_pct": round(float(ret.median() * 100), 2) if len(ret) else None,
            "negative_rate_pct": round(float((ret < 0).mean() * 100), 1) if len(ret) else None,
            "mean_mdd_pct": round(float(mdd.mean() * 100), 2) if len(mdd) else None,
        }
    return result


def main():
    cfg = parse_args()
    pro = api()
    raw = load_etf_panel(pro, cfg.start, cfg.end, cfg.refresh)
    panel = aggregate_panel(raw)
    featured = add_flow_features(panel, lookback=cfg.lookback)
    events = make_signal_events(featured, threshold=cfg.threshold, direction=cfg.direction)
    index = load_index(pro, cfg.index, cfg.start, cfg.end, cfg.refresh)
    outcomes = forward_outcomes(events, index)
    result = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "period": [cfg.start, cfg.end],
        "index": cfg.index,
        "etfs": BROAD_ETFS,
        "signal": {
            "share_change_window": "5 trading days inclusive",
            "direction": cfg.direction,
            "rolling_lookback": cfg.lookback,
            "percentile_threshold": cfg.threshold,
        },
        "summary": summarize(outcomes),
        "events": json.loads(outcomes.to_json(orient="records", date_format="iso", force_ascii=False)),
    }
    out_path = Path(cfg.output)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"written: {out_path}")


if __name__ == "__main__":
    main()
