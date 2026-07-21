"""
Unified model runner for the web "model lab".

Given a model name (env RDAGENT_MODEL) and a factor batch (env RDAGENT_FACTOR_BATCH,
empty = default SOTA), trains the model on that batch's factor dataset, runs the same
TopkDropout backtest (with cost) used elsewhere, and appends the result to
C:\\rdagent\\model_results.json keyed by "<batch>::<model>". Stochastic models use
multiple seeds (env SEEDS, default "0,1,2"); their per-stock scores are averaged and
that same ensemble is backtested, cached and published.  Per-seed dispersion is kept
as diagnostics.  Linear models are deterministic (one run).

Run (WSL rdagent env):
    RDAGENT_MODEL=xgb RDAGENT_FACTOR_BATCH=20260606_0309 python run_model.py

Supported models: lgb xgb catboost ols ridge lasso
"""
import os
import json
import pickle
import datetime
import math
import statistics
from pathlib import Path

try:
    from .factor_contract import (
        DEFAULT_ALL_FEATURES,
        FactorContract,
        apply_contract_to_handler,
        contract_from_effective_factors,
        contract_from_manifest,
    )
except ImportError:  # Direct execution from scripts/rdagent_backup.
    from factor_contract import (
        DEFAULT_ALL_FEATURES,
        FactorContract,
        apply_contract_to_handler,
        contract_from_effective_factors,
        contract_from_manifest,
    )

BT_RESULTS = Path("/mnt/c/rdagent/model_results.json")
HISTORY = Path("/mnt/c/rdagent/model_runs_history.json")
CURVES = Path("/mnt/c/rdagent/model_curves.json")  # 回测净值曲线 (供网页回测对比页画图)

EXECUTION_MODES = {
    # Qlib's daily signal is acted on at the next trade step.  Therefore a signal
    # intended for the next open must learn the following open-to-open return.
    "next_open": {
        "deal_price": "open",
        "label": "Ref($open, -2) / Ref($open, -1) - 1",
        "entry_timing": "next_trading_day_open",
        "return_horizon": "next_open_to_following_open",
    },
    "next_close": {
        "deal_price": "close",
        "label": "Ref($close, -2) / Ref($close, -1) - 1",
        "entry_timing": "next_trading_day_close",
        "return_horizon": "next_close_to_following_close",
    },
}


def resolve_execution_semantics(environ=None):
    """Resolve the one execution contract shared by training, backtest and live output.

    ``next_open`` is the production default.  The volume expression converts the
    Qlib CN provider's lot-based ``$volume`` to shares before applying the maximum
    participation rate.
    """

    env = os.environ if environ is None else environ
    raw_mode = str(env.get("RDAGENT_EXECUTION_MODE", "next_open")).strip().lower()
    mode = {"open": "next_open", "close": "next_close"}.get(raw_mode, raw_mode)
    if mode not in EXECUTION_MODES:
        raise ValueError(
            f"unsupported RDAGENT_EXECUTION_MODE={raw_mode!r}; "
            f"expected one of {sorted(EXECUTION_MODES)}"
        )
    try:
        participation = float(env.get("RDAGENT_MAX_VOLUME_PARTICIPATION", "0.05"))
    except (TypeError, ValueError) as exc:
        raise ValueError("RDAGENT_MAX_VOLUME_PARTICIPATION must be a number") from exc
    if not math.isfinite(participation) or not (0.0 < participation <= 1.0):
        raise ValueError("RDAGENT_MAX_VOLUME_PARTICIPATION must be in (0, 1]")

    resolved = dict(EXECUTION_MODES[mode])
    resolved.update(
        {
            "mode": mode,
            "max_volume_participation": participation,
            "volume_threshold": ("current", f"{participation:g} * $volume * 100"),
            "only_tradable": True,
            "score_transform": (
                "style_neutralized_size_momentum_volatility"
                if str(env.get("NEUTRALIZE_SCORE", "1")).strip().lower()
                not in {"0", "false", "no", "off", ""}
                else "raw"
            ),
        }
    )
    return resolved


def _label_slots(handler_kwargs):
    """Locate the mutable label config(s) for the handler.

    Alpha158-style handlers accept a handler-level ``label`` kwarg.  RD-Agent
    factor batches use a bare ``DataHandlerLP`` whose label lives inside the
    data-loader config; a handler-level ``label`` would make
    ``DataHandler.__init__`` raise ``TypeError``.
    """

    if "data_loader" not in handler_kwargs:
        return [handler_kwargs]

    slots = []

    def _walk(loader_cfg):
        if not isinstance(loader_cfg, dict):
            return
        kwargs = loader_cfg.get("kwargs") or {}
        config = kwargs.get("config")
        if isinstance(config, dict) and "label" in config:
            slots.append(config)
        for sub in kwargs.get("dataloader_l") or []:
            _walk(sub)

    _walk(handler_kwargs["data_loader"])
    return slots


def _read_label(slot):
    """Return the label expression list from a handler/loader label slot."""

    cur = slot.get("label")
    if isinstance(cur, (list, tuple)) and len(cur) == 2 and isinstance(cur[1], (list, tuple)):
        return list(cur[0])
    return list(cur) if isinstance(cur, (list, tuple)) else [cur]


def _write_label(slot, label_expr):
    """Replace the label expression, preserving explicit label column names."""

    cur = slot.get("label")
    if isinstance(cur, (list, tuple)) and len(cur) == 2 and isinstance(cur[1], (list, tuple)):
        slot["label"] = [[label_expr], list(cur[1])]
    else:
        slot["label"] = [label_expr]


