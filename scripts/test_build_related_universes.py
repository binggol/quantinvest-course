from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).parent / "rdagent_backup" / "build_universe.py"
SPEC = importlib.util.spec_from_file_location("rdagent_build_universe", MODULE_PATH)
assert SPEC and SPEC.loader
build_universe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_universe
SPEC.loader.exec_module(build_universe)


def _snapshot(date: str, members: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": date, "con_code": members})


def _members(size: int, *, offset: int = 0) -> list[str]:
    return [f"{value:06d}.SZ" for value in range(offset, offset + size)]


@pytest.mark.parametrize("name,expected", [("csi500", 500), ("csi1000", 1000)])
def test_related_universe_specs_have_exact_constituent_counts(name, expected):
    spec = build_universe.get_spec(name)
    assert spec.size == expected
    assert spec.index_code in {"000905.SH", "000852.SH"}
    assert spec.history_start_deadline == {
        "csi500": pd.Timestamp("2011-01-31"),
        "csi1000": pd.Timestamp("2015-07-31"),
    }[name]
    assert spec.min_month_coverage_ratio == pytest.approx(0.90)
    assert spec.max_snapshot_gap_days == 185


@pytest.mark.parametrize("expected_size", [500, 1000])
def test_every_trading_day_has_exact_size_and_reentry_gap_is_preserved(expected_size):
    initial = _members(expected_size)
    second = initial[1:] + ["600000.SH"]
    third = [initial[0]] + second[1:]
    raw = pd.concat(
        [
            _snapshot("20260105", initial),
            _snapshot("20260108", second),
            _snapshot("20260112", third),
        ],
        ignore_index=True,
    )
    calendar = pd.bdate_range("2026-01-05", "2026-01-16")

    periods = build_universe.build_membership_periods(
        raw,
        calendar,
        end_date=pd.Timestamp("2026-01-16"),
        expected_size=expected_size,
        universe_name=f"test{expected_size}",
    )

    first_code = build_universe.ts_code_to_qlib(initial[0])
    first_periods = periods[periods["code"] == first_code].reset_index(drop=True)
    assert len(first_periods) == 2
    assert list(first_periods["start"].dt.strftime("%Y-%m-%d")) == [
        "2026-01-05",
        "2026-01-12",
    ]
    assert list(first_periods["end"].dt.strftime("%Y-%m-%d")) == [
        "2026-01-07",
        "2026-01-16",
    ]
    assert first_code not in build_universe.active_members(periods, pd.Timestamp("2026-01-08"))
    for date in calendar:
        assert len(build_universe.active_members(periods, date)) == expected_size


def test_returned_snapshot_date_is_used_instead_of_month_boundary():
    raw = pd.concat(
        [
            _snapshot("20260107", ["000001.SZ", "000002.SZ"]),
            _snapshot("20260209", ["000002.SZ", "000003.SZ"]),
        ],
        ignore_index=True,
    )
    calendar = pd.bdate_range("2026-01-01", "2026-02-13")

    periods = build_universe.build_membership_periods(
        raw,
        calendar,
        end_date=pd.Timestamp("2026-02-13"),
        expected_size=2,
        universe_name="tiny",
    )

    added = periods[periods["code"] == "sz000003"].iloc[0]
    removed = periods[periods["code"] == "sz000001"].iloc[0]
    assert added["start"] == pd.Timestamp("2026-02-09")
    assert removed["end"] == pd.Timestamp("2026-02-06")


class _TruncatingClient:
    def __init__(self, rows: pd.DataFrame, limit: int):
        self.rows = build_universe.prepare_rows(rows)
        self.limit = limit
        self.calls: list[tuple[str, str]] = []

    def index_weight(self, **kwargs):
        start = pd.Timestamp(kwargs["start_date"])
        end = pd.Timestamp(kwargs["end_date"])
        self.calls.append((kwargs["start_date"], kwargs["end_date"]))
        selected = self.rows[
            (self.rows["trade_date"] >= start) & (self.rows["trade_date"] <= end)
        ]
        return selected.head(self.limit).copy()


def test_saturated_response_is_recursively_split_without_losing_snapshots():
    raw = pd.concat(
        [
            _snapshot("20260105", ["000001.SZ", "000002.SZ"]),
            _snapshot("20260108", ["000002.SZ", "000003.SZ"]),
            _snapshot("20260112", ["000003.SZ", "000004.SZ"]),
        ],
        ignore_index=True,
    )
    client = _TruncatingClient(raw, limit=4)
    spec = build_universe.UniverseSpec(
        "tiny",
        "000000.SH",
        2,
        history_start_deadline=pd.Timestamp("2026-01-01"),
        full_fetch_start=pd.Timestamp("2026-01-01"),
    )

    fetched = build_universe.fetch_range(
        client,
        spec,
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-31"),
        api_row_guard=4,
        attempts=1,
        pause_seconds=0,
    )

    assert len(client.calls) > 1
    assert len(fetched) == 6
    assert list(fetched.groupby("trade_date")["con_code"].nunique()) == [2, 2, 2]


def test_incomplete_exact_day_response_fails_instead_of_holding_previous_state():
    client = _TruncatingClient(
        _snapshot("20260105", ["000001.SZ", "000002.SZ"]),
        limit=2,
    )
    spec = build_universe.UniverseSpec(
        "tiny",
        "000000.SH",
        3,
        history_start_deadline=pd.Timestamp("2026-01-01"),
        full_fetch_start=pd.Timestamp("2026-01-01"),
    )

    with pytest.raises(build_universe.IncompleteSnapshotError, match="exact snapshot date"):
        build_universe.fetch_range(
            client,
            spec,
            pd.Timestamp("2026-01-05"),
            pd.Timestamp("2026-01-05"),
            attempts=1,
            pause_seconds=0,
        )


def test_fetch_pages_follow_calendar_months_and_keep_exact_ranges():
    pages = list(
        build_universe.iter_month_windows(
            pd.Timestamp("2026-01-15"),
            pd.Timestamp("2026-03-03"),
        )
    )
    assert pages == [
        (pd.Timestamp("2026-01-15"), pd.Timestamp("2026-01-31")),
        (pd.Timestamp("2026-02-01"), pd.Timestamp("2026-02-28")),
        (pd.Timestamp("2026-03-01"), pd.Timestamp("2026-03-03")),
    ]


def test_new_fetch_replaces_an_entire_cached_snapshot_date():
    baseline = _snapshot("20260105", ["000001.SZ", "000002.SZ"])
    corrected = _snapshot("20260105", ["000002.SZ", "000003.SZ"])

    merged = build_universe.merge_snapshot_sources(baseline, corrected)

    assert set(merged["con_code"]) == {"000002.SZ", "000003.SZ"}
    assert len(merged) == 2


def test_atomic_write_keeps_old_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "csi500.txt"
    target.write_text("old\n", encoding="utf-8")

    monkeypatch.setattr(build_universe.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(OSError, match="Failed to publish"):
        build_universe.atomic_write(target, "new\n", attempts=2)

    assert target.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".csi500.txt.*.tmp"))


