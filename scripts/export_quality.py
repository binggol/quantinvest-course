"""
质量成长选股清单(第6腿实盘池): 扣非净利增速≥20% & 营收增速≥0 & 扣非增速≥营收增速(利润弹性≥0)。
回测: 该池等权对冲市场 夏普~1.2-1.5, 与组合其它腿低/负相关(回购-0.57), 进组合+0.18夏普。
数据: fina_indicator_vip(最新报告期) + stock_meta 行业/名称。输出 data/quality.json。
跑: D:/anaconda3/python.exe scripts/export_quality.py
"""
import io, sys, os, json, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'
from datetime import datetime
import tushare as ts

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA, "quality.json")
META = os.path.join(DATA, "stock_meta.db")
tok_path = os.path.join(DATA, ".tushare_token")
TOK = open(tok_path).read().strip() if os.path.exists(tok_path) else os.environ.get("TUSHARE_TOKEN", "")
DT_MIN = 20  # 扣非增速门槛


def main():
    pro = ts.pro_api(TOK)
    ind, names = {}, {}
    if os.path.exists(META):
        con = sqlite3.connect(META)
        try:
            for tc, nm, industry in con.execute("SELECT ts_code,name,industry FROM stock_meta WHERE list_status='L'"):
                ind[tc] = industry or "其他"; names[tc] = nm
        except Exception:
            pass
        con.close()
    fin = None; period = None
    for p in ["20260331", "20251231", "20250930", "20250630"]:
        try:
            d = pro.fina_indicator_vip(period=p, fields="ts_code,end_date,or_yoy,dt_netprofit_yoy,roe,grossprofit_margin")
        except Exception:
            continue
        if d is not None and d['dt_netprofit_yoy'].notna().sum() > 2000:
            fin = d.sort_values("end_date").drop_duplicates("ts_code", keep="last"); period = p; break
    if fin is None:
        print("无可用财务期"); return

    # 流通市值过滤(与回测口径一致, 剔微盘/低基数反转噪音): 取最近交易日 circ_mv, 阈值30亿
    cap = {}
    try:
        cal = pro.trade_cal(exchange='SSE', start_date=f"{int(period[:4])}0101", end_date=datetime.now().strftime("%Y%m%d"), is_open='1')
        for d0 in sorted(cal['cal_date'].tolist())[-6:][::-1]:
            db = pro.daily_basic(trade_date=d0, fields='ts_code,circ_mv')
            if db is not None and len(db):
                cap = {r.ts_code: r.circ_mv for r in db.itertuples(index=False)}; break
    except Exception as e:
        print("daily_basic err", e)
    CAP_MIN = 300000.0  # circ_mv 单位万元 → 30亿

    rows = []
    for r in fin.itertuples(index=False):
        dt = r.dt_netprofit_yoy; orr = r.or_yoy
        if dt != dt or orr != orr:
            continue
        c = r.ts_code
        nm = names.get(c, "")
        if "ST" in nm.upper():            # 剔ST
            continue
        if not c[:6].isdigit():           # 剔非标准代码(如 A23242)
            continue
        if cap and cap.get(c, 0) < CAP_MIN:  # 流通市值<30亿剔除
            continue
        if dt >= DT_MIN and orr >= 0 and dt >= orr:  # 质量筛
            rows.append({"code": c, "name": nm, "industry": ind.get(c, ""),
                         "dt_yoy": round(float(dt), 1), "or_yoy": round(float(orr), 1),
                         "spread": round(float(dt - orr), 1),
                         "circ_mv": (round(cap.get(c, 0) / 1e4, 1) if cap else None),  # 亿元
                         "roe": (None if r.roe != r.roe else round(float(r.roe), 1)),
                         "gpm": (None if r.grossprofit_margin != r.grossprofit_margin else round(float(r.grossprofit_margin), 1))})
    rows.sort(key=lambda x: x["dt_yoy"], reverse=True)
    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "period": period, "n": len(rows),
           "criteria": f"扣非增速≥{DT_MIN}% 且 营收增速≥0 且 扣非增速≥营收增速(利润弹性≥0)", "rows": rows}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[quality] 报告期{period} 入选 {len(rows)} 只 -> {OUT}")
    for r in rows[:8]:
        print(f"  {r['code']} {r['name']:8} {r['industry']:8} 扣非{r['dt_yoy']:6}% 营收{r['or_yoy']:6}% 弹性{r['spread']:6} ROE{r['roe']}")


if __name__ == "__main__":
    main()
