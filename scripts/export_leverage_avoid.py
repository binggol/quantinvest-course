# -*- coding: utf-8 -*-
"""生成 data/leverage_avoid.json: 融资买入暴增的票=杠杆资金透支(弱避雷, 标记不剔除)。
验证 gate_margin_factor.py: 融资净买入因子 RankIC=-0.0155(反向, 融资买多→次日偏弱), 正交化后-0.0171(独立非市值代理)。
弱信号(|IC|<0.02), 仅当"杠杆透支"标记供组合清单标橙、人工决定。比热榜(-3.2%/20日)弱很多, 不硬剔。
口径: 当日融资净买入占成交额, 取分位>80%(融资集中流入)为标记。csi300成分。
跑: D:/anaconda3/python.exe scripts/export_margin_avoid.py
"""
import os, json, datetime
import pandas as pd
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "leverage_avoid.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")

def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    import tushare as ts
    tok = os.environ.get("TUSHARE_TOKEN") or open(os.path.join(DATA, ".tushare_token")).read().strip()
    pro = ts.pro_api(tok)
    # csi300成分(从qlib instruments)
    QBASE = os.environ.get("QI_QLIB_DATA_DIR", r"C:\qlib_data\cn_data")
    mem = []
    for ln in open(os.path.join(QBASE, "instruments", "csi300.txt"), encoding="utf-8"):
        p = ln.split()
        if len(p) >= 3 and p[2] >= "2025-06-01":
            c = p[0]; mem.append(c[2:] + "." + c[:2].upper())
    mem = sorted(set(mem))
    # 最近交易日
    cal = pro.trade_cal(exchange='SSE', start_date=(datetime.date.today() - datetime.timedelta(days=20)).strftime('%Y%m%d'),
                        end_date=datetime.date.today().strftime('%Y%m%d'), is_open='1')
    last_td = sorted(cal['cal_date'])[-1]
    # 拉最近一日全市场融资明细(margin_detail单日全市场)
    md = pro.margin_detail(trade_date=last_td, fields="trade_date,ts_code,rzmre,rzche,rzye")
    if md is None or not len(md):
        print(f"[leverage_avoid] {last_td} 无融资数据"); return
    dl = pro.daily(trade_date=last_td, fields="ts_code,amount")
    df = md.merge(dl, on="ts_code", how="inner")
    df = df[df["ts_code"].isin(mem)].copy()
    df["mbf"] = (df["rzmre"] - df["rzche"]) / (df["amount"] * 1000 + 1)   # 融资净买入/成交额
    df = df.dropna(subset=["mbf"])
    if not len(df):
        print("[leverage_avoid] 无csi300融资数据"); return
    thr = df["mbf"].quantile(0.80)   # 前20%融资集中流入=杠杆透支
    flag = df[df["mbf"] >= thr].sort_values("mbf", ascending=False)
    # 名称
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    nm = dict(zip(sb['ts_code'], sb['name']))
    items = []
    for r in flag.itertuples(index=False):
        items.append({"code": r.ts_code, "name": nm.get(r.ts_code, r.ts_code),
                      "mbf_pct": round(float(r.mbf) * 100, 2),       # 融资净买入占成交额%
                      "rzye_yi": round(float(r.rzye) / 1e8, 1)})     # 融资余额(亿)
    out = {"as_of": last_td[:4] + "-" + last_td[4:6] + "-" + last_td[6:8],
           "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "n": len(items), "thr_pct": round(float(thr) * 100, 2), "items": items,
           "note": "融资净买入占成交额前20%=杠杆资金集中流入(透支)。验证: 融资买入因子反向RankIC-0.0155(融资多→次日偏弱)。弱信号, 标记不剔除, 人工决定。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "leverage_avoid.json"))
    except Exception as e:
        print(f"[leverage_avoid] 拷NAS失败: {e}")
    print(f"[leverage_avoid] {out['as_of']} 杠杆透支标记 {len(items)}只(融资净买>{out['thr_pct']}%) -> {OUT}")


if __name__ == "__main__":
    main()
