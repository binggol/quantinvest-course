"""Build point-in-time CSI index instruments for Qlib.

Tushare ``index_weight`` is monthly snapshot data and a range response may be
truncated by the API row limit.  This builder therefore requests calendar
months independently and recursively bisects any saturated or incomplete
response.  Returned ``trade_date`` values are kept as the effective snapshot
dates; month boundaries are never substituted for them.

Each complete snapshot is a state transition.  A constituent that exits and
later re-enters receives separate, non-overlapping intervals, so its absence is
not bridged.  The complete interval set is validated on every Qlib trading day
before the local instrument file is atomically replaced.  NAS publication is a
best-effort second step and cannot roll back or corrupt the local file.

Examples::

    python build_universe.py csi500 csi1000
    python build_universe.py csi300 --no-nas
"""

from __future__ import annotations

import argparse
import os
import pickle
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import pandas as pd
from filelock import FileLock


DEFAULT_LOCAL_DATA = Path("C:/qlib_data/cn_data")
DEFAULT_NAS_DATA = Path("Z:/claude/qlib/data/cn_data")
DEFAULT_CACHE_ROOT = Path("C:/rdagent")
DEFAULT_TRAIN_START = pd.Timestamp("2010-01-01")
DEFAULT_FULL_FETCH_START = pd.Timestamp("2009-01-01")
API_ROW_LIMIT = 5_000
# Leave headroom for API-side metadata/limit changes and split before a
# nominally full 5,000-row response can be mistaken for complete data.
API_ROW_GUARD = 4_900
API_ATTEMPTS = 3
WRITE_ATTEMPTS = 3
REFETCH_OVERLAP_DAYS = 93
MAX_SNAPSHOT_AGE_DAYS = 45
BUILD_LOCK_NAME = ".build_csi300.lock"


@dataclass(frozen=True)
class UniverseSpec:
    name: str
    index_code: str
    size: int
    # Latest acceptable date for the first historical snapshot.  This is a
    # coverage deadline, not an assumed effective date: the returned Tushare
    # trade_date remains authoritative.  CSI500/CSI1000 public history starts
    # later than CSI300, hence their different deadlines.
    history_start_deadline: pd.Timestamp | None = None
    full_fetch_start: pd.Timestamp = DEFAULT_FULL_FETCH_START
    min_month_coverage_ratio: float = 0.90
    # Real maintained CSI history can contain roughly half-year transition
    # gaps (CSI300 has an observed 154-day maximum).  Use a 185-day ceiling so
    # those histories remain valid; the 90% monthly coverage gate separately
    # rejects broadly sparse data.
    max_snapshot_gap_days: int = 185


UNIVERSES: dict[str, UniverseSpec] = {
    "csi300": UniverseSpec("csi300", "000300.SH", 300, DEFAULT_TRAIN_START),
    # Allow a small boundary margin around the first snapshots available in
    # the maintained historical series while still rejecting recent-only API
    # responses during --full-refresh.
    "csi500": UniverseSpec(
        "csi500", "000905.SH", 500, pd.Timestamp("2011-01-31")
    ),
    "csi1000": UniverseSpec(
        "csi1000", "000852.SH", 1000, pd.Timestamp("2015-07-31")
    ),
}


@dataclass(frozen=True)
class BuildResult:
    universe: str
    output_path: Path
    snapshot_cache: Path
    snapshot_count: int
    interval_count: int
    unique_code_count: int
    first_snapshot: pd.Timestamp
    latest_snapshot: pd.Timestamp
    calendar_end: pd.Timestamp
    month_coverage_ratio: float
    max_snapshot_gap_days: int
    nas_published: bool


@dataclass(frozen=True)
class SnapshotCoverage:
    first_snapshot: pd.Timestamp
    latest_snapshot: pd.Timestamp
    snapshot_count: int
    covered_months: int
    expected_months: int
    month_coverage_ratio: float
    max_snapshot_gap_days: int


class IndexWeightClient(Protocol):
    def index_weight(self, **kwargs) -> pd.DataFrame: ...


