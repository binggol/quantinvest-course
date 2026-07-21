"""
验证"楼梯择时 叠加 基本面选股"是否加分。
质量池 = 扣非增速≥20% 且 营收增速≥0 且 扣非增速≥营收增速(利润弹性) (按公告日生效, 季度更新)。
对比每期(等权, 持H日, 超额vs全市场等权):
  市场      : 全市场等权(基准0)
  Base质量池 : 买全部质量池
  Overlay   : 质量池 ∩ 当日楼梯形态(R²≥0.85+斜率>0+maxDD≥-12%+涨幅>5%)
问题: Overlay 超额 > Base 超额? (即楼梯择时在质量票上是否加分)
跑: D:/anaconda3/python.exe scripts/test_overlay.py
"""
import io, sys, os, math
if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'
import numpy as np
import pandas as pd
import tushare as ts

START, END = "2018-01-01", "2025-12-31"
PROVIDER = "Z:/claude/qlib/data/cn_data"
R2_THR, DD_THR, RET_MIN = 0.85, 0.12, 0.05
GRID = [(20, 10), (40, 20)]
tok_path = os.path.dirname(os.path.abspath(__file__)) + "/../data/.tushare_token"
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
    """每只票 [(ann_dt, dt_yoy, or_yoy)] 列表(按公告时间), qlib小写代码."""
    pro = ts.pro_api(TOK)
    rows = []
    for y in range(2017, 2026):
        for md in ("0331", "0630", "0930", "1231"):
            p = f"{y}{md}"
            if p > "20260331":
                continue
            try:
                d = pro.fina_indicator_vip(period=p, fields="ts_code,ann_date,or_yoy,dt_netprofit_yoy")
                if d is not None and len(d):
                    rows.append(d)
            except Exception:
                pass
    fin = pd.concat(rows, ignore_index=True).dropna(subset=["ann_date"])
    fin["ann_dt"] = pd.to_datetime(fin["ann_date"], format="%Y%m%d")
    fin["qc"] = fin["ts_code"].map(lambda c: (c[-2:] + c[:6]).lower())
    fin = fin.sort_values("ann_dt")
    return fin


def quality_pool(fin, d, cols):
    """d当日: 各票最近一次公告(ann≤d, 近200天内)且通过质量筛 的 qcode集合."""
    lo = d - pd.Timedelta(days=200)
    sub = fin[(fin["ann_dt"] > lo) & (fin["ann_dt"] <= d)]
    if sub.empty:
        return set()
    last = sub.groupby("qc").tail(1)
    ok = last[(last["dt_netprofit_yoy"] >= 20) & (last["or_yoy"] >= 0) &
              (last["dt_netprofit_yoy"] >= last["or_yoy"])]
    return set(ok["qc"]) & set(cols)


def col_r2_slope(logw):
    L = logw.shape[0]; x = np.arange(L, dtype=float); xc = x - x.mean()
    sxx = (xc ** 2).sum()
    yc = logw - np.nanmean(logw, axis=0)
    sxy = np.nansum(xc[:, None] * yc, axis=0); syy = np.nansum(yc ** 2, axis=0)
    slope = np.divide(sxy, sxx, out=np.zeros_like(sxy), where=sxx > 0)
    r2 = np.divide(sxy ** 2, sxx * syy, out=np.zeros_like(sxy), where=syy > 0)
    return r2, slope


def stat(ex, H):
    ex = np.asarray([x for x in ex if x == x]); n = len(ex)
    if n < 3: return None
    m, sd = ex.mean(), ex.std(ddof=1); py = 252.0 / H
    return dict(n=n, mean=m * 100, t=(m / (sd / math.sqrt(n)) if sd > 0 else 0),
                ann=m * py * 100, sharpe=(m / sd * math.sqrt(py) if sd > 0 else 0), win=(ex > 0).mean() * 100)


def main():
    print("loading panel..."); close, amount = load_panel()
    cols = list(close.columns); colset = set(cols)
    print(f"panel {close.shape[0]}d x {close.shape[1]}stk; loading fundamentals...")
    fin = load_quality()
    print(f"fundamentals rows {len(fin)}\n")
    cidx = {c: i for i, c in enumerate(cols)}
    cv = close.values; av = amount.values; dates = close.index
    for L, H in GRID:
        base, over, npb, npo = [], [], [], []
        for i in range(2 * L, len(dates) - H, H):
            d = dates[i]; c0 = cv[i]; fwd = cv[i + H] / c0 - 1.0; retL = c0 / cv[i - L] - 1.0
            win = cv[i - L + 1:i + 1]
            cummax = np.maximum.accumulate(win, axis=0); dd = np.nanmin(win / cummax - 1.0, axis=0)
            with np.errstate(invalid='ignore', divide='ignore'):
                r2, slope = col_r2_slope(np.log(win))
            valid = np.isfinite(c0) & np.isfinite(fwd) & (c0 > 0)
            amt = np.nanmean(av[i - L + 1:i + 1], axis=0)
            med = np.nanmedian(amt[valid]); valid = valid & (amt > med) & (c0 > 2)
            mkt = np.nanmean(fwd[valid])
            pool = quality_pool(fin, d, colset)
            pidx = [cidx[c] for c in pool if c in cidx and valid[cidx[c]]]
            if len(pidx) < 10:
                continue
            base.append(np.nanmean(fwd[pidx]) - mkt); npb.append(len(pidx))
            # overlay: 质量池 ∩ 楼梯形态
            stair = (r2 >= R2_THR) & (slope > 0) & (dd >= -DD_THR) & (retL > RET_MIN)
            oidx = [j for j in pidx if stair[j]]
            if len(oidx) >= 3:
                over.append(np.nanmean(fwd[oidx]) - mkt); npo.append(len(oidx))
        print(f"===== L{L}/H{H} =====")
        b, o = stat(base, H), stat(over, H)
        hdr = "%-12s %5s %9s %7s %8s %7s %6s"
        rw = "%-12s %5d %9.3f %7.2f %8.2f %7.2f %6.1f"
        print(hdr % ("组", "n期", "超额%/期", "t值", "年化%", "夏普", "胜率%"))
        if b: print((rw % ("Base质量池", b["n"], b["mean"], b["t"], b["ann"], b["sharpe"], b["win"])) + f"  (均{np.mean(npb):.0f}只)")
        if o: print((rw % ("Overlay楼梯", o["n"], o["mean"], o["t"], o["ann"], o["sharpe"], o["win"])) + f"  (均{np.mean(npo):.0f}只)")
        if b and o: print(f"  → 楼梯择时增量: 超额 {o['mean']-b['mean']:+.3f}%/期, 夏普 {o['sharpe']-b['sharpe']:+.2f}")
        print()
    print("注: 超额=组合−全市场等权(已流动性过滤). 质量池=扣非增速≥20%&营收≥0&扣非≥营收.")


if __name__ == "__main__":
    main()
