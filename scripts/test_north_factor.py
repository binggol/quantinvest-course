"""
①新数据因子腿验证之一: 北向持股变化因子 (hk_hold Δ占比)
假设: 北向(聪明钱)月度增持 → 次月超额。月频, 多空(顶quintile-底quintile) + 多头对冲流动性EW。
数据: tushare hk_hold(月末占比) + qlib 前复权收益。样本 2018-2026。
方法论同既有 sleeve: 月频, 夏普=mean/std*sqrt(12), 流动性过滤(防微盘噪声)。
"""
import os, io, sys
if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for k in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY']:
    os.environ.pop(k, None)
os.environ['no_proxy'] = '*'
import numpy as np, pandas as pd
import tushare as ts

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token


def main():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri="Z:/claude/qlib/data/cn_data", region="cn")
    pro = ts.pro_api(get_tushare_token())

    # 1) qlib 前复权收盘 (close*factor), 全市场, 2017-12 ~ 2026-06
    insts = D.instruments("all")
    df = D.features(insts, ["$close", "$factor"], start_time="2017-12-01", end_time="2026-06-30")
    df = df.dropna()
    adj = (df["$close"] * df["$factor"]).unstack(level=0)  # index=date, col=qlib code
    adj.index = pd.to_datetime(adj.index)
    adj.columns = [c.upper() for c in adj.columns]  # SH600519
    # 月末
    me = adj.resample("ME").last()
    # 成交额代理流动性: 用 close*成交量? 只有close; 改用 daily_basic circ? 这里用 qlib $close 不够 -> 用过去20日均价*?
    # 流动性: 用月度收益绝对可得的; 简单用 circ via tushare daily_basic 月末. 先用价格>0 & 非ST(代码层面无法), 用月末成交额(qlib无vol这里)替代 -> 改取 amount
    df2 = D.features(insts, ["$money"], start_time="2017-12-01", end_time="2026-06-30")  # 成交额(千元)
    amt = df2["$money"].unstack(level=0); amt.index = pd.to_datetime(amt.index)
    amt.columns = [c.upper() for c in amt.columns]
    amt_me = amt.resample("ME").mean()  # 月内日均成交额

    # qlib code -> tushare ts_code: SH600519 -> 600519.SH
    def to_ts(c):
        return c[2:] + "." + c[:2]
    q2ts = {c: to_ts(c) for c in me.columns}
    ts2q = {v: k for k, v in q2ts.items()}

    # 2) 各月末拉 hk_hold 占比
    me_dates = [d for d in me.index if pd.Timestamp("2018-01-01") <= d <= pd.Timestamp("2026-05-31")]
    cal_dates = list(adj.index)
    ratio_panel = {}
    for d in me_dates:
        # 找<=d的最近交易日
        td = max([x for x in cal_dates if x <= d])
        got = None
        for back in range(0, 6):
            ds = (td - pd.Timedelta(days=back)).strftime("%Y%m%d")
            try:
                h = pro.hk_hold(trade_date=ds, exchange='')
            except Exception:
                h = None
            if h is not None and len(h):
                got = h; break
        if got is None:
            continue
        s = got.dropna(subset=["ratio"]).groupby("ts_code")["ratio"].last()
        ratio_panel[d] = s
        print(f"  {d.date()} hk_hold {len(s)}", flush=True)
    R = pd.DataFrame(ratio_panel).T  # index=月末, col=ts_code
    R = R.sort_index()
    print("北向占比面板:", R.shape, flush=True)

    # 3) 因子: Δ占比 (本月末 - 上月末) 和 占比水平
    dR = R.diff()

    # 4) 次月收益(前复权), 对齐到 ts_code
    me_ts = me.rename(columns=q2ts)            # 月末前复权价 by ts_code
    fwd = me_ts.pct_change().shift(-1)         # 次月收益(t->t+1)对齐到 t行
    amt_ts = amt_me.rename(columns=q2ts)

    # 5) 月度多空 + 对冲
    def backtest(factor, name, q=0.2, liq_top=0.6):
        rows = []
        for d in factor.index:
            if d not in fwd.index:
                continue
            f = factor.loc[d].dropna()
            r = fwd.loc[d]
            a = amt_ts.loc[d] if d in amt_ts.index else None
            common = f.index.intersection(r.dropna().index)
            if a is not None:
                common = common.intersection(a.dropna().index)
            if len(common) < 100:
                continue
            f = f[common]; r = r[common]
            if a is not None:  # 流动性过滤: 取成交额前liq_top
                aa = a[common]
                keep = aa[aa >= aa.quantile(1 - liq_top)].index
                f = f[keep]; r = r[keep]
            if len(f) < 50:
                continue
            n = max(10, int(len(f) * q))
            hi = f.nlargest(n).index; lo = f.nsmallest(n).index
            ls = r[hi].mean() - r[lo].mean()
            # 多头对冲: top - 流动性池EW
            hedge = r[hi].mean() - r.mean()
            rows.append((d, ls, hedge, r[hi].mean(), r[lo].mean(), len(f)))
        res = pd.DataFrame(rows, columns=["date", "ls", "hedge", "top", "bot", "n"]).set_index("date")
        if len(res) < 6:
            print(f"[{name}] 样本不足 {len(res)}"); return res
        for col in ["ls", "hedge"]:
            s = res[col].dropna()
            shp = s.mean() / s.std() * np.sqrt(12)
            ann = s.mean() * 12 * 100
            wr = (s > 0).mean() * 100
            print(f"[{name}] {col:5s} 月数={len(s)} 年化={ann:+.2f}% 夏普={shp:.2f} 胜率={wr:.0f}% 月均={s.mean()*100:+.3f}%")
        # 年度
        yr = res["hedge"].groupby(res.index.year).apply(lambda x: (x.mean()/x.std()*np.sqrt(12)) if x.std() else np.nan)
        print(f"   [{name}] 年度对冲夏普: " + " ".join(f"{y}:{v:.2f}" for y, v in yr.items()))
        return res

    print("\n=== 北向Δ占比(增持) ===")
    res_d = backtest(dR, "Δ北向占比")
    print("\n=== 北向占比水平 ===")
    res_l = backtest(R, "北向占比水平")

    # 存腿 (Δ占比对冲序列) 供后续整合
    if len(res_d):
        import pickle
        out = res_d["hedge"].dropna()
        out.to_pickle(r"C:\rdagent\_sleeve_north.pkl")
        print(f"\n已存 _sleeve_north.pkl ({len(out)}月)")


if __name__ == "__main__":
    main()
