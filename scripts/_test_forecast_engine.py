"""离线验证: 用 Excel(力量钻石 301071) 的 2021 期初 + 假设, 比对引擎复现 2022E-2024E。"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from forecast_engine import recompute

N = 3
base = {
    "code": "301071", "name": "力量钻石", "base_year": 2021, "rev_base": 498.3519,
    "opening": {
        "cash": 257.3899, "st_invest": 149.4, "ar": 61.0973, "inventory": 129.812,
        "prepaid_other_ca": 104.5938, "ppe_net": 610.2918, "cip": 27.5363,
        "intangible": 28.86, "lt_equity_invest": 2.9463, "other_lt_assets": 52.5406,
        "revolver": 16.484, "ap": 279.1429, "accrued": 0.0, "other_cl": 58.7979,
        "lt_loan": 44.94, "bonds": 0.0, "other_lt_liab": 65.8536, "minority": 0.0,
        "equity": 958.3184, "impair_prov": 0.9312, "pretax_base": 277.0587,
        "minority_loss_base": 0.0, "share_count": 144.7068, "net_debt": -2617.2383,
    },
    "product_lines": None,
}
A = {
    "growth": [1.0347, 0.6195, 0.3928], "cogs_to_rev": [0.2966, 0.2717, 0.2695],
    "admin_to_rev": 0.0299, "rnd_to_rev": 0.0533, "selling_to_rev": 0.0112, "optax_to_rev": 0.0077,
    "tax_rate": 0.1354, "payout": 0.0104,
    "ar_days": [44.7, 120, 120], "inv_days": 345.3, "prepaid_to_rev": 0.21,
    "ap_days": 742.5429, "accrued_ratio": 0.0, "other_cl_ratio": 0.3728,
    "lt_equity_add": 0.0, "other_lt_liab_add": 0.0, "minority_payout": 0.01,
    "capex_new": [1398, 1667, 1013], "cip_to_fixed_ratio": 0.5, "reno_ratio": 0.1,
    "dep_life": 10, "dep_life_new": 10, "impair_rate": 0.005,
    "intan_invest": 15, "amort_life": 23, "other_lt_assets": 750,
    "min_cash": 257, "min_revolver": 16, "revolver_rate": 0.04,
    "lt_loan_add": 20, "lt_loan_rate": 0.04, "bond_add": 0, "bond_rate": 0.04, "cash_yield": 0.015,
    "st_invest": 149.4, "other_biz_profit": 12.1, "asset_disposal": 0, "impair_fv": 0.9,
    "invest_income": 0, "fx": 0, "subsidy": 0, "nonop": 2,
    "equity_issuance": [4000, 0, 0], "share_count": 144.7068,
    "asset_beta": 0.8876, "rf": 0.0328, "erp": 0.08, "kd": 0.0475, "tax_for_wacc": 0.1354,
    "tv_growth": 0.05, "price": 161.75,
}

EXPECT = {  # Excel 计算值 2022E/2023E/2024E
    "revenue":      [1013.986, 1642.1086, 2287.0939],
    "net_income":   [461.1934, 773.5408, 1012.5426],
    "eps":          [3.1871, 5.3456, 6.9972],
    "total_assets": [6301.0192, 7446.0555, 8887.9404],
    "cash_end":     [2802.8321, 1784.7656, 2014.4163],
    "ebit":         [517.4143, 841.2757, 1133.7771],
    "dep":          [98.1933, 197.2295, 317.9653],
}

r = recompute(base, A, None, N)
inc, bal, cf, chk = r["income"], r["balance"], r["cashflow"], r["checks"]


def cmp(name, got, exp):
    ok = True
    for t in range(N):
        g, e = got[t], exp[t]
        rel = abs(g - e) / (abs(e) if e else 1)
        flag = "OK " if rel < 0.01 else "XX "
        if rel >= 0.01:
            ok = False
        print(f"  {flag}{name:14s} {['2022E','2023E','2024E'][t]}: got={g:12.3f}  exp={e:12.3f}  rel={rel*100:6.3f}%")
    return ok


allok = True
allok &= cmp("revenue", [x["revenue"] for x in inc], EXPECT["revenue"])
allok &= cmp("net_income", [x["net_income"] for x in inc], EXPECT["net_income"])
allok &= cmp("eps", [x["eps"] for x in inc], EXPECT["eps"])
allok &= cmp("ebit", [x["ebit"] for x in inc], EXPECT["ebit"])
allok &= cmp("dep", [x["dep"] for x in inc], EXPECT["dep"])
allok &= cmp("total_assets", [x["total_assets"] for x in bal], EXPECT["total_assets"])
allok &= cmp("cash_end", [x["cash_end"] for x in cf], EXPECT["cash_end"])
print("\n  balance check residual:", [round(c["residual"], 4) for c in chk])
print("  valuation per_share (FCFF-DCF):", round(r["valuation"]["per_share"], 3) if r["valuation"]["per_share"] else None,
      " WACC:", round(r["valuation"]["wacc"], 4), " Ke:", round(r["valuation"]["ke"], 4))
print("\n==>", "ALL PASS" if allok else "MISMATCH")
sys.exit(0 if allok else 1)
