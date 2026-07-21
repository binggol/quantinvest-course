"""
楼梯形态策略回测: "走楼梯式"强趋势(平滑上升、回撤小)在启动时买入是否有效。

形态量化(回看L日):
  - 对数收盘价对时间线性回归 R² 高 = 价格紧贴一条上升直线(楼梯/平滑), 非锯齿
  - 斜率 slope > 0 = 上升
  - 窗口内最大回撤 maxDD 小 = 回撤幅度小
  - 区间收益 ret_L > 0
"启动"过滤: 前一窗口[t-2L,t-L]是底部(|收益|小), 即这段平滑上涨刚开始(不追已涨很久的)

对比:
  A 平滑趋势      : R²≥r2 & slope>0 & maxDD≥-dd & ret_L>0
  B 平滑趋势+启动 : A 且 前窗口为底部(用户原意: 启动时买)
  C 裸动量(对照)  : 仅 ret_L 排前(已知A股短期反转, 作基准)
持有H日, 等权, 超额=组合−全市场等权。
不用tushare, 纯qlib. 跑: D:/anaconda3/python.exe scripts/test_staircase.py
"""
import io, sys, math
if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd

START, END = "2018-01-01", "2025-12-31"
PROVIDER = "Z:/claude/qlib/data/cn_data"
R2_THR, DD_THR = 0.85, 0.12      # R²阈值, 最大回撤阈值(12%)
RET_MIN = 0.10                    # 区间最小涨幅
BASE_MAX = 0.08                   # 启动: 前窗口涨跌幅绝对值上限(底部)
TOPN = 50
GRID = [(20, 10), (40, 20), (60, 20), (40, 10)]   # (回看L, 持有H)


def load_panel():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=PROVIDER, region="cn")
    df = D.features(D.instruments("all"), ["$close", "$volume"], start_time=START, end_time=END, freq="day")
    close = df["$close"].unstack(level=0).sort_index()
    vol = df["$volume"].unstack(level=0).reindex(close.index)
    return close, close * vol


def stats(ex, H):
    ex = np.asarray([x for x in ex if x == x])
    n = len(ex)
    if n < 3:
        return None
    m, sd = ex.mean(), ex.std(ddof=1)
    py = 252.0 / H
    return dict(n=n, mean=m * 100, t=(m / (sd / math.sqrt(n)) if sd > 0 else 0),
                ann=m * py * 100, sharpe=(m / sd * math.sqrt(py) if sd > 0 else 0), win=(ex > 0).mean() * 100)


def col_r2_slope(logw):
    """logw: (L, Nstk) 对数价窗口. 返回每列 (r2, slope)."""
    L = logw.shape[0]
    x = np.arange(L, dtype=float)
    xm = x.mean()
    xc = x - xm
    sxx = (xc ** 2).sum()
    ym = np.nanmean(logw, axis=0)
    yc = logw - ym
    sxy = np.nansum(xc[:, None] * yc, axis=0)
    syy = np.nansum(yc ** 2, axis=0)
    slope = np.divide(sxy, sxx, out=np.zeros_like(sxy), where=sxx > 0)
    r2 = np.divide(sxy ** 2, sxx * syy, out=np.zeros_like(sxy), where=(syy > 0))
    return r2, slope


def backtest(close, amount, L, H, liq=True):
    dates = close.index
    idxs = list(range(2 * L, len(dates) - H, H))
    A, B, C = [], [], []
    nA, nB = [], []
    cvals = close.values
    for i in idxs:
        c0 = cvals[i]
        win = cvals[i - L + 1:i + 1]                       # (L, N)
        fwd = cvals[i + H] / c0 - 1.0
        retL = c0 / cvals[i - L] - 1.0
        # 最大回撤(窗口内)
        cummax = np.maximum.accumulate(win, axis=0)
        dd = np.nanmin(win / cummax - 1.0, axis=0)
        with np.errstate(invalid='ignore', divide='ignore'):
            r2, slope = col_r2_slope(np.log(win))
        retPrior = cvals[i - L] / cvals[i - 2 * L] - 1.0    # 前窗口(启动判定)
        valid = np.isfinite(c0) & np.isfinite(fwd) & (c0 > 0)
        if liq:
            amt = np.nanmean(amount.values[i - L + 1:i + 1], axis=0)
            med = np.nanmedian(amt[valid])
            valid = valid & (amt > med) & (c0 > 2)
        mkt = np.nanmean(fwd[valid])
        idxall = np.where(valid)[0]
        if len(idxall) < TOPN * 2:
            continue
        # A 平滑趋势
        a_mask = valid & (r2 >= R2_THR) & (slope > 0) & (dd >= -DD_THR) & (retL > RET_MIN)
        ai = np.where(a_mask)[0]
        if len(ai):
            A.append(np.nanmean(fwd[ai]) - mkt); nA.append(len(ai))
        # B 平滑趋势 + 启动(前窗口底部)
        b_mask = a_mask & (np.abs(retPrior) < BASE_MAX)
        bi = np.where(b_mask)[0]
        if len(bi):
            B.append(np.nanmean(fwd[bi]) - mkt); nB.append(len(bi))
        # C 裸动量 TopN
        order = idxall[np.argsort(retL[idxall])[::-1]][:TOPN]
        C.append(np.nanmean(fwd[order]) - mkt)
    return (stats(A, H), stats(B, H), stats(C, H), np.mean(nA) if nA else 0, np.mean(nB) if nB else 0)


def main():
    print("loading panel...")
    close, amount = load_panel()
    print(f"panel {close.shape[0]}d x {close.shape[1]}stk")
    print(f"形态: R²≥{R2_THR} 斜率>0 maxDD≥-{DD_THR} 涨幅>{RET_MIN*100:.0f}%; 启动: 前窗口|涨跌|<{BASE_MAX*100:.0f}%\n")
    hdr = "%-14s %5s %8s %7s %8s %7s %6s"
    row = "%-14s %5d %8.3f %7.2f %8.2f %7.2f %6.1f"
    for L, H in GRID:
        a, b, c, na, nb = backtest(close, amount, L, H)
        print(f"===== 回看L{L}/持有H{H} =====")
        print(hdr % ("组", "n期", "超额%/期", "t值", "年化%", "夏普", "胜率%"))
        if a: print((row % ("A平滑趋势", a["n"], a["mean"], a["t"], a["ann"], a["sharpe"], a["win"])) + f"  (均选{na:.0f})")
        if b: print((row % ("B平滑+启动", b["n"], b["mean"], b["t"], b["ann"], b["sharpe"], b["win"])) + f"  (均选{nb:.0f})")
        if c: print(row % ("C裸动量Top", c["n"], c["mean"], c["t"], c["ann"], c["sharpe"], c["win"]))
        print()
    print("注: 超额=组合−全市场等权(已流动性过滤). |t|>1.96=>95%显著.")


if __name__ == "__main__":
    main()
