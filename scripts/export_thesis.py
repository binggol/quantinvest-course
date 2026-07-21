"""
产业链智能体·多轮递归分析: 给一个行业/主题, LLM 先分析全局, 再把产业链【上游(材料/设备)→中游(制造)→下游(应用)】逐层拆解,
对每个子环节再递归分析, 直到很细分、可对应具体上市公司的赛道(叶子)才落地个股。产出一棵可逐层点开的树。
叶子个股: 申万L3成分(精确补充)+LLM点名, 业务校验(防张冠李戴), 三年一期(扣非/毛利/ROE/订单/在建)+分业务明细(占比/同比/毛利, 相关业务高亮),
排序=核心优先→相关业务纯度/占比→相关业务营收规模→总营收。
输出 data/thesis_<slug>.json(树) + 拷NAS。用法: D:/anaconda3/python.exe export_thesis.py "AI算力"
"""
import os, io, sys, json, re, time, shutil
from datetime import datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(k, None)
os.environ['no_proxy'] = '*'
import requests
import tushare as ts

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DATA = os.path.join(PROJ, "data")
NAS = os.environ.get("SHARED_DIR", r"Z:\claude\qlib\data\csv_tmp")
MAX_DEPTH = int(os.environ.get("THESIS_MAX_DEPTH", "3"))   # 递归层数(根=0, 叶子≤此)
MAX_CHILDREN = 4                                            # 每层最多子环节
MAX_NODES = int(os.environ.get("THESIS_MAX_NODES", "28"))  # 总节点上限(防爆)
MAX_PER_NODE = 25                                          # 每个叶子申万成分落地上限(LLM点名股不受限)
os.makedirs(DATA, exist_ok=True)


def slug(s):
    return re.sub(r'[^\w]+', '_', (s or "").strip())[:40] or "theme"


def load_llm():
    for f in (r"C:\rdagent\data\.report_llm", r"C:\rdagent\data\.nim_key"):
        try:
            return json.load(open(f))
        except Exception:
            continue
    return None


def _num(v):
    try:
        f = float(v); return f if f == f else None
    except Exception:
        return None


