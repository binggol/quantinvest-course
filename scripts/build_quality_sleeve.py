r"""
质量因子腿(候选第6腿)对冲日收益序列: 多(扣非增速≥20%&营收≥0&扣非≥营收 质量池, 等权) − 空(全市场等权)。
口径与其它腿一致: 全日历日频对冲。月初按当时已公告的财务定池, 持有当月。
输出 data/sleeve_quality.json (+ C:\rdagent\_sleeve_quality.pkl 供 export_combo)。
跑: D:/anaconda3/python.exe scripts/build_quality_sleeve.py
"""
import io, sys, os, json, math, pickle
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'
import numpy as np
import pandas as pd
import tushare as ts

START, END = "2018-01-01", "2025-12-31"
PROVIDER = "Z:/claude/qlib/data/cn_data"
DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA, "sleeve_quality.json")
tok_path = os.path.join(DATA, ".tushare_token")
TOK = open(tok_path).read().strip() if os.path.exists(tok_path) else os.environ.get("TUSHARE_TOKEN", "")


def load_panel():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=PROVIDER, region="cn")
    df = D.features(D.instruments("all"), ["$close", "$volume"], start_time=START, end_time=END, freq="day")
    close = df["$close"].unstack(level=0).sort_index()
    vol = df["$volume"].unstack(level=0).reindex(close.index)
    return close, close * vol


def load_quality():
    pro = ts.pro_api(TOK)
    rows = []
    for y in range(2017, 2026):
        for md in ("0331", "0630", "0930", "1231"):
            p = f"{y}{md}"
            try:
                d = pro.fina_indicator_vip(period=p, fields="ts_code,ann_date,or_yoy,dt_netprofit_yoy")
                if d is not None and len(d):
                    rows.append(d)
            except Exception:
                pass
    fin = pd.concat(rows, ignore_index=True).dropna(subset=["ann_date"])
    fin["ann_dt"] = pd.to_datetime(fin["ann_date"], format="%Y%m%d")
    fin["qc"] = fin["ts_code"].map(lambda c: (c[-2:] + c[:6]).lower())
    return fin.sort_values("ann_dt")


def pool_at(fin, d, colset):
    lo = d - pd.Timedelta(days=200)
    sub = fin[(fin["ann_dt"] > lo) & (fin["ann_dt"] <= d)]
    if sub.empty:
        return set()
    last = sub.groupby("qc").tail(1)
    ok = last[(last["dt_netprofit_yoy"] >= 20) & (last["or_yoy"] >= 0) & (last["dt_netprofit_yoy"] >= last["or_yoy"])]
    return set(ok["qc"]) & colset


def main():
    print("loading panel..."); close, amount = load_panel()
    colset = set(close.columns)
    print(f"panel {close.shape[0]}d x {close.shape[1]}stk; fundamentals..."); fin = load_quality()
    ret = close.pct_change()
    dates = close.index
    # 月初定池, 持有当月
    month_first = {}
    for i, d in enumerate(dates):
        key = (d.year, d.month)
        if key not in month_first:
            month_first[key] = i
    excess = pd.Series(0.0, index=dates)
    liq_idx = pd.Index([]); pool_idx = pd.Index([])
    for i in range(1, len(dates)):
        d = dates[i]
        if i == month_first.get((d.year, d.month)):
            # 月初定池(用上一交易日避免未来函数). 流动性universe = 前20日均额>中位 & 价>2
            amt = amount.iloc[max(0, i - 20):i].mean()
            med = amt[amt > 0].median()
            px = close.iloc[i - 1]
            liq_idx = amt.index[(amt > med) & (px.reindex(amt.index) > 2)]
            pset = pool_at(fin, dates[i - 1], colset)
            pool_idx = pd.Index([c for c in liq_idx if c in pset])
        rr = ret.iloc[i]
        mkt = rr.reindex(liq_idx).mean()       # 基准 = 流动性过滤后全市场等权(与overlay一致)
        if len(pool_idx) and pd.notna(mkt):
            pr = rr.reindex(pool_idx).mean()
            if pd.notna(pr):
                excess.iloc[i] = pr - mkt
    s = excess[excess.index >= "2018-02-01"]
    sd = s.std(ddof=1)
    sharpe = s.mean() / sd * math.sqrt(252) if sd > 0 else 0
    ann = s.mean() * 252
    print(f"\n质量腿 对冲(多质量池−空市场): 年化={ann*100:.2f}% 夏普={sharpe:.2f} 日数={len(s)}")
    daily = {d.strftime("%Y-%m-%d"): round(float(v), 6) for d, v in s.items() if v != 0}
    json.dump({"updated": "rebuilt", "method": "多(扣非≥20&营收≥0&扣非≥营收 质量池等权)−空(全市场等权), 月初定池, 全日历",
               "full_calendar": {"sharpe": round(sharpe, 3), "ann": round(ann, 4), "vol": round(sd * math.sqrt(252), 4)},
               "daily": daily}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"saved {OUT}")
    try:
        pickle.dump(daily, open(r"C:\rdagent\_sleeve_quality.pkl", "wb")); print("saved C:/rdagent/_sleeve_quality.pkl")
    except Exception as e:
        print("pkl skip", e)


if __name__ == "__main__":
    main()
