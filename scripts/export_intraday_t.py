"""
超短线/做T 盘中实时提醒: 对持仓∪自选∪主腿, 取实时报价算 当日位置/VWAP偏离, 给高抛低吸提示。
做T = 用底仓当日高抛低吸降成本(非赌方向)。信号:
  高抛(卖部分底仓): 现价处当日高位(≥75%振幅) 且 ≥VWAP均价  (冲高滞涨)
  低吸(买回):       现价处当日低位(≤25%振幅) 且 ≤VWAP均价  (回踩企稳)
  否则 观望。涨停≈不抛/跌停≈不吸。
经典依据: A股隔夜效应(隔夜涨→日内回吐)→强势股次日早盘冲高是较好了结点; 日内U型, 开盘/尾盘波动大。
数据: tushare realtime_quote(sina). 盘中由 intraday_t_loop.py 每~60秒重跑。
跑: D:/anaconda3/python.exe scripts/export_intraday_t.py
"""
import io, sys, os, json, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'
from datetime import datetime
import tushare as ts

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
NAS_DATA = os.environ.get("NAS_DATA", r"\/app/data")
SHARED = os.environ.get("SHARED_DIR", r"Z:\claude\qlib\data\csv_tmp")
OUT = os.path.join(DATA, "intraday_t.json")
tok_path = os.path.join(DATA, ".tushare_token")
TOK = open(tok_path).read().strip() if os.path.exists(tok_path) else os.environ.get("TUSHARE_TOKEN", "")


def rd(name):
    for p in (os.path.join(NAS_DATA, name), os.path.join(SHARED, name), os.path.join(DATA, name)):
        if os.path.exists(p):
            try: return json.loads(open(p, encoding="utf-8-sig").read())
            except Exception: pass
    return None


def _ts(c):
    c = str(c).strip().upper()
    if "." in c: return c
    if c[:2] in ("SH", "SZ", "BJ"): return f"{c[2:]}.{c[:2]}"
    return f"{c}.SH" if c[:1] in ("5", "6", "9") else (f"{c}.SZ" if c[:1] in ("0", "3") else f"{c}.BJ")


def time_hint():
    hm = datetime.now().strftime("%H:%M")
    if hm < "09:30" or hm > "15:00": return "非交易时段(数据为最新收盘)"
    if hm <= "10:00": return "⏰开盘波动最大(9:30-10:00): 别追高, 做T以观察为主"
    if hm >= "14:30": return "⏰尾盘(14:30-15:00): 做T必须当日平掉, 别留隔夜"
    if "11:00" <= hm <= "13:30": return "午盘较淡, 信号噪声大"
    return "盘中"


def main():
    pos = {}
    for p in (rd("positions.json") or []):
        c = p.get("code") or p.get("ts_code")
        if c: pos[_ts(c)] = p
    src = {}
    def add(c, s): t = _ts(c); src.setdefault(t, set()).add(s) if t else None
    for c in pos: add(c, "持仓")
    wl = rd("watchlist.json") or []
    for c in (wl.get("codes", []) if isinstance(wl, dict) else wl): add(c, "自选")
    for it in (((rd("regime_advisor_pro.json") or {}).get("trade") or {}).get("items") or []):
        if it.get("action") in ("买入", "持有") and it.get("code"): add(it["code"], "主腿")
    codes = sorted(src.keys())
    if not codes:
        json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hint": time_hint(), "n": 0, "rows": []},
                  open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("[intraday_t] 无监控标的"); return

    names = {}
    mdb = os.path.join(DATA, "stock_meta.db")
    if os.path.exists(mdb):
        con = sqlite3.connect(mdb)
        try:
            for tc, nm in con.execute("SELECT ts_code,name FROM stock_meta"): names[tc] = nm
        except Exception: pass
        con.close()

    rows = []
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        try:
            q = ts.realtime_quote(ts_code=",".join(chunk), src="sina")
        except Exception as e:
            print("realtime err", e); continue
        for r in q.itertuples(index=False):
            try:
                cur = float(r.PRICE); op = float(r.OPEN); hi = float(r.HIGH); lo = float(r.LOW); pc = float(r.PRE_CLOSE)
                amt = float(r.AMOUNT); vol = float(r.VOLUME)
            except Exception:
                continue
            if cur <= 0 or pc <= 0:
                continue
            vwap = amt / vol if vol > 0 else cur
            chg = cur / pc - 1
            rng = (hi - lo)
            posr = (cur - lo) / rng if rng > 0 else 0.5      # 当日振幅位置 0低~1高
            vsv = cur / vwap - 1                              # 相对均价
            sig, lvl = "观望", 0
            near_up = chg >= 0.095; near_dn = chg <= -0.095   # 近涨跌停(10%板, 创业/科创20%此处保守)
            if posr >= 0.75 and vsv > 0 and not near_up:
                sig, lvl = "🔴冲高·可高抛(卖部分底仓)", 2
            elif posr <= 0.25 and vsv < 0 and not near_dn:
                sig, lvl = "🟢回踩·可低吸(买回)", 2
            elif posr >= 0.65 and vsv > 0:
                sig, lvl = "偏高·留意高抛", 1
            elif posr <= 0.35 and vsv < 0:
                sig, lvl = "偏低·留意低吸", 1
            code = _ts(r.TS_CODE) if hasattr(r, "TS_CODE") else ""
            rows.append({"code": code, "name": names.get(code, getattr(r, "NAME", "")),
                         "src": "/".join(sorted(src.get(code, []))), "cur": round(cur, 2),
                         "chg": round(chg * 100, 2), "vwap": round(vwap, 2), "vs_vwap": round(vsv * 100, 2),
                         "pos": round(posr * 100, 0), "hi": round(hi, 2), "lo": round(lo, 2),
                         "signal": sig, "level": lvl})
    rows.sort(key=lambda x: x["level"], reverse=True)
    json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hint": time_hint(),
               "n": len(rows), "rows": rows,
               "note": "做T=底仓高抛低吸降成本(非加仓赌方向); 做错当日纠正不留隔夜。位置=当日振幅分位, VWAP=今日成交均价。日内点位多噪声+成本, edge在纪律。"},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[intraday_t] {len(rows)}只 {time_hint()}")
    for r in rows[:8]:
        print(f"  L{r['level']} {r['code']} {r['name']:8} {r['cur']} 涨{r['chg']}% 位置{r['pos']:.0f}% vsVWAP{r['vs_vwap']}% | {r['signal']}")


if __name__ == "__main__":
    main()
