"""
组合合并器: 读各腿日收益序列 -> 算各腿夏普/年化/波动 + 相关矩阵 + 固定权重合并夏普 -> 写 combo.json.
纳入抢跑腿(第5腿)已内置.

输入: data/sleeve_<key>.json, 每个形如 build_inclusion_sleeve.py 的输出:
  {"daily": {"YYYY-MM-DD": 收益(小数), ...}, ...}   # 全日历对冲日收益, 非交易/空仓日可省略(按0)

现有4腿(主300/抢跑/PEAD/回购)的序列目前未存盘 —— 需先各跑一个 build_<leg>_sleeve.py 落出
data/sleeve_main300.json / sleeve_runup.json / sleeve_pead.json / sleeve_repo.json (格式同上),
本脚本即可一键产出含5腿的 combo.json. 缺哪条腿就跳过哪条并告警.

权重: 固定档(combo原则: 不动态优化避免小样本过拟合). 给纳入10%, 其余4腿按0.9缩放维持60/10/10/20内部比例.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent.parent / "data"
OUT = DATA / "combo.json"

# (展示名, 序列文件key, 目标权重)
LEGS = [
    ("主300", "main300", 0.54),
    ("抢跑", "runup", 0.09),
    ("PEAD", "pead", 0.09),
    ("回购", "repo", 0.18),
    ("纳入", "inclusion", 0.10),
]
ANN_DAYS = 252


def load_series(key):
    f = DATA / f"sleeve_{key}.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text(encoding="utf-8"))
    daily = d.get("daily", {})
    if not daily:
        return None
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in daily.items()}).sort_index()
    return s


def main():
    series, names, weights = {}, [], []
    missing = []
    for disp, key, w in LEGS:
        s = load_series(key)
        if s is None:
            missing.append(f"{disp}({key})")
            continue
        series[disp] = s
        names.append(disp)
        weights.append(w)
    if missing:
        print(f"⚠️ 缺序列, 跳过: {', '.join(missing)}  (放 data/sleeve_<key>.json 后重跑)")
    if len(names) < 2:
        print("可用腿不足2条, 无法合并. 先补序列文件.")
        return

    # 对齐到并集日历(空缺=0=当日空仓), 仅取各腿都有数据起的区间到末尾
    idx = sorted(set().union(*[set(series[n].index) for n in names]))
    full = pd.DataFrame(0.0, index=pd.DatetimeIndex(idx), columns=names)
    for n in names:
        full.loc[series[n].index, n] = series[n].values
    # 共同样本期: 从最晚的"首个非零日"开始, 避免某腿前期全空拉低
    starts = [series[n].index.min() for n in names]
    full = full.loc[full.index >= max(starts)]

    w = np.array(weights)
    w = w / w.sum()  # 归一(缺腿时重新归一)
    mu = full.mean().values * ANN_DAYS
    vol = full.std(ddof=1).values * math.sqrt(ANN_DAYS)
    sharpe = np.divide(mu, vol, out=np.zeros_like(mu), where=vol > 0)
    R = full.corr().values
    Sig = R * np.outer(vol / math.sqrt(ANN_DAYS), vol / math.sqrt(ANN_DAYS))  # 日频协方差
    port_daily = (full.values @ w)
    p_mu = port_daily.mean() * ANN_DAYS
    p_vol = port_daily.std(ddof=1) * math.sqrt(ANN_DAYS)
    p_sharpe = p_mu / p_vol if p_vol > 0 else 0

    sleeves = [{"name": names[i], "weight": round(float(w[i]), 3),
                "sharpe": round(float(sharpe[i]), 2), "ann": round(float(mu[i]), 4)}
               for i in range(len(names))]
    corr = {names[i]: {names[j]: round(float(R[i, j]), 2) for j in range(len(names))} for i in range(len(names))}
    out = {
        "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "common_days": int(len(full)),
        "strategy": "五腿市场中性组合(沪深300季频对冲为主腿 + 抢跑/PEAD/回购/指数纳入 四条事件腿), 固定权重",
        "sleeves": sleeves,
        "combined": {"weights": "/".join(f"{n}{round(w[i]*100)}" for i, n in enumerate(names)),
                     "sharpe": round(float(p_sharpe), 2), "ann": round(float(p_mu), 4)},
        "corr": corr,
        "note": "纳入抢跑腿为第5腿(指数重构事件, 与盈利/回购/因子触发独立). 权重固定档. 序列由各 build_*_sleeve.py 落盘后本脚本合并.",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合并 {len(names)} 腿, 共同样本 {len(full)} 日")
    for s in sleeves:
        print(f"  {s['name']:6} w={s['weight']:.2f} 夏普={s['sharpe']:.2f} 年化={s['ann']*100:.1f}%")
    print(f"  合并夏普={out['combined']['sharpe']}  年化={out['combined']['ann']*100:.1f}%")
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
