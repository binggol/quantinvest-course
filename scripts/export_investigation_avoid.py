# -*- coding: utf-8 -*-
"""生成 data/investigation_avoid.json: 证监会立案调查股=A股最强死亡信号(一票否决硬黑名单)。
验证 gate_investigation.py: 立案后20日-7.96%/60日-9.69%/120日-8.67%, 胜率28-35%, 120日崩盘率53%(回撤>30%)。
机制: 收到证监会《立案告知书》→后续ST/退市/巨亏。源 巨潮cninfo公告标题关键词。
跑: D:/anaconda3/python.exe scripts/export_investigation_avoid.py
"""
import os, json, datetime
try:
    from scripts.cninfo_query import query_announcements
except ImportError:  # direct script execution
    from cninfo_query import query_announcements
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "investigation_avoid.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def cninfo_search(keyword, sedate, column):
    out = []
    for a in query_announcements(
        keyword, sedate, column, max_pages=19, pause=0.7
    ):
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
    # 立案120交易日内有效(黑名单期). 拉近200自然日
    start = (today - datetime.timedelta(days=200)).strftime('%Y-%m-%d')
    sedate = f"{start}~{today.strftime('%Y-%m-%d')}"
    seen = {}
    for kw in ['立案告知书', '涉嫌违法违规被立案', '收到立案调查']:
        for column in ('szse', 'sse'):
            for code, ad, name, title in cninfo_search(kw, sedate, column):
                if not (ad and code and code[0] in '036'):
                    continue
                if not ('立案' in title and ('告知书' in title or '被立案' in title or '立案调查' in title)):
                    continue
                ts_code = code + (".SH" if code[0] == '6' else ".SZ")
                if ts_code not in seen or ad < seen[ts_code]['ann_date']:
                    seen[ts_code] = {"code": code, "ts_code": ts_code, "name": name, "ann_date": ad, "title": title[:40]}
    items = sorted(seen.values(), key=lambda x: x['ann_date'], reverse=True)
    for it in items:
        try:
            days = (today - datetime.date.fromisoformat(it['ann_date'])).days
            it['days_since'] = days
            it['in_blacklist'] = days <= 175   # 约120交易日黑名单期
        except Exception:
            it['in_blacklist'] = True
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today.strftime("%Y-%m-%d"),
           "n": len(items), "n_blacklist": sum(1 for x in items if x.get("in_blacklist")), "items": items,
           "note": "证监会立案调查股=最强死亡信号(一票否决)。验证: 立案后120日超额-8.67%/崩盘率53%/胜35%。120交易日内全局买入黑名单, 持仓应清。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "investigation_avoid.json"))
    except Exception as e:
        print(f"[investigation_avoid] 拷NAS失败: {e}")
    print(f"[investigation_avoid] {sedate} 立案 {len(items)}只(黑名单期{out['n_blacklist']}) -> {OUT}")


if __name__ == "__main__":
    main()
