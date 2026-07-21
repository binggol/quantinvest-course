from __future__ import annotations

import sqlite3
from pathlib import Path

import app as app_module


def _client():
    app_module.app.config.update(TESTING=True, CSRF_TESTING=False)
    return app_module.app.test_client()


def test_pattern_rejects_invalid_parameters_before_opening_metadata(monkeypatch):
    client = _client()
    opened = False

    def fail_if_opened(_path):
        nonlocal opened
        opened = True
        raise AssertionError("metadata must not be opened for invalid input")

    monkeypatch.setattr(app_module, "_open_sqlite_readonly", fail_if_opened)
    for url in (
        "/api/pattern?tf=monthly",
        "/api/pattern?cup_min=bad",
        "/api/pattern?cup_min=80&cup_max=20",
        "/api/pattern?cup_depth_max=nan",
        "/api/pattern?limit=-1",
    ):
        response = client.get(url)
        assert response.status_code == 400
        assert response.is_json
        assert response.get_json()["hits"] == []
    assert opened is False


def test_pattern_returns_json_503_for_missing_or_invalid_metadata(tmp_path, monkeypatch):
    client = _client()
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(app_module, "STOCK_META_DB", str(missing))
    response = client.get("/api/pattern")
    assert response.status_code == 503
    assert response.is_json
    assert response.get_json()["hits"] == []
    assert not missing.exists()

    invalid = tmp_path / "invalid.db"
    sqlite3.connect(invalid).close()
    monkeypatch.setattr(app_module, "STOCK_META_DB", str(invalid))
    response = client.get("/api/pattern")
    assert response.status_code == 503
    assert response.is_json
    assert response.get_json()["error"] == "股票元数据不可用"


def test_pattern_reads_valid_metadata_without_a_server_error(tmp_path, monkeypatch):
    db = tmp_path / "stock_meta.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE stock_meta ("
            "code TEXT, ts_code TEXT, name TEXT, industry TEXT, "
            "list_date TEXT, list_status TEXT)"
        )
        conn.execute(
            "INSERT INTO stock_meta VALUES (?, ?, ?, ?, ?, ?)",
            ("sh600000", "600000.SH", "浦发银行", "银行", "1999-11-10", "L"),
        )

    monkeypatch.setattr(app_module, "STOCK_META_DB", str(db))
    monkeypatch.setattr(app_module, "_weekly_ohlc", lambda _code: None)
    response = _client().get("/api/pattern")
    assert response.status_code == 200
    assert response.get_json()["scanned"] == 0


def test_local_defaults_stay_inside_the_project_workspace():
    if app_module._in_app_container:
        return
    expected = Path(app_module.APP_ROOT) / "data"
    assert Path(app_module.STOCK_META_DB).parent == expected
    assert Path(app_module.FINANCIALS_DB).parent == expected
    assert app_module.PREDICT_JSON.parent == expected
