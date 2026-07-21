"""
组合总买入清单(落地): 6腿当前持仓 × 腿权重 → 个股最终目标权重, 汇总成一份可执行清单。
宽腿截断 Top-N 等权(主腿用自带权重); 个股出现在多腿则权重相加。
各腿对冲版另用股指期货做空(IF/IC/IM), 此清单为多头股票侧。
读各腿 json(csv_tmp 同步位置), 输出 data/combo_holdings.json。
跑: D:/anaconda3/python.exe scripts/export_combo_holdings.py
"""
import io, sys, os, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from datetime import datetime

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
SHARED = os.environ.get("SHARED_DIR", r"Z:\claude\qlib\data\csv_tmp")  # 各腿清单同步位置
OUT = os.path.join(DATA, "combo_holdings.json")
TOPN = int(os.environ.get("COMBO_TOPN", "30"))
WEIGHTS = {"主300": 0.46, "抢跑": 0.08, "PEAD": 0.08, "回购": 0.15, "纳入": 0.08, "质量": 0.15}


def rd(name):
    for p in (os.path.join(SHARED, name), os.path.join(DATA, name)):
        if os.path.exists(p):
            try: return json.load(open(p, encoding="utf-8-sig"))
            except Exception: pass
    return {}


def leg_holdings():
    """各腿 -> [(code, name, within_leg_weight)] (within权重和=1)."""
    legs = {}
    # 主300: 策略顾问Pro 篮子(买入+持有), 用自带weight
    adv = rd("regime_advisor_pro.json")
    items = [i for i in (adv.get("trade", {}).get("items") or []) if i.get("action") in ("买入", "持有") and i.get("code")]
    if items:
        tot = sum(i.get("weight", 0) for i in items) or len(items)
        legs["主300"] = [(i["code"], i.get("name", ""), (i.get("weight", 0) / tot if tot else 1 / len(items))) for i in items]
    # 回购: holdings 等权, 优先注销型(验证gate_repo_purpose: 注销型60日+3.09% vs 全部+2.07%), 再按最新(held_td小)
    repo = rd("repo.json").get("holdings") or []
    _cancel_items = rd("repo_cancel.json").get("items") or []
    cancel_set6 = {str(it.get("code", ""))[:6] for it in _cancel_items}
    for r in repo:
        r["cancel_type"] = str(r.get("code", ""))[:6] in cancel_set6
    # 排序: 注销型优先(EPS被动提升真利好), 同类按held_td(新)
    repo = sorted(repo, key=lambda x: (0 if x.get("cancel_type") else 1, x.get("held_td", 99)))[:TOPN]
    if repo:
        legs["回购"] = [(r["code"], r.get("name", ""), 1 / len(repo)) for r in repo]
    global REPO_META
    REPO_META = {r["code"]: {"held_td": r.get("held_td"), "to_close": r.get("to_close"), "cancel_type": r.get("cancel_type")} for r in repo if r.get("code")}
    # 纳入: 实盘持仓/今日建仓(季节性, 常空) 等权
    inc = (rd("index_inclusion_pro.json").get("holdings") or []) + (rd("index_inclusion_pro.json").get("buy_today") or [])
    seen = set(); inc = [x for x in inc if x.get("code") and not (x["code"] in seen or seen.add(x["code"]))][:TOPN]
    if inc:
        legs["纳入"] = [(r["code"], r.get("name", ""), 1 / len(inc)) for r in inc]
    # 抢跑: 今日建仓 等权
    ru = rd("runup.json").get("buy") or []
    ru = [r for r in ru if r.get("code")][:TOPN]
    if ru:
        legs["抢跑"] = [(r["code"], r.get("name", ""), 1 / len(ru)) for r in ru]
    # PEAD: 抢跑腿B 公告后买
    pe = rd("runup.json").get("buy_post") or []
    pe = [r for r in pe if r.get("code")][:TOPN]
    if pe:
        legs["PEAD"] = [(r["code"], r.get("name", ""), 1 / len(pe)) for r in pe]
    # 质量: 池里按营收增速Top-N(避低基数反转), 等权
    q = rd("quality.json").get("rows") or []
    q = sorted(q, key=lambda x: (x.get("or_yoy") if x.get("or_yoy") is not None else -999), reverse=True)[:TOPN]
    if q:
        legs["质量"] = [(r["code"], r.get("name", ""), 1 / len(q)) for r in q]
    return legs