class IncompleteSnapshotError(RuntimeError):
    """Raised when an exact-day response is not a complete index snapshot."""


def get_spec(name: str) -> UniverseSpec:
    key = str(name).strip().lower()
    try:
        return UNIVERSES[key]
    except KeyError as exc:
        supported = ", ".join(sorted(UNIVERSES))
        raise ValueError(f"Unsupported universe {name!r}; expected one of: {supported}") from exc


def ts_code_to_qlib(con_code: str) -> str:
    parts = str(con_code).upper().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid Tushare constituent code: {con_code!r}")
    code, exchange = parts
    return f"{exchange.lower()}{code}"


def read_calendar(path: Path) -> pd.DatetimeIndex:
    dates = pd.to_datetime(path.read_text(encoding="utf-8").splitlines(), errors="coerce")
    dates = pd.DatetimeIndex(dates).dropna().normalize().sort_values().unique()
    if dates.empty:
        raise RuntimeError(f"Qlib calendar is empty: {path}")
    return dates


def prepare_rows(raw: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["trade_date", "con_code"]
    if raw is None or raw.empty or not set(columns).issubset(raw.columns):
        return pd.DataFrame(columns=columns)
    rows = raw.loc[:, columns].copy()
    rows = rows.dropna(subset=columns)
    rows["trade_date"] = pd.to_datetime(rows["trade_date"], errors="coerce").dt.normalize()
    rows["con_code"] = rows["con_code"].astype(str).str.upper().str.strip()
    rows = rows.dropna(subset=["trade_date"])
    rows = rows[rows["con_code"].str.match(r"^\d{6}\.(?:SH|SZ|BJ)$", na=False)]
    return rows.drop_duplicates().sort_values(columns).reset_index(drop=True)


def merge_snapshot_sources(*sources: pd.DataFrame | None) -> pd.DataFrame:
    """Merge snapshot sources with whole-date replacement by later sources.

    Snapshot membership is indivisible.  Row-wise union would create 501/1001
    members when Tushare corrects one constituent on an already cached date.
    A later source therefore replaces every row for each date it contains.
    """

    merged = pd.DataFrame(columns=["trade_date", "con_code"])
    for source in sources:
        rows = prepare_rows(source)
        if rows.empty:
            continue
        replacement_dates = set(rows["trade_date"])
        if not merged.empty:
            merged = merged[~merged["trade_date"].isin(replacement_dates)]
        merged = prepare_rows(pd.concat([merged, rows], ignore_index=True))
    return merged


def _snapshot_counts(rows: pd.DataFrame) -> pd.Series:
    if rows.empty:
        return pd.Series(dtype="int64")
    return rows.groupby("trade_date")["con_code"].nunique().sort_index()


def complete_snapshots(
    raw: pd.DataFrame | None,
    expected_size: int,
    universe_name: str = "index",
    *,
    log_rejected: bool = True,
) -> pd.DataFrame:
    rows = prepare_rows(raw)
    if rows.empty:
        return rows
    counts = _snapshot_counts(rows)
    complete_dates = counts[counts == expected_size].index
    rejected = counts[counts != expected_size]
    if log_rejected and not rejected.empty:
        summary = ", ".join(f"{date:%Y-%m-%d}:{int(count)}" for date, count in rejected.items())
        print(f"[{universe_name}] rejected incomplete snapshots: {summary}", flush=True)
    return rows[rows["trade_date"].isin(complete_dates)].copy()


def validate_snapshot_coverage(
    snapshots: pd.DataFrame,
    spec: UniverseSpec,
) -> SnapshotCoverage:
    """Reject recent-only or sparse history even when each snapshot is full."""

    rows = complete_snapshots(
        snapshots,
        expected_size=spec.size,
        universe_name=spec.name,
        log_rejected=False,
    )
    dates = pd.DatetimeIndex(rows["trade_date"].drop_duplicates()).sort_values()
    if dates.empty:
        raise RuntimeError(f"No complete {spec.name} snapshots are available")

    first_snapshot = pd.Timestamp(dates[0]).normalize()
    latest_snapshot = pd.Timestamp(dates[-1]).normalize()
    if (
        spec.history_start_deadline is not None
        and first_snapshot > spec.history_start_deadline
    ):
        raise RuntimeError(
            f"{spec.name} historical coverage starts at {first_snapshot:%Y-%m-%d}, "
            f"after required deadline {spec.history_start_deadline:%Y-%m-%d}; "
            "refusing recent-only history"
        )

    covered_periods = pd.PeriodIndex(dates, freq="M").unique().sort_values()
    expected_periods = pd.period_range(
        start=first_snapshot.to_period("M"),
        end=latest_snapshot.to_period("M"),
        freq="M",
    )
    expected_months = len(expected_periods)
    covered_months = len(covered_periods)
    coverage_ratio = covered_months / expected_months
    if coverage_ratio + 1e-12 < spec.min_month_coverage_ratio:
        raise RuntimeError(
            f"{spec.name} monthly snapshot coverage is {covered_months}/{expected_months} "
            f"({coverage_ratio:.1%}), below required {spec.min_month_coverage_ratio:.1%}"
        )

    gaps = dates.to_series(index=range(len(dates))).diff().dt.days.dropna()
    max_gap = int(gaps.max()) if not gaps.empty else 0
    if max_gap > spec.max_snapshot_gap_days:
        raise RuntimeError(
            f"{spec.name} maximum snapshot gap is {max_gap} days, exceeding "
            f"the {spec.max_snapshot_gap_days}-day limit"
        )

    return SnapshotCoverage(
        first_snapshot=first_snapshot,
        latest_snapshot=latest_snapshot,
        snapshot_count=len(dates),
        covered_months=covered_months,
        expected_months=expected_months,
        month_coverage_ratio=coverage_ratio,
        max_snapshot_gap_days=max_gap,
    )


def active_members(periods: pd.DataFrame, date: pd.Timestamp) -> set[str]:
    at = pd.Timestamp(date).normalize()
    mask = (periods["start"] <= at) & (periods["end"] >= at)
    return set(periods.loc[mask, "code"])


def validate_periods(
    periods: pd.DataFrame,
    states: dict[pd.Timestamp, set[str]],
    calendar: pd.DatetimeIndex,
    end_date: pd.Timestamp,
    expected_size: int,
    universe_name: str,
) -> None:
    if periods.empty:
        raise RuntimeError(f"{universe_name} produced no membership intervals")

    for snapshot_date, expected in states.items():
        if snapshot_date > end_date:
            continue
        actual = active_members(periods, snapshot_date)
        if actual != expected:
            raise RuntimeError(
                f"{universe_name} snapshot mismatch on {snapshot_date:%Y-%m-%d}: "
                f"expected {len(expected)}, got {len(actual)}"
            )

    first_snapshot = min(states)
    validation_days = calendar[(calendar >= first_snapshot) & (calendar <= end_date)]
    if validation_days.empty:
        raise RuntimeError(
            f"{universe_name} has no Qlib trading days between its first snapshot "
            f"and {end_date:%Y-%m-%d}"
        )
    for date in validation_days:
        count = len(active_members(periods, date))
        if count != expected_size:
            raise RuntimeError(
                f"{universe_name} active count is {count}, expected {expected_size}, "
                f"on {date:%Y-%m-%d}"
            )

    for code, group in periods.groupby("code"):
        previous_end: pd.Timestamp | None = None
        for row in group.sort_values("start").itertuples(index=False):
            if row.start > row.end:
                raise RuntimeError(f"Invalid interval for {code}: {row.start} > {row.end}")
            if previous_end is not None and row.start <= previous_end:
                raise RuntimeError(f"Overlapping intervals for {code}")
            previous_end = row.end


def build_membership_periods(
    snapshots: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    end_date: pd.Timestamp | None = None,
    expected_size: int = 300,
    universe_name: str = "index",
) -> pd.DataFrame:
    rows = complete_snapshots(
        snapshots,
        expected_size=expected_size,
        universe_name=universe_name,
    )
    if rows.empty:
        raise RuntimeError(f"No complete {universe_name} snapshots are available")

    calendar = pd.DatetimeIndex(calendar).dropna().normalize().sort_values().unique()
    if calendar.empty:
        raise RuntimeError("Qlib calendar is empty")
    final_date = pd.Timestamp(end_date if end_date is not None else calendar[-1]).normalize()
    if final_date not in calendar:
        raise RuntimeError(f"End date is not in the Qlib trading calendar: {final_date:%Y-%m-%d}")

    rows = rows[rows["trade_date"] <= final_date]
    if rows.empty:
        raise RuntimeError(f"No complete {universe_name} snapshot exists on or before {final_date:%Y-%m-%d}")

    states = {
        pd.Timestamp(date).normalize(): set(group["con_code"].map(ts_code_to_qlib))
        for date, group in rows.groupby("trade_date", sort=True)
    }
    active_start: dict[str, pd.Timestamp] = {}
    previous: set[str] = set()
    periods: list[dict[str, object]] = []

    for snapshot_date, current in states.items():
        if len(current) != expected_size:
            raise RuntimeError(
                f"{universe_name} snapshot {snapshot_date:%Y-%m-%d} has "
                f"{len(current)} members, expected {expected_size}"
            )
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
        periods.append({"code": code, "start": active_start[code], "end": final_date})

    result = pd.DataFrame(periods).sort_values(["code", "start", "end"]).reset_index(drop=True)
    validate_periods(
        result,
        states,
        calendar,
        final_date,
        expected_size,
        universe_name,
    )
    return result


def _read_token(cache_root: Path = DEFAULT_CACHE_ROOT) -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    configured = os.environ.get("TUSHARE_TOKEN_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            cache_root / "data/.tushare_token",
            Path(__file__).resolve().parent.parent.parent / "data/.tushare_token",
        ]
    )
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _create_client(token: str) -> IndexWeightClient:
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is unavailable")
    import tushare as ts

    ts.set_token(token)
    return ts.pro_api()


