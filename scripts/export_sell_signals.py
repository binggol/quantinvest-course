"""
卖出提醒: 对自选股(+可选持仓成本)按经典卖出规则算信号, 治"处置效应"(赢家卖太早/输家拿太久)。
规则(价格类不锚定成本, 最治处置效应):
  趋势破坏: 跌破MA20(轻) / 跌破MA60(重, 趋势转空)
  移动止损: 距20日高回撤≥10%(短) / 距60日高回撤≥15%(中) —— 让利润奔跑+破位才走
  (有成本时) 硬止损: 跌破成本-8%(欧奈尔) / 获利了结区: 浮盈≥25%且滞涨(分批)
输入: NAS data/watchlist.json(代码) + 可选 data/positions.json([{code,cost,date}])
输出: data/sell_alerts.json。跑: D:/anaconda3/python.exe scripts/export_sell_signals.py
"""
import io, sys, os, json, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'
from datetime import datetime, timedelta
import tushare as ts

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
NAS_DATA = os.environ.get("NAS_DATA", r"\/app/data")  # watchlist/positions 持久卷
SHARED = os.environ.get("SHARED_DIR", r"Z:\claude\qlib\data\csv_tmp")  # 下单页主腿篮子等
OUT = os.path.join(DATA, "sell_alerts.json")
tok_path = os.path.join(DATA, ".tushare_token")
TOK = open(tok_path).read().strip() if os.path.exists(tok_path) else os.environ.get("TUSHARE_TOKEN", "")


def rd(name):
    for p in (os.path.join(NAS_DATA, name), os.path.join(SHARED, name), os.path.join(DATA, name)):
        if os.path.exists(p):
            try: return json.loads(open(p, encoding="utf-8-sig").read())
            except Exception: pass
    return None


def main():
    pro = ts.pro_api(TOK)
    src = {}  # ts_code -> set(来源: 自选/持仓/主腿)
    def add(c, s):
        t = _ts(c)
        if t: src.setdefault(t, set()).add(s)
    wl = rd("watchlist.json") or []
    if isinstance(wl, dict):
        wl = wl.get("codes", [])
    for c in (wl or []):
        add(c, "自选")
    pos = {}  # code -> {cost, date}
    pj = rd("positions.json") or []
    for p in (pj if isinstance(pj, list) else []):
        c = p.get("code") or p.get("ts_code")
        if c:
            pos[_ts(c)] = p; add(c, "持仓")
    # 下单页主腿持仓(策略顾问Pro 买入+持有), 季度中途暴雷也能止损/破位提醒
    adv = rd("regime_advisor_pro.json") or {}
    for it in ((adv.get("trade") or {}).get("items") or []):
        if it.get("action") in ("买入", "持有") and it.get("code"):
            add(it["code"], "主腿")
    codes = sorted(src.keys())
    # 名称
    names = {}
    mdb = os.path.join(DATA, "stock_meta.db")
    if os.path.exists(mdb):
        con = sqlite3.connect(mdb)
        try:
            for tc, nm in con.execute("SELECT ts_code,name FROM stock_meta"):
                names[tc] = nm
        except Exception: pass
        con.close()

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=130)).strftime("%Y%m%d")
    rows = []
    for c in codes:
        try:
            d = pro.daily(ts_code=c, start_date=start, end_date=end)
            af = pro.adj_factor(ts_code=c, start_date=start, end_date=end)
        except Exception:
            continue
        if d is None or len(d) < 25:
            continue
        # 前复权: 否则除权日(高送转/大分红)未复权价假摔→假的跌破均线/回撤(如新易盛)
        if af is not None and len(af):
            d = d.merge(af[["trade_date", "adj_factor"]], on="trade_date", how="left")
        d = d.sort_values("trade_date")
        if "adj_factor" in d.columns:
            fac = d["adj_factor"].astype(float).ffill().bfill()
            cl = (d["close"].astype(float) * fac / fac.iloc[-1]).values  # qfq, 末值=原始价
        else:
            cl = d["close"].astype(float).values
        cur = float(cl[-1])
        ma20 = float(cl[-20:].mean()); ma60 = float(cl[-60:].mean()) if len(cl) >= 60 else None
        hi20 = float(cl[-20:].max()); hi60 = float(cl[-60:].max()) if len(cl) >= 60 else hi20
        dd20 = cur / hi20 - 1; dd60 = cur / hi60 - 1
        sig = []; level = 0
        if cur < ma20: sig.append("跌破20日线"); level = max(level, 1)
        if ma60 and cur < ma60: sig.append("跌破60日线(趋势转空)"); level = max(level, 2)
        if dd20 <= -0.10: sig.append(f"距20日高回撤{dd20*100:.0f}%"); level = max(level, 1)
        if dd60 <= -0.15: sig.append(f"距60日高回撤{dd60*100:.0f}%"); level = max(level, 2)
        p = pos.get(c); cost = None; pnl = None
        if p and p.get("cost"):
            cost = float(p["cost"]); pnl = cur / cost - 1
            if cur <= cost * 0.92: sig.append(f"破成本-8%止损(浮亏{pnl*100:.0f}%)"); level = max(level, 3)
            elif pnl >= 0.25 and cur < hi20 * 0.97: sig.append(f"浮盈{pnl*100:.0f}%+滞涨, 分批止盈"); level = max(level, 1)
        if not sig:
            continue
        rows.append({"code": c, "name": names.get(c, ""), "cur": round(cur, 2),
                     "ma20": round(ma20, 2), "ma60": (round(ma60, 2) if ma60 else None),
                     "dd20": round(dd20 * 100, 1), "dd60": round(dd60 * 100, 1),
                     "cost": (round(cost, 2) if cost else None), "pnl": (round(pnl * 100, 1) if pnl is not None else None),
                     "src": "/".join(sorted(src.get(c, []))), "signals": sig, "level": level})
    rows.sort(key=lambda x: x["level"], reverse=True)
    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "n_watch": len(codes), "n_alert": len(rows),
           "rules": "趋势:跌破MA20(轻)/MA60(重); 移动止损:距20日高-10%/60日高-15%; 有成本:破-8%硬止损/浮盈25%滞涨分批止盈",
           "alerts": rows}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[sell] 监控{len(codes)}只, 触发{len(rows)}只 -> {OUT}")
    for r in rows[:10]:
        print(f"  L{r['level']} {r['code']} {r['name']:8} {r['cur']} | {'; '.join(r['signals'])}")


def _ts(c):
    c = str(c).strip().upper()
    if "." in c: return c
    if c[:2] in ("SH", "SZ", "BJ"): return f"{c[2:]}.{c[:2]}"
    return f"{c}.SH" if c[:1] in ("5", "6", "9") else (f"{c}.SZ" if c[:1] in ("0", "3") else f"{c}.BJ")


if __name__ == "__main__":
    main()
