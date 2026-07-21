from __future__ import annotations

import json

import numpy as np
import pandas as pd
import h5py

from scripts.rdagent_backup import factor_analysis
from scripts.rdagent_backup import factor_rdagent_screen
from scripts.rdagent_backup.factor_rdagent_screen import _decay_gate


def _settings(**overrides):
    values = {
        "method": "classic",
        "stat_gate": True,
        "fdr_q": 0.10,
        "min_abs_ic": 0.005,
        "min_abs_icir": 0.02,
        "min_observations": 5,
        "seed": 7,
    }
    values.update(overrides)
    return values


def _panel(sign=1.0, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-02", periods=24, freq="B")
    instruments = [f"s{index:03d}" for index in range(80)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    signal = rng.normal(size=len(index))
    labels = sign * signal + rng.normal(scale=0.05, size=len(index))
    features = pd.DataFrame(
        {
            "selected_alpha": signal,
            "noise": rng.normal(size=len(index)),
        },
        index=index,
    )
    label_frame = pd.DataFrame({"LABEL0": labels}, index=index)
    return features, label_frame


def test_default_period_is_validation_and_comes_from_workspace_config():
    config = {
        "task": {
            "dataset": {
                "kwargs": {
                    "segments": {
                        "train": ["2020-01-01", "2023-12-31"],
                        "valid": ["2024-01-01", "2024-12-31"],
                        "test": ["2025-01-01", "2025-12-31"],
                    }
                }
            }
        }
    }

    periods = factor_analysis.derive_factor_periods(config)

    assert periods["selection"] == {
        "segment": "valid",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    assert periods["test"]["start"] == "2025-01-01"


def test_changing_test_data_cannot_change_effective_features():
    selection_features, selection_labels = _panel(sign=1.0, seed=11)
    test_features, positive_test = _panel(sign=1.0, seed=12)
    _, negative_test = _panel(sign=-1.0, seed=12)

    _, positive_metrics, positive_effective = (
        factor_analysis.evaluate_selection_and_test(
            selection_features,
            selection_labels,
            test_features,
            positive_test,
            _settings(),
        )
    )
    _, negative_metrics, negative_effective = (
        factor_analysis.evaluate_selection_and_test(
            selection_features,
            selection_labels,
            test_features,
            negative_test,
            _settings(),
        )
    )

    assert "selected_alpha" in positive_effective
    assert positive_effective == negative_effective
    positive_ic = positive_metrics.set_index("Feature").loc["selected_alpha", "Rank IC"]
    negative_ic = negative_metrics.set_index("Feature").loc["selected_alpha", "Rank IC"]
    assert positive_ic > 0.9
    assert negative_ic < -0.9


def test_bh_uses_only_finite_attempts_and_preserves_missing_values():
    adjusted = factor_analysis.benjamini_hochberg([0.01, 0.04, 0.03, None])

    assert adjusted[:3] == [0.03, 0.04, 0.04]
    assert adjusted[3] is None


def test_stat_gate_has_explicit_legacy_compatibility_switch(monkeypatch):
    features, labels = _panel(sign=1.0)
    monkeypatch.setattr(
        factor_analysis, "_statistic", lambda values, settings, seed: (0.1, 0.9)
    )

    gated = factor_analysis.evaluate_factor_frame(
        features[["selected_alpha"]], labels, _settings(stat_gate=True)
    ).iloc[0]
    compatible = factor_analysis.evaluate_factor_frame(
        features[["selected_alpha"]], labels, _settings(stat_gate=False)
    ).iloc[0]

    assert gated["base_pass"] and not gated["stat_pass"]
    assert not gated["is_effective"]
    assert compatible["is_effective"]


def test_decay_gate_aligns_negative_ic_and_rejects_fast_decay():
    durable_negative = {
        "1": -0.10,
        "2": -0.09,
        "3": -0.08,
        "5": -0.06,
        "10": -0.04,
        "20": -0.03,
    }
    fast_decay = {
        "1": 0.10,
        "2": 0.03,
        "3": 0.02,
        "5": 0.01,
        "10": 0.0,
        "20": 0.0,
    }

    durable = _decay_gate(durable_negative, 2.0, 5, 0.25)
    fast = _decay_gate(fast_decay, 2.0, 5, 0.25)

    assert durable["passed"]
    assert durable["retention"] == 0.6
    assert not fast["passed"]
    assert fast["half_life"] < 2.0


def test_screen_exact_workspace_does_not_scan_historical_siblings(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    sibling = tmp_path / "sibling"
    selected.mkdir()
    sibling.mkdir()
    for workspace, factor_name in ((selected, "selected_alpha"), (sibling, "old_alpha")):
        with h5py.File(workspace / "result.h5", "w") as handle:
            group = handle.create_group("data")
            group.create_dataset("axis0", data=np.asarray([factor_name.encode()]))

    monkeypatch.setattr(factor_rdagent_screen, "WORKSPACES", str(tmp_path))
    monkeypatch.setattr(factor_rdagent_screen, "EXACT_WORKSPACE", str(selected))
    paths, names, distinct = factor_rdagent_screen._load_distinct_factors(60)

    assert paths == [str(selected / "result.h5")]
    assert set(names) == {"selected_alpha"}
    assert [name for name, _ in distinct] == ["selected_alpha"]


def test_exact_workspace_reads_materialized_parquet_and_ignores_global_limit(
    tmp_path, monkeypatch
):
    selected = tmp_path / "selected"
    selected.mkdir()
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    instruments = ["sh600000", "sz000001"]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    factor_names = [f"factor_{number:03d}" for number in range(70)]
    columns = pd.MultiIndex.from_tuples(
        [("feature", name) for name in factor_names]
    )
    values = np.arange(len(index) * len(columns), dtype=float).reshape(
        len(index), len(columns)
    )
    pd.DataFrame(values, index=index, columns=columns).to_parquet(
        selected / factor_rdagent_screen.COMBINED_FACTORS_FILE
    )

    monkeypatch.setattr(factor_rdagent_screen, "WORKSPACES", str(tmp_path))
    monkeypatch.setattr(factor_rdagent_screen, "EXACT_WORKSPACE", str(selected))
    paths, names, distinct = factor_rdagent_screen._load_distinct_factors(60)

    assert paths == [
        str(selected / factor_rdagent_screen.COMBINED_FACTORS_FILE)
    ]
    assert set(names) == set(factor_names)
    assert len(distinct) == 70

    evaluator = type(
        "Evaluator",
        (),
        {
            "snapshots": [{"de": "2025-01-02"}, {"de": "2025-01-03"}],
            "C": pd.DataFrame(index=["2025-01-02", "2025-01-03"], columns=instruments),
        },
    )()
    panels = {
        name: panel
        for name, _source, panel in factor_rdagent_screen._iter_factor_panels(
            distinct, evaluator
        )
    }
    assert set(panels) == set(factor_names)
    assert panels["factor_069"].shape == (2, 2)
    assert panels["factor_069"].notna().all().all()


def _write_exact_screen(path, *, workspace="D:/workspace/winner", universe="csi300"):
    payload = {
        "scope": "exact_workspace",
        "workspace": workspace,
        "universe": universe,
        "screened": 3,
        "n_pass": 2,
        "passed_factors": ["alpha", "gamma"],
        "factors": [
            {"factor": "alpha", "pass": True},
            {"factor": "beta", "pass": False},
            {"factor": "gamma", "pass": True},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_factor_analysis_publishes_only_fdr_and_exact_screen_intersection(tmp_path):
    path = tmp_path / "screen.json"
    _write_exact_screen(path)

    gate = factor_analysis.load_exact_screen_gate(
        path,
        expected_workspace="d:\\workspace\\winner\\",
        expected_universe="csi300",
    )
    final = factor_analysis.apply_exact_screen_gate(
        ["alpha", "beta", "selection_only"], gate
    )

    assert gate["passed_factors"] == ["alpha", "gamma"]
    assert final == ["alpha"]


def test_factor_analysis_rejects_mismatched_exact_screen_identity(tmp_path):
    path = tmp_path / "screen.json"
    _write_exact_screen(path, universe="csi1000")

    with np.testing.assert_raises_regex(RuntimeError, "universe mismatch"):
        factor_analysis.load_exact_screen_gate(
            path,
            expected_workspace="D:/workspace/winner",
            expected_universe="csi300",
        )
