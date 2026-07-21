"""
②Numerai特征中性化 验证: 对生产因子 dgm(毛利率YoY改善) 做风格正交化。
把 dgm 对 [size(logADV), 动量(12-1月), 波动(60日)] 截面回归, 取残差=纯净因子。
对比 原始 vs 中性化 的多空夏普 + 因子与风格的相关。判读: 残差夏普≈原始→纯alpha; 大幅掉→风格代理。
复用 _gpm_q.pkl + C:/qlib_data/cn_data。带__main__保护。
"""
import os, pickle, warnings, io, sys
from multiprocessing import freeze_support
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd


def ts2q(c):
    n, mk = c.split("."); return ("sh" if mk == "SH" else "sz") + n


def zsc(s):
    s = s.astype(float)
    return (s - s.mean()) / (s.std() + 1e-9)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import qlib
    from qlib.data import D
    fi = pickle.load(open(r"C:\rdagent\_gpm_q.pkl", "rb"))
    fi = fi.dropna(subset=["ann_date", "end_date", "grossprofit_margin"]).copy()
    fi["gm"] = pd.to_numeric(fi["grossprofit_margin"], errors="coerce").clip(-50, 100)
    fi["ann"] = fi["ann_date"].astype(str); fi["end"] = fi["end_date"].astype(str)
    fi = fi.dropna(subset=["gm"]).sort_values(["ts_code", "end"]).reset_index(drop=True)
    fi["gm4"] = fi.groupby("ts_code")["gm"].shift(4); fi["dgm"] = fi["gm"] - fi["gm4"]
    fi = fi.dropna(subset=["dgm"])

    qlib.init(provider_uri=r"C:/qlib_data/cn_data", region="cn")
    uni = sorted(fi["ts_code"].unique()); uq = [ts2q(c) for c in uni]
    feat = D.features(uq, ["$close", "$volume"], start_time="2017-06-01", freq="day")
    close = feat["$close"].unstack(level="instrument").sort_index()
    vol = feat["$volume"].unstack(level="instrument").sort_index()
    adv = (close * vol).rolling(20).mean()
    dret = close.pct_change()
    vol60 = dret.rolling(60).std()
    for X in (close, adv, vol60): X.index = [str(d)[:10] for d in X.index]
    q2ts = {ts2q(c): c for c in uni}
    for X in (close, adv, vol60):
        X.columns = [q2ts.get(c, c) for c in X.columns]

    me = []; idx = list(close.index)
    for i, d in enumerate(idx):
        if i + 1 >= len(idx) or d[:7] != idx[i + 1][:7]:
            me.append(d)
    Cm = close.loc[me]; ADVm = adv.loc[me]; VOLm = vol60.loc[me]
    fwd = Cm.shift(-1) / Cm - 1
    mom = Cm.shift(1) / Cm.shift(12) - 1  # 12-1月动量

    def fac_asof(d):
        sub = fi[fi["ann"] <= d.replace("-", "")]
        if not len(sub):
            return None
        return sub.groupby("ts_code").tail(1).set_index("ts_code")["dgm"]

    def st(s):
        return 0.0 if len(s) < 6 or s.std() == 0 else s.mean() / s.std() * np.sqrt(12)

    raw_ls, neu_ls = [], []
    corr_size, corr_mom, corr_vol, r2s = [], [], [], []
    for i in range(len(me) - 1):
        d = me[i]; fa = fac_asof(d)
        if fa is None:
            continue
        a = ADVm.loc[d].dropna()
        pool = set(a.sort_values().index[-int(len(a) * 0.6):])  # 流动性top60%
        fr = fwd.loc[d].dropna()
        size = np.log(ADVm.loc[d].replace(0, np.nan))
        mm = mom.loc[d]; vv = VOLm.loc[d]
        df = pd.DataFrame({"f": fa, "size": size, "mom": mm, "vol": vv}).dropna()
        df = df[df.index.isin(pool) & df.index.isin(fr.index)]
        if len(df) < 50:
            continue
        f = zsc(df["f"]); S = zsc(df["size"]); M = zsc(df["mom"]); V = zsc(df["vol"])
        F = np.column_stack([np.ones(len(df)), S, M, V])
        beta, *_ = np.linalg.lstsq(F, f.values, rcond=None)
        pred = F @ beta
        resid = f.values - pred
        ss_tot = ((f.values - f.values.mean()) ** 2).sum()
        r2s.append(1 - (resid ** 2).sum() / (ss_tot + 1e-9))
        corr_size.append(np.corrcoef(f, S)[0, 1]); corr_mom.append(np.corrcoef(f, M)[0, 1]); corr_vol.append(np.corrcoef(f, V)[0, 1])
        rser = pd.Series(resid, index=df.index)
        d1 = me[i + 1]
        for src, store in [(df["f"], raw_ls), (rser, neu_ls)]:
            sc = src.sort_values(); k = max(10, len(sc) // 10)
            top = sc.index[-k:]; bot = sc.index[:k]
            store.append((d1, fr.reindex(top).mean() - fr.reindex(bot).mean()))
    rs = pd.Series(dict(raw_ls)); ns = pd.Series(dict(neu_ls))

    def seg(s):
        W = {"19-21": ("2019", "2021"), "22-23": ("2022", "2023"), "24-26": ("2024", "2026")}
        return ' '.join(f"{w}:{st(s[[d for d in s.index if a<=d[:4]<=b]]):.2f}" for w, (a, b) in W.items())

    print(f"中性化前 R²(因子被风格解释比例) 均值={np.mean(r2s)*100:.1f}%")
    print(f"因子vs风格 平均相关: size={np.mean(corr_size):+.3f} 动量={np.mean(corr_mom):+.3f} 波动={np.mean(corr_vol):+.3f}")
    print(f"\n原始 dgm 多空十分位:   夏普={st(rs):.2f} 年化={rs.mean()*12*100:+.2f}% | {seg(rs)}")
    print(f"中性化 dgm 多空十分位: 夏普={st(ns):.2f} 年化={ns.mean()*12*100:+.2f}% | {seg(ns)}")
    print(f"\n判读: 若中性化夏普≈原始→毛利率改善是真纯alpha(不靠size/动量/波动); 若大幅下降→部分是风格代理。")


if __name__ == "__main__":
    freeze_support(); main()
