import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from tg_bot import database
from tg_bot.repositories.conversations import record_activity_in_connection


def persist_event(
    db_path: Path,
    update_id: int,
    user: dict[str, Any],
    text: str,
    chat_id: int,
    message_id: int,
    *,
    event_type: str,
    title: str,
    edited: bool,
    admin_ids: set[int],
    include_content_delivery: bool,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Persist one inbound event and all administrator deliveries atomically."""
    context = nullcontext(connection) if connection is not None else database.connect(db_path)
    with context as conn:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            """
            INSERT OR IGNORE INTO inbound_events (
                update_id, chat_id, message_id, event_type
            ) VALUES (?, ?, ?, ?)
            """,
            (update_id, chat_id, message_id, event_type),
        )
        if claimed.rowcount == 0:
            if connection is None:
                conn.commit()
            return False

        conn.execute(
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
                chat_id,
                user.get("username"),
                user.get("first_name"),
                user.get("last_name"),
                text,
            ),
        )
        sequence = record_activity_in_connection(conn, chat_id, not edited)
        conn.execute("UPDATE inbound_events SET conversation_seq=? WHERE update_id=?", (sequence, update_id))

        updated_log = None
        if edited:
            updated_log = conn.execute(
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
        if not edited or not updated_log or updated_log.rowcount == 0:
            conn.execute(
                """
                INSERT INTO message_logs (
                    chat_id, sender_type, sender_id, text, telegram_message_id
                ) VALUES (?, 'user', ?, ?, ?)
                """,
                (chat_id, chat_id, text, message_id),
            )

        for admin_id in sorted(admin_ids):
            conn.execute(
                """
                INSERT OR IGNORE INTO admin_deliveries (
                    update_id, user_chat_id, source_message_id,
                    admin_chat_id, delivery_kind, title, content_summary
                ) VALUES (?, ?, ?, ?, 'notification', ?, ?)
                """,
                (update_id, chat_id, message_id, admin_id, title, text),
            )
            if include_content_delivery:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO admin_deliveries (
                        update_id, user_chat_id, source_message_id,
                        admin_chat_id, delivery_kind, title, content_summary
                    ) VALUES (?, ?, ?, ?, 'content', ?, ?)
                    """,
                    (update_id, chat_id, message_id, admin_id, title, text),
                )
        if connection is None:
            conn.commit()
        return True
