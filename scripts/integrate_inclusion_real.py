"""
用真实序列把"纳入抢跑腿"作为第5腿并入Pro四腿组合, 算实测相关性 + 合并夏普增量.
口径完全照搬 C:\\rdagent\\export_combo.py (各腿->月度, 60/10/10/20, 月度夏普*sqrt(12)).

输入:
  C:\\rdagent\\_sleeve_300.pkl / _sleeve_runup.pkl / _sleeve_pead.pkl / _sleeve_repo.pkl  (现有4腿)
  data/sleeve_inclusion.json  (本仓 build_inclusion_sleeve.py 产出的纳入腿日序列)
"""
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

RD = Path(r"C:\rdagent")
INCL = Path(__file__).parent.parent / "data" / "sleeve_inclusion.json"


def dcurve(P):
    s = {}
    for x in P:
        dd = pd.bdate_range(x["de"], x["dx"])
        if len(dd) <= 1:
            continue
        rt = (1 + x["net"]) ** (1.0 / (len(dd) - 1)) - 1
        for t in dd[1:]:
            s[t] = rt
    return (1 + pd.Series(s).sort_index()).resample("ME").prod() - 1


def from_daily_series(s):
    s = pd.Series(s)
    s.index = pd.to_datetime(s.index)
    return (1 + s).resample("ME").prod() - 1


legs = {}
try:
    legs["主300"] = dcurve(pickle.load(open(RD / "_sleeve_300.pkl", "rb")))
except Exception as e:
    print("主300 load fail:", e)
try:
    ru = pickle.load(open(RD / "_sleeve_runup.pkl", "rb"))
    rs = pd.Series({x["d"]: x["net"] for x in ru}); rs.index = pd.to_datetime(rs.index)
    legs["抢跑"] = (1 + rs).resample("ME").prod() - 1
except Exception as e:
    print("抢跑 load fail:", e)
try:
    legs["PEAD"] = dcurve(pickle.load(open(RD / "_sleeve_pead.pkl", "rb")))
except Exception as e:
    print("PEAD load fail:", e)
try:
    legs["回购"] = from_daily_series(pickle.load(open(RD / "_sleeve_repo.pkl", "rb")))
except Exception as e:
    print("回购 load fail:", e)

# 纳入腿: 本仓日序列 -> 月度
incl = json.loads(INCL.read_text(encoding="utf-8"))
legs["纳入"] = from_daily_series(incl["daily"])

sh = lambda x: float(x.mean() / x.std() * np.sqrt(12)) if len(x) > 3 and x.std() > 0 else 0
ann = lambda x: float((1 + x).prod() ** (12.0 / len(x)) - 1) if len(x) > 3 else 0

# 各腿单独统计(各自全样本)
print("=== 各腿(全样本月度) ===")
for k, v in legs.items():
    print(f"  {k:5} 月数={len(v):3d} 夏普={sh(v):.2f} 年化={ann(v)*100:5.1f}%")

four = ["主300", "抢跑", "PEAD", "回购"]
W4 = {"主300": 0.60, "抢跑": 0.10, "PEAD": 0.10, "回购": 0.20}

# 复现4腿组合(共同月)
df4 = pd.concat([legs[k].rename(k) for k in four if k in legs], axis=1).dropna()
comb4 = sum(W4[k] * df4[k] for k in four)
print(f"\n=== 复现4腿组合 (共同月{len(df4)}) 合并夏普={sh(comb4):.2f} 年化={ann(comb4)*100:.1f}% (标称2.34) ===")

# 5腿: 实测相关
df5 = pd.concat([legs[k].rename(k) for k in (four + ["纳入"])], axis=1).dropna()
print(f"\n=== 5腿共同月={len(df5)} ===")
print("纳入腿 vs 现有4腿 实测相关:")
for k in four:
    print(f"  纳入 ~ {k:5}: {df5['纳入'].corr(df5[k]):+.2f}")
print(f"  纳入 单独(5腿共同期) 夏普={sh(df5['纳入']):.2f}")

# 网格: 给纳入切w5, 其余按60/10/10/20缩放
print("\n=== 加纳入腿(实测相关) ===")
print("%-7s %8s %8s %9s" % ("w5", "合并夏普", "年化%", "较4腿增量"))
base = sh(sum(W4[k] * df5[k] for k in four))
best = (0, base)
for w5 in np.linspace(0, 0.4, 81):
    wr = {k: W4[k] * (1 - w5) for k in four}
    comb = sum(wr[k] * df5[k] for k in four) + w5 * df5["纳入"]
    s = sh(comb)
    if s > best[1]:
        best = (round(w5, 3), s)
    if abs(w5 - round(w5, 2)) < 1e-9 and round(w5 * 100) % 5 == 0:
        print("%-7.2f %8.2f %8.1f %+9.2f" % (w5, s, ann(comb) * 100, s - base))
print(f"\n4腿合并夏普(5腿共同期)={base:.2f}; 最优 w5={best[0]} -> 合并夏普={best[1]:.2f} (+{best[1]-base:.2f})")
print("注: 口径同 export_combo.py(月度). 相关性为实测, 非假设.")