def _call_index_weight(
    pro: IndexWeightClient,
    spec: UniverseSpec,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    attempts: int = API_ATTEMPTS,
    pause_seconds: float = 0.3,
) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            frame = pro.index_weight(
                index_code=spec.index_code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields="trade_date,con_code",
            )
            if pause_seconds > 0:
                time.sleep(pause_seconds)
            return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        except Exception as exc:  # network/API errors need bounded retry
            last_error = exc
            if attempt < attempts:
                time.sleep(max(pause_seconds, 0.1) * attempt)
    raise RuntimeError(
        f"{spec.name} index_weight failed for {start:%Y-%m-%d}..{end:%Y-%m-%d} "
        f"after {attempts} attempts"
    ) from last_error


def fetch_range(
    pro: IndexWeightClient,
    spec: UniverseSpec,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    api_row_guard: int = API_ROW_GUARD,
    attempts: int = API_ATTEMPTS,
    pause_seconds: float = 0.3,
) -> pd.DataFrame:
    """Fetch a range and recursively bisect any saturated/incomplete result."""

    first = pd.Timestamp(start).normalize()
    last = pd.Timestamp(end).normalize()
    if first > last:
        return pd.DataFrame(columns=["trade_date", "con_code"])

    raw = _call_index_weight(
        pro,
        spec,
        first,
        last,
        attempts=attempts,
        pause_seconds=pause_seconds,
    )
    raw_count = len(raw)
    rows = prepare_rows(raw)
    counts = _snapshot_counts(rows)
    incomplete = counts[counts != spec.size]
    saturated = raw_count >= api_row_guard

    if saturated or not incomplete.empty:
        if first == last:
            count = int(counts.iloc[0]) if len(counts) else 0
            reason = "row-limit saturation" if saturated else "incomplete response"
            raise IncompleteSnapshotError(
                f"{spec.name} {reason} on exact snapshot date {first:%Y-%m-%d}: "
                f"received {count} unique members / {raw_count} rows, expected {spec.size}; "
                f"API limit is {API_ROW_LIMIT}"
            )
        middle = first + (last - first) // 2
        left = fetch_range(
            pro,
            spec,
            first,
            middle,
            api_row_guard=api_row_guard,
            attempts=attempts,
            pause_seconds=pause_seconds,
        )
        right = fetch_range(
            pro,
            spec,
            middle + pd.Timedelta(days=1),
            last,
            api_row_guard=api_row_guard,
            attempts=attempts,
            pause_seconds=pause_seconds,
        )
        merged = prepare_rows(pd.concat([left, right], ignore_index=True))
        merged_counts = _snapshot_counts(merged)
        bad = merged_counts[merged_counts != spec.size]
        if not bad.empty:
            summary = ", ".join(f"{date:%Y-%m-%d}:{int(count)}" for date, count in bad.items())
            raise IncompleteSnapshotError(f"{spec.name} incomplete snapshots after split: {summary}")
        return merged

    return rows


