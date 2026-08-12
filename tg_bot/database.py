import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def enforce_private_mode(path: Path, mode: int) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(mode)


def secure_database_files(db_path: Path) -> None:
    for path in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
    ):
        enforce_private_mode(path, 0o600)


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def initialize(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                last_message TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_states (
                admin_id INTEGER PRIMARY KEY,
                target_chat_id INTEGER,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,
                sender_id INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                telegram_message_id INTEGER,
                edited_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_chat_id INTEGER NOT NULL,
                user_message_id INTEGER NOT NULL,
                admin_chat_id INTEGER NOT NULL,
                admin_message_id INTEGER NOT NULL,
                admin_message_thread_id INTEGER,
                direction TEXT NOT NULL,
                link_kind TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(admin_chat_id, admin_message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklists (
                chat_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT '',
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id INTEGER PRIMARY KEY,
                owner_admin_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                unread_count INTEGER NOT NULL DEFAULT 0,
                last_user_message_at DATETIME,
                last_admin_reply_at DATETIME,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_chat_id INTEGER,
                details TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_broadcasts (
                id TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                total_count INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                unknown_count INTEGER NOT NULL DEFAULT 0,
                confirmed_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                broadcast_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (broadcast_id, chat_id),
                FOREIGN KEY (broadcast_id) REFERENCES pending_broadcasts(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'done',
                attempts INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_events (
                update_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                admin_chat_id INTEGER NOT NULL,
                delivery_kind TEXT NOT NULL,
                title TEXT NOT NULL,
                content_summary TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                admin_message_id INTEGER,
                admin_message_thread_id INTEGER,
                last_error TEXT NOT NULL DEFAULT '',
                alerted_at DATETIME,
                alert_attempts INTEGER NOT NULL DEFAULT 0,
                alert_next_attempt_at INTEGER NOT NULL DEFAULT 0,
                sent_at DATETIME,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(update_id, admin_chat_id, delivery_kind),
                FOREIGN KEY (update_id) REFERENCES inbound_events(update_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_reply_deliveries (
                admin_chat_id INTEGER NOT NULL,
                admin_message_id INTEGER NOT NULL,
                update_id INTEGER,
                admin_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                route TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                user_message_id INTEGER,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (admin_chat_id, admin_message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_rate_limits (
                chat_id INTEGER PRIMARY KEY,
                window_started_at INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                blocked_until INTEGER NOT NULL DEFAULT 0,
                last_notified_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_verifications (
                chat_id INTEGER PRIMARY KEY,
                verified_at DATETIME,
                expires_at DATETIME,
                last_prompted_at DATETIME
            )
            """
        )

        ensure_column(conn, "message_logs", "telegram_message_id", "INTEGER")
        ensure_column(conn, "message_logs", "edited_at", "DATETIME")
        ensure_column(
            conn,
            "conversations",
            "unread_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(conn, "conversations", "last_user_message_at", "DATETIME")
        ensure_column(conn, "conversations", "last_admin_reply_at", "DATETIME")
        ensure_column(
            conn,
            "pending_broadcasts",
            "status",
            "TEXT NOT NULL DEFAULT 'pending'",
        )
        ensure_column(
            conn,
            "pending_broadcasts",
            "total_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            conn,
            "pending_broadcasts",
            "sent_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            conn,
            "pending_broadcasts",
            "failed_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            conn,
            "pending_broadcasts",
            "unknown_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(conn, "pending_broadcasts", "confirmed_at", "DATETIME")
        ensure_column(conn, "pending_broadcasts", "started_at", "DATETIME")
        ensure_column(conn, "pending_broadcasts", "completed_at", "DATETIME")
        ensure_column(
            conn,
            "pending_broadcasts",
            "last_error",
            "TEXT NOT NULL DEFAULT ''",
        )
        ensure_column(
            conn,
            "processed_updates",
            "status",
            "TEXT NOT NULL DEFAULT 'done'",
        )
        ensure_column(
            conn,
            "processed_updates",
            "attempts",
            "INTEGER NOT NULL DEFAULT 1",
        )
        ensure_column(
            conn,
            "processed_updates",
            "last_error",
            "TEXT NOT NULL DEFAULT ''",
        )
        ensure_column(conn, "processed_updates", "updated_at", "DATETIME")
        ensure_column(
            conn,
            "admin_deliveries",
            "alert_attempts",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            conn,
            "admin_deliveries",
            "alert_next_attempt_at",
            "INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute(
            "UPDATE processed_updates SET updated_at = processed_at "
            "WHERE updated_at IS NULL"
        )
        conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = 'unknown',
                last_error = 'service restarted during Telegram delivery',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'sending'
            """
        )
        conn.execute(
            "UPDATE pending_broadcasts SET status = 'queued' "
            "WHERE status = 'running'"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_logs_chat_id_id "
            "ON message_logs(chat_id, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_links_user_message "
            "ON message_links(user_chat_id, user_message_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status "
            "ON broadcast_recipients(broadcast_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_broadcasts_status "
            "ON pending_broadcasts(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_inbox "
            "ON conversations(status, unread_count, last_user_message_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created "
            "ON admin_audit_logs(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_verifications_expires "
            "ON user_verifications(expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_deliveries_pending "
            "ON admin_deliveries(status, next_attempt_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_deliveries_source "
            "ON admin_deliveries(user_chat_id, source_message_id, admin_chat_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_deliveries_alerts "
            "ON admin_deliveries(status, alert_next_attempt_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_reply_deliveries_status "
            "ON admin_reply_deliveries(status, updated_at)"
        )
        conn.commit()
    secure_database_files(db_path)


def execute(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    with connect(db_path) as conn:
        conn.execute(sql, params)
        conn.commit()


def fetchone(
    db_path: Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def fetchall(
    db_path: Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()
