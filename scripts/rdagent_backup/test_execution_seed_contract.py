from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.rdagent_backup.run_model import (
    apply_execution_semantics,
    assert_execution_semantics,
    mean_seed_predictions,
    neutralize_prediction_scores,
    parse_model_seeds,
    resolve_execution_semantics,
)


def _configs():
    dataset = {
        "kwargs": {
            "handler": {
                "kwargs": {
                    "label": ["old-label"],
                }
            }
        }
    }
    portfolio = {
        "strategy": {"kwargs": {"topk": 50, "n_drop": 5}},
        "backtest": {
            "exchange_kwargs": {
                "deal_price": "close",
                "open_cost": 0.0005,
                "close_cost": 0.0015,
            }
        },
    }
    return dataset, portfolio


def _nested_loader_dataset():
    """RD-Agent 因子批次布局: 裸 DataHandlerLP + NestedDataLoader, label 在 loader 层."""

    return {
        "kwargs": {
            "handler": {
                "class": "DataHandlerLP",
                "module_path": "qlib.contrib.data.handler",
                "kwargs": {
                    "start_time": "2008-01-01",
                    "end_time": None,
                    "instruments": "csi300",
                    "data_loader": {
                        "class": "NestedDataLoader",
                        "kwargs": {
                            "dataloader_l": [
                                {
                                    "class": "qlib.contrib.data.loader.Alpha158DL",
                                    "kwargs": {
                                        "config": {
                                            "label": [
                                                ["Ref($close, -2)/Ref($close, -1) - 1"],
                                                ["LABEL0"],
                                            ],
                                            "feature": [["Ref($close, 1)"], ["F0"]],
                                        }
                                    },
                                },
                                {
                                    "class": "qlib.data.dataset.loader.StaticDataLoader",
                                    "kwargs": {"config": "combined_factors_df.parquet"},
                                },
                            ]
                        },
                    },
                },
            }
        }
    }


def test_default_execution_contract_is_next_open_and_fail_closed_for_suspension():
    dataset, portfolio = _configs()

    semantics = apply_execution_semantics(dataset, portfolio, environ={})

    assert semantics["mode"] == "next_open"
    assert semantics["score_transform"] == "style_neutralized_size_momentum_volatility"
    assert semantics["label"] == "Ref($open, -2) / Ref($open, -1) - 1"
    assert dataset["kwargs"]["handler"]["kwargs"]["label"] == [semantics["label"]]
    assert portfolio["backtest"]["exchange_kwargs"]["deal_price"] == "open"
    assert portfolio["strategy"]["kwargs"]["only_tradable"] is True
    assert portfolio["backtest"]["exchange_kwargs"]["volume_threshold"] == (
        "current",
        "0.05 * $volume * 100",
    )
    # Existing fee assumptions are not changed by the execution contract.
    assert portfolio["backtest"]["exchange_kwargs"]["open_cost"] == 0.0005


def test_execution_contract_supports_explicit_close_and_custom_capacity():
    dataset, portfolio = _configs()

    semantics = apply_execution_semantics(
        dataset,
        portfolio,
        environ={
            "RDAGENT_EXECUTION_MODE": "next_close",
            "RDAGENT_MAX_VOLUME_PARTICIPATION": "0.1",
        },
    )

    assert semantics["deal_price"] == "close"
    assert semantics["label"] == "Ref($close, -2) / Ref($close, -1) - 1"
    assert semantics["volume_threshold"] == ("current", "0.1 * $volume * 100")


@pytest.mark.parametrize(
    "environ",
    [
        {"RDAGENT_EXECUTION_MODE": "vwap"},
        {"RDAGENT_MAX_VOLUME_PARTICIPATION": "0"},
        {"RDAGENT_MAX_VOLUME_PARTICIPATION": "nan"},
        {"RDAGENT_MAX_VOLUME_PARTICIPATION": "1.01"},
    ],
)
def test_execution_contract_rejects_unsafe_configuration(environ):
    with pytest.raises(ValueError):
        resolve_execution_semantics(environ)


