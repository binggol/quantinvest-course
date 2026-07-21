"""Fetch Tushare financial indicators into a point-in-time SQLite store.

``fina_indicators`` remains the one-row-per-report compatibility table used by
the existing application.  The authoritative table is
``fina_indicator_versions``: it is append-only and retains every distinct
disclosure/revision observed from the source.  Historical consumers should use
``query_fina_indicators_as_of`` instead of the compatibility table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import tushare as ts


DB_PATH = Path(os.environ.get("FINANCIALS_DB", "/app/data/financials.db"))
STOCK_META_DB = Path(os.environ.get("STOCK_META_DB", "/app/data/stock_meta.db"))
TOKEN = os.environ.get("TUSHARE_TOKEN", "")
START_DATE = "20210101"
SLEEP = float(os.environ.get("FINA_SLEEP", "0.15"))
SOURCE_NAME = "tushare.fina_indicator"

# ``update_flag`` is present on current Tushare fina_indicator responses, but
# older deployments/proxies may reject it.  _fetch_indicator retries with only
# the core fields when the API reports an unsupported-field error.
TS_CORE_FIELDS = [
    "ts_code",
    "ann_date",
    "end_date",
    "profit_dedt",
    "roe",
    "roe_dt",
    "q_dtprofit",
]
TS_OPTIONAL_FIELDS = ["update_flag"]
TS_FIELDS = TS_CORE_FIELDS + TS_OPTIONAL_FIELDS
TS_TO_DB = {"profit_dedt": "dt_profit_to_holder"}

COMPAT_FIELDS = [
    "ts_code",
    "ann_date",
    "end_date",
    "dt_profit_to_holder",
    "roe",
    "roe_dt",
    "q_dtprofit",
]
# Backwards-compatible name used by older one-off maintenance scripts.
FIELDS = COMPAT_FIELDS
VALUE_FIELDS = ["dt_profit_to_holder", "roe", "roe_dt", "q_dtprofit"]
VERSION_FIELDS = ["ts_code", "ann_date", "end_date", "update_flag", *VALUE_FIELDS]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fetch_financials")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _normalise_text(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    result = str(value).strip()
    return result or None


def _normalise_number(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _normalise_date(value) -> str | None:
    text = _normalise_text(value)
    if text is None:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else text


def _version_hash(record: dict) -> str:
    payload = [record.get(field) for field in VERSION_FIELDS]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_scoped_hash(source: str, content_hash: str) -> str:
    """Deduplicate refetches without hiding native provenance behind migration rows."""

    return hashlib.sha256(f"{source}\0{content_hash}".encode("utf-8")).hexdigest()


def _records_from_frame(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    work = df.rename(columns=TS_TO_DB).copy()
    for field in VERSION_FIELDS:
        if field not in work.columns:
            work[field] = None

    records: list[dict] = []
    for raw in work[VERSION_FIELDS].to_dict(orient="records"):
        record = {
            "ts_code": _normalise_text(raw["ts_code"]),
            "ann_date": _normalise_date(raw["ann_date"]),
            "end_date": _normalise_date(raw["end_date"]),
            "update_flag": _normalise_text(raw["update_flag"]),
            **{field: _normalise_number(raw[field]) for field in VALUE_FIELDS},
        }
        if not record["ts_code"] or not record["end_date"]:
            log.warning("skip financial row without ts_code/end_date: %s", raw)
            continue
        record["content_hash"] = _version_hash(record)
        records.append(record)
    return records


def _get_stock_list() -> list[str]:
    """Return all available L/D/P securities, including historical delistings.

    Some older stock_meta databases do not have ``list_status``.  In that case
    all distinct codes are included rather than silently reverting to a
    survivorship-biased current-listing universe.
    """
    if not STOCK_META_DB.exists():
        raise RuntimeError(f"stock_meta.db does not exist: {STOCK_META_DB}")
    with sqlite3.connect(STOCK_META_DB) as conn:
        columns = _table_columns(conn, "stock_meta")
        if "ts_code" not in columns:
            raise RuntimeError("stock_meta table has no ts_code column")
        if "list_status" in columns:
            rows = conn.execute(
                "SELECT DISTINCT ts_code FROM stock_meta "
                "WHERE UPPER(TRIM(COALESCE(list_status, ''))) IN ('L','D','P') "
                "ORDER BY ts_code"
            ).fetchall()
        else:
            log.warning("stock_meta.list_status is absent; fetching every ts_code")
            rows = conn.execute(
                "SELECT DISTINCT ts_code FROM stock_meta "
                "WHERE ts_code IS NOT NULL ORDER BY ts_code"
            ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def _create_compat_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fina_indicators (
            ts_code TEXT NOT NULL,
            ann_date TEXT,
            end_date TEXT NOT NULL,
            dt_profit_to_holder REAL,
            roe REAL,
            roe_dt REAL,
            q_dtprofit REAL,
            PRIMARY KEY (ts_code, end_date)
        )
        """
    )
    columns = _table_columns(conn, "fina_indicators")
    if "ts_code" not in columns or "end_date" not in columns:
        raise RuntimeError("legacy fina_indicators must contain ts_code and end_date")
    # Old local databases occasionally predate one of the optional metrics.
    # ALTER TABLE ADD COLUMN is additive and keeps all existing rows intact.
    expected_types = {
        "ann_date": "TEXT",
        "dt_profit_to_holder": "REAL",
        "roe": "REAL",
        "roe_dt": "REAL",
        "q_dtprofit": "REAL",
    }
    for column, sql_type in expected_types.items():
        if column not in columns:
            conn.execute(f'ALTER TABLE fina_indicators ADD COLUMN "{column}" {sql_type}')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_end ON fina_indicators(end_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_code ON fina_indicators(ts_code)")


def _create_version_store(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fina_indicator_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code TEXT NOT NULL,
            ann_date TEXT,
            end_date TEXT NOT NULL,
            update_flag TEXT,
            dt_profit_to_holder REAL,
            roe REAL,
            roe_dt REAL,
            q_dtprofit REAL,
            ingested_at TEXT NOT NULL,
            source TEXT NOT NULL,
            source_vintage TEXT NOT NULL,
            content_hash TEXT NOT NULL
        )
        """
    )
    # Identical snapshots from the same provider are idempotent.  Provenance is
    # deliberately part of the key so a legacy row can later be superseded by
    # an otherwise-identical native Tushare observation without losing lineage.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_fina_versions_source_content "
        "ON fina_indicator_versions(source, content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fina_versions_asof "
        "ON fina_indicator_versions(ann_date, ts_code, end_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fina_versions_report "
        "ON fina_indicator_versions(ts_code, end_date, ann_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS financials_schema_migrations (
            migration_name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_progress (
            ts_code TEXT PRIMARY KEY,
            last_fetched TEXT,
            n_rows INTEGER
        )
        """
    )


