from __future__ import annotations

import pandas as pd

from scripts.replay_advisor_pro_qlib import (
    _enhanced_leg,
    attach_fixed_baskets,
    build_execution_schedule,
    find_executable_open,
    performance_metrics,
    select_non_overlapping_periods,
    validate_period_membership,
    validate_published_ledger,
)


def record(d, de, dx, m, v, trend=1.0, spread=1.0):
    return {
        "d": d,
        "de": de,
        "dx": dx,
        "trend": trend,
        "vspread": spread,
        "e": {"M": (m, set()), "V": (v, set()), "Me": (m, set()), "Ve": (v, set())},
    }


def test_non_overlap_gate_and_published_ledger_validation():
    rows = [
        record("2020-01-01", "2020-01-02", "2020-04-01", 0.1, 0.2),
        record("2020-02-01", "2020-02-03", "2020-05-01", 0.2, 0.1),
        record("2020-04-01", "2020-04-01", "2020-07-01", 0.3, 0.1),
    ]
    selected = select_non_overlapping_periods(rows)
    assert [(row["snapshot_date"], row["legacy_exit_date"]) for row in selected] == [
        ("2020-01-01", "2020-04-01"),
        ("2020-04-01", "2020-07-01"),
    ]
    published = {
        "track": {
            "ledger": [
                {"d": row["snapshot_date"], "dx": row["legacy_exit_date"], "pick": row["pick"]}
                for row in selected
            ]
        }
    }
    assert validate_published_ledger(selected, published)["matched"]


def test_non_overlap_logic_is_independent_of_input_order():
    rows = [
        record("2020-01-01", "2020-01-02", "2020-04-01", 0.1, 0.2),
        record("2020-02-01", "2020-02-03", "2020-05-01", 0.2, 0.1),
        record("2020-04-01", "2020-04-01", "2020-07-01", 0.3, 0.1),
    ]
    assert select_non_overlapping_periods(rows) == select_non_overlapping_periods(list(reversed(rows)))


def test_fixed_basket_tie_break_and_t_plus_one_schedule():
    periods = [
        {
            "snapshot_date": "2026-01-30",
            "signal_date": "2026-02-02",
            "legacy_exit_date": "2026-05-11",
            "pick": "MOM",
            "score_field": "Mscore",
        }
    ]
    scores = pd.DataFrame(
        {
            "inst": ["SZ000002", "SH600001", "SZ000001"],
            "de": ["2026-02-02"] * 3,
            "dx": ["2026-05-11"] * 3,
            "Mscore": [2.0, 2.0, 1.0],
            "Vscore": [0.0, 0.0, 0.0],
        }
    )
    attached = attach_fixed_baskets(periods, scores, topn=2)
    assert attached[0]["codes"] == ["SH600001", "SZ000002"]
    calendar = list(pd.to_datetime(["2026-02-02", "2026-02-03", "2026-05-11", "2026-05-12"]))
    targets = build_execution_schedule(attached, calendar)
    assert targets["2026-02-03"] == {"SH600001": 0.5, "SZ000002": 0.5}
    assert targets["2026-05-12"] == {}


def test_executable_open_respects_direction_and_retry_boundary():
    dates = list(pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]))
    index = pd.MultiIndex.from_product(
        [["SH600000"], dates], names=["instrument", "datetime"]
    )
    quotes = pd.DataFrame({"$open": [10, 9, 10, 10, 10, 99]}, index=index)
    states = pd.DataFrame(
        {
            "suspended": [True, False, True, True, True, False],
            "limit_buy": [True, False, True, True, True, False],
            "limit_sell": [True, True, True, True, True, False],
        },
        index=index,
    )

    buy = find_executable_open(quotes, states, dates[:5], "600000.SH", "buy")
    sell = find_executable_open(quotes, states, dates[:5], "SH600000", "sell")

    assert buy["filled"] and buy["date"] == "2026-07-02"
    assert not sell["filled"]
    assert all(item["date"] != "2026-07-08" for item in sell["attempts"])


def test_vote_uses_only_completed_raw_legs_and_chosen_leg_controls_gate():
    prior = record("2020-01-01", "2020-01-02", "2020-04-01", 10.0, -10.0)
    prior["vote_available_date"] = "2020-04-10"
    current = record("2020-04-02", "2020-04-03", "2020-07-01", 0.1, 0.2, trend=1.0)
    current["leg_completion_dates"] = {
        "M": "2020-07-01",
        "V": "2020-07-01",
        "Me": "2020-07-03",
        "Ve": "2020-07-20",
    }

    assert _enhanced_leg(current, [prior, current], 1, lookback=1) == "M"
    selected = select_non_overlapping_periods([current], lookback=1)
    assert selected[0]["pick"] == "MOM"
    assert selected[0]["selection_gate_exit_date"] == "2020-07-03"


def test_period_membership_checks_candidates_and_selected_names():
    class FakeUniverse:
        def members_on(self, _date):
            return frozenset({"600001.SH", "000002.SZ"})

    periods = [
        {
            "snapshot_date": "2026-01-30",
            "signal_date": "2026-02-02",
            "legacy_exit_date": "2026-05-11",
            "codes": ["SH600001"],
        }
    ]
    scores = pd.DataFrame(
        {
            "inst": ["SH600001", "SZ000002"],
            "de": ["2026-02-02", "2026-02-02"],
            "dx": ["2026-05-11", "2026-05-11"],
        }
    )

    result = validate_period_membership(periods, scores, FakeUniverse())

    assert result["passed"]
    assert result["checked_candidates"] == 2
    assert result["checked_selected"] == 1


def test_period_membership_rejects_a_silently_pruned_member():
    class FakeUniverse:
        def members_on(self, _date):
            return frozenset({"600001.SH", "000002.SZ"})

    periods = [
        {
            "snapshot_date": "2026-01-30",
            "signal_date": "2026-02-02",
            "legacy_exit_date": "2026-05-11",
            "codes": ["SH600001"],
        }
    ]
    scores = pd.DataFrame(
        {"inst": ["SH600001"], "de": ["2026-02-02"], "dx": ["2026-05-11"]}
    )

    result = validate_period_membership(periods, scores, FakeUniverse())

    assert not result["passed"]
    assert result["member_omission_count"] == 1


def test_drawdown_includes_loss_from_initial_nav():
    metrics = performance_metrics(pd.Series([-0.10, 0.05]))

    assert metrics["max_drawdown"] == -0.10
