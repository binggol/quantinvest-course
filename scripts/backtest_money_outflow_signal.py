"""Backtest stock and market money-outflow signals.

Signal definition:
- stock_main_net_ratio = (large + extra-large buy amount - sell amount) / amount
- outflow means the ratio is very negative.
- market_main_net_ratio aggregates all stocks by date.

The script writes data/money_outflow_signal.json for pages/APIs to consume.
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
CACHE = DATA / "moneyflow_cache"
OUT = DATA / "money_outflow_signal.json"
DEFAULT_PARQUET_DIR = Path(r"\/app/qlib_data\csv_tmp\tushare_daily")
NAS_CSV_TMP = Path(r"\/app/qlib_data\csv_tmp")
HORIZONS = (1, 3, 5, 10, 20)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--parquet-dir", default=str(DEFAULT_PARQUET_DIR))
    p.add_argument("--sample-every", type=int, default=1, help="Use every Nth trade date for faster exploratory runs.")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--sleep", type=float, default=0.12)
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
        raise RuntimeError("missing TUSHARE_TOKEN or data/.tushare_token")
    return ts.pro_api(tok)


def ymd(value) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def pct(value, ndigits: int = 3):
    if value is None or pd.isna(value):
        return None
    return round(float(value) * 100.0, ndigits)


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    if isinstance(value, tuple):
        return [clean_json(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if not np.isfinite(float(value)):
            return None
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return value


def load_prices(parquet_dir: Path, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=max(HORIZONS) + 10)
    files = []
    for path in sorted(parquet_dir.glob("*.parquet")):
        try:
            dt = pd.Timestamp(path.stem)
        except Exception:
            continue
        if start_ts <= dt <= end_ts:
            files.append(path)
    if not files:
        raise RuntimeError(f"no parquet files under {parquet_dir}")
    frames = []
    for path in files:
        df = pd.read_parquet(path, columns=["ts_code", "trade_date", "close", "amount"])
        frames.append(df)
    data = pd.concat(frames, ignore_index=True)
    data["trade_date"] = data["trade_date"].astype(str)
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data["amount"] = pd.to_numeric(data["amount"], errors="coerce")
    data = data.dropna(subset=["ts_code", "trade_date", "close"])
    data = data.sort_values(["ts_code", "trade_date"])
    g = data.groupby("ts_code", group_keys=False)
    data["ret_1d"] = g["close"].pct_change(1)
    data["ret_5d"] = g["close"].pct_change(5)
    data["ret_20d"] = g["close"].pct_change(20)
    data["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    data["high20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=10).max())
    data["drawdown20"] = data["close"] / data["high20"] - 1.0
    data["amount_ma20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    data["amount_ratio20"] = data["amount"] / (data["amount_ma20"] + 1e-9)
    for h in HORIZONS:
        data[f"fwd_{h}d"] = data.groupby("ts_code")["close"].shift(-h) / data["close"] - 1.0
    return data


def fetch_moneyflow_one(pro, trade_date: str, refresh: bool, sleep: float) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"moneyflow_{trade_date}.csv.gz"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})
    fields = ",".join([
        "ts_code", "trade_date",
        "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
        "net_mf_amount",
    ])
    df = pro.moneyflow(trade_date=trade_date, fields=fields)
    if df is None:
        df = pd.DataFrame()
    df.to_csv(path, index=False, compression="gzip")
    time.sleep(sleep)
    return df


def load_moneyflow(pro, dates: list[str], refresh: bool, sleep: float) -> pd.DataFrame:
    frames = []
    for i, d in enumerate(dates, 1):
        try:
            df = fetch_moneyflow_one(pro, d, refresh=refresh, sleep=sleep)
        except Exception as exc:
            print(f"[moneyflow] {d} failed: {exc}", flush=True)
            continue
        if df is not None and not df.empty:
            frames.append(df)
        if i % 50 == 0:
            print(f"[moneyflow] loaded {i}/{len(dates)}", flush=True)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data["trade_date"] = data["trade_date"].astype(str)
    for c in ("buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"):
        if c in data.columns:
            data[c] = pd.to_numeric(data[c], errors="coerce").fillna(0.0)
    return data


def add_flow_features(mf: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    if mf.empty:
        return pd.DataFrame()
    x = mf.merge(prices, on=["ts_code", "trade_date"], how="inner")
    for c in ("buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"):
        if c not in x.columns:
            x[c] = 0.0
    if "net_mf_amount" not in x.columns:
        x["net_mf_amount"] = 0.0
    x["main_net_amount"] = (
        x["buy_lg_amount"] + x["buy_elg_amount"] - x["sell_lg_amount"] - x["sell_elg_amount"]
    )
    use_net = x["main_net_amount"].abs().sum() <= 0 and x["net_mf_amount"].abs().sum() > 0
    if use_net:
        x["main_net_amount"] = x["net_mf_amount"]
    amount_wan = x["amount"] / 10.0
    x["main_net_ratio"] = x["main_net_amount"] / (amount_wan.abs() + 1e-9)
    x["main_net_ratio"] = x["main_net_ratio"].clip(-1.5, 1.5)
    x["outflow_ratio"] = -x["main_net_ratio"]
    x = x.sort_values(["ts_code", "trade_date"])
    x["main_net_ratio_3d_prev"] = x.groupby("ts_code")["main_net_ratio"].transform(
        lambda s: s.rolling(3, min_periods=2).mean().shift(1)
    )
    for w in (3, 5, 10, 15):
        x[f"main_net_{w}d"] = x.groupby("ts_code")["main_net_amount"].transform(
            lambda s, window=w: s.rolling(window, min_periods=max(2, min(window, 3))).sum()
        )
        x[f"amount_{w}d"] = x.groupby("ts_code")["amount"].transform(
            lambda s, window=w: s.rolling(window, min_periods=max(2, min(window, 3))).sum()
        )
        x[f"main_net_ratio_{w}d"] = x[f"main_net_{w}d"] / (x[f"amount_{w}d"] / 10.0 + 1e-9)
    return x


def stock_backtest(x: pd.DataFrame) -> dict:
    rows = []
    for d, g in x.groupby("trade_date", sort=True):
        g = g.dropna(subset=["outflow_ratio"])
        if len(g) < 300:
            continue
        q80 = g["outflow_ratio"].quantile(0.80)
        out = g[g["outflow_ratio"] >= q80]
        rest = g[g["outflow_ratio"] < q80]
        if len(out) < 20 or len(rest) < 50:
            continue
        row = {"trade_date": d, "n": int(len(g)), "n_outflow": int(len(out)), "threshold_pct": pct(q80)}
        for h in HORIZONS:
            col = f"fwd_{h}d"
            a = out[col].dropna()
            b = rest[col].dropna()
            if len(a) < 10 or len(b) < 50:
                continue
            row[f"outflow_ret_{h}d"] = float(a.mean())
            row[f"rest_ret_{h}d"] = float(b.mean())
            row[f"spread_{h}d"] = float(a.mean() - b.mean())
            row[f"fall_rate_{h}d"] = float((a < 0).mean())
            row[f"underperform_rate_{h}d"] = float((a.mean() < b.mean()))
        rows.append(row)
    daily = pd.DataFrame(rows)
    summary = {}
    if daily.empty:
        return {"summary": summary, "daily": []}
    for h in HORIZONS:
        s = daily.get(f"spread_{h}d", pd.Series(dtype=float)).dropna()
        ret = daily.get(f"outflow_ret_{h}d", pd.Series(dtype=float)).dropna()
        fall = daily.get(f"fall_rate_{h}d", pd.Series(dtype=float)).dropna()
        if s.empty:
            continue
        summary[str(h)] = {
            "n_days": int(len(s)),
            "outflow_avg_ret_pct": pct(ret.mean()) if len(ret) else None,
            "spread_vs_rest_pct": pct(s.mean()),
            "underperform_days_pct": pct((s < 0).mean()),
            "fall_rate_pct": pct(fall.mean()) if len(fall) else None,
            "t_stat": round(float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if len(s) > 1 and s.std(ddof=1) else 0.0, 3),
        }
    return {"summary": summary, "daily": daily.tail(120).to_dict("records")}


def load_index_returns(start: str, end: str) -> pd.DataFrame:
    cache = DATA / "etf_flow_cache"
    files = sorted(cache.glob("index_000300_SH_*.csv.gz"))
    if files:
        df = pd.read_csv(files[-1], dtype={"trade_date": str})
    else:
        pro = api()
        df = pro.index_daily(ts_code="000300.SH", start_date=ymd(start), end_date=ymd(pd.Timestamp(end) + pd.Timedelta(days=40)))
    df = df.sort_values("trade_date")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    for h in HORIZONS:
        df[f"fwd_{h}d"] = df["close"].shift(-h) / df["close"] - 1.0
    return df[["trade_date", "close"] + [f"fwd_{h}d" for h in HORIZONS]]


def market_backtest(x: pd.DataFrame, start: str, end: str) -> dict:
    m = x.groupby("trade_date", as_index=False).agg(main_net_amount=("main_net_amount", "sum"), amount=("amount", "sum"))
    m["market_net_ratio"] = m["main_net_amount"] / (m["amount"] / 10.0 + 1e-9)
    m["market_net_ratio"] = m["market_net_ratio"].clip(-1.0, 1.0)
    m["outflow_3d"] = -m["market_net_ratio"].rolling(3, min_periods=2).mean()
    m["outflow_5d"] = -m["market_net_ratio"].rolling(5, min_periods=3).mean()
    idx = load_index_returns(start, end)
    m = m.merge(idx, on="trade_date", how="inner")
    summary = {}
    for signal in ("outflow_3d", "outflow_5d"):
        q80 = m[signal].quantile(0.80)
        bad = m[m[signal] >= q80]
        normal = m[m[signal] < q80]
        block = {"threshold_pct": pct(q80), "n_days": int(len(bad))}
        for h in HORIZONS:
            a = bad[f"fwd_{h}d"].dropna()
            b = normal[f"fwd_{h}d"].dropna()
            if len(a) < 5 or len(b) < 20:
                continue
            spread = a.mean() - b.mean()
            block[str(h)] = {
                "index_avg_ret_pct": pct(a.mean()),
                "spread_vs_normal_pct": pct(spread),
                "down_days_pct": pct((a < 0).mean()),
                "t_stat": round(float(spread / np.sqrt((a.var(ddof=1) / len(a)) + (b.var(ddof=1) / len(b)))) if len(a) > 1 and len(b) > 1 else 0.0, 3),
            }
        summary[signal] = block
    latest = m.dropna(subset=["market_net_ratio"]).tail(1)
    latest_item = {}
    if not latest.empty:
        r = latest.iloc[-1]
        latest_item = {
            "trade_date": f"{r.trade_date[:4]}-{r.trade_date[4:6]}-{r.trade_date[6:8]}",
            "market_net_ratio_pct": pct(r.market_net_ratio),
            "outflow_3d_pct": pct(r.outflow_3d),
            "outflow_5d_pct": pct(r.outflow_5d),
        }
    return {"summary": summary, "latest": latest_item, "daily": m.tail(180).to_dict("records")}


def entry_condition_backtest(x: pd.DataFrame) -> dict:
    """Test when stock-level money flow is more useful for entries.

    Each condition is evaluated cross-sectionally by trade date and compared
    with the rest of the same-day universe, so the result is less affected by
    broad market direction.
    """
    specs = [
        ("inflow_top20", "净流入前20%", lambda g, q: g["main_net_ratio"] >= q["inflow80"]),
        ("outflow_top20", "净流出前20%", lambda g, q: g["outflow_ratio"] >= q["outflow80"]),
        ("outflow_price_up", "净流出前20%但当日上涨", lambda g, q: (g["outflow_ratio"] >= q["outflow80"]) & (g["ret_1d"] > 0)),
        ("outflow_price_down", "净流出前20%且当日下跌", lambda g, q: (g["outflow_ratio"] >= q["outflow80"]) & (g["ret_1d"] < 0)),
        ("outflow_oversold", "净流出前20%且20日回撤>8%", lambda g, q: (g["outflow_ratio"] >= q["outflow80"]) & (g["drawdown20"] <= -0.08)),
        ("outflow_capitulation", "净流出前20%+放量+回撤", lambda g, q: (g["outflow_ratio"] >= q["outflow80"]) & (g["drawdown20"] <= -0.08) & (g["amount_ratio20"] >= 1.5)),
        ("outflow_strong_trend", "净流出前20%但仍在20日线上", lambda g, q: (g["outflow_ratio"] >= q["outflow80"]) & (g["close"] >= g["ma20"]) & (g["ret_20d"] > 0)),
        ("outflow_to_inflow", "前3日流出后当日转净流入", lambda g, q: (g["main_net_ratio_3d_prev"] < 0) & (g["main_net_ratio"] > 0)),
        ("inflow_breakout", "净流入前20%且20日新高附近", lambda g, q: (g["main_net_ratio"] >= q["inflow80"]) & (g["close"] >= g["high20"] * 0.98)),
        ("inflow_pullback", "净流入前20%且20日回撤>5%", lambda g, q: (g["main_net_ratio"] >= q["inflow80"]) & (g["drawdown20"] <= -0.05)),
    ]
    daily_rows = {key: [] for key, _, _ in specs}
    for d, g in x.groupby("trade_date", sort=True):
        g = g.dropna(subset=["main_net_ratio", "outflow_ratio"])
        if len(g) < 300:
            continue
        q = {
            "inflow80": g["main_net_ratio"].quantile(0.80),
            "outflow80": g["outflow_ratio"].quantile(0.80),
        }
        for key, label, fn in specs:
            try:
                mask = fn(g, q).fillna(False)
            except Exception:
                continue
            pick = g[mask]
            rest = g[~mask]
            if len(pick) < 10 or len(rest) < 50:
                continue
            row = {"trade_date": d, "key": key, "label": label, "n": int(len(pick))}
            for h in HORIZONS:
                col = f"fwd_{h}d"
                a = pick[col].dropna()
                b = rest[col].dropna()
                if len(a) < 8 or len(b) < 50:
                    continue
                row[f"ret_{h}d"] = float(a.mean())
                row[f"rest_{h}d"] = float(b.mean())
                row[f"spread_{h}d"] = float(a.mean() - b.mean())
                row[f"win_{h}d"] = float((a > 0).mean())
            daily_rows[key].append(row)
    summary = []
    daily_tail = {}
    for key, label, _ in specs:
        df = pd.DataFrame(daily_rows.get(key) or [])
        if df.empty:
            continue
        item = {"key": key, "label": label, "n_days": int(len(df)), "avg_n": round(float(df["n"].mean()), 1)}
        score_parts = []
        for h in HORIZONS:
            s = df.get(f"spread_{h}d", pd.Series(dtype=float)).dropna()
            ret = df.get(f"ret_{h}d", pd.Series(dtype=float)).dropna()
            win = df.get(f"win_{h}d", pd.Series(dtype=float)).dropna()
            if s.empty:
                continue
            item[str(h)] = {
                "avg_ret_pct": pct(ret.mean()) if len(ret) else None,
                "spread_pct": pct(s.mean()),
                "win_rate_pct": pct(win.mean()) if len(win) else None,
                "positive_spread_days_pct": pct((s > 0).mean()),
                "t_stat": round(float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if len(s) > 1 and s.std(ddof=1) else 0.0, 3),
            }
            if h in (5, 10, 20):
                score_parts.append(float(s.mean()))
        item["score_pct"] = pct(np.mean(score_parts)) if score_parts else None
        summary.append(item)
        daily_tail[key] = df.tail(80).to_dict("records")
    summary = sorted(summary, key=lambda r: (r.get("10") or {}).get("spread_pct") if isinstance(r.get("10"), dict) and (r.get("10") or {}).get("spread_pct") is not None else -999, reverse=True)
    return {
        "summary": summary,
        "daily": daily_tail,
        "note": "同日横截面对比：每个条件选出的股票 vs 当日其余股票，统计后续1/3/5/10/20日收益、胜率和超额。",
    }


def rolling_inflow_backtest(x: pd.DataFrame) -> dict:
    """Backtest buying stocks with high recent net inflow.

    Signals are ranked cross-sectionally each day.  Amount rank tests whether
    big absolute money buys work; ratio rank tests whether high inflow intensity
    works after normalizing by turnover.
    """
    configs = []
    for window in (3, 5, 10, 15):
        configs.append((f"amount_{window}d", f"{window}日净流入额", f"main_net_{window}d"))
        configs.append((f"ratio_{window}d", f"{window}日净流入/成交额", f"main_net_ratio_{window}d"))
    qs = (0.95, 0.90, 0.80)
    rows = []
    for d, g in x.groupby("trade_date", sort=True):
        if len(g) < 300:
            continue
        for key, label, col in configs:
            gg = g.dropna(subset=[col])
            gg = gg[gg[col] > 0]
            if len(gg) < 100:
                continue
            for q in qs:
                threshold = gg[col].quantile(q)
                pick = gg[gg[col] >= threshold]
                rest = g[~g["ts_code"].isin(set(pick["ts_code"]))]
                if len(pick) < 10 or len(rest) < 50:
                    continue
                row = {
                    "trade_date": d,
                    "key": key,
                    "label": label,
                    "top_pct": round((1.0 - q) * 100, 1),
                    "threshold": float(threshold),
                    "n": int(len(pick)),
                }
                for h in HORIZONS:
                    colret = f"fwd_{h}d"
                    a = pick[colret].dropna()
                    b = rest[colret].dropna()
                    if len(a) < 8 or len(b) < 50:
                        continue
                    row[f"ret_{h}d"] = float(a.mean())
                    row[f"rest_{h}d"] = float(b.mean())
                    row[f"spread_{h}d"] = float(a.mean() - b.mean())
                    row[f"win_{h}d"] = float((a > 0).mean())
                rows.append(row)
    daily = pd.DataFrame(rows)
    if daily.empty:
        return {"summary": [], "daily": [], "note": "no samples"}
    summary = []
    for (key, top_pct), df in daily.groupby(["key", "top_pct"], sort=False):
        label = str(df["label"].iloc[0])
        item = {"key": key, "label": label, "top_pct": top_pct, "n_days": int(len(df)), "avg_n": round(float(df["n"].mean()), 1)}
        score_parts = []
        for h in HORIZONS:
            s = df.get(f"spread_{h}d", pd.Series(dtype=float)).dropna()
            ret = df.get(f"ret_{h}d", pd.Series(dtype=float)).dropna()
            win = df.get(f"win_{h}d", pd.Series(dtype=float)).dropna()
            if s.empty:
                continue
            item[str(h)] = {
                "avg_ret_pct": pct(ret.mean()) if len(ret) else None,
                "spread_pct": pct(s.mean()),
                "win_rate_pct": pct(win.mean()) if len(win) else None,
                "positive_spread_days_pct": pct((s > 0).mean()),
                "t_stat": round(float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if len(s) > 1 and s.std(ddof=1) else 0.0, 3),
            }
            if h in (5, 10, 20):
                score_parts.append(float(s.mean()))
        item["score_pct"] = pct(np.mean(score_parts)) if score_parts else None
        summary.append(item)
    summary = sorted(summary, key=lambda r: (r.get("10") or {}).get("spread_pct") if isinstance(r.get("10"), dict) and (r.get("10") or {}).get("spread_pct") is not None else -999, reverse=True)
    return {
        "summary": summary,
        "daily": daily.tail(300).to_dict("records"),
        "note": "每日按近3/5/10/15日净流入额或净流入/成交额排序，买Top5/10/20%，与同日其余股票比较后续收益。",
    }


def load_stock_meta() -> pd.DataFrame:
    db = DATA / "stock_meta.db"
    if not db.exists():
        return pd.DataFrame()
    try:
        import sqlite3
        con = sqlite3.connect(db)
        meta = pd.read_sql("select ts_code,name,industry from stock_meta", con)
        con.close()
        return meta.drop_duplicates("ts_code")
    except Exception as exc:
        print(f"[moneyflow] stock_meta unavailable: {exc}", flush=True)
        return pd.DataFrame()


def latest_stock_flow_rows(x: pd.DataFrame, meta: pd.DataFrame | None = None, limit: int | None = None) -> list[dict]:
    if x.empty:
        return []
    last = sorted(x["trade_date"].dropna().unique())[-1]
    recent_dates = sorted(x["trade_date"].dropna().unique())[-15:]
    g = x[x["trade_date"] == last].dropna(subset=["outflow_ratio"]).copy()
    if g.empty:
        return []
    recent = x[x["trade_date"].isin(recent_dates)].copy()
    recent["main_net_amount"] = pd.to_numeric(recent["main_net_amount"], errors="coerce")
    recent["amount"] = pd.to_numeric(recent["amount"], errors="coerce")
    recent["outflow_net_amount"] = -recent["main_net_amount"]
    daily_map: dict[str, list[dict]] = {}
    recent5_map: dict[str, list[dict]] = {}
    for code, part in recent.sort_values("trade_date").groupby("ts_code"):
        daily = [
            {
                "date": f"{str(r.trade_date)[:4]}-{str(r.trade_date)[4:6]}-{str(r.trade_date)[6:8]}",
                "outflow_net_yi": round(float(r.outflow_net_amount) / 10000.0, 4) if pd.notna(r.outflow_net_amount) else None,
                "main_net_yi": round(float(r.main_net_amount) / 10000.0, 4) if pd.notna(r.main_net_amount) else None,
            }
            for r in part[["trade_date", "outflow_net_amount", "main_net_amount"]].itertuples(index=False)
        ]
        daily_map[str(code)] = daily
        recent5_map[str(code)] = list(reversed(daily[-5:]))
    sums = recent.groupby("ts_code", as_index=False).agg(
        main_net_15d=("main_net_amount", "sum"),
        amount_15d=("amount", "sum"),
        n_flow_days_15d=("trade_date", "nunique"),
    )
    g = g.drop(columns=[c for c in (
        "main_net_10d", "amount_10d", "n_flow_days_10d",
        "main_net_15d", "amount_15d", "n_flow_days_15d",
    ) if c in g.columns])
    g = g.merge(sums, on="ts_code", how="left")
    if meta is not None and not meta.empty:
        g = g.merge(meta[["ts_code", "name", "industry"]], on="ts_code", how="left")
    else:
        g["name"] = ""
        g["industry"] = ""
    g["rank_pct"] = g["outflow_ratio"].rank(pct=True)
    g["inflow_rank_pct"] = g["main_net_ratio"].rank(pct=True)
    g["outflow_net_amount"] = -pd.to_numeric(g["main_net_amount"], errors="coerce")
    g["outflow_15d_amount"] = -pd.to_numeric(g["main_net_15d"], errors="coerce")
    g["main_net_15d_ratio"] = g["main_net_15d"] / (g["amount_15d"] / 10.0 + 1e-9)
    cols = [
        "ts_code", "name", "industry", "trade_date", "main_net_amount", "outflow_net_amount",
        "main_net_ratio", "outflow_ratio", "rank_pct", "close", "main_net_15d",
        "outflow_15d_amount", "main_net_15d_ratio", "n_flow_days_15d", "inflow_rank_pct",
        "ret_1d", "ret_20d", "ma20", "high20", "drawdown20", "amount_ratio20",
    ]
    ranked = g.nlargest(limit, "outflow_net_amount") if limit else g.sort_values("outflow_net_amount", ascending=False)
    return [
        {
            "code": r.ts_code,
            "name": "" if pd.isna(r.name) else str(r.name),
            "industry": "" if pd.isna(r.industry) else str(r.industry),
            "trade_date": f"{r.trade_date[:4]}-{r.trade_date[4:6]}-{r.trade_date[6:8]}",
            "close": round(float(r.close), 3),
            "main_net_yi": round(float(r.main_net_amount) / 10000.0, 4),
            "outflow_net_yi": round(float(r.outflow_net_amount) / 10000.0, 4),
            "main_net_ratio_pct": pct(r.main_net_ratio),
            "outflow_rank_pct": pct(r.rank_pct),
            "inflow_rank_pct": pct(r.inflow_rank_pct),
            "ret_1d_pct": pct(r.ret_1d),
            "ret_20d_pct": pct(r.ret_20d),
            "ma20": round(float(r.ma20), 4) if pd.notna(r.ma20) else None,
            "high20": round(float(r.high20), 4) if pd.notna(r.high20) else None,
            "drawdown20_pct": pct(r.drawdown20),
            "amount_ratio20": round(float(r.amount_ratio20), 3) if pd.notna(r.amount_ratio20) else None,
            "main_net_15d_yi": round(float(r.main_net_15d) / 10000.0, 4) if pd.notna(r.main_net_15d) else None,
            "outflow_15d_yi": round(float(r.outflow_15d_amount) / 10000.0, 4) if pd.notna(r.outflow_15d_amount) else None,
            "main_net_15d_ratio_pct": pct(r.main_net_15d_ratio) if pd.notna(r.main_net_15d_ratio) else None,
            "n_flow_days_15d": int(r.n_flow_days_15d) if pd.notna(r.n_flow_days_15d) else 0,
            "outflow_15d_daily": daily_map.get(str(r.ts_code), []),
            "outflow_recent_5d": recent5_map.get(str(r.ts_code), []),
            # Backward-compatible aliases for pages that still read the old
            # 10d names. The values are intentionally the same 15-trading-day
            # window used above.
            "main_net_10d_yi": round(float(r.main_net_15d) / 10000.0, 4) if pd.notna(r.main_net_15d) else None,
            "outflow_10d_yi": round(float(r.outflow_15d_amount) / 10000.0, 4) if pd.notna(r.outflow_15d_amount) else None,
            "main_net_10d_ratio_pct": pct(r.main_net_15d_ratio) if pd.notna(r.main_net_15d_ratio) else None,
            "n_flow_days_10d": int(r.n_flow_days_15d) if pd.notna(r.n_flow_days_15d) else 0,
            "outflow_10d_daily": daily_map.get(str(r.ts_code), []),
        }
        for r in ranked[cols].itertuples(index=False)
    ]


def latest_stock_flags(x: pd.DataFrame, meta: pd.DataFrame | None = None) -> list[dict]:
    return latest_stock_flow_rows(x, meta=meta, limit=200)


def sector_outflow_ranking(x: pd.DataFrame, meta: pd.DataFrame | None = None) -> list[dict]:
    if x.empty or meta is None or meta.empty:
        return []
    last = sorted(x["trade_date"].dropna().unique())[-1]
    g = x[x["trade_date"] == last].copy()
    g = g.merge(meta[["ts_code", "industry"]], on="ts_code", how="left")
    g["industry"] = g["industry"].fillna("").replace("", "未分类")
    g["outflow_net_amount"] = -pd.to_numeric(g["main_net_amount"], errors="coerce")
    grouped = g.groupby("industry", as_index=False).agg(
        trade_date=("trade_date", "last"),
        n_stocks=("ts_code", "nunique"),
        outflow_net_amount=("outflow_net_amount", "sum"),
        main_net_amount=("main_net_amount", "sum"),
        amount=("amount", "sum"),
        median_outflow_ratio=("outflow_ratio", "median"),
    )
    grouped["main_net_ratio"] = grouped["main_net_amount"] / (grouped["amount"] / 10.0 + 1e-9)
    grouped["outflow_ratio"] = -grouped["main_net_ratio"]
    grouped["outflow_net_yi"] = grouped["outflow_net_amount"] / 10000.0
    grouped["main_net_yi"] = grouped["main_net_amount"] / 10000.0
    grouped["amount_yi"] = grouped["amount"] / 100000.0
    grouped = grouped.sort_values("outflow_net_yi", ascending=False)
    rows = []
    for r in grouped.head(100).itertuples(index=False):
        rows.append({
            "industry": str(r.industry),
            "trade_date": f"{r.trade_date[:4]}-{r.trade_date[4:6]}-{r.trade_date[6:8]}",
            "n_stocks": int(r.n_stocks),
            "outflow_net_yi": round(float(r.outflow_net_yi), 3),
            "main_net_yi": round(float(r.main_net_yi), 3),
            "amount_yi": round(float(r.amount_yi), 2),
            "main_net_ratio_pct": pct(r.main_net_ratio),
            "median_outflow_ratio_pct": pct(r.median_outflow_ratio),
        })
    return rows


def main():
    args = parse_args()
    start = pd.Timestamp(args.start).strftime("%Y-%m-%d")
    end = pd.Timestamp(args.end).strftime("%Y-%m-%d")
    prices = load_prices(Path(args.parquet_dir), start, end)
    trade_dates = sorted(prices.loc[(prices["trade_date"] >= ymd(start)) & (prices["trade_date"] <= ymd(end)), "trade_date"].unique())
    if args.sample_every > 1:
        trade_dates = trade_dates[:: args.sample_every]
    pro = api()
    mf = load_moneyflow(pro, trade_dates, refresh=args.refresh, sleep=args.sleep)
    featured = add_flow_features(mf, prices)
    if featured.empty:
        raise RuntimeError("moneyflow feature panel is empty")
    stock = stock_backtest(featured)
    market = market_backtest(featured, start, end)
    entry_conditions = entry_condition_backtest(featured)
    rolling_inflow = rolling_inflow_backtest(featured)
    meta = load_stock_meta()
    latest_flags = latest_stock_flags(featured, meta=meta)
    latest_flow_all = latest_stock_flow_rows(featured, meta=meta, limit=None)
    sector_flags = sector_outflow_ranking(featured, meta=meta)
    out = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": start,
        "end": end,
        "n_moneyflow_rows": int(len(mf)),
        "n_feature_rows": int(len(featured)),
        "method": {
            "stock": "每日按大单+超大单净额/成交额排序, 资金流出最重20%对比其余股票后续收益",
            "market": "全市场聚合大单+超大单净额/成交额, 3/5日均流出最重20%对比正常日期沪深300后续收益",
            "unit_note": "Tushare moneyflow金额按万元口径, 日线amount按千元口径, 已换算为同口径比值",
        },
        "stock": stock,
        "market": market,
        "entry_conditions": entry_conditions,
        "rolling_inflow": rolling_inflow,
        "latest_stock_outflow": latest_flags,
        "latest_stock_flow_all": latest_flow_all,
        "sector_outflow": sector_flags,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    out = clean_json(out)
    output.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if os.environ.get("QI_SKIP_MONEYFLOW_NAS_PUBLISH") != "1":
        try:
            NAS_CSV_TMP.mkdir(parents=True, exist_ok=True)
            (NAS_CSV_TMP / output.name).write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        except Exception as exc:
            print(f"[moneyflow] copy to NAS failed: {exc}", flush=True)
    print(f"[moneyflow] wrote {output} rows={len(featured)}")
    print(json.dumps(clean_json({"stock": stock.get("summary"), "market": market.get("summary"), "latest": market.get("latest")}), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
