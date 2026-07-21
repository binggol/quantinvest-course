"""
预测下一期指数调入(准成分) + 每日记录 + 复现度 + 生效时对比预测vs真实命中率。

原理: 宽基指数(沪深300/中证500/上证50)按自由流通市值排名选成分, 调入候选 = 下一档指数里
市值已排进目标指数 top-N 的票。每天预测、记历史, 连续多日上榜=高置信(页面深色)。每次调仓
生效后, 用真实新增(index_weight差分)对比之前的预测, 算命中率(precision/recall)。

跑: python scripts/predict_inclusion.py
输出: data/inclusion_predict.json (当前预测) + data/inclusion_predict_history.json (每日台账)
"""
import os, json, time, calendar
from datetime import datetime

for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'; os.environ['NO_PROXY'] = '*'

import pandas as pd
import tushare as ts

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

DATA_DIR = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA_DIR, "inclusion_predict.json")
HIST = os.path.join(DATA_DIR, "inclusion_predict_history.json")
TODAY = os.environ.get("INCLUSION_TODAY", datetime.now().strftime("%Y-%m-%d"))
pro = None
BUFFER = 0.97  # 缓冲: 候选需排进 top N*BUFFER 才算调入(提精度, 模拟中证缓冲区)

# 目标指数 <- 候选来源池(下一档). 上证50只取沪市.
TARGETS = [
    {"code": "000016.SH", "name": "上证50", "N": 50, "pool": ["000016.SH", "000300.SH"], "sse_only": True, "months": [6, 12]},
    {"code": "000300.SH", "name": "沪深300", "N": 300, "pool": ["000300.SH", "000905.SH"], "months": [6, 12]},
    {"code": "000905.SH", "name": "中证500", "N": 500, "pool": ["000905.SH", "000852.SH"], "months": [6, 12]},
]


def safe(fn, *a, **k):
    for _ in range(4):
        try: return fn(*a, **k)
        except Exception: time.sleep(2)
    return None


def to_ts(c6):
    c6 = str(c6).zfill(6)
    return f"{c6}.SH" if c6[0] in ('5', '6', '9') else (f"{c6}.SZ" if c6[0] in ('0', '3') else (f"{c6}.BJ" if c6[0] in ('4', '8') else None))


def members_fresh(code):
    """实时成分: akshare 直连中证官网(csindex), 比 tushare 月度成分快(当天反映调仓). 返回(set ts_code, 日期, 源)."""
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol=code[:6])
        col = '成分券代码' if '成分券代码' in df.columns else df.columns[4]
        s = {to_ts(c) for c in df[col]}
        s.discard(None)
        d = str(df['日期'].iloc[0]).replace("-", "") if '日期' in df.columns else None
        if s:
            return s, d, "中证官网"
    except Exception as e:
        print(f"[predict] akshare {code} 失败, 回退tushare: {str(e)[:60]}")
    return set(), None, None


def members(code, snap=-1):
    """tushare index_weight 倒数第 |snap| 期成分 (回退/取上次调仓前基准用). snap=-1最新."""
    w = safe(pro.index_weight, index_code=code, start_date=f"{int(TODAY[:4]) - 1}{TODAY[5:7]}01", end_date=TODAY.replace("-", ""))
    if w is None or w.empty: return set(), None
    dts = sorted(w['trade_date'].unique())
    if len(dts) < abs(snap): return set(), None
    d = dts[snap]
    return set(w[w['trade_date'] == d]['con_code']), d


def members_before(code, before_ymd):
    """tushare 中早于 before_ymd 的最近一期成分(算最近调入明细/复现基准, 即上次调仓前的旧成分)."""
    w = safe(pro.index_weight, index_code=code, start_date=f"{int(before_ymd[:4]) - 1}0101", end_date=TODAY.replace("-", ""))
    if w is None or w.empty: return set(), None
    dts = [d for d in sorted(w['trade_date'].unique()) if d < before_ymd]
    if not dts: return set(), None
    return set(w[w['trade_date'] == dts[-1]]['con_code']), dts[-1]


