from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash


ROLE_MEMBER = "member"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
VALID_ROLES = frozenset({ROLE_MEMBER, ROLE_OPERATOR, ROLE_ADMIN})

PLAN_BASIC = "basic"
PLAN_DATA_PRO = "data_pro"
PLAN_ENTERPRISE = "enterprise"
VALID_PLANS = frozenset({PLAN_BASIC, PLAN_DATA_PRO, PLAN_ENTERPRISE})

FEATURE_MARKET_DATA = "market_data"
FEATURE_ADVANCED_DATA = "advanced_data"
FEATURE_DATA_EXPORT = "data_export"
FEATURE_API_ACCESS = "api_access"
FEATURE_MEMBER_WORKSPACE = "member_workspace"
FEATURE_INTERNAL_OPERATIONS = "internal_operations"
FEATURE_MANAGE_MEMBERS = "manage_members"

MEMBER_FEATURES = frozenset(
    {
        FEATURE_MARKET_DATA,
        FEATURE_ADVANCED_DATA,
        FEATURE_DATA_EXPORT,
        FEATURE_MEMBER_WORKSPACE,
    }
)

# The feature catalogue is the single source of truth used by route guards and UI navigation.
PLAN_FEATURES: dict[str, frozenset[str]] = {
    PLAN_BASIC: frozenset({FEATURE_MARKET_DATA, FEATURE_MEMBER_WORKSPACE}),
    PLAN_DATA_PRO: MEMBER_FEATURES,
    PLAN_ENTERPRISE: MEMBER_FEATURES | frozenset({FEATURE_API_ACCESS}),
}

ROLE_FEATURES: dict[str, frozenset[str]] = {
    ROLE_MEMBER: frozenset(),
    ROLE_OPERATOR: MEMBER_FEATURES | frozenset({FEATURE_API_ACCESS, FEATURE_INTERNAL_OPERATIONS}),
    ROLE_ADMIN: MEMBER_FEATURES
    | frozenset({FEATURE_API_ACCESS, FEATURE_INTERNAL_OPERATIONS, FEATURE_MANAGE_MEMBERS}),
}


def _utcnow_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _has_active_membership(member: dict[str, Any] | None, today: date | None = None) -> bool:
    if not member or member.get("status") != "active":
        return False
    if member.get("role") in {ROLE_OPERATOR, ROLE_ADMIN}:
        return True
    until = member.get("membership_until")
    if not until:
        return False
    try:
        until_date = date.fromisoformat(str(until)[:10])
    except ValueError:
        return False
    return until_date >= (today or date.today())


def has_feature(
    member: dict[str, Any] | None,
    feature: str,
    today: date | None = None,
) -> bool:
    """Return whether an active account may use a centrally defined feature."""
    if not _has_active_membership(member, today=today):
        return False
    feature_name = str(feature or "").strip()
    role = str(member.get("role") or ROLE_MEMBER)
    if feature_name in ROLE_FEATURES.get(role, frozenset()):
        return True
    plan = str(member.get("plan") or PLAN_BASIC)
    return feature_name in PLAN_FEATURES.get(plan, frozenset())


class MembershipStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'active',
                    membership_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login TEXT,
                    plan TEXT NOT NULL DEFAULT 'basic',
                    session_version INTEGER NOT NULL DEFAULT 1,
                    terms_version TEXT,
                    terms_accepted_at TEXT
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(members)").fetchall()}
            if "plan" not in columns:
                conn.execute("ALTER TABLE members ADD COLUMN plan TEXT NOT NULL DEFAULT 'basic'")
            if "session_version" not in columns:
                conn.execute("ALTER TABLE members ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1")
            if "terms_version" not in columns:
                conn.execute("ALTER TABLE members ADD COLUMN terms_version TEXT")
            if "terms_accepted_at" not in columns:
                conn.execute("ALTER TABLE members ADD COLUMN terms_accepted_at TEXT")
            conn.execute(
                "UPDATE members SET plan = ? WHERE plan IS NULL OR TRIM(plan) = ''",
                (PLAN_BASIC,),
            )
            conn.execute(
                "UPDATE members SET session_version = 1 WHERE session_version IS NULL OR session_version < 1"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_members_email ON members(email)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS membership_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_member_id INTEGER,
                    target_email TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    remote_addr TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_membership_audit_created_at
                ON membership_audit(created_at DESC)
                """
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def normalize_email(email: str) -> str:
        return (email or "").strip().lower()

    @staticmethod
    def _date_text(value: date | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        return text or None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _protect_last_active_admin(
        conn: sqlite3.Connection,
        email: str,
        *,
        next_role: str,
        next_status: str,
    ) -> None:
        current = conn.execute(
            "SELECT id, role, status FROM members WHERE email = ?",
            (email,),
        ).fetchone()
        if (
            current is None
            or current["role"] != ROLE_ADMIN
            or current["status"] != "active"
            or (next_role == ROLE_ADMIN and next_status == "active")
        ):
            return
        other_admins = conn.execute(
            """
            SELECT COUNT(*)
            FROM members
            WHERE role = ? AND status = 'active' AND id <> ?
            """,
            (ROLE_ADMIN, current["id"]),
        ).fetchone()[0]
        if int(other_admins or 0) < 1:
            raise ValueError("cannot disable or demote the last active admin")

    def get_member_by_email(self, email: str) -> dict[str, Any] | None:
        self.init_db()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE email = ?",
                (self.normalize_email(email),),
            ).fetchone()
        return self._row_to_dict(row)

    def get_member_by_id(self, member_id: int | str | None) -> dict[str, Any] | None:
        if not member_id:
            return None
        self.init_db()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        return self._row_to_dict(row)

    def list_members(self) -> list[dict[str, Any]]:
        self.init_db()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, email, role, status, membership_until, created_at, updated_at,
                       last_login, plan, session_version, terms_version, terms_accepted_at
                FROM members
                ORDER BY role = 'admin' DESC, status = 'active' DESC, membership_until DESC, email
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_member(
        self,
        *,
        email: str,
        password: str | None = None,
        role: str = "member",
        status: str = "active",
        membership_until: date | str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        self.init_db()
        norm_email = self.normalize_email(email)
        if not norm_email or len(norm_email) > 254 or "@" not in norm_email:
            raise ValueError("valid email is required")
        if password is not None and not 8 <= len(str(password)) <= 256:
            raise ValueError("password must be 8 to 256 characters")
        if role not in VALID_ROLES:
            raise ValueError("role must be member, operator, or admin")
        if status not in {"active", "disabled"}:
            raise ValueError("status must be active or disabled")
        if plan is not None and plan not in VALID_PLANS:
            raise ValueError("plan must be basic, data_pro, or enterprise")

        now = _utcnow_text()
        until_text = self._date_text(membership_until)
        existing = self.get_member_by_email(norm_email)
        password_hash = generate_password_hash(password) if password else None
        effective_plan = plan or (str(existing.get("plan")) if existing else PLAN_BASIC)

        with self._connect() as conn:
            if existing:
                conn.execute("BEGIN IMMEDIATE")
                self._protect_last_active_admin(
                    conn,
                    norm_email,
                    next_role=role,
                    next_status=status,
                )
                security_changed = bool(password_hash) or any(
                    (
                        existing.get("role") != role,
                        existing.get("status") != status,
                        existing.get("plan") != effective_plan,
                    )
                )
                next_session_version = int(existing.get("session_version") or 1) + int(security_changed)
                if password_hash:
                    conn.execute(
                        """
                        UPDATE members
                        SET password_hash = ?, role = ?, status = ?, membership_until = ?, plan = ?,
                            session_version = ?, updated_at = ?
                        WHERE email = ?
                        """,
                        (
                            password_hash,
                            role,
                            status,
                            until_text,
                            effective_plan,
                            next_session_version,
                            now,
                            norm_email,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE members
                        SET role = ?, status = ?, membership_until = ?, plan = ?,
                            session_version = ?, updated_at = ?
                        WHERE email = ?
                        """,
                        (
                            role,
                            status,
                            until_text,
                            effective_plan,
                            next_session_version,
                            now,
                            norm_email,
                        ),
                    )
            else:
                if not password:
                    raise ValueError("password is required for new members")
                conn.execute(
                    """
                    INSERT INTO members (
                        email, password_hash, role, status, membership_until, plan,
                        session_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (norm_email, password_hash, role, status, until_text, effective_plan, now, now),
                )

        member = self.get_member_by_email(norm_email)
        assert member is not None
        return member

    def verify_login(self, email: str, password: str) -> dict[str, Any] | None:
        member = self.get_member_by_email(email)
        if not member or member.get("status") != "active":
            return None
        if not check_password_hash(member["password_hash"], password or ""):
            return None
        now = _utcnow_text()
        with self._connect() as conn:
            conn.execute("UPDATE members SET last_login = ?, updated_at = ? WHERE id = ?", (now, now, member["id"]))
        member["last_login"] = now
        return member

    def has_active_membership(self, member: dict[str, Any] | None, today: date | None = None) -> bool:
        return _has_active_membership(member, today=today)

    def has_feature(
        self,
        member: dict[str, Any] | None,
        feature: str,
        today: date | None = None,
    ) -> bool:
        return has_feature(member, feature, today=today)

    def set_status(self, email: str, status: str) -> dict[str, Any] | None:
        if status not in {"active", "disabled"}:
            raise ValueError("status must be active or disabled")
        self.init_db()
        norm_email = self.normalize_email(email)
        now = _utcnow_text()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT role FROM members WHERE email = ?",
                (norm_email,),
            ).fetchone()
            self._protect_last_active_admin(
                conn,
                norm_email,
                next_role=str(current["role"]) if current is not None else ROLE_MEMBER,
                next_status=status,
            )
            conn.execute(
                """
                UPDATE members
                SET status = ?,
                    session_version = session_version + CASE WHEN status <> ? THEN 1 ELSE 0 END,
                    updated_at = ?
                WHERE email = ?
                """,
                (status, status, now, norm_email),
            )
        return self.get_member_by_email(norm_email)

    def set_plan(self, email: str, plan: str) -> dict[str, Any] | None:
        if plan not in VALID_PLANS:
            raise ValueError("plan must be basic, data_pro, or enterprise")
        self.init_db()
        norm_email = self.normalize_email(email)
        now = _utcnow_text()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE members
                SET plan = ?,
                    session_version = session_version + CASE WHEN plan <> ? THEN 1 ELSE 0 END,
                    updated_at = ?
                WHERE email = ?
                """,
                (plan, plan, now, norm_email),
            )
        return self.get_member_by_email(norm_email)

    def bump_session_version(self, email: str) -> dict[str, Any] | None:
        """Revoke all existing sessions for an account."""
        self.init_db()
        norm_email = self.normalize_email(email)
        now = _utcnow_text()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE members
                SET session_version = session_version + 1, updated_at = ?
                WHERE email = ?
                """,
                (now, norm_email),
            )
        return self.get_member_by_email(norm_email)

    def is_session_valid(self, member_id: int | str | None, session_version: int | str | None) -> bool:
        if not member_id or session_version is None:
            return False
        try:
            expected_version = int(session_version)
        except (TypeError, ValueError):
            return False
        member = self.get_member_by_id(member_id)
        if not member or member.get("status") != "active":
            return False
        return int(member.get("session_version") or 0) == expected_version

    def accept_terms(self, member_id: int | str | None, version: str) -> dict[str, Any] | None:
        if not member_id:
            return None
        terms_version = str(version or "").strip()
        if not terms_version:
            raise ValueError("terms version is required")
        self.init_db()
        now = _utcnow_text()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE members
                SET terms_version = ?, terms_accepted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (terms_version, now, now, member_id),
            )
        return self.get_member_by_id(member_id)

    def record_audit(
        self,
        *,
        actor_member_id: int | None,
        target_email: str,
        action: str,
        details: dict[str, Any] | None = None,
        remote_addr: str | None = None,
    ) -> None:
        self.init_db()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO membership_audit (
                    actor_member_id, target_email, action, details, remote_addr, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_member_id,
                    self.normalize_email(target_email),
                    str(action or "unknown")[:80],
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    str(remote_addr or "")[:120],
                    _utcnow_text(),
                ),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            safe_limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            raise ValueError("audit limit must be an integer") from None
        self.init_db()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, actor_member_id, target_email, action, details, remote_addr, created_at
                FROM membership_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                record["details"] = json.loads(record.get("details") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                record["details"] = {}
            records.append(record)
        return records

    def extend_membership(self, email: str, days: int, today: date | None = None) -> dict[str, Any] | None:
        if days <= 0:
            raise ValueError("days must be positive")
        member = self.get_member_by_email(email)
        if not member:
            return None
        base_today = today or date.today()
        current_text = member.get("membership_until")
        try:
            current_date = date.fromisoformat(str(current_text)[:10]) if current_text else base_today
        except ValueError:
            current_date = base_today
        new_until = max(current_date, base_today) + timedelta(days=days)
        return self.upsert_member(
            email=member["email"],
            role=member["role"],
            status=member["status"],
            membership_until=new_until,
            plan=member.get("plan") or PLAN_BASIC,
        )

    def ensure_admin(self, email: str | None, password: str | None) -> dict[str, Any] | None:
        norm_email = self.normalize_email(email or "")
        if not norm_email:
            return None
        existing = self.get_member_by_email(norm_email)
        if existing:
            if existing.get("role") != ROLE_ADMIN:
                return self.upsert_member(
                    email=norm_email,
                    role=ROLE_ADMIN,
                    status=str(existing.get("status") or "disabled"),
                    membership_until=existing.get("membership_until"),
                    plan=str(existing.get("plan") or PLAN_BASIC),
                )
            return existing
        if not password:
            return None
        return self.upsert_member(
            email=norm_email,
            password=password,
            role=ROLE_ADMIN,
            status="active",
            membership_until=None,
            plan=PLAN_ENTERPRISE,
        )
