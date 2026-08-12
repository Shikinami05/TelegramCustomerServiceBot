import sqlite3
import time
from pathlib import Path

from tg_bot import database
from tg_bot.models import TelegramSendResult
from tg_bot.repositories.messages import add_link_in_connection
from tg_bot.text import truncate_text


def claim_next_admin(db_path: Path, now: int | None = None) -> sqlite3.Row | None:
    current_time = int(time.time()) if now is None else now
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        delivery = conn.execute(
            """
            SELECT *
            FROM admin_deliveries AS delivery
            WHERE delivery.status = 'pending'
              AND delivery.next_attempt_at <= ?
              AND (
                    delivery.delivery_kind = 'notification'
                    OR EXISTS (
                        SELECT 1 FROM admin_deliveries AS notification
                        WHERE notification.update_id = delivery.update_id
                          AND notification.admin_chat_id = delivery.admin_chat_id
                          AND notification.delivery_kind = 'notification'
                          AND notification.status = 'sent'
                    )
              )
            ORDER BY delivery.update_id, delivery.admin_chat_id,
                     CASE delivery.delivery_kind
                         WHEN 'notification' THEN 0 ELSE 1
                     END,
                     delivery.id
            LIMIT 1
            """,
            (current_time,),
        ).fetchone()
        if not delivery:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE admin_deliveries
            SET status = 'sending', attempts = attempts + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
            """,
            (delivery["id"],),
        )
        claimed = conn.execute(
            "SELECT * FROM admin_deliveries WHERE id = ?",
            (delivery["id"],),
        ).fetchone()
        conn.commit()
        return claimed


def complete_admin(
    db_path: Path,
    delivery: sqlite3.Row,
    result: TelegramSendResult,
) -> None:
    if result.message_id is None:
        raise RuntimeError("Telegram delivery succeeded without a message ID")
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        add_link_in_connection(
            conn,
            int(delivery["user_chat_id"]),
            int(delivery["source_message_id"]),
            int(delivery["admin_chat_id"]),
            result.message_id,
            "user_to_admin",
            str(delivery["delivery_kind"]),
            result.message_thread_id,
        )
        updated = conn.execute(
            """
            UPDATE admin_deliveries
            SET status = 'sent', admin_message_id = ?,
                admin_message_thread_id = ?, last_error = '',
                sent_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'sending'
            """,
            (result.message_id, result.message_thread_id, delivery["id"]),
        )
        if updated.rowcount != 1:
            raise RuntimeError("administrator delivery is no longer claimed")
        conn.commit()


def mark_admin_unknown(db_path: Path, delivery_id: int, error: str) -> None:
    database.execute(
        db_path,
        """
        UPDATE admin_deliveries
        SET status = 'unknown', last_error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'sending'
        """,
        (error[:500], delivery_id),
    )


def defer_or_fail_admin(
    db_path: Path,
    delivery: sqlite3.Row,
    result: TelegramSendResult,
    max_attempts: int,
    now: int | None = None,
) -> str:
    attempts = int(delivery["attempts"])
    error = truncate_text(result.description or "Telegram delivery failed", 500)
    if result.status_code is None or result.status_code >= 500:
        status, next_attempt_at = "unknown", 0
    elif result.status_code in {400, 401, 403, 404}:
        status, next_attempt_at = "failed", 0
    elif attempts >= max_attempts:
        status, next_attempt_at = "failed", 0
    else:
        status = "pending"
        wait_seconds = result.retry_after or min(2**attempts, 60)
        current_time = int(time.time()) if now is None else now
        next_attempt_at = current_time + max(1, wait_seconds)
    database.execute(
        db_path,
        """
        UPDATE admin_deliveries
        SET status = ?, next_attempt_at = ?, last_error = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'sending'
        """,
        (status, next_attempt_at, error, delivery["id"]),
    )
    return status


def claim_unalerted_admin(
    db_path: Path,
    now: int | None = None,
) -> sqlite3.Row | None:
    current_time = int(time.time()) if now is None else now
    return database.fetchone(
        db_path,
        """
        SELECT * FROM admin_deliveries
        WHERE status IN ('failed', 'unknown') AND alerted_at IS NULL
          AND alert_next_attempt_at <= ?
        ORDER BY updated_at, id
        LIMIT 1
        """,
        (current_time,),
    )


def mark_admin_alerted(db_path: Path, delivery_id: int) -> None:
    database.execute(
        db_path,
        """
        UPDATE admin_deliveries
        SET alerted_at = CURRENT_TIMESTAMP, alert_next_attempt_at = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status IN ('failed', 'unknown')
        """,
        (delivery_id,),
    )


def defer_admin_alert(
    db_path: Path,
    delivery: sqlite3.Row,
    now: int | None = None,
) -> None:
    wait_seconds = min(30 * (2 ** int(delivery["alert_attempts"])), 300)
    current_time = int(time.time()) if now is None else now
    database.execute(
        db_path,
        """
        UPDATE admin_deliveries
        SET alert_attempts = alert_attempts + 1,
            alert_next_attempt_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status IN ('failed', 'unknown') AND alerted_at IS NULL
        """,
        (current_time + wait_seconds, delivery["id"]),
    )


def retry_admin(db_path: Path, delivery_id: int) -> str:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        delivery = conn.execute(
            "SELECT status FROM admin_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()
        if not delivery:
            conn.commit()
            return "missing"
        if delivery["status"] not in {"failed", "unknown"}:
            conn.commit()
            return str(delivery["status"])
        conn.execute(
            """
            UPDATE admin_deliveries
            SET status = 'pending', attempts = 0, next_attempt_at = 0,
                last_error = '', alerted_at = NULL, alert_attempts = 0,
                alert_next_attempt_at = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (delivery_id,),
        )
        conn.commit()
        return "pending"