def assert_execution_semantics(dataset_config, port_analysis_config, semantics):
    """Fail closed when label, backtest price or tradability rules diverge."""

    try:
        handler_kwargs = dataset_config["kwargs"]["handler"]["kwargs"]
        strategy_kwargs = port_analysis_config["strategy"]["kwargs"]
        exchange_kwargs = port_analysis_config["backtest"]["exchange_kwargs"]
    except (KeyError, TypeError) as exc:
        raise ValueError("incomplete RD-Agent dataset/backtest configuration") from exc

    expected_label = [semantics["label"]]
    slots = _label_slots(handler_kwargs)
    checks = {
        "label": bool(slots) and all(_read_label(s) == expected_label for s in slots),
        "deal_price": exchange_kwargs.get("deal_price") == semantics["deal_price"],
        "only_tradable": strategy_kwargs.get("only_tradable") is True,
        "volume_threshold": exchange_kwargs.get("volume_threshold") == semantics["volume_threshold"],
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise ValueError(f"execution semantics mismatch: {', '.join(failed)}")


def apply_execution_semantics(dataset_config, port_analysis_config, environ=None):
    """Patch and verify a config using the shared execution contract."""

    semantics = resolve_execution_semantics(environ)
    try:
        handler_kwargs = dataset_config["kwargs"]["handler"]["kwargs"]
        strategy_kwargs = port_analysis_config["strategy"]["kwargs"]
        exchange_kwargs = port_analysis_config["backtest"]["exchange_kwargs"]
    except (KeyError, TypeError) as exc:
        raise ValueError("incomplete RD-Agent dataset/backtest configuration") from exc

    slots = _label_slots(handler_kwargs)
    if not slots:
        raise ValueError("no label slot in RD-Agent dataset configuration")
    for slot in slots:
        _write_label(slot, semantics["label"])
    strategy_kwargs["only_tradable"] = True
    exchange_kwargs["deal_price"] = semantics["deal_price"]
    # A zero-volume/suspended stock has zero executable capacity.  Together with
    # only_tradable=True this prevents placeholder prices from becoming fills.
    exchange_kwargs["volume_threshold"] = semantics["volume_threshold"]
    assert_execution_semantics(dataset_config, port_analysis_config, semantics)
    return semantics

# (seed-key or None for deterministic, config, family). family: "tree" raw features, "linear" needs fillna+zscore,
#   "ptnn" 深度时序模型(TSLib 适配器, GeneralPTNN+TSDatasetH 序列输入, 见 Z:/claude/rdagent_model_lib)。
STEP_LEN = 8  # ptnn 序列窗口长度; 须与适配器 num_timesteps 一致


def _ptnn(adapter):
    """GeneralPTNN 包装一个 TSLib 适配器(adapter 模块名, 暴露 model_cls)。num_features 运行时按数据集真实特征数注入。"""
    return {"class": "GeneralPTNN", "module_path": "qlib.contrib.model.pytorch_general_nn",
            "kwargs": {"n_epochs": 30, "lr": 1e-3, "GPU": 0, "batch_size": 2048, "early_stop": 8,
                       "metric": "loss", "n_jobs": 8, "pt_model_uri": f"{adapter}.model_cls",
                       "pt_model_kwargs": {"num_features": 158, "num_timesteps": STEP_LEN}}}


def model_specs():
    return {
        "lgb": ("seed", {"class": "LGBModel", "module_path": "qlib.contrib.model.gbdt",
                         "kwargs": {"loss": "mse", "colsample_bytree": 0.8879, "learning_rate": 0.2,
                                    "subsample": 0.8789, "lambda_l1": 205.6999, "lambda_l2": 580.9768,
                                    "max_depth": 8, "num_leaves": 210, "num_threads": 20}}, "tree"),
        "xgb": ("seed", {"class": "XGBModel", "module_path": "qlib.contrib.model.xgboost",
                         "kwargs": {"eta": 0.05, "max_depth": 6, "colsample_bytree": 0.8,
                                    "subsample": 0.8, "nthread": 20, "lambda": 1.0, "alpha": 0.0}}, "tree"),
        "catboost": ("random_seed", {"class": "CatBoostModel", "module_path": "qlib.contrib.model.catboost_model",
                                     "kwargs": {"loss": "RMSE", "iterations": 1000, "learning_rate": 0.05,
                                                "depth": 8, "thread_count": 20}}, "tree"),
        "ols": (None, {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                       "kwargs": {"estimator": "ols"}}, "linear"),
        "ridge": (None, {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                         "kwargs": {"estimator": "ridge", "alpha": 1.0}}, "linear"),
        "lasso": (None, {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
                         "kwargs": {"estimator": "lasso", "alpha": 1e-3}}, "linear"),
        # 深度时序(TSLib 适配器). 单次确定性跑(seedkey=None)避免3×训练成本; GeneralPTNN 内部自管随机种子。
        "dlinear": (None, _ptnn("dlinear_model"), "ptnn"),       # 强线性基线, 训练秒级
        "patchtst": (None, _ptnn("patchtst_model"), "ptnn"),
        "timesnet": (None, _ptnn("timesnet_model"), "ptnn"),
        "itransformer": (None, _ptnn("itransformer_model"), "ptnn"),
    }


def _to_wsl(p):
    p = str(p).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = f"/mnt/{p[0].lower()}{p[2:]}"
    return p


def _workspace_identity(p):
    """Return a stable Windows-style identity for production manifests."""

    value = str(p).replace("\\", "/").rstrip("/")
    lower = value.lower()
    if lower.startswith("/mnt/") and len(value) > 6:
        drive = value[5].upper()
        return f"{drive}:{value[6:]}"
    return value


def resolve_workspace_and_contract(batch):
    if batch:
        man_path = Path(f"/mnt/c/rdagent/final/batches/{batch}.json")
        if not man_path.exists():
            raise SystemExit(f"[run_model] batch manifest not found: {man_path}")
        man = json.loads(man_path.read_text(encoding="utf-8"))
        return Path(_to_wsl(man["workspace"])), contract_from_manifest(man)

    ptr = Path("/mnt/c/rdagent/sota_workspace.txt")
    raw = ptr.read_text(encoding="utf-8").strip() if ptr.exists() else "Z:/claude/rdagent_workspace/5dcf477aca8f4ac5bbbcb53092653051"
    effective_path = Path("/mnt/c/rdagent/final/effective_factors.json")
    effective = json.loads(effective_path.read_text(encoding="utf-8")) if effective_path.exists() else None
    return Path(_to_wsl(raw)), contract_from_effective_factors(effective)


def resolve_workspace(batch):
    """Backward-compatible workspace-only resolver."""

    workspace, _ = resolve_workspace_and_contract(batch)
    return workspace


def to_score_series(pred):
    import pandas as pd
    return pred.iloc[:, 0] if isinstance(pred, pd.DataFrame) else pred


def parse_model_seeds(seedkey, environ=None):
    """Return the configured stochastic seeds, rejecting ambiguous input."""

    if not seedkey:
        return [None]
    env = os.environ if environ is None else environ
    raw = str(env.get("SEEDS", "0,1,2"))
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError("SEEDS must contain at least one integer")
    try:
        seeds = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"invalid SEEDS={raw!r}; expected comma-separated integers") from exc
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"duplicate seed in SEEDS={raw!r}")
    return seeds


