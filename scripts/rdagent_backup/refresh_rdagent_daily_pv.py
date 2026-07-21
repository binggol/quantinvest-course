"""Refresh RD-Agent factor source data from the local Qlib store safely."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import warnings
from pathlib import Path


ROOT = Path("C:/rdagent")
QLIB_ROOT = Path("C:/qlib_data/cn_data")
TEMPLATE_DIR = ROOT / "rdagent/scenarios/qlib/experiment/factor_data_template"
FULL_TARGET = ROOT / "git_ignore_folder/factor_implementation_source_data/daily_pv.h5"
DEBUG_TARGET = ROOT / "git_ignore_folder/factor_implementation_source_data_debug/daily_pv.h5"
STATUS_PATH = ROOT / "daily_pv_status.json"
LOCK_PATH = ROOT / ".daily_pv_refresh.lock"
FIELDS = ["$open", "$close", "$high", "$low", "$volume", "$factor"]
OHLC_FIELDS = ["$open", "$close", "$high", "$low"]
RECENT_COVERAGE_DAYS = 21
MIN_LATEST_CSI300_COVERAGE = 0.95


def _suppress_known_pandas_optional_dependency_warnings() -> None:
    """Keep optional acceleration warnings from becoming PowerShell native errors."""
    for dependency in ("numexpr", "bottleneck"):
        warnings.filterwarnings(
            "ignore",
            message=rf"Pandas requires version .* of '{dependency}'.*",
            category=UserWarning,
        )


def _calendar_dates() -> list[str]:
    path = QLIB_ROOT / "calendars/day.txt"
    dates = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    invalid_dates = [value for value in dates if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)]
    duplicate_dates = len(dates) - len(set(dates))
    monotonic = dates == sorted(dates)
    if len(dates) < 3 or invalid_dates or duplicate_dates or not monotonic:
        max_date = max(dates, default="none")
        raise RuntimeError(
            f"Invalid Qlib calendar {path}: max_date={max_date}; rows={len(dates)}; "
            f"invalid_dates={len(invalid_dates)}; duplicate_dates={duplicate_dates}; "
            f"monotonic_increasing={monotonic}"
        )
    return dates


def _instrument_universe_summary(path: Path) -> dict:
    rows = 0
    codes = set()
    end_dates = []
    invalid_lines = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if (
            len(parts) != 3
            or not parts[0]
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1])
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[2])
            or parts[1] > parts[2]
        ):
            invalid_lines.append(line_number)
            continue
        rows += 1
        codes.add(parts[0].lower())
        end_dates.append(parts[2])
    if invalid_lines or not end_dates:
        sample = ",".join(str(value) for value in invalid_lines[:5]) or "none"
        raise RuntimeError(
            f"Invalid Qlib instrument universe {path}: valid_rows={rows}; "
            f"invalid_lines={len(invalid_lines)}; invalid_line_sample={sample}"
        )
    return {
        "rows": rows,
        "stocks": len(codes),
        "max_end_date": max(end_dates),
    }


def _validate_source_alignment(latest: str) -> dict:
    path = QLIB_ROOT / "instruments/all.txt"
    summary = _instrument_universe_summary(path)
    if summary["max_end_date"] != latest:
        raise RuntimeError(
            "Qlib source metadata is stale: "
            f"calendar_max_date={latest}; instruments_all_max_end_date={summary['max_end_date']}; "
            f"instruments_all_rows={summary['rows']}; instruments_all_stocks={summary['stocks']}"
        )
    return summary


def _active_csi300(latest: str) -> set[str]:
    members = set()
    path = QLIB_ROOT / "instruments/csi300.txt"
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] <= latest <= parts[2]:
            members.add(parts[0].lower())
    if len(members) != 300:
        raise RuntimeError(f"CSI 300 membership is invalid on {latest}: {len(members)}")
    return members


def _hdf_summary(path: Path) -> dict:
    import pandas as pd

    frame = pd.read_hdf(path, key="data")
    if list(frame.columns) != FIELDS or list(frame.index.names) != ["datetime", "instrument"]:
        raise RuntimeError(f"Unexpected factor source schema in {path}")
    dates = frame.index.get_level_values("datetime")
    return {
        "rows": int(len(frame)),
        "stocks": int(frame.index.get_level_values("instrument").nunique()),
        "min_date": str(dates.min())[:10],
        "max_date": str(dates.max())[:10],
    }


def _validate_generated_frame(frame, expected_latest: str) -> dict:
    if list(frame.columns) != FIELDS or list(frame.index.names) != ["datetime", "instrument"]:
        raise RuntimeError(
            "Generated daily_pv schema is invalid: "
            f"columns={list(frame.columns)!r}; index_names={list(frame.index.names)!r}"
        )
    if frame.empty:
        raise RuntimeError(
            "Generated daily_pv validation failed: max_date=none; "
            f"expected_max_date={expected_latest}; duplicate_rows=0; monotonic_increasing=True"
        )
    dates = frame.index.get_level_values("datetime")
    actual_max = str(dates.max())[:10]
    duplicate_rows = int(frame.index.duplicated().sum())
    monotonic = bool(frame.index.is_monotonic_increasing)
    summary = {
        "max_date": actual_max,
        "duplicate_rows": duplicate_rows,
        "monotonic_increasing": monotonic,
    }
    if actual_max != expected_latest or duplicate_rows or not monotonic:
        raise RuntimeError(
            "Generated daily_pv validation failed: "
            f"max_date={actual_max}; expected_max_date={expected_latest}; "
            f"duplicate_rows={duplicate_rows}; monotonic_increasing={monotonic}"
        )
    return summary


def _validate_csi300_ohlc_coverage(
    frame,
    csi300: set[str],
    calendar: list[str],
    *,
    recent_days: int = RECENT_COVERAGE_DAYS,
    min_latest_ratio: float = MIN_LATEST_CSI300_COVERAGE,
) -> dict:
    if not 0 < min_latest_ratio <= 1:
        raise ValueError("min_latest_ratio must be in (0, 1]")
    latest = calendar[-1]
    recent_start = calendar[max(0, len(calendar) - recent_days)]
    recent = frame.loc[recent_start:latest, OHLC_FIELDS]
    complete = recent.loc[recent.notna().all(axis=1)]
    recent_codes = {
        str(code).lower()
        for code in complete.index.get_level_values("instrument")
    }
    latest_codes = {
        str(code).lower()
        for date, code in complete.index
        if str(date)[:10] == latest
    }
    expected = {str(code).lower() for code in csi300}
    recent_coverage = len(expected & recent_codes)
    latest_coverage = len(expected & latest_codes)
    minimum_latest = math.ceil(len(expected) * min_latest_ratio)
    missing_recent = sorted(expected - recent_codes)
    missing_latest = sorted(expected - latest_codes)
    if recent_coverage != len(expected) or latest_coverage < minimum_latest:
        raise RuntimeError(
            "CSI 300 non-null OHLC coverage is incomplete: "
            f"latest_date={latest}; latest_coverage={latest_coverage}/{len(expected)}; "
            f"minimum_latest_coverage={minimum_latest}; recent_window={recent_start}..{latest}; "
            f"recent_coverage={recent_coverage}/{len(expected)}; "
            f"missing_latest_sample={','.join(missing_latest[:8]) or 'none'}; "
            f"missing_recent_sample={','.join(missing_recent[:8]) or 'none'}"
        )
    return {
        "csi300_latest_ohlc_coverage": latest_coverage,
        "csi300_recent_ohlc_coverage": recent_coverage,
        "csi300_recent_start": recent_start,
    }


def _qlib_worker_options(platform_name: str | None = None) -> dict:
    platform_name = os.name if platform_name is None else platform_name
    # Qlib's multiprocessing backend uses spawn on Windows. A single explicit
    # worker avoids nondeterministic partial results and repeated child imports.
    return {"kernels": 1} if platform_name == "nt" else {}


def _write_hdf_atomic(frame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".refresh.tmp.h5")
    try:
        frame.to_hdf(temporary, key="data", mode="w")
        summary = _hdf_summary(temporary)
        if summary["rows"] != len(frame):
            raise RuntimeError(f"HDF verification row mismatch for {temporary}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".refresh.tmp")
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise RuntimeError(f"Copy verification failed: {source} -> {target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _update_test_end(safe_end: str) -> None:
    env_path = ROOT / ".env"
    content = env_path.read_text(encoding="utf-8-sig")
    backup = env_path.with_name(".env.pre_daily_pv_auto_20260713")
    if not backup.exists():
        shutil.copy2(env_path, backup)
    for key in ("QLIB_FACTOR_TEST_END", "QLIB_MODEL_TEST_END"):
        pattern = rf"(?m)^{re.escape(key)}=.*$"
        replacement = f"{key}={safe_end}"
        if re.search(pattern, content):
            content = re.sub(pattern, replacement, content)
        else:
            content = content.rstrip() + f"\n{replacement}\n"
    temporary = env_path.with_name(".env.refresh.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, env_path)


def _load_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_status(payload: dict) -> None:
    temporary = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def _acquire_lock() -> int:
    if LOCK_PATH.exists() and dt.datetime.now().timestamp() - LOCK_PATH.stat().st_mtime > 6 * 3600:
        LOCK_PATH.unlink()
    try:
        descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Another daily_pv refresh owns {LOCK_PATH}") from exc
    os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
    return descriptor


def refresh(force: bool = False) -> dict:
    _suppress_known_pandas_optional_dependency_warnings()
    calendar = _calendar_dates()
    latest = calendar[-1]
    safe_end = calendar[-3]
    universe_summary = _validate_source_alignment(latest)
    status = _load_status()
    template_full = TEMPLATE_DIR / "daily_pv_all.h5"
    template_debug = TEMPLATE_DIR / "daily_pv_debug.h5"

    targets_exist = all(path.exists() and path.stat().st_size > 1_000_000 for path in (template_full, FULL_TARGET, DEBUG_TARGET))
    current_max = status.get("max_date") if targets_exist else None
    if current_max is None and template_full.exists():
        current_max = _hdf_summary(template_full)["max_date"]

    if not force and targets_exist and current_max == latest:
        _update_test_end(safe_end)
        payload = {
            **status,
            "state": "current",
            "max_date": latest,
            "label_safe_end": safe_end,
            "checked_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write_status(payload)
        print(f"daily_pv already current through {latest}; label-safe test end {safe_end}")
        return payload

    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(QLIB_ROOT), region="cn", **_qlib_worker_options())
    instruments = D.instruments()
    print(f"Refreshing daily_pv through {latest}...", flush=True)
    full = D.features(instruments, FIELDS, freq="day").swaplevel().sort_index()
    full = full.loc["2008-12-29":].sort_index()
    full_dates = full.index.get_level_values("datetime")
    frame_summary = _validate_generated_frame(full, latest)
    if len(full) < 10_000_000 or full.index.get_level_values("instrument").nunique() < 300:
        raise RuntimeError("Generated daily_pv is unexpectedly small")

    csi300 = _active_csi300(latest)
    coverage = _validate_csi300_ohlc_coverage(full, csi300, calendar)

    debug_period = full.loc["2018-01-01":"2019-12-31"]
    sample_codes = debug_period.index.get_level_values("instrument").unique()[:100]
    debug = debug_period.loc[(slice(None), sample_codes), FIELDS].sort_index()
    if debug.index.get_level_values("instrument").nunique() != 100:
        raise RuntimeError("Generated debug daily_pv does not contain 100 instruments")
    _write_hdf_atomic(full, template_full)
    _copy_atomic(template_full, FULL_TARGET)
    _write_hdf_atomic(debug, template_debug)
    _copy_atomic(template_debug, DEBUG_TARGET)
    _update_test_end(safe_end)

    payload = {
        "state": "updated",
        "rows": int(len(full)),
        "stocks": int(full.index.get_level_values("instrument").nunique()),
        "min_date": str(full_dates.min())[:10],
        "max_date": frame_summary["max_date"],
        "label_safe_end": safe_end,
        "debug_rows": int(len(debug)),
        "instruments_all_rows": universe_summary["rows"],
        "instruments_all_stocks": universe_summary["stocks"],
        "instruments_all_max_end_date": universe_summary["max_end_date"],
        **coverage,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_status(payload)
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    descriptor = _acquire_lock()
    try:
        refresh(force=args.force)
    finally:
        os.close(descriptor)
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