def test_execution_assertion_detects_post_patch_drift():
    dataset, portfolio = _configs()
    semantics = apply_execution_semantics(dataset, portfolio, environ={})
    portfolio["backtest"]["exchange_kwargs"]["deal_price"] = "close"

    with pytest.raises(ValueError, match="deal_price"):
        assert_execution_semantics(dataset, portfolio, semantics)


def test_execution_contract_patches_loader_level_label_for_data_handler_lp():
    _, portfolio = _configs()
    dataset = _nested_loader_dataset()

    semantics = apply_execution_semantics(dataset, portfolio, environ={})

    handler_kwargs = dataset["kwargs"]["handler"]["kwargs"]
    # 裸 DataHandlerLP 不允许 handler 级 label kwarg (DataHandler.__init__ 会 TypeError)
    assert "label" not in handler_kwargs
    loaders = handler_kwargs["data_loader"]["kwargs"]["dataloader_l"]
    assert loaders[0]["kwargs"]["config"]["label"] == [[semantics["label"]], ["LABEL0"]]
    # 因子 parquet 的 StaticDataLoader 不带 label, 不动
    assert loaders[1]["kwargs"]["config"] == "combined_factors_df.parquet"


def test_execution_assertion_detects_loader_label_drift():
    _, portfolio = _configs()
    dataset = _nested_loader_dataset()
    semantics = apply_execution_semantics(dataset, portfolio, environ={})
    loaders = dataset["kwargs"]["handler"]["kwargs"]["data_loader"]["kwargs"]["dataloader_l"]
    loaders[0]["kwargs"]["config"]["label"] = [["Ref($close, -2)/Ref($close, -1) - 1"], ["LABEL0"]]

    with pytest.raises(ValueError, match="label"):
        assert_execution_semantics(dataset, portfolio, semantics)


def test_seed_predictions_are_averaged_per_instrument_not_metric_averaged():
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-07-17"), "SH600000"),
            (pd.Timestamp("2026-07-17"), "SZ000001"),
        ],
        names=["datetime", "instrument"],
    )
    first = pd.Series([0.2, 0.8], index=index, name="score")
    # Reverse the row order to verify alignment is by index, not array position.
    second = pd.Series([0.4, 0.6], index=index[::-1], name="score")

    result = mean_seed_predictions([first, second])

    assert result.loc[(pd.Timestamp("2026-07-17"), "SH600000")] == pytest.approx(0.4)
    assert result.loc[(pd.Timestamp("2026-07-17"), "SZ000001")] == pytest.approx(0.6)
    assert result.name == "score"


def test_seed_prediction_coverage_mismatch_fails_closed():
    first = pd.Series([1.0, 2.0], index=["a", "b"])
    second = pd.Series([3.0], index=["a"])

    with pytest.raises(ValueError, match="coverage mismatch"):
        mean_seed_predictions([first, second])


def test_score_transform_can_be_explicitly_disabled_without_changing_scores():
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-07-17"), "SH600000")],
        names=["datetime", "instrument"],
    )
    raw = pd.Series([0.25], index=index, name="score")

    result = neutralize_prediction_scores(raw, {"NEUTRALIZE_SCORE": "0"})

    pd.testing.assert_series_equal(result, raw)
    assert resolve_execution_semantics({"NEUTRALIZE_SCORE": "0"})["score_transform"] == "raw"


def test_seed_parser_preserves_single_seed_compatibility_and_rejects_duplicates():
    assert parse_model_seeds("seed", {"SEEDS": "7"}) == [7]
    assert parse_model_seeds(None, {"SEEDS": "7,8"}) == [None]
    with pytest.raises(ValueError, match="duplicate seed"):
        parse_model_seeds("seed", {"SEEDS": "7,7"})


def test_live_predictor_imports_the_shared_execution_and_ensemble_contract():
    predictor = Path(__file__).with_name("predict_next_day.py").read_text(encoding="utf-8")

    assert "apply_execution_semantics(" in predictor
    assert "mean_seed_predictions(_seed_predictions)" in predictor
    assert "neutralize_prediction_scores(" in predictor
    assert "rdagent_live_seed_ensemble_v1" in predictor
