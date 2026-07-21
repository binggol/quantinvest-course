"""
复算"指数纳入抢跑腿"的全日历对冲日收益序列, 口径对齐 combo.json 的事件腿
(中性对冲、全日历日频、% 收益), 输出可与现有4腿直接比较的夏普/年化/波动.

策略(可落地版): 半年调宽基(上证50/沪深300/中证500/中证1000)新纳入成分中,
取流通权重后50%(小盘冲击效应更强), 在 [T-10, T-1] 持有(公告后~生效前),
等权多头, 减去当日全市场等权(对冲近似), 窗口外为0(空仓).

输出: data/sleeve_inclusion.json  含日收益序列 + 统计.
"""
import os
import json
import math
import calendar
from collections import defaultdict
from pathlib import Path

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

import numpy as np
import pandas as pd
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    for _p in (Path(__file__).parent.parent / "data" / ".tushare_token", Path(r"C:\rdagent\data\.tushare_token")):
        if _p.exists():
            TOKEN = _p.read_text().strip()
            break
ts.set_token(TOKEN)
pro = ts.pro_api()

INDICES = {"000016.SH": "上证50", "000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000"}
START_YEAR, END_YEAR = 2019, 2025
HOLD_FROM, HOLD_TO = -10, -1   # T-10 建仓, T-1 平仓
OUT = Path(__file__).parent.parent / "data" / "sleeve_inclusion.json"


def second_friday(y, m):
    fr = [w[4] for w in calendar.monthcalendar(y, m) if w[4]]
    return f"{y}{m:02d}{fr[1]:02d}"


def fetch_added():
    """返回 [(eff_date(YYYYMMDD), [con_code...] bottom50% by weight), ...]"""
    cal = pro.trade_cal(exchange="SSE", start_date=f"{START_YEAR}0101",
                        end_date=f"{END_YEAR+1}0131", is_open="1")["cal_date"].sort_values().reset_index(drop=True)
    events = []
    for code in INDICES:
        for y in range(START_YEAR, END_YEAR + 1):
            for m in (6, 12):
                end_c = cal[cal.str.startswith(f"{y}{m:02d}")].max()
                end_p = cal[cal.str.startswith(f"{y}{m-1:02d}")].max()
                if pd.isna(end_c) or pd.isna(end_p):
                    continue
                try:
                    wc = pro.index_weight(index_code=code, trade_date=end_c)
                    wp = pro.index_weight(index_code=code, trade_date=end_p)
                except Exception:
                    continue
                if wc is None or wp is None or wc.empty or wp.empty:
                    continue
                added = set(wc["con_code"]) - set(wp["con_code"])
                if not added:
                    continue
                # 取新增票里"权重后50%"(小盘) — weight 越小越靠后
                wadd = wc[wc["con_code"].isin(added)][["con_code", "weight"]].sort_values("weight")
                keep = wadd.head(max(1, len(wadd) // 2))["con_code"].tolist()
                sf = second_friday(y, m)
                fut = cal[cal >= sf]
                if len(fut) < 2:
                    continue
                eff = fut.iloc[1]
                events.append((eff, keep))
    return events


def qc(c):
    return (c[-2:] + c[:6]).lower()  # 600519.SH -> sh600519 (qlib all 小写)


def main():
    print("fetching added constituents ...")
    events = fetch_added()
    allcodes = sorted({qc(c) for _, lst in events for c in lst})
    print(f"events={len(events)}  unique stocks={len(allcodes)}")

    import qlib
    from qlib.data import D
    qlib.init(provider_uri="Z:/claude/qlib/data/cn_data", region="cn")
    # 全市场日收益(做对冲基准) + 个股
    mkt = D.features(D.instruments("all"), ["$close"], start_time="2018-12-01", end_time="2025-12-31")["$close"].unstack(level=0).sort_index()
    ret = mkt.pct_change()
    mkt_ret = ret.mean(axis=1)               # 全市场等权日收益(对冲近似)
    dates = ret.index

    sleeve = pd.Series(0.0, index=dates)     # 全日历日收益, 默认0(空仓)
    for eff, lst in events:
        eff_ts = pd.to_datetime(eff)
        pos = dates.get_indexer([eff_ts], method="bfill")[0]
        if pos == -1:
            continue
        cols = [c for c in (qc(x) for x in lst) if c in ret.columns]
        if not cols:
            continue
        for sh in range(HOLD_FROM, HOLD_TO + 1):
            i = pos + sh
            if 0 <= i < len(dates):
                day_long = ret.iloc[i][cols].mean()
                if pd.notna(day_long):
                    # 同一天多个事件窗口重叠 -> 叠加(近似多事件并行各占资金, 简单相加后面不归一)
                    sleeve.iloc[i] += (day_long - mkt_ret.iloc[i])

    s = sleeve[sleeve != 0]
    full = sleeve.loc[(sleeve.index >= "2019-01-01")]
    n = len(full)
    m = full.mean()
    sd = full.std(ddof=1)
    sharpe = (m / sd) * math.sqrt(252) if sd > 0 else 0
    ann = m * 252
    # 仅活跃日统计(per-trade 视角)
    act_sharpe = (s.mean() / s.std(ddof=1)) * math.sqrt(252) if s.std(ddof=1) > 0 else 0
    print(f"\n全日历: 交易日={n}, 活跃日={len(s)} ({len(s)/n*100:.1f}%)")
    print(f"全日历对冲: 年化={ann*100:.2f}%  夏普={sharpe:.2f}")
    print(f"(参考)仅活跃日年化夏普={act_sharpe:.2f}")
    print(f"\n对比 combo.json 现有腿夏普: 主300=1.85 抢跑=0.87 PEAD=0.86 回购=1.34")

    OUT.write_text(json.dumps({
        "updated": "rebuilt",
        "method": f"宽基(300/500/1000)新纳入·权重后50%·[T{HOLD_FROM},T{HOLD_TO}]持有·减全市场等权对冲·全日历",
        "n_events": len(events),
        "full_calendar": {"sharpe": round(sharpe, 3), "ann": round(ann, 4),
                          "vol": round(sd * math.sqrt(252), 4), "active_pct": round(len(s) / n, 4)},
        "daily": {d.strftime("%Y-%m-%d"): round(float(v), 6) for d, v in full.items() if v != 0},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nsaved {OUT}")

    # 落 _sleeve_inclusion.pkl 供 C:\rdagent\export_combo.py 当第5腿读 (格式同 _sleeve_repo.pkl: 全日历日收益 dict)
    import pickle
    rd_pkl = Path(r"C:\rdagent") / "_sleeve_inclusion.pkl"
    try:
        pickle.dump({d.strftime("%Y-%m-%d"): float(v) for d, v in full.items()}, open(rd_pkl, "wb"))
        print(f"saved {rd_pkl}")
    except Exception as e:
        print(f"pkl save skip: {e}")


if __name__ == "__main__":
    main()
