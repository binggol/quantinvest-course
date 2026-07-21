#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从缓存的预测分数秒级重测(免重训慢模型)。
依赖 run_model.save_artifacts 存下的 _cache/pred__<batch>__<model>.parquet。

用法(WSL rdagent env):
  python fast_backtest.py <batch> <model> [--topk 50] [--ndrop 5] [--liqfilter 0.33] [--addcost 0.0025]
例:
  python fast_backtest.py alpha158_csi1000 timesnet                 # 复现全池
  python fast_backtest.py alpha158_csi1000 timesnet --liqfilter 0.33 --addcost 0.0025  # 流动性过滤+冲击成本
列出已缓存:
  python fast_backtest.py --list
"""
import sys, json, argparse
from pathlib import Path
import pandas as pd

CACHE = Path("/mnt/c/rdagent/_cache")


def _safe_key(s):
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(s))


def load_pred(batch, model):
    p = CACHE / f"pred__{_safe_key(batch)}__{_safe_key(model)}.parquet"
    if not p.exists():
        raise SystemExit(f"无缓存预测: {p}\n先用 run_model 跑一次该(batch,model)生成缓存, 或 --list 看已有的。")
    s = pd.read_parquet(p)["score"]
    return s


def liq_mask(pred, frac):
    """剔除每日ADV最低 frac 的票(流动性过滤), 返回过滤后的pred。"""
    from qlib.data import D
    insts = sorted(set(pred.index.get_level_values(1)))
    d0 = str(pred.index.get_level_values(0).min())[:10]
    d1 = str(pred.index.get_level_values(0).max())[:10]
    adv = D.features(insts, ["Mean($volume*$close, 20)"], start_time=d0, end_time=d1)
    adv.columns = ["adv"]
    advs = adv["adv"].swaplevel().sort_index()
    df = pd.DataFrame({"pred": pred, "adv": advs}).dropna()
    cut = df.groupby(level=0)["adv"].transform(lambda x: x.quantile(frac))
    kept = df.loc[df["adv"] >= cut, "pred"]
    return kept, len(kept) / max(len(df), 1)


def backtest(pred, bench, start, end, topk, ndrop, addcost):
    import qlib
    from qlib.contrib.strategy import TopkDropoutStrategy
    from qlib.backtest import backtest as qb, executor as ex_mod
    from qlib.contrib.evaluate import risk_analysis
    strat = TopkDropoutStrategy(signal=pred, topk=topk, n_drop=ndrop)
    ex = ex_mod.SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True)
    exk = {"limit_threshold": 0.095, "deal_price": "close",
           "open_cost": 0.0005 + addcost, "close_cost": 0.0015 + addcost, "min_cost": 5}
    pm, _ = qb(executor=ex, strategy=strat, start_time=start, end_time=end,
               account=100000000, benchmark=bench, exchange_kwargs=exk)
    report, _ = pm.get("1day")
    exc = risk_analysis(report["return"] - report["bench"] - report["cost"], freq="day")
    g = lambda k: float(exc.loc[k, "risk"])
    return {"ir": round(g("information_ratio"), 3),
            "excess_ann": round(g("annualized_return"), 4),
            "mdd": round(g("max_drawdown"), 4)}


def main():
    if "--list" in sys.argv:
        idx = CACHE / "index.json"
        if idx.exists():
            for k, v in json.loads(idx.read_text(encoding="utf-8")).items():
                print(f"  {k}: {v.get('dates')} n={v.get('n_pred')} saved={v.get('saved_at')}")
        else:
            print("无缓存 (run_model 跑过才有)。")
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("batch"); ap.add_argument("model")
    ap.add_argument("--topk", type=int, default=50); ap.add_argument("--ndrop", type=int, default=5)
    ap.add_argument("--liqfilter", type=float, default=0.0, help="剔除每日ADV最低该比例(0.33=剔最低1/3); 0=不过滤")
    ap.add_argument("--addcost", type=float, default=0.0, help="单边额外成本(冲击代理), 如0.0025=+25bps")
    a = ap.parse_args()

    bench = {"alpha158_csi300": "SH000300", "alpha158_csi500": "SH000905",
             "alpha158_csi1000": "SH000852"}.get(a.batch, "SH000300")
    import qlib
    qlib.init(provider_uri="/mnt/c/qlib_data/cn_data", region="cn")
    pred = load_pred(a.batch, a.model)
    start = str(pred.index.get_level_values(0).min())[:10]
    # 回测结束日留 T+1 撮合缓冲
    from qlib.data import D
    cal = [str(x)[:10] for x in D.calendar(start_time=start)]
    end = cal[-2] if len(cal) >= 2 else cal[-1]
    kept_frac = 1.0
    if a.liqfilter > 0:
        pred, kept_frac = liq_mask(pred, a.liqfilter)
    r = backtest(pred, bench, start, end, a.topk, a.ndrop, a.addcost)
    print(json.dumps({"batch": a.batch, "model": a.model, "topk": a.topk, "ndrop": a.ndrop,
                      "liqfilter": a.liqfilter, "addcost": a.addcost, "kept_frac": round(kept_frac, 3),
                      **r}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
