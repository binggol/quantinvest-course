from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def restore_membership_test_environment():
    keys = ("SECRET_KEY", "QI_AUTH_ENABLED", "QI_MEMBERS_DB")
    original = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def restore_membership_test_state():
    """Keep membership-specific environment changes inside each test."""
    env_names = ("SECRET_KEY", "QI_AUTH_ENABLED", "QI_MEMBERS_DB")
    old_env = {name: os.environ.get(name) for name in env_names}
    app_module = sys.modules.get("app")
    old_members_db = getattr(app_module, "MEMBERS_DB", None) if app_module else None
    try:
        yield
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if app_module is not None and old_members_db is not None:
            app_module.MEMBERS_DB = old_members_db


def load_app(tmp_dir: str):
    os.environ["SECRET_KEY"] = "test-secret-key"
    os.environ["QI_AUTH_ENABLED"] = "1"
    os.environ["QI_MEMBERS_DB"] = str(Path(tmp_dir) / "members.db")
    import app as app_module

    app_module.app.config["TESTING"] = True
    app_module.MEMBERS_DB = Path(os.environ["QI_MEMBERS_DB"])
    return app_module


def test_anonymous_users_are_redirected_to_login_but_health_is_public():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        old_qlib_path = app_module.QLIB_DATA_PATH
        old_stock_db = app_module.STOCK_META_DB
        app_module.QLIB_DATA_PATH = Path(td) / "empty-qlib"
        app_module.STOCK_META_DB = Path(td) / "missing-stock-meta.db"
        client = app_module.app.test_client()

        try:
            response = client.get("/")
            health = client.get("/api/health")
        finally:
            app_module.QLIB_DATA_PATH = old_qlib_path
            app_module.STOCK_META_DB = old_stock_db

        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        assert health.status_code == 503
        assert health.headers.get("Location") is None
        assert health.is_json
        payload = health.get_json()
        assert payload["ok"] is False
        assert set(payload) == {
            "ok", "calendar_days", "qlib_features", "benchmark_close", "benchmark_code", "qlib",
            "stock_metadata", "auth_ready", "daily_update", "weekly_financials", "time",
        }
        assert payload["daily_update"]["state"] in {"unknown", "failed", "succeeded", "running", "current"}
        response_text = health.get_data(as_text=True)
        assert str(Path(td)) not in response_text
        assert str(ROOT) not in response_text


def test_health_fails_when_authentication_secret_is_not_ready(tmp_path, monkeypatch):
    qlib = tmp_path / "qlib"
    calendar = qlib / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("2026-07-13\n", encoding="utf-8")
    benchmark = qlib / "features" / "sh000300" / "close.day.bin"
    benchmark.parent.mkdir(parents=True)
    np.asarray([0, 1.0], dtype="<f4").tofile(benchmark)
    stock_db = tmp_path / "stock_meta.db"
    stock_db.write_bytes(b"ready")

    import app as app_module

    monkeypatch.setattr(app_module, "QLIB_DATA_PATH", qlib)
    monkeypatch.setattr(app_module, "STOCK_META_DB", str(stock_db))
    monkeypatch.setattr(app_module, "_secret_is_strong", False)
    monkeypatch.setenv("QI_AUTH_ENABLED", "1")
    response = app_module.app.test_client().get("/api/health")
    assert response.status_code == 503
    assert response.get_json()["auth_ready"] is False

    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    response = app_module.app.test_client().get("/api/health")
    assert response.status_code == 200
    assert response.get_json()["auth_ready"] is True


def test_active_member_can_login_and_access_pages():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        store.upsert_member(
            email="member@example.com",
            password="member-pass",
            role="member",
            status="active",
            membership_until=date.today() + timedelta(days=3),
        )
        client = app_module.app.test_client()

        login = client.post(
            "/login",
            data={"email": "member@example.com", "password": "member-pass"},
            follow_redirects=False,
        )
        page = client.get("/")

        assert login.status_code == 302
        assert page.status_code == 200


def test_expired_member_logs_in_but_is_sent_to_expired_page():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        store.upsert_member(
            email="expired@example.com",
            password="member-pass",
            role="member",
            status="active",
            membership_until=date.today() - timedelta(days=1),
        )
        client = app_module.app.test_client()

        login = client.post(
            "/login",
            data={"email": "expired@example.com", "password": "member-pass"},
            follow_redirects=False,
        )
        page = client.get("/")
        expired = client.get("/membership-expired")

        assert login.status_code == 302
        assert "/membership-expired" in login.headers["Location"]
        assert page.status_code == 302
        assert "/membership-expired" in page.headers["Location"]
        assert expired.status_code == 200
        assert "会员已到期" in expired.get_data(as_text=True)


def test_revoked_expired_member_session_cannot_use_public_expiry_page():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        member = store.upsert_member(
            email="revoked-expired@example.com",
            password="member-pass",
            role="member",
            status="active",
            membership_until=date.today() - timedelta(days=1),
        )
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["member_id"] = member["id"]
            sess["member_email"] = member["email"]
            sess["session_version"] = member["session_version"]

        assert client.get("/membership-expired").status_code == 200
        store.bump_session_version(member["email"])
        revoked = client.get("/membership-expired")

        assert revoked.status_code == 302
        assert "/login" in revoked.headers["Location"]
        with client.session_transaction() as sess:
            assert "member_id" not in sess