def llm_json(cfg, sysmsg, usermsg, max_tokens=6000):
    body = {"model": cfg["model"], "messages": [
        {"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
        "temperature": 0.4, "max_tokens": max_tokens}
    px = cfg.get("proxy"); sess = requests.Session(); sess.trust_env = bool(px)
    last = ""
    for i in range(4):
        try:
            r = sess.post(cfg["base_url"].rstrip("/") + "/chat/completions",
                          headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
                          json=body, timeout=300, proxies=({"http": px, "https": px} if px else None))
            if r.status_code != 200:
                last = f"HTTP{r.status_code}"; time.sleep(4); continue
            txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
            txt = re.sub(r'^```(json)?|```$', '', txt.strip(), flags=re.M).strip()
            m = re.search(r'\{.*\}', txt, re.S)
            if m:
                return json.loads(m.group(0)), ""
            last = "无JSON"
        except Exception as e:
            last = str(e)[:120]
        time.sleep(4)
    return None, last


def llm_node(cfg, theme, name, role, parent_ctx, depth):
    """对产业链某环节分析: 给本层 analysis; 能再拆=children(上下游子环节), 否则=叶子(给落地个股线索)。"""
    force_leaf = depth >= MAX_DEPTH
    sysmsg = (
        "你是顶尖产业链研究专家。对给定产业/环节做穿透式拆解, 把产业链【上游(原材料/关键设备/零部件)→中游(制造/集成)→下游(应用/系统/品牌)】都分析透彻。\n"
        "对当前环节输出: (a)analysis: 它在产业链中的位置与上下游关系、架构/技术趋势变化、价值量与利润分布、为何关键或为何是瓶颈(详细展开, 可多段);\n"
        "(b)若它还能继续拆成更细的上下游子环节/子赛道, 给 children(2-4个, 覆盖上中下游各侧重), is_leaf=false;\n"
        "  *拆下游应用维度时, 必须穷举该环节的主要需求终端、勿遗漏: 典型含 AI服务器/GPU算力/数据中心、汽车电子(车规)、消费电子(手机/PC)、工业/能源(光伏储能)、通信(基站/光模块)、军工航天等; 对当前正处于景气拐点或用量弹性最大的应用(当下多为AI算力/数据中心), 务必单独成一个 child, 不可并入或省略;\n"
        "(c)若它已是很细分、可直接对应到具体A股上市公司的赛道, is_leaf=true, 并给该赛道: scarcity(稀缺/壁垒来源:产能/资质/材料/设备), controller(被谁掌控/集中度), sw_l3(申万L3行业名,可多个,要真实), keywords(主营关键词2-5个,能在公司主营里检索到), stocks(该赛道8-15家A股龙头+二线,给准确6位代码.SH/.SZ), thesis(战略控制力vs估值,详), signal(应跟踪验证信号,详)。\n"
        '只输出JSON: {"analysis":"...","is_leaf":true/false,"children":[{"name":"子环节名","role":"上游/中游/下游/材料/设备/应用","why":"为何重要(一句)"}],"scarcity":"","controller":"","sw_l3":[],"keywords":[],"stocks":[{"name":"","code":""}],"thesis":"","signal":""}')
    if force_leaf:
        sysmsg += "\n注意: 已到最大深度, 本环节必须 is_leaf=true 并给出落地个股线索。"
    um = f"主题(根产业): {theme}\n当前环节: {name}" + (f" (产业链角色: {role})" if role else "")
    if parent_ctx:
        um += f"\n上层背景: {parent_ctx[:600]}"
    um += "\n请分析并按JSON输出。"
    d, err = llm_json(cfg, sysmsg, um, max_tokens=7000)
    if d and force_leaf:
        d["is_leaf"] = True
    return d, err


def latest_trade_date(pro):
    from datetime import timedelta
    d = datetime.now()
    for _ in range(10):
        ds = d.strftime("%Y%m%d")
        try:
            x = pro.daily_basic(trade_date=ds, fields='ts_code,total_mv')
            if x is not None and len(x) > 100:
                return ds, x
        except Exception:
            pass
        d = d - timedelta(days=1)
    return None, None


def ground_one(pro, basic, ts_code):
    rec = {"name": basic.loc[ts_code, "name"], "code": ts_code, "industry": basic.loc[ts_code, "industry"]}
    try:
        db = pro.daily_basic(ts_code=ts_code, start_date='20260101', end_date='20261231',
                             fields='trade_date,pe_ttm,pb,ps_ttm,total_mv')
        if len(db):
            row = db.sort_values("trade_date").iloc[-1]
            rec["pe_ttm"] = _num(row.get("pe_ttm")); rec["pb"] = _num(row.get("pb"))
            rec["ps_ttm"] = _num(row.get("ps_ttm")); rec["mv"] = _num(row.get("total_mv"))
    except Exception:
        pass
    yr = datetime.now().year
    order_by, cip_by = {}, {}
    try:
        bs = pro.balancesheet(ts_code=ts_code, start_date=f'{yr-5}0101', end_date=f'{yr}1231',
                              fields='end_date,contract_liab,adv_receipts,cip')
        if bs is not None and len(bs):
            bs = bs.dropna(subset=["end_date"]).drop_duplicates("end_date", keep="last")
            for _, r in bs.iterrows():
                e = str(r["end_date"])
                order_by[e] = (_num(r.get("contract_liab")) or 0) + (_num(r.get("adv_receipts")) or 0)
                cv = _num(r.get("cip"))
                if cv is not None:
                    cip_by[e] = cv
    except Exception:
        pass

    def _yoy(cur, base):
        if cur is None or base is None or base == 0:
            return None
        return round((cur / base - 1) * 100, 1)

    try:
        fi = pro.fina_indicator(ts_code=ts_code, start_date=f'{yr-5}0101', end_date=f'{yr}1231',
                                fields='end_date,profit_dedt,grossprofit_margin,roe')
        if fi is not None and len(fi):
            fi = fi.dropna(subset=["end_date"]).drop_duplicates(subset=["end_date"])
            by = {str(r["end_date"]): r for _, r in fi.iterrows()}
            ends = sorted(by.keys(), reverse=True)
            if ends:
                latest = ends[0]; annuals = [e for e in ends if e.endswith("1231")][:3]
                want = sorted(set(annuals + [latest]), reverse=True)
                periods = []
                for e in want:
                    r = by[e]; be = f"{int(e[:4])-1}{e[4:]}"; base = by.get(be)
                    pd0 = _num(r.get("profit_dedt")); pdb = _num(base.get("profit_dedt")) if base is not None else None
                    dt_yoy = round((pd0 - pdb) / abs(pdb) * 100, 1) if (pd0 is not None and pdb not in (None, 0)) else None
                    mm = e[4:]
                    lbl = e[:4] + ("年报" if mm == "1231" else ("一季" if mm == "0331" else ("中报" if mm == "0630" else ("三季" if mm == "0930" else ""))))
                    gm = _num(r.get("grossprofit_margin")); roe = _num(r.get("roe"))
                    periods.append({"label": lbl, "dt_yoy": dt_yoy,
                                    "gm": None if gm is None else round(gm, 1),
                                    "roe": None if roe is None else round(roe, 1),
                                    "order_yoy": _yoy(order_by.get(e), order_by.get(be)),
                                    "cip_yoy": _yoy(cip_by.get(e), cip_by.get(be))})
                rec["periods"] = periods
                if periods:
                    rec["gm"] = periods[0]["gm"]; rec["gm_yoy"] = periods[0]["dt_yoy"]
                    rec["order_yoy"] = periods[0]["order_yoy"]; rec["cip_yoy"] = periods[0]["cip_yoy"]
    except Exception:
        pass
    # 主营构成(分业务: 占比/毛利率/同比)
    try:
        cur = prev = None; cur_y = None
        for y in (yr - 1, yr - 2):
            m = pro.fina_mainbz(ts_code=ts_code, period=f'{y}1231', type='P')
            if m is not None and len(m):
                cur = m; cur_y = y; prev = pro.fina_mainbz(ts_code=ts_code, period=f'{y-1}1231', type='P'); break
        if cur is not None and len(cur):
            cur = cur.copy(); cur["s"] = cur["bz_sales"].apply(_num)
            cur = cur.dropna(subset=["s"]).drop_duplicates("bz_item"); cur = cur[cur["s"] != 0]
            tot = cur["s"].abs().sum()
            pmap = {}
            if prev is not None and len(prev):
                for _, r in prev.drop_duplicates("bz_item").iterrows():
                    pmap[r["bz_item"]] = _num(r.get("bz_sales"))
            segs = []
            for _, r in cur.sort_values("s", ascending=False).iterrows():
                it = str(r["bz_item"]); s = r["s"]; cost = _num(r.get("bz_cost")); prof = _num(r.get("bz_profit"))
                gm = round((s - cost) / s * 100, 1) if (cost is not None and s) else (round(prof / s * 100, 1) if (prof is not None and s) else None)
                ps = pmap.get(it); yoy = round((s / ps - 1) * 100, 1) if (ps and ps > 0) else None
                segs.append({"item": it, "share": round(s / tot * 100, 1) if tot else None, "gm": gm, "yoy": yoy})
            rec["segments"] = segs[:8]; rec["mainbz"] = " / ".join(x["item"] for x in segs[:4])
            rec["seg_period"] = f"{cur_y}年报"; rec["revenue"] = round(tot / 1e8, 2) if tot else None
    except Exception:
        pass
    return rec


def kw_cores(kws):
    suf = ("布", "片", "材", "材料", "液", "粉", "体", "器", "膜", "剂", "胶", "件", "芯片", "制造")
    cs = set()
    for k in kws:
        k = (k or "").strip()
        if not k:
            continue
        cs.add(k)
        for s in suf:
            if k.endswith(s) and len(k) - len(s) >= 2:
                cs.add(k[:-len(s)])
        if len(k) >= 4:
            cs.add(k[-3:]); cs.add(k[:3])
    return [c for c in cs if len(c) >= 2]


def share2(a, b):
    a, b = str(a or ""), str(b or "")
    if len(a) < 2 or len(b) < 2:
        return False
    g = {a[i:i+2] for i in range(len(a) - 1)}
    return any(b[i:i+2] in g for i in range(len(b) - 1))


def ground_leaf(node, ctx):
    """对叶子赛道落地个股(申万精确补充+LLM点名, 校验, 排序)。结果写 node['stocks']。"""
    pro, basic, name2code = ctx["pro"], ctx["basic"], ctx["name2code"]
    mem, sw_map, mv_map = ctx["mem"], ctx["sw_map"], ctx["mv_map"]
    kws = [k for k in (node.get("keywords") or []) if k]
    cores = kw_cores(kws)
    l3s = [s for s in (node.get("sw_l3") or []) if s]

    def resolve(nm, code):
        if isinstance(code, str) and re.match(r'^\d{6}\.(SH|SZ|BJ)$', code.strip()) and code.strip() in basic.index:
            return code.strip()
        if nm and nm.strip() in name2code:
            return name2code[nm.strip()]
        if nm:
            hit = basic[basic["name"].str.contains(re.escape(nm.strip()[:4]), na=False)]
            if len(hit):
                return hit.index[0]
        return None

    sw_cand = set()
    if mem is not None:
        for l3 in l3s:
            sub = mem[mem["l3_name"].astype(str).str.contains(re.escape(l3), na=False)]
            codes = [c for c in sub["ts_code"].unique() if c in basic.index]
            sw_cand.update(sorted(codes, key=lambda c: -(mv_map.get(c, 0) or 0))[:30])
    llm_named = set()
    for s in (node.get("stocks") or []):
        c = resolve(s.get("name", ""), s.get("code", ""))
        if c:
            llm_named.add(c)
    pool = sw_cand | llm_named
    ranked = sorted(pool, key=lambda c: -(mv_map.get(c, 0) or 0))
    ranked = list(dict.fromkeys(list(llm_named) + ranked))[:max(MAX_PER_NODE, len(llm_named))]
    grounded = []; n_flag = 0
    for c in ranked:
        rec = ground_one(pro, basic, c)
        rec["sw"] = sw_map.get(c, "")
        seg_hit = False
        for seg in rec.get("segments", []):
            rel = any(x in seg["item"] for x in cores)
            seg["related"] = bool(rel); seg_hit = seg_hit or rel
        hay = str(rec.get("mainbz", "")) + str(rec.get("name", ""))
        core_hit = seg_hit or (any(x in hay for x in cores) if cores else False)
        ind = rec.get("industry", "")
        plausible = core_hit or any(share2(ind, x) for x in (l3s + kws))
        if c in llm_named:
            tier = "核心" if plausible else "存疑"
            if tier == "存疑":
                rec["warn"] = "业务存疑: LLM点名但行业/主营与本赛道不符"; n_flag += 1
        elif core_hit:
            tier = "核心"
        else:
            continue
        if tier == "核心" and rec.get("segments") and not any(g.get("related") for g in rec["segments"]):
            rec["segments"][0]["related"] = True
        rec["rel_share"] = round(sum((g.get("share") or 0) for g in rec.get("segments", []) if g.get("related")), 1)
        if rec.get("revenue") is not None:
            rec["rel_rev"] = round(rec["revenue"] * rec["rel_share"] / 100, 2)
        rec["src"] = "LLM" if c in llm_named else "申万"
        rec["tier"] = tier
        grounded.append(rec)
    order = {"核心": 0, "存疑": 2}
    grounded.sort(key=lambda r: (order.get(r["tier"], 1), -(r.get("rel_share") or 0),
                                 -(r.get("rel_rev") or 0), -(r.get("revenue") or 0)))
    node["stocks"] = grounded
    node["n"] = len(grounded)
    node["n_flag"] = n_flag
    return len(grounded)


def build_tree(cfg, theme, name, role, parent_ctx, depth, ctx):
    if ctx["count"][0] >= MAX_NODES:
        return None
    ctx["count"][0] += 1
    idx = ctx["count"][0]
    _status(theme, "running", f"第{idx}个环节分析中: {name} (L{depth})")
    print(f"  {'  '*depth}[{idx}] L{depth} {name}", flush=True)
    d, err = llm_node(cfg, theme, name, role, parent_ctx, depth)
    node = {"name": name, "role": role or "", "level": depth,
            "analysis": (d or {}).get("analysis", "") if d else f"(分析失败: {err})",
            "is_leaf": True, "children": []}
    if not d:
        node["is_leaf"] = True; node["stocks"] = []; return node
    is_leaf = bool(d.get("is_leaf")) or depth >= MAX_DEPTH
    if is_leaf:
        node["is_leaf"] = True
        node["scarcity"] = d.get("scarcity", ""); node["controller"] = d.get("controller", "")
        node["thesis"] = d.get("thesis", ""); node["signal"] = d.get("signal", "")
        node["sw_l3"] = d.get("sw_l3") or []; node["keywords"] = d.get("keywords") or []
        node["stocks"] = d.get("stocks") or []   # 先放 LLM 点名 spec, ground_leaf 读后覆盖为落地结果
        ground_leaf(node, ctx)
        return node
    # 非叶子: 递归子环节
    node["is_leaf"] = False
    children = (d.get("children") or [])[:MAX_CHILDREN]
    for ch in children:
        if ctx["count"][0] >= MAX_NODES:
            break
        cn = build_tree(cfg, theme, ch.get("name", "?"), ch.get("role", ""),
                        (node["analysis"] or "")[:600], depth + 1, ctx)
        if cn:
            node["children"].append(cn)
    return node


def _status(theme, state, msg):
    sp = os.path.join(DATA, "thesis_status.json")
    d = {"theme": theme, "state": state, "msg": msg, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    json.dump(d, open(sp, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        shutil.copy(sp, os.path.join(NAS, "thesis_status.json"))
    except Exception:
        pass


def count_leaves(node):
    if node.get("is_leaf"):
        return 1, len(node.get("stocks") or [])
    nl = ns = 0
    for c in node.get("children", []):
        a, b = count_leaves(c); nl += a; ns += b
    return nl, ns


def main():
    theme = os.environ.get("THESIS_THEME") or (sys.argv[1] if len(sys.argv) > 1 else "AI算力")  # env优先(中文不乱码)
    print(f"[thesis] 产业链树分析: {theme} (max_depth={MAX_DEPTH}, max_nodes={MAX_NODES})", flush=True)
    cfg = load_llm()
    if not cfg:
        _status(theme, "error", "无LLM配置"); return
    pro = ts.pro_api(get_tushare_token())
    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry').set_index("ts_code")
    name2code = {}
    for c, n in basic["name"].items():
        name2code.setdefault(n, c)
    sw_map = {}
    try:
        mem = pro.index_member_all(fields='l1_name,l2_name,l3_name,ts_code,name,in_date,out_date,is_new')
        mem = mem[(mem["is_new"] == "Y") | (mem["out_date"].isna()) | (mem["out_date"] == "")]
        for _, r in mem.drop_duplicates("ts_code").iterrows():
            l2 = str(r.get("l2_name") or ""); l3 = str(r.get("l3_name") or "")
            sw_map[r["ts_code"]] = (f"{l2}/{l3}" if l2 and l3 and l2 != l3 else (l3 or l2))
    except Exception as e:
        print("申万成分拉取失败:", e); mem = None
    _status(theme, "running", "拉最新估值...")
    tdate, mv_all = latest_trade_date(pro)
    mv_map = dict(zip(mv_all["ts_code"], mv_all["total_mv"])) if mv_all is not None else {}
    ctx = {"pro": pro, "basic": basic, "name2code": name2code, "mem": mem,
           "sw_map": sw_map, "mv_map": mv_map, "count": [0]}
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tree = build_tree(cfg, theme, theme, "", "", 0, ctx)
    nl, ns = count_leaves(tree) if tree else (0, 0)
    out = {"theme": theme, "ts": ts_now, "trade_date": tdate, "tree": tree,
           "n_nodes": ctx["count"][0], "n_leaves": nl, "n_stocks": ns, "max_depth": MAX_DEPTH}
    fp = os.path.join(DATA, f"thesis_{slug(theme)}.json")
    json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # 主题索引
    idxp = os.path.join(DATA, "thesis_index.json")
    idx = {}
    if os.path.exists(idxp):
        try:
            idx = {x["theme"]: x for x in json.load(open(idxp, encoding="utf-8"))}
        except Exception:
            idx = {}
    idx[theme] = {"theme": theme, "ts": ts_now, "slug": slug(theme), "n_leaves": nl, "n_stocks": ns}
    json.dump(sorted(idx.values(), key=lambda x: x["ts"], reverse=True),
              open(idxp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for f in (fp, idxp):
        try:
            shutil.copy(f, os.path.join(NAS, os.path.basename(f)))
        except Exception as e:
            print("拷NAS失败", e)
    print(f"[thesis] 完成: {ctx['count'][0]}节点 {nl}叶子 {ns}股 -> {fp}", flush=True)
    _status(theme, "done", f"{ctx['count'][0]}环节 {nl}细分赛道 {ns}股")


if __name__ == "__main__":
    main()
