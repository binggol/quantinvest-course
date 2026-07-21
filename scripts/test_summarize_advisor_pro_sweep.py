import json
from pathlib import Path

import pytest

from scripts.summarize_advisor_pro_sweep import (
    atomic_write_json,
    dominates,
    summarize_chunks,
)


def metrics(
    annualized_return: float,
    *,
    sharpe: float = 1.0,
    calmar: float = 1.0,
    rolling_p10: float = 0.1,
    max_drawdown: float = -0.15,
    worst_60d: float = -0.10,
    double_cost: float = 0.08,
) -> dict:
    return {
        "n": 750,
        "annualized_return": annualized_return,
        "sharpe": sharpe,
        "calmar": calmar,
        "rolling_252d_sharpe_p10": rolling_p10,
        "rolling_252d_return_p10": 0.02,
        "max_drawdown": max_drawdown,
        "worst_60d": worst_60d,
        "double_cost_annualized_return": double_cost,
    }


def checkpoint(
    run_id: str,
    *,
    topn: int = 10,
    frequency: int = 5,
    offset: int = 0,
    replacement_ratio: float | None = 0.2,
    max_replacements: int | None = 2,
    account: float = 10_000_000,
    validation: dict | None = None,
    recent: dict | None = None,
    hedged_validation: dict | None = None,
    hedged_recent: dict | None = None,
    turnover: float = 8.0,
    no_fill_rate: float = 0.02,
) -> dict:
    long_validation = validation or metrics(0.12)
    long_recent = recent or metrics(0.50)
    hedged_validation = hedged_validation or long_validation
    hedged_recent = hedged_recent or long_recent
    return {
        "run_id": run_id,
        "status": "success",
        "spec": {
            "portfolio_topn": topn,
            "frequency_days": frequency,
            "frequency_offset": offset,
            "replacement_ratio": replacement_ratio,
            "max_replacements": max_replacements,
            "rebalance_mode": "replace_only",
            "account": account,
        },
        "result": {
            "evaluation_periods": {
                "validation_2022_2024": {
                    "long_only": long_validation,
                    "exposure_matched_hedged": hedged_validation,
                },
                "recent_2025_plus": {
                    "long_only": long_recent,
                    "exposure_matched_hedged": hedged_recent,
                },
            },
            "execution": {
                "attempts": 1000,
                "unfilled": int(1000 * no_fill_rate),
                "no_fill_rate": no_fill_rate,
                "annualized_one_way_turnover": turnover,
                "final_holding_count": 0,
                "settlement_mode": "liquidated",
            },
        },
    }


