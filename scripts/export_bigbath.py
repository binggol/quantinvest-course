# -*- coding: utf-8 -*-
"""生成 data/bigbath.json: 商誉/资产减值"洗大澡"股(预亏巨亏+减值→次年低基数暴增反弹)。
验证 gate_commit_bigbath: 预告披露后60日-1.43%(利空消化)→120日+6.15%/250日+3.91%(先抑后扬)。
源 tushare forecast(预减/首亏/续亏 + 减值关键词 + 净利降>100%)。⚠️需忍前60天下跌, 中线(120日)反弹。
跑: D:/anaconda3/python.exe scripts/export_bigbath.py
"""
import os, json, datetime
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "bigbath.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    import pandas as pd
    import pickle
    today = datetime.date.today()
    # 股票简称map (tushare stock_basic)
    name_map = {}
    try:
        import tushare as ts
        tok = os.environ.get("TUSHARE_TOKEN") or open(os.path.join(DATA, ".tushare_token")).read().strip()
        pro = ts.pro_api(tok)
        sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
        name_map = dict(zip(sb['ts_code'], sb['name']))
    except Exception as e:
        print(f"[bigbath] 股票简称map拉取失败(用代码代替): {e}")
    # tushare forecast(period=)被拒, 用 _forecast_1000.pkl 缓存(csi1000, PC侧; runup同源)
    items = []
    BAD = {'预减', '首亏', '续亏', '增亏'}
    seen = set()
    forecast_cache = os.environ.get("QI_FORECAST_CACHE", r"C:\rdagent\_forecast_1000.pkl")
    try:
        with open(forecast_cache, 'rb') as stream:
            cached = pickle.load(stream)
        if not isinstance(cached, dict) or not isinstance(cached.get('forecast'), dict):
            raise ValueError("forecast cache must contain a forecast mapping")
        fcd = cached['forecast']
        if not fcd:
            raise ValueError("forecast mapping is empty")
    except Exception as e:
        raise RuntimeError(f"forecast缓存不可用: {forecast_cache}: {e}") from e
    cutoff = (today - datetime.timedelta(days=300)).strftime('%Y%m%d')   # 近300天的洗大澡(覆盖反弹窗250日)
    for tscode, df in fcd.items():
        if df is None or not len(df) or tscode in seen:
            continue
        for r in df.itertuples(index=False):
            typ = str(getattr(r, "type", "")); reason = str(getattr(r, "change_reason", "") or "") + str(getattr(r, "summary", "") or "")
            pmin = getattr(r, "p_change_min", None)
            ad = getattr(r, "first_ann_date", None) or getattr(r, "ann_date", None)
            code = str(tscode)[:6]
            if typ in BAD and ('减值' in reason or '商誉' in reason) and pmin is not None and pmin < -100:
                if tscode in seen or not ad or pd.isna(ad):
                    continue
                ad = str(int(float(ad)))
                if ad < cutoff:
                    continue
                ad_iso = ad[:4] + "-" + ad[4:6] + "-" + ad[6:8]
                try:
                    days = (today - datetime.date.fromisoformat(ad_iso)).days
                except Exception:
                    days = 0
                seen.add(tscode)
                items.append({"code": code, "ts_code": tscode, "name": name_map.get(tscode, ""),
                              "ann_date": ad_iso, "days_since": days,
                              "type": typ, "p_change_min": float(pmin),
                              "reason": str(getattr(r, "summary", "") or "")[:30],
                              "in_rebound_window": 50 <= days <= 250})
                break
    items = sorted(items, key=lambda x: x['ann_date'], reverse=True)
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today.strftime("%Y-%m-%d"),
           "n": len(items), "n_rebound": sum(1 for x in items if x.get("in_rebound_window")), "items": items[:80],
           "source_health": {"forecast_codes": len(fcd), "forecast_cache": forecast_cache},
           "note": "商誉/资产减值洗大澡股。验证: 预告披露后先抑(60日-1.43%)后扬(120日+6.15%/250日+3.91%)。⚠️减值非现金, 次年低基数暴增→机构追捧反弹; 但需忍前60天下跌(接飞刀)。反弹窗=披露后约50-250日。仅现金流为正的才是真洗澡(主营未损)。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "bigbath.json"))
    except Exception as e:
        print(f"[bigbath] 拷NAS失败: {e}")
    print(f"[bigbath] 洗大澡 {len(items)}只(反弹窗{out['n_rebound']}) -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