def mean_seed_predictions(seed_predictions):
    """Average per-stock scores across seeds with strict coverage validation.

    Silently averaging a varying number of seeds by stock makes the published
    portfolio impossible to reproduce.  All seed predictions therefore must have
    exactly the same (datetime, instrument) coverage and finite scores.
    """

    import numpy as np
    import pandas as pd

    if not seed_predictions:
        raise ValueError("at least one seed prediction is required")
    normalized = []
    reference_index = None
    for position, raw in enumerate(seed_predictions):
        score = to_score_series(raw).copy()
        if not isinstance(score, pd.Series):
            raise TypeError(f"seed prediction {position} is not a pandas Series/DataFrame")
        if score.index.has_duplicates:
            raise ValueError(f"seed prediction {position} contains duplicate rows")
        score = score.sort_index().astype(float)
        if reference_index is None:
            reference_index = score.index
        elif not score.index.equals(reference_index):
            raise ValueError(
                f"seed prediction coverage mismatch at position {position}: "
                f"expected {len(reference_index)} rows, got {len(score.index)}"
            )
        if not np.isfinite(score.to_numpy()).all():
            raise ValueError(f"seed prediction {position} contains NaN/inf scores")
        normalized.append(score)

    frame = pd.concat(
        [score.rename(f"seed_{position}") for position, score in enumerate(normalized)],
        axis=1,
        verify_integrity=True,
    )
    ensemble = frame.mean(axis=1)
    ensemble.name = "score"
    return ensemble


