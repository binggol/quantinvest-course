"""基本面+行业+事件特征 -> qlib .bin, 给 RD-Agent 因子挖矿喂量价之外的正交维度。
PIT 防未来函数: 每个交易日 t 取值 = ann_date<=t 的最近一期(财报/快报/预告级联, 高精度覆盖低精度)。
特征:
  基础财务(ann_date阶梯对齐, winsorize):
    $gm 毛利率[0,1] | $dedt_yoy 扣非同比[-2,2] | $roe [-1,1] | $debt 资产负债率[0,1]
  行业聚合(每日截面, 让LLM写 ($gm-$ind_mean_gm)/$ind_std_gm = 行业中性):
    $ind_mean_gm | $ind_std_gm | $ind_mean_dedt_yoy
  业绩预告/快报 PIT级联 + 信息衰减:
    $perf_skew 业绩超预期偏离(PIT净利 vs 去年同期)[-1,1]
    $fund_fresh 财报新鲜度 exp(-ln2/15 * 距上次披露天数) (0,1] -> 抓PEAD
用法: python scripts/dump_fundamental_features.py
(token: 环境变量 TUSHARE_TOKEN 或 data/.tushare_token; qlib数据目录用 QI_QLIB_DIR 覆盖)
"""
import os
import time
import numpy as np
import pandas as pd
import tushare as ts
from pathlib import Path


def _load_token():
    t = os.environ.get("TUSHARE_TOKEN")
    if t: return t.strip()
    for p in (Path(os.environ.get("QI_RDAGENT_DIR", "C:/rdagent")) / "data" / ".tushare_token",
              Path(__file__).resolve().parents[1] / "data" / ".tushare_token"):
        if p.exists(): return p.read_text(encoding="utf-8").strip()
    raise SystemExit("缺tushare token: 设环境变量 TUSHARE_TOKEN 或放 data/.tushare_token")


TOKEN = _load_token()
QROOT = Path(os.environ.get("QI_QLIB_DIR", "C:/qlib_data/cn_data"))
LAMBDA = np.log(2) / 15.0  # 财报新鲜度半衰期15天

cal = QROOT.joinpath("calendars/day.txt").read_text().split()
CAL = pd.to_datetime(cal)
FIN_FIELDS = {"grossprofit_margin": ("gm", 100.0, (0.0, 1.0)),
              "dt_netprofit_yoy": ("dedt_yoy", 100.0, (-2.0, 2.0)),
              "roe": ("roe", 100.0, (-1.0, 1.0)),
              "debt_to_assets": ("debt", 100.0, (0.0, 1.0))}


def ts2q(c):
    a, e = c.split("."); return f"{e.lower()}{a}"


def periods():
    return [f"{y}{m}" for y in range(2010, pd.Timestamp.now().year + 1) for m in ("0331", "0630", "0930", "1231")]


def _vip(pro, fn, p, fields):
    for _ in range(3):
        try:
            d = getattr(pro, fn)(period=p, fields=fields)
            if d is not None: return d
        except Exception:
            time.sleep(8)
    return None


def asof_panel(events, valuecol, codes):
    """events: df[code, ann_dt, value]; 返回 日历×code 的 PIT 阶梯面板(ann_date<=t 的最近值)."""
    pan = pd.DataFrame(index=CAL, columns=codes, dtype="float32")
    for code, sub in events.groupby("code"):
        s = sub.dropna(subset=[valuecol]).sort_values("ann_dt").drop_duplicates("ann_dt", keep="last")
        if s.empty: continue
        ser = pd.Series(s[valuecol].values, index=pd.to_datetime(s["ann_dt"].values))
        ser = ser[~ser.index.duplicated(keep="last")].sort_index()
        pan[code] = ser.reindex(CAL.union(ser.index)).ffill().reindex(CAL).values
    return pan


Z_FEAT = Path("Z:/claude/qlib/data/cn_data/features")  # robocopy /MIR 源, 双写自愈防冲掉


