"""
Daily update: download today's tushare daily data + rebuild qlib bin.

Idempotent — safe to run multiple times. Skips dates already downloaded.
Mirrors the logic of Z:\\claude\\qlib\\scripts\\download_tushare.py +
build_qlib_bin.py but self-contained (no qlib python package needed).
"""

import os
import sys
import time
import logging
import threading
import json
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN", "")
QLIB_DATA_PATH = Path(os.environ.get("QLIB_DATA_PATH", "/app/qlib_data/cn_data"))
PARQUET_DIR = Path(os.environ.get("PARQUET_DIR", "/app/qlib_data/csv_tmp/tushare_daily"))

CALENDARS_DIR = QLIB_DATA_PATH / "calendars"
INSTRUMENTS_DIR = QLIB_DATA_PATH / "instruments"
FEATURES_DIR = QLIB_DATA_PATH / "features"

# 注: factor 字段保持 1.0 (RD-Agent / qlib Alpha158 的兼容约定),
# 新增 adj 字段存真实 adj_factor, 供 quantinvest 切换复权用
FIELDS = ["open", "close", "high", "low", "volume", "change", "factor", "adj"]
BENCHMARK_INDEX_TS_CODES = ("000300.SH", "000905.SH", "000852.SH")
DOWNLOAD_LOOKBACK_DAYS = int(os.environ.get("DAILY_UPDATE_LOOKBACK_DAYS", "45"))
ADJ_FACTOR_REPAIR_BUCKET_YEARS = max(
    1, int(os.environ.get("QI_ADJ_FACTOR_REPAIR_BUCKET_YEARS", "4"))
)

_UPDATE_THREAD_LOCK = threading.Lock()