def next_effective(months):
    """下一次调仓生效日(2nd Friday次一交易日): 今天之后最近一个调仓月. cal从年初取以正确解析."""
    cal = safe(pro.trade_cal, exchange='SSE', start_date=f"{int(TODAY[:4])}0101", end_date=f"{int(TODAY[:4]) + 1}1231", is_open='1')
    tds = sorted(cal['cal_date'].tolist()) if cal is not None else []
    tymd = TODAY.replace("-", "")
    cands = []
    for y in (int(TODAY[:4]), int(TODAY[:4]) + 1):
        for m in months:
            fr = [w[4] for w in calendar.monthcalendar(y, m) if w[4]]
            sf = f"{y}{m:02d}{fr[1]:02d}"            # 第二个周五
            fut = [d for d in tds if d > sf]          # 其后第一个交易日 = 生效日
            if fut and fut[0] > tymd:
                cands.append(fut[0])
    cands.sort()
    return f"{cands[0][:4]}-{cands[0][4:6]}-{cands[0][6:]}" if cands else None


def last_effective(months):
    """最近一次已生效的调仓日(≤今天)."""
    cal = safe(pro.trade_cal, exchange='SSE', start_date=f"{int(TODAY[:4]) - 1}0101", end_date=TODAY.replace("-", ""), is_open='1')
    tds = sorted(cal['cal_date'].tolist()) if cal is not None else []
    tymd = TODAY.replace("-", "")
    past = []
    for y in (int(TODAY[:4]), int(TODAY[:4]) - 1):
        for m in months:
            fr = [w[4] for w in calendar.monthcalendar(y, m) if w[4]]
            sf = f"{y}{m:02d}{fr[1]:02d}"
            fut = [d for d in tds if d > sf]
            if fut and fut[0] <= tymd:
                past.append(fut[0])
    past.sort()
    return f"{past[-1][:4]}-{past[-1][4:6]}-{past[-1][6:]}" if past else None


