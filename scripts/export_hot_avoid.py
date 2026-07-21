"""
热榜避雷清单(落地): 同花顺热榜"关注度反转"信号 → 当前上榜股=情绪透支, 系统性跑输市场。
验证(2026-06-19 test_attention_factor.py): 上热榜后未来5/10/20日相对全市场EW超额 -0.85/-1.92/-3.19%,
t=-6~-14 极显著, 单调(越久越跌), 关注度越高越跌(Top20<全热股), 高换手非微盘陷阱→信号干净。
A股个股难融券→可落地形态=从多头持仓/买入候选里"剔除", 不是做空。

本脚本: 拉最近 WINDOW 个交易日的 ths_hot(data_type='热股', 仅A股), 算每只"连续上榜天数",
出 data/hot_avoid.json 供 /avoid 页(双避雷)展示, 并被 export_combo_holdings.py 用作长腿过滤。
跑: D:/anaconda3/python.exe scripts/export_hot_avoid.py
"""
import os, io, sys, re, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(k, None)
os.environ['no_proxy'] = '*'
import pandas as pd
import tushare as ts
from datetime import datetime

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA, "hot_avoid.json")
WINDOW = int(os.environ.get("HOT_WINDOW", "12"))   # 回看交易日数, 算连续上榜
A_RE = re.compile(r"^\d{6}\.(SZ|SH|BJ)$")           # 仅A股(剔HK/US/行业板块/概念/ETF)


def main():
    pro = ts.pro_api(get_tushare_token())
    # 真A股集合 + 权威名字: 热榜里混入的商品期货(焦煤2609/沪银2608)ts_code会撞真股代码(002609.SZ=捷顺科技),
    # 仅靠代码拦不住→必须用 stock_basic 名字交叉核对, 名字对不上=数据撞码, 丢弃。
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    name_of = dict(zip(sb['ts_code'], sb['name']))
    valid = set(name_of)
    print(f"  stock_basic 真A股 {len(valid)} 只")
    # 最近 WINDOW+缓冲 个交易日
    cal = pro.trade_cal(exchange='', start_date='20260101', end_date='20271231', is_open='1')
    open_days = sorted(cal['cal_date'].tolist())
    today = datetime.now().strftime("%Y%m%d")
    open_days = [d for d in open_days if d <= today][-WINDOW:]
    if not open_days:
        print("[hot_avoid] 无交易日"); return

    # 逐日拉热股(A股), 记 code->(rank,name,pct,concept)
    per_day = {}           # date -> {code: info}
    for ds in open_days:
        try:
            h = pro.ths_hot(trade_date=ds, data_type='热股')
        except Exception as e:
            print(f"  {ds} 拉取失败 {e}"); continue
        if h is None or not len(h):
            continue
        h = h[h['ts_code'].astype(str).str.match(A_RE) & h['ts_code'].isin(valid)].copy()
        if not len(h):
            continue
        # 名字交叉核对: 丢掉撞码的期货(stock_basic名字≠热榜名字)
        h = h[h.apply(lambda r: str(r['ts_name']).strip() == name_of.get(r['ts_code'], '').strip(), axis=1)]
        if not len(h):
            continue
        # 同代码可能多概念多行, 取rank最小(最热)
        h = h.sort_values('rank').drop_duplicates('ts_code', keep='first')
        d = {}
        for _, r in h.iterrows():
            d[r['ts_code']] = {
                "name": name_of.get(r['ts_code'], str(r.get('ts_name', ''))),
                "rank": int(r['rank']) if pd.notna(r['rank']) else 999,
                "pct": round(float(r['pct_change']), 2) if pd.notna(r.get('pct_change')) else None,
                "concept": str(r['concept']) if pd.notna(r.get('concept')) and str(r['concept']) not in ('None', 'nan') else "",
            }
        per_day[ds] = d
        print(f"  {ds} 热股A股 {len(d)} 只")

    if not per_day:
        print("[hot_avoid] 窗口内无热榜数据"); return

    as_of = max(per_day.keys())
    today_hot = per_day[as_of]
    # 连续/累计上榜天数(窗口内出现次数)
    cnt = {}
    for ds, d in per_day.items():
        for c in d:
            cnt[c] = cnt.get(c, 0) + 1

    items = []
    for code, info in today_hot.items():
        items.append({
            "code": code,
            "name": info["name"],
            "rank": info["rank"],
            "pct": info["pct"],
            "days_on": cnt.get(code, 1),
            "top20": info["rank"] <= 20,           # 高关注度=跌更狠(Top20超额-4.4%/t=-11.5)
            "concept": info["concept"][:40],
        })
    # 排序: Top20优先, 再按连续天数, 再按rank
    items.sort(key=lambda x: (not x["top20"], -x["days_on"], x["rank"]))

    out = {
        "as_of": as_of,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window_days": len(per_day),
        "n": len(items),
        "n_top20": sum(1 for x in items if x["top20"]),
        "items": items,
        "note": "上同花顺热榜=散户关注度透支, 未来5/10/20日系统性跑输全市场(超额-0.85/-1.92/-3.19%, t=-6~-14, 越久越跌)。可落地=持仓/买入候选剔除上榜股, 非做空(A股难融券)。Top20=高关注度跌更狠; 连续上榜天数越多情绪越透支。样本2024-2026(2.5年单区间), 待更长OOS。",
        "message": "",
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 留痕: 每个 as_of 追加一份当日热榜快照(供后续分析"标红的票后来是否真跑输")
    HIST = os.path.join(DATA, "hot_avoid_history.json")
    try:
        hist = json.load(open(HIST, encoding="utf-8")) if os.path.exists(HIST) else []
    except Exception:
        hist = []
    if not isinstance(hist, list):
        hist = []
    if not any(h.get("as_of") == as_of for h in hist):
        hist.append({"as_of": as_of, "updated": out["updated"], "n": len(items),
                     "n_top20": out["n_top20"],
                     "codes": [{"code": x["code"], "name": x["name"], "rank": x["rank"], "top20": x["top20"]} for x in items]})
        hist = hist[-250:]  # 约一年交易日
        json.dump(hist, open(HIST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[hot_avoid] 留痕快照 {as_of} ({len(items)}只) -> {HIST} (累计{len(hist)}日)")

    print(f"[hot_avoid] as_of={as_of} 当前热榜A股 {len(items)} 只(Top20 {out['n_top20']}只) -> {OUT}")
    for x in items[:12]:
        print(f"  {x['code']} {x['name']:8} rank{x['rank']:>3} 连续{x['days_on']}日 {x['pct'] if x['pct'] is not None else '':>6} {x['concept']}")


if __name__ == "__main__":
    main()
