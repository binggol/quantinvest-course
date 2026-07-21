from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))

from prediction_preflight import (  # noqa: E402
    PredictionPreflightError,
    atomic_write_text,
    audit_from_environment,
    validate_prediction_frame,
    validate_universe_snapshot,
)


def _write_qlib_snapshot(
    root: Path,
    universe: str,
    count: int,
    *,
    market_date: dt.date = dt.date(2026, 7, 17),
) -> list[str]:
    calendar = root / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(
        f"{market_date - dt.timedelta(days=1)}\n{market_date}\n", encoding="utf-8"
    )
    codes = [f"sh{index:06d}" for index in range(count)]
    instruments = root / "instruments" / f"{universe}.txt"
    instruments.parent.mkdir(parents=True)
    instruments.write_text(
        "".join(f"{code}\t2020-01-01\t{market_date}\n" for code in codes),
        encoding="utf-8",
    )
    return codes


@pytest.mark.parametrize(
    ("universe", "expected"),
    [("csi300", 300), ("csi500", 500), ("csi1000", 1000)],
)
def test_exact_pit_membership_is_accepted(tmp_path: Path, universe: str, expected: int):
    codes = _write_qlib_snapshot(tmp_path, universe, expected)

    audit = validate_universe_snapshot(
        tmp_path,
        universe,
        now=dt.date(2026, 7, 19),
        expected_market_date="2026-07-17",
    )

    assert audit.expected_count == expected
    assert audit.market_date == dt.date(2026, 7, 17)
    assert audit.active_codes == frozenset(codes)
    assert audit.freshness_basis == "explicit_expected_market_date+wall_clock_guard"


def test_duplicate_or_partial_pit_membership_fails_closed(tmp_path: Path):
    codes = _write_qlib_snapshot(tmp_path, "csi300", 299)
    instrument = tmp_path / "instruments" / "csi300.txt"
    with instrument.open("a", encoding="utf-8") as handle:
        handle.write(f"{codes[0]}\t2020-01-01\t2026-07-17\n")

    with pytest.raises(PredictionPreflightError, match="rows=300, unique=299, expected=300"):
        validate_universe_snapshot(
            tmp_path, "csi300", now=dt.date(2026, 7, 19)
        )


def test_stale_or_unsynchronised_calendar_fails_closed(tmp_path: Path):
    _write_qlib_snapshot(
        tmp_path, "csi300", 300, market_date=dt.date(2026, 7, 15)
    )

    with pytest.raises(PredictionPreflightError, match="calendar is stale"):
        validate_universe_snapshot(
            tmp_path, "csi300", now=dt.date(2026, 8, 2)
        )

    with pytest.raises(PredictionPreflightError, match="explicit expected date"):
        validate_universe_snapshot(
            tmp_path,
            "csi300",
            now=dt.date(2026, 7, 16),
            expected_market_date="2026-07-16",
        )


def test_long_exchange_holiday_does_not_trip_wall_clock_guard(tmp_path: Path):
    _write_qlib_snapshot(
        tmp_path, "csi300", 300, market_date=dt.date(2026, 2, 13)
    )

    audit = validate_universe_snapshot(
        tmp_path,
        "csi300",
        now=dt.date(2026, 2, 23),
        expected_market_date="2026-02-13",
    )

    assert audit.calendar_age_days == 10
    assert audit.freshness_basis == "explicit_expected_market_date+wall_clock_guard"


def test_direct_predictor_uses_latest_market_parquet_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    qlib_root = tmp_path / "qlib"
    market_root = tmp_path / "tushare_daily"
    today = dt.date.today()
    _write_qlib_snapshot(qlib_root, "csi300", 300, market_date=today)
    market_root.mkdir()
    (market_root / f"{today - dt.timedelta(days=1):%Y%m%d}.parquet").write_bytes(b"old")
    (market_root / f"{today:%Y%m%d}.parquet").write_bytes(b"new")
    monkeypatch.setenv("RDAGENT_MARKET_DATA_ROOT", str(market_root))
    monkeypatch.setenv("RDAGENT_MAX_CALENDAR_AGE_DAYS", "30")
    monkeypatch.delenv("RDAGENT_EXPECTED_MARKET_DATE", raising=False)

    audit = audit_from_environment(qlib_root, "csi300")

    assert audit.market_date == today
    assert audit.freshness_basis == "latest_market_parquet+wall_clock_guard"


def test_prediction_must_cover_active_pit_universe(tmp_path: Path):
    codes = _write_qlib_snapshot(tmp_path, "csi300", 300)
    audit = validate_universe_snapshot(
        tmp_path, "csi300", now=dt.date(2026, 7, 19)
    )
    index = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-07-17")], codes[:270]],
        names=["datetime", "instrument"],
    )
    pred = pd.DataFrame({"score": range(270)}, index=index)

    today = validate_prediction_frame(pred, audit, min_coverage=0.90)

    assert len(today) == 270
    assert today.attrs["coverage_audit"].coverage == pytest.approx(0.9)


def test_thin_wrong_date_or_outside_prediction_fails_closed(tmp_path: Path):
    codes = _write_qlib_snapshot(tmp_path, "csi300", 300)
    audit = validate_universe_snapshot(
        tmp_path, "csi300", now=dt.date(2026, 7, 19)
    )

    thin_index = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-07-17")], codes[:269]],
        names=["datetime", "instrument"],
    )
    with pytest.raises(PredictionPreflightError, match="coverage is insufficient"):
        validate_prediction_frame(
            pd.DataFrame({"score": range(269)}, index=thin_index), audit
        )

    old_index = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-07-16")], codes],
        names=["datetime", "instrument"],
    )
    with pytest.raises(PredictionPreflightError, match="prediction date"):
        validate_prediction_frame(
            pd.DataFrame({"score": range(300)}, index=old_index), audit
        )

    outside_codes = [*codes[:-1], "sz999999"]
    outside_index = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-07-17")], outside_codes],
        names=["datetime", "instrument"],
    )
    with pytest.raises(PredictionPreflightError, match="outside csi300"):
        validate_prediction_frame(
            pd.DataFrame({"score": range(300)}, index=outside_index), audit
        )


def test_atomic_writer_preserves_previous_artifact_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "published.json"
    target.write_text("old", encoding="utf-8")

    def _fail_replace(_source, _destination):
        raise OSError("simulated publish failure")

    monkeypatch.setattr("prediction_preflight.os.replace", _fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))
