from __future__ import annotations

import importlib.util
import pickle
import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backtest_engine" / "historical_universe.py"
REAL_CACHE = Path(r"C:\rdagent\_combo_cache_300_long.pkl")


def load_module():
    name = "quantinvest_historical_universe"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()
HistoricalUniverse = MODULE.HistoricalUniverse
UniverseCoverageError = MODULE.UniverseCoverageError
audit_membership_intervals = MODULE.audit_membership_intervals


def frame(snapshots: dict[str, list[str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": snapshot_date, "con_code": code}
            for snapshot_date, members in snapshots.items()
            for code in members
        ]
    )


def business_calendar(start: str, end: str) -> list[pd.Timestamp]:
    return list(pd.bdate_range(start, end))


def interval_dates(universe, code: str) -> list[tuple[str, str]]:
    return [
        (item.start_date.strftime("%Y%m%d"), item.end_date.strftime("%Y%m%d"))
        for item in universe.intervals_for(code)
    ]


def test_sparse_snapshots_preserve_exit_and_reentry_as_two_intervals():
    weights = frame(
        {
            "20240102": ["A", "B", "C"],
            "20240105": ["B", "C", "D"],
            "20240109": ["A", "C", "D"],
        }
    )
    original = weights.copy(deep=True)
    universe = HistoricalUniverse.from_index_weight(
        weights,
        trading_calendar=business_calendar("2024-01-02", "2024-01-10"),
        mode="snapshot",
        expected_snapshot_size=3,
        as_of="20240110",
    )

    assert universe.members_on("20240103") == frozenset({"A", "B", "C"})
    assert universe.members_on("20240105") == frozenset({"B", "C", "D"})
    assert universe.members_on("20240108") == frozenset({"B", "C", "D"})
    assert universe.members_on("20240109") == frozenset({"A", "C", "D"})
    assert interval_dates(universe, "A") == [
        ("20240102", "20240104"),
        ("20240109", "20240110"),
    ]
    report = universe.audit_report()
    assert report["multi_interval_codes"]["A"] == 2
    assert report["swallowed_gap_count"] == 0
    pd.testing.assert_frame_equal(weights, original)


def test_daily_source_does_not_forward_fill_a_missing_trading_day():
    weights = frame(
        {
            "20240102": ["A", "B"],
            "20240103": ["A", "B"],
            "20240105": ["A", "B"],
        }
    )
    universe = HistoricalUniverse.from_index_weight(
        weights,
        trading_calendar=business_calendar("2024-01-02", "2024-01-05"),
        mode="daily",
        expected_snapshot_size=2,
        as_of="20240105",
    )

    assert interval_dates(universe, "A") == [
        ("20240102", "20240103"),
        ("20240105", "20240105"),
    ]
    with pytest.raises(UniverseCoverageError):
        universe.members_on("20240104")
    assert universe.members_on("20240104", strict=False) == frozenset()
    report = universe.audit_report()
    assert report["coverage_gaps"] == [
        {
            "start_date": "20240104",
            "end_date": "20240104",
            "trading_days": 1,
            "reason": "missing_daily_snapshot",
        }
    ]


def test_incomplete_snapshot_is_an_explicit_coverage_barrier():
    weights = frame(
        {
            "20240102": ["A", "B", "C"],
            "20240105": ["A", "B"],
            "20240109": ["A", "C", "D"],
        }
    )
    universe = HistoricalUniverse.from_index_weight(
        weights,
        trading_calendar=business_calendar("2024-01-02", "2024-01-10"),
        mode="snapshot",
        expected_snapshot_size=3,
        as_of="20240110",
    )

    assert universe.members_on("20240104") == frozenset({"A", "B", "C"})
    with pytest.raises(UniverseCoverageError):
        universe.members_on("20240105")
    with pytest.raises(UniverseCoverageError):
        universe.members_on("20240108")
    assert universe.members_on("20240109") == frozenset({"A", "C", "D"})
    report = universe.audit_report()
    assert report["incomplete_snapshot_count"] == 1
    assert report["incomplete_snapshots"][0]["unique_members"] == 2
    assert report["coverage_gaps"][0]["reason"] == "invalid_snapshot_barrier"


def test_duplicate_pair_is_reported_but_deduplicated_for_membership():
    weights = pd.DataFrame(
        [
            {"trade_date": "20240102", "con_code": "A"},
            {"trade_date": "20240102", "con_code": "A"},
            {"trade_date": "20240102", "con_code": "B"},
            {"trade_date": "20240102", "con_code": "C"},
        ]
    )
    universe = HistoricalUniverse.from_index_weight(
        weights,
        trading_calendar=["20240102"],
        mode="snapshot",
        expected_snapshot_size=3,
    )

    assert universe.members_on("20240102") == frozenset({"A", "B", "C"})
    report = universe.audit_report()
    assert report["duplicate_row_count"] == 1
    assert report["duplicate_pair_count"] == 1
    assert report["snapshot_counts"][0]["rows"] == 4
    assert report["snapshot_counts"][0]["unique_members"] == 3


def test_future_snapshot_is_excluded_and_reported_against_as_of():
    weights = frame(
        {
            "20240102": ["A", "B", "C"],
            "20240109": ["B", "C", "D"],
            "20240112": ["A", "C", "D"],
        }
    )
    universe = HistoricalUniverse.from_index_weight(
        weights,
        trading_calendar=business_calendar("2024-01-02", "2024-01-12"),
        mode="snapshot",
        expected_snapshot_size=3,
        as_of="20240110",
    )

    assert universe.members_on("20240110") == frozenset({"B", "C", "D"})
    assert max(universe.intervals["end_date"]) <= pd.Timestamp("2024-01-10")
    report = universe.audit_report()
    assert report["future_snapshot_dates"] == ["20240112"]
    assert report["future_snapshot_count"] == 1
    assert report["future_interval_count"] == 0


def test_interval_audit_detects_min_max_gap_swallow_and_future_end():
    weights = frame(
        {
            "20240102": ["A", "B", "C"],
            "20240105": ["B", "C", "D"],
            "20240109": ["A", "C", "D"],
        }
    )
    bad_intervals = pd.DataFrame(
        [
            {
                "con_code": "A",
                "start_date": "20240102",
                "end_date": "20240112",
            }
        ]
    )

    report = audit_membership_intervals(
        bad_intervals,
        weights,
        as_of="20240110",
        expected_snapshot_size=3,
    )
    assert report["future_interval_count"] == 1
    assert report["swallowed_gap_count"] == 1
    assert report["swallowed_gap_violations"][0] == {
        "con_code": "A",
        "interval_start": "20240102",
        "interval_end": "20240112",
        "absent_snapshot": "20240105",
    }
    assert report["ok"] is False


def test_auto_mode_distinguishes_dense_daily_data_from_sparse_snapshots():
    calendar = business_calendar("2024-01-02", "2024-01-31")
    daily = frame(
        {value.strftime("%Y%m%d"): ["A", "B"] for value in calendar}
    )
    sparse = frame(
        {
            "20240102": ["A", "B"],
            "20240116": ["A", "C"],
            "20240131": ["B", "C"],
        }
    )

    assert HistoricalUniverse.from_index_weight(
        daily,
        trading_calendar=calendar,
        mode="auto",
        expected_snapshot_size=2,
    ).mode == "daily"
    assert HistoricalUniverse.from_index_weight(
        sparse,
        trading_calendar=calendar,
        mode="auto",
        expected_snapshot_size=2,
    ).mode == "snapshot"


def test_invalid_rows_are_reported_with_custom_column_names():
    weights = pd.DataFrame(
        [
            {"date value": "20240102", "member code": "A"},
            {"date value": "not-a-date", "member code": "B"},
            {"date value": "20240102", "member code": ""},
        ]
    )
    universe = HistoricalUniverse.from_index_weight(
        weights,
        mode="auto",
        expected_snapshot_size=1,
        date_col="date value",
        code_col="member code",
    )

    assert universe.mode == "snapshot"
    assert universe.members_on("20240102") == frozenset({"A"})
    report = universe.audit_report()
    assert report["invalid_source_row_count"] == 2
    assert {row["source_row"] for row in report["invalid_source_rows"]} == {"1", "2"}


@pytest.mark.skipif(not REAL_CACHE.is_file(), reason="local CSI300 cache is unavailable")
def test_real_csi300_cache_read_only_audit():
    before = REAL_CACHE.stat()
    with REAL_CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    weights = payload["iw"]
    universe = HistoricalUniverse.from_index_weight(
        weights,
        mode="snapshot",
        expected_snapshot_size=300,
        as_of=str(weights["trade_date"].max()),
    )
    report = universe.audit_report()
    after = REAL_CACHE.stat()

    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert report["source_rows"] == len(weights)
    assert report["snapshot_count"] == weights["trade_date"].nunique()
    assert report["duplicate_row_count"] == int(
        weights.duplicated(["trade_date", "con_code"]).sum()
    )
    assert report["swallowed_gap_count"] == 0
    print(
        "real CSI300 audit",
        {
            key: report[key]
            for key in (
                "snapshot_count",
                "incomplete_snapshot_count",
                "duplicate_row_count",
                "interval_count",
                "multi_interval_code_count",
                "coverage_gap_count",
                "swallowed_gap_count",
            )
        },
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
