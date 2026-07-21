from __future__ import annotations

import json

import pytest

from scripts.summarize_advisor_pro_staggered import main, summarize_staggered


EXECUTION_PARAMETERS = {
    "rank_buffer": 0,
    "commission": 0.0003,
    "max_volume_participation": 0.1,
    "impact_cost": 0.1,
    "risk_degree": 0.95,
    "retry_days": 5,
    "hedge_yearly_cost": 0.01,
}


def run(
    offset: int,
    step: int,
    *,
    turnover: float | None = None,
    cost_rates: tuple[float, float] | None = None,
    execution_counts: dict | None = None,
) -> dict:
    start_day = 2 + offset
    return {
        "frequency_days": step * 5,
        "frequency_step": step,
        "frequency_offset": offset,
        "portfolio_spec": {
            "portfolio_topn": 10,
            "max_replacements": 2,
            "rebalance_mode": "replace_only",
            "account": 10_000_000,
        },
        "execution_parameters": dict(EXECUTION_PARAMETERS),
        "execution": {
            "settlement_mode": "liquidated",
            "final_holding_count": 0,
            "annualized_one_way_turnover": (
                float(offset + 1) if turnover is None else turnover
            ),
            **({} if execution_counts is None else execution_counts),
        },
        "daily_path": [
            {
                "date": f"2025-01-{start_day:02d}",
                "net_return": 0.10 * (offset + 1),
                "hedged_return": 0.05 * (offset + 1),
                **({"cost_rate": cost_rates[0]} if cost_rates is not None else {}),
            },
            {
                "date": f"2025-01-{start_day + 1:02d}",
                "net_return": 0.0,
                "hedged_return": 0.0,
                **({"cost_rate": cost_rates[1]} if cost_rates is not None else {}),
            },
        ],
    }


@pytest.mark.parametrize("step", [1, 2, 3, 4, 12])
def test_supports_every_required_offset_step(step):
    summary = summarize_staggered({"runs": [run(offset, step) for offset in range(step)]})

    assert summary["offsets"] == {
        "expected": list(range(step)),
        "observed": list(range(step)),
        "count": step,
    }
    assert set(summary["staggered"]["long_only"]) == {
        "full",
        "development_2017_2021",
        "validation_2022_2024",
        "recent_2025_plus",
    }
    assert summary["turnover"]["median"] == pytest.approx((step + 1) / 2)


def test_equal_capital_combination_keeps_later_sleeve_in_cash():
    summary = summarize_staggered({"runs": [run(0, 2), run(1, 2)]})

    assert summary["staggered"]["long_only"]["full"]["total_return"] == 0.15
    assert summary["staggered"]["exposure_matched_hedged"]["full"][
        "total_return"
    ] == 0.075
    assert summary["construction"].endswith("no capital transfers between sleeves")


def test_combines_exact_double_cost_paths_when_every_sleeve_has_daily_cost():
    summary = summarize_staggered(
        {"runs": [run(0, 1, cost_rates=(0.01, 0.02))]}
    )

    assert summary["schema_version"] == 3
    assert summary["double_cost"]["available"] is True
    assert summary["double_cost"]["missing_offsets"] == []
    assert summary["double_cost"]["staggered"]["long_only"]["full"][
        "total_return"
    ] == 0.0682
    assert summary["double_cost"]["staggered"]["exposure_matched_hedged"][
        "full"
    ]["total_return"] == 0.0192


def test_legacy_daily_paths_remain_supported_without_double_cost_metrics():
    summary = summarize_staggered({"runs": [run(0, 2), run(1, 2)]})

    assert summary["double_cost"] == {
        "available": False,
        "definition": (
            "subtract each sleeve's realized daily cost_rate once more from its "
            "already-net return before equal-capital NAV combination"
        ),
        "missing_offsets": [0, 1],
        "staggered": None,
    }
    assert summary["execution_quality"] == {
        "available": False,
        "all_liquidated": True,
        "unavailable_offsets": [0, 1],
        "aggregate": None,
        "per_offset_ranges": None,
    }


def test_aggregates_exact_execution_counts_and_offset_ranges():
    first = {
        "attempts": 10,
        "trades": 8,
        "unfilled": 2,
        "no_fill_rate": 0.2,
        "partial_fill_rate": 0.1,
        "incomplete_fill_rate": 0.3,
        "reason_counts": {"filled": 7, "partial": 1, "suspended": 2},
    }
    second = {
        "attempts": 20,
        "trades": 18,
        "unfilled": 2,
        "reason_counts": {"filled": 15, "partial": 3, "suspended": 2},
    }

    summary = summarize_staggered(
        {
            "runs": [
                run(0, 2, execution_counts=first),
                run(1, 2, execution_counts=second),
            ]
        }
    )

    quality = summary["execution_quality"]
    assert quality["available"] is True
    assert quality["all_liquidated"] is True
    assert quality["unavailable_offsets"] == []
    assert quality["aggregate"] == {
        "attempts": 30,
        "trades": 26,
        "no_fill_count": 4,
        "no_fill_rate": pytest.approx(4 / 30),
        "partial_count": 4,
        "partial_rate": pytest.approx(4 / 30),
        "incomplete_count": 8,
        "incomplete_rate": pytest.approx(8 / 30),
    }
    assert quality["per_offset_ranges"]["attempts"] == {"min": 10, "max": 20}
    assert quality["per_offset_ranges"]["partial_count"] == {"min": 1, "max": 3}
    assert quality["per_offset_ranges"]["incomplete_rate"] == {
        "min": pytest.approx(0.25),
        "max": pytest.approx(0.3),
    }
    assert summary["offset_runs"][0]["execution_quality"]["no_fill_count"] == 2