def write_chunk(path: Path, runs: list[dict]) -> None:
    path.write_text(json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8")


def test_summary_deduplicates_run_id_and_ignores_recent_period_in_ranking(tmp_path):
    better_validation = metrics(0.18, sharpe=1.2, double_cost=0.12)
    worse_validation = metrics(0.10, sharpe=0.8, double_cost=0.04)
    first = checkpoint("better", validation=better_validation, recent=metrics(-0.90))
    second = checkpoint("worse", validation=worse_validation, recent=metrics(9.00))
    one = tmp_path / "one.json"
    two = tmp_path / "two.json"
    write_chunk(one, [first, second])
    write_chunk(two, [first])

    summary = summarize_chunks([one, two], top_n=5)

    assert summary["selection_basis"] == "long_only"
    assert summary["selection_policy"]["selection_basis"] == "long_only"
    assert summary["counts"]["unique_checkpoints"] == 2
    assert summary["inputs"]["duplicate_run_ids"] == ["better"]
    assert summary["rankings"]["highest_return"][0]["run_id"] == "better"
    assert summary["rankings"]["highest_return"][0][
        "recent_2025_plus_report_only"
    ]["annualized_return"] == -0.9
    assert "recent_2025_plus" not in summary["selection_policy"]["pareto_objectives"]


def test_default_long_only_and_explicit_hedged_use_their_own_period_metrics(tmp_path):
    long_winner = checkpoint(
        "long-winner",
        validation=metrics(0.18, sharpe=1.3, double_cost=0.12),
        recent=metrics(-0.90),
        hedged_validation=metrics(0.06, sharpe=0.8, double_cost=0.02),
        hedged_recent=metrics(9.0),
    )
    hedged_winner = checkpoint(
        "hedged-winner",
        validation=metrics(0.08, sharpe=0.9, double_cost=0.03),
        recent=metrics(9.0),
        hedged_validation=metrics(0.20, sharpe=1.4, double_cost=0.14),
        hedged_recent=metrics(-0.90),
    )
    source = tmp_path / "chunk.json"
    write_chunk(source, [long_winner, hedged_winner])

    default_summary = summarize_chunks([source])
    hedged_summary = summarize_chunks([source], basis="exposure_matched_hedged")

    assert default_summary["selection_basis"] == "long_only"
    assert default_summary["rankings"]["highest_return"][0]["run_id"] == "long-winner"
    assert default_summary["rankings"]["highest_return"][0][
        "recent_2025_plus_report_only"
    ]["annualized_return"] == -0.9
    assert hedged_summary["selection_basis"] == "exposure_matched_hedged"
    assert hedged_summary["rankings"]["highest_return"][0]["run_id"] == "hedged-winner"
    assert hedged_summary["rankings"]["highest_return"][0][
        "recent_2025_plus_report_only"
    ]["annualized_return"] == -0.9


def test_unknown_selection_basis_is_rejected(tmp_path):
    source = tmp_path / "chunk.json"
    write_chunk(source, [checkpoint("run")])

    with pytest.raises(ValueError, match="basis must be one of"):
        summarize_chunks([source], basis="unknown")


def test_hard_gate_failures_are_excluded_from_percentiles_and_rankings(tmp_path):
    passed = checkpoint("passed", validation=metrics(0.08, double_cost=0.03))
    failed = checkpoint(
        "failed-gate",
        validation=metrics(0.25, max_drawdown=-0.35, double_cost=-0.02),
    )
    source = tmp_path / "chunk.json"
    write_chunk(source, [passed, failed])

    summary = summarize_chunks([source])
    by_id = {row["run_id"]: row for row in summary["runs"]}

    assert summary["counts"]["hard_gate_passed"] == 1
    assert summary["rankings"]["highest_return"][0]["run_id"] == "passed"
    assert by_id["failed-gate"]["scores"] is None
    assert set(by_id["failed-gate"]["hard_gate"]["failed_checks"]) == {
        "positive_validation_double_cost_return",
        "validation_drawdown_within_25pct",
    }


def test_percentile_scores_use_all_eight_declared_objectives(tmp_path):
    high_return = checkpoint(
        "return",
        validation=metrics(
            0.20,
            sharpe=0.7,
            calmar=0.8,
            rolling_p10=-0.1,
            max_drawdown=-0.22,
            worst_60d=-0.15,
            double_cost=0.10,
        ),
        turnover=12,
    )
    stable = checkpoint(
        "stable",
        validation=metrics(
            0.12,
            sharpe=1.6,
            calmar=1.8,
            rolling_p10=0.6,
            max_drawdown=-0.08,
            worst_60d=-0.04,
            double_cost=0.09,
        ),
        turnover=4,
    )
    source = tmp_path / "chunk.json"
    write_chunk(source, [high_return, stable])

    summary = summarize_chunks([source])
    by_id = {row["run_id"]: row for row in summary["runs"]}

    assert summary["rankings"]["highest_return"][0]["run_id"] == "return"
    assert summary["rankings"]["most_stable"][0]["run_id"] == "stable"
    assert summary["rankings"]["best_balanced"][0]["run_id"] == "stable"
    assert set(by_id["stable"]["percentiles"]) == set(
        summary["selection_policy"]["pareto_objectives"]
    )


def test_pareto_rejects_a_run_that_is_worse_on_every_objective(tmp_path):
    dominant = checkpoint(
        "dominant",
        validation=metrics(
            0.18,
            sharpe=1.4,
            calmar=1.5,
            rolling_p10=0.4,
            max_drawdown=-0.10,
            worst_60d=-0.05,
            double_cost=0.12,
        ),
        turnover=5,
    )
    dominated = checkpoint(
        "dominated",
        validation=metrics(
            0.10,
            sharpe=0.9,
            calmar=0.8,
            rolling_p10=0.0,
            max_drawdown=-0.20,
            worst_60d=-0.12,
            double_cost=0.03,
        ),
        turnover=9,
    )
    source = tmp_path / "chunk.json"
    write_chunk(source, [dominant, dominated])
    summary = summarize_chunks([source])
    normalized = {row["run_id"]: row for row in summary["runs"]}

    assert dominates(normalized["dominant"], normalized["dominated"])
    assert summary["pareto_front"] == ["dominant"]


def test_parameter_neighborhood_uses_adjacent_single_dimension_changes(tmp_path):
    base = checkpoint("n10", topn=10, max_replacements=2, replacement_ratio=0.2)
    neighbor = checkpoint("n12", topn=12, max_replacements=2, replacement_ratio=0.2)
    not_neighbor = checkpoint(
        "n20-f10", topn=20, frequency=10, max_replacements=4, replacement_ratio=0.2
    )
    source = tmp_path / "chunk.json"
    write_chunk(source, [base, neighbor, not_neighbor])

    summary = summarize_chunks([source])
    neighborhoods = summary["parameter_neighborhoods"]

    assert neighborhoods["n10"]["neighbor_run_ids"] == ["n12"]
    assert "n20-f10" not in neighborhoods["n10"]["neighbor_run_ids"]


def test_missing_validation_metric_has_precise_location(tmp_path):
    broken = checkpoint("broken")
    del broken["result"]["evaluation_periods"]["validation_2022_2024"]["long_only"][
        "rolling_252d_sharpe_p10"
    ]
    source = tmp_path / "chunk.json"
    write_chunk(source, [broken])

    with pytest.raises(ValueError, match=r"runs\[0\].*rolling_252d_sharpe_p10 is required"):
        summarize_chunks([source])


def test_duplicate_run_id_with_conflicting_results_is_rejected(tmp_path):
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    write_chunk(first, [checkpoint("same", validation=metrics(0.10))])
    write_chunk(second, [checkpoint("same", validation=metrics(0.11))])

    with pytest.raises(ValueError, match="conflicting successful results"):
        summarize_chunks([first, second])


def test_failed_checkpoint_is_reported_but_not_scored(tmp_path):
    failed = checkpoint("failed")
    failed["status"] = "failed"
    failed.pop("result")
    failed["error"] = {"type": "RuntimeError", "message": "backtest failed"}
    source = tmp_path / "chunk.json"
    write_chunk(source, [failed, checkpoint("passed")])

    summary = summarize_chunks([source])

    assert summary["counts"]["failed_checkpoints"] == 1
    assert summary["failed_checkpoints"][0]["run_id"] == "failed"
    assert all(
        row["run_id"] != "failed"
        for rows in summary["rankings"].values()
        for row in rows
    )


def test_atomic_write_json_replaces_file_without_leaving_temp_files(tmp_path):
    output = tmp_path / "summary.json"
    output.write_text('{"old": true}', encoding="utf-8")

    atomic_write_json(output, {"new": True})

    assert json.loads(output.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob(".summary.json.*.tmp")) == []
