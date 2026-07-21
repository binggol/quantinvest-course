from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class MemberDataStore:
    """Transaction-safe JSON documents scoped to a single member."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS member_documents (
                    member_id INTEGER NOT NULL,
                    namespace TEXT NOT NULL,
                    item_key TEXT NOT NULL DEFAULT 'default',
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (member_id, namespace, item_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_member_documents_namespace "
                "ON member_documents(member_id, namespace)"
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _member_id(member_id: int | str) -> int:
        try:
            value = int(member_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("valid member_id is required") from exc
        if value < 0:
            raise ValueError("valid member_id is required")
        return value

    @staticmethod
    def _document_name(namespace: str, item_key: str) -> tuple[str, str]:
        ns = str(namespace or "").strip()
        key = str(item_key or "default").strip()
        if not ns or len(ns) > 80 or not key or len(key) > 160:
            raise ValueError("invalid document name")
        return ns, key

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(
        self,
        member_id: int | str,
        namespace: str,
        item_key: str = "default",
        default: Any = None,
    ) -> Any:
        self.init_db()
        mid = self._member_id(member_id)
        ns, key = self._document_name(namespace, item_key)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM member_documents "
                "WHERE member_id = ? AND namespace = ? AND item_key = ?",
                (mid, ns, key),
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return default

    def put(
        self,
        member_id: int | str,
        namespace: str,
        value: Any,
        item_key: str = "default",
    ) -> Any:
        return self.put_many(member_id, {(namespace, item_key): value})[(namespace, item_key)]

    def put_many(
        self,
        member_id: int | str,
        documents: dict[tuple[str, str], Any],
    ) -> dict[tuple[str, str], Any]:
        self.init_db()
        mid = self._member_id(member_id)
        normalized = {
            self._document_name(namespace, item_key): value
            for (namespace, item_key), value in documents.items()
        }
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for (namespace, item_key), value in normalized.items():
                payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """
                    INSERT INTO member_documents (
                        member_id, namespace, item_key, payload, version, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(member_id, namespace, item_key) DO UPDATE SET
                        payload = excluded.payload,
                        version = member_documents.version + 1,
                        updated_at = excluded.updated_at
                    """,
                    (mid, namespace, item_key, payload, now),
                )
        return normalized

    def update(
        self,
        member_id: int | str,
        namespace: str,
        updater: Callable[[Any], Any],
        *,
        item_key: str = "default",
        default: Any = None,
    ) -> Any:
        self.init_db()
        mid = self._member_id(member_id)
        ns, key = self._document_name(namespace, item_key)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM member_documents "
                "WHERE member_id = ? AND namespace = ? AND item_key = ?",
                (mid, ns, key),
            ).fetchone()
            current = default
            if row:
                try:
                    current = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    current = default
            updated = updater(current)
            payload = json.dumps(updated, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO member_documents (
                    member_id, namespace, item_key, payload, version, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(member_id, namespace, item_key) DO UPDATE SET
                    payload = excluded.payload,
                    version = member_documents.version + 1,
                    updated_at = excluded.updated_at
                """,
                (mid, ns, key, payload, self._now()),
            )
        return updated

    def delete(self, member_id: int | str, namespace: str, item_key: str = "default") -> bool:
        self.init_db()
        mid = self._member_id(member_id)
        ns, key = self._document_name(namespace, item_key)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM member_documents "
                "WHERE member_id = ? AND namespace = ? AND item_key = ?",
                (mid, ns, key),
            )
        return cur.rowcount > 0
