from pathlib import Path
from typing import Any

from tg_bot import database


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
) -> bool:
    """Persist one inbound event and all administrator deliveries atomically."""
    with database.connect(db_path) as conn:
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
        conn.execute(
            """
            INSERT INTO conversations (
                chat_id, status, unread_count, last_user_message_at, updated_at
            ) VALUES (?, 'open', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                status = 'open',
                unread_count = CASE
                    WHEN ? THEN conversations.unread_count + 1
                    ELSE MAX(conversations.unread_count, 1)
                END,
                last_user_message_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP,
                closed_at = NULL
            """,
            (chat_id, 0 if edited else 1),
        )

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
        conn.commit()
        return True
