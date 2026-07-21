"""Build a point-in-time CSI 300 instrument file for Qlib.

Each index snapshot is treated as a state transition. A stock that leaves and
later returns gets multiple non-overlapping intervals; gaps are never bridged.
"""

from __future__ import annotations

import os
import pickle
import shutil
import time
import uuid
from pathlib import Path

import pandas as pd
from filelock import FileLock


INDEX_CODE = "000300.SH"
INDEX_SIZE = 300
TRAIN_START = pd.Timestamp("2010-01-01")
FULL_FETCH_START = pd.Timestamp("2009-01-01")
LOCAL_DATA = Path("C:/qlib_data/cn_data")
CALENDAR_PATH = LOCAL_DATA / "calendars/day.txt"
OUT_PATH = LOCAL_DATA / "instruments/csi300.txt"
NAS_OUT_PATH = Path("Z:/claude/qlib/data/cn_data/instruments/csi300.txt")
SNAPSHOT_CACHE = Path("C:/rdagent/csi300_weight_snapshots.csv")
COMBO_CACHE = Path("C:/rdagent/_combo_cache_300_long.pkl")
BUILD_LOCK_PATH = Path("C:/rdagent/.build_csi300.lock")
API_ROW_GUARD = 4_900
WRITE_ATTEMPTS = 3


def ts_code_to_qlib(con_code: str) -> str:
    code, exchange = con_code.upper().split(".")
    return f"{exchange.lower()}{code}"


def _read_calendar(path: Path = CALENDAR_PATH) -> pd.DatetimeIndex:
    dates = pd.to_datetime(path.read_text(encoding="utf-8").splitlines(), errors="coerce")
    dates = pd.DatetimeIndex(dates).dropna().sort_values().unique()
    if dates.empty:
        raise RuntimeError(f"Qlib calendar is empty: {path}")
    return dates


def _prepare_rows(raw: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "con_code"}
    if raw is None or raw.empty or not required.issubset(raw.columns):
        return pd.DataFrame(columns=["trade_date", "con_code"])
    rows = raw.loc[:, ["trade_date", "con_code"]].copy()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"], errors="coerce")
    rows["con_code"] = rows["con_code"].astype(str).str.upper().str.strip()
    rows = rows.dropna().drop_duplicates().sort_values(["trade_date", "con_code"])
    return rows


def complete_snapshots(raw: pd.DataFrame, expected_size: int = INDEX_SIZE) -> pd.DataFrame:
    rows = _prepare_rows(raw)
    if rows.empty:
        return rows
    counts = rows.groupby("trade_date")["con_code"].nunique()
    complete_dates = counts[counts == expected_size].index
    rejected = counts[counts != expected_size]
    if not rejected.empty:
        summary = ", ".join(f"{d:%Y-%m-%d}:{int(n)}" for d, n in rejected.items())
        print(f"[csi300] rejected incomplete snapshots: {summary}", flush=True)
    return rows[rows["trade_date"].isin(complete_dates)].copy()


def build_membership_periods(
    snapshots: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    end_date: pd.Timestamp | None = None,
    expected_size: int = INDEX_SIZE,
) -> pd.DataFrame:
    rows = complete_snapshots(snapshots, expected_size=expected_size)
    if rows.empty:
        raise RuntimeError("No complete CSI 300 snapshots are available")

    calendar = pd.DatetimeIndex(calendar).sort_values().unique()
    end_date = pd.Timestamp(end_date or calendar[-1]).normalize()
    rows = rows[rows["trade_date"] <= end_date]
    if rows.empty:
        raise RuntimeError(f"No complete snapshot exists on or before {end_date:%Y-%m-%d}")

    states = {
        pd.Timestamp(date): set(group["con_code"].map(ts_code_to_qlib))
        for date, group in rows.groupby("trade_date", sort=True)
    }
    active_start: dict[str, pd.Timestamp] = {}
    previous: set[str] = set()
    periods: list[dict[str, object]] = []

    for snapshot_date, current in states.items():
        prior_days = calendar[calendar < snapshot_date]
        if previous and prior_days.empty:
            raise RuntimeError(f"No prior Qlib trading day for snapshot {snapshot_date:%Y-%m-%d}")
        close_date = prior_days[-1] if len(prior_days) else None

        for code in sorted(previous - current):
            start = active_start.pop(code)
            if close_date is not None and start <= close_date:
                periods.append({"code": code, "start": start, "end": close_date})
        for code in sorted(current - previous):
            active_start[code] = snapshot_date
        previous = current

    for code in sorted(previous):
        periods.append({"code": code, "start": active_start[code], "end": end_date})

    result = pd.DataFrame(periods).sort_values(["code", "start", "end"]).reset_index(drop=True)
    _validate_periods(result, states, calendar, end_date, expected_size)
    return result


def _active_members(periods: pd.DataFrame, date: pd.Timestamp) -> set[str]:
    mask = (periods["start"] <= date) & (periods["end"] >= date)
    return set(periods.loc[mask, "code"])


def _validate_periods(
    periods: pd.DataFrame,
    states: dict[pd.Timestamp, set[str]],
    calendar: pd.DatetimeIndex,
    end_date: pd.Timestamp,
    expected_size: int,
) -> None:
    for snapshot_date, expected in states.items():
        if snapshot_date > end_date:
            continue
        actual = _active_members(periods, snapshot_date)
        if actual != expected:
            raise RuntimeError(
                f"Snapshot mismatch on {snapshot_date:%Y-%m-%d}: "
                f"expected {len(expected)}, got {len(actual)}"
            )

    first_snapshot = min(states)
    for date in calendar[(calendar >= first_snapshot) & (calendar <= end_date)]:
        count = len(_active_members(periods, date))
        if count != expected_size:
            raise RuntimeError(f"CSI 300 active count is {count} on {date:%Y-%m-%d}")

    for code, group in periods.groupby("code"):
        ordered = group.sort_values("start")
        previous_end = None
        for row in ordered.itertuples(index=False):
            if previous_end is not None and row.start <= previous_end:
                raise RuntimeError(f"Overlapping intervals for {code}")
            previous_end = row.end


def _load_baseline_snapshots() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if SNAPSHOT_CACHE.exists():
        try:
            frames.append(pd.read_csv(SNAPSHOT_CACHE, dtype=str))
        except Exception as exc:
            print(f"[csi300] snapshot CSV ignored: {exc}", flush=True)
    if COMBO_CACHE.exists():
        try:
            with COMBO_CACHE.open("rb") as handle:
                cached = pickle.load(handle)
            if isinstance(cached, dict) and isinstance(cached.get("iw"), pd.DataFrame):
                frames.append(cached["iw"])
        except Exception as exc:
            print(f"[csi300] combo cache ignored: {exc}", flush=True)
    if not frames:
        return pd.DataFrame(columns=["trade_date", "con_code"])
    return _prepare_rows(pd.concat(frames, ignore_index=True))


def _read_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("TUSHARE_TOKEN_FILE", "").strip()
    candidates = [Path(token_file)] if token_file else []
    candidates.append(Path("quantinvest-course/data/.tushare_token"))
    for path in candidates:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return ""