def iter_month_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> Iterable[tuple[pd.Timestamp, pd.Timestamp]]:
    """Yield non-overlapping calendar-month API pages, clipped to the range."""

    first = pd.Timestamp(start).normalize()
    last = pd.Timestamp(end).normalize()
    cursor = first
    while cursor <= last:
        month_end = (cursor + pd.offsets.MonthEnd(0)).normalize()
        page_end = min(month_end, last)
        yield cursor, page_end
        cursor = page_end + pd.Timedelta(days=1)


def fetch_incremental(
    pro: IndexWeightClient,
    spec: UniverseSpec,
    baseline: pd.DataFrame,
    end_date: pd.Timestamp,
    *,
    full_refresh: bool = False,
    pause_seconds: float = 0.3,
    api_row_guard: int = API_ROW_GUARD,
) -> pd.DataFrame:
    complete = complete_snapshots(
        baseline,
        expected_size=spec.size,
        universe_name=spec.name,
        log_rejected=False,
    )
    fetch_start = spec.full_fetch_start
    if not full_refresh and not complete.empty:
        fetch_start = complete["trade_date"].max() - pd.Timedelta(days=REFETCH_OVERLAP_DAYS)
    fetch_start = max(fetch_start.normalize(), spec.full_fetch_start.normalize())
    final = pd.Timestamp(end_date).normalize()

    pages: list[pd.DataFrame] = []
    for page_start, page_end in iter_month_windows(fetch_start, final):
        print(
            f"[{spec.name}] fetching monthly page "
            f"{page_start:%Y-%m-%d} -> {page_end:%Y-%m-%d}",
            flush=True,
        )
        pages.append(
            fetch_range(
                pro,
                spec,
                page_start,
                page_end,
                api_row_guard=api_row_guard,
                pause_seconds=pause_seconds,
            )
        )
    if not pages:
        return pd.DataFrame(columns=["trade_date", "con_code"])
    return prepare_rows(pd.concat(pages, ignore_index=True))


