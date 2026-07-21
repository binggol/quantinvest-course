import json
import statistics
from pathlib import Path

import pytest

from rdagent_backup import promote_production_champion as promotion


def execution():
    return {
        "mode": "next_open",
        "deal_price": "open",
        "entry_timing": "next_trading_day_open",
        "return_horizon": "next_open_to_following_open",
        "only_tradable": True,
        "max_volume_participation": 0.05,
        "volume_threshold": ["current", "0.05 * $volume * 100"],
        "score_transform": "style_neutralized_size_momentum_volatility",
    }


def evaluation():
    return {
        "test_start": "2025-07-01",
        "test_end": "2026-07-15",
        "signal_data_start": "2025-07-01",
        "signal_data_end": "2026-07-16",
        "benchmark": "SH000300",
        "account": 100000000.0,
        "costs": {"open_cost": 0.0005, "close_cost": 0.0015, "min_cost": 5.0},
        "strategy": {"topk": 50, "n_drop": 5},
    }


def result(key, excesses, *, ensemble, ir, maxdd):
    seed_metrics = [
        {
            "seed": seed,
            "excess": value,
            "ir": ir + (seed - 1) * 0.02,
            "maxdd": maxdd,
            "ann": 0.2 + value,
            "rank_ic": 0.02,
            "rank_icir": 0.15,
        }
        for seed, value in enumerate(excesses)
    ]
    return {
        "key": key,
        "batch": key.split("::", 1)[0],
        "model": "lgb",
        "rank_ic": 0.02,
        "excess": ensemble,
        "excess_lo": min(excesses),
        "excess_hi": max(excesses),
        "ir": ir,
        "maxdd": maxdd,
        "ann": 0.25,
        "n_seeds": len(excesses),
        "aggregation": "per_instrument_score_mean",
        "seed_dispersion": {
            "excess_std": statistics.pstdev(excesses),
            "ir_std": 0.01,
            "maxdd_std": 0.0,
            "ann_std": 0.01,
            "rank_ic_std": 0.0,
        },
        "seed_metrics": seed_metrics,
        "execution": execution(),
        "evaluation": evaluation(),
        "provenance": {
            "workspace": f"Z:/claude/rdagent_workspace/{'a' * 32}",
            "universe": "csi300",
            "effective_factors": ["alpha"],
            "all_features": ["alpha"],
        },
        "updated_at": "2026-07-19 08:00:00",
    }