def test_nas_failure_does_not_damage_successful_local_publish(tmp_path, monkeypatch):
    local_data = tmp_path / "local"
    nas_data = tmp_path / "nas"
    cache_root = tmp_path / "cache"
    (local_data / "calendars").mkdir(parents=True)
    calendar = pd.bdate_range("2026-01-05", "2026-01-16")
    (local_data / "calendars/day.txt").write_text(
        "\n".join(date.strftime("%Y-%m-%d") for date in calendar) + "\n",
        encoding="utf-8",
    )
    cache_root.mkdir(parents=True)
    snapshots = pd.concat(
        [
            _snapshot("20260105", ["000001.SZ", "000002.SZ", "000003.SZ"]),
            _snapshot("20260112", ["000002.SZ", "000003.SZ", "000004.SZ"]),
        ],
        ignore_index=True,
    )
    snapshots.to_csv(cache_root / "csi500_weight_snapshots.csv", index=False)
    output = local_data / "instruments/csi500.txt"
    output.parent.mkdir(parents=True)
    output.write_text("old-local\n", encoding="utf-8")

    tiny_spec = build_universe.UniverseSpec(
        "csi500",
        "000905.SH",
        3,
        history_start_deadline=pd.Timestamp("2026-01-05"),
        full_fetch_start=pd.Timestamp("2026-01-01"),
    )
    monkeypatch.setitem(build_universe.UNIVERSES, "csi500", tiny_spec)
    real_atomic_write = build_universe.atomic_write

    def fail_only_nas(path, text, attempts=build_universe.WRITE_ATTEMPTS):
        if Path(path) == nas_data / "instruments/csi500.txt":
            raise OSError("simulated NAS outage")
        return real_atomic_write(path, text, attempts=attempts)

    monkeypatch.setattr(build_universe, "atomic_write", fail_only_nas)
    result = build_universe.build(
        "csi500",
        local_data=local_data,
        nas_data=nas_data,
        cache_root=cache_root,
        token="",
        pause_seconds=0,
    )

    assert result.nas_published is False
    assert "old-local" not in output.read_text(encoding="utf-8")
    assert output.read_text(encoding="utf-8").count("\n") >= 3
    assert (local_data / "instruments/csi500.txt.bak").read_text(encoding="utf-8") == "old-local\n"


