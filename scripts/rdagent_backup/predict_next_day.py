"""
Generate next-trading-day buy list using the latest SOTA factor+model config.
- Reuses the rendered config from the SOTA workspace.
- Patches dates to extend the test set and applies run_model's shared execution contract.
- Retrains the selected model for each configured seed and averages per-stock scores.
- Evolves and saves the actionable TopkDropout target for the latest prediction date.
"""

import os
import sys
import pickle
import datetime
import copy
from pathlib import Path


def main():
    import pandas as pd
    import qlib
    from qlib.utils import init_instance_by_config

    import json
    from prediction_preflight import (
        atomic_write_text,
        audit_from_environment,
        prediction_coverage_from_environment,
    )
    from live_topk_dropout import (
        select_previous_holdings,
        strategy_metadata,
        topk_dropout_transition,
    )

    def _to_wsl(p):
        p = str(p).replace("\\", "/")
        if len(p) > 1 and p[1] == ":":          # Windows drive -> WSL /mnt mount
            p = f"/mnt/{p[0].lower()}{p[2:]}"
        return p

    # factor universe evaluated for the original SOTA workspace (used when a batch
    # manifest doesn't carry its own all_features list, and for the default path).
    DEFAULT_ALL_FEATURES = [
        "RSI_6D_Rank", "RSQR5", "VSTD5", "STD5", "CORD5", "CORD10", "WVMA60", "CORD60", "KLEN", "KLOW",
        "WVMA5", "CORR10", "RESI5", "CORR5", "RSQR20", "RESI10", "RSQR10", "BB_Width_20D_Rank", "ROC60",
        "CORR20", "RSQR60", "CORR60", "BB_Position_10D_Rank", "RSI_14D_Rank",
    ]

    # Factor batch selection. RDAGENT_FACTOR_BATCH=<label> loads a self-contained manifest
    # final/batches/<label>.json {workspace, effective_factors, all_features} and predicts
    # against THAT batch's workspace. Empty -> default SOTA (sota_workspace.txt pointer) +
    # final/effective_factors.json + DEFAULT_ALL_FEATURES.
    _batch = os.environ.get("RDAGENT_FACTOR_BATCH", "").strip()
    effective_factors = None
    all_evaluated_features = set(DEFAULT_ALL_FEATURES)
    if _batch:
        man_path = Path(f"/mnt/c/rdagent/final/batches/{_batch}.json")
        if not man_path.exists():
            raise SystemExit(f"[predict_next_day] batch manifest not found: {man_path}")
        man = json.loads(man_path.read_text(encoding="utf-8"))
        SOTA_WS = Path(_to_wsl(man["workspace"]))
        effective_factors = set(man.get("effective_factors", []))
        all_evaluated_features = set(man.get("all_features", DEFAULT_ALL_FEATURES))
        cache_key = _batch
    else:
        ptr = Path("/mnt/c/rdagent/sota_workspace.txt")
        raw = (ptr.read_text(encoding="utf-8").strip() if ptr.exists()
               else "Z:/claude/rdagent_workspace/5dcf477aca8f4ac5bbbcb53092653051")
        SOTA_WS = Path(_to_wsl(raw))
        fn_eff = Path("/mnt/c/rdagent/final/effective_factors.json")
        if fn_eff.exists():
            effective_factors = set(json.loads(fn_eff.read_text(encoding="utf-8")))
        cache_key = "default"
    print(f"[predict_next_day] batch={_batch or 'default'}  SOTA workspace={SOTA_WS}", flush=True)

    # model selection (web "predict with selected model"); empty/lgb -> the SOTA-tuned LGB.
    _model = os.environ.get("RDAGENT_MODEL", "").strip().lower()
    if _model in ("", "default", "lightgbm"):
        _model = "lgb"
    try:
        from run_model import (
            apply_execution_semantics,
            mean_seed_predictions,
            model_specs as _mspecs,
            neutralize_prediction_scores,
            parse_model_seeds,
            to_score_series,
        )
        _MSPEC = _mspecs()
    except Exception as exc:
        raise RuntimeError(
            "predict_next_day requires run_model's shared model/execution contract"
        ) from exc
    _model_family = _MSPEC[_model][2] if _model in _MSPEC else "tree"
    cache_key = f"{cache_key}_{_model}"
    print(f"[predict_next_day] model = {_model} (family={_model_family})", flush=True)

    config_candidates = list(SOTA_WS.glob("mlruns/*/*/artifacts/config"))
    cfg_ws = SOTA_WS
    if len(config_candidates) != 1:
        ptr = Path("/mnt/c/rdagent/sota_workspace.txt")
        global_sota_str = ptr.read_text(encoding="utf-8").strip() if ptr.exists() else ""
        if global_sota_str and _to_wsl(global_sota_str) != str(SOTA_WS):
            print(f"[predict_next_day] New workspace has no mlruns, falling back to global SOTA for config: {global_sota_str}", flush=True)
            cfg_ws = Path(_to_wsl(global_sota_str))
            config_candidates = list(cfg_ws.glob("mlruns/*/*/artifacts/config"))
        if len(config_candidates) != 1:
            raise RuntimeError(f"Expected exactly 1 config, found {len(config_candidates)}")
            
    CONFIG_PKL = config_candidates[0]
    OUTPUT_CSV = Path("/mnt/c/rdagent/buy_list_next_day.csv")
    _univ = (os.environ.get("RDAGENT_UNIVERSE", "csi300").strip() or "csi300").lower()
    # Fail before dataset/model work when the local market date or point-in-time
    # constituent snapshot is incomplete.  The post-prediction coverage gate
    # below repeats the contract against actual finite model scores.
    _universe_audit = audit_from_environment(
        Path("/mnt/c/qlib_data/cn_data"), _univ
    )
    NEW_END = _universe_audit.market_date
    TOP_K = 50
    print(
        f"[predict_next_day] preflight OK: {_univ} "
        f"date={NEW_END}, PIT={_universe_audit.expected_count}, "
        f"calendar_age={_universe_audit.calendar_age_days}d, "
        f"freshness_basis={_universe_audit.freshness_basis}",
        flush=True,
    )

    os.chdir(str(cfg_ws))

    with open(CONFIG_PKL, "rb") as f:
        cfg = pickle.load(f)

    # Setup the provider explicitly in case it uses windows style paths
    cfg["qlib_init"]["provider_uri"] = "/mnt/c/qlib_data/cn_data"
    cfg["data_handler_config"]["end_time"] = NEW_END
    
    seg_test_start = cfg["task"]["dataset"]["kwargs"]["segments"]["test"][0]
    cfg["task"]["dataset"]["kwargs"]["segments"]["test"] = [seg_test_start, NEW_END]
    cfg["port_analysis_config"]["backtest"]["end_time"] = NEW_END

    # === ALPHA158 模式: 用全部 Alpha158(158因子)+csi300 替代 RD-Agent 24因子 workspace, 做预测对比 ===
    ALPHA158 = os.environ.get("RDAGENT_ALPHA158", "").strip().lower() in ("1", "true", "yes")
    if ALPHA158:
        effective_factors = None  # 全特征, 跳过因子筛选 monkey-patch
        all_evaluated_features = set()
        OUTPUT_CSV = Path("/mnt/c/rdagent/buy_list_a158.csv")
        cache_key = f"alpha158_{_univ}_{_model}"  # 独立模型缓存, 含股票池, 不与24因子/其他池混
        cfg["task"]["dataset"] = {
            "class": "DatasetH", "module_path": "qlib.data.dataset",
            "kwargs": {
                "handler": {"class": "Alpha158", "module_path": "qlib.contrib.data.handler",
                    "kwargs": {
                        "start_time": "2010-01-01", "end_time": str(NEW_END),
                        "fit_start_time": "2010-01-01", "fit_end_time": f"{NEW_END.year - 2}-12-31",
                        "instruments": _univ,
                        "infer_processors": [
                            {"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature", "clip_outlier": True}},
                            {"class": "Fillna", "kwargs": {"fields_group": "feature"}}],
                        "learn_processors": [
                            {"class": "DropnaLabel"},
                            {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label"}}],
                        "label": ["Ref($close, -2)/Ref($close, -1) - 1"]}},
                # 预测用: 训练到最近(非回测切分)。train 到前年底, valid 用去年, test=今年到最新日,
                # 让模型学到最新市场, 买入清单才新鲜 (回测的固定切分只用于擂台对比, 不用于实盘预测)。
                "segments": {"train": ["2010-01-01", f"{NEW_END.year - 2}-12-31"],
                             "valid": [f"{NEW_END.year - 1}-01-01", f"{NEW_END.year - 1}-12-31"],
                             "test": [f"{NEW_END.year}-01-01", str(NEW_END)]}}}
        print(f"[predict_next_day] *** ALPHA158 模式: 全 Alpha158 + {_univ}, model={_model} (不走24因子workspace) ***", flush=True)

    # === 真路B: 批次模式 + 指定股票池. 把批次config的instruments换成目标池 ->
    #   Alpha158DL按新instruments现算; 自定义因子从combined_factors_df.parquet(全市场5783股)取该池子集.
    #   不用重跑RD-Agent因子执行(自定义因子全市场早算好)。csi300=批次原配置不动。
    if not ALPHA158 and _univ != "csi300":
        # _batch空(default-SOTA基线)也切池: 基面对比要两侧同池, 否则基线留csi300不对等。
        _bench = {"csi500": "sh000905", "csi1000": "sh000852"}.get(_univ, "sh000300")
        try:
            cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["instruments"] = _univ
            if isinstance(cfg.get("data_handler_config"), dict):
                cfg["data_handler_config"]["instruments"] = _univ
            cfg["port_analysis_config"]["backtest"]["benchmark"] = _bench
            cache_key = f"{_batch or 'default'}_{_univ}_{_model}"   # 各池×各模型独立缓存, 不混
            print(f"[predict_next_day] *** 真路B: 批次{_batch or 'default'} -> {_univ} (Alpha158现算+自定义因子全市场parquet取子集, benchmark={_bench}) ***", flush=True)
        except Exception as e:
            raise RuntimeError(
                f"failed to apply the requested {_univ} universe to the prediction config"
            ) from e

    execution = apply_execution_semantics(
        cfg["task"]["dataset"], cfg["port_analysis_config"]
    )
    _strategy_cfg = cfg["port_analysis_config"].get("strategy") or {}
    _strategy_kwargs = _strategy_cfg.get("kwargs") or {}
    if _strategy_cfg.get("class") != "TopkDropoutStrategy":
        raise RuntimeError(
            "live portfolio state requires backtest strategy TopkDropoutStrategy"
        )
    try:
        _configured_topk = int(_strategy_kwargs["topk"])
        N_DROP = int(_strategy_kwargs["n_drop"])
        _hold_thresh = int(_strategy_kwargs.get("hold_thresh", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid TopkDropoutStrategy configuration") from exc
    if _configured_topk != TOP_K:
        raise RuntimeError(
            f"prediction top_k={TOP_K} != backtest topk={_configured_topk}"
        )
    if not 0 <= N_DROP <= TOP_K or _hold_thresh < 0:
        raise RuntimeError("invalid TopkDropout n_drop/hold_thresh bounds")
    if (
        str(_strategy_kwargs.get("method_buy", "top")) != "top"
        or str(_strategy_kwargs.get("method_sell", "bottom")) != "bottom"
    ):
        raise RuntimeError("live transition only supports Qlib method_buy=top/method_sell=bottom")
    _strategy_meta = strategy_metadata(
        topk=TOP_K,
        n_drop=N_DROP,
        hold_thresh=_hold_thresh,
        only_tradable=bool(_strategy_kwargs.get("only_tradable")),
    )
    print(
        f"[{datetime.datetime.now():%H:%M:%S}] Patched config: test_end={NEW_END}, "
        f"execution={execution['mode']}, label={execution['return_horizon']}, "
        f"deal_price={execution['deal_price']}, "
        f"max_volume_participation={execution['max_volume_participation']:.1%}",
        flush=True,
    )
    print(f"  Train: {cfg['task']['dataset']['kwargs']['segments']['train']}", flush=True)
    print(f"  Valid: {cfg['task']['dataset']['kwargs']['segments']['valid']}", flush=True)
    print(f"  Test : {cfg['task']['dataset']['kwargs']['segments']['test']}", flush=True)

    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region=cfg["qlib_init"]["region"])

    if _model_family in ("linear", "ptnn"):   # 线性/深度网络都不吃 NaN; 截面zscore归一+填0
        _hk = cfg["task"]["dataset"]["kwargs"]["handler"].setdefault("kwargs", {})
        _hk["infer_processors"] = [
            {"class": "CSZScoreNorm", "kwargs": {"fields_group": "feature"}},
            {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
        ]
    if _model_family == "ptnn":
        # 深度时序: DatasetH -> TSDatasetH(序列窗口 step_len), 喂 (B,T,F) 给 GeneralPTNN。
        from run_model import STEP_LEN as _STEP_LEN
        cfg["task"]["dataset"]["class"] = "TSDatasetH"
        cfg["task"]["dataset"]["kwargs"]["step_len"] = _STEP_LEN
        import sys as _sys
        os.environ.setdefault("TSLIB_PATH", "/mnt/z/claude/Time-Series-Library")
        _mlib = "/mnt/z/claude/rdagent_model_lib"
        if _mlib not in _sys.path:
            _sys.path.insert(0, _mlib)
    # === 训练进度心跳: 写 train_progress.json(+NAS), 网页轮询显示细粒度进度(建数据集/训练EpochN/预测), 不必干等 ===
    import shutil as _sh3, re as _re3, logging as _logging3
    _PROG3 = Path("/mnt/c/rdagent/train_progress.json")
    _NAS_PROG3 = Path("/mnt/z/claude/qlib/data/csv_tmp/train_progress.json")
    try:
        _nep3 = (_MSPEC.get(_model, [None, {}])[1].get("kwargs", {}) or {}).get("n_epochs") if _model in _MSPEC else None
    except Exception:
        _nep3 = None
    def _prog3(phase, epoch=None):
        try:
            _PROG3.write_text(json.dumps(
                {"batch": _batch or "default", "universe": _univ, "model": _model, "family": _model_family,
                 "phase": phase, "epoch": epoch, "n_epochs": _nep3, "alpha158": bool(ALPHA158),
                 "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
            try:
                _sh3.copy(str(_PROG3), str(_NAS_PROG3))
            except Exception:
                pass
        except Exception:
            pass

    class _EpochHandler3(_logging3.Handler):
        def emit(self, rec):
            try:
                _m = _re3.search(r"Epoch(\d+)", rec.getMessage())
                if _m:
                    _prog3("训练中", int(_m.group(1)))
            except Exception:
                pass
    if _model_family == "ptnn":
        _logging3.getLogger("qlib").addHandler(_EpochHandler3())
    _prog3("建数据集")
    print(f"[{datetime.datetime.now():%H:%M:%S}] Building dataset (Alpha158 + SOTA factor library)...", flush=True)
    dataset = init_instance_by_config(cfg["task"]["dataset"])
    if _model_family == "ptnn":
        # ptnn 走 TSDataSampler, 下方DataFrame级monkey-patch对它失效。改为直接从 handler 的处理后数据
        # (_infer/_learn/_data)里删掉噪声因子列 -> 序列采样器只看到精选因子, 真正实现"少而精"。
        # 失败则退回全特征(=旧行为, 不会更糟)。
        _nf = len(dataset.handler.get_cols(col_set="feature"))
        if effective_factors is not None:
            try:
                _noisy = all_evaluated_features - effective_factors
                h = dataset.handler
                for _attr in ("_infer", "_learn", "_data"):
                    _df = getattr(h, _attr, None)
                    if isinstance(_df, pd.DataFrame) and isinstance(_df.columns, pd.MultiIndex):
                        _keep = [c for c in _df.columns if not (c[0] == "feature" and c[1] in _noisy)]
                        setattr(h, _attr, _df[_keep])
                _src = getattr(h, "_infer", None)
                if isinstance(_src, pd.DataFrame) and isinstance(_src.columns, pd.MultiIndex):
                    _nf = sum(1 for c in _src.columns if c[0] == "feature")
                # ptnn 已在数据源剥离, 跳过下方DataFrame monkey-patch(置空 effective_factors 防重复处理)
                effective_factors = None
                print(f"[{datetime.datetime.now():%H:%M:%S}] [ptnn] 已从handler数据剥离{len(_noisy)}个噪声因子 -> 精选{_nf}个 (真'少而精')", flush=True)
            except Exception as _e:
                print(f"[{datetime.datetime.now():%H:%M:%S}] [ptnn] 因子子集剥离失败({_e}), 退回全特征", flush=True)
        _MSPEC[_model][1]["kwargs"]["pt_model_kwargs"]["num_features"] = _nf
        print(f"[{datetime.datetime.now():%H:%M:%S}] [ptnn] num_features={_nf} step_len={_STEP_LEN}", flush=True)

    # --- Dynamic Factor Selection Patch (effective_factors / all_evaluated_features resolved above) ---
    if effective_factors is not None:
        # Any evaluated factor that did not pass IC selection is noisy and should be dropped
        noisy_features = all_evaluated_features - effective_factors

        print(f"[{datetime.datetime.now():%H:%M:%S}] [FactorSelection] Loaded {len(effective_factors)} effective features (batch={_batch or 'default'}).", flush=True)
        print(f"[{datetime.datetime.now():%H:%M:%S}] [FactorSelection] Filtering out {len(noisy_features)} noisy features: {sorted(list(noisy_features))}", flush=True)
        
        # Monkey patch dataset.prepare to filter columns
        orig_prepare = dataset.prepare
        def patched_prepare(self, *args, **kwargs):
            col_set = kwargs.get("col_set", args[1] if len(args) > 1 else None)
            res = orig_prepare(*args, **kwargs)
            if col_set == "feature" or (isinstance(col_set, list) and "feature" in col_set):
                if isinstance(res, pd.DataFrame):
                    if isinstance(res.columns, pd.MultiIndex):
                        keep_cols = [c for c in res.columns if not (c[0] == "feature" and c[1] in noisy_features)]
                        res = res[keep_cols]
                    else:
                        keep_cols = [c for c in res.columns if c not in noisy_features]
                        res = res[keep_cols]
                elif isinstance(res, tuple) or isinstance(res, list):
                    new_res = []
                    for df in res:
                        if isinstance(df, pd.DataFrame):
                            if isinstance(df.columns, pd.MultiIndex):
                                keep_cols = [c for c in df.columns if not (c[0] == "feature" and c[1] in noisy_features)]
                                new_res.append(df[keep_cols])
                            else:
                                keep_cols = [c for c in df.columns if c not in noisy_features]
                                new_res.append(df[keep_cols])
                        else:
                            new_res.append(df)
                    res = tuple(new_res)
            return res
            
        import types
        dataset.prepare = types.MethodType(patched_prepare, dataset)
        print(f"[{datetime.datetime.now():%H:%M:%S}] [FactorSelection] Instantiated Dynamic Feature Filtering monkey-patch.", flush=True)
    else:
        print(f"[{datetime.datetime.now():%H:%M:%S}] [FactorSelection] No effective factors resolved; model training will use all raw features.", flush=True)
    # --- End Patch ---

    # retrain (default) trains a fresh LGB and caches it; no-retrain reuses the cache.
    # cache is per factor-batch (a model trained on one factor set can't predict another).
    MODEL_CACHE = Path(f"/mnt/c/rdagent/model_cache_{cache_key}.pkl")
    retrain = os.environ.get("RDAGENT_RETRAIN", "1").strip().lower() not in ("0", "false", "no", "")
    _seedkey = _MSPEC[_model][0] if _model in _MSPEC else None
    _seeds = parse_model_seeds(_seedkey)

    if not retrain and MODEL_CACHE.exists():
        print(f"[{datetime.datetime.now():%H:%M:%S}] [no-retrain] Loading cached model from {MODEL_CACHE}", flush=True)
        with open(MODEL_CACHE, "rb") as f:
            _artifact = pickle.load(f)
        if not isinstance(_artifact, dict) or _artifact.get("artifact_type") != "rdagent_live_seed_ensemble_v1":
            raise RuntimeError(
                f"legacy model cache {MODEL_CACHE} has no seed/execution metadata; "
                "rerun with RDAGENT_RETRAIN=1"
            )
        if _artifact.get("seeds") != _seeds:
            raise RuntimeError(
                f"cached seeds {_artifact.get('seeds')} != requested {_seeds}; "
                "rerun with RDAGENT_RETRAIN=1"
            )
        if (
            _artifact.get("execution_mode") != execution["mode"]
            or _artifact.get("label") != execution["label"]
            or _artifact.get("score_transform") != execution["score_transform"]
        ):
            raise RuntimeError(
                "cached execution semantics do not match the requested live contract; "
                "rerun with RDAGENT_RETRAIN=1"
            )
        _models = _artifact.get("models") or []
        if len(_models) != len(_seeds):
            raise RuntimeError("cached model count does not match cached seed metadata")
    else:
        if _model not in _MSPEC and _model != "lgb":
            raise RuntimeError(f"Model specs for '{_model}' not found in run_model.py. Supported: {list(_MSPEC.keys())}")
        _base_mcfg = cfg["task"]["model"] if _model == "lgb" else _MSPEC[_model][1]
        _models = []
        for _seed in _seeds:
            _mcfg = copy.deepcopy(_base_mcfg)
            if _seedkey and _seed is not None:
                _mcfg.setdefault("kwargs", {})[_seedkey] = _seed
            _tag = f"seed={_seed}" if _seed is not None else "deterministic"
            print(f"[{datetime.datetime.now():%H:%M:%S}] Building {_model} model ({_tag})...", flush=True)
            _fitted = init_instance_by_config(_mcfg)
            print(f"[{datetime.datetime.now():%H:%M:%S}] Training {_model} ({_tag})...", flush=True)
            _prog3("训练中", 0 if _model_family == "ptnn" else None)   # epoch只对深度模型有意义
            _fitted.fit(dataset)
            _models.append(_fitted)
        _artifact = {
            "artifact_type": "rdagent_live_seed_ensemble_v1",
            "aggregation": "per_instrument_score_mean",
            "models": _models,
            "seeds": _seeds,
            "execution_mode": execution["mode"],
            "label": execution["label"],
            "score_transform": execution["score_transform"],
        }
        with open(MODEL_CACHE, "wb") as f:
            pickle.dump(_artifact, f)
        print(
            f"[{datetime.datetime.now():%H:%M:%S}] Saved {len(_models)}-model "
            f"score-mean ensemble to {MODEL_CACHE}",
            flush=True,
        )

    _prog3("预测中")
    print(f"[{datetime.datetime.now():%H:%M:%S}] Predicting {_seeds}...", flush=True)
    _seed_predictions = [to_score_series(item.predict(dataset)) for item in _models]
    pred = neutralize_prediction_scores(
        mean_seed_predictions(_seed_predictions)
    ).to_frame("score")

    # --- 打分中性化(剥 size/mom/vol 风格暴露). 默认开(NEUTRALIZE_SCORE=0 关) ---
    # 验证(validate_score_neutralize.py, 7模型同期OOS): 模型打分平均35%由风格解释, 中性化后
    # Top50夏普一致提升 Δ+1.6(100%), 因模型本意找alpha而非赌风格→剥无意风格暴露=去风险。后处理不碰LGB训练, 完全可逆。
    # Kept temporarily for source-level backward comparison; the shared helper
    # above is now the sole executable score transform.
    if False and os.environ.get("NEUTRALIZE_SCORE", "1").strip().lower() not in ("0", "false", "no", ""):
        try:
            import numpy as np
            from qlib.data import D
            import feature_neutralize as _neut
            insts = sorted(pred.index.get_level_values(1).unique())
            d0 = pred.index.get_level_values(0).min(); d1 = pred.index.get_level_values(0).max()
            start = (pd.Timestamp(d0) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
            _f = D.features(insts, ["$close", "$volume"], start_time=start, end_time=pd.Timestamp(d1).strftime("%Y-%m-%d"), freq="day")
            _c = _f["$close"].unstack(level="instrument").sort_index(); _v = _f["$volume"].unstack(level="instrument").sort_index()
            _SIZE = np.log((_c * _v).rolling(20, min_periods=10).mean().replace(0, np.nan))
            _MOM = _c.shift(1) / _c.shift(21) - 1
            _VOL = _c.pct_change().rolling(20, min_periods=10).std()
            new = {}
            for d, sub in pred.groupby(level=0):
                sc = sub["score"]; sc.index = sc.index.get_level_values(1)
                if d not in _SIZE.index:
                    new[d] = sc; continue
                sty = {"size": _SIZE.loc[d].reindex(sc.index), "mom": _MOM.loc[d].reindex(sc.index), "vol": _VOL.loc[d].reindex(sc.index)}
                resid, _ = _neut.style_residual(sc, sty)
                new[d] = resid if resid is not None else sc
            pred = pd.concat(new).rename("score").to_frame()
            pred.index = pred.index.set_names(["datetime", "instrument"])
            print(f"[{datetime.datetime.now():%H:%M:%S}] [Neutralize] 打分已剥 size/mom/vol 风格暴露(NEUTRALIZE_SCORE=1)", flush=True)
        except Exception as _e:
            print(f"[Neutralize] 失败, 退回原始打分: {_e}", flush=True)

    counts = pred.groupby(level=0).size()
    print(f"\nPrediction coverage (last 5 dates):", flush=True)
    print(counts.tail(5), flush=True)

    todays = prediction_coverage_from_environment(
        pred, _universe_audit, top_k=TOP_K
    )
    _coverage = todays.attrs["coverage_audit"]
    print(
        f"[predict_next_day] coverage gate OK: {_coverage.predicted_count}/"
        f"{_coverage.expected_count} ({_coverage.coverage:.1%})",
        flush=True,
    )
    ld = _coverage.market_date
    _preflight_meta = {
        "freshness_basis": _universe_audit.freshness_basis,
        "calendar_age_days": _universe_audit.calendar_age_days,
        "expected_constituents": _universe_audit.expected_count,
        "predicted_constituents": _coverage.predicted_count,
        "prediction_coverage": round(_coverage.coverage, 6),
    }

    # Restore the latest strictly earlier, successful portfolio for this exact
    # sleeve.  Same-day reruns are deliberately ignored because their target has
    # not yet reached the next trading day's execution step.
    _hp = Path("/mnt/c/rdagent/buylist_history.json")
    if _hp.exists():
        try:
            _hist = json.loads(_hp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot restore live portfolio history {_hp}: {exc}") from exc
        if not isinstance(_hist, dict) or not isinstance(_hist.get("runs"), list):
            raise RuntimeError(f"invalid live portfolio history schema: {_hp}")
    else:
        _hist = {"runs": []}
    _portfolio_mode = "alpha158" if ALPHA158 else "factor"
    _portfolio_batch = "alpha158" if ALPHA158 else (_batch or "")
    try:
        _max_history_gap = int(os.environ.get("RDAGENT_MAX_PORTFOLIO_HISTORY_GAP_DAYS", "14"))
    except ValueError as exc:
        raise RuntimeError("RDAGENT_MAX_PORTFOLIO_HISTORY_GAP_DAYS must be an integer") from exc
    _previous = select_previous_holdings(
        _hist["runs"],
        current_as_of=str(ld),
        model=_model,
        universe=_univ,
        batch=_portfolio_batch,
        mode=_portfolio_mode,
        topk=TOP_K,
        n_drop=N_DROP,
        current_universe_size=int(len(todays)),
        current_codes=todays.index,
        hold_thresh=_hold_thresh,
        only_tradable=bool(_strategy_kwargs.get("only_tradable")),
        maximum_calendar_gap_days=_max_history_gap,
    )
    _transition = topk_dropout_transition(
        [(str(code), row["score"]) for code, row in todays.iterrows()],
        _previous.codes,
        topk=TOP_K,
        n_drop=N_DROP,
    )
    top = todays.reindex(list(_transition.target)).copy()
    _unranked_carry = [str(code) for code in top.index[top["score"].isna()]]
    if _unranked_carry:
        # A constituent can leave the signal universe while TopkDropout retires
        # at most n_drop names.  Qlib ranks that NaN last; use an explicitly
        # audited finite floor so downstream CSV/JSON consumers never emit NaN.
        _score_min = float(todays["score"].min())
        _score_span = float(todays["score"].max() - todays["score"].min())
        top.loc[top["score"].isna(), "score"] = _score_min - max(_score_span, 1.0)
    top["rank"] = range(1, len(top) + 1)
    atomic_write_text(OUTPUT_CSV, top.to_csv())

    _signal_rank = {str(code): rank for rank, code in enumerate(todays.index, 1)}
    _unranked_carry_set = set(_unranked_carry)
    _target_recs = []
    for code, row in top.iterrows():
        score = None if pd.isna(row["score"]) else round(float(row["score"]), 6)
        normalized_code = str(code)
        _target_recs.append(
            {
                "code": normalized_code,
                "rank": int(row["rank"]),
                "signal_rank": _signal_rank.get(normalized_code),
                "score": score,
                "score_source": (
                    "unranked_carry_floor"
                    if normalized_code in _unranked_carry_set
                    else "current_signal"
                ),
                "action": _transition.action_for(normalized_code),
            }
        )
    _signal_top = [
        {"code": str(code), "rank": rank, "score": round(float(row["score"]), 6)}
        for rank, (code, row) in enumerate(todays.head(TOP_K).iterrows(), 1)
    ]
    _gen_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _portfolio_meta = {
        "previous": _previous.metadata(str(ld)),
        "history_max_gap_days": _max_history_gap,
        "retained": list(_transition.retained),
        "sold": list(_transition.sold),
        "added": list(_transition.added),
        "target": list(_transition.target),
        "unranked_carried": _unranked_carry,
        "target_size": len(_transition.target),
        "initialized": _transition.initialized,
    }
    _rec_out = {
        "status": "success",
        "portfolio_state_valid": True,
        "mode": _portfolio_mode,
        "model": _model,
        "universe": _univ,
        "batch": _portfolio_batch,
        "as_of": str(ld),
        "generated_at": _gen_at,
        "n_universe": int(len(todays)),
        "top_k": TOP_K,
        "n_drop": N_DROP,
        "n_seeds": len(_seeds),
        "seeds": _seeds,
        "aggregation": "per_instrument_score_mean",
        "execution": execution,
        "preflight": _preflight_meta,
        "strategy": _strategy_meta,
        "portfolio": _portfolio_meta,
        "signal_top": _signal_top,
        # Existing API consumers read hits; it now means the actionable target.
        "hits": _target_recs,
    }
    print(
        f"[predict_next_day] TopkDropout target: previous={len(_previous.codes)} "
        f"retained={len(_transition.retained)} sold={len(_transition.sold)} "
        f"added={len(_transition.added)} target={len(_transition.target)} "
        f"source={_previous.source_schema}:{_previous.as_of or 'initialize'}",
        flush=True,
    )

    # 通用买入清单导出口: 设 RDAGENT_BUYLIST_OUT=<path> 就把 top 买入清单(code/rank/score)写过去,
    # 不动 predictions*.json/pool_buy 等默认文件 —— 给 fund_compare_predict.py 抓基线/批次两侧用.
    _buyout = os.environ.get("RDAGENT_BUYLIST_OUT", "").strip()
    if _buyout:
        atomic_write_text(Path(_buyout), json.dumps(_rec_out, ensure_ascii=False))
        print(f"[predict_next_day] wrote RDAGENT_BUYLIST_OUT -> {_buyout} ({len(_target_recs)} stocks)", flush=True)
    if ALPHA158:
        # 给网页写一份 JSON(buy list + 元数据)
        recs = _target_recs
        webout = dict(_rec_out)
        if _univ == "csi300":
            # 默认池: 保持现有文件名(Alpha158预测页/集成/24vs158对比 都读这些)
            atomic_write_text(
                Path("/mnt/c/rdagent/predictions_a158.json"),
                json.dumps(webout, ensure_ascii=False),
            )
            print(f"[predict_next_day] wrote predictions_a158.json ({len(recs)} stocks, model={_model})", flush=True)
            n = len(todays)
            rk = todays["score"].rank(ascending=True, method="average")
            scores_all = {str(idx): round(float(rk[idx]) / n, 6) for idx in todays.index}
            atomic_write_text(
                Path(f"/mnt/c/rdagent/a158_scores_{_model}.json"),
                json.dumps({"model": _model, "as_of": str(ld),
                            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "n": int(n), "preflight": _preflight_meta,
                            "scores": scores_all}, ensure_ascii=False),
            )
            print(f"[predict_next_day] wrote a158_scores_{_model}.json ({n} stocks rank-pct)", flush=True)
        else:
            # 分池(csi500/csi1000): 一池一模型一文件, 供分池买入清单页读取
            atomic_write_text(
                Path(f"/mnt/c/rdagent/pool_buy_{_univ}_{_model}.json"),
                json.dumps(webout, ensure_ascii=False),
            )
            print(f"[predict_next_day] wrote pool_buy_{_univ}_{_model}.json ({len(recs)} stocks, {_univ}/{_model})", flush=True)

    # Persist every successful target, not only RDAGENT_BUYLIST_OUT runs.  The
    # next prediction therefore has state for default and Alpha158 sleeves too.
    _hist["runs"].insert(0, _rec_out)
    _hist["runs"] = _hist["runs"][:500]
    atomic_write_text(_hp, json.dumps(_hist, ensure_ascii=False))
    import shutil as _sh
    try:
        _sh.copy(str(_hp), "/mnt/z/claude/qlib/data/csv_tmp/buylist_history.json")
    except Exception as _copy_error:
        print(f"[predict_next_day] history NAS copy warning: {_copy_error}", flush=True)
    print(
        f"[predict_next_day] portfolio history += 1 "
        f"({len(_hist['runs'])} rows -> buylist_history.json)",
        flush=True,
    )
    print(f"\n[{datetime.datetime.now():%H:%M:%S}] Last prediction date: {ld}", flush=True)
    print(f"Universe size on last date: {len(todays)}", flush=True)
    print(
        f"\nTop-{TOP_K} target portfolio (entry={execution['entry_timing']}, "
        f"score ensemble={len(_seeds)} seed(s)):",
        flush=True,
    )
    print(top.to_string(), flush=True)
    print(f"\nSaved to {OUTPUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
