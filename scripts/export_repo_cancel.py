# -*- coding: utf-8 -*-
"""生成 data/repo_cancel.json: 注销型回购清单(回购腿提纯=只/优先保留注销型)。
验证 gate_repo_purpose.py: 注销型回购 公告后60日超额+3.09%(vs 全部回购+2.07%, 提纯多1点)/120日+8.62%。
机制: 注销=总股本减少=EPS被动提升, 真金白银利好 > 股权激励型(变相补贴管理层)。2024新国九条后A股回购多为注销式。
源: 巨潮cninfo公告标题(含'回购股份用于注销'), 关键词分类目的, 不用读全文PDF。
跑: D:/anaconda3/python.exe scripts/export_repo_cancel.py
"""
import os, json, datetime
try:
    from scripts.cninfo_query import query_announcements
except ImportError:  # direct script execution
    from cninfo_query import query_announcements
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "repo_cancel.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def cninfo_search(keyword, sedate, column):
    out = []
    for a in query_announcements(
        keyword, sedate, column, max_pages=14, pause=0.7
    ):
        code = str(a.get('secCode', ''))[:6]
        t = a.get('announcementTime')
        try:
            ad = datetime.datetime.utcfromtimestamp(t / 1000).strftime('%Y-%m-%d')
        except Exception:
            ad = None
        adjunct = str(a.get('adjunctUrl') or '')
        announcement_id = str(a.get('announcementId') or '')
        url = ('https://static.cninfo.com.cn/' + adjunct.lstrip('/')) if adjunct else ''
        out.append((code, ad, a.get('secName', ''), a.get('announcementTitle', ''),
                    announcement_id, url))
    return out


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    today = datetime.date.today()
    # 近120天的注销型回购(覆盖回购腿60日持有窗口+前瞻)
    start = (today - datetime.timedelta(days=120)).strftime('%Y-%m-%d')
    sedate = f"{start}~{today.strftime('%Y-%m-%d')}"
    res = []
    for column in ('szse', 'sse'):
        res.extend(cninfo_search('回购股份用于注销', sedate, column))
    seen = {}
    for code, ad, name, title, announcement_id, url in res:
        if not (ad and code and code[0] in '036'):
            continue
        # 排除"进展/结果/完成"类的重复, 取每股最早一次注销公告
        ts_code = code + (".SH" if code[0] == '6' else ".SZ")
        if ts_code not in seen or ad < seen[ts_code]['ann_date']:
            seen[ts_code] = {"code": code, "ts_code": ts_code, "name": name, "ann_date": ad,
                             "title": title[:80], "announcement_id": announcement_id,
                             "announcement_url": url, "source": "巨潮资讯"}
    items = sorted(seen.values(), key=lambda x: x['ann_date'], reverse=True)
    # 标"在60日持有窗口内"(公告后60交易日≈84自然日内)
    for it in items:
        try:
            days = (today - datetime.date.fromisoformat(it['ann_date'])).days
            it['days_since'] = days
            it['in_window'] = days <= 90   # 粗略60交易日窗
        except Exception:
            it['in_window'] = False
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "as_of": today.strftime("%Y-%m-%d"), "n": len(items),
           "n_in_window": sum(1 for x in items if x.get("in_window")),
           "items": items,
           "note": "注销型回购(cninfo公告标题分类)。验证: 注销型公告后60日+3.09%(全部回购+2.07%)/120日+8.62%。注销=总股本减→EPS被动升=真利好, 强于股权激励型。回购腿优先此名单。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "repo_cancel.json"))
    except Exception as e:
        print(f"[repo_cancel] 拷NAS失败: {e}")
    print(f"[repo_cancel] {sedate} 注销型回购 {len(items)}只(窗口内{out['n_in_window']}) -> {OUT}")


if __name__ == "__main__":
    main()