def write_manifest(root: Path, label: str, factors=("alpha",)):
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "label": label,
        "workspace": f"Z:/claude/rdagent_workspace/{'a' * 32}",
        "test_used_for_selection": False,
        "selection_period": {"start": "2024-01-01", "end": "2024-12-31"},
        "test_report_period": {"start": "2025-01-01", "end": "2025-06-30"},
        "fdr_effective_factors": list(factors),
        "effective_factors": list(factors),
        "exact_screen_gate": {
            "scope": "exact_workspace",
            "universe": "csi300",
            "workspace": f"D:/rdagent_workspace/{'a' * 32}",
            "screened": len(factors),
            "n_pass": len(factors),
            "passed_factors": list(factors),
        },
    }
    (root / f"{label}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def write_results(path: Path, rows):
    path.write_text(json.dumps({"results": rows}), encoding="utf-8")


def test_tournament_uses_joint_gate_and_conservative_seed_order(tmp_path):
    batches = tmp_path / "batches"
    write_manifest(batches, "candidate_a")
    write_manifest(batches, "candidate_b")
    results = tmp_path / "model_results.json"
    write_results(
        results,
        [
            result("default::lgb", [0.01, 0.02, 0.03], ensemble=0.02, ir=0.4, maxdd=-0.08),
            result("candidate_a::lgb", [0.07, 0.11, 0.12], ensemble=0.11, ir=1.0, maxdd=-0.05),
            result("candidate_b::lgb", [0.08, 0.09, 0.10], ensemble=0.095, ir=0.9, maxdd=-0.04),
        ],
    )

    decision = promotion.build_decision(
        labels=["candidate_a", "candidate_b"],
        batches_dir=batches,
        model_results_path=results,
        incumbent_key="default::lgb",
        policy=promotion.PromotionPolicy(),
    )

    assert decision["eligible_count"] == 2
    assert decision["selected_label"] == "candidate_b"
    assert set(decision["pareto_labels"]) == {"candidate_a", "candidate_b"}


def test_negative_worst_seed_blocks_high_ensemble_return(tmp_path):
    batches = tmp_path / "batches"
    write_manifest(batches, "unstable")
    results = tmp_path / "model_results.json"
    write_results(
        results,
        [
            result("default::lgb", [-0.01, 0.02, 0.03], ensemble=0.02, ir=0.4, maxdd=-0.08),
            result("unstable::lgb", [-0.02, 0.15, 0.16], ensemble=0.12, ir=1.1, maxdd=-0.05),
        ],
    )

    decision = promotion.build_decision(
        labels=["unstable"],
        batches_dir=batches,
        model_results_path=results,
        incumbent_key="default::lgb",
        policy=promotion.PromotionPolicy(max_seed_excess_std=0.2),
    )

    assert decision["promote"] is False
    assert "worst-seed net excess return below minimum" in decision["candidates"][0]["failures"]


def test_mismatched_oos_contract_blocks_candidate(tmp_path):
    batches = tmp_path / "batches"
    write_manifest(batches, "different_window")
    incumbent = result("default::lgb", [0.01, 0.02, 0.03], ensemble=0.02, ir=0.4, maxdd=-0.08)
    candidate = result(
        "different_window::lgb", [0.08, 0.09, 0.10], ensemble=0.09, ir=0.9, maxdd=-0.05
    )
    candidate["evaluation"]["test_start"] = "2026-01-01"
    results = tmp_path / "model_results.json"
    write_results(results, [incumbent, candidate])

    decision = promotion.build_decision(
        labels=["different_window"],
        batches_dir=batches,
        model_results_path=results,
        incumbent_key="default::lgb",
        policy=promotion.PromotionPolicy(),
    )

    assert decision["promote"] is False
    assert "OOS/cost/strategy contract differs from incumbent" in decision["candidates"][0]["failures"]


def test_manifest_requires_test_isolation_and_exact_factor_intersection(tmp_path):
    batches = tmp_path / "batches"
    payload = write_manifest(batches, "leaky", factors=("alpha",))
    payload["test_used_for_selection"] = True
    (batches / "leaky.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(promotion.PromotionError, match="test_used_for_selection"):
        promotion.load_batch_manifest(batches, "leaky")

    payload["test_used_for_selection"] = False
    payload["exact_screen_gate"]["passed_factors"] = ["other"]
    (batches / "leaky.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(promotion.PromotionError, match="subset"):
        promotion.load_batch_manifest(batches, "leaky")

    payload["exact_screen_gate"]["passed_factors"] = ["alpha"]
    payload["exact_screen_gate"]["universe"] = "csi1000"
    (batches / "leaky.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(promotion.PromotionError, match="not a csi300"):
        promotion.load_batch_manifest(batches, "leaky")


def test_commit_updates_legacy_pointers_and_auditable_champion_last(tmp_path):
    batches = tmp_path / "batches"
    write_manifest(batches, "winner", factors=("alpha", "beta"))
    results = tmp_path / "model_results.json"
    incumbent = result("default::lgb", [0.01, 0.02, 0.03], ensemble=0.02, ir=0.4, maxdd=-0.08)
    winner = result("winner::lgb", [0.08, 0.09, 0.10], ensemble=0.09, ir=0.9, maxdd=-0.05)
    winner["provenance"]["effective_factors"] = ["alpha", "beta"]
    winner["provenance"]["all_features"] = ["alpha", "beta"]
    write_results(
        results,
        [
            incumbent,
            winner,
        ],
    )
    decision = promotion.build_decision(
        labels=["winner"],
        batches_dir=batches,
        model_results_path=results,
        incumbent_key="default::lgb",
        policy=promotion.PromotionPolicy(),
    )
    workspace_pointer = tmp_path / "sota_workspace.txt"
    factor_pointer = tmp_path / "effective_factors.json"
    champion_path = tmp_path / "production_champion.json"

    champion = promotion.commit_decision(
        decision,
        champion_path=champion_path,
        workspace_pointer=workspace_pointer,
        factor_pointer=factor_pointer,
    )

    assert workspace_pointer.read_text(encoding="utf-8").strip().endswith("a" * 32)
    assert json.loads(factor_pointer.read_text(encoding="utf-8")) == ["alpha", "beta"]
    persisted = json.loads(champion_path.read_text(encoding="utf-8"))
    assert persisted["generation"] == champion["generation"]
    assert persisted["champion"]["label"] == "winner"


def test_live_incumbent_must_match_backtest_provenance(tmp_path):
    decision = {
        "incumbent": {
            "metrics": {
        "provenance": {
            "workspace": f"Z:/claude/rdagent_workspace/{'a' * 32}",
            "universe": "csi300",
                    "effective_factors": ["alpha"],
                }
            }
        }
    }
    workspace_pointer = tmp_path / "sota_workspace.txt"
    factor_pointer = tmp_path / "effective_factors.json"
    workspace_pointer.write_text(
        f"Z:/claude/rdagent_workspace/{'b' * 32}\n", encoding="utf-8"
    )
    factor_pointer.write_text('["alpha"]', encoding="utf-8")

    with pytest.raises(promotion.PromotionError, match="live workspace pointer"):
        promotion.validate_live_incumbent(decision, workspace_pointer, factor_pointer)
