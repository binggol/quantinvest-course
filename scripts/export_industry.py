"""
行业基本面分析: 按行业聚合 扣非净利润增速 / 营收增速 / 利润弹性(扣非增速−营收增速), 供选股参考。
利润弹性>0 = 扣非利润增速跑赢营收增速 = 降本增效/经营杠杆/毛利改善, 通常是更优的景气行业。

数据: tushare fina_indicator_vip(最新报告期) 全市场 or_yoy/dt_netprofit_yoy + stock_meta.db 行业分类。
聚合用中位数(抗异常值)。输出 data/industry.json。
跑: python scripts/export_industry.py
"""
import os, json, sqlite3, statistics
from datetime import datetime

for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'; os.environ['NO_PROXY'] = '*'

import tushare as ts

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

DATA_DIR = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA_DIR, "industry.json")
META_DB = os.path.join(DATA_DIR, "stock_meta.db")
MIN_N = 5  # 行业最少成分数(太少不稳健)


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def main():
    pro = ts.pro_api(get_tushare_token())
    # 行业分类(stock_meta 已有), 退回 tushare stock_basic
    ind = {}
    names = {}
    if os.path.exists(META_DB):
        con = sqlite3.connect(META_DB)
        try:
            for tc, nm, industry in con.execute("SELECT ts_code,name,industry FROM stock_meta WHERE list_status='L'"):
                ind[tc] = industry or "其他"; names[tc] = nm
        except Exception as e:
            print("meta err", e)
        con.close()
    if not ind:
        sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
        for r in sb.itertuples(index=False):
            ind[r.ts_code] = (r.industry or "其他"); names[r.ts_code] = r.name

    # 最新有数据的报告期
    fin = None; period = None
    for p in ["20260331", "20251231", "20250930", "20250630"]:
        try:
            d = pro.fina_indicator_vip(period=p, fields='ts_code,end_date,or_yoy,dt_netprofit_yoy,netprofit_yoy')
        except Exception as e:
            print(f"vip {p} err {e}"); continue
        if d is not None and d['dt_netprofit_yoy'].notna().sum() > 2000:
            fin = d; period = p; break
    if fin is None:
        print("无可用财务期"); return
    print(f"[industry] 报告期 {period}, {len(fin)} 条")

    f = {r.ts_code: r for r in fin.itertuples(index=False)}

    # 按行业聚合
    groups = {}
    for tc, industry in ind.items():
        r = f.get(tc)
        if r is None:
            continue
        dt = None if r.dt_netprofit_yoy != r.dt_netprofit_yoy else float(r.dt_netprofit_yoy)
        orr = None if r.or_yoy != r.or_yoy else float(r.or_yoy)
        groups.setdefault(industry, []).append((tc, dt, orr))

    rows = []
    for industry, members in groups.items():
        dts = [m[1] for m in members if m[1] is not None]
        ors = [m[2] for m in members if m[2] is not None]
        n = len(members)
        if n < MIN_N or not dts or not ors:
            continue
        dt_med = med(dts); or_med = med(ors)
        spread = round(dt_med - or_med, 1) if (dt_med is not None and or_med is not None) else None
        # 占比: 扣非增速>营收增速 / 扣非增速>0
        both = [(m[1], m[2]) for m in members if m[1] is not None and m[2] is not None]
        pct_dt_gt_or = round(sum(1 for a, b in both if a > b) / len(both) * 100, 0) if both else 0
        pct_dt_pos = round(sum(1 for x in dts if x > 0) / len(dts) * 100, 0)
        # 该行业 扣非增速最高的几只(选股参考)
        top = sorted([m for m in members if m[1] is not None], key=lambda m: m[1], reverse=True)[:5]
        top_stocks = [{"code": m[0], "name": names.get(m[0], ""), "dt_yoy": round(m[1], 1),
                       "or_yoy": (round(m[2], 1) if m[2] is not None else None)} for m in top]
        rows.append({"industry": industry, "n": n, "dt_yoy": dt_med, "or_yoy": or_med,
                     "spread": spread, "pct_dt_gt_or": pct_dt_gt_or, "pct_dt_pos": pct_dt_pos,
                     "top_stocks": top_stocks})

    rows.sort(key=lambda x: (x["spread"] if x["spread"] is not None else -999), reverse=True)
    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "period": period,
           "n_industries": len(rows), "min_members": MIN_N, "rows": rows}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[industry] {len(rows)} 个行业 -> {OUT}")
    for r in rows[:8]:
        print(f"  {r['industry']:10} n={r['n']:3} 扣非{r['dt_yoy']:6}% 营收{r['or_yoy']:6}% 弹性{r['spread']:6} 利润跑赢占比{r['pct_dt_gt_or']:.0f}%")


if __name__ == "__main__":
    main()
