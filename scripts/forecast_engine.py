"""三表联动财务预测引擎 (复刻 力量钻石 张小兵 DCF 模型)。

纯 Python, 无 tushare/IO。被 Flask 容器内 /api/forecast/recompute 直接 import 调用。
单位: 全程 百万元 (fetch_statements.py 把 tushare 的 元 ÷1e6 后喂进来)。

关键发现(对照 Excel): 本模型 **无真正循环引用** —— 利息按"期初(上年末)余额"计:
  循环贷利息=期初循环贷×率; 长贷利息=(期初+期末)/2×率(期末=期初+固定假设增量); 利息收入=期初现金×率。
因此各年可由上年期末余额 **顺序递推**, 不需迭代求解。

公开入口: recompute(base, assumptions, product_lines=None, horizon=5) -> dict
"""
import math


def _g(d, k, default=0.0):
    v = d.get(k, default)
    return default if v is None else v


def _vec(A, key, horizon, default=0.0):
    """取假设里的逐年向量; 标量则广播; 不足 horizon 用最后一个值延长。"""
    v = A.get(key, default)
    if not isinstance(v, (list, tuple)):
        return [float(v if v is not None else default)] * horizon
    out = [float(x if x is not None else default) for x in v]
    if not out:
        return [float(default)] * horizon
    while len(out) < horizon:
        out.append(out[-1])
    return out[:horizon]


def _revenue_cogs(base, A, product_lines, horizon):
    """收入与销货成本。优先用产品线明细 (Σ各线), 否则用总增长率+成本率。"""
    if product_lines:
        rev, cogs = [], []
        # 每条线: base_rev / base 后逐年 *(1+g), cost = rev*(1-gm)
        lines = []
        for pl in product_lines:
            lines.append({
                "name": pl.get("name", ""),
                "rev": float(_g(pl, "base_rev", 0.0)),
                "growth": _vec(pl, "growth", horizon, 0.0),
                "gm": _vec(pl, "gross_margin", horizon, 0.0),
            })
        per_line = []
        for t in range(horizon):
            yr_rev = yr_cost = 0.0
            row = []
            for ln in lines:
                ln["rev"] = ln["rev"] * (1 + ln["growth"][t])
                c = ln["rev"] * (1 - ln["gm"][t])
                yr_rev += ln["rev"]; yr_cost += c
                row.append({"name": ln["name"], "rev": ln["rev"], "cost": c, "gm": ln["gm"][t]})
            rev.append(yr_rev); cogs.append(yr_cost); per_line.append(row)
        return rev, cogs, per_line
    # 总量法
    g = _vec(A, "growth", horizon)
    c2r = _vec(A, "cogs_to_rev", horizon)
    rev, cogs = [], []
    prev = float(_g(base["opening"], "revenue_base", _g(base, "rev_base", 0.0)))
    for t in range(horizon):
        r = prev * (1 + g[t]); rev.append(r); cogs.append(r * c2r[t]); prev = r
    return rev, cogs, None