def neutralize_prediction_scores(prediction, environ=None):
    """Apply the exact live score transform to any historical prediction panel.

    This function is intentionally shared by ``run_model`` and
    ``predict_next_day``.  When neutralization is enabled, failure is closed by
    default so a raw-score backtest cannot silently authorize a neutralized live
    portfolio (or vice versa).
    """

    env = os.environ if environ is None else environ
    enabled = str(env.get("NEUTRALIZE_SCORE", "1")).strip().lower() not in {
        "0", "false", "no", "off", "",
    }
    score = to_score_series(prediction).copy().sort_index().astype(float)
    score.name = "score"
    if not enabled:
        return score

    try:
        import numpy as np
        import pandas as pd
        from qlib.data import D
        import feature_neutralize

        instruments = sorted(score.index.get_level_values("instrument").unique())
        first_date = score.index.get_level_values("datetime").min()
        last_date = score.index.get_level_values("datetime").max()
        feature_start = (pd.Timestamp(first_date) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
        features = D.features(
            instruments,
            ["$close", "$volume"],
            start_time=feature_start,
            end_time=pd.Timestamp(last_date).strftime("%Y-%m-%d"),
            freq="day",
        )
        close = features["$close"].unstack(level="instrument").sort_index()
        volume = features["$volume"].unstack(level="instrument").sort_index()
        style_size = np.log((close * volume).rolling(20, min_periods=10).mean().replace(0, np.nan))
        style_momentum = close.shift(1) / close.shift(21) - 1
        style_volatility = close.pct_change().rolling(20, min_periods=10).std()
        transformed = {}
        for date, values in score.groupby(level="datetime"):
            cross_section = values.copy()
            cross_section.index = cross_section.index.get_level_values("instrument")
            if date not in style_size.index:
                raise RuntimeError(f"style features unavailable for prediction date {date}")
            styles = {
                "size": style_size.loc[date].reindex(cross_section.index),
                "mom": style_momentum.loc[date].reindex(cross_section.index),
                "vol": style_volatility.loc[date].reindex(cross_section.index),
            }
            residual, _diagnostics = feature_neutralize.style_residual(cross_section, styles)
            if residual is None:
                raise RuntimeError(f"style neutralization returned no residual for {date}")
            transformed[date] = residual.reindex(cross_section.index)
        result = pd.concat(transformed).rename("score")
        result.index = result.index.set_names(["datetime", "instrument"])
        if not np.isfinite(result.to_numpy(dtype=float)).all():
            raise RuntimeError("style-neutralized scores contain NaN/inf")
        return result.sort_index()
    except Exception as exc:
        fail_open = str(env.get("NEUTRALIZE_SCORE_FAIL_OPEN", "0")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        if fail_open:
            print(f"[Neutralize] failed open to raw scores: {exc}", flush=True)
            return score
        raise RuntimeError(f"score neutralization failed closed: {exc}") from exc


def build_seed_ensemble_artifact(models, seeds, semantics):
    """Create a self-describing cache artifact for multi-seed live prediction."""

    if len(models) != len(seeds) or not models:
        raise ValueError("seed ensemble model/seed counts do not match")
    if len(models) == 1:
        # Preserve the old single-model pickle shape for callers explicitly using
        # one seed.  Its score is also the one-member ensemble score.
        return models[0]
    return {
        "artifact_type": "rdagent_score_mean_seed_ensemble_v1",
        "aggregation": "per_instrument_score_mean",
        "models": models,
        "seeds": list(seeds),
        "execution_mode": semantics["mode"],
        "label": semantics["label"],
        "score_transform": semantics["score_transform"],
    }


def rank_ic_stats(pred, label):
    import pandas as pd
    df = pd.concat([pred.rename("p"), label.rename("l")], axis=1).dropna()
    ic = df.groupby(level=0).apply(lambda g: g["p"].corr(g["l"], method="spearman")).dropna()
    m = ic.mean()
    return float(m), float(m / ic.std() if ic.std() else 0.0)


def run_backtest(pred, pac):
    from qlib.contrib.strategy import TopkDropoutStrategy
    from qlib.backtest import backtest as qb
    from qlib.backtest import executor as ex_mod
    from qlib.contrib.evaluate import risk_analysis
    bt = pac["backtest"]
    sk = dict(pac["strategy"]["kwargs"]); sk["signal"] = pred
    strat = TopkDropoutStrategy(**sk)
    ex = ex_mod.SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True)
    pm, _ = qb(executor=ex, strategy=strat, start_time=bt["start_time"], end_time=bt["end_time"],
               account=bt["account"], benchmark=bt["benchmark"], exchange_kwargs=bt["exchange_kwargs"])
    report, _ = pm.get("1day")
    exc = risk_analysis(report["return"] - report["bench"] - report["cost"], freq="day")
    strat_r = risk_analysis(report["return"] - report["cost"], freq="day")
    g = lambda d, k: float(d.loc[k, "risk"])
    # 累计净值曲线 (起点 1.0): 策略(扣成本) / 基准 / 超额, 供网页画图对比
    eq_strat = (1.0 + (report["return"] - report["cost"])).cumprod()
    eq_bench = (1.0 + report["bench"]).cumprod()
    eq_excess = (1.0 + (report["return"] - report["bench"] - report["cost"])).cumprod()
    curve = {
        "dates": [str(d)[:10] for d in report.index],
        "strat": [round(float(v), 4) for v in eq_strat.values],
        "bench": [round(float(v), 4) for v in eq_bench.values],
        "excess_nv": [round(float(v), 4) for v in eq_excess.values],
    }
    return {"excess": g(exc, "annualized_return"), "mdd": g(exc, "max_drawdown"),
            "ir": g(exc, "information_ratio"), "ann": g(strat_r, "annualized_return"), "curve": curve}


def liquidity_validate(pred, pac, univ, model):
    """流动性过滤 + 加冲击成本 复验(RDAGENT_LIQ_VALIDATE=1触发)。
    用已训模型的test预测, 跑3个回测对比: 全池 / 剔除每日ADV最低1/3(流动性过滤) / 过滤+加冲击成本。
    看高IR在剔掉买不进的低流动票+更真实成本后还守不守得住。写 liq_validate_<univ>_<model>.json。"""
    import copy as _cp, pandas as _pd
    from qlib.data import D as _D
    print("[liq-validate] 计算 20日ADV(成交额≈volume×close) 流动性...", flush=True)
    insts = sorted(set(pred.index.get_level_values(1)))
    d1 = str(pred.index.get_level_values(0).max())[:10]
    d0 = str(pred.index.get_level_values(0).min())[:10]
    adv = _D.features(insts, ["Mean($volume*$close, 20)"], start_time=d0, end_time=d1)
    adv.columns = ["adv"]
    advs = adv["adv"].swaplevel().sort_index()           # -> (datetime, instrument)
    df = _pd.DataFrame({"pred": pred, "adv": advs}).dropna()
    # 每日横截面: 保留ADV最高2/3(剔掉最低1/3=最难买的微盘/低流动)
    cut = df.groupby(level=0)["adv"].transform(lambda x: x.quantile(1/3))
    pred_filt = df.loc[df["adv"] >= cut, "pred"]
    kept_frac = len(pred_filt) / max(len(df), 1)
    print(f"[liq-validate] 过滤后保留 {kept_frac*100:.0f}% 的(日,股)样本, 跑回测...", flush=True)
    full = run_backtest(pred, pac)
    filt = run_backtest(pred_filt, pac)
    pac_hi = _cp.deepcopy(pac)                             # 加冲击成本代理(各边+25bps)
    ek = pac_hi["backtest"]["exchange_kwargs"]
    ek["open_cost"] = ek.get("open_cost", 0.0005) + 0.0025
    ek["close_cost"] = ek.get("close_cost", 0.0015) + 0.0025
    filt_hi = run_backtest(pred_filt, pac_hi)
    out = {"universe": univ, "model": model, "kept_frac": round(kept_frac, 3),
           "full": {"ir": round(full["ir"], 3), "excess": round(full["excess"], 4), "mdd": round(full["mdd"], 4)},
           "liq_filtered": {"ir": round(filt["ir"], 3), "excess": round(filt["excess"], 4), "mdd": round(filt["mdd"], 4)},
           "liq_filtered_impact": {"ir": round(filt_hi["ir"], 3), "excess": round(filt_hi["excess"], 4), "mdd": round(filt_hi["mdd"], 4)},
           "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    p = Path(f"/mnt/c/rdagent/liq_validate_{univ}_{model}.json")
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[liq-validate] === 复验结果 ===", flush=True)
    print(f"  全池            IR={out['full']['ir']:+.2f}  超额={out['full']['excess']*100:+.1f}%  回撤={out['full']['mdd']*100:.1f}%", flush=True)
    print(f"  流动性过滤      IR={out['liq_filtered']['ir']:+.2f}  超额={out['liq_filtered']['excess']*100:+.1f}%  回撤={out['liq_filtered']['mdd']*100:.1f}%", flush=True)
    print(f"  过滤+冲击成本   IR={out['liq_filtered_impact']['ir']:+.2f}  超额={out['liq_filtered_impact']['excess']*100:+.1f}%  回撤={out['liq_filtered_impact']['mdd']*100:.1f}%", flush=True)
    return out


CACHE_DIR = Path("/mnt/c/rdagent/_cache")


def _safe_key(s):
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(s))


def save_artifacts(batch, model, m_obj, pred, label):
    """缓存训练产物, 供未来秒级重算(免重训慢模型):
       pred(test每日打分)→回测/过滤/改成本/换topk; model.pkl→给新日期预测; label→算IC。
       key=<batch>__<model>。dataset不存(qlib Alpha158 handler pickle后_infer会丢, 踩过坑)。"""
    import pickle as _pk
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        k = _safe_key(f"{batch or 'default'}__{model}")
        pred.rename("score").to_frame().to_parquet(CACHE_DIR / f"pred__{k}.parquet")
        label.rename("label").to_frame().to_parquet(CACHE_DIR / f"label__{_safe_key(batch or 'default')}.parquet")
        with open(CACHE_DIR / f"model__{k}.pkl", "wb") as f:
            _pk.dump(m_obj, f)
        # 索引: 记录每个缓存的元信息, 便于fast_backtest查
        idx_p = CACHE_DIR / "index.json"
        idx = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p.exists() else {}
        idx[k] = {"batch": batch or "default", "model": model,
                  "n_pred": int(len(pred)),
                  "dates": [str(pred.index.get_level_values(0).min())[:10], str(pred.index.get_level_values(0).max())[:10]],
                  "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        idx_p.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[cache] 已存 pred/model/label -> _cache/*__{k}.* (未来重测/预测免重训)", flush=True)
    except Exception as e:
        print(f"[cache] 存档失败(不影响主流程): {e}", flush=True)


def save_result(batch, model, rec):
    store = {"results": []}
    if BT_RESULTS.exists():
        try:
            store = json.loads(BT_RESULTS.read_text(encoding="utf-8"))
        except Exception:
            store = {"results": []}
    key = f"{batch or 'default'}::{model}"
    store["results"] = [r for r in store.get("results", []) if r.get("key") != key]
    store["results"].append(rec)
    store["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    BT_RESULTS.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def save_curve(batch, model, curve, meta):
    """存回测净值曲线, key=<batch>::<model> (最新覆盖)。"""
    store = {"curves": {}}
    if CURVES.exists():
        try:
            store = json.loads(CURVES.read_text(encoding="utf-8"))
        except Exception:
            store = {"curves": {}}
    store.setdefault("curves", {})
    key = f"{batch or 'default'}::{model}"
    store["curves"][key] = {**meta, **curve}
    store["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    CURVES.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")


def append_history(rec):
    """Keep every run (not deduped) so the web can compare a model/batch across time."""
    hist = {"runs": []}
    if HISTORY.exists():
        try:
            hist = json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            hist = {"runs": []}
    runs = hist.get("runs", [])
    runs.append(rec)
    hist["runs"] = runs[-500:]   # cap to avoid unbounded growth
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    import copy
    import qlib
    from qlib.utils import init_instance_by_config

    model = os.environ.get("RDAGENT_MODEL", "lgb").strip().lower()
    batch = os.environ.get("RDAGENT_FACTOR_BATCH", "").strip()
    specs = model_specs()
    if model not in specs:
        raise SystemExit(f"unknown model {model}; supported: {list(specs)}")
    seedkey, base, family = specs[model]

    ws, factor_contract = resolve_workspace_and_contract(batch)
    print(f"[run_model] model={model} batch={batch or 'default'} ws={ws}", flush=True)
    
    cfg_ws = ws
    config_candidates = list(ws.glob("mlruns/*/*/artifacts/config"))
    if len(config_candidates) != 1:
        raise RuntimeError(
            f"Batch {batch or 'default'} workspace {ws} must contain exactly 1 "
            f"evaluated config; found {len(config_candidates)}"
        )
            
    cfg = pickle.load(open(config_candidates[0], "rb"))
    os.chdir(str(cfg_ws))
    cfg["qlib_init"]["provider_uri"] = "/mnt/c/qlib_data/cn_data"
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region=cfg["qlib_init"]["region"])
    pac = cfg["port_analysis_config"]

    # === ALPHA158 擂台模式: 用全 Alpha158(158因子)+csi300 回测任何模型, 结果归到 batch=alpha158 ===
    _A158 = os.environ.get("RDAGENT_ALPHA158", "").strip().lower() in ("1", "true", "yes")
    _univ = (os.environ.get("RDAGENT_UNIVERSE", "csi300").strip() or "csi300")  # csi300/csi500/csi1000/all
    _bench = {"csi300": "SH000300", "csi500": "SH000905", "csi1000": "SH000852"}.get(_univ, "SH000300")  # 各池用自己指数; all兜底沪深300
    if _A158:
        batch = f"alpha158_{_univ}"  # 结果按股票池区分
        factor_contract = FactorContract.disabled(DEFAULT_ALL_FEATURES)
        # 2026-06-21: 改为与24因子批次同期(近期)回测, 不再固定2010-23 -> 回测对比页可同轴比较。end由下方_latest延到最新交易日。
        # fit_start/end = 训练段 (qlib Alpha158 handler 强制要求非None; 用训练段拟合归一化, 无未来函数)
        _dh = {"start_time": "2010-01-01", "end_time": "2026-05-28",
               "fit_start_time": "2010-01-01", "fit_end_time": "2024-06-30", "instruments": _univ,
               "infer_processors": [{"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature", "clip_outlier": True}},
                                    {"class": "Fillna", "kwargs": {"fields_group": "feature"}}],
               "learn_processors": [{"class": "DropnaLabel"}, {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label"}}],
               "label": ["Ref($close, -2)/Ref($close, -1) - 1"]}
        cfg["task"]["dataset"] = {"class": "DatasetH", "module_path": "qlib.data.dataset",
            "kwargs": {"handler": {"class": "Alpha158", "module_path": "qlib.contrib.data.handler", "kwargs": _dh},
                       "segments": {"train": ["2010-01-01", "2024-06-30"], "valid": ["2024-07-01", "2025-06-30"],
                                    "test": ["2025-07-01", "2026-05-28"]}}}
        cfg["port_analysis_config"] = {
            "strategy": {"class": "TopkDropoutStrategy", "module_path": "qlib.contrib.strategy",
                         "kwargs": {"signal": "<PRED>", "topk": 50, "n_drop": 5}},
            "backtest": {"start_time": "2025-07-01", "end_time": "2026-05-28", "account": 100000000,
                         "benchmark": _bench,
                         "exchange_kwargs": {"limit_threshold": 0.095, "deal_price": "close",
                                             "open_cost": 0.0005, "close_cost": 0.0015, "min_cost": 5}}}
        pac = cfg["port_analysis_config"]
        print(f"[run_model] *** ALPHA158 擂台模式: 全 Alpha158 + {_univ}(基准{_bench}), model={model} ***", flush=True)

    # === 批次擂台模式: 用某批次因子在指定股票池回测(真路B). 非A158且非csi300时切 instruments+benchmark ===
    if not _A158 and batch and _univ != "csi300":
        try:
            cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["instruments"] = _univ
            if isinstance(cfg.get("data_handler_config"), dict):
                cfg["data_handler_config"]["instruments"] = _univ
            pac["backtest"]["benchmark"] = _bench
            print(f"[run_model] *** 批次擂台: 批次{batch} -> {_univ}(基准{_bench}, Alpha158现算+自定义因子全市场parquet取子集), model={model} ***", flush=True)
        except Exception as e:
            print(f"[run_model] 批次池override失败({e}), 退回批次默认csi300", flush=True)

    execution = apply_execution_semantics(cfg["task"]["dataset"], pac)
    print(
        f"[run_model] execution={execution['mode']} label={execution['return_horizon']} "
        f"deal_price={execution['deal_price']} only_tradable=1 "
        f"max_volume_participation={execution['max_volume_participation']:.1%}",
        flush=True,
    )

    ds_cfg = copy.deepcopy(cfg["task"]["dataset"])
    if family in ("linear", "ptnn"):   # 线性/深度网络都不吃 NaN; 截面zscore归一+填0
        hk = ds_cfg["kwargs"]["handler"].setdefault("kwargs", {})
        hk["infer_processors"] = [
            {"class": "CSZScoreNorm", "kwargs": {"fields_group": "feature"}},
            {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
        ]
    if family == "ptnn":
        # 深度时序: 把 DatasetH 换成 TSDatasetH(序列窗口 step_len), 喂 (B,T,F) 给 GeneralPTNN。
        ds_cfg["class"] = "TSDatasetH"
        ds_cfg["kwargs"]["step_len"] = STEP_LEN
        # 适配器(dlinear_model 等)与 TSLib 源的导入路径(WSL): 加到 sys.path + 设 TSLIB_PATH。
        import sys as _sys
        os.environ.setdefault("TSLIB_PATH", "/mnt/z/claude/Time-Series-Library")
        _mlib = "/mnt/z/claude/rdagent_model_lib"
        if _mlib not in _sys.path:
            _sys.path.insert(0, _mlib)
    # 配置把 handler 数据范围 / test 段 / 回测期 的结束日都写死到某旧日期(如 2026-05-28),
    # 导致回测停在那天。这里统一延到日历最新交易日, 让回测跑到最新。
    try:
        from qlib.data import D as _D
        _cal = _D.calendar(start_time="2025-06-01")
        _latest = str(_cal[-1])[:10]
        # qlib 回测撮合最后一次调仓需要"结束日的下一交易日"成交/结算; 若回测结束日=日历最后一天,
        # 没有 T+1 -> IndexError(index N out of bounds for size N)。故回测结束日留一天缓冲(倒数第二个交易日);
        # 测试/handler 段仍到最新日(预测需要最新一天)。
        _bt_end = str(_cal[-2])[:10] if len(_cal) >= 2 else _latest
    except Exception:
        _latest = None
        _bt_end = None
    # Optional OOS window used by the regime gate. Move train/validation before the
    # requested test period so the override cannot introduce look-ahead leakage.
    _bt_ts = os.environ.get("RDAGENT_BT_TEST_START", "").strip()
    _bt_te = os.environ.get("RDAGENT_BT_TEST_END", "").strip()
    if _bt_ts and _bt_te:
        _latest = None
        _yr = int(_bt_ts[:4])
        try:
            _segs = ds_cfg["kwargs"].get("segments")
            if _segs:
                _segs["train"] = ["2008-01-01", f"{_yr - 2}-12-31"]
                _segs["valid"] = [f"{_yr - 1}-01-01", f"{_yr - 1}-12-31"]
                _segs["test"] = [_bt_ts, _bt_te]
            _hk = ds_cfg["kwargs"]["handler"].setdefault("kwargs", {})
            _hk["end_time"] = _bt_te
            if "fit_end_time" in _hk:
                _hk["fit_end_time"] = f"{_yr - 2}-12-31"
            pac["backtest"]["start_time"] = _bt_ts
            pac["backtest"]["end_time"] = _bt_te
            print(
                f"[run_model] *** custom window: train..{_yr - 2} / "
                f"valid {_yr - 1} / test {_bt_ts}~{_bt_te} ***",
                flush=True,
            )
        except Exception as _e:
            print(f"[run_model] custom window setup failed: {_e}", flush=True)
    if _latest:   # 测试段/回测延到最新交易日 (A158 已改为近期同期, 也一起延)
        pac["backtest"]["end_time"] = _bt_end
        _hk = ds_cfg["kwargs"]["handler"].setdefault("kwargs", {})
        _hk["end_time"] = _latest
        _segs = ds_cfg["kwargs"].get("segments")
        if _segs and "test" in _segs:
            _segs["test"] = [_segs["test"][0], _latest]
        print(f"[run_model] 测试段结束日={_latest}, 回测结束日={_bt_end} (留 T+1 撮合缓冲)", flush=True)
    # === 训练进度心跳: 写 train_progress.json(+NAS), 网页轮询显示细粒度进度(建数据集/训练EpochN/回测), 不必干等 ===
    import shutil as _sh2, re as _re2, logging as _logging
    _PROG = Path("/mnt/c/rdagent/train_progress.json")
    _NAS_PROG = Path("/mnt/z/claude/qlib/data/csv_tmp/train_progress.json")
    _n_epochs = ((base.get("kwargs", {}) or {}).get("n_epochs"))
    def _prog(phase, epoch=None):
        try:
            _d = {"batch": batch or "default", "universe": _univ, "model": model, "family": family,
                  "phase": phase, "epoch": epoch, "n_epochs": _n_epochs, "alpha158": bool(_A158),
                  "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            _PROG.write_text(json.dumps(_d, ensure_ascii=False), encoding="utf-8")
            try:
                _sh2.copy(str(_PROG), str(_NAS_PROG))
            except Exception:
                pass
        except Exception:
            pass

    class _EpochHandler(_logging.Handler):
        def emit(self, rec):
            try:
                _m = _re2.search(r"Epoch(\d+)", rec.getMessage())
                if _m:
                    _prog("训练中", int(_m.group(1)))
            except Exception:
                pass
    if family == "ptnn":
        _logging.getLogger("qlib").addHandler(_EpochHandler())

    _prog("建数据集")
    print(f"[{datetime.datetime.now():%H:%M:%S}] building dataset ({family})...", flush=True)
    dataset = init_instance_by_config(ds_cfg)   # (数据集缓存因pickle后Alpha158 handler丢_infer而废弃, 2026-06-21)
    if factor_contract.enabled:
        removed = apply_contract_to_handler(dataset.handler, factor_contract)
        remaining = len(dataset.handler.get_cols(col_set="feature"))
        print(
            f"[run_model] [FactorSelection] effective={len(factor_contract.effective_factors)} "
            f"excluded={len(factor_contract.excluded_features)} removed={removed} "
            f"dataset_features={remaining}",
            flush=True,
        )
    else:
        print("[run_model] [FactorSelection] no effective-factor contract; using all dataset features", flush=True)
    # 防 inf: 某些自定义因子在特定股票(如csi500个别票)算出inf, xgb/树模型不吃inf直接崩(Check failed: valid)。
    # 把特征列的 inf/-inf -> nan -> 0, 全family通用(归一处理对inf无效, 故在数据源直接清)。
    try:
        import numpy as _np2, pandas as pd
        _h = dataset.handler
        for _at in ("_infer", "_learn", "_data"):
            _df = getattr(_h, _at, None)
            if isinstance(_df, pd.DataFrame) and len(_df):
                _fc = [c for c in _df.columns if isinstance(c, tuple) and c[0] == "feature"]
                if _fc and _np2.isinf(_df[_fc].to_numpy(dtype="float64", na_value=_np2.nan)).any():
                    _df[_fc] = _df[_fc].replace([_np2.inf, -_np2.inf], _np2.nan).fillna(0)
                    setattr(_h, _at, _df)
                    print(f"[run_model] 清理 {_at} 特征列的 inf -> 0", flush=True)
    except Exception as _e:
        print(f"[run_model] inf清理跳过({_e})", flush=True)
    if family == "ptnn":
        # 按数据集真实特征数注入(RD-Agent 因子批次特征数 ≠ 默认158); 适配器据此建网络。
        n_feat = len(dataset.handler.get_cols(col_set="feature"))
        base["kwargs"]["pt_model_kwargs"]["num_features"] = n_feat
        print(f"[run_model] ptnn num_features={n_feat} step_len={STEP_LEN}", flush=True)
        # TSDatasetH.prepare 返回序列采样器, 取 label 走 handler.fetch(test段)避免类型不符
        _seg = ds_cfg["kwargs"]["segments"]["test"]
        _lbl = dataset.handler.fetch(selector=slice(str(_seg[0]), str(_seg[1])), col_set="label")
        label_test = to_score_series(_lbl).rename("label")
    else:
        label_test = to_score_series(dataset.prepare("test", col_set="label")).rename("label")

    seeds = parse_model_seeds(seedkey)
    seed_predictions, seed_models, seed_metrics = [], [], []
    for sd in seeds:
        mcfg = copy.deepcopy(base)
        if seedkey and sd is not None:
            mcfg.setdefault("kwargs", {})[seedkey] = sd
        tag = f"seed={sd}" if sd is not None else "deterministic"
        print(f"[{datetime.datetime.now():%H:%M:%S}] {model} {tag} ...", flush=True)
        m = init_instance_by_config(mcfg)
        _prog("训练中", 0 if family == "ptnn" else None)   # epoch只对深度模型有意义, 树/线性不显示
        m.fit(dataset)
        _prog("预测+回测")
        raw_pred = to_score_series(m.predict(dataset))
        seed_pred = neutralize_prediction_scores(raw_pred)
        ic, icir = rank_ic_stats(seed_pred, label_test)
        bt = run_backtest(seed_pred, pac)
        seed_predictions.append(raw_pred)
        seed_models.append(m)
        seed_metrics.append(
            {
                "seed": sd,
                "rank_ic": float(ic),
                "rank_icir": float(icir),
                "excess": float(bt["excess"]),
                "ir": float(bt["ir"]),
                "maxdd": float(bt["mdd"]),
                "ann": float(bt["ann"]),
            }
        )
        print(f"    -> RankIC={ic:.4f} excess={bt['excess']*100:+.2f}% IR={bt['ir']:.3f}", flush=True)

    # One portfolio, one ledger: the score saved below is exactly the score used
    # for IC, backtest metrics and the published curve.  Single-seed runs naturally
    # reduce to their sole prediction without changing behavior.
    pred = neutralize_prediction_scores(mean_seed_predictions(seed_predictions))
    ensemble_ic, ensemble_icir = rank_ic_stats(pred, label_test)
    if len(seed_predictions) == 1:
        # Avoid a redundant backtest for the compatibility path.
        ensemble_bt = bt
    else:
        print(
            f"[{datetime.datetime.now():%H:%M:%S}] backtesting score-mean ensemble "
            f"({len(seeds)} seeds)...",
            flush=True,
        )
        ensemble_bt = run_backtest(pred, pac)
    last_curve = ensemble_bt["curve"]
    model_artifact = build_seed_ensemble_artifact(seed_models, seeds, execution)
    save_artifacts(batch, model, model_artifact, pred, label_test)

    if os.environ.get("RDAGENT_PRED_ONLY", "").strip() in ("1", "true", "yes"):
        print(
            "[run_model] PRED_ONLY: prediction cached; skipping production ledgers and curves",
            flush=True,
        )
        return

    if os.environ.get("RDAGENT_LIQ_VALIDATE", "").strip() in ("1", "true", "yes"):
        try:
            liquidity_validate(pred, pac, _univ, model)
        except Exception as _e:
            print(f"[liq-validate] 失败: {_e}", flush=True)

    seed_excess = [item["excess"] for item in seed_metrics]
    seed_ir = [item["ir"] for item in seed_metrics]
    seed_mdd = [item["maxdd"] for item in seed_metrics]
    seed_ann = [item["ann"] for item in seed_metrics]
    seed_ic = [item["rank_ic"] for item in seed_metrics]
    # Persist the exact OOS/cost contract so a production promoter can compare
    # like-for-like results and fail closed on stale or cheaper backtests.
    _test_segment = ds_cfg["kwargs"]["segments"]["test"]
    _backtest_contract = pac["backtest"]
    _exchange_contract = _backtest_contract["exchange_kwargs"]
    _strategy_contract = pac["strategy"]["kwargs"]
    evaluation = {
        "test_start": str(_backtest_contract.get("start_time", _test_segment[0]))[:10],
        "test_end": str(_backtest_contract.get("end_time", _test_segment[1]))[:10],
        "signal_data_start": str(_test_segment[0])[:10],
        "signal_data_end": str(_test_segment[1])[:10],
        "benchmark": str(_backtest_contract.get("benchmark", "")),
        "account": float(_backtest_contract.get("account", 0)),
        "costs": {
            "open_cost": float(_exchange_contract.get("open_cost", 0)),
            "close_cost": float(_exchange_contract.get("close_cost", 0)),
            "min_cost": float(_exchange_contract.get("min_cost", 0)),
        },
        "strategy": {
            "topk": int(_strategy_contract.get("topk", 0)),
            "n_drop": int(_strategy_contract.get("n_drop", 0)),
        },
    }
    provenance = {
        "workspace": _workspace_identity(ws),
        "universe": _univ,
        "effective_factors": (
            sorted(factor_contract.effective_factors)
            if factor_contract.effective_factors is not None
            else None
        ),
        "all_features": sorted(factor_contract.all_features),
    }
    rec = {
        "key": f"{batch or 'default'}::{model}",
        "batch": batch or "default", "model": model, "family": family,
        "rank_ic": round(ensemble_ic, 4), "rank_icir": round(ensemble_icir, 4),
        "excess": round(ensemble_bt["excess"], 4),
        "ir": round(ensemble_bt["ir"], 3), "maxdd": round(ensemble_bt["mdd"], 4),
        "ann": round(ensemble_bt["ann"], 4), "n_seeds": len(seeds),
        "aggregation": "per_instrument_score_mean",
        "excess_lo": round(min(seed_excess), 4), "excess_hi": round(max(seed_excess), 4),
        "seed_dispersion": {
            "excess_std": round(statistics.pstdev(seed_excess), 6),
            "ir_std": round(statistics.pstdev(seed_ir), 6),
            "maxdd_std": round(statistics.pstdev(seed_mdd), 6),
            "ann_std": round(statistics.pstdev(seed_ann), 6),
            "rank_ic_std": round(statistics.pstdev(seed_ic), 6),
        },
        "seed_metrics": [
            {
                key: (round(value, 6) if isinstance(value, float) else value)
                for key, value in item.items()
            }
            for item in seed_metrics
        ],
        "execution": execution,
        "evaluation": evaluation,
        "provenance": provenance,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 池无关键(batch::model)的 model_results/curves 只在【原生池csi300】或【A158(键已含池名)】时写,
    # 否则批次在csi500/csi1000上回测会用同一key覆盖csi300结果 -> /backtest(量价沪深300)数据被污染、与批次擂台对不上。
    # 非原生池的数据已存进 batch_arena.json(按池区分), 不必再写池无关键。
    _native = _A158 or _univ == "csi300"
    if _native:
        save_result(batch, model, rec)
        append_history(rec)
    if not _A158:
        # 批次擂台台账(batch × universe × model), 供"批次股票池对比"页: 选批次→看各模型各池IR矩阵
        _bentry = {"batch": batch or "default", "universe": _univ, "model": model, "family": family,
                   "ir": rec["ir"], "ann": rec["ann"], "excess": rec["excess"], "maxdd": rec["maxdd"],
                   "rank_ic": rec["rank_ic"], "updated_at": rec["updated_at"]}
        BARENA = Path("/mnt/c/rdagent/batch_arena.json")
        try:
            ba = json.loads(BARENA.read_text(encoding="utf-8-sig")) if BARENA.exists() else []
        except Exception:
            ba = []
        ba = [a for a in ba if not (a.get("batch") == (batch or "default") and a.get("universe") == _univ and a.get("model") == model)]
        ba.append(_bentry)
        ba.sort(key=lambda a: (a.get("batch", ""), a.get("universe", ""), -(a.get("ir") or -99)))
        BARENA.write_text(json.dumps(ba, ensure_ascii=False), encoding="utf-8")
        print(f"[run_model] batch_arena += ({batch or 'default'}/{_univ}/{model} IR={rec['ir']})", flush=True)
        # 历史留痕: 每次回测都追加一条(不去重), 带测试期+时间 -> 同一(批次×池×模型)跨时点/时段可对比, 不被重跑覆盖
        try:
            _seg = ds_cfg["kwargs"].get("segments", {}).get("test", ["", ""])
            BHIST = Path("/mnt/c/rdagent/batch_arena_history.json")
            _bh = json.loads(BHIST.read_text(encoding="utf-8")) if BHIST.exists() else []
            _bh.append({**_bentry, "test_start": str(_seg[0])[:10], "test_end": str(_seg[1])[:10],
                        "computed_at": rec["updated_at"], "excess": rec["excess"], "n_seeds": rec["n_seeds"]})
            BHIST.write_text(json.dumps(_bh[-5000:], ensure_ascii=False), encoding="utf-8")
            print(f"[run_model] batch_arena history += 1 (共{len(_bh)}条)", flush=True)
        except Exception as _e:
            print(f"[run_model] batch_arena history skip: {_e}", flush=True)
    if _A158:
        _entry = {"universe": _univ, "model": model, "family": family, "ir": rec["ir"], "ann": rec["ann"],
                  "excess": rec["excess"], "maxdd": rec["maxdd"], "rank_ic": rec["rank_ic"], "updated_at": rec["updated_at"]}
        # 1) 股票池擂台台账(universe x model), 供股票池对比页
        UARENA = Path("/mnt/c/rdagent/universe_arena.json")
        try:
            ua = json.loads(UARENA.read_text(encoding="utf-8-sig")) if UARENA.exists() else []
        except Exception:
            ua = []
        ua = [a for a in ua if not (a.get("universe") == _univ and a.get("model") == model)]  # 同池同模型去重
        ua.append(_entry)
        ua.sort(key=lambda a: (a.get("universe", ""), -(a.get("ir") or -99)))
        UARENA.write_text(json.dumps(ua, ensure_ascii=False), encoding="utf-8")
        # 2) csi300 仍兼容写老 alpha158_arena.json (现有模型擂台页读它)
        if _univ == "csi300":
            ARENA = Path("/mnt/c/rdagent/alpha158_arena.json")
            try:
                arr = json.loads(ARENA.read_text(encoding="utf-8-sig")) if ARENA.exists() else []
            except Exception:
                arr = []
            arr = [a for a in arr if a.get("model") != model]
            arr.append({"model": model, "family": family, "ir": rec["ir"], "ann": rec["ann"],
                        "maxdd": rec["maxdd"], "rank_ic": rec["rank_ic"], "updated_at": rec["updated_at"]})
            arr.sort(key=lambda a: a.get("ir", -99), reverse=True)
            ARENA.write_text(json.dumps(arr, ensure_ascii=False), encoding="utf-8")
        print(f"[run_model] arena -> universe_arena.json ({_univ}/{model} IR={rec['ir']})", flush=True)
        # 历史留痕: 每次arena计算追加一条(不去重), 带测试期+时间, 供跨时点/时段对比分析
        try:
            _seg = ds_cfg["kwargs"].get("segments", {}).get("test", ["", ""])
            HIST = Path("/mnt/c/rdagent/universe_arena_history.json")
            _h = json.loads(HIST.read_text(encoding="utf-8")) if HIST.exists() else []
            _h.append({**_entry, "test_start": str(_seg[0])[:10], "test_end": str(_seg[1])[:10],
                       "computed_at": rec["updated_at"]})
            HIST.write_text(json.dumps(_h[-3000:], ensure_ascii=False), encoding="utf-8")
            print(f"[run_model] arena history += 1 (共{len(_h)}条)", flush=True)
        except Exception as _e:
            print(f"[run_model] arena history skip: {_e}", flush=True)
    if _native:   # 同上: 非原生池不写池无关曲线键, 防覆盖污染 /backtest
        save_curve(batch, model, last_curve, {
            "batch": batch or "default", "model": model,
            "excess": rec["excess"], "ir": rec["ir"], "ann": rec["ann"], "maxdd": rec["maxdd"],
            "n_seeds": rec["n_seeds"], "aggregation": rec["aggregation"],
            "seed_dispersion": rec["seed_dispersion"], "seed_metrics": rec["seed_metrics"],
            "execution": rec["execution"], "evaluation": rec["evaluation"],
            "provenance": rec["provenance"],
            "updated_at": rec["updated_at"],
        })
    print(f"[run_model] saved -> {BT_RESULTS}: excess(ensemble)={rec['excess']*100:+.2f}% IR={rec['ir']} "
          f"range[{rec['excess_lo']*100:+.1f},{rec['excess_hi']*100:+.1f}]%", flush=True)

    # 每次计算后自动同步结果到 NAS 共享, 网页实时最新, 不必手动copy(回测对比/股票池对比页读它们)
    import shutil as _sh
    _NAS = Path("/mnt/z/claude/qlib/data/csv_tmp")
    if _NAS.exists():
        for _f in ("model_curves.json", "universe_arena.json", "universe_arena_history.json",
                   "model_results.json", "model_runs_history.json", "alpha158_arena.json",
                   "batch_arena.json", "batch_arena_history.json"):
            _src = Path("/mnt/c/rdagent") / _f
            if _src.exists():
                try:
                    _sh.copy(str(_src), str(_NAS / _f))
                except Exception as _e:
                    print(f"[run_model] NAS同步 {_f} 跳过: {_e}", flush=True)
        print("[run_model] 结果已自动同步 NAS", flush=True)


if __name__ == "__main__":
    main()
