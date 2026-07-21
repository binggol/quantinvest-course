"""
检验「每隔一段时间, 买入近期涨幅最大的股票」能否提高收益.
本质 = A股短期动量 vs 反转检验.

方法:
- 全市场 qlib 收盘价 (前复权).
- 网格: 回看窗口 L (按此排涨幅) x 持有窗口 H.
- 每隔 H 个交易日调仓: 按过去 L 日涨幅排序, 取 Top N (及对照: 涨幅最大的反向=买跌幅最大者).
- 组合 = 等权, 持有 H 日; 基准 = 当期全市场等权平均.
- 超额 = 组合 - 基准; 统计 均值/t值/年化/夏普/胜率.
- 两版: 不过滤 vs 流动性过滤(过去L日成交额>全市场中位数, 价>2, 剔除涨停封板买不进的近似).

不用 tushare, 纯 qlib, 多进程安全 (无模块级副作用).
"""
import math

import numpy as np
import pandas as pd

START, END = "2018-01-01", "2025-12-31"
PROVIDER = "Z:/claude/qlib/data/cn_data"

GRID = [(5, 5), (10, 10), (20, 20), (5, 20), (20, 5), (60, 20)]  # (回看L, 持有H)
TOP_N = 50


def load_panel():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=PROVIDER, region="cn")
    insts = D.instruments("all")
    df = D.features(insts, ["$close", "$volume"], start_time=START, end_time=END, freq="day")
    if df.empty:
        raise RuntimeError("qlib features empty")
    close = df["$close"].unstack(level=0)   # index=date, cols=stock
    vol = df["$volume"].unstack(level=0)
    close = close.sort_index()
    vol = vol.reindex(close.index)
    amount = close * vol                     # 成交额近似 (前复权价*量)
    return close, amount


def run_strategy(close, amount, L, H, top_n=TOP_N, liq_filter=False, side="winners"):
    dates = close.index
    rebals = list(range(L, len(dates) - H, H))
    rows = []
    for i in rebals:
        d, dprev, dfwd = dates[i], dates[i - L], dates[i + H]
        c0 = close.iloc[i]
        past = close.iloc[i] / close.iloc[i - L] - 1.0       # 过去L日涨幅
        fwd = close.iloc[i + H] / close.iloc[i] - 1.0        # 未来H日收益
        valid = past.notna() & fwd.notna() & (c0 > 0)
        if liq_filter:
            amt = amount.iloc[i - L + 1:i + 1].mean()         # 过去L日平均成交额
            med = amt[valid].median()
            valid = valid & (amt > med) & (c0 > 2)
        v = valid[valid].index
        if len(v) < top_n * 2:
            continue
        s = past[v].sort_values(ascending=False)
        pick = (s.head(top_n) if side == "winners" else s.tail(top_n)).index
        port = fwd[pick].mean()
        bench = fwd[v].mean()                                  # 全市场等权基准
        rows.append((port, bench, port - bench))
    if not rows:
        return None
    arr = np.array(rows)
    excess = arr[:, 2]
    n = len(excess)
    m = excess.mean()
    sd = excess.std(ddof=1)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    py = 252.0 / H                                             # 每年调仓次数
    ann = m * py
    sharpe = (m / sd) * math.sqrt(py) if sd > 0 else 0.0
    win = (excess > 0).mean() * 100
    raw_ann = arr[:, 0].mean() * py                           # 组合绝对年化
    bench_ann = arr[:, 1].mean() * py
    return dict(n=n, mean=m * 100, t=t, ann=ann * 100, sharpe=sharpe, win=win,
                raw_ann=raw_ann * 100, bench_ann=bench_ann * 100)


def main():
    print("loading qlib full-market panel ...")
    close, amount = load_panel()
    print(f"panel: {close.shape[0]} 交易日 x {close.shape[1]} 只股票\n")

    for liq in (False, True):
        tag = "流动性过滤(成交额>中位/价>2)" if liq else "不过滤(含小盘/低价)"
        print(f"\n########## 买入近期涨幅最大 Top{TOP_N}  [{tag}] ##########")
        print("%-12s %5s %8s %7s %9s %8s %6s %9s %9s" %
              ("回看/持有", "n", "超额%/期", "t值", "超额年化%", "夏普", "胜率%", "组合年化%", "基准年化%"))
        for L, H in GRID:
            r = run_strategy(close, amount, L, H, liq_filter=liq, side="winners")
            if r is None:
                print(f"L{L}/H{H}: 样本不足"); continue
            print("L%-3d/H%-4d %6d %8.3f %7.2f %9.2f %8.2f %6.1f %9.1f %9.1f" %
                  (L, H, r["n"], r["mean"], r["t"], r["ann"], r["sharpe"], r["win"], r["raw_ann"], r["bench_ann"]))

    # 反转对照: 买跌幅最大者 (不过滤)
    print(f"\n########## 对照: 买入近期跌幅最大 Top{TOP_N} (反转, 不过滤) ##########")
    print("%-12s %5s %8s %7s %9s %8s %6s" % ("回看/持有", "n", "超额%/期", "t值", "超额年化%", "夏普", "胜率%"))
    for L, H in GRID:
        r = run_strategy(close, amount, L, H, liq_filter=False, side="losers")
        if r is None:
            print(f"L{L}/H{H}: 样本不足"); continue
        print("L%-3d/H%-4d %6d %8.3f %7.2f %9.2f %8.2f %6.1f" %
              (L, H, r["n"], r["mean"], r["t"], r["ann"], r["sharpe"], r["win"]))
    print("\n注: 超额=组合-全市场等权基准. |t|>1.96=>95%显著. 夏普为超额年化夏普.")


if __name__ == "__main__":
    main()