def _capex_schedule(base, A, horizon, ppe_open, cip_open, intan_open, impair_fv):
    """资本开支/折旧/摊销 (vintage 直线法)。返回逐年 dep, amort, ppe_net, cip, intan, capex_outflow。"""
    dep_life = float(_g(A, "dep_life", 10) or 10)
    dep_life_new = float(_g(A, "dep_life_new", dep_life) or dep_life)
    reno_ratio = _vec(A, "reno_ratio", horizon, 0.1)
    cip_to_fixed_v = _vec(A, "cip_to_fixed_ratio", horizon, 0.5)
    capex_new = _vec(A, "capex_new", horizon, 0.0)
    amort_life = float(_g(A, "amort_life", 23) or 23)
    intan_inv = _vec(A, "intan_invest", horizon, 0.0)

    existing_dep = ppe_open / dep_life if dep_life else 0.0  # 初始原值按净值近似, 每年固定
    vintages = []          # 每条: 转固额 addition, 起始年 t0
    dep, amort, ppe_net, cip, intan, capex_out = [], [], [], [], [], []
    ppe_prev, cip_prev, intan_prev = ppe_open, cip_open, intan_open
    for t in range(horizon):
        # 在建/改造
        reno_in = ppe_prev * reno_ratio[t]          # 改造投入 = 期初固定资产净值 × 比率
        reno_to_fixed = reno_in                      # 改造转固 (转固比率=1)
        cip_in = capex_new[t] - reno_in              # 在建投入
        cip_to_fixed = (cip_in + cip_prev) * cip_to_fixed_v[t]
        addition = reno_to_fixed + cip_to_fixed      # 当期转固 (固定资产增加)
        cip_now = cip_prev + (reno_in + cip_in) - reno_to_fixed - cip_to_fixed
        # 折旧: 存量 + 各 vintage
        vintages.append({"add": addition, "t0": t})
        d = existing_dep
        for v in vintages:
            if v["t0"] == t:
                d += v["add"] / dep_life_new / 2.0   # 当年半折旧
            else:
                d += v["add"] / dep_life_new
        ppe_now = ppe_prev + addition - d + impair_fv[t]   # +公允价值变动 (复刻 Excel G46)
        # 无形
        am = (intan_prev + intan_inv[t]) / amort_life if amort_life else 0.0
        intan_now = intan_prev + intan_inv[t] - am
        dep.append(d); amort.append(am)
        ppe_net.append(ppe_now); cip.append(cip_now); intan.append(intan_now)
        capex_out.append(capex_new[t])               # 现金流口径固定资产投资 = 新建投资
        ppe_prev, cip_prev, intan_prev = ppe_now, cip_now, intan_now
    return dep, amort, ppe_net, cip, intan, capex_out