def test_unresolved_incomplete_cache_refuses_publication(tmp_path, monkeypatch):
    local_data = tmp_path / "local"
    cache_root = tmp_path / "cache"
    (local_data / "calendars").mkdir(parents=True)
    (local_data / "calendars/day.txt").write_text(
        "2026-01-05\n2026-01-06\n",
        encoding="utf-8",
    )
    (local_data / "instruments").mkdir(parents=True)
    output = local_data / "instruments/csi500.txt"
    output.write_text("known-good\n", encoding="utf-8")
    cache_root.mkdir(parents=True)
    _snapshot("20260105", ["000001.SZ", "000002.SZ"]).to_csv(
        cache_root / "csi500_weight_snapshots.csv",
        index=False,
    )
    tiny_spec = build_universe.UniverseSpec(
        "csi500",
        "000905.SH",
        3,
        history_start_deadline=pd.Timestamp("2026-01-05"),
        full_fetch_start=pd.Timestamp("2026-01-01"),
    )
    monkeypatch.setitem(build_universe.UNIVERSES, "csi500", tiny_spec)

    with pytest.raises(build_universe.IncompleteSnapshotError, match="refusing publication"):
        build_universe.build(
            "csi500",
            local_data=local_data,
            nas_data=None,
            cache_root=cache_root,
            token="",
            max_snapshot_age_days=None,
        )

    assert output.read_text(encoding="utf-8") == "known-good\n"


def test_full_refresh_cannot_borrow_old_cache_to_hide_recent_only_api_history(
    tmp_path,
    monkeypatch,
):
    local_data = tmp_path / "local"
    cache_root = tmp_path / "cache"
    (local_data / "calendars").mkdir(parents=True)
    calendar = pd.bdate_range("2025-01-02", "2026-03-31")
    (local_data / "calendars/day.txt").write_text(
        "\n".join(date.strftime("%Y-%m-%d") for date in calendar) + "\n",
        encoding="utf-8",
    )
    (local_data / "instruments").mkdir(parents=True)
    output = local_data / "instruments/csi500.txt"
    output.write_text("known-good-history\n", encoding="utf-8")
    cache_root.mkdir(parents=True)

    cached = pd.concat(
        [
            _snapshot("20250102", ["000001.SZ", "000002.SZ", "000003.SZ"]),
            _snapshot("20250203", ["000001.SZ", "000002.SZ", "000003.SZ"]),
        ],
        ignore_index=True,
    )
    cache_path = cache_root / "csi500_weight_snapshots.csv"
    cached.to_csv(cache_path, index=False)
    cache_before = cache_path.read_text(encoding="utf-8")

    recent_only = pd.concat(
        [
            _snapshot("20260202", ["000001.SZ", "000002.SZ", "000003.SZ"]),
            _snapshot("20260302", ["000001.SZ", "000002.SZ", "000003.SZ"]),
        ],
        ignore_index=True,
    )
    client = _TruncatingClient(recent_only, limit=100)
    spec = build_universe.UniverseSpec(
        "csi500",
        "000905.SH",
        3,
        history_start_deadline=pd.Timestamp("2025-01-31"),
        full_fetch_start=pd.Timestamp("2025-01-01"),
    )
    monkeypatch.setitem(build_universe.UNIVERSES, "csi500", spec)

    with pytest.raises(RuntimeError, match="refusing recent-only history"):
        build_universe.build(
            "csi500",
            local_data=local_data,
            nas_data=None,
            cache_root=cache_root,
            pro=client,
            full_refresh=True,
            pause_seconds=0,
        )

    assert output.read_text(encoding="utf-8") == "known-good-history\n"
    assert cache_path.read_text(encoding="utf-8") == cache_before


def test_sparse_complete_snapshots_fail_monthly_history_coverage():
    sparse = pd.concat(
        [
            _snapshot("20250102", ["000001.SZ", "000002.SZ"]),
            _snapshot("20260105", ["000001.SZ", "000002.SZ"]),
        ],
        ignore_index=True,
    )
    spec = build_universe.UniverseSpec(
        "tiny",
        "000000.SH",
        2,
        history_start_deadline=pd.Timestamp("2025-01-31"),
        full_fetch_start=pd.Timestamp("2025-01-01"),
        min_month_coverage_ratio=0.90,
        max_snapshot_gap_days=500,
    )

    with pytest.raises(RuntimeError, match="monthly snapshot coverage"):
        build_universe.validate_snapshot_coverage(sparse, spec)