def load_baseline_snapshots(
    spec: UniverseSpec,
    snapshot_cache: Path,
    *,
    combo_cache: Path | None = None,
) -> pd.DataFrame:
    # The purpose-built snapshot CSV is newer/more authoritative than the
    # legacy CSI300 combo cache, so it is merged last and wins whole dates.
    frames: list[pd.DataFrame] = []
    if combo_cache is not None and combo_cache.exists():
        try:
            with combo_cache.open("rb") as handle:
                cached = pickle.load(handle)
            if isinstance(cached, dict) and isinstance(cached.get("iw"), pd.DataFrame):
                frames.append(cached["iw"])
        except Exception as exc:
            print(f"[{spec.name}] combo cache ignored: {exc}", flush=True)
    if snapshot_cache.exists():
        try:
            frames.append(pd.read_csv(snapshot_cache, dtype=str))
        except Exception as exc:
            print(f"[{spec.name}] snapshot CSV ignored: {exc}", flush=True)
    if not frames:
        return pd.DataFrame(columns=["trade_date", "con_code"])
    return merge_snapshot_sources(*frames)


def atomic_write(path: Path, text: str, attempts: int = WRITE_ATTEMPTS) -> None:
    if not text:
        raise ValueError(f"Refusing to publish empty content: {path}")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    expected_size = len(text.encode("utf-8"))
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
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


