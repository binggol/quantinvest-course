"""同行业“正向业绩事件 × 回购”历史回测。

严格时点：对每个回购公告，只匹配公告日前 lookback_days 内已经披露的
forecast/express 正向事件；不使用未来公告。结果写 data/event_resonance_backtest.json。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "event_resonance_cache"
OUT = DATA / "event_resonance_backtest.json"
POSITIVE = {"预增", "略增", "扭亏", "续盈", "减亏"}


def args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--lookback-days", type=int, default=20)
    p.add_argument("--benchmark", default="000852.SH")
    p.add_argument("--max-events-per-group", type=int, default=600)
    p.add_argument("--refresh", action="store_true")
    return p.parse_args()


def api():
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少 TUSHARE_TOKEN")
    ts.set_token(token)
    return ts.pro_api()


def cached_query(pro, name, start, end, refresh=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{name}_{start}_{end}.csv.gz"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"ann_date": str, "ts_code": str})
    frames = []
    fn = getattr(pro, name)
    if name in {"forecast", "express"}:
        # 当前接口强制要求ann_date或ts_code，只能按公告工作日拉取。
        days = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
        for n, day in enumerate(days, 1):
            ann_date = day.strftime("%Y%m%d")
            try:
                df = fn(ann_date=ann_date)
            except Exception as exc:
                if "频率" in str(exc) or "每分钟" in str(exc):
                    time.sleep(2)
                    df = fn(ann_date=ann_date)
                else:
                    raise
            if df is not None and len(df):
                frames.append(df)
            if n % 100 == 0:
                print(f"[{name}] {n}/{len(days)}", flush=True)
            time.sleep(.31)
    else:
        months = pd.date_range(pd.Timestamp(start).to_period("M").to_timestamp(),
                               pd.Timestamp(end), freq="MS")
        for month in months:
            a = max(pd.Timestamp(start), month).strftime("%Y%m%d")
            b = min(pd.Timestamp(end), month + pd.offsets.MonthEnd(1)).strftime("%Y%m%d")
            df = fn(start_date=a, end_date=b)
            if df is not None and len(df):
                frames.append(df)
            time.sleep(.15)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(out) and "ann_date" in out:
        ann = out["ann_date"].astype(str)
        out = out[(ann >= start) & (ann <= end)]
    out.to_csv(path, index=False, compression="gzip")
    return out


def load_events(pro, cfg):
    start = cfg.start.replace("-", "")
    end = cfg.end.replace("-", "")
    repo = cached_query(pro, "repurchase", start, end, cfg.refresh)
    forecast = cached_query(pro, "forecast", start, end, cfg.refresh)
    express = cached_query(pro, "express", start, end, cfg.refresh)
    basic_path = CACHE / "stock_basic.csv.gz"
    if basic_path.exists() and not cfg.refresh:
        basic = pd.read_csv(basic_path, dtype=str)
    else:
        basic = pro.stock_basic(exchange="", list_status="L",
                                fields="ts_code,name,industry,list_date")
        basic.to_csv(basic_path, index=False, compression="gzip")
    return repo, forecast, express, basic


def positive_earnings(forecast, express):
    rows = []
    guidance = {}
    if len(forecast):
        f = forecast.copy()
        f["ann_dt"] = pd.to_datetime(f["ann_date"], format="%Y%m%d", errors="coerce")
        f = f.sort_values("ann_dt")
        f = f[f["type"].astype(str).isin(POSITIVE)]
        # 同一公司同一报告期：首次正向预告建立市场预期，后续重复预告不重复计分。
        first = f.drop_duplicates(["ts_code", "end_date"], keep="first")
        for _, r in first.iterrows():
            growth = r.get("p_change_min")
            rows.append({"ts_code": r["ts_code"], "ann_date": r.get("ann_date"),
                         "end_date": r.get("end_date"), "source": "forecast",
                         "type": r.get("type"), "growth": growth})
        # 最新预告上沿用于判断快报是否带来新增惊喜。
        latest = f.drop_duplicates(["ts_code", "end_date"], keep="last")
        for _, r in latest.iterrows():
            try:
                guidance[(r["ts_code"], str(r.get("end_date")))] = (
                    float(r.get("p_change_min")), float(r.get("p_change_max")), r["ann_dt"])
            except (TypeError, ValueError):
                pass
    if len(express):
        e = express.copy()
        growth_col = "yoy_net_profit" if "yoy_net_profit" in e.columns else "yoy_dedu_np"
        if growth_col in e:
            e[growth_col] = pd.to_numeric(e[growth_col], errors="coerce")
            e = e[e[growth_col] > 0]
        e["ann_dt"] = pd.to_datetime(e["ann_date"], format="%Y%m%d", errors="coerce")
        e = e.sort_values("ann_dt").drop_duplicates(["ts_code", "end_date"], keep="first")
        for _, r in e.iterrows():
            actual = r.get(growth_col) if growth_col in e else None
            prior = guidance.get((r["ts_code"], str(r.get("end_date"))))
            source, typ = "express_without_guidance", "快报增长"
            if prior and pd.notna(actual) and prior[2] < r["ann_dt"]:
                midpoint = (prior[0] + prior[1]) / 2
                if float(actual) < midpoint:
                    continue  # 低于预告中值：边际信息不足
                source, typ = "express_above_midpoint", "快报高于预告中值"
            rows.append({"ts_code": r["ts_code"], "ann_date": r.get("ann_date"),
                         "end_date": r.get("end_date"), "source": source, "type": typ,
                         "growth": actual,
                         "guidance_position": (
                             round((float(actual) - prior[0]) / max(1.0, prior[1] - prior[0]), 3)
                             if prior and pd.notna(actual) else None)})
    out = pd.DataFrame(rows)
    if len(out):
        out["ann_date"] = pd.to_datetime(out["ann_date"], format="%Y%m%d", errors="coerce")
        out = out.dropna(subset=["ann_date", "ts_code"])
    return out


def price_data(pro, code, start, end, refresh=False, index=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    key = code.replace(".", "_")
    path = CACHE / f"{'idx' if index else 'stock'}_{key}_{start}_{end}.csv.gz"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"trade_date": str})
    fn = pro.index_daily if index else pro.daily
    df = fn(ts_code=code, start_date=start, end_date=end)
    if df is None:
        df = pd.DataFrame()
    df.to_csv(path, index=False, compression="gzip")
    time.sleep(.12)
    return df


def forward_returns(pro, events, cfg):
    start = (pd.Timestamp(cfg.start) - pd.Timedelta(days=10)).strftime("%Y%m%d")
    end = (pd.Timestamp(cfg.end) + pd.Timedelta(days=100)).strftime("%Y%m%d")
    bench = price_data(pro, cfg.benchmark, start, end, cfg.refresh, index=True)
    bench = bench.sort_values("trade_date")
    bm = dict(zip(bench["trade_date"].astype(str), pd.to_numeric(bench["close"], errors="coerce")))
    horizons = [5, 10, 20, 60]
    results = []
    grouped = list(events.groupby("ts_code"))
    price_map = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(price_data, pro, code, start, end, cfg.refresh): code
                   for code, _ in grouped}
        for n, fut in enumerate(as_completed(futures), 1):
            code = futures[fut]
            try:
                price_map[code] = fut.result()
            except Exception as exc:
                print(f"[price skip] {code}: {exc}", flush=True)
            if n % 50 == 0:
                print(f"[prices] {n}/{len(grouped)}", flush=True)
    for code, group in grouped:
        px = price_map.get(code, pd.DataFrame())
        if px.empty:
            continue
        px = px.sort_values("trade_date").reset_index(drop=True)
        dates = px["trade_date"].astype(str).tolist()
        opens = pd.to_numeric(px["open"], errors="coerce").to_numpy()
        closes = pd.to_numeric(px["close"], errors="coerce").to_numpy()
        for _, ev in group.iterrows():
            ann = ev["ann_date"].strftime("%Y%m%d")
            entry_i = next((i for i, d in enumerate(dates) if d > ann), None)
            if entry_i is None or not np.isfinite(opens[entry_i]) or opens[entry_i] <= 0:
                continue
            rec = ev.to_dict()
            rec["ann_date"] = ev["ann_date"].strftime("%Y-%m-%d")
            rec["entry_date"] = dates[entry_i]
            for h in horizons:
                j = entry_i + h - 1
                if j >= len(dates) or not np.isfinite(closes[j]):
                    rec[f"excess_{h}"] = None
                    continue
                b0, bh = bm.get(dates[entry_i]), bm.get(dates[j])
                raw = closes[j] / opens[entry_i] - 1
                br = bh / b0 - 1 if b0 and bh else 0
                rec[f"excess_{h}"] = round((raw - br) * 100, 3)
            results.append(rec)
    return results


def summarize(rows, key):
    out = {}
    for h in (5, 10, 20, 60):
        vals = [r[f"excess_{h}"] for r in rows if r.get(f"excess_{h}") is not None]
        out[str(h)] = {
            "n": len(vals),
            "mean": round(float(np.mean(vals)), 3) if vals else None,
            "median": round(float(np.median(vals)), 3) if vals else None,
            "win_rate": round(float(np.mean(np.array(vals) > 0) * 100), 1) if vals else None,
        }
    return {"group": key, "horizons": out}


def compare_groups(resonance, ordinary, draws=2000):
    rng = np.random.default_rng(20260702)
    out = {}
    for h in (5, 10, 20, 60):
        a = np.array([r[f"excess_{h}"] for r in resonance if r.get(f"excess_{h}") is not None])
        b = np.array([r[f"excess_{h}"] for r in ordinary if r.get(f"excess_{h}") is not None])
        if not len(a) or not len(b):
            out[str(h)] = {}
            continue
        lifts = np.empty(draws)
        for i in range(draws):
            lifts[i] = rng.choice(a, len(a), replace=True).mean() - rng.choice(b, len(b), replace=True).mean()
        out[str(h)] = {
            "mean_lift": round(float(a.mean() - b.mean()), 3),
            "ci95": [round(float(np.quantile(lifts, .025)), 3),
                     round(float(np.quantile(lifts, .975)), 3)],
            "bootstrap_prob_positive": round(float(np.mean(lifts > 0) * 100), 1),
        }
    return out


def main():
    cfg = args()
    pro = api()
    repo, forecast, express, basic = load_events(pro, cfg)
    if repo.empty:
        raise SystemExit("Tushare repurchase 没有返回数据")
    meta = basic.set_index("ts_code")[["name", "industry"]].to_dict("index")
    earnings = positive_earnings(forecast, express)
    earnings["industry"] = earnings["ts_code"].map(lambda x: (meta.get(x) or {}).get("industry", ""))
    earnings = earnings[earnings["industry"] != ""]

    repo = repo.copy()
    if "proc" in repo:
        repo = repo[repo["proc"].astype(str) == "预案"]
    repo["ann_date"] = pd.to_datetime(repo["ann_date"], format="%Y%m%d", errors="coerce")
    repo = repo.dropna(subset=["ann_date", "ts_code"]).drop_duplicates(["ts_code", "ann_date"])
    repo["industry"] = repo["ts_code"].map(lambda x: (meta.get(x) or {}).get("industry", ""))
    event_rows = []
    for _, r in repo.iterrows():
        lo = r["ann_date"] - pd.Timedelta(days=cfg.lookback_days)
        peers = earnings[(earnings["industry"] == r["industry"])
                         & (earnings["ts_code"] != r["ts_code"])
                         & (earnings["ann_date"] >= lo)
                         & (earnings["ann_date"] <= r["ann_date"])]
        event_rows.append({
            "ts_code": r["ts_code"], "name": (meta.get(r["ts_code"]) or {}).get("name", ""),
            "industry": r["industry"], "ann_date": r["ann_date"],
            "resonance": bool(len(peers)), "peer_count": int(len(peers)),
            "peer_codes": ",".join(peers["ts_code"].astype(str).unique()[:5]),
        })
    events = pd.DataFrame(event_rows)
    # 首轮研究使用平衡样本，避免普通回购数量远大于共振组；固定种子确保可复现。
    events["event_year"] = events["ann_date"].dt.year
    rg = events[events["resonance"]]
    og = events[~events["resonance"]]
    cap = cfg.max_events_per_group
    if len(rg) > cap:
        rg = rg.sample(cap, random_state=20260702)
    # 普通回购按同年度+同行业匹配，减少行业行情和年份差异造成的伪增量。
    matched = []
    for (year, industry), group in rg.groupby(["event_year", "industry"]):
        pool = og[(og["event_year"] == year) & (og["industry"] == industry)]
        if len(pool):
            matched.append(pool.sample(min(len(group), len(pool)), random_state=20260702))
    og = pd.concat(matched, ignore_index=True).drop_duplicates(
        ["ts_code", "ann_date"]) if matched else og.iloc[:0]
    events = pd.concat([rg, og], ignore_index=True).drop(columns=["event_year"])
    rows = forward_returns(pro, events, cfg)
    resonance = [r for r in rows if r["resonance"]]
    ordinary = [r for r in rows if not r["resonance"]]
    by_year = []
    for year in sorted({r["ann_date"][:4] for r in rows}):
        yr = [r for r in rows if r["ann_date"].startswith(year)]
        by_year.append({"year": year,
                        "resonance": summarize([r for r in yr if r["resonance"]], "resonance"),
                        "ordinary": summarize([r for r in yr if not r["resonance"]], "ordinary")})
    result = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "period": [cfg.start, cfg.end], "lookback_days": cfg.lookback_days,
        "benchmark": cfg.benchmark,
        "timing_rule": "回购公告只匹配此前已披露的同行业正向forecast/express",
        "n_repo": len(rows), "n_resonance": len(resonance),
        "resonance": summarize(resonance, "resonance"),
        "ordinary": summarize(ordinary, "ordinary"),
        "comparison": compare_groups(resonance, ordinary),
        "by_year": by_year, "rows": rows,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"n_repo": len(rows), "n_resonance": len(resonance),
                      "resonance": result["resonance"],
                      "ordinary": result["ordinary"]}, ensure_ascii=False, indent=2))
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
