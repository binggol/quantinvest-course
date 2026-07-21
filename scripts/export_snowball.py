"""
雪球避雷清单(风控旗标, 非alpha): 读场外雪球合约表(W盘Excel) → 实时价算 距敲入/距敲出 → 旗标 + 持仓交叉。
机制真实(券商Delta对冲: 近敲入被迫买入托底、破敲入斩仓踩踏、敲出清底仓抛压), 与海力士/回购同类=资金流信号。
但: 主要是指数现象/单票影响相对大盘股ADV偏小; 无历史合约库=无法回测 -> 只当"持仓踩踏预警"用, 不做黄金坑抄底/网格(纯理论)。
跑: D:/anaconda3/python.exe scripts/export_snowball.py   (Excel在W盘office映射, 仅PC可读)
"""
import os, io, sys, json
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

XLSX = os.environ.get("SNOWBALL_XLSX", r"W:\办公\雪球\雪球.xlsx")
DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA, "snowball_avoid.json")
POS = os.path.join(DATA, "positions.json")
KI_NEAR, KO_NEAR = 0.05, 0.03   # 距敲入<5%=踩踏预警, 距敲出<3%=抛压预警


def norm_a(raw):
    """雪球表代码→标准A股 ts_code; 港股(.HK)返回None(qlib系统用不了)。"""
    s = str(raw).strip().upper()
    if 'HK' in s or '.H' in s:
        return None
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    c6 = digits.zfill(6)[-6:] if len(digits) < 6 else digits[:6]
    if c6[0] == '6':
        return c6 + '.SH'
    if c6[0] == '8' or c6[0] == '4' or c6[:3] in ('430', '830', '870', '920'):
        return c6 + '.BJ'
    return c6 + '.SZ'