def _insert_version_records(
    conn: sqlite3.Connection,
    records: Iterable[dict],
    *,
    ingested_at: str,
    source: str,
    source_vintage: str,
) -> int:
    rows = [
        (
            record["ts_code"],
            record["ann_date"],
            record["end_date"],
            record["update_flag"],
            record["dt_profit_to_holder"],
            record["roe"],
            record["roe_dt"],
            record["q_dtprofit"],
            ingested_at,
            source,
            source_vintage,
            _source_scoped_hash(source, record["content_hash"]),
        )
        for record in records
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO fina_indicator_versions (
            ts_code, ann_date, end_date, update_flag,
            dt_profit_to_holder, roe, roe_dt, q_dtprofit,
            ingested_at, source, source_vintage, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return conn.total_changes - before


def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
    migration_name = "legacy_fina_indicators_to_versions_v1"
    if conn.execute(
        "SELECT 1 FROM financials_schema_migrations WHERE migration_name = ?",
        (migration_name,),
    ).fetchone():
        return

    columns = _table_columns(conn, "fina_indicators")
    select_columns = []
    for field in COMPAT_FIELDS:
        if field in columns:
            select_columns.append(f'"{field}" AS "{field}"')
        else:
            select_columns.append(f'NULL AS "{field}"')
    legacy = pd.read_sql_query(
        f"SELECT {', '.join(select_columns)} FROM fina_indicators", conn
    )
    records = _records_from_frame(legacy)
    applied_at = _utc_now()
    _insert_version_records(
        conn,
        records,
        ingested_at=applied_at,
        source="legacy.fina_indicators",
        source_vintage="pre-pit-migration",
    )
    conn.execute(
        "INSERT INTO financials_schema_migrations(migration_name, applied_at) VALUES (?, ?)",
        (migration_name, applied_at),
    )


def _init_db() -> None:
    """Create the PIT store and copy legacy rows without deleting/replacing them."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # A single SQLite transaction makes the additive migration atomic.
        _create_compat_table(conn)
        _create_version_store(conn)
        _migrate_legacy_rows(conn)


def _latest_version_for_report(
    conn: sqlite3.Connection, ts_code: str, end_date: str
) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT ts_code, ann_date, end_date,
               dt_profit_to_holder, roe, roe_dt, q_dtprofit
        FROM fina_indicator_versions
        WHERE ts_code = ? AND end_date = ?
        ORDER BY
            CASE WHEN ann_date IS NULL OR ann_date = '' THEN 0 ELSE 1 END DESC,
            ann_date DESC,
            CASE
                WHEN update_flag GLOB '[0-9]*' THEN CAST(update_flag AS INTEGER)
                ELSE 0
            END DESC,
            CASE WHEN source = 'legacy.fina_indicators' THEN 0 ELSE 1 END DESC,
            ingested_at DESC,
            version_id DESC
        LIMIT 1
        """,
        (ts_code, end_date),
    ).fetchone()


def _refresh_compat_snapshot(
    conn: sqlite3.Connection, report_keys: Iterable[tuple[str, str]]
) -> None:
    """Refresh current-value compatibility rows; version history stays immutable."""
    for ts_code, end_date in sorted(set(report_keys)):
        latest = _latest_version_for_report(conn, ts_code, end_date)
        if latest is None:
            continue
        values = tuple(latest[field] for field in COMPAT_FIELDS)
        update_values = (
            latest["ann_date"],
            latest["dt_profit_to_holder"],
            latest["roe"],
            latest["roe_dt"],
            latest["q_dtprofit"],
            latest["ts_code"],
            latest["end_date"],
        )
        cursor = conn.execute(
            """
            UPDATE fina_indicators
            SET ann_date = ?, dt_profit_to_holder = ?, roe = ?, roe_dt = ?, q_dtprofit = ?
            WHERE ts_code = ? AND end_date = ?
            """,
            update_values,
        )
        if cursor.rowcount == 0:
            conn.execute(
                """
                INSERT INTO fina_indicators (
                    ts_code, ann_date, end_date,
                    dt_profit_to_holder, roe, roe_dt, q_dtprofit
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )


def _upsert_rows(
    df: pd.DataFrame,
    *,
    ingested_at: str | None = None,
    source_vintage: str | None = None,
    source: str = SOURCE_NAME,
) -> int:
    """Append distinct versions and refresh the legacy-compatible snapshot.

    The function name is retained for callers, but financial source rows are
    never replaced.  An identical row fetched again is deduplicated by its
    deterministic content hash.
    """
    records = _records_from_frame(df)
    if not records:
        return 0
    observed_at = ingested_at or _utc_now()
    vintage = source_vintage or observed_at
    with sqlite3.connect(DB_PATH) as conn:
        inserted = _insert_version_records(
            conn,
            records,
            ingested_at=observed_at,
            source=source,
            source_vintage=vintage,
        )
        _refresh_compat_snapshot(
            conn,
            ((record["ts_code"], record["end_date"]) for record in records),
        )
    return inserted


def _as_yyyymmdd(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 8:
        raise ValueError(f"as_of must contain a full date, got {value!r}")
    return digits[:8]


def query_fina_indicators_as_of(
    as_of: str | date | datetime,
    *,
    db_path: str | Path | None = None,
    ts_codes: Iterable[str] | None = None,
    ingested_as_of: str | None = None,
) -> pd.DataFrame:
    """Return the latest version visible on ``as_of`` for each report period.

    ``ann_date`` is the source-effective date.  ``ingested_as_of`` is optional
    and can reproduce what this local database had observed at a prior instant;
    it should be an ISO-8601 UTC timestamp matching ``ingested_at``.
    Rows with no announcement date are deliberately excluded from PIT queries.
    """
    cutoff = _as_yyyymmdd(as_of)
    path = Path(db_path) if db_path is not None else DB_PATH
    filters = ["ann_date IS NOT NULL", "ann_date != ''", "ann_date <= ?"]
    params: list[object] = [cutoff]
    if ingested_as_of is not None:
        filters.append("ingested_at <= ?")
        params.append(ingested_as_of)
    codes = sorted({str(code) for code in ts_codes or [] if code})
    if codes:
        filters.append(f"ts_code IN ({','.join('?' for _ in codes)})")
        params.extend(codes)

    sql = f"""
        WITH visible AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY ts_code, end_date
                       ORDER BY ann_date DESC,
                                CASE
                                    WHEN update_flag GLOB '[0-9]*'
                                    THEN CAST(update_flag AS INTEGER)
                                    ELSE 0
                                END DESC,
                                CASE
                                    WHEN source = 'legacy.fina_indicators' THEN 0
                                    ELSE 1
                                END DESC,
                                ingested_at DESC,
                                version_id DESC
                   ) AS visible_rank
            FROM fina_indicator_versions
            WHERE {' AND '.join(filters)}
        )
        SELECT version_id, ts_code, ann_date, end_date, update_flag,
               dt_profit_to_holder, roe, roe_dt, q_dtprofit,
               ingested_at, source, source_vintage
        FROM visible
        WHERE visible_rank = 1
        ORDER BY ts_code, end_date
    """
    with sqlite3.connect(path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def _unsupported_optional_field(exc: Exception) -> bool:
    message = str(exc).lower()
    hints = ("update_flag", "field", "column", "字段", "参数")
    return any(hint in message for hint in hints)


def _fetch_indicator(pro, *, ts_code: str, start_date: str, end_date: str):
    kwargs = {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
    try:
        return pro.fina_indicator(fields=",".join(TS_FIELDS), **kwargs)
    except Exception as exc:
        if not _unsupported_optional_field(exc):
            raise
        log.warning("%s: update_flag unavailable; retrying core fields", ts_code)
        return pro.fina_indicator(fields=",".join(TS_CORE_FIELDS), **kwargs)


def fetch_all(start_date: str = START_DATE, force: bool = False) -> None:
    if not TOKEN:
        raise RuntimeError("TUSHARE_TOKEN is not configured")
    ts.set_token(TOKEN)
    pro = ts.pro_api()

    _init_db()
    codes = _get_stock_list()
    log.info("fetching %d L/D/P securities (start_date=%s)", len(codes), start_date)

    with sqlite3.connect(DB_PATH) as conn:
        if not force:
            done = pd.read_sql_query(
                "SELECT ts_code FROM fetch_progress "
                "WHERE last_fetched > datetime('now', '-7 days')",
                conn,
            )["ts_code"].tolist()
        else:
            done = []
    done_codes = set(done)
    todo = [code for code in codes if code not in done_codes]
    log.info("actual fetch count: %d (skipped %d recent)", len(todo), len(codes) - len(todo))

    run_vintage = _utc_now()
    started = time.time()
    ok_count = fail_count = total_rows = 0
    for index, ts_code in enumerate(todo, 1):
        try:
            df = _fetch_indicator(
                pro,
                ts_code=ts_code,
                start_date=start_date,
                end_date=datetime.now().strftime("%Y%m%d"),
            )
            inserted = _upsert_rows(
                df if df is not None else pd.DataFrame(),
                source_vintage=run_vintage,
            )
            total_rows += inserted
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """
                    INSERT INTO fetch_progress(ts_code, last_fetched, n_rows)
                    VALUES (?, datetime('now'), ?)
                    ON CONFLICT(ts_code) DO UPDATE SET
                        last_fetched = excluded.last_fetched,
                        n_rows = excluded.n_rows
                    """,
                    (ts_code, inserted),
                )
            ok_count += 1
        except Exception as exc:
            fail_count += 1
            log.warning("%s: %s", ts_code, exc)

        if index % 100 == 0 or index == len(todo):
            elapsed = time.time() - started
            rate = index / elapsed if elapsed > 0 else 0
            eta = (len(todo) - index) / rate if rate > 0 else 0
            log.info(
                "[%d/%d] ok=%d fail=%d new_versions=%d rate=%.1f/s eta=%.1fmin",
                index,
                len(todo),
                ok_count,
                fail_count,
                total_rows,
                rate,
                eta / 60,
            )
        time.sleep(SLEEP)

    log.info(
        "DONE: ok=%d fail=%d new_versions=%d elapsed=%.1fmin",
        ok_count,
        fail_count,
        total_rows,
        (time.time() - started) / 60,
    )


if __name__ == "__main__":
    fetch_all(force="--force" in sys.argv)