def test_admin_can_create_and_extend_member():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        store.upsert_member(
            email="admin@example.com",
            password="admin-pass",
            role="admin",
            status="active",
            membership_until=None,
        )
        client = app_module.app.test_client()
        client.post("/login", data={"email": "admin@example.com", "password": "admin-pass"})

        admin_page = client.get("/admin/members")
        create = client.post(
            "/admin/members",
            data={
                "action": "save",
                "email": "new@example.com",
                "password": "new-pass",
                "role": "member",
                "status": "active",
                "plan": "basic",
                "membership_until": date.today().isoformat(),
            },
        )
        extend = client.post(
            "/admin/members",
            data={"action": "extend", "email": "new@example.com", "days": "30"},
        )
        change_plan = client.post(
            "/admin/members",
            data={"action": "plan", "email": "new@example.com", "plan": "data_pro"},
        )
        member = store.get_member_by_email("new@example.com")

        assert admin_page.status_code == 200
        assert "会员管理" in admin_page.get_data(as_text=True)
        assert create.status_code == 302
        assert extend.status_code == 302
        assert change_plan.status_code == 302
        assert member is not None
        assert member["plan"] == "data_pro"
        assert member["membership_until"] == (date.today() + timedelta(days=30)).isoformat()


def test_admin_member_actions_are_audited_without_password_material():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        admin = store.upsert_member(
            email="admin@example.com",
            password="admin-pass",
            role="admin",
            status="active",
            membership_until=None,
        )
        client = app_module.app.test_client()
        client.post("/login", data={"email": "admin@example.com", "password": "admin-pass"})

        created = client.post(
            "/admin/members",
            data={
                "action": "save",
                "email": "AUDITED@Example.COM",
                "password": "member-secret-that-must-not-be-audited",
                "role": "member",
                "status": "active",
                "plan": "data_pro",
                "membership_until": date.today().isoformat(),
            },
        )
        member = store.get_member_by_email("audited@example.com")
        assert created.status_code == 302
        assert member is not None

        old_version = member["session_version"]
        revoked = client.post(
            "/admin/members",
            data={"action": "revoke_sessions", "email": "audited@example.com"},
        )
        updated = store.get_member_by_email("audited@example.com")
        assert revoked.status_code == 302
        assert updated is not None
        assert updated["session_version"] == old_version + 1

        audits = store.list_audit()
        assert [item["action"] for item in audits[:2]] == ["revoke_sessions", "save"]
        save_audit = audits[1]
        assert save_audit["actor_member_id"] == admin["id"]
        assert save_audit["target_email"] == "audited@example.com"
        assert save_audit["details"]["before"] is None
        assert save_audit["details"]["after"]["plan"] == "data_pro"
        assert save_audit["details"]["password_changed"] is True
        assert "member-secret-that-must-not-be-audited" not in str(save_audit)
        assert "password_hash" not in str(save_audit)
        assert audits[0]["details"]["before"]["session_version"] == old_version
        assert audits[0]["details"]["after"]["session_version"] == old_version + 1


def test_invalid_admin_actions_are_failed_audits_and_do_not_mutate_members():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        store.upsert_member(
            email="admin@example.com",
            password="admin-pass",
            role="admin",
            status="active",
            membership_until=None,
        )
        client = app_module.app.test_client()
        client.post("/login", data={"email": "admin@example.com", "password": "admin-pass"})

        missing = client.post(
            "/admin/members",
            data={"action": "extend", "email": "missing@example.com", "days": "30"},
        )
        unknown = client.post(
            "/admin/members",
            data={
                "action": "unexpected",
                "email": "must-not-exist@example.com",
                "password": "member-pass",
                "role": "member",
                "status": "active",
                "plan": "basic",
            },
        )

        assert missing.status_code == 200
        assert unknown.status_code == 200
        assert store.get_member_by_email("must-not-exist@example.com") is None
        audits = store.list_audit()
        assert [item["action"] for item in audits[:2]] == ["unexpected_failed", "extend_failed"]
        assert audits[0]["details"]["error"] == "unsupported member action"
        assert audits[1]["details"]["error"] == "member not found"


def test_operator_cannot_manage_members_or_create_admin_audit_records():
    with tempfile.TemporaryDirectory() as td:
        app_module = load_app(td)
        store = app_module._membership_store()
        store.upsert_member(
            email="operator@example.com",
            password="operator-pass",
            role="operator",
            status="active",
            membership_until=None,
        )
        client = app_module.app.test_client()
        client.post("/login", data={"email": "operator@example.com", "password": "operator-pass"})

        assert client.get("/admin/members").status_code == 403
        denied = client.post(
            "/admin/members",
            data={
                "action": "save",
                "email": "forbidden@example.com",
                "password": "member-pass",
                "role": "member",
                "status": "active",
                "plan": "basic",
            },
        )
        assert denied.status_code == 403
        assert store.get_member_by_email("forbidden@example.com") is None
        assert store.list_audit() == []


if __name__ == "__main__":
    test_anonymous_users_are_redirected_to_login_but_health_is_public()
    test_active_member_can_login_and_access_pages()
    test_expired_member_logs_in_but_is_sent_to_expired_page()
    test_revoked_expired_member_session_cannot_use_public_expiry_page()
    test_admin_can_create_and_extend_member()
    test_admin_member_actions_are_audited_without_password_material()
    test_invalid_admin_actions_are_failed_audits_and_do_not_mutate_members()
    test_operator_cannot_manage_members_or_create_admin_audit_records()
    print("ok")
