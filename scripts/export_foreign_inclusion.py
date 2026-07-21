# -*- coding: utf-8 -*-
"""生成 foreign_inclusion.json: 境外指数(MSCI中国/富时罗素中国)纳入日历 + 粗略市值代理候选。
⚠️诚实: MSCI/富时无公开成分数据(tushare/akshare都没), 无法真预测准成分。
- 日历: 用户提供的调整日程(公告日/生效日/倒计时), 可靠。
- 候选: 仅"大流通市值A股+好流动性+不在沪深300/上证50"的市值代理(MSCI中国大盘≈300+部分), 非官方仅参考。
跑: D:/anaconda3/python.exe scripts/export_foreign_inclusion.py
"""
import os, json, datetime
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "foreign_inclusion.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")

# 调整日历(手工维护; MSCI季度/半年、富时季度)
SCHEDULE = [
    {"index": "MSCI中国(季度)", "ann_date": "2026-08-13", "eff_date": "2026-08-31"},
    {"index": "富时罗素中国", "ann_date": "2026-09-04", "eff_date": "2026-09-18"},
    {"index": "MSCI中国(半年)", "ann_date": "2026-11-12", "eff_date": "2026-11-30"},
]


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    import tushare as ts
    import pandas as pd
    tok = os.environ.get("TUSHARE_TOKEN") or open(os.path.join(DATA, ".tushare_token")).read().strip()
    pro = ts.pro_api(tok)
    today = datetime.date.today()
    # 倒计时
    cal_out = []
    for s in SCHEDULE:
        eff = datetime.date.fromisoformat(s["eff_date"])
        ann = datetime.date.fromisoformat(s["ann_date"])
        cal_out.append({**s, "days_to_ann": (ann - today).days, "days_to_eff": (eff - today).days})
    # 粗略候选: 流通市值大、不在沪深300/上证50、流动性好
    last_td = pro.trade_cal(exchange='SSE', start_date=(today - datetime.timedelta(days=12)).strftime('%Y%m%d'),
                            end_date=today.strftime('%Y%m%d'), is_open='1')['cal_date'].max()
    db = pro.daily_basic(trade_date=last_td, fields='ts_code,circ_mv,turnover_rate_f,close')
    QBASE = os.environ.get("QI_QLIB_DATA_DIR", r"C:\qlib_data\cn_data")
    def members(inst):
        m = set()
        for ln in open(os.path.join(QBASE, "instruments", inst + ".txt"), encoding="utf-8"):
            p = ln.split()
            if len(p) >= 3 and p[2] >= "2026-01-01":
                c = p[0]; m.add(c[2:] + "." + c[:2].upper())
        return m
    in300 = members("csi300")
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    nm = dict(zip(sb['ts_code'], sb['name'])); ind = dict(zip(sb['ts_code'], sb['industry']))
    df = db.dropna(subset=['circ_mv']).copy()
    df = df[~df['ts_code'].isin(in300)]                      # 剔已在300(大概率已在MSCI)
    df = df[df['turnover_rate_f'].fillna(0) >= 0.5]          # 流动性门槛
    df = df[df['circ_mv'] >= 3000000]                        # 流通市值>=300亿(circ_mv单位万元; MSCI中盘门槛粗略)
    df = df.sort_values('circ_mv', ascending=False).head(40)
    cands = [{"code": r.ts_code, "name": nm.get(r.ts_code, r.ts_code), "industry": ind.get(r.ts_code, ""),
              "circ_mv_yi": round(float(r.circ_mv) / 10000.0, 0), "turnover": round(float(r.turnover_rate_f or 0), 2)}
             for r in df.itertuples(index=False)]
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": last_td[:4]+"-"+last_td[4:6]+"-"+last_td[6:8],
           "schedule": cal_out, "candidates": cands, "n_cand": len(cands),
           "disclaimer": "⚠️ MSCI/富时无公开成分数据, 无法真预测准成分。日历可靠; 候选仅=大流通市值(>300亿)+好流动性+不在沪深300的市值代理(MSCI中国大盘≈沪深300+部分中盘), 非官方预测, 仅供关注方向, 别当真名单交易。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "foreign_inclusion.json"))
    except Exception as e:
        print(f"[foreign_inclusion] 拷NAS失败: {e}")
    print(f"[foreign_inclusion] 日历{len(cal_out)}期 候选{len(cands)}只(市值代理) -> {OUT}")


if __name__ == "__main__":
    main()
