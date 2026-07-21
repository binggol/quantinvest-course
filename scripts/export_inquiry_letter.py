# -*- coding: utf-8 -*-
"""生成 data/inquiry_letter.json: 收到问询函/关注函=监管负面信号(避雷)。
验证 gate_scan_verify(自动扫描器筛出+精测): 收到问询函公告后20日-4.38%/中位-5.24%/胜31%, 2023/24/25三年全负(-3.7/-5.3/-4.0)。
比立案温和(立案-9%)但更高频。源 巨潮cninfo公告标题"问询函/关注函"。20交易日避雷窗。
跑: D:/anaconda3/python.exe scripts/export_inquiry_letter.py
"""
import os, json, datetime
try:
    from scripts.cninfo_query import query_announcements
except ImportError:  # direct script execution
    from cninfo_query import query_announcements
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "inquiry_letter.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def cninfo(kw, sedate, col):
    out = []
    for a in query_announcements(kw, sedate, col, max_pages=13, pause=0.5):
        code = str(a.get('secCode', ''))[:6]; t = a.get('announcementTime'); ti = a.get('announcementTitle', '')
        try:
            ad = datetime.datetime.utcfromtimestamp(t / 1000).strftime('%Y-%m-%d')
        except Exception:
            ad = None
        out.append((code, ad, a.get('secName', ''), ti))
    return out


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=45)).strftime('%Y-%m-%d')   # 20交易日避雷窗≈30自然日, 拉45天
    sedate = f"{start}~{today.strftime('%Y-%m-%d')}"
    seen = {}
    for col in ['szse', 'sse']:
        for code, ad, name, title in cninfo('问询函', sedate, col):
            if not (ad and code and code[0] in '036'):
                continue
            if not ('问询函' in title or '关注函' in title):
                continue
            # 排除"回复问询函"(回复≠新收到, 利空已消化部分)? 保留"收到/关于...问询函"
            if '回复' in title and '收到' not in title:
                continue
            ts_code = code + (".SH" if code[0] == '6' else ".SZ")
            if ts_code not in seen or ad > seen[ts_code]['ann_date']:
                seen[ts_code] = {"code": code, "ts_code": ts_code, "name": name, "ann_date": ad, "title": title[:40]}
    items = sorted(seen.values(), key=lambda x: x['ann_date'], reverse=True)
    for it in items:
        try:
            days = (today - datetime.date.fromisoformat(it['ann_date'])).days
            it['days_since'] = days; it['in_window'] = days <= 30   # ~20交易日避雷窗
        except Exception:
            it['in_window'] = True
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today.strftime("%Y-%m-%d"),
           "n": len(items), "n_window": sum(1 for x in items if x.get("in_window")), "items": items,
           "note": "收到监管问询函/关注函=负面信号(避雷)。验证: 公告后20日-4.38%/中位-5.24%/胜31%, 三年全负。比立案(-9%)温和但更高频。20交易日内避免/减仓。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "inquiry_letter.json"))
    except Exception as e:
        print(f"[inquiry_letter] 拷NAS失败: {e}")
    print(f"[inquiry_letter] 问询函 {len(items)}只(窗口内{out['n_window']}) -> {OUT}")


if __name__ == "__main__":
    main()
