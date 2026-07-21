from __future__ import annotations

import argparse
from dataclasses import FrozenInstanceError
import os

import numpy as np
import pandas as pd
import pytest

from scripts.backtest_advisor_pro_frequency import (
    PortfolioSpec,
    SCORING_INPUT_ARGUMENTS,
    SCORING_SCHEMA_VERSION,
    WeeklySignalBuilder,
    adjust_fundamentals_by_price,
    build_growth_records,
    build_signal_cache_signature,
    build_targets,
    capped_topk_dropout,
    choose_regime,
    compact_period_metrics,
    detailed_metrics,
    evaluation_period_masks,
    first_flat_position_date,
    is_valid_signal_price,
    latest_asof,
    marked_residual_holdings,
    rank_codes_by_score,
    week_end_dates,
)


class FakePosition:
    def __init__(self, amounts, prices=None):
        self.amounts = amounts
        self.prices = prices or {}

    def get_stock_amount_dict(self):
        return self.amounts

    def get_stock_price(self, code):
        return self.prices[code]


def make_signature_inputs(tmp_path):
    paths = {}
    values = {}
    for name in SCORING_INPUT_ARGUMENTS:
        path = tmp_path / f"{name}.pkl"
        path.write_bytes(f"source:{name}".encode("ascii"))
        paths[name] = path
        values[name] = str(path)

    qlib_root = tmp_path / "qlib_data"
    calendar = qlib_root / "calendars" / "day.txt"
    instrument = qlib_root / "instruments" / "csi300.txt"
    feature = qlib_root / "features" / "sh600000" / "close.day.bin"
    for path, payload in (
        (calendar, b"2026-07-10\n"),
        (instrument, b"SH600000\t2000-01-01\t2099-12-31\n"),
        (feature, b"close-data"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    paths.update(
        qlib_calendar=calendar,
        qlib_instrument=instrument,
        qlib_feature=feature,
    )
    values.update(
        qlib_data=str(qlib_root),
        signal_start="2020-01-01",
        signal_end="2026-05-15",
        topn=20,
        completed_lookback=16,
    )
    return argparse.Namespace(**values), paths


SIGNATURE_SOURCE_NAMES = list(SCORING_INPUT_ARGUMENTS) + [
    "qlib_calendar",
    "qlib_instrument",
    "qlib_feature",
]


def test_week_end_dates_use_last_open_day_in_holiday_week():
    calendar = pd.to_datetime(
        ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09", "2026-10-12"]
    )
    result = week_end_dates(calendar, start="2026-09-28", end="2026-10-12")
    assert [value.strftime("%Y-%m-%d") for value in result] == [
        "2026-09-30",
        "2026-10-09",
        "2026-10-12",
    ]


def test_latest_asof_never_uses_future_snapshot():
    dates = ["2026-01-30", "2026-02-27", "2026-03-31"]
    assert latest_asof(dates, "2026-03-15") == "2026-02-27"
    assert latest_asof(dates, "2026-01-01") is None


def test_signal_cache_signature_records_all_scoring_input_metadata(tmp_path):
    args, paths = make_signature_inputs(tmp_path)

    signature = build_signal_cache_signature(args)

    assert set(signature["scoring_inputs"]) == set(SCORING_INPUT_ARGUMENTS)
    for name in SCORING_INPUT_ARGUMENTS:
        fingerprint = signature["scoring_inputs"][name]
        assert fingerprint == {
            "path": str(paths[name].resolve()),
            "size": paths[name].stat().st_size,
            "mtime_ns": paths[name].stat().st_mtime_ns,
        }
    assert set(signature["qlib_data"]) == {
        "root",
        "calendars",
        "instruments",
        "features",
    }
    assert signature["scoring_schema"] == SCORING_SCHEMA_VERSION
    assert "portfolio_topn" not in signature


def test_signal_cache_signature_is_independent_of_portfolio_topn(tmp_path):
    args, _ = make_signature_inputs(tmp_path)
    args.portfolio_topn = 8
    first = build_signal_cache_signature(args)

    args.portfolio_topn = 30

    assert build_signal_cache_signature(args) == first


@pytest.mark.parametrize("source_name", SIGNATURE_SOURCE_NAMES)
def test_signal_cache_signature_invalidates_when_any_input_size_changes(tmp_path, source_name):
    args, paths = make_signature_inputs(tmp_path)
    baseline = build_signal_cache_signature(args)

    source = paths[source_name]
    source.write_bytes(source.read_bytes() + b"x")

    assert build_signal_cache_signature(args) != baseline


@pytest.mark.parametrize("source_name", SIGNATURE_SOURCE_NAMES)
def test_signal_cache_signature_invalidates_when_any_input_mtime_changes(tmp_path, source_name):
    args, paths = make_signature_inputs(tmp_path)
    baseline = build_signal_cache_signature(args)

    source = paths[source_name]
    metadata = source.stat()
    os.utime(
        source,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
    )

    assert build_signal_cache_signature(args) != baseline


def test_signal_cache_signature_invalidates_when_scoring_schema_changes(tmp_path):
    args, _ = make_signature_inputs(tmp_path)

    first = build_signal_cache_signature(
        args, scoring_schema="scores-v1"
    )
    second = build_signal_cache_signature(
        args, scoring_schema="scores-v2"
    )

    assert first != second


def test_portfolio_spec_is_frozen_validated_and_can_inherit_cli_topn():
    args = argparse.Namespace(
        portfolio_topn=None,
        topn=12,
        max_replacements=2,
        rebalance_mode="replace_only",
        account=10_000_000,
    )
    spec = PortfolioSpec.from_args(args)

    assert spec == PortfolioSpec(12, 2, "replace_only", 10_000_000)
    with pytest.raises(FrozenInstanceError):
        spec.portfolio_topn = 20
    with pytest.raises(ValueError, match="max_replacements"):
        PortfolioSpec(10, 11, "replace_only")
    with pytest.raises(ValueError, match="account"):
        PortfolioSpec(10, 2, "replace_only", 0)


def test_evaluation_periods_use_fixed_nonoverlapping_boundaries():
    index = pd.to_datetime(
        ["2017-01-01", "2021-12-31", "2022-01-01", "2024-12-31", "2025-01-01"]
    )

    masks = evaluation_period_masks(index)

    assert masks["development_2017_2021"].tolist() == [True, True, False, False, False]
    assert masks["validation_2022_2024"].tolist() == [False, False, True, True, False]
    assert masks["recent_2025_plus"].tolist() == [False, False, False, False, True]


def test_compact_period_metrics_reports_search_and_cost_drag_fields():
    index = pd.bdate_range("2024-01-02", periods=300)
    returns = pd.Series(np.where(np.arange(300) % 3, 0.001, -0.0005), index=index)
    extra_cost = pd.Series(0.0001, index=index)

    metrics = compact_period_metrics(returns, extra_cost)

    assert set(metrics) == {
        "n",
        "annualized_return",
        "sharpe",
        "calmar",
        "max_drawdown",
        "rolling_252d_sharpe_p10",
        "rolling_252d_return_p10",
        "worst_60d",
        "double_cost_annualized_return",
        "annualized_cost_drag",
    }
    assert metrics["n"] == 300
    assert metrics["annualized_cost_drag"] > 0


def test_price_ratio_updates_ep_bp_and_market_value():
    ep, bp, market_value = adjust_fundamentals_by_price(10, 2, 1000, 10, 20)
    assert ep == 0.05
    assert bp == 0.25
    assert market_value == 2000


def test_signal_price_gate_excludes_missing_and_nonpositive_closes():
    assert is_valid_signal_price(12.5)
    assert not is_valid_signal_price(np.nan)
    assert not is_valid_signal_price(np.inf)
    assert not is_valid_signal_price(0)
    assert not is_valid_signal_price(-0.01)


def test_growth_uses_latest_visible_report_period_not_source_row_order():
    frame = pd.DataFrame(
        [
            {"ann_date": "20260429", "end_date": "20251231", "netprofit_yoy": -52.0},
            {"ann_date": "20260429", "end_date": "20260331", "netprofit_yoy": 35.0},
            {"ann_date": "20260510", "end_date": "20260331", "netprofit_yoy": 40.0},
            {"ann_date": "20260830", "end_date": "20260630", "netprofit_yoy": 50.0},
            {"ann_date": "20260429", "end_date": "20260331", "netprofit_yoy": 35.0},
        ]
    ).sample(frac=1.0, random_state=7)

    records, audit = build_growth_records({"000001.SZ": frame})
    dates, values = records["000001.SZ"]

    assert dates == ["20260429", "20260510", "20260830"]
    assert values.tolist() == [35.0, 40.0, 50.0]
    assert audit["exact_duplicate_rows"] == 1
    assert audit["same_announcement_multi_period_groups"] == 1

    builder = object.__new__(WeeklySignalBuilder)
    builder.growth_records = records
    assert np.isnan(builder._growth_asof("000001.SZ", pd.Timestamp("2026-04-28")))
    assert builder._growth_asof("000001.SZ", pd.Timestamp("2026-04-29")) == 35.0
    assert builder._growth_asof("000001.SZ", pd.Timestamp("2026-08-29")) == 40.0


def test_growth_excludes_unordered_conflicting_duplicate_without_revision_time():
    frame = pd.DataFrame(
        [
            {"ann_date": "20260301", "end_date": "20251231", "netprofit_yoy": 10.0},
            {"ann_date": "20260401", "end_date": "20260331", "netprofit_yoy": 20.0},
            {"ann_date": "20260401", "end_date": "20260331", "netprofit_yoy": 30.0},
        ]
    )

    records, audit = build_growth_records({"000001.SZ": frame})
    dates, values = records["000001.SZ"]

    assert dates == ["20260301"]
    assert values.tolist() == [10.0]
    assert audit["conflicting_duplicate_keys"] == 1
    assert audit["conflicting_duplicate_rows"] == 2


def test_regime_ignores_a_leg_that_has_not_completed():
    history = [
        {
            "completion_date": "2026-03-10",
            "base_m_return": -1.0,
            "base_v_return": 1.0,
            "value_spread": 0.1,
        },
        {
            "completion_date": "2026-05-10",
            "base_m_return": -100.0,
            "base_v_return": 100.0,
            "value_spread": 0.2,
        },
    ]
    regime, audit = choose_regime(
        history,
        signal_date="2026-04-01",
        trend=1.0,
        value_spread=np.nan,
        completed_lookback=1,
    )
    assert regime == "M"
    assert audit["completed_legs_available"] == 1


def test_score_ranking_is_complete_and_uses_code_as_tie_breaker():
    scores = pd.Series(
        [1.0, 2.0, 2.0, np.nan],
        index=["SZ000002", "SZ000001", "SH600001", "SH600002"],
    )

    assert rank_codes_by_score(scores) == ["SH600001", "SZ000001", "SZ000002"]


def test_capped_topk_dropout_replaces_only_two_lowest_holdings():
    current = [f"SH{600000 + index:06d}" for index in range(10)]
    newcomers = [f"SZ{1 + index:06d}" for index in range(3)]
    ranking = newcomers + current

    result = capped_topk_dropout(
        current, ranking, topn=10, max_replacements=2
    )

    assert result == newcomers[:2] + current[:8]
    assert set(current) - set(result) == set(current[-2:])


def test_capped_topk_dropout_converges_without_forcing_two_replacements():
    original = [f"SH{600000 + index:06d}" for index in range(10)]
    newcomers = [f"SZ{1 + index:06d}" for index in range(3)]
    ranking = newcomers + original
    first = capped_topk_dropout(
        original, ranking, topn=10, max_replacements=2
    )

    second = capped_topk_dropout(
        first, ranking, topn=10, max_replacements=2
    )

    assert second == newcomers + original[:7]
    assert len(set(first) - set(second)) == 1
    assert capped_topk_dropout(
        second, ranking, topn=10, max_replacements=2
    ) == second


def test_capped_topk_dropout_treats_unranked_holdings_as_lowest():
    ranking = [f"SH{600000 + index:06d}" for index in range(12)]
    current = ranking[:8] + ["SZ000099", "SZ000098"]

    result = capped_topk_dropout(
        current, ranking, topn=10, max_replacements=2
    )

    assert result == ranking[:10]


def test_build_targets_uses_weekly_entries_and_final_clear():
    records = [
        {"entry_date": "2026-01-05", "codes": ["SH600000", "SZ000001"], "ranked_codes": ["SH600000", "SZ000001"]},
        {"entry_date": "2026-01-12", "codes": ["SH600001", "SZ000002"], "ranked_codes": ["SH600001", "SZ000002"]},
    ]
    targets, selected = build_targets(
        records, frequency_days=5, topn=2, final_clear_date="2026-01-19"
    )
    assert len(selected) == 2
    assert targets["2026-01-05"] == {"SH600000": 0.5, "SZ000001": 0.5}
    assert targets["2026-01-12"] == {"SH600001": 0.5, "SZ000002": 0.5}
    assert targets["2026-01-19"] == {}


def test_build_targets_full_replacement_uses_complete_ranking_not_cached_codes():
    records = [
        {
            "entry_date": "2026-01-05",
            "codes": ["SH600099"],
            "ranked_codes": ["SH600001", "SH600002", "SH600003"],
        }
    ]

    targets, selected = build_targets(
        records,
        frequency_days=5,
        topn=3,
        final_clear_date="2026-01-12",
    )

    assert targets["2026-01-05"] == {
        "SH600001": pytest.approx(1 / 3),
        "SH600002": pytest.approx(1 / 3),
        "SH600003": pytest.approx(1 / 3),
    }
    assert selected[0]["executed_codes"] == ["SH600001", "SH600002", "SH600003"]


def test_build_targets_applies_frequency_offset_to_start_week():
    records = [
        {
            "entry_date": f"2026-01-{day:02d}",
            "codes": [f"SH60000{index}"],
            "ranked_codes": [f"SH60000{index}"],
        }
        for index, day in enumerate((5, 12, 19, 26))
    ]

    targets, selected = build_targets(
        records,
        frequency_days=10,
        frequency_offset=1,
        topn=1,
        final_clear_date="2026-02-02",
    )

    assert [record["entry_date"] for record in selected] == [
        "2026-01-12",
        "2026-01-26",
    ]
    assert set(targets) == {"2026-01-12", "2026-01-26", "2026-02-02"}
    assert all("executed_codes" not in record for record in records)


def test_build_targets_rejects_offset_outside_frequency_step():
    records = [
        {
            "entry_date": "2026-01-05",
            "codes": ["SH600000"],
            "ranked_codes": ["SH600000"],
        }
    ]
    with pytest.raises(ValueError, match="frequency_offset"):
        build_targets(
            records,
            frequency_days=5,
            frequency_offset=1,
            topn=1,
            final_clear_date="2026-01-12",
        )


def test_build_targets_caps_each_week_at_two_replacements_without_lookahead():
    original = [f"SH{600000 + index:06d}" for index in range(10)]
    newcomers = [f"SZ{1 + index:06d}" for index in range(4)]
    records = [
        {
            "entry_date": "2026-01-05",
            "codes": original,
            "ranked_codes": original + newcomers,
        },
        {
            "entry_date": "2026-01-12",
            "codes": newcomers + original[:6],
            "ranked_codes": newcomers + original,
        },
        {
            "entry_date": "2026-01-19",
            "codes": list(reversed(original)),
            "ranked_codes": list(reversed(original)) + newcomers,
        },
    ]

    targets, selected = build_targets(
        records,
        frequency_days=5,
        topn=10,
        max_replacements=2,
        final_clear_date="2026-01-26",
    )
    baskets = [set(targets[record["entry_date"]]) for record in selected]

    assert baskets[0] == set(original)
    assert baskets[1] == set(newcomers[:2] + original[:8])
    assert all(
        len(before - after) <= 2 and len(after - before) <= 2
        for before, after in zip(baskets, baskets[1:])
    )
    assert targets["2026-01-26"] == {}


def test_build_targets_rejects_replacement_cap_with_rank_buffer():
    records = [
        {
            "entry_date": "2026-01-05",
            "codes": ["SH600000"],
            "ranked_codes": ["SH600000"],
        }
    ]
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_targets(
            records,
            frequency_days=5,
            topn=1,
            rank_buffer=2,
            max_replacements=1,
            final_clear_date="2026-01-12",
        )


def test_detailed_metrics_includes_loss_from_initial_nav():
    returns = pd.Series(
        [-0.1, 0.05], index=pd.to_datetime(["2026-01-02", "2026-01-05"])
    )
    metrics = detailed_metrics(returns)
    assert metrics["max_drawdown"] == -0.1
    assert metrics["max_drawdown_duration_days"] >= 1


def test_first_flat_position_date_waits_for_actual_liquidation():
    positions = {
        pd.Timestamp("2026-05-18"): FakePosition({"SH600000": 100}),
        pd.Timestamp("2026-05-19"): FakePosition({"SH600000": 20}),
        pd.Timestamp("2026-05-20"): FakePosition({}),
        pd.Timestamp("2026-05-21"): FakePosition({}),
    }

    assert first_flat_position_date(positions, on_or_after="2026-05-18") == pd.Timestamp(
        "2026-05-20"
    )


def test_marked_residual_holdings_discloses_stale_quote_basis():
    position = FakePosition({"SH601989": 100}, {"SH601989": 5.1})
    quotes = pd.DataFrame(
        {"$close": [5.0, 5.1]},
        index=pd.MultiIndex.from_tuples(
            [
                ("SH601989", pd.Timestamp("2025-08-11")),
                ("SH601989", pd.Timestamp("2025-08-12")),
            ],
            names=["instrument", "datetime"],
        ),
    )

    residual = marked_residual_holdings(position, quotes)["SH601989"]
    assert residual == {
        "amount": 100.0,
        "mark_price": 5.1,
        "market_value": 510.0,
        "last_quote_date": "2025-08-12",
    }