def prepare_reply(
    db_path: Path,
    update_id: int | None,
    admin_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    user_chat_id: int,
    route: str,
) -> str:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT user_chat_id, route, status
            FROM admin_reply_deliveries
            WHERE admin_chat_id = ? AND admin_message_id = ?
            """,
            (admin_chat_id, admin_message_id),
        ).fetchone()
        if existing:
            if (
                int(existing["user_chat_id"]) != user_chat_id
                or str(existing["route"]) != route
            ):
                raise RuntimeError("refusing to remap an administrator reply")
            status = str(existing["status"])
            if status == "sending":
                conn.execute(
                    """
                    UPDATE admin_reply_deliveries
                    SET status = 'unknown',
                        last_error = 'previous delivery did not finish recording',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE admin_chat_id = ? AND admin_message_id = ?
                    """,
                    (admin_chat_id, admin_message_id),
                )
                status = "unknown"
            conn.commit()
            return status
        conn.execute(
            """
            INSERT INTO admin_reply_deliveries (
                admin_chat_id, admin_message_id, update_id, admin_id,
                user_chat_id, route, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                admin_chat_id,
                admin_message_id,
                update_id,
                admin_id,
                user_chat_id,
                route,
            ),
        )
        conn.commit()
        return "pending"


def mark_reply_sending(
    db_path: Path,
    admin_chat_id: int,
    admin_message_id: int,
) -> bool:
    with database.connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE admin_reply_deliveries
            SET status = 'sending', last_error = '', updated_at = CURRENT_TIMESTAMP
            WHERE admin_chat_id = ? AND admin_message_id = ? AND status = 'pending'
            """,
            (admin_chat_id, admin_message_id),
        )
        conn.commit()
        return cursor.rowcount == 1


def fail_reply(
    db_path: Path,
    admin_chat_id: int,
    admin_message_id: int,
    result: TelegramSendResult,
) -> None:
    status = (
        "unknown"
        if result.status_code is None or result.status_code >= 500
        else "failed"
    )
    database.execute(
        db_path,
        """
        UPDATE admin_reply_deliveries
        SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE admin_chat_id = ? AND admin_message_id = ? AND status = 'sending'
        """,
        (
            status,
            truncate_text(result.description or "Telegram delivery failed", 500),
            admin_chat_id,
            admin_message_id,
        ),
    )


def mark_reply_unknown(
    db_path: Path,
    admin_chat_id: int,
    admin_message_id: int,
    error: str,
) -> None:
    database.execute(
        db_path,
        """
        UPDATE admin_reply_deliveries
        SET status = 'unknown', last_error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE admin_chat_id = ? AND admin_message_id = ? AND status = 'sending'
        """,
        (truncate_text(error, 500), admin_chat_id, admin_message_id),
    )


def complete_reply(
    db_path: Path,
    admin_chat_id: int,
    admin_message_id: int,
    user_chat_id: int,
    admin_id: int,
    route: str,
    content: str,
    kind: str,
    result: TelegramSendResult,
    admin_message_thread_id: int | None = None,
) -> None:
    if result.message_id is None:
        raise RuntimeError("Telegram reply succeeded without a message ID")
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        delivery = conn.execute(
            """
            SELECT status, user_chat_id, route
            FROM admin_reply_deliveries
            WHERE admin_chat_id = ? AND admin_message_id = ?
            """,
            (admin_chat_id, admin_message_id),
        ).fetchone()
        if (
            not delivery
            or delivery["status"] != "sending"
            or int(delivery["user_chat_id"]) != user_chat_id
            or str(delivery["route"]) != route
        ):
            raise RuntimeError("administrator reply is no longer claimed")
        add_link_in_connection(
            conn,
            user_chat_id,
            result.message_id,
            admin_chat_id,
            admin_message_id,
            "admin_to_user",
            "admin_reply",
            admin_message_thread_id,
        )
        conn.execute(
            """
            INSERT INTO message_logs (
                chat_id, sender_type, sender_id, text, telegram_message_id
            ) VALUES (?, 'admin', ?, ?, ?)
            """,
            (user_chat_id, admin_id, content, admin_message_id),
        )
        conn.execute(
            """
            INSERT INTO conversations (
                chat_id, status, unread_count, last_admin_reply_at, updated_at
            ) VALUES (?, 'open', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                status = 'open', unread_count = 0,
                last_admin_reply_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP, closed_at = NULL
            """,
            (user_chat_id,),
        )
        conn.execute(
            """
            INSERT INTO admin_audit_logs (
                admin_id, action, target_chat_id, details
            ) VALUES (?, 'message_sent', ?, ?)
            """,
            (admin_id, user_chat_id, f"type={kind} route={route}"[:500]),
        )
        conn.execute(
            """
            UPDATE admin_reply_deliveries
            SET status = 'sent', user_message_id = ?, last_error = '',
                completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE admin_chat_id = ? AND admin_message_id = ?
            """,
            (result.message_id, admin_chat_id, admin_message_id),
        )
        if route == "state":
            conn.execute(
                """
                UPDATE admin_states
                SET updated_at = CURRENT_TIMESTAMP
                WHERE admin_id = ? AND target_chat_id = ?
                """,
                (admin_id, user_chat_id),
            )
        conn.commit()