def recompute(base, A, product_lines=None, horizon=5):
    horizon = int(horizon)
    op = base["opening"]
    if product_lines is None:
        product_lines = base.get("product_lines")

    rev, cogs, per_line = _revenue_cogs(base, A, product_lines, horizon)

    selling_r = _vec(A, "selling_to_rev", horizon)
    admin_r = _vec(A, "admin_to_rev", horizon)
    rnd_r = _vec(A, "rnd_to_rev", horizon)
    optax_r = _vec(A, "optax_to_rev", horizon)
    tax_rate = _vec(A, "tax_rate", horizon)
    payout = _vec(A, "payout", horizon)

    ar_days = _vec(A, "ar_days", horizon); inv_days = _vec(A, "inv_days", horizon)
    ap_days = _vec(A, "ap_days", horizon); prepaid_r = _vec(A, "prepaid_to_rev", horizon)
    accrued_r = _vec(A, "accrued_ratio", horizon); other_cl_r = _vec(A, "other_cl_ratio", horizon)

    # 非经常性损益分量 (逐年, 默认沿用 opening 推得的水平由 fetch 写入 assumptions)
    obp = _vec(A, "other_biz_profit", horizon); adisp = _vec(A, "asset_disposal", horizon)
    ifv = _vec(A, "impair_fv", horizon); iinc = _vec(A, "invest_income", horizon)
    fx = _vec(A, "fx", horizon); subsidy = _vec(A, "subsidy", horizon); nonop = _vec(A, "nonop", horizon)

    min_cash = _vec(A, "min_cash", horizon); min_rev = _vec(A, "min_revolver", horizon)
    rev_rate = _vec(A, "revolver_rate", horizon, 0.04)
    lt_add = _vec(A, "lt_loan_add", horizon); lt_rate = _vec(A, "lt_loan_rate", horizon, 0.04)
    bond_add = _vec(A, "bond_add", horizon); bond_rate = _vec(A, "bond_rate", horizon, 0.04)
    cash_yield = _vec(A, "cash_yield", horizon, 0.015)
    st_invest = _vec(A, "st_invest", horizon, float(_g(op, "st_invest", 0.0)))
    other_lt_assets = _vec(A, "other_lt_assets", horizon, float(_g(op, "other_lt_assets", 0.0)))
    lt_eq_add = _vec(A, "lt_equity_add", horizon); other_ltl_add = _vec(A, "other_lt_liab_add", horizon)
    minority_payout = _vec(A, "minority_payout", horizon)
    equity_issue = _vec(A, "equity_issuance", horizon)
    share_cnt = _vec(A, "share_count", horizon, float(_g(op, "share_count", 1.0)))
    impair_rate = float(_g(A, "impair_rate", 0.005) or 0.0)

    dep, amort, ppe_net, cip, intan, capex_out = _capex_schedule(
        base, A, horizon, float(_g(op, "ppe_net")), float(_g(op, "cip")),
        float(_g(op, "intangible")), ifv)

    income, balance, cashflow, checks = [], [], [], []
    # 期初(上年末)状态
    st = {
        "cash": float(_g(op, "cash")), "revolver": float(_g(op, "revolver")),
        "lt_loan": float(_g(op, "lt_loan")), "bonds": float(_g(op, "bonds")),
        "equity": float(_g(op, "equity")), "minority": float(_g(op, "minority")),
        "ar": float(_g(op, "ar")), "inventory": float(_g(op, "inventory")),
        "prepaid": float(_g(op, "prepaid_other_ca")), "ap": float(_g(op, "ap")),
        "accrued": float(_g(op, "accrued")), "other_cl": float(_g(op, "other_cl")),
        "st_invest": float(_g(op, "st_invest")), "lt_equity": float(_g(op, "lt_equity_invest")),
        "other_lta": float(_g(op, "other_lt_assets")), "other_ltl": float(_g(op, "other_lt_liab")),
        "ppe_net_imp": float(_g(op, "ppe_net")) * (1 - impair_rate),  # 减值后净额(近似上年)
        "cip": float(_g(op, "cip")), "intangible": float(_g(op, "intangible")),
    }
    prev_pretax = float(_g(op, "pretax_base", 1.0)) or 1.0
    prev_minority_loss = float(_g(op, "minority_loss_base", 0.0))

    converged = True
    for t in range(horizon):
        r = rev[t]; cg = cogs[t]
        selling = r * selling_r[t]; admin = r * admin_r[t]; rnd = r * rnd_r[t]
        ebitda = r - cg - selling - admin - rnd
        d = dep[t]; am = amort[t]
        ebit = ebitda - d - am
        # 利息 (期初余额)
        lt_close = st["lt_loan"] + lt_add[t]
        bond_close = st["bonds"] + bond_add[t]
        int_exp = st["revolver"] * rev_rate[t] + (st["lt_loan"] + lt_close) / 2 * lt_rate[t] \
            + (st["bonds"] + bond_close) / 2 * bond_rate[t]
        int_inc = st["cash"] * cash_yield[t]
        nonrecur = (obp[t] + adisp[t]) + ifv[t] + (iinc[t] + fx[t] + subsidy[t] + nonop[t])
        pretax = ebit - int_exp + int_inc + nonrecur + fx[t]
        tax = pretax * tax_rate[t]
        minority_loss = (pretax / prev_pretax * prev_minority_loss) if prev_pretax else 0.0
        net = pretax - tax + minority_loss
        eps = net / share_cnt[t] if share_cnt[t] else None
        dividend = net * payout[t] if net * payout[t] > 0 else 0.0

        # 营运资本 (期末)
        ar = r * ar_days[t] / 365.0
        inventory = cg * inv_days[t] / 365.0
        prepaid = r * prepaid_r[t]
        ap = cg * ap_days[t] / 365.0
        accrued = (selling + admin) * accrued_r[t]
        other_cl = (cg + selling + admin) * other_cl_r[t]
        lt_equity = st["lt_equity"] + lt_eq_add[t]
        other_ltl = st["other_ltl"] + other_ltl_add[t]

        # 现金流量表 (间接法)
        d_ar = st["ar"] - ar; d_inv = st["inventory"] - inventory; d_prepaid = st["prepaid"] - prepaid
        d_ap = ap - st["ap"]; d_accrued = accrued - st["accrued"]; d_other_cl = other_cl - st["other_cl"]
        d_other_ltl = other_ltl - st["other_ltl"]
        # 固定资产减值准备变动(加回): 当期计提 - 上期计提
        cur_impair = ppe_net[t] * impair_rate
        prev_impair = (float(_g(op, "impair_prov", float(_g(op, "ppe_net")) * impair_rate))
                       if t == 0 else ppe_net[t - 1] * impair_rate)
        d_impair = cur_impair - prev_impair
        dWC = d_ar + d_inv + d_prepaid + d_ap + d_accrued + d_other_cl + d_other_ltl + d_impair
        # 经营现金流: 税后利润 - 少数股东 - 公允价值变动 + 折旧摊销 + 营运资金净变动
        cfo = net + (-minority_loss) + (-ifv[t]) + (d + am) + dWC

        d_st_invest = st["st_invest"] - st_invest[t]
        d_other_lta = st["other_lta"] - other_lt_assets[t]
        cfi = d_st_invest - lt_eq_add[t] - intan_inv_t(A, t, horizon) - capex_out[t] + d_other_lta
        cff_ex = equity_issue[t] + (lt_close - st["lt_loan"]) + (bond_close - st["bonds"]) - dividend

        cash_before = st["cash"] + cfo + cfi + cff_ex
        # 循环贷 plug (复刻 债务预测: 现金缺口则增贷, 富余则还到最低)
        surplus = cash_before - min_cash[t]
        excess_rev = st["revolver"] - min_rev[t]
        d_revolver = -min(surplus, excess_rev)
        revolver_close = st["revolver"] + d_revolver
        if surplus > excess_rev:
            cash_close = min_cash[t] + (surplus - excess_rev)
        else:
            cash_close = min_cash[t]
        cff = cff_ex + d_revolver
        net_change = cfo + cfi + cff

        # 资产负债表
        ppe_net_v = ppe_net[t]
        impair_prov = -ppe_net_v * impair_rate
        ppe_net_after = ppe_net_v + impair_prov
        cur_assets = cash_close + st_invest[t] + ar + inventory + prepaid
        fixed_total = ppe_net_after + cip[t]
        equity_close = st["equity"] + net - dividend + equity_issue[t]
        minority_close = st["minority"] - minority_loss * (1 - minority_payout[t])
        total_assets = cur_assets + fixed_total + intan[t] + lt_equity + other_lt_assets[t]
        cur_liab = revolver_close + ap + accrued + other_cl
        total_liab = cur_liab + lt_close + other_ltl + bond_close
        total_le = total_liab + minority_close + equity_close
        check = total_assets - total_le

        income.append({
            "year": _year_label(base, t), "is_forecast": True,
            "revenue": r, "cogs": cg, "gross_profit": r - cg,
            "selling": selling, "admin": admin, "rnd": rnd, "ebitda": ebitda,
            "dep": d, "amort": am, "ebit": ebit, "int_exp": int_exp, "int_inc": int_inc,
            "nonrecurring": nonrecur, "pretax": pretax, "tax": tax, "minority": minority_loss,
            "net_income": net, "eps": eps, "dividend": dividend,
        })
        balance.append({
            "year": _year_label(base, t), "is_forecast": True,
            "cash": cash_close, "st_invest": st_invest[t], "ar": ar, "inventory": inventory,
            "prepaid": prepaid, "cur_assets": cur_assets, "ppe_net": ppe_net_after, "cip": cip[t],
            "intangible": intan[t], "lt_equity": lt_equity, "other_lt_assets": other_lt_assets[t],
            "total_assets": total_assets, "revolver": revolver_close, "ap": ap, "accrued": accrued,
            "other_cl": other_cl, "cur_liab": cur_liab, "lt_loan": lt_close, "bonds": bond_close,
            "other_lt_liab": other_ltl, "total_liab": total_liab, "minority": minority_close,
            "equity": equity_close, "total_le": total_le,
        })
        cashflow.append({
            "year": _year_label(base, t), "is_forecast": True,
            "net_income": net, "da": d + am, "fair_value": -ifv[t], "dWC": dWC, "cfo": cfo,
            "capex": -capex_out[t], "intan_invest": -intan_inv_t(A, t, horizon), "cfi": cfi,
            "equity_issue": equity_issue[t], "d_lt_loan": lt_close - st["lt_loan"],
            "dividend": -dividend, "d_revolver": d_revolver, "cff": cff,
            "net_change": net_change, "cash_end": cash_close,
        })
        checks.append({"year": _year_label(base, t), "residual": check,
                       "balanced": (abs(check) / total_assets < 1e-4) if total_assets else False})

        # 滚动
        prev_impair = cur_impair
        prev_pretax = pretax if pretax else prev_pretax
        prev_minority_loss = minority_loss
        st = {
            "cash": cash_close, "revolver": revolver_close, "lt_loan": lt_close, "bonds": bond_close,
            "equity": equity_close, "minority": minority_close, "ar": ar, "inventory": inventory,
            "prepaid": prepaid, "ap": ap, "accrued": accrued, "other_cl": other_cl,
            "st_invest": st_invest[t], "lt_equity": lt_equity, "other_lta": other_lt_assets[t],
            "other_ltl": other_ltl, "ppe_net_imp": ppe_net_after, "cip": cip[t], "intangible": intan[t],
        }

    valuation = _valuation(base, A, income, cashflow, horizon)
    return {"income": income, "balance": balance, "cashflow": cashflow,
            "checks": checks, "per_line": per_line, "valuation": valuation,
            "meta": {"converged": converged, "horizon": horizon, "unit": "百万元"}}


