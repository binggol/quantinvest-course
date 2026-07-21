"""
MSCI/富时罗素 中国指数 A股新增 半自动抓取 → data/upcoming_inclusions.json (供 export_index_inclusion_pro 真抢跑用)。

为何半自动: MSCI/富时的成分调整发在 press release(新闻稿/PDF), 只在生效前约2周公布, URL每期变、
A股部分埋在英文正文里。全自动"搜索→定位正确公告→解析"在无人值守管线里不可靠。
故采用: 你在 data/foreign_inclusion_sources.json 配置 [{url, index_name, effective_date}],
本脚本自动 fetch 每个URL、用正则抽出 6位A股代码+名称(对 stock_basic 校验)、写入 upcoming_inclusions.json。
=> 省掉手工逐只录入; 每期只需贴一个公告URL。

跑: python scripts/fetch_foreign_inclusion.py
"""
import os, re, json, time

for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'; os.environ['NO_PROXY'] = '*'

import requests
import tushare as ts

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

DATA_DIR = os.path.dirname(os.path.abspath(__file__)) + "/../data"
SOURCES = os.path.join(DATA_DIR, "foreign_inclusion_sources.json")  # 你配置的公告URL
UPCOMING = os.path.join(DATA_DIR, "upcoming_inclusions.json")        # 输出(pro脚本读)
# 公告里A股可能写成 "600519.SS"/"600519 CH"/"贵州茅台"/"600519.SH" 等; 统一成 ts_code.
CODE_RE = re.compile(r'\b(\d{6})\b')


def to_ts(c6):
    return f"{c6}.SH" if c6[0] in ('5', '6', '9') else (f"{c6}.SZ" if c6[0] in ('0', '3') else (f"{c6}.BJ" if c6[0] in ('4', '8') else None))


def main():
    if not os.path.exists(SOURCES):
        # 给个模板, 方便你填
        tmpl = [{"url": "https://www.msci.com/.../press-release", "index_name": "MSCI中国", "effective_date": "2026-08-26", "_note": "把url换成官方公告页, effective_date=生效日"}]
        json.dump(tmpl, open(SOURCES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[foreign] 已生成模板 {SOURCES}, 请填入公告URL后重跑")
        return
    srcs = json.load(open(SOURCES, encoding="utf-8"))
    pro = ts.pro_api(get_tushare_token())
    nb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    valid = set(nb['ts_code']); name2ts = dict(zip(nb['name'], nb['ts_code']))

    out = []
    for s in srcs:
        url = s.get("url", "")
        if not url.startswith("http"):
            continue
        idx = s.get("index_name", "境外指数"); eff = s.get("effective_date", "")
        if not eff:
            print(f"[foreign] {idx} 缺 effective_date, 跳过"); continue
        try:
            html = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
        except Exception as e:
            print(f"[foreign] {idx} 抓取失败 {url}: {e}"); continue
        found = set()
        # ① 6位代码
        for m in CODE_RE.findall(html):
            t = to_ts(m)
            if t and t in valid:
                found.add(t)
        # ② 中文简称(公告若含中文名)
        for nm, t in name2ts.items():
            if len(nm) >= 3 and nm in html:
                found.add(t)
        for t in found:
            out.append({"ts_code": t, "index": idx, "inclusion_date": eff, "src": "foreign_auto"})
        print(f"[foreign] {idx} ({eff}): 解析出 {len(found)} 只A股  <- {url[:60]}")

    if not out:
        print("[foreign] 未解析到A股(检查URL是否含成分明细/是否PDF). 未改 upcoming_inclusions.json")
        return
    # 合并进 upcoming(保留已有手工条目, 去重)
    cur = []
    if os.path.exists(UPCOMING):
        try: cur = json.load(open(UPCOMING, encoding="utf-8"))
        except Exception: cur = []
    seen = {(e.get("ts_code"), e.get("inclusion_date")) for e in cur}
    for e in out:
        if (e["ts_code"], e["inclusion_date"]) not in seen:
            cur.append(e)
    json.dump(cur, open(UPCOMING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[foreign] 写入 {UPCOMING}: 新增{len(out)}条, 共{len(cur)}条")


if __name__ == "__main__":
    main()
