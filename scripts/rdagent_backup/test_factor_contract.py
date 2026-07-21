from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.rdagent_backup.factor_contract import (
    FactorContract,
    apply_contract_to_handler,
    contract_from_effective_factors,
    contract_from_manifest,
    filter_feature_frame,
)


def test_maintained_runner_preserves_contract_and_oos_controls():
    runner = (Path(__file__).with_name("run_model.py")).read_text(encoding="utf-8")

    assert "resolve_workspace_and_contract(batch)" in runner
    assert "apply_contract_to_handler(dataset.handler, factor_contract)" in runner
    assert 'os.environ.get("RDAGENT_BT_TEST_START"' in runner
    assert 'os.environ.get("RDAGENT_BT_TEST_END"' in runner
    assert 'os.environ.get("RDAGENT_PRED_ONLY"' in runner


def test_repair_script_deploys_runner_and_contract_together():
    repair = (
        Path(__file__).parents[1] / "repair_rdagent_factor_mining.ps1"
    ).read_text(encoding="utf-8")

    assert '"factor_contract.py"' in repair
    assert '"run_model.py"' in repair


def test_manifest_contract_excludes_only_evaluated_non_effective_factors():
    contract = contract_from_manifest(
        {
            "effective_factors": ["accepted", "accepted"],
            "all_features": ["accepted", "rejected", "rejected_2"],
        }
    )

    assert contract.effective_factors == frozenset({"accepted"})
    assert contract.excluded_features == frozenset({"rejected", "rejected_2"})


def test_manifest_without_effective_factors_fails_closed_like_prediction():
    contract = contract_from_manifest({"all_features": ["candidate_a", "candidate_b"]})

    assert contract.enabled
    assert contract.effective_factors == frozenset()
    assert contract.excluded_features == frozenset({"candidate_a", "candidate_b"})


def test_default_contract_without_selection_keeps_filtering_disabled():
    contract = contract_from_effective_factors(None, ["candidate"])

    assert not contract.enabled
    assert contract.excluded_features == frozenset()


def test_invalid_factor_list_is_rejected_instead_of_treating_string_as_characters():
    with pytest.raises(ValueError, match="effective_factors must be a list"):
        contract_from_manifest(
            {"effective_factors": "factor_a", "all_features": ["factor_a"]}
        )


def test_filter_feature_frame_preserves_base_features_and_labels():
    columns = pd.MultiIndex.from_tuples(
        [
            ("feature", "base_alpha"),
            ("feature", "accepted"),
            ("feature", "rejected"),
            ("label", "rejected"),
        ]
    )
    frame = pd.DataFrame([[1.0, 2.0, 3.0, 4.0]], columns=columns)

    filtered = filter_feature_frame(frame, {"rejected"})

    assert list(filtered.columns) == [
        ("feature", "base_alpha"),
        ("feature", "accepted"),
        ("label", "rejected"),
    ]


def test_apply_contract_filters_all_materialized_handler_frames():
    columns = pd.MultiIndex.from_tuples(
        [
            ("feature", "accepted"),
            ("feature", "rejected"),
            ("label", "LABEL0"),
        ]
    )
    handler = SimpleNamespace(
        _infer=pd.DataFrame([[1, 2, 3]], columns=columns),
        _learn=pd.DataFrame([[4, 5, 6]], columns=columns),
        _data=pd.DataFrame([[7, 8, 9]], columns=columns),
    )
    contract = FactorContract(
        effective_factors=frozenset({"accepted"}),
        all_features=frozenset({"accepted", "rejected"}),
    )

    removed = apply_contract_to_handler(handler, contract)

    assert removed == {"_infer": 1, "_learn": 1, "_data": 1}
    for attribute in ("_infer", "_learn", "_data"):
        assert list(getattr(handler, attribute).columns) == [
            ("feature", "accepted"),
            ("label", "LABEL0"),
        ]