def test_missing_partial_reason_is_exact_zero():
    counts = {
        "attempts": 4,
        "trades": 3,
        "unfilled": 1,
        "reason_counts": {"filled": 3, "suspended": 1},
    }

    summary = summarize_staggered(
        {"runs": [run(0, 1, execution_counts=counts)]}
    )

    aggregate = summary["execution_quality"]["aggregate"]
    assert aggregate["partial_count"] == 0
    assert aggregate["partial_rate"] == 0
    assert aggregate["incomplete_count"] == 1


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ({"attempts": 1}, "counts are incomplete"),
        (
            {
                "attempts": 9,
                "trades": 10,
                "unfilled": -1,
                "reason_counts": {"filled": 9},
            },
            "cannot be negative",
        ),
        (
            {
                "attempts": 10,
                "trades": 7,
                "unfilled": 2,
                "reason_counts": {"filled": 8, "suspended": 2},
            },
            "attempts must equal",
        ),
        (
            {
                "attempts": 10,
                "trades": 8,
                "unfilled": 2,
                "reason_counts": {"filled": 7, "suspended": 2},
            },
            "reason_counts must sum",
        ),
        (
            {
                "attempts": 10,
                "trades": 8,
                "unfilled": 2,
                "no_fill_rate": 0.1,
                "reason_counts": {"filled": 8, "suspended": 2},
            },
            "does not match the exact execution counts",
        ),
    ],
)
def test_rejects_invalid_execution_counts(counts, message):
    with pytest.raises(ValueError, match=message):
        summarize_staggered({"runs": [run(0, 1, execution_counts=counts)]})


def test_mixed_new_and_legacy_sleeves_remain_supported_without_partial_stress():
    summary = summarize_staggered(
        {"runs": [run(0, 2, cost_rates=(0.01, 0.0)), run(1, 2)]}
    )

    assert summary["double_cost"]["available"] is False
    assert summary["double_cost"]["missing_offsets"] == [1]


def test_rejects_partially_populated_daily_cost_path():
    payload = run(0, 1, cost_rates=(0.01, 0.02))
    payload["daily_path"][1].pop("cost_rate")

    with pytest.raises(ValueError, match="cost_rate must be present on every row"):
        summarize_staggered({"runs": [payload]})


def test_accepts_detailed_checkpoint_wrappers_with_outer_spec():
    detailed = run(0, 1)
    portfolio_spec = detailed.pop("portfolio_spec")
    execution_parameters = detailed.pop("execution_parameters")
    wrapper = {
        "status": "success",
        "spec": {**portfolio_spec, "run_parameters": execution_parameters},
        "result": detailed,
    }

    summary = summarize_staggered({"runs": [wrapper]})

    assert summary["portfolio_spec"]["portfolio_topn"] == 10
    assert summary["execution_parameters"] == EXECUTION_PARAMETERS


def test_rejects_incomplete_offsets():
    with pytest.raises(ValueError, match="offsets must be complete"):
        summarize_staggered({"runs": [run(0, 3), run(2, 3)]})


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item["portfolio_spec"].update(portfolio_topn=12), "same portfolio"),
        (
            lambda item: item["execution_parameters"].update(commission=0.0004),
            "same execution",
        ),
        (
            lambda item: item["execution"].update(settlement_mode="unresolved_mark_to_market"),
            "fully liquidated",
        ),
        (lambda item: item.pop("daily_path"), "daily_path"),
    ],
)
def test_rejects_mismatched_or_incomplete_sleeves(mutation, message):
    runs = [run(0, 2), run(1, 2)]
    mutation(runs[1])

    with pytest.raises(ValueError, match=message):
        summarize_staggered({"runs": runs})


def test_cli_writes_summary_without_running_backtest(tmp_path, capsys):
    source = tmp_path / "detailed.json"
    output = tmp_path / "summary.json"
    source.write_text(json.dumps({"runs": [run(0, 1)]}), encoding="utf-8")

    main(["--input", str(source), "--out", str(output)])

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["offsets"]["count"] == 1
    assert json.loads(capsys.readouterr().out)["offset_count"] == 1
