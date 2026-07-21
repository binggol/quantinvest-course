"""
催化剂版追涨检验: 在"近期涨幅最大"里, 只保留"财报刚超预期"的票, 看能否把裸追涨的负alpha翻正.

三组对比 (同一价格面板 + 同一基准):
  A 裸追涨        : 过去L日涨幅 Top50               (上个脚本已证显著为负)
  B 追涨∩超预期   : 过去L日涨幅 Top200 中, 近90天内有新公告且单季扣非同比>阈值
  C 纯超预期(PEAD): 近90天内有新公告且单季扣非同比>阈值 (不看涨幅, 对照)

催化剂数据: tushare fina_indicator_vip 批量拉全市场单季扣非(q_dtprofit)+公告日(ann_date).
单季扣非同比 = 本季 q_dtprofit / 去年同季 q_dtprofit - 1.
"""
import os
import math

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

import numpy as np
import pandas as pd

START, END = "2018-01-01", "2025-12-31"
PROVIDER = "Z:/claude/qlib/data/cn_data"
TOP_N = 50           # 最终持仓数(裸追涨) / catalyst组上限
TOP_POOL = 200       # 追涨∩超预期: 先取涨幅Top200做候选池
FRESH_DAYS = 90      # 公告新鲜窗口(自然日)
SURPRISE_THR = 30.0  # 单季扣非同比阈值(%)
GRID = [(20, 20), (20, 5), (5, 20), (60, 20)]


def load_price():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=PROVIDER, region="cn")
    df = D.features(D.instruments("all"), ["$close"], start_time=START, end_time=END, freq="day")
    close = df["$close"].unstack(level=0).sort_index()
    return close


def load_earnings():
    import tushare as ts
    ts.set_token(os.environ["TUSHARE_TOKEN"])
    pro = ts.pro_api()
    rows = []
    periods = [f"{y}{md}" for y in range(2017, 2027) for md in ("0331", "0630", "0930", "1231")]
    periods = [p for p in periods if p <= "20260331"]
    for p in periods:
        try:
            d = pro.fina_indicator_vip(period=p, fields="ts_code,ann_date,end_date,q_dtprofit")
            if d is not None and not d.empty:
                rows.append(d)
        except Exception as e:
            print(f"  vip {p} err: {e}")
    fin = pd.concat(rows, ignore_index=True)
    fin = fin.dropna(subset=["ann_date", "q_dtprofit"]).drop_duplicates(["ts_code", "end_date"])
    # 单季扣非同比
    q = {(r.ts_code, str(r.end_date)): r.q_dtprofit for r in fin.itertuples(index=False)}
    def yoy(r):
        be = f"{int(str(r.end_date)[:4]) - 1}{str(r.end_date)[4:]}"
        b = q.get((r.ts_code, be))
        if b is None or b <= 0 or pd.isna(b):
            return None
        return (r.q_dtprofit / b - 1) * 100
    fin["q_yoy"] = [yoy(r) for r in fin.itertuples(index=False)]
    fin = fin.dropna(subset=["q_yoy"])
    fin["ann_dt"] = pd.to_datetime(fin["ann_date"], format="%Y%m%d")
    # qlib 代码: 600519.SH -> SH600519
    def qc(c):
        return (c[-2:] + c[:6]).lower()  # qlib D.instruments('all') 列名为小写
    fin["qcode"] = fin["ts_code"].map(qc)
    return fin[["qcode", "ann_dt", "q_yoy"]].sort_values("ann_dt").reset_index(drop=True)


def fresh_surprise_set(fin, d, fresh_days, thr):
    """d 当日, 近 fresh_days 自然日内有公告且单季扣非同比>thr 的 qcode 集合(取每票最近一条)."""
    lo = d - pd.Timedelta(days=fresh_days)
    sub = fin[(fin["ann_dt"] > lo) & (fin["ann_dt"] <= d)]
    if sub.empty:
        return set()
    last = sub.groupby("qcode").tail(1)
    return set(last.loc[last["q_yoy"] > thr, "qcode"])


def stats(excess, H):
    excess = np.asarray(excess)
    n = len(excess)
    if n < 3:
        return None
    m, sd = excess.mean(), excess.std(ddof=1)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0
    py = 252.0 / H
    return dict(n=n, mean=m * 100, t=t, ann=m * py * 100,
                sharpe=(m / sd * math.sqrt(py)) if sd > 0 else 0,
                win=(excess > 0).mean() * 100)


def backtest(close, fin, L, H):
    dates = close.index
    idxs = list(range(max(L, 1), len(dates) - H, H))
    A, B, C, nB, nC = [], [], [], [], []
    for i in idxs:
        d = dates[i]
        past = close.iloc[i] / close.iloc[i - L] - 1.0
        fwd = close.iloc[i + H] / close.iloc[i] - 1.0
        valid = past.notna() & fwd.notna() & (close.iloc[i] > 0)
        v = valid[valid].index
        if len(v) < TOP_POOL:
            continue
        bench = fwd[v].mean()
        s = past[v].sort_values(ascending=False)
        # A 裸追涨 Top50
        A.append(fwd[s.head(TOP_N).index].mean() - bench)
        # 催化剂集合
        cat = fresh_surprise_set(fin, d, FRESH_DAYS, SURPRISE_THR)
        cat_v = [c for c in v if c in cat]
        # B 追涨∩超预期: 涨幅Top200 ∩ 催化剂
        pool = set(s.head(TOP_POOL).index)
        pick_b = [c for c in pool if c in cat]
        if pick_b:
            B.append(fwd[pick_b].mean() - bench); nB.append(len(pick_b))
        # C 纯超预期(全市场催化剂, 不看涨幅)
        if cat_v:
            C.append(fwd[cat_v].mean() - bench); nC.append(len(cat_v))
    return (stats(A, H), stats(B, H), stats(C, H),
            (np.mean(nB) if nB else 0), (np.mean(nC) if nC else 0))


def main():
    print("loading price panel ...")
    close = load_price()
    print(f"price: {close.shape[0]}d x {close.shape[1]} stk")
    print("loading earnings (vip) ...")
    fin = load_earnings()
    print(f"earnings rows: {len(fin)}  唯一票: {fin['qcode'].nunique()}\n")
    print(f"催化剂定义: 近{FRESH_DAYS}自然日内有公告 且 单季扣非同比 > {SURPRISE_THR}%\n")
    hdr = "%-10s %5s %8s %7s %8s %7s %6s"
    row = "%-10s %5d %8.3f %7.2f %8.2f %7.2f %6.1f"
    for L, H in GRID:
        a, b, c, navgB, navgC = backtest(close, fin, L, H)
        print(f"===== L{L}/H{H} =====")
        print(hdr % ("组", "n", "超额%/期", "t值", "年化%", "夏普", "胜率%"))
        if a: print(row % ("A裸追涨", a["n"], a["mean"], a["t"], a["ann"], a["sharpe"], a["win"]))
        if b: print((row % (f"B追涨∩超预期", b["n"], b["mean"], b["t"], b["ann"], b["sharpe"], b["win"])) + f"  (均选{navgB:.0f}只)")
        if c: print((row % ("C纯超预期PEAD", c["n"], c["mean"], c["t"], c["ann"], c["sharpe"], c["win"])) + f"  (均选{navgC:.0f}只)")
        print()
    print("注: 超额=组合-全市场等权基准. |t|>1.96=>95%显著.")


if __name__ == "__main__":
    main()