def intan_inv_t(A, t, horizon):
    return _vec(A, "intan_invest", horizon, 0.0)[t]


def _year_label(base, t):
    by = base.get("base_year")
    try:
        return str(int(by) + t + 1) + "E"
    except Exception:
        return f"+{t+1}"


def _valuation(base, A, income, cashflow, horizon):
    """标准 FCFF-DCF: FCFF=EBIT(1-T)+D&A-ΔWC(净)-capex; Gordon 终值; WACC=CAPM(无杠杆β→有杠杆β)。"""
    rf = float(_g(A, "rf", 0.0328)); erp = float(_g(A, "erp", 0.08))
    beta_a = float(_g(A, "asset_beta", 0.8876)); kd = float(_g(A, "kd", 0.0475))
    t_wacc = float(_g(A, "tax_for_wacc", 0.1354)); g = float(_g(A, "tv_growth", 0.05))
    price = float(_g(A, "price", 0.0)); share = _vec(A, "share_count", horizon, 1.0)[-1]
    last_bs = None
    # 净债务 = 有息负债 - 现金 (用末年)
    fcff = []
    for t in range(horizon):
        inc = income[t]; cf = cashflow[t]
        ebit_at = inc["ebit"] * (1 - inc["tax"] / inc["pretax"] if inc["pretax"] else (1 - t_wacc))
        f = ebit_at + cf["da"] + cf["dWC"] - (-cf["capex"])
        fcff.append(f)
    # 资本结构 (末年)
    equity_mv = price * share if price and share else None
    debt = 0.0
    netdebt = float(_g(base["opening"], "net_debt", 0.0))
    # 有杠杆 beta & WACC
    de = (debt / equity_mv) if equity_mv else 0.0
    beta_e = beta_a * (1 + (1 - t_wacc) * de)
    ke = rf + beta_e * erp
    if equity_mv:
        wacc = equity_mv / (equity_mv + debt) * ke + debt / (equity_mv + debt) * kd * (1 - t_wacc)
    else:
        wacc = ke
    pv = 0.0
    for t in range(horizon):
        pv += fcff[t] / ((1 + wacc) ** (t + 1))
    tv = fcff[-1] * (1 + g) / (wacc - g) if wacc > g else 0.0
    pv_tv = tv / ((1 + wacc) ** horizon)
    ev = pv + pv_tv
    equity_val = ev - netdebt
    per_share = equity_val / share if share else None
    return {"wacc": wacc, "ke": ke, "beta_e": beta_e, "fcff": fcff, "pv_fcff": pv,
            "tv": tv, "pv_tv": pv_tv, "ev": ev, "net_debt": netdebt,
            "equity_value": equity_val, "per_share": per_share, "price": price}