def fnum(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def dstr(v):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def main():
    if not os.path.exists(XLSX):
        print(f"[snowball] Excel不存在: {XLSX} (W盘office映射, 仅PC可读)")
        # Fail closed: keep the last valid snapshot so the health monitor can
        # mark it stale.  Replacing it with a fresh empty list would incorrectly
        # look like "no risk" when the source drive is merely unavailable.
        return 2
    x = pd.read_excel(XLSX, header=None)
    pro = ts.pro_api(get_tushare_token())
    today = datetime.now().strftime("%Y-%m-%d")

    # 解析合约(数据行从第1行起): 列 1代码 2名称 3名义 5期初 6敲出 7敲入 9保证金 10起息 11到期 20票息
    cons = []
    for i in range(1, len(x)):
        code = norm_a(x.iat[i, 1])
        ini, ko, ki = fnum(x.iat[i, 5]), fnum(x.iat[i, 6]), fnum(x.iat[i, 7])
        if not code or not ini or not ki or not ko:
            continue
        cons.append({"code": code, "name": dstr(x.iat[i, 2]), "notional": fnum(x.iat[i, 3]),
                     "initial": ini, "ko": ko, "ki": ki, "margin": fnum(x.iat[i, 9]),
                     "start": dstr(x.iat[i, 10]), "end": dstr(x.iat[i, 11]), "coupon": fnum(x.iat[i, 20])})
    print(f"[snowball] A股合约 {len(cons)} 份(港股已剔)")
    if not cons:
        print("[snowball] Excel无有效A股雪球合约; 保留上一版输出")
        return 3

    # 实时价(realtime_quote), 失败回退最近日线收盘
    codes = sorted({c["code"] for c in cons})
    px = {}
    try:
        q = ts.realtime_quote(ts_code=",".join(codes))
        for _, r in q.iterrows():
            p = fnum(r.get("PRICE"))
            if p and p > 0:
                px[str(r.get("TS_CODE"))] = p
    except Exception as e:
        print(f"[snowball] realtime_quote失败({e}), 回退日线")
    miss = [c for c in codes if c not in px]
    if miss:
        try:
            now = datetime.now()
            today_compact = now.strftime("%Y%m%d")
            cal = pro.trade_cal(
                exchange="",
                start_date=f"{now.year - 1}0101",
                end_date=today_compact,
                is_open="1",
            )
            ld = sorted(d for d in cal["cal_date"].tolist() if d <= today_compact)[-1]
            dd = pro.daily(trade_date=ld)
            cl = dict(zip(dd['ts_code'], dd['close']))
            for c in miss:
                if c in cl:
                    px[c] = float(cl[c])
        except Exception as e:
            print(f"[snowball] 日线回退失败 {e}")

    active_codes = {
        c["code"] for c in cons if not c["end"] or c["end"] >= today
    }
    coverage_codes = active_codes or set(codes)
    priced_codes = coverage_codes.intersection(px)
    coverage = len(priced_codes) / len(coverage_codes) if coverage_codes else 0.0
    if not priced_codes or coverage < 0.8:
        print(
            f"[snowball] 行情覆盖不足: {len(priced_codes)}/{len(coverage_codes)} "
            f"({coverage:.0%}); 保留上一版输出"
        )
        return 4

    # 持仓交叉
    held = set()
    try:
        pj = json.load(open(POS, encoding="utf-8")) if os.path.exists(POS) else {}
        for p in (pj.get("positions") or pj.get("items") or []):
            cc = norm_a(p.get("code") or p.get("ts_code") or "")
            if cc:
                held.add(cc)
    except Exception:
        pass

    items = []
    for c in cons:
        p = px.get(c["code"])
        expired = bool(c["end"]) and c["end"] < today
        dki = (p - c["ki"]) / c["initial"] if p else None       # 距敲入(占期初%), 越小越危
        dko = (c["ko"] - p) / c["initial"] if p else None       # 距敲出
        if p is None:
            st, lvl = "无报价", 0
        elif expired:
            st, lvl = "已到期", 0
        elif p <= c["ki"]:
            st, lvl = "🔴已破敲入(踩踏/价值真空)", 3
        elif dki is not None and dki < KI_NEAR:
            st, lvl = "⚠️临近敲入(踩踏风险)", 3
        elif p >= c["ko"]:
            st, lvl = "已敲出(合约终止/底仓抛压)", 1
        elif dko is not None and dko < KO_NEAR:
            st, lvl = "🔔临近敲出(底仓抛压)", 2
        else:
            st, lvl = "区间内(波动被压制)", 0
        items.append({**c, "price": round(p, 4) if p else None,
                      "dist_ki": round(dki * 100, 1) if dki is not None else None,
                      "dist_ko": round(dko * 100, 1) if dko is not None else None,
                      "status": st, "level": lvl, "held": c["code"] in held, "expired": expired,
                      "coupon_pct": round(c["coupon"] * 100, 1) if c["coupon"] else None})
    # 排序: 持仓优先, 再按风险等级(踩踏>抛压), 再按距敲入
    items.sort(key=lambda v: (not v["held"], -v["level"], v["dist_ki"] if v["dist_ki"] is not None else 999))
    n_warn = sum(1 for v in items if v["level"] >= 2 and not v["expired"])
    n_held_warn = sum(1 for v in items if v["held"] and v["level"] >= 2 and not v["expired"])

    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today,
           "n": len(items), "n_warn": n_warn, "n_held_warn": n_held_warn, "items": items,
           "note": "场外雪球Delta对冲对正股有反作用: 区间内券商高抛低吸→压波动; 价近敲入(70%)→券商被迫买入托底, 一旦跌破→斩仓踩踏(下方断崖); 价近敲出(103%)→提前终止后清底仓→获利回吐抛压。票息∝隐波(高波股票息高)。"
                   "【局限】主要是中证500/1000指数现象, 单票雪球对冲量相对大盘股ADV偏小; 无历史合约库=无法回测, 故仅作持仓踩踏/抛压预警, 不做黄金坑抄底/网格(纯理论)。港股合约用不了已剔。",
           "message": ""}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    HIST = os.path.join(DATA, "snowball_history.json")
    try:
        hist = json.load(open(HIST, encoding="utf-8")) if os.path.exists(HIST) else []
    except Exception:
        hist = []
    if not isinstance(hist, list):
        hist = []
    # A scheduled retry replaces today's snapshot instead of appending a
    # duplicate history row.
    hist = [
        row for row in hist
        if isinstance(row, dict) and str(row.get("as_of") or "") != today
    ]
    hist.append({"as_of": today, "updated": out["updated"], "n": len(items), "n_warn": n_warn,
                 "warns": [{"code": v["code"], "name": v["name"], "status": v["status"],
                            "dist_ki": v["dist_ki"], "price": v["price"], "held": v["held"]}
                           for v in items if v["level"] >= 2 and not v["expired"]]})
    hist = hist[-250:]
    json.dump(hist, open(HIST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"[snowball] {today} A股合约{len(items)}份, 预警{n_warn}个(持仓踩{n_held_warn}) -> {OUT}")
    for v in items[:12]:
        flag = "【持仓】" if v["held"] else ""
        print(f"  {flag}{v['code']} {v['name']:8} 现{v['price']} 距敲入{v['dist_ki']}% 距敲出{v['dist_ko']}% {v['status']}")


if __name__ == "__main__":
    raise SystemExit(main())
