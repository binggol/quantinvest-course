"""Repair equity Qlib bins whose published tail was not written.

This is deliberately narrower than a rebuild.  It only appends rows for dates
that already exist in the published calendar.  Missing stock rows inside that
range are represented with the same suspension placeholders as the full
builder.  All source and bin validation completes before the first published
file is replaced.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scripts.update_daily import UpdateAlreadyRunning, qlib_update_lock
except ImportError:  # Direct execution from the scripts directory.
    from update_daily import UpdateAlreadyRunning, qlib_update_lock


FIELDS = ("open", "close", "high", "low", "volume", "change", "factor", "adj")
OHLC_FIELDS = ("open", "close", "high", "low")
BENCHMARK_CODES = frozenset(("sh000300", "sh000905", "sh000852"))


@dataclass(frozen=True)
class BinState:
    start: int
    count: int
    end: int
    old_max_adj: float
    invalid_envelope_rows: int
    last_raw_close: float
    last_adj: float


@dataclass(frozen=True)
class RepairPlan:
    code: str
    state: BinState
    rows: pd.DataFrame
    new_max_adj: float


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tail-repair.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, payload: str) -> None:
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _read_calendar(root: Path) -> list[str]:
    path = root / "calendars" / "day.txt"
    if not path.exists():
        raise RuntimeError(f"published calendar is missing: {path}")
    dates = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not dates:
        raise RuntimeError("published calendar is empty")
    parsed = pd.to_datetime(pd.Series(dates), format="%Y-%m-%d", errors="coerce")
    normalized = parsed.dt.strftime("%Y-%m-%d").tolist()
    if parsed.isna().any() or normalized != dates or dates != sorted(set(dates)):
        raise RuntimeError("published calendar must contain unique, increasing YYYY-MM-DD dates")
    return dates


def _to_code(ts_code: str) -> str:
    parts = str(ts_code).strip().upper().split(".")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isalpha():
        raise RuntimeError(f"invalid ts_code: {ts_code!r}")
    return f"{parts[1].lower()}{parts[0]}"


def _bin_path(root: Path, code: str, field: str) -> Path:
    return root / "features" / code / f"{field}.day.bin"


def _read_bin(path: Path) -> np.ndarray:
    if not path.exists():
        raise RuntimeError(f"bin is missing: {path}")
    if path.stat().st_size % np.dtype("<f4").itemsize:
        raise RuntimeError(f"bin byte length is invalid: {path}")
    values = np.fromfile(path, dtype="<f4")
    if len(values) < 2:
        raise RuntimeError(f"bin has no observations: {path}")
    return values


def _validate_bins(root: Path, code: str, calendar_size: int) -> BinState:
    arrays = {field: _read_bin(_bin_path(root, code, field)) for field in FIELDS}
    lengths = {len(values) for values in arrays.values()}
    headers = [float(values[0]) for values in arrays.values()]
    if len(lengths) != 1:
        detail = ", ".join(f"{field}={len(arrays[field])}" for field in FIELDS)
        raise RuntimeError(f"bin lengths do not align for {code}: {detail}")
    if any(not math.isfinite(value) or value < 0 or not value.is_integer() for value in headers):
        raise RuntimeError(f"bin header is not a non-negative calendar index for {code}")
    if len(set(headers)) != 1:
        raise RuntimeError(f"bin headers do not align for {code}: {headers}")

    start = int(headers[0])
    count = next(iter(lengths)) - 1
    end = start + count - 1
    if start >= calendar_size or end >= calendar_size:
        raise RuntimeError(
            f"bin calendar range is outside the published calendar for {code}: {start}..{end}"
        )

    data = {field: values[1:].astype(np.float64, copy=False) for field, values in arrays.items()}
    for field in FIELDS:
        finite = np.isfinite(data[field])
        if field == "change" and len(finite):
            # The first real observation may have no prior close, so its
            # return is undefined.  No later NaN and no infinity is valid.
            valid_initial_nan = np.isnan(data[field][0]) and finite[1:].all()
            if not finite.all() and not valid_initial_nan:
                raise RuntimeError(
                    f"existing {field} bin contains non-finite values for {code}"
                )
        elif not finite.all():
            raise RuntimeError(f"existing {field} bin contains non-finite values for {code}")
    for field in OHLC_FIELDS + ("adj", "factor"):
        if (data[field] <= 0).any():
            raise RuntimeError(f"existing {field} bin contains non-positive values for {code}")
    if (data["volume"] < 0).any():
        raise RuntimeError(f"existing volume bin contains negative values for {code}")
    envelope_invalid = (
        (data["high"] < np.maximum(data["open"], data["close"]))
        | (data["low"] > np.minimum(data["open"], data["close"]))
        | (data["high"] < data["low"])
    )
    old_max_adj = float(data["adj"].max())
    last_adj = float(data["adj"][-1])
    last_raw_close = float(data["close"][-1] * old_max_adj / last_adj)
    if not math.isfinite(last_raw_close) or last_raw_close <= 0:
        raise RuntimeError(f"cannot recover the last raw close for {code}")
    return BinState(
        start=start,
        count=count,
        end=end,
        old_max_adj=old_max_adj,
        invalid_envelope_rows=int(envelope_invalid.sum()),
        last_raw_close=last_raw_close,
        last_adj=last_adj,
    )


def _load_daily(path: Path, date_iso: str) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"required daily parquet is missing: {path}")
    frame = pd.read_parquet(path)
    volume_name = "vol" if "vol" in frame.columns else "volume" if "volume" in frame.columns else None
    change_name = "pct_chg" if "pct_chg" in frame.columns else "change" if "change" in frame.columns else None
    required = {"ts_code", "trade_date", "open", "close", "high", "low", "adj_factor"}
    missing = required - set(frame.columns)
    if volume_name is None:
        missing.add("vol/volume")
    if change_name is None:
        missing.add("pct_chg/change")
    if missing:
        raise RuntimeError(f"daily parquet {path.name} is missing columns: {sorted(missing)}")
    if frame.empty:
        raise RuntimeError(f"daily parquet is empty: {path}")

    work = pd.DataFrame()
    work["ts_code"] = frame["ts_code"].astype(str).str.strip().str.upper()
    parsed = pd.to_datetime(frame["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    if parsed.isna().any() or not parsed.dt.strftime("%Y-%m-%d").eq(date_iso).all():
        raise RuntimeError(f"daily parquet contains a wrong or invalid trade_date: {path.name}")
    work["date"] = date_iso
    try:
        work["code"] = work["ts_code"].map(_to_code)
    except RuntimeError as exc:
        raise RuntimeError(f"{path.name}: {exc}") from exc
    if work["code"].duplicated().any():
        sample = work.loc[work["code"].duplicated(keep=False), "ts_code"].head(5).tolist()
        raise RuntimeError(f"daily parquet contains duplicate stocks: {path.name}, sample={sample}")

    for field in OHLC_FIELDS:
        work[field] = pd.to_numeric(frame[field], errors="coerce")
    work["adj"] = pd.to_numeric(frame["adj_factor"], errors="coerce")
    work["volume"] = pd.to_numeric(frame[volume_name], errors="coerce")
    work["change"] = pd.to_numeric(frame[change_name], errors="coerce")
    if change_name == "pct_chg":
        work["change"] /= 100.0

    numeric = work[list(OHLC_FIELDS) + ["adj", "volume", "change"]].to_numpy(dtype=np.float64)
    invalid = ~np.isfinite(numeric).all(axis=1)
    invalid |= (work[list(OHLC_FIELDS) + ["adj"]] <= 0).any(axis=1).to_numpy()
    invalid |= work["volume"].lt(0).to_numpy()
    invalid |= (
        work["high"].lt(work[["open", "close"]].max(axis=1))
        | work["low"].gt(work[["open", "close"]].min(axis=1))
        | work["high"].lt(work["low"])
    ).to_numpy()
    if invalid.any():
        sample = work.loc[invalid, "ts_code"].head(5).tolist()
        raise RuntimeError(
            f"daily parquet contains invalid adjustment/OHLC/volume/change: {path.name}, "
            f"count={int(invalid.sum())}, sample={sample}"
        )
    return work[["code", "date", *OHLC_FIELDS, "volume", "change", "adj"]]


def _metadata_payload(
    root: Path,
    calendar: list[str],
    prospective: dict[str, tuple[int, int]],
    target_codes: set[str],
) -> tuple[str, int]:
    path = root / "instruments" / "all.txt"
    if not path.exists():
        raise RuntimeError(f"instrument metadata is missing: {path}")
    original = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    original_by_code: dict[str, str] = {}
    order: list[str] = []
    for line in original:
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            raise RuntimeError(f"invalid instrument metadata line: {line!r}")
        code = parts[0].lower()
        if code in original_by_code:
            raise RuntimeError(f"duplicate instrument metadata entry: {code}")
        original_by_code[code] = line
        order.append(code)

    # Old Qlib roots can retain thousands of orphan feature directories from
    # legacy OTC universes.  Rebuilding metadata from every directory would
    # accidentally republish incomplete instruments.  Preserve the published
    # universe and add only stocks confirmed by the target-day parquet.
    equity_codes = (set(original_by_code) - BENCHMARK_CODES) | (target_codes - BENCHMARK_CODES)
    ranges: dict[str, tuple[int, int]] = {}
    for code in sorted(equity_codes):
        if code in prospective:
            ranges[code] = prospective[code]
            continue
        close = _read_bin(_bin_path(root, code, "close"))
        header = float(close[0])
        if not math.isfinite(header) or header < 0 or not header.is_integer():
            raise RuntimeError(f"invalid close bin header while rebuilding metadata: {code}")
        start = int(header)
        end = start + len(close) - 2
        if end >= len(calendar):
            raise RuntimeError(f"close bin exceeds published calendar while rebuilding metadata: {code}")
        ranges[code] = (start, end)

    output: list[str] = []
    emitted: set[str] = set()
    for code in order:
        if code in BENCHMARK_CODES:
            output.append(original_by_code[code])
        else:
            start, end = ranges[code]
            output.append(f"{code}\t{calendar[start]}\t{calendar[end]}")
        emitted.add(code)
    for code in sorted(equity_codes - emitted):
        start, end = ranges[code]
        output.append(f"{code}\t{calendar[start]}\t{calendar[end]}")
    return "\n".join(output) + "\n", len(output)


def _write_plan(root: Path, plan: RepairPlan) -> tuple[int, bool]:
    old_max = plan.state.old_max_adj
    denominator = plan.new_max_adj
    scale = old_max / denominator
    rows = plan.rows
    append_values: dict[str, np.ndarray] = {}
    ratio = rows["adj"].to_numpy(dtype=np.float64) / denominator
    for field in OHLC_FIELDS:
        append_values[field] = rows[field].to_numpy(dtype=np.float64) * ratio
    append_values["volume"] = rows["volume"].to_numpy(dtype=np.float64)
    append_values["change"] = rows["change"].to_numpy(dtype=np.float64)
    append_values["factor"] = np.ones(len(rows), dtype=np.float64)
    append_values["adj"] = rows["adj"].to_numpy(dtype=np.float64)

    combined_values: dict[str, np.ndarray] = {}
    for field in FIELDS:
        existing = _read_bin(_bin_path(root, plan.code, field))
        old = existing[1:].astype(np.float64)
        if field in OHLC_FIELDS and scale != 1.0:
            old *= scale
        combined_values[field] = np.concatenate((old, append_values[field]))

    combined_values["high"] = np.maximum.reduce((
        combined_values["high"],
        combined_values["open"],
        combined_values["close"],
    ))
    combined_values["low"] = np.minimum.reduce((
        combined_values["low"],
        combined_values["open"],
        combined_values["close"],
    ))

    payloads: dict[str, bytes] = {}
    for field in FIELDS:
        combined = combined_values[field].astype("<f4")
        payloads[field] = np.concatenate(
            (np.array([plan.state.start], dtype="<f4"), combined)
        ).tobytes()
    # Build all eight payloads before publishing the first one.  Source/bin
    # problems therefore cannot leave a stock with mixed old and new lengths.
    for field in FIELDS:
        _atomic_write_bytes(_bin_path(root, plan.code, field), payloads[field])
    return len(FIELDS), scale != 1.0


def repair_qlib_tail(
    qlib_root: Path | str,
    parquet_dir: Path | str,
    through: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    root = Path(qlib_root)
    parquet_root = Path(parquet_dir)
    with qlib_update_lock(root):
        calendar = _read_calendar(root)
        target = through or calendar[-1]
        try:
            target_iso = pd.Timestamp(target).strftime("%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid --through/--date value: {target!r}") from exc
        if target_iso not in calendar:
            raise RuntimeError(
                f"target date {target_iso} is not in the published calendar; tail repair cannot publish dates"
            )
        target_idx = calendar.index(target_iso)

        loaded: dict[str, pd.DataFrame] = {}

        def load(date_iso: str) -> pd.DataFrame:
            if date_iso not in loaded:
                loaded[date_iso] = _load_daily(
                    parquet_root / f"{date_iso.replace('-', '')}.parquet", date_iso
                )
            return loaded[date_iso]

        target_frame = load(target_iso)
        candidate_codes = sorted(set(target_frame["code"]) - BENCHMARK_CODES)
        states: dict[str, BinState] = {}
        envelope_only_states: dict[str, BinState] = {}
        missing_bins: list[str] = []
        already_current: list[str] = []
        for code in candidate_codes:
            stock_dir = root / "features" / code
            if not stock_dir.is_dir() or any(not _bin_path(root, code, field).exists() for field in FIELDS):
                missing_bins.append(code)
                continue
            state = _validate_bins(root, code, len(calendar))
            if state.end >= target_idx:
                if state.invalid_envelope_rows:
                    envelope_only_states[code] = state
                else:
                    already_current.append(code)
            else:
                states[code] = state

        if missing_bins:
            raise RuntimeError(
                "target-date stocks are missing one or more Qlib bins; full rebuild required: "
                f"count={len(missing_bins)}, sample={','.join(missing_bins[:10])}"
            )

        if states:
            earliest = min(state.end + 1 for state in states.values())
            for date_iso in calendar[earliest:target_idx + 1]:
                load(date_iso)
        combined = pd.concat(loaded.values(), ignore_index=True) if loaded else pd.DataFrame()
        plans: list[RepairPlan] = []
        suspension_rows_filled = 0
        for code, state in states.items():
            required_dates = calendar[state.end + 1:target_idx + 1]
            source_rows = combined[
                combined["code"].eq(code) & combined["date"].isin(required_dates)
            ].copy()
            source_rows = source_rows.sort_values("date")
            by_date = {
                str(row["date"]): row.to_dict()
                for _, row in source_rows.iterrows()
            }
            previous_close = state.last_raw_close
            previous_adj = state.last_adj
            repaired_rows: list[dict[str, object]] = []
            for date_iso in required_dates:
                row = by_date.get(date_iso)
                if row is None:
                    row = {
                        "code": code,
                        "date": date_iso,
                        "open": previous_close,
                        "close": previous_close,
                        "high": previous_close,
                        "low": previous_close,
                        "volume": 0.0,
                        "change": 0.0,
                        "adj": previous_adj,
                    }
                    suspension_rows_filled += 1
                else:
                    previous_close = float(row["close"])
                    previous_adj = float(row["adj"])
                repaired_rows.append(row)
            rows = pd.DataFrame(repaired_rows, columns=combined.columns)
            source_max = float(rows["adj"].max())
            # Stored adjustments are float32.  Treat float-rounding noise as
            # the same maximum so unchanged factors do not rescale history.
            new_max = (
                source_max
                if source_max > state.old_max_adj * (1.0 + 1e-7)
                else state.old_max_adj
            )
            plans.append(RepairPlan(code=code, state=state, rows=rows, new_max_adj=new_max))
        empty_rows = target_frame.iloc[0:0].copy()
        for code, state in envelope_only_states.items():
            plans.append(
                RepairPlan(
                    code=code,
                    state=state,
                    rows=empty_rows,
                    new_max_adj=state.old_max_adj,
                )
            )
        prospective = {
            plan.code: (plan.state.start, target_idx)
            for plan in plans
        }
        metadata, instrument_count = _metadata_payload(
            root,
            calendar,
            prospective,
            set(candidate_codes),
        )
        metadata_path = root / "instruments" / "all.txt"
        metadata_changed = metadata_path.read_text(encoding="utf-8") != metadata

        bins_replaced = 0
        rescaled = 0
        if not dry_run:
            for plan in plans:
                count, was_rescaled = _write_plan(root, plan)
                bins_replaced += count
                rescaled += int(was_rescaled)
            if metadata_changed:
                _atomic_write_text(metadata_path, metadata)

        return {
            "status": "dry_run" if dry_run else "ok",
            "through": target_iso,
            "calendar_tail": calendar[-1],
            "candidate_stocks": len(candidate_codes),
            "repaired_stocks": len(plans),
            "tail_appended_stocks": len(states),
            "already_current": len(already_current),
            "missing_bin_stocks": len(missing_bins),
            "missing_bin_sample": missing_bins[:10],
            "dates_loaded": sorted(loaded),
            "source_rows_loaded": int(sum(len(frame) for frame in loaded.values())),
            "bins_replaced": bins_replaced if not dry_run else 0,
            "rescaled_stocks": rescaled if not dry_run else sum(
                plan.new_max_adj != plan.state.old_max_adj for plan in plans
            ),
            "legacy_envelope_rows_repaired": sum(
                plan.state.invalid_envelope_rows for plan in plans
            ),
            "suspension_rows_filled": suspension_rows_filled,
            "instrument_count": instrument_count,
            "metadata_changed": metadata_changed,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qlib-root",
        default=os.environ.get("QLIB_DATA_PATH", "/app/qlib_data/cn_data"),
        help="Qlib cn_data root containing calendars, instruments and features",
    )
    parser.add_argument(
        "--parquet-dir",
        default=os.environ.get("PARQUET_DIR", "/app/qlib_data/csv_tmp/tushare_daily"),
        help="Directory containing YYYYMMDD.parquet daily files",
    )
    parser.add_argument("--through", "--date", dest="through", help="Published calendar date to repair through")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without replacing files")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = repair_qlib_tail(args.qlib_root, args.parquet_dir, args.through, args.dry_run)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
