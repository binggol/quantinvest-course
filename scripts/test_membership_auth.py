from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

from membership_auth import (
    FEATURE_ADVANCED_DATA,
    FEATURE_DATA_EXPORT,
    FEATURE_INTERNAL_OPERATIONS,
    FEATURE_MANAGE_MEMBERS,
    FEATURE_MARKET_DATA,
    FEATURE_MEMBER_WORKSPACE,
    PLAN_BASIC,
    PLAN_DATA_PRO,
    PLAN_ENTERPRISE,
    MembershipStore,
    has_feature,
)


def test_member_login_and_expiry_rules():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        tomorrow = date.today() + timedelta(days=1)

        store.upsert_member(
            email="USER@Example.COM",
            password="secret123",
            role="member",
            status="active",
            membership_until=tomorrow,
        )

        member = store.verify_login("user@example.com", "secret123")
        assert member is not None
        assert member["email"] == "user@example.com"
        assert member["plan"] == PLAN_BASIC
        assert member["session_version"] == 1
        assert store.has_active_membership(member, today=date.today()) is True
        assert store.verify_login("user@example.com", "wrong") is None

        store.set_status("user@example.com", "disabled")
        disabled = store.get_member_by_email("user@example.com")
        assert disabled is not None
        assert store.has_active_membership(disabled, today=date.today()) is False


def test_expired_member_is_not_active_but_admin_is_active():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        yesterday = date.today() - timedelta(days=1)

        store.upsert_member(
            email="expired@example.com",
            password="secret123",
            role="member",
            status="active",
            membership_until=yesterday,
        )
        store.upsert_member(
            email="admin@example.com",
            password="admin-secret",
            role="admin",
            status="active",
            membership_until=None,
        )

        expired = store.get_member_by_email("expired@example.com")
        admin = store.get_member_by_email("admin@example.com")

        assert expired is not None
        assert admin is not None
        assert store.has_active_membership(expired, today=date.today()) is False
        assert store.has_active_membership(admin, today=date.today()) is True


def test_extend_member_sets_later_expiry():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        today = date.today()

        store.upsert_member(
            email="member@example.com",
            password="secret123",
            role="member",
            status="active",
            membership_until=today,
            plan=PLAN_DATA_PRO,
        )

        updated = store.extend_membership("member@example.com", 30, today=today)

        assert updated is not None
        assert updated["membership_until"] == (today + timedelta(days=30)).isoformat()
        assert updated["plan"] == PLAN_DATA_PRO


