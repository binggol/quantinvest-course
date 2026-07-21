"""Strict refresh runner for the older page-level JSON data sources.

Each exporter still writes its normal local ``data/*.json`` file.  This runner
backs that file up first, redirects the exporter's legacy NAS copy into an
isolated temporary directory, validates the new local snapshot, and only then
publishes it atomically to the shared directory.  A failed job is rolled back
without preventing the remaining jobs in the selected group from running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import refresh_daily_console as daily_console  # noqa: E402


DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "page_refresh_state.json"
DEFAULT_QLIB_DATA_DIR = Path(r"C:\qlib_data\cn_data")
MAX_ITEMS = 100_000
MAX_COUNT = 1_000_000

Validator = Callable[[dict[str, Any], date], int]
_PAGE_REFRESH_THREAD_LOCK = threading.Lock()


class PageRefreshAlreadyRunning(RuntimeError):
    pass


@contextmanager
def page_refresh_lock(
    data_dir: Path,
    *,
    wait_seconds: float = 0,
    poll_seconds: float = 1,
):
    """Serialize page refresh groups across Task Scheduler and manual runs."""
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    remaining = max(0.0, deadline - time.monotonic())
    acquired = (
        _PAGE_REFRESH_THREAD_LOCK.acquire(timeout=remaining)
        if wait_seconds > 0
        else _PAGE_REFRESH_THREAD_LOCK.acquire(blocking=False)
    )
    if not acquired:
        raise PageRefreshAlreadyRunning("another page refresh is running in this process")
    handle = None
    locked = False
    lock_path = Path(data_dir) / ".page_refresh.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise PageRefreshAlreadyRunning(
                            f"another page refresh owns {lock_path}"
                        ) from exc
                    time.sleep(min(poll_seconds, max(0.01, deadline - time.monotonic())))
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise PageRefreshAlreadyRunning(
                            f"another page refresh owns {lock_path}"
                        ) from exc
                    time.sleep(min(poll_seconds, max(0.01, deadline - time.monotonic())))
        locked = True
        yield lock_path
    finally:
        if handle is not None:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        _PAGE_REFRESH_THREAD_LOCK.release()


@dataclass(frozen=True)
class JobSpec:
    key: str
    script: str
    output: str
    validator: Validator


def _integer(
    value: Any, field: str, *, minimum: int = 0, maximum: int = MAX_COUNT
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} is outside the allowed range: {value}")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _items(payload: dict[str, Any], field: str = "items") -> list[dict[str, Any]]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    if len(value) > MAX_ITEMS:
        raise ValueError(f"{field} has an unreasonable number of rows: {len(value)}")
    if any(not isinstance(row, dict) for row in value):
        raise ValueError(f"every {field} row must be an object")
    return value


def _updated_today(payload: dict[str, Any], today: date) -> None:
    updated = _text(payload.get("updated"), "updated")
    if not updated.startswith(today.isoformat()):
        raise ValueError(f"updated is not today: {updated}")


def _iso_date(value: Any, field: str) -> date:
    raw = _text(value, field)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date: {raw}") from exc


def _today_as_of(payload: dict[str, Any], today: date) -> None:
    observed = _iso_date(payload.get("as_of"), "as_of")
    if observed != today:
        raise ValueError(f"as_of is not today: {observed.isoformat()}")


def _recent_as_of(payload: dict[str, Any], today: date, *, max_age_days: int = 14) -> None:
    observed = _iso_date(payload.get("as_of"), "as_of")
    age = (today - observed).days
    if age < 0 or age > max_age_days:
        raise ValueError(
            f"as_of is outside the freshness window: {observed.isoformat()}"
        )


def _exact_count(
    payload: dict[str, Any], items: list[dict[str, Any]], field: str = "n"
) -> int:
    count = _integer(payload.get(field), field)
    if count != len(items):
        raise ValueError(f"{field}={count} does not match {len(items)} rows")
    return count


def _subset_count(payload: dict[str, Any], field: str, total: int) -> None:
    count = _integer(payload.get(field), field)
    if count > total:
        raise ValueError(f"{field}={count} exceeds total={total}")


def _validate_event(payload: dict[str, Any], today: date) -> int:
    _updated_today(payload, today)
    _today_as_of(payload, today)
    cats = payload.get("cats")
    if not isinstance(cats, dict) or not cats or len(cats) > 50:
        raise ValueError("cats must be a non-empty object with at most 50 categories")
    total = 0
    for name, category in cats.items():
        _text(name, "category name")
        if not isinstance(category, dict):
            raise ValueError(f"category {name!r} must be an object")
        _text(category.get("desc"), f"cats.{name}.desc")
        window = _integer(category.get("win_days"), f"cats.{name}.win_days")
        if window < 1 or window > 3_660:
            raise ValueError(f"cats.{name}.win_days is unreasonable: {window}")
        rows = _items(category)
        count = _exact_count(category, rows)
        _subset_count(category, "n_window", count)
        total += count
    if total > MAX_COUNT:
        raise ValueError(f"event total is unreasonable: {total}")
    if total <= 0:
        raise ValueError("event scan returned no announcements")
    return total


def _validate_windowed(
    payload: dict[str, Any], today: date, *, subset_field: str
) -> int:
    _updated_today(payload, today)
    _today_as_of(payload, today)
    rows = _items(payload)
    count = _exact_count(payload, rows)
    if count <= 0:
        raise ValueError("windowed event scan returned no announcements")
    _subset_count(payload, subset_field, count)
    return count


def _validate_inquiry(payload: dict[str, Any], today: date) -> int:
    return _validate_windowed(payload, today, subset_field="n_window")


def _validate_investigation(payload: dict[str, Any], today: date) -> int:
    return _validate_windowed(payload, today, subset_field="n_blacklist")


def _validate_repo_cancel(payload: dict[str, Any], today: date) -> int:
    return _validate_windowed(payload, today, subset_field="n_in_window")


def _validate_commit(payload: dict[str, Any], today: date) -> int:
    return _validate_windowed(payload, today, subset_field="n_window")


def _validate_leverage(payload: dict[str, Any], today: date) -> int:
    _updated_today(payload, today)
    _recent_as_of(payload, today)
    rows = _items(payload)
    count = _exact_count(payload, rows)
    if count <= 0:
        raise ValueError("leverage scan returned no rows")
    _number(payload.get("thr_pct"), "thr_pct")
    return count


def _validate_lhb(payload: dict[str, Any], today: date) -> int:
    _updated_today(payload, today)
    _recent_as_of(payload, today)
    rows = _items(payload)
    if "n" not in payload:
        raise ValueError("LHB scan returned no rows")
    count = _exact_count(payload, rows)
    if count <= 0:
        raise ValueError("LHB scan returned no rows")
    if count:
        _number(payload.get("thr_pct"), "thr_pct")
    return count


def _validate_bigbath(payload: dict[str, Any], today: date) -> int:
    _updated_today(payload, today)
    _today_as_of(payload, today)
    rows = _items(payload)
    total = _integer(payload.get("n"), "n")
    if total < len(rows):
        raise ValueError(f"n={total} is smaller than the {len(rows)} visible rows")
    _subset_count(payload, "n_rebound", total)
    source_health = payload.get("source_health")
    if not isinstance(source_health, dict):
        raise ValueError("bigbath source_health must be an object")
    if _integer(source_health.get("forecast_codes"), "source_health.forecast_codes") <= 0:
        raise ValueError("bigbath forecast cache contains no stock histories")
    _text(source_health.get("forecast_cache"), "source_health.forecast_cache")
    return total


def _validate_late(payload: dict[str, Any], today: date) -> int:
    _updated_today(payload, today)
    period = _text(payload.get("rpt_period"), "rpt_period")
    if len(period) != 8 or not period.isdigit():
        raise ValueError(f"rpt_period must be YYYYMMDD: {period}")
    try:
        datetime.strptime(period, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"rpt_period is invalid: {period}") from exc
    if not isinstance(payload.get("in_season"), bool):
        raise ValueError("in_season must be boolean")
    rows = _items(payload)
    if "n" in payload:
        count = _exact_count(payload, rows)
    else:
        if rows or not str(payload.get("msg") or "").strip():
            raise ValueError("an empty disclosure snapshot must include an explanatory msg")
        count = 0
    if "as_of" in payload:
        _recent_as_of(payload, today)
    elif count:
        raise ValueError("a non-empty disclosure snapshot must include as_of")
    return count


def _validate_foreign(payload: dict[str, Any], today: date) -> int:
    _updated_today(payload, today)
    _recent_as_of(payload, today)
    schedule = payload.get("schedule")
    if not isinstance(schedule, list) or not schedule or len(schedule) > 200:
        raise ValueError("schedule must contain between 1 and 200 entries")
    latest_effective: date | None = None
    for index, row in enumerate(schedule):
        if not isinstance(row, dict):
            raise ValueError(f"schedule[{index}] must be an object")
        _text(row.get("index"), f"schedule[{index}].index")
        announced = _iso_date(row.get("ann_date"), f"schedule[{index}].ann_date")
        effective = _iso_date(row.get("eff_date"), f"schedule[{index}].eff_date")
        if effective < announced:
            raise ValueError(f"schedule[{index}] is effective before announcement")
        if _integer(
            row.get("days_to_ann"),
            f"schedule[{index}].days_to_ann",
            minimum=-10_000,
            maximum=10_000,
        ) != (announced - today).days:
            raise ValueError(f"schedule[{index}].days_to_ann is inconsistent")
        if _integer(
            row.get("days_to_eff"),
            f"schedule[{index}].days_to_eff",
            minimum=-10_000,
            maximum=10_000,
        ) != (effective - today).days:
            raise ValueError(f"schedule[{index}].days_to_eff is inconsistent")
        latest_effective = max(latest_effective or effective, effective)
    if latest_effective is None or latest_effective < today:
        raise ValueError("foreign-index schedule has no current or future effective date")
    candidates = _items(payload, "candidates")
    count = _exact_count(payload, candidates, "n_cand")
    _text(payload.get("disclaimer"), "disclaimer")
    return count


JOBS: dict[str, JobSpec] = {
    "event": JobSpec("event", "export_event_avoid.py", "event_avoid.json", _validate_event),
    "inquiry": JobSpec(
        "inquiry", "export_inquiry_letter.py", "inquiry_letter.json", _validate_inquiry
    ),
    "investigation": JobSpec(
        "investigation",
        "export_investigation_avoid.py",
        "investigation_avoid.json",
        _validate_investigation,
    ),
    "repo_cancel": JobSpec(
        "repo_cancel", "export_repo_cancel.py", "repo_cancel.json", _validate_repo_cancel
    ),
    "commit": JobSpec(
        "commit", "export_commit_nosell.py", "commit_nosell.json", _validate_commit
    ),
    "leverage": JobSpec(
        "leverage", "export_leverage_avoid.py", "leverage_avoid.json", _validate_leverage
    ),
    "lhb": JobSpec("lhb", "export_lhb_avoid.py", "lhb_avoid.json", _validate_lhb),
    "bigbath": JobSpec("bigbath", "export_bigbath.py", "bigbath.json", _validate_bigbath),
    "late": JobSpec(
        "late", "export_late_disclosure.py", "late_disclosure.json", _validate_late
    ),
    "foreign": JobSpec(
        "foreign",
        "export_foreign_inclusion.py",
        "foreign_inclusion.json",
        _validate_foreign,
    ),
}

GROUPS: dict[str, tuple[str, ...]] = {
    "company-events": ("event", "inquiry", "investigation", "repo_cancel", "commit"),
    "closing-risk": ("leverage", "lhb", "bigbath"),
    "weekly-sources": ("late", "foreign"),
    "all": tuple(JOBS),
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False
    ).encode("utf-8")
    temp_path: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _restore_local(output: Path, backup: Path | None) -> None:
    if backup is None:
        output.unlink(missing_ok=True)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output.with_name(f".{output.name}.rollback-{os.getpid()}.tmp")
    try:
        shutil.copy2(backup, temp_path)
        os.replace(temp_path, output)
    finally:
        temp_path.unlink(missing_ok=True)


def expand_selection(selection: str | Sequence[str]) -> tuple[str, ...]:
    requested = (selection,) if isinstance(selection, str) else tuple(selection)
    expanded: list[str] = []
    for name in requested:
        if name in GROUPS:
            candidates = GROUPS[name]
        elif name in JOBS:
            candidates = (name,)
        else:
            raise ValueError(f"unknown page-source job or group: {name}")
        for key in candidates:
            if key not in expanded:
                expanded.append(key)
    if not expanded:
        raise ValueError("at least one page-source job is required")
    return tuple(expanded)


def _run_job(
    spec: JobSpec,
    *,
    root: Path,
    data_dir: Path,
    shared_dir: Path,
    python: Path,
    qlib_data_dir: Path,
    staging_dir: Path,
    timeout: int,
    run_command: Callable[..., Any],
    publisher: Callable[[Path, Path], None],
) -> dict[str, Any]:
    started_at = _now()
    output = data_dir / spec.output
    script = root / "scripts" / spec.script
    backup: Path | None = None
    old_digest: str | None = None
    if output.is_file():
        old_digest = _sha256(output)
        backup = staging_dir / "backup" / spec.output
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, backup)

    export_copy_dir = staging_dir / "export-copy"
    export_copy_dir.mkdir(parents=True, exist_ok=True)
    child_env = {
        **os.environ,
        "QI_EXPORT_NAS_DIR": str(export_copy_dir),
        "QI_QLIB_DATA_DIR": str(qlib_data_dir),
    }
    result: dict[str, Any] = {
        "status": "running",
        "started_at": started_at,
        "script": spec.script,
        "output": spec.output,
        "old_sha256": old_digest,
    }
    try:
        if not script.is_file():
            raise FileNotFoundError(f"exporter is missing: {script}")
        if spec.key == "bigbath":
            forecast_cache = Path(
                os.environ.get("QI_FORECAST_CACHE", r"C:\rdagent\_forecast_1000.pkl")
            )
            if not forecast_cache.is_file() or forecast_cache.stat().st_size <= 0:
                raise FileNotFoundError(
                    f"forecast cache is unavailable: {forecast_cache}"
                )
            cache_age_days = (
                datetime.now().timestamp() - forecast_cache.stat().st_mtime
            ) / 86_400
            if cache_age_days > 7:
                raise RuntimeError(
                    f"forecast cache is stale: {cache_age_days:.1f} days old"
                )
            child_env["QI_FORECAST_CACHE"] = str(forecast_cache)
        completed = run_command(
            [str(python), str(script)],
            cwd=str(root),
            env=child_env,
            timeout=timeout,
            check=False,
        )
        returncode = getattr(completed, "returncode", None)
        if returncode != 0:
            raise RuntimeError(f"exporter exited with code {returncode}")
        if not output.is_file():
            raise FileNotFoundError(f"exporter did not create {output}")
        new_digest = _sha256(output)
        if new_digest == old_digest:
            raise RuntimeError(f"exporter did not materially change {spec.output}")

        raw_payload = daily_console.validate_json(output)
        if not isinstance(raw_payload, dict):
            raise ValueError(f"{spec.output} JSON root must be an object")
        row_count = spec.validator(raw_payload, datetime.now().date())

        destination = shared_dir / spec.output
        publisher(output, destination)
        result.update(
            {
                "status": "success",
                "finished_at": _now(),
                "new_sha256": new_digest,
                "rows": row_count,
                "published_to": str(destination),
            }
        )
        return result
    except Exception as exc:
        rollback_error = ""
        try:
            _restore_local(output, backup)
        except Exception as rollback_exc:
            rollback_error = f"; rollback failed: {type(rollback_exc).__name__}: {rollback_exc}"
        result.update(
            {
                "status": "error",
                "finished_at": _now(),
                "error": f"{type(exc).__name__}: {exc}{rollback_error}",
            }
        )
        return result


def _run_selection_unlocked(
    selection: str | Sequence[str],
    *,
    python: Path | None = None,
    shared_dir: Path | None = None,
    qlib_data_dir: Path | None = None,
    root: Path = ROOT,
    data_dir: Path | None = None,
    state_path: Path | None = None,
    timeout: int = 1_800,
    run_command: Callable[..., Any] | None = None,
    publisher: Callable[[Path, Path], None] | None = None,
) -> int:
    keys = expand_selection(selection)
    python = Path(python or sys.executable)
    shared_dir = Path(shared_dir or daily_console.resolve_shared_dir())
    qlib_data_dir = Path(
        qlib_data_dir
        or os.environ.get("QI_QLIB_DATA_DIR", "").strip()
        or DEFAULT_QLIB_DATA_DIR
    )
    data_dir = Path(data_dir or (root / "data"))
    state_path = Path(state_path or (data_dir / "page_refresh_state.json"))
    run_command = run_command or subprocess.run
    publisher = publisher or daily_console.atomic_publish
    data_dir.mkdir(parents=True, exist_ok=True)

    state: dict[str, Any] = {
        "version": 1,
        "status": "running",
        "selection": list(keys),
        "started_at": _now(),
        "updated_at": _now(),
        "shared_dir": str(shared_dir),
        "qlib_data_dir": str(qlib_data_dir),
        "jobs": {key: {"status": "queued"} for key in keys},
    }
    _atomic_write_json(state_path, state)

    failures: list[str] = []
    state_write_failed = False
    with tempfile.TemporaryDirectory(prefix=".page-refresh-", dir=data_dir) as raw_stage:
        run_stage = Path(raw_stage)
        for key in keys:
            result = _run_job(
                JOBS[key],
                root=root,
                data_dir=data_dir,
                shared_dir=shared_dir,
                python=python,
                qlib_data_dir=qlib_data_dir,
                staging_dir=run_stage / key,
                timeout=timeout,
                run_command=run_command,
                publisher=publisher,
            )
            state["jobs"][key] = result
            state["updated_at"] = _now()
            if result["status"] == "error":
                failures.append(key)
                print(f"[page-refresh] ERROR {key}: {result['error']}", file=sys.stderr)
            else:
                print(
                    f"[page-refresh] OK {key}: {result['rows']} rows -> "
                    f"{result['published_to']}"
                )
            try:
                _atomic_write_json(state_path, state)
            except Exception as exc:
                state_write_failed = True
                print(
                    f"[page-refresh] ERROR state: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    state["status"] = "error" if failures or state_write_failed else "success"
    state["finished_at"] = _now()
    state["updated_at"] = state["finished_at"]
    state["success_count"] = sum(
        1 for value in state["jobs"].values() if value.get("status") == "success"
    )
    state["error_count"] = len(failures) + int(state_write_failed)
    _atomic_write_json(state_path, state)
    if failures:
        print(
            f"[page-refresh] completed with {len(failures)} failed job(s): "
            + ", ".join(failures),
            file=sys.stderr,
        )
    return 1 if failures or state_write_failed else 0


def run_selection(
    selection: str | Sequence[str],
    *,
    python: Path | None = None,
    shared_dir: Path | None = None,
    qlib_data_dir: Path | None = None,
    root: Path = ROOT,
    data_dir: Path | None = None,
    state_path: Path | None = None,
    timeout: int = 1_800,
    lock_wait_seconds: float = 0,
    run_command: Callable[..., Any] | None = None,
    publisher: Callable[[Path, Path], None] | None = None,
) -> int:
    resolved_data_dir = Path(data_dir or (root / "data"))
    try:
        with page_refresh_lock(
            resolved_data_dir,
            wait_seconds=lock_wait_seconds,
        ):
            return _run_selection_unlocked(
                selection,
                python=python,
                shared_dir=shared_dir,
                qlib_data_dir=qlib_data_dir,
                root=root,
                data_dir=resolved_data_dir,
                state_path=state_path,
                timeout=timeout,
                run_command=run_command,
                publisher=publisher,
            )
    except PageRefreshAlreadyRunning as exc:
        if state_path is not None:
            _atomic_write_json(Path(state_path), {
                "version": 1,
                "status": "busy",
                "selection": list(expand_selection(selection)),
                "updated_at": _now(),
                "error": str(exc),
            })
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "selection",
        nargs="?",
        default="all",
        choices=tuple(GROUPS) + tuple(JOBS),
        help="job group or individual page source (default: all)",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--shared-dir", type=Path, default=None)
    parser.add_argument("--qlib-data-dir", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=1_800)
    parser.add_argument("--lock-wait-seconds", type=float, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_selection(
        args.selection,
        python=args.python,
        shared_dir=args.shared_dir,
        qlib_data_dir=args.qlib_data_dir,
        state_path=args.state,
        timeout=args.timeout,
        lock_wait_seconds=args.lock_wait_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