def _fetch_range(pro, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if start > end:
        return pd.DataFrame(columns=["trade_date", "con_code"])
    frame = pro.index_weight(
        index_code=INDEX_CODE,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    time.sleep(0.3)
    frame = _prepare_rows(frame)
    counts = frame.groupby("trade_date")["con_code"].nunique() if not frame.empty else pd.Series(dtype=int)
    needs_split = len(frame) >= API_ROW_GUARD or bool((counts != INDEX_SIZE).any())
    if needs_split and start < end:
        middle = start + (end - start) // 2
        left = _fetch_range(pro, start, middle)
        right = _fetch_range(pro, middle + pd.Timedelta(days=1), end)
        return _prepare_rows(pd.concat([left, right], ignore_index=True))
    return frame


def _fetch_incremental(baseline: pd.DataFrame, end_date: pd.Timestamp) -> pd.DataFrame:
    token = _read_token()
    if not token:
        print("[csi300] TUSHARE_TOKEN is unavailable; using verified local snapshots", flush=True)
        return pd.DataFrame(columns=["trade_date", "con_code"])

    import tushare as ts

    ts.set_token(token)
    pro = ts.pro_api()
    complete = complete_snapshots(baseline)
    fetch_start = FULL_FETCH_START
    if not complete.empty:
        fetch_start = complete["trade_date"].max() - pd.Timedelta(days=90)

    chunks: list[pd.DataFrame] = []
    cursor = fetch_start.normalize()
    while cursor <= end_date:
        chunk_end = min(cursor + pd.DateOffset(years=1) - pd.Timedelta(days=1), end_date)
        print(f"[csi300] fetching {cursor:%Y-%m-%d} -> {chunk_end:%Y-%m-%d}", flush=True)
        chunks.append(_fetch_range(pro, cursor, chunk_end))
        cursor = chunk_end + pd.Timedelta(days=1)
    return _prepare_rows(pd.concat(chunks, ignore_index=True)) if chunks else pd.DataFrame()


def _atomic_write(path: Path, text: str, attempts: int = WRITE_ATTEMPTS) -> None:
    if not text:
        raise ValueError(f"Refusing to publish empty content: {path}")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    expected_size = len(text.encode("utf-8"))
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.stat().st_size != expected_size:
                raise OSError(
                    f"temporary size mismatch for {path}: "
                    f"expected {expected_size}, got {temporary.stat().st_size}"
                )
            if path.exists() and path.stat().st_size > 0:
                shutil.copy2(path, backup)
            os.replace(temporary, path)
            return
        except OSError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.5 * attempt)
        finally:
            temporary.unlink(missing_ok=True)
    raise OSError(f"Failed to publish {path} after {attempts} attempts") from last_error


def _build() -> None:
    calendar = _read_calendar()
    calendar_end = pd.Timestamp(calendar[-1]).normalize()
    baseline = _load_baseline_snapshots()
    fetched = _fetch_incremental(baseline, min(pd.Timestamp.now().normalize(), calendar_end))
    combined = _prepare_rows(pd.concat([baseline, fetched], ignore_index=True))
    snapshots = complete_snapshots(combined)
    if snapshots.empty or snapshots["trade_date"].min() > TRAIN_START:
        raise RuntimeError("A complete snapshot before the training start is required")

    cache_rows = snapshots.copy()
    cache_rows["trade_date"] = cache_rows["trade_date"].dt.strftime("%Y%m%d")
    _atomic_write(SNAPSHOT_CACHE, cache_rows.to_csv(index=False))

    periods = build_membership_periods(snapshots, calendar, end_date=calendar_end)
    text = "".join(
        f"{row.code}\t{row.start:%Y-%m-%d}\t{row.end:%Y-%m-%d}\n"
        for row in periods.itertuples(index=False)
    )
    _atomic_write(OUT_PATH, text)
    try:
        _atomic_write(NAS_OUT_PATH, text)
    except Exception as exc:
        print(f"[csi300] NAS write skipped: {exc}", flush=True)

    unique_codes = periods["code"].nunique()
    multi_period = int((periods.groupby("code").size() > 1).sum())
    print(
        f"[csi300] wrote {len(periods)} intervals / {unique_codes} codes; "
        f"{multi_period} codes have multiple intervals; calendar_end={calendar_end:%Y-%m-%d}",
        flush=True,
    )

    if os.environ.get("BUILD_RELATED_UNIVERSES", "") == "1":
        try:
            from build_universe import build as build_universe

            for universe in ("csi500", "csi1000"):
                build_universe(universe)
        except Exception as exc:
            print(f"[csi300] csi500/csi1000 rebuild skipped: {exc}", flush=True)
        try:
            from build_index_bins import INDICES, dump

            for qlib_code, tushare_code in INDICES.items():
                dump(qlib_code, tushare_code)
        except Exception as exc:
            print(f"[csi300] index-bin rebuild skipped: {exc}", flush=True)


def main() -> None:
    BUILD_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"[csi300] waiting for build lock: {BUILD_LOCK_PATH}", flush=True)
    with FileLock(str(BUILD_LOCK_PATH), timeout=900):
        _build()


if __name__ == "__main__":
    main()
