# -*- coding: utf-8 -*-
"""生成 data/commit_nosell.json: 大股东/实控人主动承诺不减持=低估信号(事件腿)。
验证 gate_commit_bigbath: 承诺不减持公告后20日+1.37%/60日+3.25%/胜52%(120日衰减,60日窗口最佳)。
源 巨潮cninfo公告标题"承诺不减持"。持有~60交易日。
跑: D:/anaconda3/python.exe scripts/export_commit_nosell.py
"""
import os, json, datetime
try:
    from scripts.cninfo_query import query_announcements
except ImportError:  # direct script execution
    from cninfo_query import query_announcements
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "commit_nosell.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def cninfo(kw, sedate, col):
    out = []
    for a in query_announcements(kw, sedate, col, max_pages=11, pause=0.5):
        code = str(a.get('secCode', ''))[:6]; t = a.get('announcementTime'); title = a.get('announcementTitle', '')
        try:
            ad = datetime.datetime.utcfromtimestamp(t / 1000).strftime('%Y-%m-%d')
        except Exception:
            ad = None
        out.append((code, ad, a.get('secName', ''), title))
    return out


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=100)).strftime('%Y-%m-%d')   # 近100天(覆盖60交易日持有窗口)
    sedate = f"{start}~{today.strftime('%Y-%m-%d')}"
    seen = {}
    for col in ['szse', 'sse']:
        for code, ad, name, title in cninfo('承诺不减持', sedate, col):
            if not (ad and code and code[0] in '036'):
                continue
            if not ('不减持' in title and '承诺' in title):
                continue
            ts_code = code + (".SH" if code[0] == '6' else ".SZ")
            if ts_code not in seen or ad > seen[ts_code]['ann_date']:
                seen[ts_code] = {"code": code, "ts_code": ts_code, "name": name, "ann_date": ad, "title": title[:40]}
    items = sorted(seen.values(), key=lambda x: x['ann_date'], reverse=True)
    for it in items:
        try:
            days = (today - datetime.date.fromisoformat(it['ann_date'])).days
            it['days_since'] = days; it['in_window'] = days <= 90   # ~60交易日窗口
        except Exception:
            it['in_window'] = True
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today.strftime("%Y-%m-%d"),
           "n": len(items), "n_window": sum(1 for x in items if x.get("in_window")), "items": items,
           "note": "大股东/实控人主动承诺不减持=低估信号。验证: 公告后60日+3.25%/胜52%(20日+1.37%, 120日衰减)。持有~60交易日。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "commit_nosell.json"))
    except Exception as e:
        print(f"[commit_nosell] 拷NAS失败: {e}")
    print(f"[commit_nosell] 承诺不减持 {len(items)}只(窗口内{out['n_window']}) -> {OUT}")


if __name__ == "__main__":
    main()