def test_observed_154_day_snapshot_gap_is_allowed():
    first = pd.Timestamp("2025-01-02")
    second = first + pd.Timedelta(days=154)
    snapshots = pd.concat(
        [
            _snapshot(first.strftime("%Y%m%d"), ["000001.SZ", "000002.SZ"]),
            _snapshot(second.strftime("%Y%m%d"), ["000001.SZ", "000002.SZ"]),
        ],
        ignore_index=True,
    )
    spec = build_universe.UniverseSpec(
        "tiny",
        "000000.SH",
        2,
        history_start_deadline=first,
        full_fetch_start=first,
        # Isolate the gap boundary; monthly density has its own test above.
        min_month_coverage_ratio=0,
        max_snapshot_gap_days=185,
    )

    coverage = build_universe.validate_snapshot_coverage(snapshots, spec)

    assert coverage.max_snapshot_gap_days == 154


def test_snapshot_gap_over_185_days_is_rejected():
    first = pd.Timestamp("2025-01-02")
    second = first + pd.Timedelta(days=186)
    snapshots = pd.concat(
        [
            _snapshot(first.strftime("%Y%m%d"), ["000001.SZ", "000002.SZ"]),
            _snapshot(second.strftime("%Y%m%d"), ["000001.SZ", "000002.SZ"]),
        ],
        ignore_index=True,
    )
    spec = build_universe.UniverseSpec(
        "tiny",
        "000000.SH",
        2,
        history_start_deadline=first,
        full_fetch_start=first,
        min_month_coverage_ratio=0,
        max_snapshot_gap_days=185,
    )

    with pytest.raises(RuntimeError, match="186 days.*185-day limit"):
        build_universe.validate_snapshot_coverage(snapshots, spec)


def test_full_refresh_without_api_access_never_falls_back_to_cache(tmp_path, monkeypatch):
    local_data = tmp_path / "local"
    cache_root = tmp_path / "cache"
    (local_data / "calendars").mkdir(parents=True)
    (local_data / "calendars/day.txt").write_text("2026-01-05\n", encoding="utf-8")
    cache_root.mkdir(parents=True)
    _snapshot("20260105", ["000001.SZ", "000002.SZ"]).to_csv(
        cache_root / "csi500_weight_snapshots.csv",
        index=False,
    )
    spec = build_universe.UniverseSpec(
        "csi500",
        "000905.SH",
        2,
        history_start_deadline=pd.Timestamp("2026-01-05"),
        full_fetch_start=pd.Timestamp("2026-01-01"),
    )
    monkeypatch.setitem(build_universe.UNIVERSES, "csi500", spec)

    with pytest.raises(RuntimeError, match="requires Tushare access"):
        build_universe.build(
            "csi500",
            local_data=local_data,
            nas_data=None,
            cache_root=cache_root,
            token="",
            full_refresh=True,
        )


def test_cli_defaults_to_related_universes_and_no_nas_never_publishes_nas(
    tmp_path,
    monkeypatch,
):
    calls = []

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(build_universe, "FileLock", lambda *_, **__: _Lock())
    monkeypatch.setattr(build_universe, "build", lambda name, **kwargs: calls.append((name, kwargs)))

    local_root = tmp_path / "local"
    nas_root = tmp_path / "must-not-be-used"
    cache_root = tmp_path / "cache"
    build_universe.main(
        [
            "--local-root",
            str(local_root),
            "--nas-root",
            str(nas_root),
            "--cache-root",
            str(cache_root),
            "--no-nas",
        ]
    )

    assert [name for name, _ in calls] == ["csi500", "csi1000"]
    assert all(kwargs["local_data"] == local_root for _, kwargs in calls)
    assert all(kwargs["nas_data"] is None for _, kwargs in calls)
    assert not nas_root.exists()


def test_cli_keeps_local_and_nas_roots_separate_when_nas_is_enabled(
    tmp_path,
    monkeypatch,
):
    calls = []

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(build_universe, "FileLock", lambda *_, **__: _Lock())
    monkeypatch.setattr(build_universe, "build", lambda name, **kwargs: calls.append((name, kwargs)))
    local_root = tmp_path / "local"
    nas_root = tmp_path / "nas"
    build_universe.main(
        [
            "csi500",
            "--local-root",
            str(local_root),
            "--nas-root",
            str(nas_root),
            "--cache-root",
            str(tmp_path / "cache"),
        ]
    )

    assert len(calls) == 1
    assert calls[0][0] == "csi500"
    assert calls[0][1]["local_data"] == local_root
    assert calls[0][1]["nas_data"] == nas_root


def test_source_contains_no_embedded_tushare_token():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "TOKEN =" not in source
    assert "TUSHARE_TOKEN" in source