def test_legacy_database_is_migrated_without_losing_login_data():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "members.db"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'active',
                    membership_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO members (
                    email, password_hash, role, status, membership_until, created_at, updated_at
                ) VALUES (?, ?, 'member', 'active', ?, ?, ?)
                """,
                (
                    "legacy@example.com",
                    generate_password_hash("legacy-secret"),
                    date.today().isoformat(),
                    "2025-01-01T00:00:00",
                    "2025-01-01T00:00:00",
                ),
            )
            conn.commit()

        store = MembershipStore(db_path)
        store.init_db()
        store.init_db()

        legacy = store.verify_login("legacy@example.com", "legacy-secret")
        assert legacy is not None
        assert legacy["plan"] == PLAN_BASIC
        assert legacy["session_version"] == 1
        assert legacy["terms_version"] is None
        assert legacy["terms_accepted_at"] is None
        with closing(sqlite3.connect(db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(members)")}
        assert {"plan", "session_version", "terms_version", "terms_accepted_at"}.issubset(columns)


def test_plan_and_role_feature_matrix():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        today = date.today()
        basic = store.upsert_member(
            email="basic@example.com",
            password="secret123",
            membership_until=today,
        )

        assert has_feature(basic, FEATURE_MARKET_DATA, today=today) is True
        assert store.has_feature(basic, FEATURE_MEMBER_WORKSPACE, today=today) is True
        assert store.has_feature(basic, FEATURE_ADVANCED_DATA, today=today) is False
        assert store.has_feature(basic, FEATURE_DATA_EXPORT, today=today) is False
        assert store.has_feature(basic, FEATURE_INTERNAL_OPERATIONS, today=today) is False

        data_pro = store.set_plan("basic@example.com", PLAN_DATA_PRO)
        assert data_pro is not None
        assert store.has_feature(data_pro, FEATURE_ADVANCED_DATA, today=today) is True
        assert store.has_feature(data_pro, FEATURE_DATA_EXPORT, today=today) is True

        operator = store.upsert_member(
            email="operator@example.com",
            password="operator-secret",
            role="operator",
            membership_until=None,
        )
        assert store.has_active_membership(operator, today=today) is True
        assert store.has_feature(operator, FEATURE_INTERNAL_OPERATIONS, today=today) is True
        assert store.has_feature(operator, FEATURE_ADVANCED_DATA, today=today) is True
        assert store.has_feature(operator, FEATURE_MANAGE_MEMBERS, today=today) is False

        admin = store.upsert_member(
            email="admin@example.com",
            password="admin-secret",
            role="admin",
            membership_until=None,
        )
        assert store.has_feature(admin, FEATURE_INTERNAL_OPERATIONS, today=today) is True
        assert store.has_feature(admin, FEATURE_MANAGE_MEMBERS, today=today) is True

        expired = store.upsert_member(
            email="expired-feature@example.com",
            password="secret123",
            membership_until=today - timedelta(days=1),
            plan=PLAN_ENTERPRISE,
        )
        assert store.has_feature(expired, FEATURE_MARKET_DATA, today=today) is False


def test_plan_validation_and_session_revocation():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        member = store.upsert_member(
            email="session@example.com",
            password="secret123",
            membership_until=date.today(),
        )
        version = member["session_version"]
        assert store.is_session_valid(member["id"], version) is True

        bumped = store.bump_session_version(member["email"])
        assert bumped is not None
        assert bumped["session_version"] == version + 1
        assert store.is_session_valid(member["id"], version) is False
        assert store.is_session_valid(member["id"], bumped["session_version"]) is True

        changed_plan = store.set_plan(member["email"], PLAN_DATA_PRO)
        assert changed_plan is not None
        assert changed_plan["session_version"] == bumped["session_version"] + 1
        unchanged_plan = store.set_plan(member["email"], PLAN_DATA_PRO)
        assert unchanged_plan is not None
        assert unchanged_plan["session_version"] == changed_plan["session_version"]

        disabled = store.set_status(member["email"], "disabled")
        assert disabled is not None
        assert disabled["session_version"] == unchanged_plan["session_version"] + 1
        assert store.is_session_valid(member["id"], disabled["session_version"]) is False

        try:
            store.set_plan(member["email"], "unknown")
        except ValueError as exc:
            assert "plan must be" in str(exc)
        else:
            raise AssertionError("unknown plan should be rejected")


def test_accept_terms_records_version_without_revoking_session():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        member = store.upsert_member(
            email="terms@example.com",
            password="secret123",
            membership_until=date.today(),
        )

        accepted = store.accept_terms(member["id"], "terms-2026-07")
        assert accepted is not None
        assert accepted["terms_version"] == "terms-2026-07"
        assert accepted["terms_accepted_at"]
        assert accepted["session_version"] == member["session_version"]
        assert store.accept_terms(999999, "terms-2026-07") is None

        try:
            store.accept_terms(member["id"], "  ")
        except ValueError as exc:
            assert "terms version is required" in str(exc)
        else:
            raise AssertionError("blank terms version should be rejected")


def test_membership_audit_records_actor_target_and_structured_details():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        actor = store.upsert_member(
            email="admin@example.com",
            password="admin-secret",
            role="admin",
            membership_until=None,
        )
        store.record_audit(
            actor_member_id=actor["id"],
            target_email="  USER@Example.COM ",
            action="save",
            details={"after": {"plan": PLAN_DATA_PRO}, "password_changed": True},
            remote_addr="127.0.0.1",
        )
        store.record_audit(
            actor_member_id=actor["id"],
            target_email="second@example.com",
            action="status",
            details={"status": "disabled"},
        )

        records = store.list_audit(limit=1)
        assert len(records) == 1
        assert records[0]["action"] == "status"
        all_records = store.list_audit()
        saved = all_records[1]
        assert saved["actor_member_id"] == actor["id"]
        assert saved["target_email"] == "user@example.com"
        assert saved["details"]["after"]["plan"] == PLAN_DATA_PRO
        assert saved["details"]["password_changed"] is True
        assert saved["remote_addr"] == "127.0.0.1"
        assert saved["created_at"]

        try:
            store.list_audit(limit="not-a-number")
        except ValueError as exc:
            assert "audit limit must be an integer" in str(exc)
        else:
            raise AssertionError("invalid audit limit should be rejected")


def test_member_listing_does_not_return_password_hashes():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        store.upsert_member(
            email="listed@example.com",
            password="listed-secret",
            membership_until=date.today(),
        )

        members = store.list_members()

        assert len(members) == 1
        assert "password_hash" not in members[0]


def test_last_active_admin_cannot_be_disabled_or_demoted():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        admin = store.upsert_member(
            email="only-admin@example.com",
            password="admin-secret",
            role="admin",
            membership_until=None,
        )

        for action in (
            lambda: store.set_status(admin["email"], "disabled"),
            lambda: store.upsert_member(
                email=admin["email"],
                role="member",
                status="active",
                membership_until=date.today(),
            ),
        ):
            try:
                action()
            except ValueError as exc:
                assert "last active admin" in str(exc)
            else:
                raise AssertionError("the last active admin was removed")

        unchanged = store.get_member_by_email(admin["email"])
        assert unchanged is not None
        assert unchanged["role"] == "admin"
        assert unchanged["status"] == "active"

        store.upsert_member(
            email="second-admin@example.com",
            password="second-secret",
            role="admin",
            membership_until=None,
        )
        demoted = store.upsert_member(
            email=admin["email"],
            role="member",
            status="active",
            membership_until=date.today(),
        )
        assert demoted["role"] == "member"


def test_ensure_admin_does_not_reactivate_or_reset_existing_account():
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "members.db")
        disabled_member = store.upsert_member(
            email="bootstrap@example.com",
            password="original-secret",
            role="member",
            status="disabled",
            membership_until=None,
        )

        ensured = store.ensure_admin("bootstrap@example.com", "replacement-secret")
        assert ensured is not None
        assert ensured["role"] == "admin"
        assert ensured["status"] == "disabled"
        ensured_again = store.ensure_admin("bootstrap@example.com", "replacement-secret")
        assert ensured_again is not None
        assert ensured_again["session_version"] == ensured["session_version"]

        store.set_status("bootstrap@example.com", "active")
        assert store.verify_login("bootstrap@example.com", "original-secret") is not None
        assert store.verify_login("bootstrap@example.com", "replacement-secret") is None

        created = store.ensure_admin("new-admin@example.com", "new-admin-secret")
        assert created is not None
        assert created["role"] == "admin"
        assert created["status"] == "active"
        assert created["plan"] == PLAN_ENTERPRISE
        assert disabled_member["session_version"] < ensured["session_version"]


if __name__ == "__main__":
    test_member_login_and_expiry_rules()
    test_expired_member_is_not_active_but_admin_is_active()
    test_extend_member_sets_later_expiry()
    test_legacy_database_is_migrated_without_losing_login_data()
    test_plan_and_role_feature_matrix()
    test_plan_validation_and_session_revocation()
    test_accept_terms_records_version_without_revoking_session()
    test_membership_audit_records_actor_target_and_structured_details()
    test_member_listing_does_not_return_password_hashes()
    test_last_active_admin_cannot_be_disabled_or_demoted()
    test_ensure_admin_does_not_reactivate_or_reset_existing_account()
    print("ok")
