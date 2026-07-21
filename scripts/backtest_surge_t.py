"""滚动底仓 T+1 回测：当日低吸，下一交易日冲高回落卖出。

示例：
  python scripts/backtest_surge_t.py --start 2024-01-01 --end 2026-06-30
  python scripts/backtest_surge_t.py --codes 300408.SZ,300308.SZ --refresh

数据源为 Tushare stk_mins，原始分钟数据缓存在 data/surge_t_cache/。
研究结果写入 data/surge_t_backtest.json，供网页展示。该脚本不下单。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "surge_t_cache"
POSITIONS = DATA / "positions.json"
OUTPUT = DATA / "surge_t_backtest.json"


@dataclass(frozen=True)
class Params:
    dev_atr: float
    range_pos: float
    surge_pct: float
    reject_pct: float
    max_hold: int = 1
    stop_pct: float = 6.0
    stop_atr: float = 2.0


def parse_args():
    p = argparse.ArgumentParser(description="回测滚动底仓T+1策略")
    p.add_argument("--codes", help="逗号分隔代码；默认读取 data/positions.json")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--freq", default="5min", choices=["1min", "5min", "15min"])
    p.add_argument("--capital", type=float, default=100000)
    p.add_argument("--commission", type=float, default=0.00025)
    p.add_argument("--min-commission", type=float, default=5.0)
    p.add_argument("--stamp-tax", type=float, default=0.0005)
    p.add_argument("--slippage-bp", type=float, default=2.0)
    p.add_argument("--train-months", type=int, default=12)
    p.add_argument("--test-months", type=int, default=3)
    p.add_argument("--quick", action="store_true", help="使用12组代表性参数，先快速得到样本外结果")
    p.add_argument("--refresh", action="store_true")
    return p.parse_args()


def load_codes(raw):
    if raw:
        return sorted({x.strip().upper() for x in raw.split(",") if x.strip()})
    try:
        positions = json.loads(POSITIONS.read_text(encoding="utf-8"))
        return sorted({str(x.get("code", "")).upper() for x in positions if x.get("code")})
    except Exception:
        return []


def tushare_api():
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少 TUSHARE_TOKEN 环境变量")
    ts.set_token(token)
    return ts.pro_api()


def fetch_minutes(pro, code, start, end, freq, refresh=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{code.replace('.', '_')}_{freq}_{start}_{end}.csv.gz"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["trade_time"])
    # 按月拉取，降低单次返回行数和接口超限风险。
    chunks = []
    first_month = pd.Timestamp(start).to_period("M").to_timestamp()
    starts = pd.date_range(first_month, pd.Timestamp(end), freq="MS")
    for month in starts:
        a = max(pd.Timestamp(start), month)
        b = min(pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1),
                month + pd.offsets.MonthEnd(1) + pd.Timedelta(hours=15))
        df = pro.stk_mins(ts_code=code, freq=freq,
                          start_date=a.strftime("%Y-%m-%d 09:00:00"),
                          end_date=b.strftime("%Y-%m-%d 15:05:00"))
        if df is not None and len(df):
            chunks.append(df)
    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks, ignore_index=True).drop_duplicates("trade_time")
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df = df.sort_values("trade_time")
    need = ["trade_time", "open", "high", "low", "close", "vol"]
    missing = [x for x in need if x not in df.columns]
    if missing:
        raise RuntimeError(f"{code} 分钟数据缺字段: {missing}")
    df[need].to_csv(path, index=False, compression="gzip")
    return df[need]


def prepare(df):
    if df.empty:
        return {}, pd.DataFrame()
    x = df.copy()
    x["date"] = x["trade_time"].dt.normalize()
    x["time"] = x["trade_time"].dt.strftime("%H:%M")
    days = {d: g.reset_index(drop=True) for d, g in x.groupby("date", sort=True)}
    daily = x.groupby("date").agg(open=("open", "first"), high=("high", "max"),
                                   low=("low", "min"), close=("close", "last"),
                                   volume=("vol", "sum")).sort_index()
    daily["ma20"] = daily["close"].rolling(20).mean()
    daily["ma60"] = daily["close"].rolling(60).mean()
    prev = daily["close"].shift(1)
    tr = pd.concat([(daily["high"] - daily["low"]),
                    (daily["high"] - prev).abs(),
                    (daily["low"] - prev).abs()], axis=1).max(axis=1)
    daily["atr14"] = tr.rolling(14).mean()
    return days, daily


def features(g):
    z = g.copy()
    tp = (z["high"] + z["low"] + z["close"]) / 3
    cv = z["vol"].replace(0, np.nan).cumsum()
    z["vwap"] = (tp * z["vol"]).cumsum() / cv
    prev = z["close"].shift(1)
    tr = pd.concat([(z["high"] - z["low"]), (z["high"] - prev).abs(),
                    (z["low"] - prev).abs()], axis=1).max(axis=1)
    z["iatr"] = tr.rolling(14, min_periods=5).mean()
    z["day_low"] = z["low"].cummin()
    z["day_high"] = z["high"].cummax()
    z["range_pos"] = (z["close"] - z["day_low"]) / (z["day_high"] - z["day_low"]).replace(0, np.nan)
    return z


def costs(price, shares, args, sell=False):
    value = price * shares
    commission = max(args.min_commission, value * args.commission)
    return commission + (value * args.stamp_tax if sell else 0)


def simulate_code(code, df, params, args):
    days, daily = prepare(df)
    dates = list(daily.index)
    trades = []
    slip = args.slippage_bp / 10000
    for i in range(60, len(dates) - params.max_hold):
        d = dates[i]
        day = features(days[d])
        dd = daily.loc[d]
        known = daily.iloc[i - 1]
        # 只能使用前一交易日收盘后已知的趋势，禁止引用买入当天收盘造成前视偏差。
        if not (known["close"] >= known["ma20"] >= known["ma60"]) or not np.isfinite(known["atr14"]):
            continue
        candidates = day[(day["time"] >= "10:00") & (day["time"] <= "14:40")].copy()
        candidates["dev_atr"] = (candidates["close"] - candidates["vwap"]) / candidates["iatr"]
        stable = ((candidates["close"] >= candidates["close"].shift(1))
                  & (candidates["low"] >= candidates["low"].shift(1)))
        hit = candidates[(candidates["dev_atr"] <= -params.dev_atr)
                         & (candidates["range_pos"] <= params.range_pos)
                         & stable]
        if hit.empty:
            continue
        buy_bar = hit.iloc[0]
        buy = float(buy_bar["close"]) * (1 + slip)
        shares = max(100, math.floor(args.capital / buy / 100) * 100)
        stop_price = max(buy * (1 - params.stop_pct / 100),
                         buy - params.stop_atr * float(known["atr14"]))

        sell_bar, sell_date, reason = None, None, f"持有{params.max_hold}日到期"
        for hold_day in range(1, params.max_hold + 1):
            next_d = dates[i + hold_day]
            nxt = features(days[next_d])
            preclose = float(daily.loc[dates[i + hold_day - 1], "close"])
            for _, row in nxt.iterrows():
                if row["time"] < "09:45":
                    continue
                ret_from_buy = (row["close"] / buy - 1) * 100
                if float(row["close"]) <= stop_price:
                    sell_bar, sell_date, reason = row, next_d, "止损"
                    break
                surge = (row["day_high"] / preclose - 1) * 100
                reject = (row["close"] / row["day_high"] - 1) * 100
                # 冲高形态成立且覆盖交易成本后才兑现；低于买价的反弹不当作成功T。
                if (surge >= params.surge_pct and reject <= -params.reject_pct
                        and ret_from_buy >= 0.30):
                    sell_bar, sell_date, reason = row, next_d, "冲高回落"
                    break
            if sell_bar is not None:
                break
        if sell_bar is None:
            sell_date = dates[i + params.max_hold]
            last = features(days[sell_date])
            pool = last[last["time"] <= "14:45"]
            sell_bar = pool.iloc[-1] if len(pool) else last.iloc[-1]
        sell = float(sell_bar["close"]) * (1 - slip)
        gross = (sell - buy) * shares
        fee = costs(buy, shares, args) + costs(sell, shares, args, sell=True)
        net = gross - fee
        trades.append({
            "code": code, "buy_date": str(d.date()), "buy_time": buy_bar["time"],
            "sell_date": str(sell_date.date()), "sell_time": sell_bar["time"],
            "buy": round(buy, 3), "sell": round(sell, 3), "shares": shares,
            "reason": reason, "gross_pct": round((sell / buy - 1) * 100, 3),
            "net_pct": round(net / (buy * shares) * 100, 3), "net": round(net, 2),
        })
    return trades


def metrics(trades):
    if not trades:
        return {"n": 0, "win_rate": None, "mean": None, "median": None,
                "profit_factor": None, "net": 0, "max_drawdown": None}
    ordered = sorted(trades, key=lambda x: (x["sell_date"], x["sell_time"], x["code"]))
    r = np.array([x["net_pct"] for x in ordered], dtype=float)
    pnl = np.array([x["net"] for x in ordered], dtype=float)
    curve = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0, curve])
    dd = np.r_[0, curve] - peak
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return {
        "n": len(trades), "win_rate": round(float((r > 0).mean() * 100), 2),
        "mean": round(float(r.mean()), 3), "median": round(float(np.median(r)), 3),
        "profit_factor": round(float(gains / losses), 3) if losses > 0 else None,
        "net": round(float(pnl.sum()), 2), "max_drawdown": round(float(dd.min()), 2),
        "p05": round(float(np.quantile(r, .05)), 3),
    }


def month_add(value, months):
    return value + pd.DateOffset(months=months)


def walk_forward(all_data, grid, args):
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    cursor, folds, oos = start, [], []
    while month_add(cursor, args.train_months + args.test_months) <= end + pd.offsets.MonthEnd(1):
        train_end = month_add(cursor, args.train_months)
        test_end = month_add(train_end, args.test_months)
        scored = []
        for params in grid:
            tr = []
            for code, df in all_data.items():
                sample = df[(df["trade_time"] >= cursor) & (df["trade_time"] < train_end)]
                tr += simulate_code(code, sample, params, args)
            m = metrics(tr)
            # 先要求足够样本，再按净期望和Profit Factor选；不直接最大化胜率。
            score = (m["mean"] or -99) * min(1, m["n"] / 30) if m["n"] else -99
            scored.append((score, params, m))
        _, best, train_m = max(scored, key=lambda x: x[0])
        test_trades = []
        # 给测试段保留60个交易日预热，成交仍只计测试区间。
        warm = train_end - pd.Timedelta(days=120)
        for code, df in all_data.items():
            sample = df[(df["trade_time"] >= warm) & (df["trade_time"] < test_end)]
            test_trades += [x for x in simulate_code(code, sample, best, args)
                            if pd.Timestamp(x["buy_date"]) >= train_end]
        oos += test_trades
        folds.append({"train": [str(cursor.date()), str((train_end - pd.Timedelta(days=1)).date())],
                      "test": [str(train_end.date()), str((test_end - pd.Timedelta(days=1)).date())],
                      "params": asdict(best), "train_metrics": train_m,
                      "test_metrics": metrics(test_trades)})
        cursor = month_add(cursor, args.test_months)
    return folds, oos


def main():
    args = parse_args()
    codes = load_codes(args.codes)
    if not codes:
        raise SystemExit("没有股票代码；请传 --codes 或先录入 positions.json")
    pro = tushare_api()
    all_data = {}
    for code in codes:
        print(f"[fetch] {code}", flush=True)
        df = fetch_minutes(pro, code, args.start, args.end, args.freq, args.refresh)
        if len(df):
            all_data[code] = df
    if args.quick:
        grid = [Params(*x) for x in itertools.product(
            [1.2, 1.5], [0.35], [0.8, 1.5], [0.3, 0.6], [2, 5], [5.0], [2.0])]
    else:
        grid = [Params(*x) for x in itertools.product(
            [0.8, 1.0, 1.2, 1.5], [0.25, 0.35], [0.8, 1.2, 1.8],
            [0.3, 0.5, 0.8], [2, 3, 5], [4.0, 6.0], [1.5, 2.0])]
    folds, oos = walk_forward(all_data, grid, args)
    result = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "method": "12个月训练/3个月样本外滚动；当日低吸、下一交易日高抛",
        "codes": codes, "freq": args.freq,
        "period": [args.start, args.end],
        "costs": {"commission": args.commission, "min_commission": args.min_commission,
                  "stamp_tax": args.stamp_tax, "slippage_bp": args.slippage_bp},
        "grid_size": len(grid), "folds": folds,
        "oos_metrics": metrics(oos), "oos_trades": oos,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["oos_metrics"], ensure_ascii=False, indent=2))
    print(f"written: {OUTPUT}")


if __name__ == "__main__":
    main()
