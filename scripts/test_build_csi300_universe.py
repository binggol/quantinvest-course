from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).parent / "rdagent_backup" / "build_csi300.py"
SPEC = importlib.util.spec_from_file_location("rdagent_build_csi300", MODULE_PATH)
assert SPEC and SPEC.loader
build_csi300 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_csi300)


def _snapshot(date: str, members: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": date, "con_code": members})


def test_membership_periods_preserve_exit_and_reentry_gaps():
    raw = pd.concat(
        [
            _snapshot("20260105", ["000001.SZ", "000002.SZ", "600000.SH"]),
            _snapshot("20260108", ["000002.SZ", "000003.SZ", "600000.SH"]),
            _snapshot("20260112", ["000001.SZ", "000003.SZ", "600000.SH"]),
        ],
        ignore_index=True,
    )
    calendar = pd.bdate_range("2026-01-05", "2026-01-16")

    periods = build_csi300.build_membership_periods(
        raw,
        calendar,
        end_date=pd.Timestamp("2026-01-16"),
        expected_size=3,
    )

    stock = periods[periods["code"] == "sz000001"].reset_index(drop=True)
    assert len(stock) == 2
    assert list(stock["start"].dt.strftime("%Y-%m-%d")) == ["2026-01-05", "2026-01-12"]
    assert list(stock["end"].dt.strftime("%Y-%m-%d")) == ["2026-01-07", "2026-01-16"]
    assert "sz000001" not in build_csi300._active_members(periods, pd.Timestamp("2026-01-08"))

    for date in calendar:
        assert len(build_csi300._active_members(periods, date)) == 3


def test_incomplete_snapshots_are_rejected_and_previous_state_is_held():
    raw = pd.concat(
        [
            _snapshot("20260105", ["000001.SZ", "000002.SZ", "600000.SH"]),
            _snapshot("20260108", ["000001.SZ", "000002.SZ"]),
            _snapshot("20260112", ["000002.SZ", "000003.SZ", "600000.SH"]),
        ],
        ignore_index=True,
    )
    calendar = pd.bdate_range("2026-01-05", "2026-01-16")

    complete = build_csi300.complete_snapshots(raw, expected_size=3)
    assert set(complete["trade_date"].dt.strftime("%Y%m%d")) == {"20260105", "20260112"}

    periods = build_csi300.build_membership_periods(
        raw,
        calendar,
        end_date=pd.Timestamp("2026-01-16"),
        expected_size=3,
    )
    assert build_csi300._active_members(periods, pd.Timestamp("2026-01-08")) == {
        "sz000001",
        "sz000002",
        "sh600000",
    }


def test_atomic_write_preserves_old_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "csi300.txt"
    target.write_text("old\n", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(build_csi300.os, "replace", fail_replace)

    with pytest.raises(OSError, match="Failed to publish"):
        build_csi300._atomic_write(target, "new\n", attempts=2)

    assert target.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".csi300.txt.*.tmp"))


def test_atomic_write_fsyncs_and_replaces_with_unique_temp(tmp_path, monkeypatch):
    target = tmp_path / "csi300.txt"
    target.write_text("old\n", encoding="utf-8")
    fsync_calls = []
    real_fsync = os.fsync

    def record_fsync(fd):
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(build_csi300.os, "fsync", record_fsync)
    build_csi300._atomic_write(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "csi300.txt.bak").read_text(encoding="utf-8") == "old\n"
    assert fsync_calls
    assert not list(tmp_path.glob(".csi300.txt.*.tmp"))


def test_atomic_write_rejects_empty_content(tmp_path):
    with pytest.raises(ValueError, match="empty content"):
        build_csi300._atomic_write(tmp_path / "csi300.txt", "")


def test_main_serializes_the_complete_build(tmp_path, monkeypatch):
    events = []
    lock_path = tmp_path / "build.lock"

    class RecordingLock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, exc_type, exc, traceback):
            events.append("lock-exit")

    def make_lock(path, timeout):
        assert path == str(lock_path)
        assert timeout == 900
        events.append("lock-created")
        return RecordingLock()

    monkeypatch.setattr(build_csi300, "BUILD_LOCK_PATH", lock_path)
    monkeypatch.setattr(build_csi300, "FileLock", make_lock)
    monkeypatch.setattr(build_csi300, "_build", lambda: events.append("build"))

    build_csi300.main()

    assert events == ["lock-created", "lock-enter", "build", "lock-exit"]
