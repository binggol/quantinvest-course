"""Refresh time-sensitive data used by the daily operations console.

This runner is intentionally independent from ``watch_predict_pc.ps1`` so a
long RD-Agent mining process cannot delay the 09:20/14:35 decision snapshots.
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
import time
import uuid
from datetime import date, datetime
from pathlib import Path

try:
    from scripts.process_lock import process_lock
except ImportError:  # direct ``python scripts/refresh_daily_console.py`` execution
    from process_lock import process_lock


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RDAGENT_DIR = Path(r"C:\rdagent")
DEFAULT_NAS_APP_DATA_DIR = Path(r"\/app/data")
MAPPED_SHARED_DIR = Path(r"Z:\claude\qlib\data\csv_tmp")
UNC_SHARED_DIR = Path(
    r"\/app/qlib_data\csv_tmp"
)
DEFAULT_SNOWBALL_XLSX = Path(
    r"\\your-nas\share\data.xlsx"  # TODO: 改为你的文件路径
)


def resolve_shared_dir() -> Path:
    configured = os.environ.get("QI_SHARED_DIR", "").strip()
    if configured:
        return Path(configured)
    return MAPPED_SHARED_DIR if MAPPED_SHARED_DIR.is_dir() else UNC_SHARED_DIR


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def validate_json(path: Path) -> dict | list:
    payload = json.loads(
        path.read_text(encoding="utf-8-sig"), parse_constant=_reject_json_constant
    )
    if not isinstance(payload, (dict, list)):
        raise ValueError(f"JSON root must be an object or array: {path}")
    return payload


def atomic_publish(source: Path, destination: Path) -> None:
    payload = source.read_bytes()
    json.loads(payload.decode("utf-8-sig"), parse_constant=_reject_json_constant)
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"shared directory unavailable: {destination.parent}")
    temp_path: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def atomic_copy(source: Path, destination: Path) -> None:
    """Atomically publish a non-JSON support file such as a compressed cache."""
    if not source.is_file() or source.stat().st_size <= 0:
        raise FileNotFoundError(f"source file unavailable or empty: {source}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"destination directory unavailable: {destination.parent}")
    temp_path: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as target, source.open("rb") as origin:
            shutil.copyfileobj(origin, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _today() -> str:
    return datetime.now().date().isoformat()


def _require_today(payload: dict, *fields: str) -> None:
    value = next((payload.get(field) for field in fields if payload.get(field)), None)
    if not str(value or "").startswith(_today()):
        raise RuntimeError(
            f"output timestamp is not today ({'/'.join(fields)}={value!r})"
        )


def _date_from_value(value: object) -> date | None:
    raw = str(value or "").strip().replace("/", "-")
    for fmt, width in (("%Y-%m-%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(raw[:width], fmt).date()
        except ValueError:
            continue
    return None


def _require_recent_market_date(value: object, *, max_age_days: int = 4) -> None:
    observed = _date_from_value(value)
    if observed is None:
        raise RuntimeError(f"missing market date: {value!r}")
    age = (datetime.now().date() - observed).days
    if age < 0 or age > max_age_days:
        raise RuntimeError(f"market date is stale: {observed} ({age} days old)")


def _snapshot_files(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore_files(snapshot: dict[Path, bytes | None]) -> None:
    for path, payload in snapshot.items():
        try:
            if payload is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, raw_temp = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".rollback", dir=path.parent
                )
                temp_path = Path(raw_temp)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_path, path)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
        except OSError:
            pass


def _publish_file_bundle(publications: list[tuple[object, Path, Path]]) -> None:
    """Publish related files and roll every destination back on failure."""
    destinations = [destination for _, _, destination in publications]
    snapshot = _snapshot_files(destinations)
    try:
        for publisher, source, destination in publications:
            publisher(source, destination)
    except Exception:
        _restore_files(snapshot)
        raise


def _publish_json_bundle(publications: list[tuple[Path, Path]]) -> None:
    _publish_file_bundle(
        [(atomic_publish, source, destination) for source, destination in publications]
    )


def _run_fresh_output(
    command: list[str],
    *,
    cwd: Path,
    output: Path,
    env: dict[str, str] | None = None,
) -> None:
    before = output.stat().st_mtime_ns if output.exists() else -1
    before_digest = hashlib.sha256(output.read_bytes()).digest() if output.exists() else None
    started = time.time_ns()
    subprocess.run(command, cwd=cwd, check=True, timeout=1800, env=env)
    if not output.is_file():
        raise FileNotFoundError(f"expected output was not created: {output}")
    # Filesystems can have coarse timestamps, so compare both with the previous
    # value and with a small start-time tolerance.
    after = output.stat().st_mtime_ns
    after_digest = hashlib.sha256(output.read_bytes()).digest()
    changed = after > before or after_digest != before_digest
    if not changed or after < started - 2_000_000_000:
        raise RuntimeError(f"exporter did not refresh output: {output}")
    validate_json(output)


def refresh_cross_market(*, python: Path, shared_dir: Path) -> Path:
    staging_dir = ROOT / "data"
    output = staging_dir / "cross_market_storage.json"
    _run_fresh_output(
        [
            str(python),
            str(ROOT / "scripts" / "export_cross_market_storage.py"),
            "--output-dir",
            str(staging_dir),
        ],
        cwd=ROOT,
        output=output,
    )
    status = staging_dir / "cross_market_storage_status.json"
    status_payload = validate_json(status)
    result_payload = validate_json(output)
    if not isinstance(status_payload, dict) or status_payload.get("status") != "done":
        raise RuntimeError(f"cross-market exporter did not report done: {status}")
    today = datetime.now().date().isoformat()
    generated_at = str(
        (result_payload if isinstance(result_payload, dict) else {}).get("generated_at")
        or ""
    )
    if not generated_at.startswith(today):
        raise RuntimeError(
            f"cross-market output is not today's snapshot: {generated_at or 'missing'}"
        )
    data_health = (
        result_payload.get("data_health")
        if isinstance(result_payload, dict)
        else None
    )
    if not isinstance(data_health, dict) or data_health.get("status") != "ok":
        raise RuntimeError("cross-market snapshot has no valid current-session data")
    if not str(data_health.get("market_at") or "").startswith(today):
        raise RuntimeError("cross-market market timestamp is not from today")
    if not isinstance(result_payload.get("leaders"), list) or not result_payload["leaders"]:
        raise RuntimeError("cross-market snapshot returned no leader quotes")

    atomic_publish(status, shared_dir / status.name)
    destination = shared_dir / output.name
    atomic_publish(output, destination)
    return destination


def refresh_korea(*, python: Path, shared_dir: Path, rdagent_dir: Path) -> Path:
    source = rdagent_dir / "korea_semi.json"
    _run_fresh_output(
        [str(python), str(rdagent_dir / "export_korea_semi.py")],
        cwd=rdagent_dir,
        output=source,
    )
    payload = validate_json(source)
    today = datetime.now().date().isoformat()
    if not isinstance(payload, dict) or not str(payload.get("updated") or "").startswith(today):
        raise RuntimeError("Korea exporter did not create today's snapshot")
    if not payload.get("hynix_date") or payload.get("hynix_ret") is None:
        raise RuntimeError("Korea exporter returned no usable SK Hynix session")
    destination = shared_dir / source.name
    atomic_publish(source, destination)
    return destination


def refresh_snowball(*, python: Path, shared_dir: Path, xlsx: Path) -> Path:
    if not xlsx.is_file():
        raise FileNotFoundError(f"snowball workbook unavailable: {xlsx}")
    source = ROOT / "data" / "snowball_avoid.json"
    history = ROOT / "data" / "snowball_history.json"
    child_env = {**os.environ, "SNOWBALL_XLSX": str(xlsx)}
    _run_fresh_output(
        [str(python), str(ROOT / "scripts" / "export_snowball.py")],
        cwd=ROOT,
        output=source,
        env=child_env,
    )
    payload = validate_json(source)
    history_payload = validate_json(history)
    today = datetime.now().date().isoformat()
    if not isinstance(payload, dict) or not str(payload.get("updated") or "").startswith(today):
        raise RuntimeError("snowball exporter did not create today's snapshot")
    items = payload.get("items")
    if not isinstance(items, list) or int(payload.get("n", -1)) != len(items):
        raise RuntimeError("snowball output failed item-count validation")
    if not isinstance(history_payload, list):
        raise RuntimeError("snowball history must be a JSON array")
    latest_history = history_payload[-1] if history_payload else None
    if not isinstance(latest_history, dict) or str(
        latest_history.get("as_of") or ""
    ) != today:
        raise RuntimeError("snowball history did not append today's snapshot")
    if (
        latest_history.get("updated") != payload.get("updated")
        or int(latest_history.get("n", -1)) != len(items)
    ):
        raise RuntimeError("snowball history does not match the main snapshot")

    # Publish the supporting history first and the console snapshot last.  The
    # main file therefore becomes visible only after every output is validated.
    atomic_publish(history, shared_dir / history.name)
    destination = shared_dir / source.name
    atomic_publish(source, destination)
    return destination


def refresh_rolling(*, python: Path, shared_dir: Path) -> Path:
    output = ROOT / "data" / "rolling_earnings.json"
    _run_fresh_output(
        [
            str(python),
            str(ROOT / "scripts" / "build_rolling_earnings.py"),
            "--data-dir",
            str(ROOT / "data"),
            "--shared-dir",
            str(shared_dir),
            "--output-dir",
            str(ROOT / "data"),
        ],
        cwd=ROOT,
        output=output,
    )
    payload = validate_json(output)
    today = datetime.now().date().isoformat()
    if not isinstance(payload, dict) or not str(payload.get("updated") or "").startswith(today):
        raise RuntimeError("rolling earnings did not create today's snapshot")
    items = ((payload.get("rolling") or {}).get("items"))
    if not isinstance(items, list) or int(payload.get("n", -1)) != len(items):
        raise RuntimeError("rolling earnings failed item-count validation")
    source_health = payload.get("source_health")
    if (
        not isinstance(source_health, dict)
        or int(source_health.get("shared_payloads", 0)) < 1
        or int(source_health.get("event_rows", 0)) < 1
    ):
        raise RuntimeError("rolling earnings has no valid shared upstream data")
    destination = shared_dir / output.name
    atomic_publish(output, destination)
    return destination


def refresh_inclusion(*, python: Path, shared_dir: Path) -> Path:
    source = ROOT / "data" / "index_inclusion.json"
    _run_fresh_output(
        [str(python), str(ROOT / "scripts" / "export_index_inclusion.py")],
        cwd=ROOT,
        output=source,
    )
    payload = validate_json(source)
    today = datetime.now().date().isoformat()
    if not isinstance(payload, dict) or not str(payload.get("updated_at") or "").startswith(today):
        raise RuntimeError("inclusion research did not create today's snapshot")
    if not isinstance(payload.get("details"), list) or not payload["details"]:
        raise RuntimeError("inclusion research returned no detail rows")
    if not isinstance(payload.get("stats"), dict) or not payload["stats"]:
        raise RuntimeError("inclusion research returned no statistics")
    destination = shared_dir / source.name
    atomic_publish(source, destination)
    return destination


def refresh_advisor(*, python: Path, shared_dir: Path, rdagent_dir: Path) -> Path:
    source = rdagent_dir / "regime_advisor.json"
    snapshot = _snapshot_files([source])
    try:
        _run_fresh_output(
            [str(python), str(rdagent_dir / "regime_advisor.py")],
            cwd=rdagent_dir,
            output=source,
        )
        payload = validate_json(source)
        if not isinstance(payload, dict):
            raise RuntimeError("advisor output must be a JSON object")
        _require_today(payload, "updated_at", "updated")
        current = payload.get("current")
        if not isinstance(current, dict):
            raise RuntimeError("advisor output has no current decision")
        _require_recent_market_date(current.get("as_of"))
        if not isinstance(current.get("basket"), list) or not current["basket"]:
            raise RuntimeError("advisor output has no current basket")
        destination = shared_dir / source.name
        atomic_publish(source, destination)
        return destination
    except Exception:
        _restore_files(snapshot)
        raise


def refresh_transfer_documents(*, python: Path, shared_dir: Path) -> Path:
    lock = shared_dir / "cninfo_transfer.lock"
    with process_lock(
        lock,
        wait_seconds=0,
        reason="scheduled-transfer-documents",
    ):
        return _refresh_transfer_documents_locked(python=python, shared_dir=shared_dir)


def _refresh_transfer_documents_locked(*, python: Path, shared_dir: Path) -> Path:
    source = ROOT / "data" / "cninfo_transfer.json"
    overlay = ROOT / "data" / "transfer_terms_overlay.json"
    snapshot = _snapshot_files([source, overlay])
    try:
        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "export_transfer_events.py"),
                "--data-dir",
                str(ROOT / "data"),
                "--incremental",
                "--incremental-overlap-days",
                "2",
            ],
            cwd=ROOT,
            output=source,
        )
        payload = validate_json(source)
        if not isinstance(payload, dict):
            raise RuntimeError("transfer output must be a JSON object")
        _require_today(payload, "updated")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise RuntimeError("transfer output has no retained announcement history")
        if payload.get("errors"):
            raise RuntimeError(f"transfer exporter reported errors: {payload['errors'][:3]}")
        query = payload.get("query") or {}
        if str(query.get("end") or "") != _today():
            raise RuntimeError("transfer query did not cover today")

        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "enrich_transfer_terms.py"),
                "--source",
                str(source),
                "--output",
                str(overlay),
                "--limit",
                "30",
                "--retry-errors",
            ],
            cwd=ROOT,
            output=overlay,
        )
        overlay_payload = validate_json(overlay)
        if not isinstance(overlay_payload, dict):
            raise RuntimeError("transfer overlay must be a JSON object")
        _require_today(overlay_payload, "updated")
        if not isinstance(overlay_payload.get("items"), list):
            raise RuntimeError("transfer overlay has no item list")
        stats = overlay_payload.get("stats") or {}
        if int(stats.get("errors") or 0) > 0:
            raise RuntimeError(f"transfer overlay reported parse errors: {stats}")

        destination = shared_dir / source.name
        _publish_json_bundle(
            [
                (overlay, shared_dir / overlay.name),
                (source, destination),
            ]
        )
        return destination
    except Exception:
        _restore_files(snapshot)
        raise


def refresh_placement_documents(*, python: Path, shared_dir: Path) -> Path:
    lock = shared_dir / "placement_documents.lock"
    with process_lock(
        lock,
        wait_seconds=0,
        reason="scheduled-placement-documents",
    ):
        return _refresh_placement_documents_locked(python=python, shared_dir=shared_dir)


def _refresh_placement_documents_locked(*, python: Path, shared_dir: Path) -> Path:
    seed = ROOT / "data" / "asset_injection.json"
    source = ROOT / "data" / "cninfo_placement.json"
    snapshot = _snapshot_files([seed, source])
    try:
        _run_fresh_output(
            [str(python), str(ROOT / "scripts" / "export_asset_injection.py")],
            cwd=ROOT,
            output=seed,
        )
        seed_payload = validate_json(seed)
        if not isinstance(seed_payload, dict):
            raise RuntimeError("placement seed must be a JSON object")
        _require_today(seed_payload, "updated")
        seed_items = seed_payload.get("items")
        if (
            not isinstance(seed_items, list)
            or not seed_items
            or int(seed_payload.get("n", -1)) != len(seed_items)
        ):
            raise RuntimeError("placement seed failed item-count validation")

        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "export_placement_events.py"),
                "--data-dir",
                str(ROOT / "data"),
                "--seed-file",
                str(seed),
                "--output",
                str(source),
            ],
            cwd=ROOT,
            output=source,
        )
        payload = validate_json(source)
        if not isinstance(payload, dict):
            raise RuntimeError("placement output must be a JSON object")
        _require_today(payload, "updated")
        items = payload.get("items")
        if (
            not isinstance(items, list)
            or not items
            or int(payload.get("count", -1)) != len(items)
        ):
            raise RuntimeError("placement output failed item-count validation")
        if payload.get("errors"):
            raise RuntimeError(f"placement exporter reported errors: {payload['errors'][:3]}")

        destination = shared_dir / source.name
        _publish_json_bundle(
            [
                (seed, shared_dir / seed.name),
                (source, destination),
            ]
        )
        return destination
    except Exception:
        _restore_files(snapshot)
        raise


def refresh_earnings_announcements(*, python: Path, shared_dir: Path) -> Path:
    local_source = ROOT / "data" / "cninfo_earnings_announcements.json"
    source = shared_dir / local_source.name
    lock_path = shared_dir / "cninfo_earnings_announcements.lock"
    with process_lock(
        lock_path,
        wait_seconds=0,
        reason="scheduled-earnings-announcements",
    ):
        # A lock-busy caller must never restore bytes captured while the active
        # writer is publishing a newer canonical snapshot.
        snapshot = _snapshot_files([local_source, source])
        try:
            # The shared snapshot is canonical because the watcher adds targeted
            # event-time backfills there.  Seed it once from local data if needed,
            # then always increment and validate the canonical copy in place.
            if not source.is_file() and local_source.is_file():
                atomic_publish(local_source, source)
            _run_fresh_output(
                [
                    str(python),
                    str(ROOT / "scripts" / "export_earnings_announcement_times.py"),
                    "--data-dir",
                    str(shared_dir),
                    "--start",
                    "2024-01-01",
                    "--end",
                    _today(),
                    "--incremental-overlap-days",
                    "2",
                    "--sleep",
                    "0.15",
                    "--workers",
                    "4",
                ],
                cwd=ROOT,
                output=source,
            )
            payload = validate_json(source)
            if not isinstance(payload, dict):
                raise RuntimeError("earnings announcement output must be a JSON object")
            _require_today(payload, "updated")
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                raise RuntimeError("earnings announcement output has no retained history")
            if payload.get("errors"):
                raise RuntimeError(
                    f"earnings announcement exporter reported errors: {payload['errors'][:3]}"
                )
            query = payload.get("query") or {}
            if str(query.get("end") or "") != _today():
                raise RuntimeError("earnings announcement query did not cover today")
            atomic_publish(source, local_source)
            return source
        except Exception:
            _restore_files(snapshot)
            raise


def _publish_json_to_data_roots(
    source: Path, *, shared_dir: Path, nas_app_data_dir: Path
) -> None:
    _publish_json_bundle(
        [
            (source, shared_dir / source.name),
            (source, nas_app_data_dir / source.name),
        ]
    )


def refresh_top_risk(
    *, python: Path, shared_dir: Path, nas_app_data_dir: Path
) -> Path:
    broad = ROOT / "data" / "etf_flow_top_signal.json"
    sector = ROOT / "data" / "sector_etf_flow_signal.json"
    huijin = ROOT / "data" / "huijin_etf_flow.json"
    huijin_series = ROOT / "data" / "huijin_etf_share_series.json"
    cache_dir = ROOT / "data" / "etf_flow_cache"
    snapshot = _snapshot_files([broad, sector, huijin, huijin_series])
    try:
        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "backtest_etf_flow_signal.py"),
                "--refresh",
            ],
            cwd=ROOT,
            output=broad,
        )
        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "backtest_sector_etf_flow_signal.py"),
                "--refresh",
            ],
            cwd=ROOT,
            output=sector,
        )
        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "backtest_huijin_etf_flow.py"),
                "--refresh",
            ],
            cwd=ROOT,
            output=huijin,
        )
        broad_payload = validate_json(broad)
        sector_payload = validate_json(sector)
        for label, payload in (("broad", broad_payload), ("sector", sector_payload)):
            if not isinstance(payload, dict):
                raise RuntimeError(f"{label} ETF flow output must be a JSON object")
            _require_today(payload, "updated")
            period = payload.get("period") or []
            if not isinstance(period, list) or len(period) < 2:
                raise RuntimeError(f"{label} ETF flow output has no period")
            _require_recent_market_date(period[-1])
            if not isinstance(payload.get("events"), list) or not payload["events"]:
                raise RuntimeError(f"{label} ETF flow output has no events")
        if not isinstance(broad_payload.get("etfs"), (list, dict)) or not broad_payload["etfs"]:
            raise RuntimeError("broad ETF flow output has no ETF universe")
        if sector_payload.get("missing"):
            raise RuntimeError(f"sector ETF flow has missing sources: {sector_payload['missing']}")
        huijin_payload = validate_json(huijin)
        if not isinstance(huijin_payload, dict):
            raise RuntimeError("Huijin ETF flow output must be a JSON object")
        _require_today(huijin_payload, "updated")
        _require_recent_market_date(huijin_payload.get("as_of"))
        if not isinstance(huijin_payload.get("etfs"), list) or not huijin_payload["etfs"]:
            raise RuntimeError("Huijin ETF flow output has no verified roster")
        if not isinstance(huijin_payload.get("aggregate_series"), list) or not huijin_payload["aggregate_series"]:
            raise RuntimeError("Huijin ETF flow output has no daily trend")
        quality = huijin_payload.get("data_quality") or {}
        if float(quality.get("coverage_pct") or 0) < 95:
            raise RuntimeError(f"Huijin ETF flow coverage is below 95%: {quality}")
        series_payload = validate_json(huijin_series)
        if not isinstance(series_payload, dict) or not isinstance(series_payload.get("funds"), dict) or not series_payload["funds"]:
            raise RuntimeError("Huijin ETF share series output has no per-ETF history")

        cache_files = [path for path in cache_dir.glob("*") if path.is_file()]
        if len(cache_files) < 5:
            raise RuntimeError("ETF flow cache is incomplete")
        shared_cache = shared_dir / "etf_flow_cache"
        nas_cache = nas_app_data_dir / "etf_flow_cache"
        shared_cache.mkdir(parents=True, exist_ok=True)
        nas_cache.mkdir(parents=True, exist_ok=True)
        publications = []
        for cache_file in cache_files:
            publications.extend(
                [
                    (atomic_copy, cache_file, shared_cache / cache_file.name),
                    (atomic_copy, cache_file, nas_cache / cache_file.name),
                ]
            )
        publications.extend(
            [
                (atomic_publish, sector, shared_dir / sector.name),
                (atomic_publish, sector, nas_app_data_dir / sector.name),
                (atomic_publish, broad, shared_dir / broad.name),
                (atomic_publish, broad, nas_app_data_dir / broad.name),
                (atomic_publish, huijin, shared_dir / huijin.name),
                (atomic_publish, huijin, nas_app_data_dir / huijin.name),
                (atomic_publish, huijin_series, shared_dir / huijin_series.name),
                (atomic_publish, huijin_series, nas_app_data_dir / huijin_series.name),
            ]
        )
        _publish_file_bundle(publications)
        return shared_dir / broad.name
    except Exception:
        _restore_files(snapshot)
        raise


def refresh_money_outflow(
    *, python: Path, shared_dir: Path, nas_app_data_dir: Path
) -> Path:
    source = ROOT / "data" / "money_outflow_signal.json"
    snapshot = _snapshot_files([source])
    child_env = {**os.environ, "QI_SKIP_MONEYFLOW_NAS_PUBLISH": "1"}
    try:
        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "backtest_money_outflow_signal.py"),
                "--start",
                "2026-01-01",
                "--end",
                _today(),
                "--sample-every",
                "1",
                "--sleep",
                "0.05",
                "--output",
                str(source),
            ],
            cwd=ROOT,
            output=source,
            env=child_env,
        )
        payload = validate_json(source)
        if not isinstance(payload, dict):
            raise RuntimeError("money-outflow output must be a JSON object")
        _require_today(payload, "updated")
        if int(payload.get("n_moneyflow_rows") or 0) <= 0 or int(
            payload.get("n_feature_rows") or 0
        ) <= 0:
            raise RuntimeError("money-outflow output has no source rows")
        latest = payload.get("latest_stock_outflow")
        if not isinstance(latest, list) or not latest:
            raise RuntimeError("money-outflow output has no latest stock ranking")
        latest_date = max(str(row.get("trade_date") or "") for row in latest)
        _require_recent_market_date(latest_date)
        _publish_json_to_data_roots(
            source, shared_dir=shared_dir, nas_app_data_dir=nas_app_data_dir
        )
        return shared_dir / source.name
    except Exception:
        _restore_files(snapshot)
        raise


def refresh_growth_queue(*, python: Path, shared_dir: Path) -> Path:
    source = ROOT / "data" / "growth_report_queue.json"
    snapshot = _snapshot_files([source])
    try:
        _run_fresh_output(
            [
                str(python),
                str(ROOT / "scripts" / "build_growth_report_queue.py"),
                "--data-dir",
                str(ROOT / "data"),
                "--shared-dir",
                str(shared_dir),
                "--output-dir",
                str(ROOT / "data"),
            ],
            cwd=ROOT,
            output=source,
        )
        payload = validate_json(source)
        if not isinstance(payload, dict):
            raise RuntimeError("growth-report queue must be a JSON object")
        _require_today(payload, "updated")
        items = payload.get("items")
        if not isinstance(items, list) or int(payload.get("n", -1)) != len(items):
            raise RuntimeError("growth-report queue failed item-count validation")
        window = payload.get("window") or {}
        if not window.get("trade_day") or not window.get("start") or not window.get("end"):
            raise RuntimeError("growth-report queue has no evaluated close window")
        destination = shared_dir / source.name
        atomic_publish(source, destination)
        return destination
    except Exception:
        _restore_files(snapshot)
        raise


def refresh_rolling_backtest(*, python: Path, shared_dir: Path) -> Path:
    local_source = ROOT / "data" / "rolling_earnings_backtest_top50.json"
    source = shared_dir / local_source.name
    status = shared_dir / "rolling_earnings_backtest_status.json"
    lock = shared_dir / "rolling_earnings_backtest.lock"
    with process_lock(
        lock,
        wait_seconds=0,
        reason="scheduled-weekly-runner",
    ):
        # The wrapper owns the production mutex.  The child keeps its existing
        # lock/status protocol on a unique internal path so it cannot deadlock
        # against its parent; every other production entry point still targets
        # ``lock`` and is excluded for this entire transaction.
        snapshot = _snapshot_files([local_source, source])
        child_lock = lock.with_name(
            f".{lock.name}.child-{os.getpid()}-{uuid.uuid4().hex}.lock"
        )
        try:
            _run_fresh_output(
                [
                    str(python),
                    str(ROOT / "scripts" / "backtest_rolling_earnings.py"),
                    "--topn",
                    "50",
                    "--announcement-cache",
                    str(shared_dir / "cninfo_earnings_announcements.json"),
                    "--out",
                    str(source),
                    "--lock-file",
                    str(child_lock),
                    "--status-file",
                    str(status),
                    "--lock-wait-seconds",
                    "0",
                    "--reason",
                    "scheduled-weekly",
                ],
                cwd=ROOT,
                output=source,
            )
            payload = validate_json(source)
            if not isinstance(payload, dict):
                raise RuntimeError("rolling-earnings backtest must be a JSON object")
            _require_today(payload, "updated")
            if int(payload.get("n_events") or 0) <= 0 or not isinstance(
                payload.get("summary"), dict
            ):
                raise RuntimeError("rolling-earnings backtest has no validated events")
            status_payload = validate_json(status)
            if (
                not isinstance(status_payload, dict)
                or status_payload.get("state") != "done"
                or status_payload.get("reason") != "scheduled-weekly"
            ):
                raise RuntimeError("rolling-earnings backtest status is not a matching success")
            atomic_publish(source, local_source)
            return source
        except Exception:
            _restore_files(snapshot)
            raise
        finally:
            try:
                child_lock.unlink(missing_ok=True)
            except OSError:
                pass


def refresh_earnings_entry_lag(*, python: Path, shared_dir: Path) -> Path:
    variants = (
        (20, "rolling_earnings_interim_entry_lag_top50.json"),
        (50, "rolling_earnings_interim_entry_lag_g50_top50.json"),
        (100, "rolling_earnings_interim_entry_lag_g100_top50.json"),
    )
    outputs = [ROOT / "data" / filename for _growth, filename in variants]
    snapshot = _snapshot_files(outputs)
    try:
        for min_growth, filename in variants:
            output = ROOT / "data" / filename
            _run_fresh_output(
                [
                    str(python),
                    str(ROOT / "scripts" / "backtest_rolling_earnings.py"),
                    "--entry-lag-only",
                    "--period-suffix",
                    "0630",
                    "--min-growth",
                    str(min_growth),
                    "--entry-lags",
                    "1,2,3,4,5",
                    "--topn",
                    "50",
                    "--out",
                    str(output),
                    "--reason",
                    "scheduled-weekly-entry-lag",
                ],
                cwd=ROOT,
                output=output,
            )
            payload = validate_json(output)
            if not isinstance(payload, dict):
                raise RuntimeError(f"entry-lag output must be a JSON object: {filename}")
            _require_today(payload, "updated")
            if int(payload.get("n_source_events") or 0) <= 0 or int(
                payload.get("n_codes") or 0
            ) <= 0:
                raise RuntimeError(f"entry-lag output has no source events: {filename}")
            analysis = payload.get("entry_lag_analysis")
            if not isinstance(analysis, dict) or not analysis:
                raise RuntimeError(f"entry-lag output has no analysis: {filename}")
        for output in outputs:
            atomic_publish(output, shared_dir / output.name)
        return shared_dir / outputs[0].name
    except Exception:
        _restore_files(snapshot)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "job",
        choices=(
            "cross-market",
            "korea",
            "snowball",
            "rolling",
            "inclusion",
            "advisor",
            "transfer-documents",
            "placement-documents",
            "earnings-announcements",
            "top-risk",
            "money-outflow",
            "growth-queue",
            "rolling-backtest",
            "earnings-entry-lag",
        ),
    )
    parser.add_argument("--python", default=os.environ.get("QI_PYTHON", sys.executable))
    parser.add_argument(
        "--shared-dir", default=str(resolve_shared_dir())
    )
    parser.add_argument(
        "--rdagent-dir",
        default=os.environ.get("QI_RDAGENT_DIR", str(DEFAULT_RDAGENT_DIR)),
    )
    parser.add_argument(
        "--snowball-xlsx",
        default=os.environ.get("SNOWBALL_XLSX", str(DEFAULT_SNOWBALL_XLSX)),
    )
    parser.add_argument(
        "--nas-app-data-dir",
        default=os.environ.get(
            "QI_NAS_APP_DATA_DIR", str(DEFAULT_NAS_APP_DATA_DIR)
        ),
    )
    args = parser.parse_args(argv)

    python = Path(args.python)
    shared_dir = Path(args.shared_dir)
    rdagent_dir = Path(args.rdagent_dir)
    if not python.is_file():
        raise FileNotFoundError(f"Python unavailable: {python}")
    if not shared_dir.is_dir():
        raise FileNotFoundError(f"shared directory unavailable: {shared_dir}")

    if args.job == "cross-market":
        output = refresh_cross_market(python=python, shared_dir=shared_dir)
    elif args.job == "korea":
        output = refresh_korea(
            python=python, shared_dir=shared_dir, rdagent_dir=rdagent_dir
        )
    elif args.job == "rolling":
        output = refresh_rolling(python=python, shared_dir=shared_dir)
    elif args.job == "inclusion":
        output = refresh_inclusion(python=python, shared_dir=shared_dir)
    elif args.job == "snowball":
        output = refresh_snowball(
            python=python,
            shared_dir=shared_dir,
            xlsx=Path(args.snowball_xlsx),
        )
    elif args.job == "advisor":
        output = refresh_advisor(
            python=python, shared_dir=shared_dir, rdagent_dir=rdagent_dir
        )
    elif args.job == "transfer-documents":
        output = refresh_transfer_documents(python=python, shared_dir=shared_dir)
    elif args.job == "placement-documents":
        output = refresh_placement_documents(python=python, shared_dir=shared_dir)
    elif args.job == "earnings-announcements":
        output = refresh_earnings_announcements(python=python, shared_dir=shared_dir)
    elif args.job == "top-risk":
        output = refresh_top_risk(
            python=python,
            shared_dir=shared_dir,
            nas_app_data_dir=Path(args.nas_app_data_dir),
        )
    elif args.job == "money-outflow":
        output = refresh_money_outflow(
            python=python,
            shared_dir=shared_dir,
            nas_app_data_dir=Path(args.nas_app_data_dir),
        )
    elif args.job == "growth-queue":
        output = refresh_growth_queue(python=python, shared_dir=shared_dir)
    elif args.job == "rolling-backtest":
        output = refresh_rolling_backtest(python=python, shared_dir=shared_dir)
    else:
        output = refresh_earnings_entry_lag(python=python, shared_dir=shared_dir)
    print(
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] daily-console {args.job} -> {output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] daily-console failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise
