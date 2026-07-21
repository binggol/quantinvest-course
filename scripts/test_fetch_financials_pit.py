import sqlite3

import pandas as pd

from scripts import fetch_financials as financials


def _legacy_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE fina_indicators (
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


def test_versions_are_retained_and_as_of_uses_visible_revision(tmp_path, monkeypatch):
    db_path = tmp_path / "financials.db"
    monkeypatch.setattr(financials, "DB_PATH", db_path)
    financials._init_db()

    first = pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240420",
                "end_date": "20231231",
                "update_flag": "0",
                "profit_dedt": 100.0,
                "roe": 8.0,
            }
        ]
    )
    revised = first.assign(ann_date="20240506", update_flag="1", profit_dedt=125.0, roe=9.0)

    assert financials._upsert_rows(
        first,
        ingested_at="2024-04-20T10:00:00+00:00",
        source_vintage="snapshot-20240420",
    ) == 1
    assert financials._upsert_rows(
        revised,
        ingested_at="2024-05-06T10:00:00+00:00",
        source_vintage="snapshot-20240506",
    ) == 1
    # A later refetch of identical source content is not a new disclosure version.
    assert financials._upsert_rows(
        revised,
        ingested_at="2024-05-07T10:00:00+00:00",
        source_vintage="snapshot-20240507",
    ) == 0

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fina_indicator_versions").fetchone()[0] == 2
        latest = conn.execute(
            "SELECT ann_date, dt_profit_to_holder FROM fina_indicators"
        ).fetchone()
        assert latest == ("20240506", 125.0)

    before_revision = financials.query_fina_indicators_as_of(
        "2024-05-01", db_path=db_path
    )
    assert before_revision.iloc[0]["ann_date"] == "20240420"
    assert before_revision.iloc[0]["dt_profit_to_holder"] == 100.0

    after_revision = financials.query_fina_indicators_as_of(
        "20240506", db_path=db_path
    )
    assert after_revision.iloc[0]["ann_date"] == "20240506"
    assert after_revision.iloc[0]["dt_profit_to_holder"] == 125.0


def test_as_of_can_replay_local_ingestion_cutoff(tmp_path, monkeypatch):
    db_path = tmp_path / "financials.db"
    monkeypatch.setattr(financials, "DB_PATH", db_path)
    financials._init_db()
    rows = pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240420",
                "end_date": "20231231",
                "update_flag": "0",
                "profit_dedt": 100.0,
            },
            {
                "ts_code": "000002.SZ",
                "ann_date": "20240420",
                "end_date": "20231231",
                "update_flag": "0",
                "profit_dedt": 200.0,
            },
        ]
    )
    financials._upsert_rows(
        rows.iloc[[0]],
        ingested_at="2024-04-21T00:00:00+00:00",
        source_vintage="v1",
    )
    financials._upsert_rows(
        rows.iloc[[1]],
        ingested_at="2024-04-22T00:00:00+00:00",
        source_vintage="v2",
    )

    visible = financials.query_fina_indicators_as_of(
        "20240430",
        db_path=db_path,
        ingested_as_of="2024-04-21T23:59:59+00:00",
    )
    assert visible["ts_code"].tolist() == ["000001.SZ"]


def test_legacy_database_migrates_additively_and_idempotently(tmp_path, monkeypatch):
    db_path = tmp_path / "financials.db"
    with sqlite3.connect(db_path) as conn:
        _legacy_schema(conn)
        conn.execute(
            "INSERT INTO fina_indicators VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("600000.SH", "20240329", "20231231", 88.0, 6.5, 6.0, 20.0),
        )
    monkeypatch.setattr(financials, "DB_PATH", db_path)

    financials._init_db()
    financials._init_db()

    with sqlite3.connect(db_path) as conn:
        # The old row/table remains available to every existing reader.
        legacy = conn.execute(
            "SELECT ts_code, ann_date, end_date, dt_profit_to_holder FROM fina_indicators"
        ).fetchall()
        assert legacy == [("600000.SH", "20240329", "20231231", 88.0)]
        versions = conn.execute(
            "SELECT source, source_vintage FROM fina_indicator_versions"
        ).fetchall()
        assert versions == [("legacy.fina_indicators", "pre-pit-migration")]

    as_of = financials.query_fina_indicators_as_of("20240401", db_path=db_path)
    assert as_of.iloc[0]["ts_code"] == "600000.SH"
    assert as_of.iloc[0]["dt_profit_to_holder"] == 88.0


def test_native_observation_is_not_hidden_by_identical_legacy_content(tmp_path, monkeypatch):
    db_path = tmp_path / "financials.db"
    with sqlite3.connect(db_path) as conn:
        _legacy_schema(conn)
        conn.execute(
            "INSERT INTO fina_indicators VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("600000.SH", "20240329", "20231231", 88.0, 6.5, 6.0, 20.0),
        )
    monkeypatch.setattr(financials, "DB_PATH", db_path)
    financials._init_db()

    identical_native = pd.DataFrame([{
        "ts_code": "600000.SH",
        "ann_date": "20240329",
        "end_date": "20231231",
        "profit_dedt": 88.0,
        "roe": 6.5,
        "roe_dt": 6.0,
        "q_dtprofit": 20.0,
    }])
    assert financials._upsert_rows(
        identical_native,
        ingested_at="2024-03-29T10:00:00+00:00",
        source_vintage="native-backfill",
    ) == 1

    with sqlite3.connect(db_path) as conn:
        sources = conn.execute(
            "SELECT source FROM fina_indicator_versions ORDER BY source"
        ).fetchall()
    assert sources == [("legacy.fina_indicators",), ("tushare.fina_indicator",)]


def test_native_source_is_not_swallowed_by_identical_legacy_content(tmp_path, monkeypatch):
    db_path = tmp_path / "financials.db"
    with sqlite3.connect(db_path) as conn:
        _legacy_schema(conn)
        conn.execute(
            "INSERT INTO fina_indicators VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("600000.SH", "20240329", "20231231", 88.0, 6.5, 6.0, 20.0),
        )
    monkeypatch.setattr(financials, "DB_PATH", db_path)
    financials._init_db()

    same_native_content = pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "ann_date": "20240329",
                "end_date": "20231231",
                "profit_dedt": 88.0,
                "roe": 6.5,
                "roe_dt": 6.0,
                "q_dtprofit": 20.0,
            }
        ]
    )
    assert financials._upsert_rows(
        same_native_content,
        ingested_at="2024-04-01T00:00:00+00:00",
        source_vintage="tushare-snapshot-20240401",
    ) == 1
    # Refetch remains idempotent within the native source.
    assert financials._upsert_rows(
        same_native_content,
        ingested_at="2024-04-02T00:00:00+00:00",
        source_vintage="tushare-snapshot-20240402",
    ) == 0

    with sqlite3.connect(db_path) as conn:
        sources = conn.execute(
            "SELECT source FROM fina_indicator_versions ORDER BY source"
        ).fetchall()
    assert sources == [("legacy.fina_indicators",), ("tushare.fina_indicator",)]

    latest = financials.query_fina_indicators_as_of("20240401", db_path=db_path)
    assert latest.iloc[0]["source"] == "tushare.fina_indicator"


def test_stock_universe_includes_l_d_p_and_handles_missing_status(tmp_path, monkeypatch):
    meta_path = tmp_path / "stock_meta.db"
    with sqlite3.connect(meta_path) as conn:
        conn.execute("CREATE TABLE stock_meta(ts_code TEXT, list_status TEXT)")
        conn.executemany(
            "INSERT INTO stock_meta VALUES (?, ?)",
            [
                ("000001.SZ", "L"),
                ("000002.SZ", "D"),
                ("000003.SZ", "P"),
                ("000004.SZ", "X"),
            ],
        )
    monkeypatch.setattr(financials, "STOCK_META_DB", meta_path)
    assert financials._get_stock_list() == ["000001.SZ", "000002.SZ", "000003.SZ"]

    fallback_path = tmp_path / "stock_meta_without_status.db"
    with sqlite3.connect(fallback_path) as conn:
        conn.execute("CREATE TABLE stock_meta(ts_code TEXT)")
        conn.executemany(
            "INSERT INTO stock_meta VALUES (?)", [("000002.SZ",), ("000001.SZ",)]
        )
    monkeypatch.setattr(financials, "STOCK_META_DB", fallback_path)
    assert financials._get_stock_list() == ["000001.SZ", "000002.SZ"]