class UpdateAlreadyRunning(RuntimeError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("update_daily")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace one published file only after its complete payload is on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    """Remove one file or directory without following directory symlinks."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


class _QlibStagedPublication:
    """Stage and publish the Qlib tree with a recoverable three-target swap.

    The feature tree is intentionally built beside the live tree on the same
    filesystem.  Commit then uses same-filesystem renames for, in order,
    ``features``, ``instruments/all.txt`` and ``calendars/day.txt``.  The old
    targets remain in ``rollback_root`` until the final commit marker exists.
    A later process can therefore restore an update interrupted by process or
    host failure, rather than leaving a new calendar over partial features.
    """

    STATE_NAME = "transaction.json"
    TARGETS = (
        Path("features"),
        Path("instruments") / "all.txt",
        Path("calendars") / "day.txt",
    )

    def __init__(self, root: Path | str):
        self.root = Path(root)
        token = f"{os.getpid()}.{uuid.uuid4().hex}"
        self.stage_root = self.root / f".update_daily.stage.{token}"
        self.rollback_root = self.root / f".update_daily.rollback.{token}"
        self.records: list[dict[str, object]] = []
        self.committed = False

    @property
    def features_dir(self) -> Path:
        return self.stage_root / "features"

    @property
    def instruments_dir(self) -> Path:
        return self.stage_root / "instruments"

    @property
    def calendars_dir(self) -> Path:
        return self.stage_root / "calendars"

    def __enter__(self):
        self.features_dir.mkdir(parents=True, exist_ok=False)
        self.instruments_dir.mkdir(parents=True, exist_ok=True)
        self.calendars_dir.mkdir(parents=True, exist_ok=True)
        return self

    def _state_payload(self, state: str) -> dict[str, object]:
        return {
            "state": state,
            "stage_root": self.stage_root.name,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "records": self.records,
        }

    def _write_state(self, state: str) -> None:
        self.rollback_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self.rollback_root / self.STATE_NAME,
            json.dumps(self._state_payload(state), ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _restore_records(
        root: Path,
        rollback_root: Path,
        records: list[dict[str, object]],
        stage_root: Path | None = None,
    ) -> None:
        failures = []
        for record in reversed(records):
            relative = Path(str(record["relative_path"]))
            live = root / relative
            backup = rollback_root / relative
            phase = str(record.get("phase", "prepared"))
            had_live = bool(record.get("had_live"))
            try:
                backup_exists = backup.exists() or backup.is_symlink()
                # Infer the narrow crash windows from the filesystem as well
                # as the journal.  A process can die after either rename but
                # before the following state-file replace.
                if had_live and backup_exists:
                    if live.exists() or live.is_symlink():
                        if stage_root is not None:
                            staged = stage_root / relative
                            if staged.exists() or staged.is_symlink():
                                _remove_path(staged)
                            staged.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(live, staged)
                        else:
                            _remove_path(live)
                    live.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, live)
                elif had_live and phase in {"backed_up", "published"}:
                    raise RuntimeError(f"rollback target is missing: {backup}")
                elif not had_live and phase in {"backed_up", "published"}:
                    if live.exists() or live.is_symlink():
                        if stage_root is not None:
                            staged = stage_root / relative
                            if staged.exists() or staged.is_symlink():
                                _remove_path(staged)
                            staged.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(live, staged)
                        else:
                            _remove_path(live)
            except Exception as exc:
                failures.append(f"{relative}: {type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("Qlib publication rollback failed: " + "; ".join(failures))

    def _rollback(self) -> None:
        self._restore_records(
            self.root,
            self.rollback_root,
            self.records,
            stage_root=self.stage_root,
        )
        shutil.rmtree(self.rollback_root, ignore_errors=True)

    def commit(self) -> None:
        for relative in self.TARGETS:
            staged = self.stage_root / relative
            if not staged.exists():
                raise RuntimeError(f"staged Qlib target is missing: {staged}")

        self._write_state("committing")
        try:
            for relative in self.TARGETS:
                live = self.root / relative
                staged = self.stage_root / relative
                backup = self.rollback_root / relative
                record: dict[str, object] = {
                    "relative_path": relative.as_posix(),
                    "had_live": live.exists() or live.is_symlink(),
                    "phase": "prepared",
                }
                self.records.append(record)
                self._write_state("committing")

                if bool(record["had_live"]):
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(live, backup)
                record["phase"] = "backed_up"
                self._write_state("committing")

                live.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, live)
                record["phase"] = "published"
                self._write_state("committing")

            self._write_state("committed")
            self.committed = True
        except Exception as exc:
            try:
                self._rollback()
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"Qlib publication failed ({exc}); rollback also failed ({rollback_exc}); "
                    f"recovery data kept at {self.rollback_root}"
                ) from exc
            raise
        else:
            shutil.rmtree(self.rollback_root, ignore_errors=True)
            shutil.rmtree(self.stage_root, ignore_errors=True)

    def __exit__(self, exc_type, exc, traceback):
        if not self.committed:
            shutil.rmtree(self.stage_root, ignore_errors=True)
        return False


def recover_incomplete_qlib_publications(root: Path | str) -> list[str]:
    """Rollback interrupted commits and discard never-published staging trees."""
    root = Path(root)
    recovered = []
    for rollback_root in sorted(root.glob(".update_daily.rollback.*")):
        state_path = rollback_root / _QlibStagedPublication.STATE_NAME
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot recover incomplete Qlib publication {rollback_root}: {exc}"
            ) from exc
        stage_name = str(payload.get("stage_root") or "")
        if not stage_name.startswith(".update_daily.stage.") or Path(stage_name).name != stage_name:
            raise RuntimeError(f"invalid staged Qlib path in {state_path}: {stage_name!r}")
        if payload.get("state") != "committed":
            records = payload.get("records")
            if not isinstance(records, list):
                raise RuntimeError(f"invalid rollback records in {state_path}")
            _QlibStagedPublication._restore_records(
                root,
                rollback_root,
                records,
                stage_root=root / stage_name,
            )
            recovered.append(rollback_root.name)
        shutil.rmtree(rollback_root, ignore_errors=True)
        shutil.rmtree(root / stage_name, ignore_errors=True)

    # A crash during staging cannot have changed a live target; it is safe to
    # discard these only after every rollback journal above has been handled.
    referenced_stages = {
        str(json.loads((path / _QlibStagedPublication.STATE_NAME).read_text(encoding="utf-8")).get("stage_root"))
        for path in root.glob(".update_daily.rollback.*")
        if (path / _QlibStagedPublication.STATE_NAME).is_file()
    }
    for stage_root in root.glob(".update_daily.stage.*"):
        if stage_root.name not in referenced_stages:
            shutil.rmtree(stage_root, ignore_errors=True)
    return recovered


def _adj_factor_repair_status_path() -> Path:
    configured = os.environ.get("QI_ADJ_FACTOR_REPAIR_STATUS", "").strip()
    return Path(configured) if configured else PARQUET_DIR.parent / "adj_factor_repair_status.json"


def _write_adj_factor_repair_status(state: str, **fields) -> dict[str, object]:
    """Persist a compact audit record independently of the scheduler status."""
    payload: dict[str, object] = {
        "state": state,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    _atomic_write_text(
        _adj_factor_repair_status_path(),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )
    return payload


def _complete_staged_adj_factor_repair_status(completed_by: str) -> dict[str, object] | None:
    """Promote a staged repair only after the caller's publication boundary."""
    path = _adj_factor_repair_status_path()
    if not path.is_file():
        return None
    try:
        staged = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read staged adj_factor repair status: {exc}") from exc
    if staged.get("state") != "staged":
        return staged
    outcome = staged.get("intended_outcome")
    if outcome not in {"clean", "repaired", "identity_quarantined"}:
        raise RuntimeError(f"invalid staged adj_factor repair outcome: {outcome!r}")
    fields = {
        key: value
        for key, value in staged.items()
        if key not in {"state", "updated_at", "stage", "intended_outcome"}
    }
    return _write_adj_factor_repair_status(
        str(outcome),
        stage="complete",
        completed_by=completed_by,
        **fields,
    )


@contextmanager
def qlib_update_lock(root: Path | None = None):
    """Prevent boot catch-up and the scheduled job from rebuilding together."""
    if not _UPDATE_THREAD_LOCK.acquire(blocking=False):
        raise UpdateAlreadyRunning("another daily update is already running in this process")

    lock_root = Path(root) if root is not None else QLIB_DATA_PATH
    lock_path = lock_root / ".update_daily.lock"
    handle = None
    locked = False
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UpdateAlreadyRunning(f"another daily update owns {lock_path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UpdateAlreadyRunning(f"another daily update owns {lock_path}") from exc
        locked = True
        yield
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
        _UPDATE_THREAD_LOCK.release()


def _pro():
    if not TOKEN:
        raise RuntimeError("TUSHARE_TOKEN not set")
    ts.set_token(TOKEN)
    return ts.pro_api()


def _ts_code_to_qlib(ts_code: str) -> str:
    code, exch = ts_code.split(".")
    return f"{exch.lower()}{code}"


def _fetch_index_history(
    pro,
    ts_code: str,
    end: str,
    sleep: float = 0.2,
    empty_retries: int = 2,
) -> pd.DataFrame:
    """Fetch one index in bounded chunks so Tushare row limits cannot truncate it."""
    end_ts = pd.Timestamp(datetime.strptime(end, "%Y%m%d"))
    chunk_start = pd.Timestamp("2000-01-01")
    frames = []
    while chunk_start <= end_ts:
        chunk_end = min(chunk_start + pd.DateOffset(years=8) - pd.Timedelta(days=1), end_ts)
        frame = pd.DataFrame()
        # Empty pre-inception chunks are legitimate. Once history has begun,
        # or for the latest chunk, retry empties because they indicate a likely
        # transient/truncated response rather than index inception.
        retries = empty_retries if frames or chunk_end >= end_ts else 0
        for attempt in range(retries + 1):
            candidate = pro.index_daily(
                ts_code=ts_code,
                start_date=chunk_start.strftime("%Y%m%d"),
                end_date=chunk_end.strftime("%Y%m%d"),
            )
            if candidate is not None and not candidate.empty:
                candidate = candidate.copy()
                if not {"ts_code", "trade_date"}.issubset(candidate.columns):
                    candidate = pd.DataFrame()
                    continue
                candidate["trade_date"] = candidate["trade_date"].astype(str)
                candidate = candidate[
                    candidate["trade_date"].between(
                        chunk_start.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
                    )
                ]
                candidate = candidate[candidate["ts_code"].astype(str).str.upper().eq(ts_code)]
            if candidate is not None and not candidate.empty:
                frame = candidate
                break
            if attempt < retries and sleep:
                time.sleep(sleep)
        if not frame.empty:
            frames.append(frame)
        elif frames or chunk_end >= end_ts:
            log.warning(
                "index %s returned empty chunk after retries: %s ~ %s",
                ts_code,
                chunk_start.strftime("%Y%m%d"),
                chunk_end.strftime("%Y%m%d"),
            )
        chunk_start = chunk_end + pd.Timedelta(days=1)
        if sleep:
            time.sleep(sleep)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True, sort=False)
    result["ts_code"] = result["ts_code"].astype(str).str.upper()
    return result[result["ts_code"].eq(ts_code)].drop_duplicates("trade_date", keep="last")


def _prepare_benchmark_frame(raw: pd.DataFrame, ts_code: str, cals: list[str]) -> tuple[pd.DataFrame, int, int]:
    """Validate complete benchmark coverage before producing any bin payload."""
    if raw.empty:
        raise RuntimeError(f"index_daily returned no history for {ts_code}")
    required = {"trade_date", "open", "close", "high", "low"}
    if not required.issubset(raw.columns):
        raise RuntimeError(f"index_daily missing columns for {ts_code}: {sorted(required - set(raw.columns))}")

    frame = raw.copy()
    parsed_dates = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    if parsed_dates.isna().any():
        raise RuntimeError(f"index_daily contains invalid trade_date for {ts_code}")
    frame["date_iso"] = parsed_dates.dt.strftime("%Y-%m-%d")
    frame = frame[frame["date_iso"].isin(cals)].copy()
    frame = frame.sort_values("date_iso").drop_duplicates("date_iso", keep="last")
    if frame.empty:
        raise RuntimeError(f"index_daily history does not overlap calendar for {ts_code}")
    if frame["date_iso"].iloc[-1] != cals[-1]:
        raise RuntimeError(
            f"index_daily tail is stale for {ts_code}: "
            f"latest={frame['date_iso'].iloc[-1]}, expected={cals[-1]}"
        )

    cal_idx = {value: index for index, value in enumerate(cals)}
    start_idx = cal_idx[frame["date_iso"].iloc[0]]
    last_idx = cal_idx[frame["date_iso"].iloc[-1]]
    expected_dates = cals[start_idx:last_idx + 1]
    actual_dates = frame["date_iso"].tolist()
    if actual_dates != expected_dates:
        actual_set = set(actual_dates)
        missing = [value for value in expected_dates if value not in actual_set]
        raise RuntimeError(
            f"index_daily has calendar gaps for {ts_code}: "
            f"missing={len(missing)}, sample={missing[:5]}"
        )

    numeric = {}
    for field in ("open", "close", "high", "low"):
        numeric[field] = pd.to_numeric(frame[field], errors="coerce").to_numpy(dtype=np.float64)
    invalid_numeric = np.zeros(len(frame), dtype=bool)
    for values in numeric.values():
        invalid_numeric |= ~np.isfinite(values) | (values <= 0)
    envelope_invalid = (
        (numeric["high"] < np.maximum(numeric["open"], numeric["close"]))
        | (numeric["low"] > np.minimum(numeric["open"], numeric["close"]))
        | (numeric["high"] < numeric["low"])
    )
    invalid = invalid_numeric | envelope_invalid
    if invalid.any():
        bad_dates = frame.loc[invalid, "date_iso"].head(5).tolist()
        raise RuntimeError(
            f"index_daily contains invalid OHLC for {ts_code}: "
            f"count={int(invalid.sum())}, sample={bad_dates}"
        )

    n = len(frame)
    volume = frame.get("vol", frame.get("volume", pd.Series([0.0] * n, index=frame.index)))
    pct_chg = frame.get("pct_chg", pd.Series([0.0] * n, index=frame.index))
    prepared = pd.DataFrame({
        "open": numeric["open"],
        "close": numeric["close"],
        "high": numeric["high"],
        "low": numeric["low"],
        "volume": pd.to_numeric(volume, errors="coerce").fillna(0.0).to_numpy(),
        "change": (pd.to_numeric(pct_chg, errors="coerce").fillna(0.0) / 100.0).to_numpy(),
    })
    prepared["factor"] = np.float32(1.0)
    prepared["adj"] = np.float32(1.0)
    return prepared, start_idx, last_idx


def refresh_benchmark_index_bins(
    pro,
    end: str | None = None,
    sleep: float = 0.2,
    calendars: list[str] | None = None,
    base_instruments: list[str] | None = None,
    publish_instruments: bool = True,
    features_dir: Path | None = None,
    instruments_dir: Path | None = None,
    calendars_dir: Path | None = None,
):
    """Refresh benchmark bins independently from equity daily parquet.

    Equity parquet intentionally contains no index rows. Keeping this as a
    separate full-history build prevents a recent-only index fetch from
    replacing the existing long benchmark history.
    """
    end = end or datetime.now().strftime("%Y%m%d")
    target_features_dir = Path(features_dir) if features_dir is not None else FEATURES_DIR
    target_instruments_dir = Path(instruments_dir) if instruments_dir is not None else INSTRUMENTS_DIR
    source_calendars_dir = Path(calendars_dir) if calendars_dir is not None else CALENDARS_DIR
    if calendars is None:
        cal_path = source_calendars_dir / "day.txt"
        if not cal_path.exists():
            raise RuntimeError(f"calendar missing: {cal_path}")
        cals = [line.strip() for line in cal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        cals = list(calendars)
    if not cals:
        raise RuntimeError("calendar is empty")
    prepared_indices = []
    instrument_updates = {}

    for ts_code in BENCHMARK_INDEX_TS_CODES:
        raw = _fetch_index_history(pro, ts_code, end=end, sleep=sleep)
        frame, start_idx, last_idx = _prepare_benchmark_frame(raw, ts_code, cals)
        prepared_indices.append((ts_code, frame, start_idx, last_idx))

    # Nothing is replaced until all benchmark histories pass validation.
    for ts_code, frame, start_idx, last_idx in prepared_indices:
        code = _ts_code_to_qlib(ts_code)
        stock_dir = target_features_dir / code
        stock_dir.mkdir(parents=True, exist_ok=True)
        for field in FIELDS:
            values = frame[field].to_numpy(dtype="<f4")
            array = np.hstack([np.float32(start_idx), values]).astype("<f4")
            path = stock_dir / f"{field}.day.bin"
            _atomic_write_bytes(path, array.tobytes())
        instrument_updates[code] = f"{code}\t{cals[start_idx]}\t{cals[last_idx]}"
        log.info("benchmark %s: %s rows, %s ~ %s", code, len(frame), cals[start_idx], cals[last_idx])

    instruments_path = target_instruments_dir / "all.txt"
    existing = {}
    if base_instruments is not None:
        for line in base_instruments:
            parts = line.split("\t")
            if parts and parts[0]:
                existing[parts[0]] = line
    elif instruments_path.exists():
        for line in instruments_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if parts and parts[0]:
                existing[parts[0]] = line
    existing.update(instrument_updates)
    instrument_lines = [existing[key] for key in sorted(existing)]
    if publish_instruments:
        target_instruments_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(instruments_path, "\n".join(instrument_lines) + "\n")
    return instrument_lines


# ---------- step 1: download today's parquet ----------


def _validate_equity_history(
    frame: pd.DataFrame,
    source: str = "equity parquet",
    *,
    repair_legacy_envelope: bool = False,
    allow_legacy_initial_pct_chg: bool = False,
    certified_legacy_initial_pct_chg_keys: set[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Validate and normalize equity rows before any qlib file is replaced."""
    if frame is None or frame.empty:
        raise RuntimeError(f"{source} contains no rows")

    required = {
        "ts_code", "trade_date", "open", "close", "high", "low",
        "pct_chg", "adj_factor",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"{source} missing columns: {missing}")
    volume_field = "vol" if "vol" in frame.columns else "volume" if "volume" in frame.columns else ""
    if not volume_field:
        raise RuntimeError(f"{source} missing volume column (vol or volume)")

    result = frame.copy()
    codes = result["ts_code"].astype(str).str.upper().str.strip()
    valid_codes = codes.str.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", na=False)
    if not valid_codes.all():
        sample = codes[~valid_codes].head(5).tolist()
        raise RuntimeError(f"{source} contains invalid ts_code: count={int((~valid_codes).sum())}, sample={sample}")
    result["ts_code"] = codes

    raw_dates = result["trade_date"].astype(str).str.replace("-", "", regex=False).str.strip()
    parsed_dates = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
    if parsed_dates.isna().any():
        sample = raw_dates[parsed_dates.isna()].head(5).tolist()
        raise RuntimeError(
            f"{source} contains invalid trade_date: count={int(parsed_dates.isna().sum())}, sample={sample}"
        )
    result["trade_date"] = parsed_dates.dt.strftime("%Y%m%d")

    numeric: dict[str, pd.Series] = {}
    for field in ("open", "close", "high", "low", "adj_factor", volume_field):
        numeric[field] = pd.to_numeric(result[field], errors="coerce")
        result[field] = numeric[field]

    adj_values = numeric["adj_factor"].to_numpy(dtype=np.float64)
    invalid_adj = ~np.isfinite(adj_values) | (adj_values <= 0)
    if invalid_adj.any():
        sample = (
            result.loc[invalid_adj, ["ts_code", "trade_date", "adj_factor"]]
            .head(5).to_dict("records")
        )
        raise RuntimeError(
            f"{source} contains invalid adj_factor: count={int(invalid_adj.sum())}, sample={sample}"
        )

    ohlc = {field: numeric[field].to_numpy(dtype=np.float64) for field in ("open", "close", "high", "low")}
    invalid_ohlc = np.zeros(len(result), dtype=bool)
    for values in ohlc.values():
        invalid_ohlc |= ~np.isfinite(values) | (values <= 0)
    if invalid_ohlc.any():
        sample = (
            result.loc[invalid_ohlc, ["ts_code", "trade_date", "open", "close", "high", "low"]]
            .head(5).to_dict("records")
        )
        raise RuntimeError(
            f"{source} contains invalid OHLC: count={int(invalid_ohlc.sum())}, sample={sample}"
        )

    invalid_envelope = (
        (ohlc["high"] < np.maximum(ohlc["open"], ohlc["close"]))
        | (ohlc["low"] > np.minimum(ohlc["open"], ohlc["close"]))
        | (ohlc["high"] < ohlc["low"])
    )
    if invalid_envelope.any() and not repair_legacy_envelope:
        sample = (
            result.loc[invalid_envelope, ["ts_code", "trade_date", "open", "close", "high", "low"]]
            .head(5).to_dict("records")
        )
        raise RuntimeError(
            f"{source} contains invalid OHLC envelope: "
            f"count={int(invalid_envelope.sum())}, sample={sample}"
        )
    if invalid_envelope.any():
        log.warning(
            "%s contains %s legacy OHLC envelope anomalies; expanding high/low to include open/close",
            source,
            int(invalid_envelope.sum()),
        )
        result.loc[invalid_envelope, "high"] = np.maximum.reduce((
            ohlc["high"][invalid_envelope],
            ohlc["open"][invalid_envelope],
            ohlc["close"][invalid_envelope],
        ))
        result.loc[invalid_envelope, "low"] = np.minimum.reduce((
            ohlc["low"][invalid_envelope],
            ohlc["open"][invalid_envelope],
            ohlc["close"][invalid_envelope],
        ))

    volume_values = numeric[volume_field].to_numpy(dtype=np.float64)
    invalid_volume = ~np.isfinite(volume_values) | (volume_values < 0)
    if invalid_volume.any():
        sample = (
            result.loc[invalid_volume, ["ts_code", "trade_date", volume_field]]
            .head(5).to_dict("records")
        )
        raise RuntimeError(
            f"{source} contains invalid {volume_field}: count={int(invalid_volume.sum())}, sample={sample}"
        )

    if "pct_chg" in result.columns:
        change = pd.to_numeric(result["pct_chg"], errors="coerce")
        invalid_change = ~np.isfinite(change.to_numpy(dtype=np.float64))
        if invalid_change.any():
            allowed_change = np.zeros(len(result), dtype=bool)
            invalid_positions = np.flatnonzero(invalid_change)
            # Tushare's legacy Beijing-market history has no prior close on
            # the first observed day of some instruments.  That return is
            # mathematically undefined: preserve NaN, but only when both raw
            # provenance fields agree and the all-history boundary (or an
            # exact key previously certified against it) proves the case.
            if "pre_close" in result.columns and "change" in result.columns:
                invalid_rows = result.iloc[invalid_positions]
                pre_close = pd.to_numeric(
                    invalid_rows["pre_close"], errors="coerce",
                ).to_numpy(dtype=np.float64)
                raw_change = pd.to_numeric(
                    invalid_rows["change"], errors="coerce",
                ).to_numpy(dtype=np.float64)
                undefined_pct_chg = (
                    invalid_rows["pct_chg"].isna().to_numpy()
                    & np.isnan(change.iloc[invalid_positions].to_numpy(dtype=np.float64))
                )
                provenance_missing = (
                    undefined_pct_chg
                    & invalid_rows["pre_close"].isna().to_numpy()
                    & invalid_rows["change"].isna().to_numpy()
                    & np.isnan(pre_close)
                    & np.isnan(raw_change)
                )
                # More than one missing-return row for the same code is not a
                # history boundary and must continue to fail closed.
                one_missing_row_per_code = ~invalid_rows["ts_code"].duplicated(
                    keep=False,
                ).to_numpy()
                boundary_match = np.zeros(len(invalid_rows), dtype=bool)
                if allow_legacy_initial_pct_chg:
                    affected_codes = invalid_rows["ts_code"].unique()
                    affected_history = result.loc[result["ts_code"].isin(affected_codes)]
                    unique_history_keys = ~affected_history.duplicated(
                        ["ts_code", "trade_date"], keep=False,
                    )
                    unique_key_set = set(
                        affected_history.loc[
                            unique_history_keys, ["ts_code", "trade_date"]
                        ].itertuples(index=False, name=None)
                    )
                    first_dates = affected_history.groupby(
                        "ts_code", sort=False,
                    )["trade_date"].min()
                    first_date_match = invalid_rows["trade_date"].eq(
                        invalid_rows["ts_code"].map(first_dates),
                    ).to_numpy()
                    unique_key_match = np.fromiter(
                        (
                            (code, trade_date) in unique_key_set
                            for code, trade_date in invalid_rows[
                                ["ts_code", "trade_date"]
                            ].itertuples(index=False, name=None)
                        ),
                        dtype=bool,
                        count=len(invalid_rows),
                    )
                    boundary_match = first_date_match & unique_key_match
                elif certified_legacy_initial_pct_chg_keys is not None:
                    certified = certified_legacy_initial_pct_chg_keys
                    locally_unique_keys = ~result.duplicated(
                        ["ts_code", "trade_date"], keep=False,
                    )
                    certified_match = np.fromiter(
                        (
                            (str(code), str(trade_date)) in certified
                            for code, trade_date in invalid_rows[
                                ["ts_code", "trade_date"]
                            ].itertuples(index=False, name=None)
                        ),
                        dtype=bool,
                        count=len(invalid_rows),
                    )
                    boundary_match = (
                        certified_match
                        & locally_unique_keys.iloc[invalid_positions].to_numpy()
                    )
                allowed_change[invalid_positions] = (
                    provenance_missing & one_missing_row_per_code & boundary_match
                )

            rejected_change = invalid_change & ~allowed_change
            if allow_legacy_initial_pct_chg and allowed_change.any():
                log.warning(
                    "%s contains %s legacy initial rows without prior close; "
                    "preserving undefined pct_chg as NaN",
                    source,
                    int(allowed_change.sum()),
                )
            if rejected_change.any():
                sample = (
                    result.loc[rejected_change, ["ts_code", "trade_date", "pct_chg"]]
                    .head(5).to_dict("records")
                )
                raise RuntimeError(
                    f"{source} contains invalid pct_chg: "
                    f"count={int(rejected_change.sum())}, sample={sample}"
                )
        result["pct_chg"] = change

    return result


def _invalid_adj_factor_mask(frame: pd.DataFrame) -> pd.Series:
    values = pd.to_numeric(frame["adj_factor"], errors="coerce")
    numeric = values.to_numpy(dtype=np.float64)
    return pd.Series(~np.isfinite(numeric) | (numeric <= 0), index=frame.index)


def _normalize_adj_factor_rows(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    required = {"ts_code", "trade_date", "adj_factor"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"{source} missing columns: {missing}")
    result = frame.copy()
    result["ts_code"] = result["ts_code"].astype(str).str.upper().str.strip()
    valid_codes = result["ts_code"].str.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", na=False)
    if not valid_codes.all():
        sample = result.loc[~valid_codes, "ts_code"].head(5).tolist()
        raise RuntimeError(f"{source} contains invalid ts_code: sample={sample}")
    raw_dates = result["trade_date"].astype(str).str.replace("-", "", regex=False).str.strip()
    parsed = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
    if parsed.isna().any():
        sample = raw_dates[parsed.isna()].head(5).tolist()
        raise RuntimeError(f"{source} contains invalid trade_date: sample={sample}")
    result["trade_date"] = parsed.dt.strftime("%Y%m%d")
    result["adj_factor"] = pd.to_numeric(result["adj_factor"], errors="coerce")
    return result


def _adj_factor_query_windows(
    code_rows: pd.DataFrame,
    missing_dates: list[str],
) -> list[tuple[str, str]]:
    """Bound source queries and include adjacent valid rows as consistency anchors."""
    parsed_missing = pd.to_datetime(pd.Series(missing_dates), format="%Y%m%d")
    bucket = parsed_missing.dt.year // ADJ_FACTOR_REPAIR_BUCKET_YEARS
    valid = code_rows.loc[~_invalid_adj_factor_mask(code_rows)].sort_values("trade_date")
    windows: list[tuple[str, str]] = []
    for _, dates in parsed_missing.groupby(bucket):
        start = dates.min().strftime("%Y%m%d")
        end = dates.max().strftime("%Y%m%d")
        previous = valid.loc[valid["trade_date"].lt(start), "trade_date"]
        following = valid.loc[valid["trade_date"].gt(end), "trade_date"]
        if not previous.empty:
            start = str(previous.iloc[-1])
        if not following.empty:
            end = str(following.iloc[0])
        windows.append((start, end))
    return windows


def _validated_original_adj_factors(
    pro,
    history: pd.DataFrame,
    invalid_rows: pd.DataFrame,
) -> tuple[dict[tuple[str, str], float], list[str]]:
    """Fetch exact factors and reject source/local conflicts at valid anchor dates."""
    valid_local = history.loc[~_invalid_adj_factor_mask(history)].copy()
    local_by_key: dict[tuple[str, str], float] = {}
    for key, rows in valid_local.groupby(["ts_code", "trade_date"], sort=False):
        values = rows["adj_factor"].to_numpy(dtype=np.float64)
        if not np.allclose(values, values[0], rtol=1e-10, atol=1e-12):
            raise RuntimeError(f"local adj_factor conflict for {key[0]} {key[1]}")
        local_by_key[(str(key[0]), str(key[1]))] = float(values[0])

    fetched: dict[tuple[str, str], float] = {}
    source_errors: list[str] = []
    for code, missing_for_code in invalid_rows.groupby("ts_code", sort=True):
        code_rows = history.loc[history["ts_code"].eq(code)].copy()
        missing_dates = sorted(missing_for_code["trade_date"].astype(str).unique())
        for start_date, end_date in _adj_factor_query_windows(code_rows, missing_dates):
            try:
                raw = pro.adj_factor(
                    ts_code=str(code),
                    start_date=start_date,
                    end_date=end_date,
                )
                if raw is None or raw.empty:
                    raise RuntimeError("empty response")
                source = _normalize_adj_factor_rows(
                    pd.DataFrame(raw),
                    f"adj_factor source {code} {start_date}~{end_date}",
                )
                unexpected = source.loc[~source["ts_code"].eq(code), "ts_code"].unique().tolist()
                if unexpected:
                    raise RuntimeError(f"unexpected ts_code values: {unexpected[:5]}")
                invalid_source = _invalid_adj_factor_mask(source)
                if invalid_source.any():
                    dates = source.loc[invalid_source, "trade_date"].head(5).tolist()
                    raise RuntimeError(f"non-positive/non-finite source factors: {dates}")
                for trade_date, rows in source.groupby("trade_date", sort=False):
                    values = rows["adj_factor"].to_numpy(dtype=np.float64)
                    if not np.allclose(values, values[0], rtol=1e-10, atol=1e-12):
                        raise RuntimeError(f"conflicting source factors on {trade_date}")
                    value = float(values[0])
                    key = (str(code), str(trade_date))
                    local_value = local_by_key.get(key)
                    if local_value is not None and not np.isclose(
                        value, local_value, rtol=1e-8, atol=1e-10,
                    ):
                        raise RuntimeError(
                            f"source/local anchor conflict on {trade_date}: "
                            f"source={value}, local={local_value}"
                        )
                    existing = fetched.get(key)
                    if existing is not None and not np.isclose(
                        value, existing, rtol=1e-10, atol=1e-12,
                    ):
                        raise RuntimeError(f"conflicting source windows on {trade_date}")
                    fetched[key] = value
            except Exception as exc:
                source_errors.append(
                    f"{code} {start_date}~{end_date}: {type(exc).__name__}: {exc}"
                )
    return fetched, source_errors


def _authoritative_listing_boundaries(
    pro,
    codes: list[str],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return exact Tushare stock_basic listing boundaries for requested codes.

    A boundary is usable only when the source returns one unambiguous, valid
    ``list_date`` for the exact security code.  Missing or malformed metadata is
    recorded but never guessed from the price history.
    """
    boundaries: dict[str, str] = {}
    names: dict[str, str] = {}
    source_errors: list[str] = []
    stock_basic = getattr(pro, "stock_basic", None)
    if not callable(stock_basic):
        return boundaries, names, ["stock_basic unavailable on adj_factor source"]

    for code in sorted(set(codes)):
        try:
            raw = stock_basic(
                ts_code=code,
                fields="ts_code,name,list_date",
            )
            if raw is None or pd.DataFrame(raw).empty:
                raise RuntimeError("empty response")
            source = pd.DataFrame(raw).copy()
            required = {"ts_code", "list_date"}
            missing = sorted(required - set(source.columns))
            if missing:
                raise RuntimeError(f"missing columns: {missing}")
            source["ts_code"] = source["ts_code"].astype(str).str.upper().str.strip()
            exact = source.loc[source["ts_code"].eq(code)].copy()
            if exact.empty:
                raise RuntimeError("exact ts_code absent from response")
            raw_dates = (
                exact["list_date"].astype(str).str.replace("-", "", regex=False).str.strip()
            )
            parsed = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
            if parsed.isna().any():
                raise RuntimeError(
                    f"invalid list_date values: {raw_dates[parsed.isna()].head(5).tolist()}"
                )
            dates = sorted(parsed.dt.strftime("%Y%m%d").unique().tolist())
            if len(dates) != 1:
                raise RuntimeError(f"ambiguous list_date values: {dates[:5]}")
            boundaries[code] = dates[0]
            if "name" in exact.columns:
                nonempty_names = [
                    value.strip()
                    for value in exact["name"].astype(str).tolist()
                    if value.strip() and value.strip().lower() != "nan"
                ]
                if nonempty_names:
                    names[code] = nonempty_names[0]
        except Exception as exc:
            source_errors.append(f"{code}: {type(exc).__name__}: {exc}")
    return boundaries, names, source_errors


def _exclude_pre_listing_identity_rows(
    frame: pd.DataFrame,
    pro,
    original_invalid: pd.Series,
) -> tuple[pd.DataFrame, set[tuple[str, str]], dict[str, object]]:
    """Exclude predecessor history only with an authoritative identity boundary.

    Tushare occasionally reuses a current ``ts_code`` for rows belonging to a
    predecessor security.  ``stock_basic.list_date`` is the evidence boundary:
    all rows before it are excluded from the current instrument build, while the
    source parquet is intentionally left untouched as a quarantined audit trail.
    """
    affected_codes = sorted(
        frame.loc[original_invalid, "ts_code"].astype(str).unique().tolist()
    )
    boundaries, names, source_errors = _authoritative_listing_boundaries(
        pro, affected_codes,
    )
    excluded = pd.Series(False, index=frame.index)
    provenance: list[dict[str, object]] = []
    for code, list_date in sorted(boundaries.items()):
        code_rows = frame["ts_code"].eq(code)
        pre_listing = code_rows & frame["trade_date"].lt(list_date)
        if not pre_listing.any():
            continue
        excluded |= pre_listing
        record: dict[str, object] = {
            "ts_code": code,
            "list_date": list_date,
            "source": "tushare.stock_basic",
            "excluded_rows": int(pre_listing.sum()),
            "excluded_invalid_rows": int((pre_listing & original_invalid).sum()),
        }
        if code in names:
            record["name"] = names[code]
        provenance.append(record)

    excluded_keys = {
        (str(row.ts_code), str(row.trade_date))
        for row in frame.loc[excluded, ["ts_code", "trade_date"]].itertuples(index=False)
    }
    summary: dict[str, object] = {
        "identity_quarantined_rows": int(excluded.sum()),
        "identity_quarantined_invalid_rows": int((excluded & original_invalid).sum()),
        "identity_quarantined_codes": len(provenance),
        "identity_boundary_source": "tushare.stock_basic.list_date",
        "identity_exclusion_persistence": "build_filter_source_parquet_unchanged",
        "identity_boundaries": provenance,
        "identity_source_errors": source_errors[:10],
    }
    return frame.loc[~excluded].copy(), excluded_keys, summary


def _repair_historical_adj_factors(
    frame: pd.DataFrame,
    pro,
) -> tuple[
    pd.DataFrame,
    dict[str, object],
    dict[tuple[str, str], float],
    set[tuple[str, str]],
]:
    """Quarantine evidenced predecessor rows, then repair only safe current rows.

    ``stock_basic.list_date`` may exclude predecessor history without inventing
    factors.  Remaining rows use exact source factors or an auditable internal
    run bounded by equal factors; leading/trailing or changed-factor runs fail.
    """
    work = _normalize_adj_factor_rows(frame, "historical equity parquet")
    original_invalid = _invalid_adj_factor_mask(work)
    invalid_count = int(original_invalid.sum())
    if not invalid_count:
        return work, {
            "invalid_rows": 0,
            "repaired_rows": 0,
            "original_source_rows": 0,
            "bounded_previous_rows": 0,
            "unresolved_rows": 0,
            "affected_codes": 0,
            "source_errors": [],
            "identity_quarantined_rows": 0,
            "identity_quarantined_invalid_rows": 0,
            "identity_quarantined_codes": 0,
            "identity_boundary_source": "tushare.stock_basic.list_date",
            "identity_exclusion_persistence": "build_filter_source_parquet_unchanged",
            "identity_boundaries": [],
            "identity_source_errors": [],
        }, {}, set()

    affected_code_count = int(work.loc[original_invalid, "ts_code"].nunique())
    work, excluded_keys, identity_summary = _exclude_pre_listing_identity_rows(
        work, pro, original_invalid,
    )
    remaining_invalid = _invalid_adj_factor_mask(work)

    invalid_rows = work.loc[remaining_invalid, ["ts_code", "trade_date", "adj_factor"]]
    duplicate_invalid = invalid_rows.duplicated(["ts_code", "trade_date"], keep=False)
    if duplicate_invalid.any():
        sample = invalid_rows.loc[duplicate_invalid, ["ts_code", "trade_date"]].head(5)
        raise RuntimeError(
            "cannot safely repair duplicate invalid adj_factor keys: "
            f"sample={sample.to_dict('records')}"
        )

    fetched: dict[tuple[str, str], float] = {}
    source_errors: list[str] = []
    if not invalid_rows.empty:
        fetched, source_errors = _validated_original_adj_factors(pro, work, invalid_rows)
    repair_methods: dict[tuple[str, str], str] = {}
    for index in work.index[remaining_invalid]:
        key = (str(work.at[index, "ts_code"]), str(work.at[index, "trade_date"]))
        if key in fetched:
            work.at[index, "adj_factor"] = fetched[key]
            repair_methods[key] = "original_source"

    # Source APIs can omit a suspended/legacy row.  Only an internal run whose
    # preceding and following factors are equal may inherit that prior value.
    for code, rows in work.groupby("ts_code", sort=False):
        ordered_indices = rows.sort_values("trade_date").index.tolist()
        position = 0
        while position < len(ordered_indices):
            index = ordered_indices[position]
            if not bool(_invalid_adj_factor_mask(work.loc[[index]]).iloc[0]):
                position += 1
                continue
            run_start = position
            while position < len(ordered_indices):
                candidate = ordered_indices[position]
                if not bool(_invalid_adj_factor_mask(work.loc[[candidate]]).iloc[0]):
                    break
                position += 1
            run_end = position
            if run_start == 0 or run_end >= len(ordered_indices):
                continue
            previous_index = ordered_indices[run_start - 1]
            following_index = ordered_indices[run_end]
            previous = float(work.at[previous_index, "adj_factor"])
            following = float(work.at[following_index, "adj_factor"])
            if not (
                np.isfinite(previous)
                and previous > 0
                and np.isclose(previous, following, rtol=1e-10, atol=1e-12)
            ):
                continue
            for repair_index in ordered_indices[run_start:run_end]:
                key = (
                    str(work.at[repair_index, "ts_code"]),
                    str(work.at[repair_index, "trade_date"]),
                )
                work.at[repair_index, "adj_factor"] = previous
                repair_methods[key] = "bounded_previous"

    unresolved = _invalid_adj_factor_mask(work)
    unresolved_rows = work.loc[unresolved, ["ts_code", "trade_date", "adj_factor"]]
    source_count = sum(value == "original_source" for value in repair_methods.values())
    bounded_count = sum(value == "bounded_previous" for value in repair_methods.values())
    summary: dict[str, object] = {
        "invalid_rows": invalid_count,
        "repaired_rows": len(repair_methods),
        "original_source_rows": source_count,
        "bounded_previous_rows": bounded_count,
        "unresolved_rows": int(unresolved.sum()),
        "affected_codes": affected_code_count,
        "source_errors": source_errors[:10],
        "unresolved_sample": unresolved_rows.head(10).to_dict("records"),
        **identity_summary,
    }
    if unresolved.any():
        _write_adj_factor_repair_status("failed", stage="resolve", **summary)
        raise RuntimeError(
            "adj_factor repair unresolved: "
            f"invalid={invalid_count}, source={source_count}, bounded_previous={bounded_count}, "
            f"unresolved={int(unresolved.sum())}, "
            f"sample={summary['unresolved_sample']}, source_errors={source_errors[:3]}"
        )

    repairs = {
        key: float(work.loc[
            work["ts_code"].eq(key[0]) & work["trade_date"].eq(key[1]),
            "adj_factor",
        ].iloc[-1])
        for key in repair_methods
    }
    return work, summary, repairs, excluded_keys


def _persist_adj_factor_repairs(
    files: list[Path],
    frames: list[pd.DataFrame],
    repairs: dict[tuple[str, str], float],
    excluded_keys: set[tuple[str, str]] | None = None,
    certified_legacy_initial_pct_chg_keys: set[tuple[str, str]] | None = None,
) -> int:
    """Stage and validate every affected parquet before replacing the first."""
    if not repairs:
        return 0
    excluded_keys = excluded_keys or set()
    staged: list[tuple[Path, Path]] = []
    try:
        for path, original in zip(files, frames):
            work = _normalize_adj_factor_rows(original, path.name)
            invalid = _invalid_adj_factor_mask(work)
            if not invalid.any():
                continue
            changed = False
            for index in work.index[invalid]:
                key = (str(work.at[index, "ts_code"]), str(work.at[index, "trade_date"]))
                value = repairs.get(key)
                if value is not None:
                    work.at[index, "adj_factor"] = value
                    changed = True
            if not changed:
                continue
            kept = pd.Series([
                (str(code), str(trade_date)) not in excluded_keys
                for code, trade_date in zip(work["ts_code"], work["trade_date"])
            ], index=work.index)
            if _invalid_adj_factor_mask(work.loc[kept]).any():
                raise RuntimeError(f"staged adj_factor repair is incomplete for {path.name}")
            tmp = path.with_name(f".{path.name}.{os.getpid()}.adj-repair.tmp")
            work.to_parquet(tmp, index=False)
            _validate_equity_history(
                pd.read_parquet(tmp).loc[kept.to_numpy()].reset_index(drop=True),
                source=f"staged {path.name}",
                repair_legacy_envelope=True,
                certified_legacy_initial_pct_chg_keys=(
                    certified_legacy_initial_pct_chg_keys
                ),
            )
            staged.append((path, tmp))
        for path, tmp in staged:
            os.replace(tmp, path)
        return len(staged)
    finally:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)


def _valid_daily_parquet(path: Path, trade_date: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        frame = _validate_equity_history(pd.read_parquet(path), source=path.name)
        dates = frame["trade_date"].astype(str)
        return bool(dates.eq(trade_date).all())
    except Exception:
        return False


def _download_start(end_ts: pd.Timestamp, lookback_days: int) -> pd.Timestamp:
    rolling_start = end_ts - pd.Timedelta(days=lookback_days)
    published_dates = []
    for path in PARQUET_DIR.glob("*.parquet"):
        stem = path.stem
        if len(stem) != 8 or not stem.isdigit():
            continue
        try:
            candidate = pd.Timestamp(datetime.strptime(stem, "%Y%m%d"))
        except ValueError:
            continue
        if candidate <= end_ts:
            published_dates.append(candidate)
    if not published_dates:
        return rolling_start
    # Include an overlap before the last published day to repair recent holes;
    # if that day is months old, this naturally covers the entire outage gap.
    catchup_start = max(published_dates) - pd.Timedelta(days=lookback_days)
    return min(rolling_start, catchup_start)


def download_recent(
    pro,
    end: str | None = None,
    lookback_days: int = DOWNLOAD_LOOKBACK_DAYS,
    sleep: float = 0.4,
):
    """Download and validate every missing recent trading day.

    A wider rolling window repairs a day that failed while later days succeeded;
    the previous five-calendar-day window could leave that hole permanently.
    """
    end = end or datetime.now().strftime("%Y%m%d")
    end_ts = pd.Timestamp(datetime.strptime(end, "%Y%m%d"))
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    start = _download_start(end_ts, lookback_days).strftime("%Y%m%d")
    log.info(f"checking trade calendar {start} ~ {end}")
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    if cal is None or cal.empty or "cal_date" not in cal:
        raise RuntimeError(f"trade_cal returned no open dates for {start} ~ {end}")
    dates = sorted(set(cal["cal_date"].astype(str).tolist()))

    todo = [
        d for d in dates
        if not _valid_daily_parquet(PARQUET_DIR / f"{d}.parquet", d)
    ]
    log.info(f"to download: {len(todo)} (skipping {len(dates) - len(todo)} already done)")

    failures = []
    for td in todo:
        target = PARQUET_DIR / f"{td}.parquet"
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            daily = pro.daily(trade_date=td)
            if daily is None or daily.empty:
                raise RuntimeError("daily returned no rows")
            time.sleep(sleep)
            adj = pro.adj_factor(trade_date=td)
            if adj is None or adj.empty:
                raise RuntimeError("adj_factor returned no rows")
            required = {"ts_code", "trade_date", "adj_factor"}
            if not required.issubset(adj.columns):
                raise RuntimeError(f"adj_factor missing columns: {sorted(required - set(adj.columns))}")
            daily = daily.copy()
            adj = adj.copy()
            daily["trade_date"] = daily["trade_date"].astype(str)
            adj["trade_date"] = adj["trade_date"].astype(str)
            adj = adj.drop_duplicates(["ts_code", "trade_date"], keep="last")
            merged = daily.merge(
                adj[["ts_code", "trade_date", "adj_factor"]],
                on=["ts_code", "trade_date"], how="left",
            )
            merged["adj_factor"] = pd.to_numeric(merged["adj_factor"], errors="coerce")
            missing_adj = int(merged["adj_factor"].isna().sum())
            invalid_adj = int(merged["adj_factor"].le(0).sum())
            if missing_adj or invalid_adj:
                raise RuntimeError(
                    f"adj_factor incomplete: missing={missing_adj}, non_positive={invalid_adj}"
                )
            merged = _validate_equity_history(merged, source=f"download {td}")
            merged.to_parquet(tmp, index=False)
            if not _valid_daily_parquet(tmp, td):
                raise RuntimeError("written parquet failed validation")
            os.replace(tmp, target)
            log.info(f"  saved {td} ({len(merged)} rows)")
            time.sleep(sleep)
        except Exception as e:
            log.error(f"{td}: failed: {e}")
            failures.append(f"{td}: {e}")
        finally:
            tmp.unlink(missing_ok=True)

    if failures:
        preview = "; ".join(failures[:5])
        raise RuntimeError(f"daily download incomplete ({len(failures)} dates): {preview}")
    return {"dates": dates, "downloaded": len(todo), "skipped": len(dates) - len(todo)}


# ---------- step 2: build qlib bin ----------


def _require_prefix_calendar(
    prospective: list[str],
    published_calendars_dir: Path | None = None,
) -> None:
    """Reject historical calendar insertion before it can shift live bin indices."""
    path = (Path(published_calendars_dir) if published_calendars_dir is not None else CALENDARS_DIR) / "day.txt"
    if not path.exists():
        return
    published = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(prospective) < len(published) or prospective[:len(published)] != published:
        mismatch = next(
            (
                index
                for index, (old, new) in enumerate(zip(published, prospective))
                if old != new
            ),
            min(len(published), len(prospective)),
        )
        old_value = published[mismatch] if mismatch < len(published) else "<missing>"
        new_value = prospective[mismatch] if mismatch < len(prospective) else "<missing>"
        raise RuntimeError(
            "prospective calendar is not a tail-only extension of the published calendar: "
            f"index={mismatch}, published={old_value}, prospective={new_value}; "
            "historical holes require an offline staged rebuild"
        )


def build_qlib_bin(
    publish_calendar: bool = True,
    adj_factor_source=None,
    features_dir: Path | None = None,
    instruments_dir: Path | None = None,
    calendars_dir: Path | None = None,
    published_calendars_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Rebuild calendar, instruments, and per-stock bin files from all parquet."""
    target_features_dir = Path(features_dir) if features_dir is not None else FEATURES_DIR
    target_instruments_dir = Path(instruments_dir) if instruments_dir is not None else INSTRUMENTS_DIR
    target_calendars_dir = Path(calendars_dir) if calendars_dir is not None else CALENDARS_DIR
    files = sorted(PARQUET_DIR.glob("*.parquet"))
    if not files:
        raise RuntimeError(f"no parquet files in {PARQUET_DIR}")
    log.info(f"loading {len(files)} parquet files ...")

    dfs = []
    for i, f in enumerate(files, 1):
        dfs.append(pd.read_parquet(f))
        if i % 1000 == 0:
            log.info(f"  loaded {i}/{len(files)}")
    df = pd.concat(dfs, ignore_index=True)
    log.info(f"total rows: {len(df):,}")

    # Validate the entire historical input before creating a feature directory
    # or replacing any published qlib file.
    repair_summary: dict[str, object] | None = None
    repairs: dict[tuple[str, str], float] = {}
    excluded_identity_keys: set[tuple[str, str]] = set()
    if adj_factor_source is not None:
        try:
            df, repair_summary, repairs, excluded_identity_keys = _repair_historical_adj_factors(
                df, adj_factor_source,
            )
        except Exception as exc:
            if not str(exc).startswith("adj_factor repair unresolved:"):
                _write_adj_factor_repair_status(
                    "failed",
                    stage="source_validation",
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                )
            raise
    try:
        df = _validate_equity_history(
            df,
            source="historical equity parquet",
            repair_legacy_envelope=True,
            allow_legacy_initial_pct_chg=True,
        )
        legacy_initial_pct_chg_keys: set[tuple[str, str]] = set()
        if "pct_chg" in df.columns:
            pct_chg_values = pd.to_numeric(df["pct_chg"], errors="coerce")
            undefined_pct_chg = ~np.isfinite(
                pct_chg_values.to_numpy(dtype=np.float64),
            )
            legacy_initial_pct_chg_keys = set(
                df.loc[
                    undefined_pct_chg, ["ts_code", "trade_date"]
                ].itertuples(index=False, name=None)
            )
        files_rewritten = _persist_adj_factor_repairs(
            files,
            dfs,
            repairs,
            excluded_identity_keys,
            legacy_initial_pct_chg_keys,
        )
    except Exception as exc:
        if adj_factor_source is not None:
            _write_adj_factor_repair_status(
                "failed",
                stage="history_validation_or_persist",
                error=f"{type(exc).__name__}: {exc}"[:2000],
                **(repair_summary or {}),
            )
        raise
    if repair_summary is not None:
        _write_adj_factor_repair_status(
            "staged",
            stage="history_validated_and_repairs_persisted",
            intended_outcome=(
                "repaired" if repairs else
                "identity_quarantined" if excluded_identity_keys else
                "clean"
            ),
            parquet_files_rewritten=files_rewritten,
            **repair_summary,
        )
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df["code"] = df["ts_code"].map(_ts_code_to_qlib)

    # Build against the prospective calendar, but publish it only after every
    # equity bin is complete. Readers therefore never see a new date pointing
    # at an old or partially-written bin.
    cals = sorted(df["trade_date"].dt.strftime("%Y-%m-%d").unique())
    _require_prefix_calendar(cals, published_calendars_dir=published_calendars_dir)

    cal_idx = {d: i for i, d in enumerate(cals)}
    df["cal_idx"] = df["trade_date"].dt.strftime("%Y-%m-%d").map(cal_idx)

    # Stable internal normalization. Readers convert this to qfq/hfq/raw.
    max_adj = df.groupby("code")["adj_factor"].transform("max")
    normalized_ratio = df["adj_factor"] / max_adj
    for c in ("open", "close", "high", "low"):
        df[c] = (df[c] * normalized_ratio).astype("float32")
    df["volume"] = df["vol"].astype("float32") if "vol" in df.columns else df["volume"].astype("float32")
    df["change"] = (df["pct_chg"].astype("float32") / 100.0) if "pct_chg" in df.columns else 0.0
    df["factor"] = np.float32(1.0)
    df["adj"] = df["adj_factor"].astype("float32")  # 真实 adj_factor, 用于复权计算

    target_features_dir.mkdir(parents=True, exist_ok=True)
    # Build the row index once. Filtering the full market DataFrame inside the
    # stock loop turns a rebuild into O(total_rows * stocks) and can take hours.
    grouped = df.groupby("code", sort=False, observed=True)
    code_count = grouped.ngroups
    log.info(f"writing per-stock bin files for {code_count} stocks ...")
    instruments = []
    for i, (code, sub) in enumerate(grouped, 1):
        sub = sub.sort_values("cal_idx")
        if sub.empty:
            continue
        start_idx = int(sub["cal_idx"].iloc[0])
        last_idx = int(sub["cal_idx"].iloc[-1])
        # !! 关键: qlib bin 格式假设从 start_idx 起逐日连续. 停牌日(全市场在交易、
        # 该股无数据)必须填充占位, 否则停牌日之后的所有值整体前移、与日期错位.
        # reindex 到 [start_idx, last_idx] 连续区间, 停牌日前向填充.
        sub = (sub.drop_duplicates("cal_idx", keep="last")
                  .set_index("cal_idx")
                  .reindex(range(start_idx, last_idx + 1)))
        susp = sub["close"].isna()                 # 停牌日掩码
        sub["close"] = sub["close"].ffill()
        for c in ("open", "high", "low"):          # 停牌日 O=H=L=前一日收盘 (平盘)
            sub[c] = sub[c].where(~susp, sub["close"])
        sub["adj"] = sub["adj"].ffill()            # 复权因子停牌期间不变
        sub["volume"] = sub["volume"].fillna(0.0)  # 停牌日无成交
        # A synthetic suspension row has a defined zero return.  A real
        # instrument's first row without pre_close does not; retain its NaN so
        # downstream research cannot mistake an invented zero for an observed
        # listing-day return.
        sub["change"] = sub["change"].where(~susp, 0.0)
        sub["factor"] = np.float32(1.0)
        stock_dir = target_features_dir / code
        stock_dir.mkdir(parents=True, exist_ok=True)
        for field in FIELDS:
            values = sub[field].to_numpy(dtype="<f4")
            arr = np.hstack([np.float32(start_idx), values]).astype("<f4")
            _atomic_write_bytes(stock_dir / f"{field}.day.bin", arr.tobytes())
        instruments.append(f"{code}\t{cals[start_idx]}\t{cals[last_idx]}")
        if i % 500 == 0:
            log.info(f"  wrote {i}/{code_count}")

    target_instruments_dir.mkdir(parents=True, exist_ok=True)
    instruments_path = target_instruments_dir / "all.txt"
    if instruments_path.exists():
        benchmark_codes = {_ts_code_to_qlib(value) for value in BENCHMARK_INDEX_TS_CODES}
        for line in instruments_path.read_text(encoding="utf-8").splitlines():
            code = line.split("\t", 1)[0]
            if code in benchmark_codes:
                instruments.append(line)
    if publish_calendar:
        _atomic_write_text(instruments_path, "\n".join(instruments) + "\n")
        log.info(f"instruments: {len(instruments)} stocks -> {target_instruments_dir / 'all.txt'}")
    else:
        log.info(f"instruments prepared: {len(instruments)} stocks")

    if publish_calendar:
        target_calendars_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target_calendars_dir / "day.txt", "\n".join(cals) + "\n")
        log.info(f"calendar: {len(cals)} days, {cals[0]} ~ {cals[-1]}")
    else:
        log.info(f"calendar prepared: {len(cals)} days, {cals[0]} ~ {cals[-1]}")
    if repair_summary is not None:
        if publish_calendar:
            _complete_staged_adj_factor_repair_status("build_qlib_bin")
        else:
            _write_adj_factor_repair_status(
                "staged",
                stage="equity_build_complete_awaiting_pipeline_publication",
                intended_outcome=(
                    "repaired" if repairs else
                    "identity_quarantined" if excluded_identity_keys else
                    "clean"
                ),
                parquet_files_rewritten=files_rewritten,
                **repair_summary,
            )
    return cals, instruments


def main():
    t0 = time.time()
    with qlib_update_lock():
        recovered = recover_incomplete_qlib_publications(QLIB_DATA_PATH)
        if recovered:
            log.warning("recovered interrupted Qlib publications: %s", ", ".join(recovered))
        pro = _pro()
        download_recent(pro)
        with _QlibStagedPublication(QLIB_DATA_PATH) as publication:
            calendars, equity_instruments = build_qlib_bin(
                publish_calendar=False,
                adj_factor_source=pro,
                features_dir=publication.features_dir,
                instruments_dir=publication.instruments_dir,
                calendars_dir=publication.calendars_dir,
                published_calendars_dir=CALENDARS_DIR,
            )
            all_instruments = refresh_benchmark_index_bins(
                pro,
                calendars=calendars,
                base_instruments=equity_instruments,
                publish_instruments=False,
                features_dir=publication.features_dir,
                instruments_dir=publication.instruments_dir,
                calendars_dir=publication.calendars_dir,
            )
            _atomic_write_text(
                publication.instruments_dir / "all.txt",
                "\n".join(all_instruments) + "\n",
            )
            _atomic_write_text(
                publication.calendars_dir / "day.txt",
                "\n".join(calendars) + "\n",
            )
            publication.commit()
        _complete_staged_adj_factor_repair_status("main_after_equity_benchmark_publication")
        log.info(
            "calendar published after equities and benchmarks: %s days, %s ~ %s",
            len(calendars), calendars[0], calendars[-1],
        )
    log.info(f"DONE in {(time.time()-t0)/60:.1f}min")


if __name__ == "__main__":
    main()
