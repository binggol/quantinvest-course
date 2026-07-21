import importlib.util
import warnings
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("rdagent_backup") / "refresh_rdagent_daily_pv.py"


@pytest.fixture
def refresh_module():
    spec = importlib.util.spec_from_file_location("refresh_rdagent_daily_pv_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._suppress_known_pandas_optional_dependency_warnings()
    return module


def _frame(module, rows):
    import pandas as pd

    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(date), instrument) for date, instrument, _values in rows],
        names=["datetime", "instrument"],
    )
    return pd.DataFrame(
        [values for _date, _instrument, values in rows],
        index=index,
        columns=module.FIELDS,
    )


def test_source_alignment_reports_calendar_and_universe_max_dates(
    refresh_module, monkeypatch, tmp_path
):
    qlib_root = tmp_path / "cn_data"
    instruments = qlib_root / "instruments"
    instruments.mkdir(parents=True)
    (instruments / "all.txt").write_text(
        "sh600000\t2000-01-01\t2026-07-10\n"
        "sz000001\t1991-04-03\t2026-07-10\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(refresh_module, "QLIB_ROOT", qlib_root)

    with pytest.raises(RuntimeError) as error:
        refresh_module._validate_source_alignment("2026-07-13")

    message = str(error.value)
    assert "calendar_max_date=2026-07-13" in message
    assert "instruments_all_max_end_date=2026-07-10" in message
    assert "instruments_all_rows=2" in message


def test_calendar_error_reports_duplicates_and_monotonic_state(
    refresh_module, monkeypatch, tmp_path
):
    qlib_root = tmp_path / "cn_data"
    calendars = qlib_root / "calendars"
    calendars.mkdir(parents=True)
    (calendars / "day.txt").write_text(
        "2026-07-10\n2026-07-13\n2026-07-10\n", encoding="utf-8"
    )
    monkeypatch.setattr(refresh_module, "QLIB_ROOT", qlib_root)

    with pytest.raises(RuntimeError) as error:
        refresh_module._calendar_dates()

    message = str(error.value)
    assert "duplicate_dates=1" in message
    assert "monotonic_increasing=False" in message
    assert "max_date=2026-07-13" in message


def test_generated_frame_error_reports_each_integrity_condition(refresh_module):
    frame = _frame(
        refresh_module,
        [
            ("2026-07-10", "sh600001", [1, 1, 1, 1, 1, 1]),
            ("2026-07-09", "sh600002", [1, 1, 1, 1, 1, 1]),
            ("2026-07-09", "sh600002", [1, 1, 1, 1, 1, 1]),
        ],
    )

    with pytest.raises(RuntimeError) as error:
        refresh_module._validate_generated_frame(frame, "2026-07-13")

    message = str(error.value)
    assert "max_date=2026-07-10" in message
    assert "expected_max_date=2026-07-13" in message
    assert "duplicate_rows=1" in message
    assert "monotonic_increasing=False" in message


def test_csi300_coverage_requires_non_null_ohlc_rows(refresh_module):
    frame = _frame(
        refresh_module,
        [
            ("2026-07-10", "sh600001", [1, 1, 1, 1, 10, 1]),
            ("2026-07-10", "sh600002", [2, 2, 2, 2, 10, 1]),
            ("2026-07-10", "sh600003", [None, None, None, None, 0, 1]),
            ("2026-07-13", "sh600001", [1, 1, 1, 1, 10, 1]),
            ("2026-07-13", "sh600002", [2, 2, 2, 2, 10, 1]),
            ("2026-07-13", "sh600003", [None, None, None, None, 0, 1]),
        ],
    ).sort_index()

    with pytest.raises(RuntimeError) as error:
        refresh_module._validate_csi300_ohlc_coverage(
            frame,
            {"sh600001", "sh600002", "sh600003"},
            ["2026-07-09", "2026-07-10", "2026-07-13"],
            min_latest_ratio=1.0,
        )

    message = str(error.value)
    assert "latest_coverage=2/3" in message
    assert "recent_coverage=2/3" in message
    assert "missing_latest_sample=sh600003" in message
    assert "missing_recent_sample=sh600003" in message


def test_csi300_coverage_returns_latest_and_recent_counts(refresh_module):
    frame = _frame(
        refresh_module,
        [
            ("2026-07-10", "sh600001", [1, 1, 1, 1, 10, 1]),
            ("2026-07-10", "sh600002", [2, 2, 2, 2, 10, 1]),
            ("2026-07-10", "sh600003", [3, 3, 3, 3, 10, 1]),
            ("2026-07-13", "sh600001", [1, 1, 1, 1, 10, 1]),
            ("2026-07-13", "sh600002", [2, 2, 2, 2, 10, 1]),
        ],
    ).sort_index()

    summary = refresh_module._validate_csi300_ohlc_coverage(
        frame,
        {"sh600001", "sh600002", "sh600003"},
        ["2026-07-09", "2026-07-10", "2026-07-13"],
        min_latest_ratio=2 / 3,
    )

    assert summary["csi300_latest_ohlc_coverage"] == 2
    assert summary["csi300_recent_ohlc_coverage"] == 3


def test_windows_qlib_features_use_one_explicit_worker(refresh_module):
    assert refresh_module._qlib_worker_options("nt") == {"kernels": 1}
    assert refresh_module._qlib_worker_options("posix") == {}


def test_only_known_optional_pandas_warnings_are_suppressed(refresh_module):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        refresh_module._suppress_known_pandas_optional_dependency_warnings()
        warnings.warn(
            "Pandas requires version '99' or newer of 'numexpr' (version '1' currently installed).",
            UserWarning,
        )
        warnings.warn("unrelated validation warning", UserWarning)

    assert [str(item.message) for item in caught] == ["unrelated validation warning"]
