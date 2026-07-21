# -*- coding: utf-8 -*-
"""生成 data/lhb_avoid.json: 龙虎榜净卖出股 = 避雷信号(标记不剔除)。
验证 gate_lhb_verify.py: 龙虎榜净卖出(净买占比后20%)股, T+1开盘买后5日超额-2.19%, 2024/25/26三年全负(-2.85/-1.21/-2.63)。
⚠️选股侧IC是时间穿越假象(盘后披露), 真信号只在避雷侧(净卖=出货砸盘)。强度≈热榜。
口径: 最近1交易日龙虎榜中 净买额占成交额 后30%(净卖最重)的股, 标避雷。
跑: D:/anaconda3/python.exe scripts/export_lhb_avoid.py
"""
import os, json, datetime
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "lhb_avoid.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    import tushare as ts
    tok = os.environ.get("TUSHARE_TOKEN") or open(os.path.join(DATA, ".tushare_token")).read().strip()
    pro = ts.pro_api(tok)
    today = datetime.date.today()
    cal = pro.trade_cal(exchange='SSE', start_date=(today - datetime.timedelta(days=12)).strftime('%Y%m%d'),
                        end_date=today.strftime('%Y%m%d'), is_open='1')
    last_td = sorted(cal['cal_date'])[-1]
    tl = pro.top_list(trade_date=last_td, fields="ts_code,name,net_amount,amount,reason")
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "as_of": last_td[:4] + "-" + last_td[4:6] + "-" + last_td[6:8],
           "note": "龙虎榜净卖出股=避雷(验证: T+1买后5日-2.19%, 三年全负, 强度≈热榜)。⚠️仅避雷侧真, 选股侧IC是盘后穿越假象。标记不剔除人工判断。",
           "items": []}
    if tl is None or not len(tl):
        out["msg"] = f"{last_td} 无龙虎榜"
        _write(out); return
    import pandas as pd
    tl = tl.copy()
    tl["net_ratio"] = tl["net_amount"] / (tl["amount"].astype(float) + 1)
    thr = tl["net_ratio"].quantile(0.30)   # 后30%=净卖最重
    bad = tl[tl["net_ratio"] <= thr].sort_values("net_ratio")
    items = []
    for r in bad.itertuples(index=False):
        items.append({"code": str(r.ts_code)[:6] + str(r.ts_code)[-3:] if "." in str(r.ts_code) else str(r.ts_code),
                      "ts_code": str(r.ts_code), "name": str(getattr(r, "name", "")),
                      "net_yi": round(float(r.net_amount) / 1e8, 2),       # 净买额(亿, 负=净卖)
                      "net_ratio_pct": round(float(r.net_ratio) * 100, 1),  # 净买占成交额%(负=净卖)
                      "reason": str(getattr(r, "reason", ""))[:30]})
    out["items"] = items
    out["n"] = len(items)
    out["thr_pct"] = round(float(thr) * 100, 1)
    _write(out)


def _write(out):
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "lhb_avoid.json"))
    except Exception as e:
        print(f"[lhb_avoid] 拷NAS失败: {e}")
    print(f"[lhb_avoid] {out.get('as_of')} 龙虎榜净卖避雷 {out.get('n', 0)}只 -> {OUT}")


if __name__ == "__main__":
    main()