def _render_periods(periods: pd.DataFrame) -> str:
    return "".join(
        f"{row.code}\t{row.start:%Y-%m-%d}\t{row.end:%Y-%m-%d}\n"
        for row in periods.itertuples(index=False)
    )


def build(
    name: str,
    *,
    local_data: Path = DEFAULT_LOCAL_DATA,
    nas_data: Path | None = DEFAULT_NAS_DATA,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    calendar_path: Path | None = None,
    snapshot_cache: Path | None = None,
    combo_cache: Path | None = None,
    pro: IndexWeightClient | None = None,
    token: str | None = None,
    full_refresh: bool = False,
    pause_seconds: float = 0.3,
    max_snapshot_age_days: int | None = MAX_SNAPSHOT_AGE_DAYS,
) -> BuildResult:
    """Build and publish one validated universe.  Does not acquire the lock."""

    spec = get_spec(name)
    local_data = Path(local_data)
    cache_root = Path(cache_root)
    calendar_file = Path(calendar_path) if calendar_path is not None else local_data / "calendars/day.txt"
    local_output = local_data / f"instruments/{spec.name}.txt"
    nas_output = Path(nas_data) / f"instruments/{spec.name}.txt" if nas_data is not None else None
    cache_file = Path(snapshot_cache) if snapshot_cache is not None else cache_root / f"{spec.name}_weight_snapshots.csv"
    if combo_cache is None and spec.name == "csi300":
        combo_cache = cache_root / "_combo_cache_300_long.pkl"

    calendar = read_calendar(calendar_file)
    calendar_end = pd.Timestamp(calendar[-1]).normalize()
    fetch_end = min(pd.Timestamp.now().normalize(), calendar_end)
    baseline = load_baseline_snapshots(spec, cache_file, combo_cache=combo_cache)

    if pro is None:
        resolved_token = token if token is not None else _read_token(cache_root)
        if resolved_token:
            pro = _create_client(resolved_token)

    if pro is None:
        if full_refresh:
            raise RuntimeError(
                f"{spec.name} --full-refresh requires Tushare access; "
                "cached snapshots are deliberately excluded"
            )
        print(f"[{spec.name}] TUSHARE_TOKEN unavailable; validating cached snapshots", flush=True)
        fetched = pd.DataFrame(columns=["trade_date", "con_code"])
    else:
        fetched = fetch_incremental(
            pro,
            spec,
            baseline,
            fetch_end,
            full_refresh=full_refresh,
            pause_seconds=pause_seconds,
        )

    # A full refresh must stand on the newly fetched history alone.  Retaining
    # old cache rows here would let an API response containing only recent
    # snapshots appear historically complete.
    combined = (
        merge_snapshot_sources(fetched)
        if full_refresh
        else merge_snapshot_sources(baseline, fetched)
    )
    unresolved = _snapshot_counts(combined)
    unresolved = unresolved[unresolved != spec.size]
    if not unresolved.empty:
        summary = ", ".join(
            f"{date:%Y-%m-%d}:{int(count)}" for date, count in unresolved.items()
        )
        raise IncompleteSnapshotError(
            f"{spec.name} has unresolved incomplete snapshots; refusing publication: {summary}"
        )
    snapshots = complete_snapshots(
        combined,
        expected_size=spec.size,
        universe_name=spec.name,
    )
    if snapshots.empty:
        raise RuntimeError(f"No complete {spec.name} snapshots are available")
    coverage = validate_snapshot_coverage(snapshots, spec)
    first_snapshot = coverage.first_snapshot
    latest_snapshot = coverage.latest_snapshot
    if max_snapshot_age_days is not None:
        age = (calendar_end - latest_snapshot).days
        if age > max_snapshot_age_days:
            raise RuntimeError(
                f"{spec.name} latest complete snapshot {latest_snapshot:%Y-%m-%d} is "
                f"{age} days behind calendar end {calendar_end:%Y-%m-%d}; "
                f"limit is {max_snapshot_age_days} days"
            )

    periods = build_membership_periods(
        snapshots,
        calendar,
        end_date=calendar_end,
        expected_size=spec.size,
        universe_name=spec.name,
    )
    instrument_text = _render_periods(periods)

    # Publish only after the full PIT interval set has passed validation.  The
    # local cache and instrument both use replace-on-success writes.
    cache_rows = snapshots.copy()
    cache_rows["trade_date"] = cache_rows["trade_date"].dt.strftime("%Y%m%d")
    atomic_write(cache_file, cache_rows.to_csv(index=False))
    atomic_write(local_output, instrument_text)

    nas_published = False
    if nas_output is not None:
        try:
            atomic_write(nas_output, instrument_text)
            nas_published = True
        except Exception as exc:
            print(
                f"[{spec.name}] NAS publish failed after local success; "
                f"local file remains valid: {exc}",
                flush=True,
            )

    unique_codes = int(periods["code"].nunique())
    multi_period = int((periods.groupby("code").size() > 1).sum())
    print(
        f"[{spec.name}] wrote {len(periods)} intervals / {unique_codes} codes; "
        f"{multi_period} codes have multiple intervals; "
        f"snapshots={snapshots['trade_date'].nunique()} "
        f"({first_snapshot:%Y-%m-%d}..{latest_snapshot:%Y-%m-%d}); "
        f"monthly_coverage={coverage.covered_months}/{coverage.expected_months} "
        f"({coverage.month_coverage_ratio:.1%}); max_gap={coverage.max_snapshot_gap_days}d; "
        f"calendar_end={calendar_end:%Y-%m-%d}; nas={nas_published}",
        flush=True,
    )
    return BuildResult(
        universe=spec.name,
        output_path=local_output,
        snapshot_cache=cache_file,
        snapshot_count=int(snapshots["trade_date"].nunique()),
        interval_count=len(periods),
        unique_code_count=unique_codes,
        first_snapshot=first_snapshot,
        latest_snapshot=latest_snapshot,
        calendar_end=calendar_end,
        month_coverage_ratio=coverage.month_coverage_ratio,
        max_snapshot_gap_days=coverage.max_snapshot_gap_days,
        nas_published=nas_published,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "universes",
        nargs="*",
        choices=sorted(UNIVERSES),
        default=None,
        help="universes to build (default: csi500 csi1000)",
    )
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_DATA)
    parser.add_argument("--nas-root", type=Path, default=DEFAULT_NAS_DATA)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--no-nas", action="store_true")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--pause-seconds", type=float, default=0.3)
    parser.add_argument("--max-snapshot-age-days", type=int, default=MAX_SNAPSHOT_AGE_DAYS)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.pause_seconds < 0:
        raise SystemExit("--pause-seconds must be non-negative")
    if args.max_snapshot_age_days < 0:
        raise SystemExit("--max-snapshot-age-days must be non-negative")

    universes = args.universes or ["csi500", "csi1000"]

    lock_path = args.cache_root / BUILD_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[universe] waiting for build lock: {lock_path}", flush=True)
    with FileLock(str(lock_path), timeout=900):
        for name in universes:
            build(
                name,
                local_data=args.local_root,
                nas_data=None if args.no_nas else args.nas_root,
                cache_root=args.cache_root,
                calendar_path=args.calendar,
                full_refresh=args.full_refresh,
                pause_seconds=args.pause_seconds,
                max_snapshot_age_days=args.max_snapshot_age_days,
            )


if __name__ == "__main__":
    main()
