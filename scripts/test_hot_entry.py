"""
热榜"入榜买入 / 掉榜卖出"事件策略检验 (ths_hot 热股, 2024-2026)。
假设(与"在榜反转"不同): 抓【首次入榜】的关注度脉冲, 在榜期间持有, 【掉榜】(连续在榜天数不再增长)即卖。
对每次入榜事件算: 持有期个股收益 vs 同期全市场EW收益 -> 超额。聚合 t检验/胜率/年化。
执行两种口径:
  T0(乐观): 首次上榜当日收盘买, 掉榜当日收盘卖 (用到当日榜单, 偏乐观);
  T1(现实): 上榜次日收盘买, 掉榜后一日收盘卖 (无前视)。
对照: 同一批入榜事件改用"固定持有5日", 看"掉榜才走"是否更优。
"""
import os, io, sys
if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for k in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY']:
    os.environ.pop(k, None)
os.environ['no_proxy'] = '*'
import numpy as np, pandas as pd

CACHE = r"Z:\claude\_ths_hot_2024_2026.pkl"
RT_COST = 0.002  # 单边千二(双边千四), 散户热门股冲击成本可能更高


def main():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri="Z:/claude/qlib/data/cn_data", region="cn")

    insts = D.instruments("all")
    df = D.features(insts, ["$close", "$factor"], start_time="2023-12-01", end_time="2026-06-30").dropna()
    adj = (df["$close"] * df["$factor"]).unstack(level=0)
    adj.index = pd.to_datetime(adj.index)
    adj.columns = [c.upper() for c in adj.columns]
    q2ts = {c: c[2:] + "." + c[:2] for c in adj.columns}
    adj_ts = adj.rename(columns=q2ts)  # 列=ts_code

    cal = list(adj.index)
    date2idx = {d.strftime("%Y%m%d"): i for i, d in enumerate(cal)}
    ret1 = adj_ts.pct_change()
    mkt = ret1.mean(axis=1)  # 全市场EW日收益(基准)

    if not os.path.exists(CACHE):
        print("无热榜缓存, 先跑 test_attention_factor.py 生成"); return
    hot = pd.read_pickle(CACHE)

    # 只用"既在热榜缓存、又在价格日历"里的日期, 按时间排序 -> 连续性按此序列定义(避开拉取缺失日噪声)
    hot_dates = sorted([ds for ds in hot if ds in date2idx], key=lambda x: date2idx[x])
    hd_idx = {ds: k for k, ds in enumerate(hot_dates)}  # 在热榜序列中的序号
    print(f"热榜有效交易日={len(hot_dates)} ({hot_dates[0]}~{hot_dates[-1]})", flush=True)

    # 每只股票的入榜事件: 在 hot_dates[k] 在榜 且 hot_dates[k-1] 不在榜
    # 在榜集合
    onset = {ds: set(hot[ds].keys()) for ds in hot_dates}

    events = []  # (code, enter_ds, last_on_ds, n_on_days, enter_rank)
    all_codes = set().union(*[onset[ds] for ds in hot_dates])
    for code in all_codes:
        if code not in adj_ts.columns:
            continue  # 非A股/期货撞码/无价格
        on_flags = [code in onset[ds] for ds in hot_dates]
        k = 0
        n = len(hot_dates)
        while k < n:
            if on_flags[k] and (k == 0 or not on_flags[k-1]):
                # 入榜: 找连续在榜的最后一天
                j = k
                while j + 1 < n and on_flags[j+1]:
                    j += 1
                enter_ds, last_ds = hot_dates[k], hot_dates[j]
                events.append((code, enter_ds, last_ds, j - k + 1, hot[enter_ds].get(code, 99)))
                k = j + 1
            else:
                k += 1
    print(f"入榜事件总数={len(events)} (覆盖{len(set(e[0] for e in events))}只股)", flush=True)

    def trade_ret(enter_ds, last_ds, off_shift_buy, off_shift_sell):
        # 买入索引 = enter在全日历的idx + off_shift_buy; 卖出索引 = last的idx + off_shift_sell
        bi = date2idx[enter_ds] + off_shift_buy
        si = date2idx[last_ds] + off_shift_sell
        if bi >= len(cal) or si >= len(cal) or si <= bi:
            return None
        return bi, si

    def run(off_buy, off_sell, label, fixed_hold=None):
        exc, raw, mret_l, holds, ranks = [], [], [], [], []
        for code, eds, lds, n_on, rk in events:
            tr = trade_ret(eds, lds, off_buy, off_sell)
            if tr is None:
                continue
            bi, si = tr
            if fixed_hold is not None:
                si = bi + fixed_hold
                if si >= len(cal):
                    continue
            pser = adj_ts[code]
            pb, ps = pser.iloc[bi], pser.iloc[si]
            if pd.isna(pb) or pd.isna(ps) or pb <= 0:
                continue
            r = ps / pb - 1 - 2 * RT_COST  # 双边成本
            m = mkt.iloc[bi+1:si+1].sum()    # 同期市场EW
            exc.append(r - m); raw.append(r); mret_l.append(m)
            holds.append(si - bi); ranks.append(rk)
        exc = np.array(exc) * 100; raw = np.array(raw) * 100; mr = np.array(mret_l) * 100
        if len(exc) < 10:
            print(f"  {label}: 样本不足({len(exc)})"); return
        t = exc.mean() / (exc.std() / np.sqrt(len(exc)))
        ah = np.mean(holds)
        # 年化超额(按平均持有天数, 252交易日): 复利近似用算术
        ann = exc.mean()/100 * (252/ah) * 100 if ah > 0 else 0
        print(f"  {label}: 事件={len(exc)} 平均持有={ah:.1f}日 | "
              f"个股收益={raw.mean():+.2f}% 市场={mr.mean():+.2f}% 超额={exc.mean():+.3f}% "
              f"t={t:+.1f} 胜率={(exc>0).mean()*100:.0f}% 年化超额≈{ann:+.1f}%")
        # 分入榜名次
        rk = np.array(ranks)
        for lo, hi, nm in [(1,10,"入榜Top10"),(11,30,"11-30名"),(31,200,"31名外")]:
            m_ = (rk>=lo)&(rk<=hi)
            if m_.sum() >= 10:
                e_ = exc[m_]; t_ = e_.mean()/(e_.std()/np.sqrt(len(e_)))
                print(f"      {nm}: n={m_.sum()} 超额={e_.mean():+.3f}% t={t_:+.1f} 胜率={(e_>0).mean()*100:.0f}%")

    print("\n=== 策略A: 入榜买 / 掉榜卖 (持有=连续在榜天数) ===")
    print("[T0 乐观: 上榜当日收盘买, 掉榜当日收盘卖]")
    run(0, 1, "T0")   # 买=入榜当日(off0), 卖=最后在榜日的下一日即掉榜日(off+1)
    print("[T1 现实: 上榜次日买, 掉榜后一日卖 (无前视)]")
    run(1, 2, "T1")   # 买=入榜次日(off+1), 卖=掉榜日的再下一日(off+2)

    print("\n=== 对照: 同样入榜买入, 但固定持有N日(看'掉榜才走'是否更优) ===")
    for hh in [3, 5, 10]:
        run(1, 2, f"T1+固定持有{hh}日", fixed_hold=hh)

    # 持有天数分布
    alln = [e[3] for e in events]
    print(f"\n连续在榜天数分布: 中位={np.median(alln):.0f} 均值={np.mean(alln):.1f} "
          f"占比[1日={np.mean(np.array(alln)==1)*100:.0f}% ≤2日={np.mean(np.array(alln)<=2)*100:.0f}% ≥5日={np.mean(np.array(alln)>=5)*100:.0f}%]")
    print(f"\n口径: 双边成本{2*RT_COST*100:.1f}%; 基准=全市场EW; 样本2024-2026(单区间,约2.5年)。")


if __name__ == "__main__":
    main()
