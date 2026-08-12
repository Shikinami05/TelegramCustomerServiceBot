import sqlite3
from pathlib import Path
from typing import Any

from tg_bot import database


def upsert(db_path: Path, user: dict[str, Any], text: str) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO users (
            chat_id, username, first_name, last_name, last_message, updated_at
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            last_message = excluded.last_message,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            user["id"],
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
            text,
        ),
    )


def get(db_path: Path, chat_id: int) -> sqlite3.Row | None:
    return database.fetchone(
        db_path,
        "SELECT * FROM users WHERE chat_id = ?",
        (chat_id,),
    )


def count(db_path: Path) -> int:
    row = database.fetchone(db_path, "SELECT COUNT(*) AS total FROM users")
    return int(row["total"]) if row else 0


def list_recent(
    db_path: Path,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    return database.fetchall(
        db_path,
        """
        SELECT u.chat_id, u.username, u.first_name, u.last_name, u.last_message,
               u.updated_at, b.chat_id AS blocked,
               COALESCE(c.unread_count, 0) AS unread_count,
               c.status AS conversation_status
        FROM users u
        LEFT JOIN blacklists b ON b.chat_id = u.chat_id
        LEFT JOIN conversations c ON c.chat_id = u.chat_id
        ORDER BY u.updated_at DESC, u.chat_id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )


def add_message_log(
    db_path: Path,
    chat_id: int,
    sender_type: str,
    sender_id: int,
    text: str,
    telegram_message_id: int | None = None,
) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO message_logs (
            chat_id, sender_type, sender_id, text, telegram_message_id
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, sender_type, sender_id, text, telegram_message_id),
    )


def update_user_message_log(
    db_path: Path,
    chat_id: int,
    message_id: int,
    text: str,
) -> bool:
    with database.connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE message_logs
            SET text = ?, edited_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM message_logs
                WHERE chat_id = ? AND sender_type = 'user'
                  AND telegram_message_id = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (text, chat_id, message_id),
        )
        conn.commit()
        return cursor.rowcount == 1


def get_history(
    db_path: Path,
    chat_id: int,
    limit: int,
    exclude_message_id: int | None = None,
) -> list[sqlite3.Row]:
    if exclude_message_id is None:
        rows = database.fetchall(
            db_path,
            """
            SELECT sender_type, text, edited_at, created_at
            FROM message_logs
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
    else:
        rows = database.fetchall(
            db_path,
            """
            SELECT sender_type, text, edited_at, created_at
            FROM message_logs
            WHERE chat_id = ?
              AND (telegram_message_id IS NULL OR telegram_message_id != ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, exclude_message_id, limit),
        )
    return rows[::-1]


def add_admin_audit(
    db_path: Path,
    admin_id: int,
    action: str,
    target_chat_id: int | None = None,
    details: str = "",
) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO admin_audit_logs (
            admin_id, action, target_chat_id, details
        ) VALUES (?, ?, ?, ?)
        """,
        (admin_id, action[:80], target_chat_id, details[:500]),
    )


def list_admin_audits(db_path: Path, limit: int = 20) -> list[sqlite3.Row]:
    return database.fetchall(
        db_path,
        """
        SELECT admin_id, action, target_chat_id, details, created_at
        FROM admin_audit_logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
