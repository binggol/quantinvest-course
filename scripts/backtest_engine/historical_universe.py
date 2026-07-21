"""Point-in-time constituent reconstruction for sparse or daily index weights.

The input used by Tushare's ``index_weight`` endpoint is a collection of
complete constituent snapshots, not a list of single-stock entry/exit dates.
This module preserves that distinction and never reduces a stock's history to
one ``min(date)``/``max(date)`` interval.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Literal, Sequence

import pandas as pd


UniverseMode = Literal["auto", "daily", "snapshot"]
IncompletePolicy = Literal["barrier", "keep", "raise"]


class UniverseCoverageError(LookupError):
    """Raised when a requested date has no trustworthy membership snapshot."""


class UniverseAuditError(ValueError):
    """Raised when strict construction encounters source audit failures."""


@dataclass(frozen=True, order=True)
class MembershipInterval:
    """An inclusive constituent membership interval."""

    code: str
    start_date: pd.Timestamp
    end_date: pd.Timestamp

    def contains(self, trade_date: Any) -> bool:
        value = _coerce_timestamp(trade_date, "trade_date")
        return self.start_date <= value <= self.end_date

    def to_dict(self) -> dict[str, str]:
        return {
            "con_code": self.code,
            "start_date": _date_key(self.start_date),
            "end_date": _date_key(self.end_date),
        }


@dataclass(frozen=True)
class _Snapshot:
    trade_date: pd.Timestamp
    members: frozenset[str]
    raw_rows: int
    duplicate_rows: int
    complete: bool
    on_calendar: bool
    effective: bool


@dataclass(frozen=True)
class _Segment:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    members: frozenset[str]


def _date_key(value: pd.Timestamp) -> str:
    return value.strftime("%Y%m%d")


def _coerce_timestamp(value: Any, label: str) -> pd.Timestamp:
    if value is None or (not isinstance(value, (date, datetime, pd.Timestamp)) and pd.isna(value)):
        raise ValueError(f"{label} is missing")

    if isinstance(value, (date, datetime, pd.Timestamp)):
        parsed = pd.Timestamp(value)
    else:
        text = str(value).strip()
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        if len(text) == 8 and text.isdigit():
            parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
        else:
            parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid {label}: {value!r}")
    parsed = pd.Timestamp(parsed)
    if parsed.tzinfo is not None:
        parsed = parsed.tz_localize(None)
    return parsed.normalize()


def _normalise_date_series(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip().str.replace(r"\.0+$", "", regex=True)
    compact = text.str.fullmatch(r"\d{8}", na=False)
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if compact.any():
        parsed.loc[compact] = pd.to_datetime(
            text.loc[compact], format="%Y%m%d", errors="coerce"
        ).to_numpy()
    if (~compact).any():
        parsed.loc[~compact] = pd.to_datetime(text.loc[~compact], errors="coerce").to_numpy()
    return parsed.dt.normalize()


def _normalise_calendar(calendar: Iterable[Any] | None) -> tuple[pd.Timestamp, ...]:
    if calendar is None:
        return ()
    values: set[pd.Timestamp] = set()
    for value in calendar:
        values.add(_coerce_timestamp(value, "calendar date"))
    return tuple(sorted(values))


def _normalise_source(
    index_weight: pd.DataFrame,
    *,
    date_col: str,
    code_col: str,
    index_col: str | None,
    index_code: str | Sequence[str] | None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if not isinstance(index_weight, pd.DataFrame):
        raise TypeError("index_weight must be a pandas DataFrame")
    missing = [column for column in (date_col, code_col) if column not in index_weight.columns]
    if missing:
        raise ValueError(f"index_weight missing required columns: {missing}")

    source = index_weight.copy(deep=True)
    source["_source_row"] = source.index.map(str)
    if index_code is not None:
        if not index_col or index_col not in source.columns:
            raise ValueError(f"index_weight missing index column: {index_col!r}")
        accepted = {str(index_code)} if isinstance(index_code, str) else {str(v) for v in index_code}
        source = source[source[index_col].astype(str).isin(accepted)].copy()

    source["_trade_date"] = _normalise_date_series(source[date_col])
    source["_con_code"] = source[code_col].astype("string").str.strip().str.upper()
    invalid_mask = (
        source["_trade_date"].isna()
        | source["_con_code"].isna()
        | source["_con_code"].eq("")
        | source["_con_code"].str.lower().isin({"nan", "none", "nat", "<na>"})
    )
    invalid_rows = [
        {
            "source_row": str(row["_source_row"]),
            "trade_date": str(row[date_col]),
            "con_code": str(row[code_col]),
        }
        for _, row in source.loc[invalid_mask].iterrows()
    ]
    clean = source.loc[~invalid_mask, ["_source_row", "_trade_date", "_con_code"]].copy()
    clean = clean.rename(columns={"_trade_date": "trade_date", "_con_code": "con_code"})
    return clean, invalid_rows


def _previous_date(
    boundary: pd.Timestamp,
    calendar: tuple[pd.Timestamp, ...],
) -> pd.Timestamp:
    if calendar:
        position = bisect_left(calendar, boundary) - 1
        if position < 0:
            return boundary - pd.Timedelta(days=1)
        return calendar[position]
    return boundary - pd.Timedelta(days=1)


def _calendar_floor(value: pd.Timestamp, calendar: tuple[pd.Timestamp, ...]) -> pd.Timestamp:
    if not calendar:
        return value
    position = bisect_right(calendar, value) - 1
    if position < 0:
        raise ValueError("as_of precedes the first trading calendar date")
    return calendar[position]


def _are_adjacent(
    left: pd.Timestamp,
    right: pd.Timestamp,
    calendar_positions: dict[pd.Timestamp, int],
) -> bool:
    if calendar_positions:
        left_pos = calendar_positions.get(left)
        right_pos = calendar_positions.get(right)
        return left_pos is not None and right_pos == left_pos + 1
    return right <= left + pd.Timedelta(days=1)


def _detect_mode(
    snapshot_dates: Sequence[pd.Timestamp],
    calendar: tuple[pd.Timestamp, ...],
    threshold: float,
) -> tuple[str, float]:
    if len(snapshot_dates) < 2:
        return "snapshot", 0.0
    first = min(snapshot_dates)
    last = max(snapshot_dates)
    if calendar:
        denominator = sum(first <= value <= last for value in calendar)
    else:
        denominator = len(pd.bdate_range(first, last))
    coverage = len(set(snapshot_dates)) / max(denominator, 1)
    return ("daily" if coverage >= threshold else "snapshot"), coverage


def _compact_gap_ranges(
    calendar: tuple[pd.Timestamp, ...],
    covered: set[pd.Timestamp],
    first: pd.Timestamp,
    last: pd.Timestamp,
    invalid_dates: set[pd.Timestamp],
    mode: str,
) -> list[dict[str, Any]]:
    relevant = [value for value in calendar if first <= value <= last]
    gaps: list[dict[str, Any]] = []
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    count = 0
    for value in relevant:
        if value not in covered:
            if start is None:
                start = value
                count = 0
            end = value
            count += 1
            continue
        if start is not None and end is not None:
            reason = "invalid_snapshot_barrier" if any(start <= d <= end for d in invalid_dates) else (
                "missing_daily_snapshot" if mode == "daily" else "uncovered"
            )
            gaps.append(
                {
                    "start_date": _date_key(start),
                    "end_date": _date_key(end),
                    "trading_days": count,
                    "reason": reason,
                }
            )
            start = end = None
    if start is not None and end is not None:
        reason = "invalid_snapshot_barrier" if any(start <= d <= end for d in invalid_dates) else (
            "missing_daily_snapshot" if mode == "daily" else "uncovered"
        )
        gaps.append(
            {
                "start_date": _date_key(start),
                "end_date": _date_key(end),
                "trading_days": count,
                "reason": reason,
            }
        )
    return gaps


def audit_membership_intervals(
    intervals: pd.DataFrame,
    index_weight: pd.DataFrame,
    *,
    as_of: Any | None = None,
    expected_snapshot_size: int | None = 300,
    date_col: str = "trade_date",
    code_col: str = "con_code",
    interval_code_col: str = "con_code",
    start_col: str = "start_date",
    end_col: str = "end_date",
    max_issue_records: int = 100,
) -> dict[str, Any]:
    """Audit arbitrary intervals for future leakage and swallowed exits.

    A swallowed exit is present when an interval covers a complete snapshot on
    which that code is absent.  This catches the common, incorrect construction
    ``start=min(member_dates), end=max(member_dates)``.
    """

    required = (interval_code_col, start_col, end_col)
    if not isinstance(intervals, pd.DataFrame):
        raise TypeError("intervals must be a pandas DataFrame")
    missing = [column for column in required if column not in intervals.columns]
    if missing:
        raise ValueError(f"intervals missing required columns: {missing}")

    source, invalid_source = _normalise_source(
        index_weight,
        date_col=date_col,
        code_col=code_col,
        index_col=None,
        index_code=None,
    )
    work = intervals.copy(deep=True)
    work["_code"] = work[interval_code_col].astype("string").str.strip().str.upper()
    work["_start"] = _normalise_date_series(work[start_col])
    work["_end"] = _normalise_date_series(work[end_col])
    invalid_mask = work["_code"].isna() | work["_code"].eq("") | work["_start"].isna() | work["_end"].isna()
    invalid_mask |= work["_start"] > work["_end"]
    invalid_intervals = [
        {
            "row": str(index),
            "con_code": str(row[interval_code_col]),
            "start_date": str(row[start_col]),
            "end_date": str(row[end_col]),
        }
        for index, row in work.loc[invalid_mask].head(max_issue_records).iterrows()
    ]
    clean = work.loc[~invalid_mask, ["_code", "_start", "_end"]].copy()

    if as_of is None:
        if source.empty:
            cutoff = clean["_end"].max() if not clean.empty else pd.Timestamp.min.normalize()
        else:
            cutoff = source["trade_date"].max()
    else:
        cutoff = _coerce_timestamp(as_of, "as_of")

    future = clean[(clean["_start"] > cutoff) | (clean["_end"] > cutoff)]
    future_intervals = [
        {
            "con_code": code,
            "start_date": _date_key(start_date),
            "end_date": _date_key(end_date),
            "as_of": _date_key(cutoff),
        }
        for code, start_date, end_date in future.head(max_issue_records).itertuples(
            index=False, name=None
        )
    ]

    duplicate_count = int(clean.duplicated(["_code", "_start", "_end"]).sum())
    overlaps: list[dict[str, str]] = []
    overlap_count = 0
    for code, group in clean.sort_values(["_code", "_start", "_end"]).groupby("_code", sort=False):
        previous_end: pd.Timestamp | None = None
        for start_date, end_date in group[["_start", "_end"]].itertuples(index=False, name=None):
            if previous_end is not None and start_date <= previous_end:
                overlap_count += 1
                if len(overlaps) < max_issue_records:
                    overlaps.append(
                        {
                            "con_code": str(code),
                            "start_date": _date_key(start_date),
                            "previous_end_date": _date_key(previous_end),
                        }
                    )
            previous_end = max(previous_end, end_date) if previous_end is not None else end_date

    snapshots: dict[pd.Timestamp, frozenset[str]] = {}
    for snapshot_date, group in source.groupby("trade_date", sort=True):
        members = frozenset(group["con_code"])
        if expected_snapshot_size is None or len(members) == expected_snapshot_size:
            snapshots[pd.Timestamp(snapshot_date)] = members

    swallowed: list[dict[str, str]] = []
    swallowed_count = 0
    snapshot_items = tuple(snapshots.items())
    ordered_intervals = clean.sort_values(["_code", "_start", "_end"])
    for code, start_date, end_date in ordered_intervals.itertuples(index=False, name=None):
        for snapshot_date, members in snapshot_items:
            if snapshot_date < start_date:
                continue
            if snapshot_date > end_date:
                break
            if code not in members:
                swallowed_count += 1
                if len(swallowed) < max_issue_records:
                    swallowed.append(
                        {
                            "con_code": code,
                            "interval_start": _date_key(start_date),
                            "interval_end": _date_key(end_date),
                            "absent_snapshot": _date_key(snapshot_date),
                        }
                    )

    return {
        "as_of": _date_key(cutoff),
        "interval_rows": int(len(intervals)),
        "valid_interval_rows": int(len(clean)),
        "invalid_interval_count": int(invalid_mask.sum()),
        "invalid_intervals": invalid_intervals,
        "duplicate_interval_count": duplicate_count,
        "overlap_count": overlap_count,
        "overlaps": overlaps,
        "future_interval_count": int(len(future)),
        "future_intervals": future_intervals,
        "swallowed_gap_count": swallowed_count,
        "swallowed_gap_violations": swallowed,
        "invalid_source_row_count": len(invalid_source),
        "ok": not invalid_intervals and overlap_count == 0 and future.empty and swallowed_count == 0,
    }


class HistoricalUniverse:
    """Point-in-time index membership with explicit coverage boundaries."""

    def __init__(
        self,
        *,
        mode: str,
        valid_through: pd.Timestamp,
        calendar: tuple[pd.Timestamp, ...],
        snapshots: tuple[_Snapshot, ...],
        segments: tuple[_Segment, ...],
        intervals_by_code: dict[str, tuple[MembershipInterval, ...]],
        audit: dict[str, Any],
    ) -> None:
        self._mode = mode
        self._valid_through = valid_through
        self._calendar = calendar
        self._calendar_set = frozenset(calendar)
        self._snapshots = snapshots
        self._segments = segments
        self._segment_starts = tuple(segment.start_date for segment in segments)
        self._intervals_by_code = intervals_by_code
        self._audit = audit

    @classmethod
    def from_index_weight(
        cls,
        index_weight: pd.DataFrame,
        *,
        trading_calendar: Iterable[Any] | None = None,
        mode: UniverseMode = "auto",
        expected_snapshot_size: int | None = 300,
        as_of: Any | None = None,
        incomplete_policy: IncompletePolicy = "barrier",
        strict: bool = False,
        date_col: str = "trade_date",
        code_col: str = "con_code",
        index_col: str | None = "index_code",
        index_code: str | Sequence[str] | None = None,
        daily_detection_threshold: float = 0.80,
        max_issue_records: int = 100,
    ) -> "HistoricalUniverse":
        if mode not in {"auto", "daily", "snapshot"}:
            raise ValueError(f"unsupported mode: {mode!r}")
        if incomplete_policy not in {"barrier", "keep", "raise"}:
            raise ValueError(f"unsupported incomplete_policy: {incomplete_policy!r}")
        if expected_snapshot_size is not None and expected_snapshot_size <= 0:
            raise ValueError("expected_snapshot_size must be positive or None")
        if not 0 < daily_detection_threshold <= 1:
            raise ValueError("daily_detection_threshold must be in (0, 1]")

        source, invalid_rows = _normalise_source(
            index_weight,
            date_col=date_col,
            code_col=code_col,
            index_col=index_col,
            index_code=index_code,
        )
        if source.empty:
            raise ValueError("index_weight has no valid rows")

        calendar = _normalise_calendar(trading_calendar)
        raw_max = source["trade_date"].max()
        requested_as_of = _coerce_timestamp(as_of, "as_of") if as_of is not None else raw_max
        valid_through = _calendar_floor(requested_as_of, calendar)

        future_source = source[source["trade_date"] > valid_through]
        future_snapshot_dates = sorted(future_source["trade_date"].unique())
        current = source[source["trade_date"] <= valid_through].copy()
        if current.empty:
            raise ValueError("index_weight has no rows on or before as_of")

        duplicate_groups = (
            current.groupby(["trade_date", "con_code"], sort=True)
            .size()
            .loc[lambda values: values > 1]
        )
        duplicate_pairs = [
            {
                "trade_date": _date_key(pd.Timestamp(snapshot_date)),
                "con_code": str(code),
                "rows": int(rows),
                "extra_rows": int(rows - 1),
            }
            for (snapshot_date, code), rows in duplicate_groups.head(max_issue_records).items()
        ]
        duplicate_row_count = int((duplicate_groups - 1).sum()) if not duplicate_groups.empty else 0

        calendar_set = frozenset(calendar)
        records: list[_Snapshot] = []
        incomplete_snapshots: list[dict[str, Any]] = []
        non_trading_snapshots: list[str] = []
        snapshot_counts: list[dict[str, Any]] = []
        for snapshot_date, group in current.groupby("trade_date", sort=True):
            snapshot_date = pd.Timestamp(snapshot_date)
            raw_rows = int(len(group))
            members = frozenset(group["con_code"])
            duplicate_rows = raw_rows - len(members)
            complete = expected_snapshot_size is None or len(members) == expected_snapshot_size
            on_calendar = not calendar or snapshot_date in calendar_set
            if not on_calendar:
                non_trading_snapshots.append(_date_key(snapshot_date))
            if not complete:
                incomplete_snapshots.append(
                    {
                        "trade_date": _date_key(snapshot_date),
                        "rows": raw_rows,
                        "unique_members": len(members),
                        "expected_members": expected_snapshot_size,
                    }
                )
            effective = on_calendar and (complete or incomplete_policy == "keep")
            records.append(
                _Snapshot(
                    trade_date=snapshot_date,
                    members=members,
                    raw_rows=raw_rows,
                    duplicate_rows=duplicate_rows,
                    complete=complete,
                    on_calendar=on_calendar,
                    effective=effective,
                )
            )
            snapshot_counts.append(
                {
                    "trade_date": _date_key(snapshot_date),
                    "rows": raw_rows,
                    "unique_members": len(members),
                    "duplicate_rows": duplicate_rows,
                    "complete": complete,
                    "on_calendar": on_calendar,
                    "effective": effective,
                }
            )

        if incomplete_policy == "raise" and incomplete_snapshots:
            dates = ", ".join(item["trade_date"] for item in incomplete_snapshots[:10])
            raise UniverseAuditError(f"incomplete constituent snapshots: {dates}")

        detected_mode, snapshot_coverage_ratio = _detect_mode(
            [record.trade_date for record in records], calendar, daily_detection_threshold
        )
        resolved_mode = detected_mode if mode == "auto" else mode

        segments: list[_Segment] = []
        for index, record in enumerate(records):
            if not record.effective or record.trade_date > valid_through:
                continue
            if resolved_mode == "daily":
                end_date = record.trade_date
            else:
                next_date = records[index + 1].trade_date if index + 1 < len(records) else None
                end_date = _previous_date(next_date, calendar) if next_date is not None else valid_through
                end_date = min(end_date, valid_through)
            if end_date >= record.trade_date:
                segments.append(_Segment(record.trade_date, end_date, record.members))

        calendar_positions = {value: index for index, value in enumerate(calendar)}
        mutable_intervals: dict[str, list[MembershipInterval]] = {}
        for segment in segments:
            for code in segment.members:
                code_intervals = mutable_intervals.setdefault(code, [])
                if code_intervals and _are_adjacent(
                    code_intervals[-1].end_date, segment.start_date, calendar_positions
                ):
                    previous = code_intervals[-1]
                    code_intervals[-1] = MembershipInterval(
                        code=code,
                        start_date=previous.start_date,
                        end_date=max(previous.end_date, segment.end_date),
                    )
                else:
                    code_intervals.append(
                        MembershipInterval(code, segment.start_date, segment.end_date)
                    )
        intervals_by_code = {
            code: tuple(values) for code, values in sorted(mutable_intervals.items())
        }

        interval_rows = [interval.to_dict() for values in intervals_by_code.values() for interval in values]
        interval_frame = pd.DataFrame(interval_rows, columns=["con_code", "start_date", "end_date"])
        effective_rows = [
            {"trade_date": _date_key(record.trade_date), "con_code": code}
            for record in records
            if record.effective
            for code in record.members
        ]
        effective_frame = pd.DataFrame(effective_rows, columns=["trade_date", "con_code"])
        interval_audit = audit_membership_intervals(
            interval_frame,
            effective_frame,
            as_of=valid_through,
            expected_snapshot_size=None,
            max_issue_records=max_issue_records,
        )

        covered_dates: set[pd.Timestamp] = set()
        if calendar and records:
            for segment in segments:
                left = bisect_left(calendar, segment.start_date)
                right = bisect_right(calendar, segment.end_date)
                covered_dates.update(calendar[left:right])
            first_source_date = min(record.trade_date for record in records)
            invalid_snapshot_dates = {
                record.trade_date for record in records if not record.effective
            }
            coverage_gaps = _compact_gap_ranges(
                calendar,
                covered_dates,
                first_source_date,
                valid_through,
                invalid_snapshot_dates,
                resolved_mode,
            )
        else:
            coverage_gaps = []
            for idx, record in enumerate(records):
                if record.effective:
                    continue
                if idx > 0 and not records[idx - 1].effective:
                    continue
                next_valid = next(
                    (later.trade_date for later in records[idx + 1 :] if later.effective),
                    None,
                )
                gap_end = _previous_date(next_valid, ()) if next_valid is not None else valid_through
                if gap_end >= record.trade_date:
                    coverage_gaps.append(
                        {
                            "start_date": _date_key(record.trade_date),
                            "end_date": _date_key(gap_end),
                            "trading_days": None,
                            "reason": "invalid_snapshot_barrier",
                        }
                    )

        multi_interval_codes = {
            code: len(values) for code, values in intervals_by_code.items() if len(values) > 1
        }
        errors: list[str] = []
        warnings: list[str] = []
        if invalid_rows:
            errors.append(f"{len(invalid_rows)} invalid source rows")
        if incomplete_snapshots:
            errors.append(f"{len(incomplete_snapshots)} incomplete snapshots")
        if non_trading_snapshots:
            errors.append(f"{len(non_trading_snapshots)} snapshots outside trading calendar")
        if future_snapshot_dates:
            errors.append(f"{len(future_snapshot_dates)} future snapshots beyond as_of")
        if coverage_gaps:
            errors.append(f"{len(coverage_gaps)} uncovered membership ranges")
        if interval_audit["future_interval_count"]:
            errors.append(f"{interval_audit['future_interval_count']} future intervals")
        if interval_audit["swallowed_gap_count"]:
            errors.append(f"{interval_audit['swallowed_gap_count']} swallowed membership gaps")
        if duplicate_row_count:
            warnings.append(f"{duplicate_row_count} duplicate date/code rows")

        audit = {
            "ok": not errors,
            "mode_requested": mode,
            "mode_resolved": resolved_mode,
            "snapshot_coverage_ratio": round(snapshot_coverage_ratio, 6),
            "expected_snapshot_size": expected_snapshot_size,
            "incomplete_policy": incomplete_policy,
            "source_rows": int(len(index_weight)),
            "valid_source_rows": int(len(source)),
            "invalid_source_row_count": len(invalid_rows),
            "invalid_source_rows": invalid_rows[:max_issue_records],
            "snapshot_count": len(records),
            "first_snapshot": _date_key(records[0].trade_date),
            "last_snapshot": _date_key(records[-1].trade_date),
            "valid_through": _date_key(valid_through),
            "snapshot_counts": snapshot_counts,
            "incomplete_snapshot_count": len(incomplete_snapshots),
            "incomplete_snapshots": incomplete_snapshots[:max_issue_records],
            "duplicate_row_count": duplicate_row_count,
            "duplicate_pair_count": int(len(duplicate_groups)),
            "duplicate_pairs": duplicate_pairs,
            "non_trading_snapshot_count": len(non_trading_snapshots),
            "non_trading_snapshots": non_trading_snapshots[:max_issue_records],
            "future_snapshot_count": len(future_snapshot_dates),
            "future_snapshot_dates": [
                _date_key(pd.Timestamp(value)) for value in future_snapshot_dates[:max_issue_records]
            ],
            "segment_count": len(segments),
            "constituent_count": len(intervals_by_code),
            "interval_count": sum(len(values) for values in intervals_by_code.values()),
            "multi_interval_code_count": len(multi_interval_codes),
            "multi_interval_codes": dict(list(multi_interval_codes.items())[:max_issue_records]),
            "coverage_gap_count": len(coverage_gaps),
            "coverage_gaps": coverage_gaps[:max_issue_records],
            "future_interval_count": interval_audit["future_interval_count"],
            "future_intervals": interval_audit["future_intervals"],
            "swallowed_gap_count": interval_audit["swallowed_gap_count"],
            "swallowed_gap_violations": interval_audit["swallowed_gap_violations"],
            "interval_audit": interval_audit,
            "errors": errors,
            "warnings": warnings,
        }
        if strict and (errors or warnings):
            raise UniverseAuditError("; ".join(errors + warnings))

        return cls(
            mode=resolved_mode,
            valid_through=valid_through,
            calendar=calendar,
            snapshots=tuple(records),
            segments=tuple(segments),
            intervals_by_code=intervals_by_code,
            audit=audit,
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def valid_through(self) -> pd.Timestamp:
        return self._valid_through

    @property
    def intervals(self) -> pd.DataFrame:
        rows = [
            {
                "con_code": interval.code,
                "start_date": interval.start_date,
                "end_date": interval.end_date,
            }
            for intervals in self._intervals_by_code.values()
            for interval in intervals
        ]
        return pd.DataFrame(rows, columns=["con_code", "start_date", "end_date"])

    def intervals_for(self, code: str) -> tuple[MembershipInterval, ...]:
        return self._intervals_by_code.get(str(code).strip().upper(), ())

    def members_on(self, trade_date: Any, *, strict: bool = True) -> frozenset[str]:
        value = _coerce_timestamp(trade_date, "trade_date")
        if value > self._valid_through:
            if strict:
                raise UniverseCoverageError(
                    f"{_date_key(value)} is after valid_through {_date_key(self._valid_through)}"
                )
            return frozenset()
        if self._calendar and value not in self._calendar_set:
            if strict:
                raise UniverseCoverageError(f"{_date_key(value)} is not a trading day")
            return frozenset()

        position = bisect_right(self._segment_starts, value) - 1
        if position >= 0:
            segment = self._segments[position]
            if segment.start_date <= value <= segment.end_date:
                return segment.members
        if strict:
            raise UniverseCoverageError(f"no trustworthy membership for {_date_key(value)}")
        return frozenset()

    def is_member(self, code: str, trade_date: Any, *, strict: bool = True) -> bool:
        return str(code).strip().upper() in self.members_on(trade_date, strict=strict)

    def audit_report(self) -> dict[str, Any]:
        """Return a JSON-serialisable copy of the construction audit."""

        return deepcopy(self._audit)


def build_historical_universe(
    index_weight: pd.DataFrame,
    **kwargs: Any,
) -> HistoricalUniverse:
    """Convenience wrapper around :meth:`HistoricalUniverse.from_index_weight`."""

    return HistoricalUniverse.from_index_weight(index_weight, **kwargs)


def rebuild_membership_intervals(
    index_weight: pd.DataFrame,
    **kwargs: Any,
) -> pd.DataFrame:
    """Rebuild all point-in-time intervals as a DataFrame."""

    return build_historical_universe(index_weight, **kwargs).intervals


def audit_index_weight(index_weight: pd.DataFrame, **kwargs: Any) -> dict[str, Any]:
    """Build the universe and return its source and interval audit report."""

    return build_historical_universe(index_weight, **kwargs).audit_report()


__all__ = [
    "HistoricalUniverse",
    "MembershipInterval",
    "UniverseAuditError",
    "UniverseCoverageError",
    "audit_index_weight",
    "audit_membership_intervals",
    "build_historical_universe",
    "rebuild_membership_intervals",
]
