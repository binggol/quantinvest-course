"""
扩展版指数纳入抢跑可信度检验: 上证50 / 中证1000 / 科创50 / 创业板指.
方法学与 export_index_inclusion.py 完全一致 (index_weight 比较前后两期成分算新增,
qlib 收盘价算 T-20→T-1 等窗口收益), 额外输出 t 检验.

注意:
- 半年调 (6/12月, 生效日=第二个周五次一交易日): 上证50/中证1000/创业板指
- 季调 (3/6/9/12月): 科创50
- 收益为绝对收益(未扣基准), 与现有研究集口径一致, 仅供横向对比.
"""
import os
import math
import calendar
from collections import defaultdict

# 国内数据直连, 清掉环境里的 NIM 代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

import numpy as np
import pandas as pd
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN", "")
ts.set_token(TOKEN)
pro = ts.pro_api()

# index_code -> (中文名, 调仓月份列表)
INDICES = {
    "000016.SH": ("上证50", [6, 12]),
    "000852.SH": ("中证1000", [6, 12]),
    "000688.SH": ("科创50", [3, 6, 9, 12]),
    "399006.SZ": ("创业板指", [6, 12]),
}

START_YEAR, END_YEAR = 2019, 2024


def second_friday(year, month):
    c = calendar.monthcalendar(year, month)
    fridays = [w[4] for w in c if w[4] != 0]
    return f"{year}{month:02d}{fridays[1]:02d}"


def effective_date(year, month, cal):
    sf = second_friday(year, month)
    fut = cal[cal >= sf]
    return fut.iloc[1] if len(fut) > 1 else None


def fetch_events(index_code, months):
    cal = pro.trade_cal(exchange="SSE", start_date=f"{START_YEAR}0101",
                        end_date=f"{END_YEAR+1}0131", is_open="1")["cal_date"].sort_values().reset_index(drop=True)
    events = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in months:
            prev_month = month - 1
            end_curr = cal[cal.str.startswith(f"{year}{month:02d}")].max()
            end_prev = cal[cal.str.startswith(f"{year}{prev_month:02d}")].max()
            if pd.isna(end_curr) or pd.isna(end_prev):
                continue
            try:
                wc = pro.index_weight(index_code=index_code, trade_date=end_curr)
                wp = pro.index_weight(index_code=index_code, trade_date=end_prev)
            except Exception as e:
                print(f"  weight fail {index_code} {year}-{month}: {e}")
                continue
            if wc is None or wp is None or wc.empty or wp.empty:
                continue
            added = set(wc["con_code"]) - set(wp["con_code"])
            if not added:
                continue
            eff = effective_date(year, month, cal)
            if not eff:
                continue
            for code in added:
                events.append({"ts_code": code, "inclusion_date": eff, "period": f"{year}-{month:02d}"})
    return pd.DataFrame(events)


def q_code(code):
    if code.endswith(".SH"):
        return "SH" + code[:6]
    if code.endswith(".SZ"):
        return "SZ" + code[:6]
    if code.endswith(".BJ"):
        return "BJ" + code[:6]
    return code


def calc_returns(ev):
    import qlib
    from qlib.data import D
    try:
        qlib.init(provider_uri="Z:/claude/qlib/data/cn_data", region="cn")
    except Exception:
        pass
    codes = [q_code(c) for c in ev["ts_code"].unique()]
    close = D.features(codes, ["$close"], start_time="2018-01-01", end_time="2025-12-31")
    if close.empty:
        print("qlib close empty!")
        return pd.DataFrame()
    cal = pd.Series(D.calendar(start_time="2018-01-01", end_time="2025-12-31"))
    rows = []
    for _, r in ev.iterrows():
        qc = q_code(r["ts_code"])
        inc = pd.to_datetime(r["inclusion_date"])
        vc = cal[cal >= inc]
        if vc.empty:
            continue
        t_date = vc.iloc[0]
        try:
            sdf = close.loc[(qc, slice(None)), :].reset_index(level=0, drop=True)
        except KeyError:
            continue
        if sdf.empty:
            continue
        ix = sdf.index.get_indexer([t_date], method="bfill")
        if ix[0] == -1:
            continue
        t = ix[0]

        def c(sh):
            i = t + sh
            return sdf.iloc[i]["$close"] if 0 <= i < len(sdf) else np.nan
        ret = lambda a, b: float(a / b - 1) if pd.notna(a) and pd.notna(b) and b > 0 else None
        c20, c10, c5, c1, c0 = c(-20), c(-10), c(-5), c(-1), c(0)
        p5, p10, p20 = c(5), c(10), c(20)
        rows.append({
            "ts_code": r["ts_code"], "period": r["period"],
            "ret_T20_T1": ret(c1, c20), "ret_T10_T1": ret(c1, c10), "ret_T5_T1": ret(c1, c5),
            "ret_T1_T0": ret(c0, c1), "ret_T0_T5": ret(p5, c0), "ret_T0_T10": ret(p10, c0),
            "ret_T0_T20": ret(p20, c0),
        })
    return pd.DataFrame(rows)


def tstat(vals):
    v = [x for x in vals if isinstance(x, (int, float)) and pd.notna(x)]
    n = len(v)
    if n < 2:
        return n, None, None, None
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    t = m / (sd / math.sqrt(n)) if sd > 0 else None
    win = sum(1 for x in v if x > 0) / n * 100
    return n, m * 100, t, win


WINS = [("ret_T20_T1", "抢跑 T-20→T-1"), ("ret_T10_T1", "T-10→T-1"), ("ret_T5_T1", "T-5→T-1"),
        ("ret_T1_T0", "纳入当日 T-1→T0"), ("ret_T0_T5", "纳入后 T0→+5"),
        ("ret_T0_T10", "T0→+10"), ("ret_T0_T20", "T0→+20")]


def main():
    for code, (name, months) in INDICES.items():
        print(f"\n##### {name} ({code}) 调仓月={months} #####")
        ev = fetch_events(code, months)
        if ev.empty:
            print("  无纳入事件 (可能该指数早期无 index_weight 数据)")
            continue
        ret = calc_returns(ev)
        if ret.empty:
            print(f"  事件 {len(ev)} 起, 但 qlib 收益为空")
            continue
        print(f"  事件 {len(ev)} 起, 有收益 {len(ret)} 起")
        print("  %-16s %4s %8s %7s %6s" % ("窗口", "n", "均值%", "t值", "胜率%"))
        for k, lab in WINS:
            n, m, t, win = tstat(ret[k].tolist())
            if m is None:
                print("  %-16s %4d  数据不足" % (lab, n))
                continue
            print("  %-16s %4d %8.2f %7.2f %6.1f" % (lab, n, m, (t or 0), win))
    print("\n注: |t|>1.96=>95%显著; |t|>2.58=>99%显著. 收益为绝对(未扣基准).")


if __name__ == "__main__":
    main()
