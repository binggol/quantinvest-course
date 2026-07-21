# -*- coding: utf-8 -*-
"""生成 data/asset_injection.json，供定增生命周期公告核验使用。

口径: akshare stock_qbzf_em 定向增发 + 数据源披露锁定期属于 {3年,5年}，
且上市后约 250 个交易日（380 个自然日）内。锁定期是逐笔交易条款，
不是跨时期、跨交易类型通用的法定期限；最终事件日期以对应公告证据为准。
跑: D:/anaconda3/python.exe scripts/export_asset_injection.py
"""
import os, json, datetime
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "asset_injection.json")


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    import akshare as ak
    import pandas as pd
    today = datetime.date.today()
    df = ak.stock_qbzf_em()
    dz = df[df['发行方式'].astype(str).str.contains('定向|非公开', na=False)].copy()
    dz['发行日'] = pd.to_datetime(dz['发行日期'], errors='coerce')
    dz['上市日'] = pd.to_datetime(dz['增发上市日期'], errors='coerce')
    dz['lk'] = dz['锁定期'].astype(str)
    dz = dz.dropna(subset=['上市日'])
    cut = pd.Timestamp(today - datetime.timedelta(days=380))   # 漂移窗口≈250交易日
    az = dz[(dz['lk'].isin(['3年', '5年'])) & (dz['上市日'] >= cut) & (dz['上市日'] <= pd.Timestamp(today))]
    az = az.sort_values('上市日', ascending=False)
    items = []
    for r in az.itertuples(index=False):
        code = str(r.股票代码).zfill(6)
        ts_code = code + (".SH" if code[0] == '6' else ".SZ")
        days = (today - r.上市日.date()).days
        issue_date = str(r.发行日)[:10] if pd.notna(r.发行日) else ""
        item = {"code": code, "ts_code": ts_code, "name": str(r.股票简称),
                "issue_date": issue_date,
                "list_date": str(r.上市日)[:10], "days_since": days,
                "issue_price": float(r.发行价格) if pd.notna(r.发行价格) else None,
                "lock": r.lk, "lock_period": r.lk, "in_window": days <= 380}
        if issue_date:
            item["issue_date_source"] = "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE"
        items.append(item)
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today.strftime("%Y-%m-%d"),
           "n": len(items), "items": items,
           "note": "资产注入型定增候选样本；3年/5年为数据源逐笔披露的交易条款，不代表跨时期、跨交易类型的统一期限。历史回测标签仅用于样本筛选，实际阶段及期限以对应制度和公告证据综合核验。"}
    tmp_out = OUT + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as stream:
        json.dump(out, stream, ensure_ascii=False, indent=1)
    os.replace(tmp_out, OUT)
    print(f"[asset_injection] 资产注入型定增 {len(items)}只(窗口内) -> {OUT}")


if __name__ == "__main__":
    main()
