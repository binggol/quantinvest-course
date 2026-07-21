"""Fail-closed validation for RD-Agent live prediction inputs and outputs.

The live predictor must never turn a stale calendar, a partial point-in-time
index file, or a thin prediction frame into a newly published buy list.  This
module deliberately has no Qlib dependency so the same checks can be exercised
by lightweight regression tests and deployment preflights.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


EXPECTED_UNIVERSE_SIZE = {
    "csi300": 300,
    "csi500": 500,
    "csi1000": 1000,
}
# The wall-clock guard is only a fallback for direct/manual predictor runs.
# Fourteen days covers normal Spring Festival / National Day closures without
# silently accepting a genuinely abandoned data directory.  Production watcher
# runs additionally bind the local tail to the freshly mirrored source tail.
DEFAULT_MAX_CALENDAR_AGE_DAYS = 14
DEFAULT_MIN_PREDICTION_COVERAGE = 0.90
DEFAULT_MARKET_DATA_ROOT = Path(
    "/mnt/z/claude/qlib/data/csv_tmp/tushare_daily"
)


class PredictionPreflightError(RuntimeError):
    """Raised when live prediction data is not safe to publish."""


@dataclass(frozen=True)
class UniverseAudit:
    universe: str
    market_date: dt.date
    expected_count: int
    active_codes: frozenset[str]
    calendar_age_days: int
    freshness_basis: str


@dataclass(frozen=True)
class PredictionCoverageAudit:
    market_date: dt.date
    expected_count: int
    predicted_count: int
    coverage: float


def _normalise_code(value: object) -> str:
    return str(value).strip().lower()


def _parse_iso_date(value: object, label: str) -> dt.date:
    text = str(value).strip()
    try:
        return dt.date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise PredictionPreflightError(f"invalid {label}: {text!r}") from exc


def _read_calendar_tail(path: Path) -> dt.date:
    if not path.is_file():
        raise PredictionPreflightError(f"missing Qlib calendar: {path}")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise PredictionPreflightError(f"empty Qlib calendar: {path}")
    tail = _parse_iso_date(lines[-1], "Qlib calendar tail")
    if len(lines) > 1:
        previous = _parse_iso_date(lines[-2], "Qlib calendar penultimate date")
        if previous >= tail:
            raise PredictionPreflightError(
                f"Qlib calendar tail is not strictly increasing: {previous} -> {tail}"
            )
    return tail


def _active_constituents(path: Path, market_date: dt.date) -> tuple[int, frozenset[str]]:
    if not path.is_file():
        raise PredictionPreflightError(f"missing Qlib universe file: {path}")
    active_rows = 0
    active_codes: set[str] = set()
    malformed = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        if len(parts) < 3:
            malformed += 1
            continue
        code = _normalise_code(parts[0])
        try:
            start = _parse_iso_date(parts[1], "constituent start date")
            end = _parse_iso_date(parts[2], "constituent end date")
        except PredictionPreflightError:
            malformed += 1
            continue
        if not code or start > end:
            malformed += 1
            continue
        if start <= market_date <= end:
            active_rows += 1
            active_codes.add(code)
    if malformed:
        raise PredictionPreflightError(
            f"{path.name} contains {malformed} malformed membership row(s)"
        )
    return active_rows, frozenset(active_codes)


def latest_market_date_from_parquet(path: str | Path) -> dt.date:
    """Return the newest non-empty ``YYYYMMDD*.parquet`` market snapshot."""

    root = Path(path)
    if not root.is_dir():
        raise PredictionPreflightError(f"market-data directory is unavailable: {root}")
    dates: list[dt.date] = []
    for item in root.glob("*.parquet"):
        if not item.is_file() or item.stat().st_size <= 0:
            continue
        prefix = item.stem[:8]
        if len(prefix) != 8 or not prefix.isdigit():
            continue
        try:
            dates.append(dt.datetime.strptime(prefix, "%Y%m%d").date())
        except ValueError:
            continue
    if not dates:
        raise PredictionPreflightError(
            f"no non-empty dated market parquet is available in {root}"
        )
    return max(dates)


def validate_universe_snapshot(
    qlib_root: str | Path,
    universe: str,
    *,
    now: dt.date | dt.datetime | None = None,
    expected_market_date: str | dt.date | None = None,
    expected_market_date_basis: str = "explicit_expected_market_date",
    max_calendar_age_days: int = DEFAULT_MAX_CALENDAR_AGE_DAYS,
) -> UniverseAudit:
    """Validate calendar freshness and exact PIT membership for ``universe``."""

    universe = str(universe).strip().lower()
    if universe not in EXPECTED_UNIVERSE_SIZE:
        raise PredictionPreflightError(f"unsupported live universe: {universe!r}")
    if not isinstance(max_calendar_age_days, int) or not 0 <= max_calendar_age_days <= 30:
        raise PredictionPreflightError(
            f"max_calendar_age_days must be an integer in [0, 30], got {max_calendar_age_days!r}"
        )

    root = Path(qlib_root)
    market_date = _read_calendar_tail(root / "calendars" / "day.txt")
    if isinstance(now, dt.datetime):
        today = now.date()
    else:
        today = now or dt.date.today()
    age_days = (today - market_date).days
    if age_days < 0:
        raise PredictionPreflightError(
            f"Qlib calendar tail {market_date} is in the future relative to {today}"
        )
    if age_days > max_calendar_age_days:
        raise PredictionPreflightError(
            f"Qlib calendar is stale: tail={market_date}, today={today}, "
            f"age={age_days}d > {max_calendar_age_days}d"
        )

    freshness_basis = "wall_clock_guard"
    if expected_market_date not in (None, ""):
        expected = (
            expected_market_date
            if isinstance(expected_market_date, dt.date)
            else _parse_iso_date(expected_market_date, "expected market date")
        )
        if market_date != expected:
            raise PredictionPreflightError(
                f"local Qlib calendar tail {market_date} != explicit expected date {expected}"
            )
        basis = str(expected_market_date_basis).strip() or "explicit_expected_market_date"
        freshness_basis = f"{basis}+wall_clock_guard"

    expected_count = EXPECTED_UNIVERSE_SIZE[universe]
    active_rows, active_codes = _active_constituents(
        root / "instruments" / f"{universe}.txt", market_date
    )
    if active_rows != expected_count or len(active_codes) != expected_count:
        raise PredictionPreflightError(
            f"{universe} PIT membership is incomplete on {market_date}: "
            f"rows={active_rows}, unique={len(active_codes)}, expected={expected_count}"
        )

    return UniverseAudit(
        universe=universe,
        market_date=market_date,
        expected_count=expected_count,
        active_codes=active_codes,
        calendar_age_days=age_days,
        freshness_basis=freshness_basis,
    )


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PredictionPreflightError(f"{name} must be numeric, got {raw!r}") from exc
    return value


def validate_prediction_frame(
    pred,
    universe_audit: UniverseAudit,
    *,
    min_coverage: float = DEFAULT_MIN_PREDICTION_COVERAGE,
    min_candidates: int = 50,
):
    """Return the clean latest-day score frame after strict coverage checks.

    ``pred`` is intentionally duck-typed as a pandas DataFrame.  Keeping pandas
    out of this module's import path lets the file-level preflight run in a
    minimal deployment environment.
    """

    if not 0 < min_coverage <= 1:
        raise PredictionPreflightError(
            f"min prediction coverage must be in (0, 1], got {min_coverage!r}"
        )
    if min_candidates < 1:
        raise PredictionPreflightError("min_candidates must be positive")
    if pred is None or getattr(pred, "empty", True):
        raise PredictionPreflightError("prediction frame is empty")
    if "score" not in getattr(pred, "columns", []):
        raise PredictionPreflightError("prediction frame has no score column")
    index = getattr(pred, "index", None)
    if index is None or getattr(index, "nlevels", 0) < 2:
        raise PredictionPreflightError(
            "prediction frame must use a (datetime, instrument) MultiIndex"
        )

    dates = index.get_level_values(0)
    try:
        latest_timestamp = max(dates)
        latest_date = latest_timestamp.date()
    except Exception as exc:
        raise PredictionPreflightError("prediction frame has invalid dates") from exc
    if latest_date != universe_audit.market_date:
        raise PredictionPreflightError(
            f"prediction date {latest_date} != Qlib market date {universe_audit.market_date}"
        )

    try:
        today = pred.xs(latest_timestamp, level=0).copy()
    except Exception as exc:
        raise PredictionPreflightError(
            f"cannot extract prediction rows for {latest_timestamp}"
        ) from exc
    normalised_codes = [_normalise_code(value) for value in today.index]
    if len(normalised_codes) != len(set(normalised_codes)):
        raise PredictionPreflightError("prediction frame contains duplicate instruments")
    today.index = normalised_codes

    outside = sorted(set(normalised_codes) - set(universe_audit.active_codes))
    if outside:
        sample = ",".join(outside[:5])
        raise PredictionPreflightError(
            f"prediction frame contains {len(outside)} code(s) outside "
            f"{universe_audit.universe} PIT universe: {sample}"
        )

    finite_mask = today["score"].map(
        lambda value: bool(value is not None and math.isfinite(float(value)))
        if _is_float_like(value)
        else False
    )
    clean = today.loc[finite_mask].copy()
    predicted_count = len(clean)
    coverage = predicted_count / universe_audit.expected_count
    required = max(min_candidates, math.ceil(universe_audit.expected_count * min_coverage))
    if predicted_count < required:
        raise PredictionPreflightError(
            f"{universe_audit.universe} prediction coverage is insufficient on "
            f"{universe_audit.market_date}: valid={predicted_count}/"
            f"{universe_audit.expected_count} ({coverage:.1%}), required>={required}"
        )

    clean = clean.sort_values("score", ascending=False)
    clean.attrs["coverage_audit"] = PredictionCoverageAudit(
        market_date=latest_date,
        expected_count=universe_audit.expected_count,
        predicted_count=predicted_count,
        coverage=coverage,
    )
    return clean


def _is_float_like(value: object) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace one text artifact while preserving the old file on error."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def audit_from_environment(qlib_root: str | Path, universe: str) -> UniverseAudit:
    """Run the file preflight using the live predictor environment contract."""

    raw_age = os.environ.get("RDAGENT_MAX_CALENDAR_AGE_DAYS", "").strip()
    try:
        max_age = int(raw_age) if raw_age else DEFAULT_MAX_CALENDAR_AGE_DAYS
    except ValueError as exc:
        raise PredictionPreflightError(
            f"RDAGENT_MAX_CALENDAR_AGE_DAYS must be an integer, got {raw_age!r}"
        ) from exc
    expected_market_date = os.environ.get("RDAGENT_EXPECTED_MARKET_DATE", "").strip()
    expected_basis = os.environ.get(
        "RDAGENT_EXPECTED_MARKET_DATE_BASIS", "explicit_expected_market_date"
    ).strip()
    if not expected_market_date:
        configured_root = os.environ.get("RDAGENT_MARKET_DATA_ROOT", "").strip()
        market_root = Path(configured_root) if configured_root else DEFAULT_MARKET_DATA_ROOT
        if market_root.exists():
            expected_market_date = latest_market_date_from_parquet(market_root).isoformat()
            expected_basis = "latest_market_parquet"
    return validate_universe_snapshot(
        qlib_root,
        universe,
        expected_market_date=expected_market_date,
        expected_market_date_basis=expected_basis,
        max_calendar_age_days=max_age,
    )


def prediction_coverage_from_environment(pred, audit: UniverseAudit, *, top_k: int):
    min_coverage = _read_float_env(
        "RDAGENT_MIN_PREDICTION_COVERAGE", DEFAULT_MIN_PREDICTION_COVERAGE
    )
    return validate_prediction_frame(
        pred,
        audit,
        min_coverage=min_coverage,
        min_candidates=top_k,
    )


__all__ = [
    "DEFAULT_MAX_CALENDAR_AGE_DAYS",
    "DEFAULT_MIN_PREDICTION_COVERAGE",
    "DEFAULT_MARKET_DATA_ROOT",
    "EXPECTED_UNIVERSE_SIZE",
    "PredictionCoverageAudit",
    "PredictionPreflightError",
    "UniverseAudit",
    "atomic_write_text",
    "audit_from_environment",
    "latest_market_date_from_parquet",
    "prediction_coverage_from_environment",
    "validate_prediction_frame",
    "validate_universe_snapshot",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qlib-root", required=True)
    parser.add_argument("--universe", choices=sorted(EXPECTED_UNIVERSE_SIZE), required=True)
    parser.add_argument("--expected-market-date", default="")
    parser.add_argument(
        "--expected-market-date-basis", default="explicit_expected_market_date"
    )
    parser.add_argument(
        "--max-calendar-age-days", type=int, default=DEFAULT_MAX_CALENDAR_AGE_DAYS
    )
    args = parser.parse_args(argv)
    audit = validate_universe_snapshot(
        args.qlib_root,
        args.universe,
        expected_market_date=args.expected_market_date,
        expected_market_date_basis=args.expected_market_date_basis,
        max_calendar_age_days=args.max_calendar_age_days,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "universe": audit.universe,
                "market_date": audit.market_date.isoformat(),
                "expected_count": audit.expected_count,
                "calendar_age_days": audit.calendar_age_days,
                "freshness_basis": audit.freshness_basis,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PredictionPreflightError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