def main():
    tushare_token = get_tushare_token()
    legs = leg_holdings()
    agg = {}
    for lg, hold in legs.items():
        w = WEIGHTS.get(lg, 0)
        for rank, (code, name, iw) in enumerate(hold, 1):  # rank=该腿内位次(按腿的排序)
            e = agg.setdefault(code, {"code": code, "name": name, "weight": 0.0, "legs": []})
            e["weight"] += w * iw
            le = {"leg": lg, "rank": rank}
            if lg == "回购" and code in globals().get("REPO_META", {}):
                rm = REPO_META[code]
                le["held_td"] = rm.get("held_td"); le["to_close"] = rm.get("to_close")  # 已持有/距平仓(回购持60日)
                le["cancel_type"] = rm.get("cancel_type")  # 注销型(真利好, EPS被动升)
            e["legs"].append(le)
    rows = sorted(agg.values(), key=lambda x: x["weight"], reverse=True)
    # 名称兜底: 某腿JSON的name缺失/等于代码时, 用stock_basic补全(回购腿等常缺名)
    need = [r["code"] for r in rows if not r.get("name") or str(r["name"]).replace(".", "").isdigit() or r["name"] == r["code"]]
    if need:
        try:
            import os as _os
            for _k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
                _os.environ.pop(_k, None)
            _os.environ['no_proxy'] = '*'
            import tushare as _ts
            _sb = _ts.pro_api(tushare_token).stock_basic(exchange='', list_status='L', fields='ts_code,name')
            _nm = dict(zip(_sb['ts_code'], _sb['name']))
            for r in rows:
                if r["code"] in need and _nm.get(r["code"]):
                    r["name"] = _nm[r["code"]]
            print(f"[combo] 名称兜底补全 {len(need)} 只")
        except Exception as _e:
            print(f"[combo] 名称兜底跳过: {_e}")
    # ADV容量校准: 取每只20日均成交额(亿元), 标流动性容量。组合Top30等权真下单受冲击成本限制,
    # 小盘低ADV票=买不进去/冲击成本吃掉超额。<2亿=小容量标橙警示, 人工决定是否减配。
    try:
        import os as _os
        for _k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
            _os.environ.pop(_k, None)
        _os.environ['no_proxy'] = '*'
        import tushare as _ts, datetime as _dtm
        _pro = _ts.pro_api(tushare_token)
        _end = _dtm.date.today().strftime('%Y%m%d')
        _start = (_dtm.date.today() - _dtm.timedelta(days=40)).strftime('%Y%m%d')
        n_adv = 0
        for r in rows:
            try:
                _df = _pro.daily(ts_code=r["code"], start_date=_start, end_date=_end, fields='ts_code,trade_date,amount')
                if _df is not None and len(_df):
                    _adv = float(_df['amount'].head(20).mean()) / 100000.0   # amount单位千元 → 亿元
                    r["adv_yi"] = round(_adv, 2)
                    r["low_liq"] = _adv < 2.0   # <2亿日均成交=小容量
                    if r["low_liq"]:
                        n_adv += 1
            except Exception:
                pass
        print(f"[combo] ADV容量校准: {n_adv}只小容量(<2亿日均)")
    except Exception as _e:
        print(f"[combo] ADV校准跳过: {_e}")
    for r in rows:
        r["weight_pct"] = round(r["weight"] * 100, 3)
        r.pop("weight", None)
    # 热榜避雷: 标红留痕(不剔除) — 当前在同花顺热榜=关注度透支, 未来20日跑输市场-3.2%/t-14。
    # 故意保留持仓不删, 便于后续对比"被标红的票后来是否真跑输", 减不减仓由人工决定。
    hot = rd("hot_avoid.json")
    hotmap = {h["code"]: h for h in (hot.get("items") or [])}
    n_flag = 0
    for r in rows:
        hm = hotmap.get(r["code"])
        if hm:
            r["hot_avoid"] = True
            r["hot_rank"] = hm.get("rank")
            r["hot_days"] = hm.get("days_on")
            r["hot_top20"] = bool(hm.get("top20"))
            n_flag += 1
    # 毛利率恶化剔除overlay: dgm(毛利率同比)严重恶化=基本面变差。验证(factor_margin_combo): 作独立第5腿无增量(HL落地版-0.07),
    # 但价值全在空头侧(踢恶化股 IR+1.3不需融券)→ 故标记留痕(不自动剔, 人工决定), 当负面信号。
    marg = rd("margin_avoid.json")
    margmap = {m["code"]: m for m in (marg.get("items") or [])}
    n_marg = 0
    for r in rows:
        mm = margmap.get(r["code"])
        if mm:
            r["margin_avoid"] = True
            r["dgm"] = mm.get("dgm")          # 毛利率同比变化(负=恶化)
            r["gm"] = mm.get("gm")
            n_marg += 1
    # 龙虎榜净卖避雷(验证gate_lhb_verify: 净卖股T+1后5日-2.19%, 三年全负, 强度≈热榜; 仅避雷侧真选股侧穿越)
    lhb = rd("lhb_avoid.json")
    lhbmap = {m.get("ts_code", m.get("code")): m for m in (lhb.get("items") or [])}
    lhbmap6 = {str(k)[:6]: v for k, v in lhbmap.items()}
    n_lhb = 0
    for r in rows:
        lm = lhbmap.get(r["code"]) or lhbmap6.get(str(r["code"])[:6])
        if lm:
            r["lhb_avoid"] = True
            r["lhb_net_pct"] = lm.get("net_ratio_pct")   # 龙虎榜净买占成交额%(负=净卖)
            n_lhb += 1
    # 立案调查黑名单(最强死亡信号, 一票否决): 验证gate_investigation 立案后120日-8.67%/崩盘率53%/胜35%
    invd = rd("investigation_avoid.json")
    inv6 = {str(m.get("code", ""))[:6] for m in (invd.get("items") or []) if m.get("in_blacklist")}
    n_inv = 0
    for r in rows:
        if str(r["code"])[:6] in inv6:
            r["investigation"] = True   # 立案=硬黑名单, 该清仓(标黑红)
            n_inv += 1
    # 避雷合成: 热榜/毛利恶化/龙虎榜净卖 中≥2个同亮=高危(验证: 同尺度坏信号复合, 热榜∩毛利双亮20日-3.27%/胜31%)
    n_double = 0
    for r in rows:
        nflag = (1 if r.get("hot_avoid") else 0) + (1 if r.get("margin_avoid") else 0) + (1 if r.get("lhb_avoid") else 0)
        if nflag >= 2:
            r["danger_double"] = True   # ≥2避雷信号同亮=高危(标深红)
            r["danger_count"] = nflag
            n_double += 1
    # 杠杆透支标记: 融资净买入暴增(弱避雷, 反向IC-0.0155, 标记不剔除)
    lev = rd("leverage_avoid.json")
    levmap = {m["code"]: m for m in (lev.get("items") or [])}
    n_lev = 0
    for r in rows:
        lm = levmap.get(r["code"])
        if lm:
            r["lev_avoid"] = True
            r["lev_mbf"] = lm.get("mbf_pct")   # 融资净买入占成交额%
            n_lev += 1
    leg_meta = [{"name": lg, "weight": WEIGHTS.get(lg, 0), "n": len(hold)} for lg, hold in legs.items()]
    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "topn": TOPN,
           "weights": "主46/抢8/PEAD8/回购15/纳入8/质量15", "legs": leg_meta,
           "n_stocks": len(rows), "holdings": rows,
           "hot_as_of": hot.get("as_of", ""), "n_hot_flag": n_flag,
           "margin_as_of": marg.get("as_of", ""), "n_margin_flag": n_marg,
           "lev_as_of": lev.get("as_of", ""), "n_lev_flag": n_lev,
           "lhb_as_of": lhb.get("as_of", ""), "n_lhb_flag": n_lhb,
           "inv_as_of": invd.get("as_of", ""), "n_inv_flag": n_inv,
           "n_double_flag": n_double,
           "n_low_liq": sum(1 for r in rows if r.get("low_liq")),
           "note": "各腿Top%d等权(主腿用自带权重)×腿权重汇总; 多腿重叠则相加。多头股票侧, 对冲版另做空对应股指期货(主300/质量→IF, 抢跑/回购/纳入→IC/IM)。季节性事件腿(抢跑/纳入)空窗期可能为空。标红=当前在同花顺热榜(关注度透支, 20日-3.2%%/t-14), 留痕不剔除供对比, 人工决定减仓。" % TOPN}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # 演进版本快照: 组合权重变化时, 存一份该版买入清单(供演进历史每版回看)
    try:
        cj = rd("combo.json"); cmb = cj.get("combined") or {}
        ver = cmb.get("weights", out["weights"]); shp = cmb.get("sharpe")
        hp = os.path.join(DATA, "combo_holdings_history.json")
        try: hist = json.load(open(hp, encoding="utf-8"))
        except Exception: hist = []
        if not isinstance(hist, list): hist = []
        if not hist or hist[-1].get("weights") != ver:
            hist.append({"date": out["updated"][:10], "weights": ver, "sharpe": shp,
                         "n_stocks": len(rows), "holdings": rows[:50]})
            open(hp, "w", encoding="utf-8").write(json.dumps(hist, ensure_ascii=False, indent=1))
            print(f"[combo_holdings] 新版本快照 {ver} ({len(rows)}只)")
    except Exception as e:
        print("[combo_holdings] snapshot err", e)
    print(f"[combo_holdings] 腿{len(legs)} 个股{len(rows)} -> {OUT}")
    for lg in leg_meta:
        print(f"  {lg['name']:5} 权重{lg['weight']*100:.0f}% 持仓{lg['n']}")
    for r in rows[:10]:
        print(f"  {r['code']} {r['name']:8} {r['weight_pct']:.2f}% [{'/'.join(x['leg']+'#'+str(x['rank']) for x in r['legs'])}]")


if __name__ == "__main__":
    main()
