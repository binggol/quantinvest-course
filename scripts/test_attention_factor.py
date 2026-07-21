"""
①新数据因子腿验证之二: 同花顺热榜"关注度反转" (ths_hot 热股)
假设: 散户高关注度=情绪透支 → 未来跑输市场(反转)。日频事件研究, 2024-2026。
对每个交易日的热股, 算未来5/20日 相对全市场EW 的超额。若显著为负=可做"负面剔除/反向"信号。
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

CACHE = r"Z:\claude\_ths_hot_2024_2026.pkl"


def pull_hot(pro, dates):
    if os.path.exists(CACHE):
        d = pd.read_pickle(CACHE)
        print(f"热榜缓存 {len(d)}日", flush=True)
        return d
    out = {}
    for i, ds in enumerate(dates):
        try:
            h = pro.ths_hot(trade_date=ds, data_type='热股')
            if h is not None and len(h):
                codes = [c for c in h['ts_code'].tolist() if isinstance(c, str) and c[:1].isdigit()]
                out[ds] = dict(zip(h['ts_code'], h['rank']))
        except Exception as e:
            pass
        if i % 50 == 0:
            print(f"  拉取 {i}/{len(dates)} {ds}", flush=True)
    pd.to_pickle(out, CACHE)
    print(f"已缓存热榜 {len(out)}日", flush=True)
    return out


def main():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri="Z:/claude/qlib/data/cn_data", region="cn")
    pro = ts.pro_api(get_tushare_token())

    # qlib 前复权日收盘, 2023-12 ~ 2026-06
    insts = D.instruments("all")
    df = D.features(insts, ["$close", "$factor"], start_time="2023-12-01", end_time="2026-06-30").dropna()
    adj = (df["$close"] * df["$factor"]).unstack(level=0)
    adj.index = pd.to_datetime(adj.index)
    adj.columns = [c.upper() for c in adj.columns]
    q2ts = {c: c[2:] + "." + c[:2] for c in adj.columns}
    adj_ts = adj.rename(columns=q2ts)  # 列=ts_code

    cal = list(adj.index)
    trade_dates = [d.strftime("%Y%m%d") for d in cal if d >= pd.Timestamp("2024-01-01")]
    hot = pull_hot(pro, trade_dates)

    # 全市场EW日收益(基准)
    ret1 = adj_ts.pct_change()
    mkt = ret1.mean(axis=1)  # EW市场

    # 前向收益: r[d -> d+h]
    def fwd_ret(d_idx, h):
        if d_idx + h >= len(cal):
            return None
        d0, d1 = cal[d_idx], cal[d_idx + h]
        return adj_ts.loc[d1] / adj_ts.loc[d0] - 1, mkt.iloc[d_idx + 1:d_idx + h + 1].sum()

    date2idx = {d.strftime("%Y%m%d"): i for i, d in enumerate(cal)}

    for h in [5, 10, 20]:
        exc_all, exc_top20, n_ev = [], [], 0
        for ds, ranks in hot.items():
            i = date2idx.get(ds)
            if i is None:
                continue
            fr = fwd_ret(i, h)
            if fr is None:
                continue
            r, mret = fr
            codes = [c for c in ranks if c in r.index and pd.notna(r[c])]
            if not codes:
                continue
            stk = r[codes].mean()
            exc_all.append(stk - mret)
            top = [c for c in codes if ranks[c] <= 20]
            if top:
                exc_top20.append(r[top].mean() - mret)
            n_ev += 1
        ea = np.array(exc_all) * 100
        et = np.array(exc_top20) * 100
        # t检验
        t_all = ea.mean() / (ea.std() / np.sqrt(len(ea))) if len(ea) else 0
        t_top = et.mean() / (et.std() / np.sqrt(len(et))) if len(et) else 0
        print(f"[{h:2d}日] 全热股 超额均值={ea.mean():+.3f}% t={t_all:+.1f} 胜率={(ea>0).mean()*100:.0f}% | "
              f"Top20 超额={et.mean():+.3f}% t={t_top:+.1f} (事件日={len(ea)})")

    # 月频: 进过热榜的股票 vs 没进过 (剔除信号检验)
    print("\n--- 月频: 月内进过热榜 vs 全市场 次月超额 ---")
    me = adj_ts.resample("ME").last()
    fwd_m = me.pct_change().shift(-1)
    rows = []
    for d in me.index:
        if d not in fwd_m.index:
            continue
        month_days = [ds for ds in hot if pd.Timestamp(ds[:4]+'-'+ds[4:6]+'-'+ds[6:]).to_period('M') == d.to_period('M')]
        hotset = set()
        for ds in month_days:
            hotset |= set(hot[ds].keys())
        r = fwd_m.loc[d].dropna()
        hotc = [c for c in hotset if c in r.index]
        if len(hotc) < 20 or len(r) < 100:
            continue
        rows.append((d, r[hotc].mean() - r.mean(), len(hotc)))
    res = pd.DataFrame(rows, columns=["date", "exc", "n"]).set_index("date")
    if len(res) >= 6:
        s = res["exc"].dropna()
        print(f"  月数={len(s)} 月均超额={s.mean()*100:+.3f}% 年化={s.mean()*12*100:+.2f}% "
              f"夏普={s.mean()/s.std()*np.sqrt(12):.2f} 胜率={(s>0).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
