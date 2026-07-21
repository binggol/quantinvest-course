"""
质量因子腿(第6腿)并入组合: 与现有5腿(主300/抢跑/PEAD/回购/纳入)实测相关性 + 合并夏普增量。
口径同 export_combo (各腿→月度, 月度夏普*sqrt(12))。
"""
import io, sys, json, pickle, math
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from pathlib import Path
import numpy as np
import pandas as pd

RD = Path(r"C:\rdagent")
DATA = Path(__file__).parent.parent / "data"


def dcurve(P):
    s = {}
    for x in P:
        dd = pd.bdate_range(x["de"], x["dx"])
        if len(dd) <= 1: continue
        rt = (1 + x["net"]) ** (1.0 / (len(dd) - 1)) - 1
        for t in dd[1:]: s[t] = rt
    return (1 + pd.Series(s).sort_index()).resample("ME").prod() - 1


def from_daily(s):
    s = pd.Series(s); s.index = pd.to_datetime(s.index)
    return (1 + s).resample("ME").prod() - 1


legs = {}
try: legs["主300"] = dcurve(pickle.load(open(RD / "_sleeve_300.pkl", "rb")))
except Exception as e: print("主300 fail", e)
try:
    ru = pickle.load(open(RD / "_sleeve_runup.pkl", "rb")); rs = pd.Series({x["d"]: x["net"] for x in ru}); rs.index = pd.to_datetime(rs.index)
    legs["抢跑"] = (1 + rs).resample("ME").prod() - 1
except Exception as e: print("抢跑 fail", e)
try: legs["PEAD"] = dcurve(pickle.load(open(RD / "_sleeve_pead.pkl", "rb")))
except Exception as e: print("PEAD fail", e)
try: legs["回购"] = from_daily(pickle.load(open(RD / "_sleeve_repo.pkl", "rb")))
except Exception as e: print("回购 fail", e)
legs["纳入"] = from_daily(json.loads((DATA / "sleeve_inclusion.json").read_text(encoding="utf-8"))["daily"])
legs["质量"] = from_daily(json.loads((DATA / "sleeve_quality.json").read_text(encoding="utf-8"))["daily"])

sh = lambda x: float(x.mean() / x.std() * np.sqrt(12)) if len(x) > 3 and x.std() > 0 else 0
ann = lambda x: float((1 + x).prod() ** (12.0 / len(x)) - 1) if len(x) > 3 else 0

print("=== 各腿(全样本月度) ===")
for k, v in legs.items():
    print(f"  {k:5} 月数={len(v):3d} 夏普={sh(v):.2f} 年化={ann(v)*100:5.1f}%")

# 现有5腿权重(combo.json: 主54/抢9/PEAD9/回购18/纳入10)
W5 = {"主300": 0.54, "抢跑": 0.09, "PEAD": 0.09, "回购": 0.18, "纳入": 0.10}
five = list(W5)
df = pd.concat([legs[k].rename(k) for k in (five + ["质量"]) if k in legs], axis=1).dropna()
print(f"\n=== 6腿共同月={len(df)} ===")
print("质量腿 vs 现有5腿 实测相关:")
for k in five:
    if k in df: print(f"  质量 ~ {k:5}: {df['质量'].corr(df[k]):+.2f}")
print(f"  质量 单独(共同期) 夏普={sh(df['质量']):.2f}")

base = sh(sum(W5[k] * df[k] for k in five if k in df))
print(f"\n=== 加质量腿(实测相关), 现有5腿合并夏普(共同期)={base:.2f} ===")
print("%-7s %8s %8s %9s" % ("w质量", "合并夏普", "年化%", "较5腿增量"))
best = (0, base)
for w6 in np.linspace(0, 0.4, 81):
    wr = {k: W5[k] * (1 - w6) for k in five}
    comb = sum(wr[k] * df[k] for k in five if k in df) + w6 * df["质量"]
    s = sh(comb)
    if s > best[1]: best = (round(w6, 3), s)
    if abs(w6 - round(w6, 2)) < 1e-9 and round(w6 * 100) % 5 == 0:
        print("%-7.2f %8.2f %8.1f %+9.2f" % (w6, s, ann(comb) * 100, s - base))
print(f"\n最优 w质量={best[0]} -> 合并夏普={best[1]:.2f} (+{best[1]-base:.2f}); 口径同export_combo(月度), 相关实测")