def main():
    global pro
    pro = ts.pro_api(get_tushare_token())
    # 最近交易日 circ_mv (自由流通市值): 当日EOD可能未出, 回退到最近有数据的交易日
    cal = safe(pro.trade_cal, exchange='SSE', start_date=f"{int(TODAY[:4])}0101", end_date=TODAY.replace("-", ""), is_open='1')
    tds_year = sorted(cal['cal_date'].tolist()) if cal is not None and len(cal) else [TODAY.replace("-", "")]
    cap = {}
    for d in reversed(tds_year[-6:]):  # 最多回退6个交易日
        db = safe(pro.daily_basic, trade_date=d, fields='ts_code,circ_mv')
        if db is not None and len(db):
            cap = {r.ts_code: r.circ_mv for r in db.itertuples(index=False)}
            print(f"[predict] circ_mv 取自 {d} ({len(cap)}只)")
            break
    nb = safe(pro.stock_basic, exchange='', list_status='L', fields='ts_code,name')
    names = dict(zip(nb['ts_code'], nb['name'])) if nb is not None else {}

    hist = {}
    if os.path.exists(HIST):
        try: hist = json.load(open(HIST, encoding='utf-8'))
        except Exception: hist = {}

    result = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "today": TODAY, "indices": []}
    today_log = {}

    for t in TARGETS:
        # 现成分: 优先 akshare 中证官网(实时, 当天反映调仓), 失败回退 tushare(月度滞后)
        cur, cur_d, src = members_fresh(t["code"])
        if not cur:
            cur, cur_d = members(t["code"], -1); src = "tushare(滞后)"
        if not cur:
            continue
        last_eff = last_effective(t["months"])
        # 上次调仓前旧成分(tushare), 算"最近调入明细"+复现基准
        prev, prev_d = members_before(t["code"], last_eff.replace("-", "")) if last_eff else (set(), None)
        pool = set()
        for p in t["pool"]:
            m, _, _ = members_fresh(p)
            if not m:
                m, _ = members(p, -1)
            pool |= m
        if t.get("sse_only"):
            pool = {c for c in pool if c.endswith(".SH")}
        pool = [c for c in pool if c in cap]
        ranked = sorted(pool, key=lambda c: cap.get(c, 0), reverse=True)
        rank = {c: i + 1 for i, c in enumerate(ranked)}
        N = t["N"]
        # 候选调入 = 非现成分中市值排名 ≤ N*1.12 (top-down, 不排除靠前; 成分实时后靠前的非成分本就罕见)
        adds = [c for c in ranked[:int(N * 1.12)] if c not in cur]
        drops = [c for c in cur if c in rank and rank[c] > int(N * 1.05)]
        eff = next_effective(t["months"])
        # 成分源是否滞后(回退tushare且快照早于上次生效)→只做文字提醒, 不抑制候选
        stale = bool(src and "滞后" in src and last_eff and cur_d and cur_d < last_eff.replace("-", ""))
        # 最近一次调入明细 = 实时成分 − 上次调仓前旧成分
        added_codes = (cur - prev) if prev else set()
        last_review = {"date": last_eff, "src": src, "stale": stale,
                       "added": [{"code": c, "name": names.get(c, "")} for c in added_codes]}

        today_log[t["name"]] = {"adds": adds, "drops": drops}

        # 复现度: 自上次调仓生效(last_eff)以来本轮各候选在历史里出现的天数
        since = last_eff or "2000-01-01"
        rel_dates = [d for d in hist if d > since]
        def seen(code):
            return 1 + sum(1 for d in rel_dates if code in (hist[d].get(t["name"], {}).get("adds", [])))
        tot = len(rel_dates) + 1
        cand = [{"code": c, "name": names.get(c, ""), "circ_mv": round(cap.get(c, 0) / 1e4, 1),  # 亿元
                 "rank": rank.get(c), "in_zone": (rank.get(c, 9999) <= N), "days_seen": seen(c),
                 "total_days": tot, "conf": round(seen(c) / tot, 2)} for c in adds]
        cand.sort(key=lambda x: (not x["in_zone"], -x["conf"], x["rank"] or 9999))

        # 命中率: 若刚发生过调仓(cur_d != prev_d 且 prev_d 之后真实新增), 对比之前预测
        acc = None
        if prev and cur != prev:
            actual_adds = cur - prev
            # 用紧邻上次生效前的最后一条预测
            preds_before = [d for d in hist if d <= since]
            if preds_before and actual_adds:
                lastpred = set(hist[max(preds_before)].get(t["name"], {}).get("adds", []))
                if lastpred:
                    hit = lastpred & actual_adds
                    acc = {"predicted": len(lastpred), "actual": len(actual_adds), "hit": len(hit),
                           "precision": round(len(hit) / len(lastpred), 2),
                           "recall": round(len(hit) / len(actual_adds), 2)}

        result["indices"].append({
            "name": t["name"], "code": t["code"], "N": N, "as_of_weight": cur_d,
            "next_review": eff, "stale": stale, "last_review": last_review,
            "n_candidates": len(cand), "candidates": cand[:40],
            "drops": [{"code": c, "name": names.get(c, ""), "rank": rank.get(c)} for c in (drops if not stale else [])][:20],
            "accuracy": acc,
        })

    hist[TODAY] = today_log
    # 只保留最近400天历史
    if len(hist) > 400:
        for d in sorted(hist)[:-400]:
            hist.pop(d, None)
    json.dump(hist, open(HIST, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for idx in result["indices"]:
        print(f"[predict] {idx['name']}: 候选{idx['n_candidates']}只 下次{idx['next_review']} "
              f"命中{idx['accuracy'] or '-'}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