def dump_bin(code, name, series):
    vals = series.values.astype("float32")
    valid = np.where(~np.isnan(vals))[0]
    if len(valid) == 0: return False
    start = int(valid[0])
    out = np.concatenate([[np.float32(start)], vals[start:]]).astype("<f4")
    for root in (QROOT / "features", Z_FEAT):  # 双写: C挖矿读 + Z源保留
        try:
            d = root / code; d.mkdir(parents=True, exist_ok=True)
            out.tofile(d / f"{name}.day.bin")
        except Exception:
            pass
    return True


def main():
    pro = ts.pro_api(TOKEN)
    # ---- 1) 基础财务(fina_indicator) ----
    fin = []
    fcols = "ts_code,ann_date,end_date,profit_dedt," + ",".join(FIN_FIELDS)
    for p in periods():
        d = _vip(pro, "fina_indicator_vip", p, fcols)
        if d is not None and len(d): fin.append(d); print(f"  fina {p}:{len(d)}", flush=True)
        time.sleep(0.4)
    fin = pd.concat(fin, ignore_index=True).dropna(subset=["ann_date", "ts_code"])
    fin["code"] = fin["ts_code"].map(ts2q); fin["ann_dt"] = pd.to_datetime(fin["ann_date"])
    codes = sorted(fin["code"].unique())
    print(f"基础财务: {len(codes)}股", flush=True)

    # ---- 2) 业绩预告(forecast: 区间取中位) + 快报(express: 单点) 做PIT级联 ----
    fc = []
    for p in periods():
        d = _vip(pro, "forecast_vip", p, "ts_code,ann_date,end_date,net_profit_min,net_profit_max")
        if d is not None and len(d): fc.append(d)
        time.sleep(0.4)
    ex = []
    for p in periods():
        d = _vip(pro, "express_vip", p, "ts_code,ann_date,end_date,n_income")
        if d is not None and len(d): ex.append(d)
        time.sleep(0.4)
    # 统一成 (code, ann_dt, end_date, profit, level): 预告1<快报2<财报3
    rows = []
    if fc:
        fc = pd.concat(fc, ignore_index=True)
        fc["profit"] = (pd.to_numeric(fc["net_profit_min"], errors="coerce") + pd.to_numeric(fc["net_profit_max"], errors="coerce")) / 2
        rows.append(fc.assign(code=fc.ts_code.map(ts2q), ann_dt=pd.to_datetime(fc.ann_date), level=1)[["code", "ann_dt", "end_date", "profit", "level"]])
    if ex:
        ex = pd.concat(ex, ignore_index=True)
        rows.append(ex.assign(code=ex.ts_code.map(ts2q), ann_dt=pd.to_datetime(ex.ann_date), profit=pd.to_numeric(ex.n_income, errors="coerce"), level=2)[["code", "ann_dt", "end_date", "profit", "level"]])
    rows.append(fin.assign(profit=pd.to_numeric(fin.profit_dedt, errors="coerce"), level=3)[["code", "ann_dt", "end_date", "profit", "level"]])
    allp = pd.concat(rows, ignore_index=True).dropna(subset=["profit"])
    # PIT: 同一(code,end_date)在某ann_dt保留level最高; perf_skew = (本期PIT净利 - 去年同期最终值)/|去年同期|
    allp["end_date"] = allp["end_date"].astype(str)
    fin_final = fin.assign(profit=pd.to_numeric(fin.profit_dedt, errors="coerce")).dropna(subset=["profit"])
    prior = {(r.code, str(r.end_date)): r.profit for r in fin_final.itertuples()}  # 去年同期用财报最终值
    def py(code, end):
        try: e = str(int(end[:4]) - 1) + end[4:8]
        except Exception: return np.nan
        return prior.get((code, e), np.nan)
    allp["prior"] = [py(c, e) for c, e in zip(allp.code, allp.end_date)]
    allp["skew"] = (allp["profit"] - allp["prior"]) / allp["prior"].abs().replace(0, np.nan)
    # 每个披露事件用其 skew; 取 ann_dt 上 level 最高
    ev = allp.dropna(subset=["skew"]).sort_values(["level"]).drop_duplicates(["code", "ann_dt"], keep="last")
    perf = asof_panel(ev.rename(columns={"skew": "v"})[["code", "ann_dt", "v"]], "v", codes).clip(-1, 1)

    # ---- 3) 行业(stock_basic) ----
    sb = pro.stock_basic(exchange="", fields="ts_code,industry")
    ind_map = {ts2q(r.ts_code): (r.industry or "NA") for r in sb.itertuples()}

    # ---- 4) 构建基础财务面板 + 行业聚合 + 新鲜度 ----
    panels = {}
    for tsf, (qn, div, (lo, hi)) in FIN_FIELDS.items():
        ev2 = fin.assign(v=pd.to_numeric(fin[tsf], errors="coerce") / div)[["code", "ann_dt", "v"]]
        panels[qn] = asof_panel(ev2, "v", codes).clip(lo, hi)
    # 行业聚合(每日截面 group by 行业)
    inds = pd.Series({c: ind_map.get(c, "NA") for c in codes})
    for src, mname, sname in [("gm", "ind_mean_gm", "ind_std_gm"), ("dedt_yoy", "ind_mean_dedt_yoy", None)]:
        P = panels[src]
        gm_mean = P.T.groupby(inds).transform("mean").T  # 每日每股 -> 其行业当日均值
        panels[mname] = gm_mean
        if sname:
            panels[sname] = P.T.groupby(inds).transform("std").T.fillna(1e-4)
    # 财报新鲜度: 距上次任意财报披露天数 -> exp(-lambda*days) (PEAD); 按股searchsorted算, 不走float32面板免精度丢失
    cal_ord = CAL.values.astype("datetime64[D]").astype(int)
    fresh = pd.DataFrame(index=CAL, columns=codes, dtype="float32")
    for code, sub in fin.groupby("code"):
        anns = np.sort(sub["ann_dt"].values.astype("datetime64[D]").astype(int))
        if not len(anns): continue
        pos = np.searchsorted(anns, cal_ord, side="right") - 1  # 每个交易日: 最近一次<=它的披露
        ds = np.where(pos >= 0, cal_ord - anns[np.clip(pos, 0, len(anns) - 1)], np.nan)
        fresh[code] = np.where(pos >= 0, np.exp(-LAMBDA * np.clip(ds, 0, 400)), np.nan).astype("float32")
    panels["fund_fresh"] = fresh
    panels["perf_skew"] = perf

    # ---- 特征元信息(覆盖率/范围/样例) 给基本面特征页 ----
    import json
    meta = {"updated": time.strftime("%Y-%m-%d %H:%M"), "n_codes": len(codes), "n_days": len(CAL), "features": []}
    RANGE = {"gm": (0, 1), "dedt_yoy": (-2, 2), "roe": (-1, 1), "debt": (0, 1), "perf_skew": (-1, 1), "fund_fresh": (0, 1)}
    for name, P in panels.items():
        v = P.values[~np.isnan(P.values)]
        if not len(v): continue
        cov = float(np.isfinite(P.values).mean())
        meta["features"].append({"name": name, "coverage": round(cov, 3),
            "min": round(float(np.min(v)), 4), "max": round(float(np.max(v)), 4),
            "mean": round(float(np.mean(v)), 4), "median": round(float(np.median(v)), 4),
            "winsor": RANGE.get(name), "kind": ("行业聚合" if name.startswith("ind_") else ("事件/衰减" if name in ("perf_skew", "fund_fresh") else "基础财务")),
            "hist": [int(x) for x in np.histogram(np.clip(v, *RANGE.get(name, (float(np.min(v)), float(np.max(v))))), bins=12)[0]]})

    # ---- 5) dump ----
    n = 0
    for code in codes:
        wrote = False
        for name, P in panels.items():
            if code in P.columns and dump_bin(code, name, P[code]): wrote = True
        if wrote:
            n += 1
            if n % 800 == 0: print(f"  dump {n}股", flush=True)
    meta["n_dumped"] = n
    out_dir = Path(os.environ.get("QI_RDAGENT_DIR", "C:/rdagent"))
    out_dir.joinpath("fund_features_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    try:
        import shutil; shutil.copy(out_dir / "fund_features_meta.json", r"Z:\claude\qlib\data\csv_tmp\fund_features_meta.json")
    except Exception as e:
        print("NAS copy:", e)
    print(f"[Done] {n}股 x {len(panels)}特征: {list(panels)} -> fund_features_meta.json", flush=True)


if __name__ == "__main__":
    main()
