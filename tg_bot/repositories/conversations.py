import sqlite3
from pathlib import Path
from typing import Any

from tg_bot import database
from tg_bot.models import ConversationClaimResult


QUEUE_NAMES = {"inbox", "pending", "closed"}


def set_admin_state(db_path: Path, admin_id: int, target_chat_id: int) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO admin_states (admin_id, target_chat_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(admin_id) DO UPDATE SET
            target_chat_id = excluded.target_chat_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (admin_id, target_chat_id),
    )


def get_admin_state(
    db_path: Path,
    admin_id: int,
    ttl_seconds: int,
) -> int | None:
    with database.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT target_chat_id,
                   CASE
                       WHEN updated_at < datetime('now', ?) THEN 1
                       ELSE 0
                   END AS expired
            FROM admin_states
            WHERE admin_id = ?
            """,
            (f"-{ttl_seconds} seconds", admin_id),
        ).fetchone()
        if not row:
            return None
        if row["expired"]:
            conn.execute("DELETE FROM admin_states WHERE admin_id = ?", (admin_id,))
            conn.commit()
            return None
        return int(row["target_chat_id"])


def clear_admin_state(
    db_path: Path,
    admin_id: int,
    ttl_seconds: int,
    expected_target_chat_id: int | None = None,
) -> int | None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT target_chat_id,
                   CASE
                       WHEN updated_at < datetime('now', ?) THEN 1
                       ELSE 0
                   END AS expired
            FROM admin_states
            WHERE admin_id = ?
            """,
            (f"-{ttl_seconds} seconds", admin_id),
        ).fetchone()
        if not row:
            conn.commit()
            return None

        target_chat_id = int(row["target_chat_id"])
        if row["expired"]:
            conn.execute("DELETE FROM admin_states WHERE admin_id = ?", (admin_id,))
            conn.commit()
            return None
        if (
            expected_target_chat_id is not None
            and target_chat_id != expected_target_chat_id
        ):
            conn.commit()
            return None

        conn.execute("DELETE FROM admin_states WHERE admin_id = ?", (admin_id,))
        conn.commit()
        return target_chat_id


def clear_admin_states_for_target(db_path: Path, chat_id: int) -> None:
    database.execute(
        db_path,
        "DELETE FROM admin_states WHERE target_chat_id = ?",
        (chat_id,),
    )


def record_user_activity(
    db_path: Path,
    chat_id: int,
    increment_unread: bool = True,
) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO conversations (
            chat_id, status, unread_count, last_user_message_at, updated_at
        )
        VALUES (?, 'open', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
        (chat_id, 1 if increment_unread else 0),
    )


def mark_replied(db_path: Path, chat_id: int) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO conversations (
            chat_id, status, unread_count, last_admin_reply_at, updated_at
        )
        VALUES (?, 'open', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            status = 'open', unread_count = 0,
            last_admin_reply_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP, closed_at = NULL
        """,
        (chat_id,),
    )


def reopen(db_path: Path, chat_id: int) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO conversations (chat_id, status, unread_count, updated_at)
        VALUES (?, 'open', 0, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            status = 'open', updated_at = CURRENT_TIMESTAMP, closed_at = NULL
        """,
        (chat_id,),
    )


def get(db_path: Path, chat_id: int) -> sqlite3.Row | None:
    return database.fetchone(
        db_path,
        """
        SELECT chat_id, owner_admin_id, status, unread_count,
               last_user_message_at, last_admin_reply_at,
               updated_at, closed_at
        FROM conversations
        WHERE chat_id = ?
        """,
        (chat_id,),
    )


def get_owner(db_path: Path, chat_id: int) -> int | None:
    row = get(db_path, chat_id)
    if not row or row["status"] != "open" or row["owner_admin_id"] is None:
        return None
    return int(row["owner_admin_id"])


def claim(
    db_path: Path,
    chat_id: int,
    admin_id: int,
    *,
    force: bool = False,
    reopen_closed: bool = False,
    activate_reply: bool = False,
) -> ConversationClaimResult:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conversation = conn.execute(
            "SELECT owner_admin_id, status FROM conversations WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        current_owner = (
            int(conversation["owner_admin_id"])
            if conversation and conversation["owner_admin_id"] is not None
            else None
        )
        if conversation and conversation["status"] == "closed" and not reopen_closed:
            conn.commit()
            return ConversationClaimResult("closed", current_owner)
        if current_owner not in {None, admin_id} and not force:
            conn.commit()
            return ConversationClaimResult("conflict", current_owner)

        conn.execute(
            "DELETE FROM admin_states WHERE target_chat_id = ? AND admin_id != ?",
            (chat_id, admin_id),
        )
        conn.execute(
            """
            INSERT INTO conversations (chat_id, owner_admin_id, status, updated_at)
            VALUES (?, ?, 'open', CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                owner_admin_id = excluded.owner_admin_id,
                status = 'open', updated_at = CURRENT_TIMESTAMP, closed_at = NULL
            """,
            (chat_id, admin_id),
        )
        if activate_reply:
            conn.execute(
                """
                INSERT INTO admin_states (admin_id, target_chat_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(admin_id) DO UPDATE SET
                    target_chat_id = excluded.target_chat_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (admin_id, chat_id),
            )
        conn.commit()
        return ConversationClaimResult("acquired", admin_id)


def close(db_path: Path, chat_id: int) -> None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO conversations (
                chat_id, owner_admin_id, status, updated_at, closed_at
            ) VALUES (?, NULL, 'closed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                owner_admin_id = NULL, status = 'closed', unread_count = 0,
                updated_at = CURRENT_TIMESTAMP, closed_at = CURRENT_TIMESTAMP
            """,
            (chat_id,),
        )
        conn.execute("DELETE FROM admin_states WHERE target_chat_id = ?", (chat_id,))
        conn.commit()


def queue_filter(
    queue_name: str,
    pending_minutes: int,
) -> tuple[str, str, tuple[Any, ...]]:
    params: tuple[Any, ...] = ()
    if queue_name == "inbox":
        where_clause = "c.status = 'open' AND c.unread_count > 0"
        order_clause = "c.last_user_message_at DESC, c.chat_id DESC"
    elif queue_name == "pending":
        where_clause = (
            "c.status = 'open' AND c.unread_count > 0 "
            "AND c.last_user_message_at <= datetime('now', ?)"
        )
        order_clause = "c.last_user_message_at ASC, c.chat_id ASC"
        params = (f"-{pending_minutes} minutes",)
    elif queue_name == "closed":
        where_clause = "c.status = 'closed'"
        order_clause = "c.closed_at DESC, c.chat_id DESC"
    else:
        raise ValueError(f"unsupported queue: {queue_name}")
    return where_clause, order_clause, params


def count_queue(db_path: Path, queue_name: str, pending_minutes: int) -> int:
    where_clause, _, params = queue_filter(queue_name, pending_minutes)
    row = database.fetchone(
        db_path,
        f"SELECT COUNT(*) AS total FROM conversations c WHERE {where_clause}",
        params,
    )
    return int(row["total"]) if row else 0


def list_queue(
    db_path: Path,
    queue_name: str,
    pending_minutes: int,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    where_clause, order_clause, params = queue_filter(
        queue_name,
        pending_minutes,
    )
    return database.fetchall(
        db_path,
        f"""
        SELECT c.chat_id, c.owner_admin_id, c.status, c.unread_count,
               c.last_user_message_at, c.last_admin_reply_at,
               c.updated_at, c.closed_at,
               u.username, u.first_name, u.last_name, u.last_message
        FROM conversations c
        LEFT JOIN users u ON u.chat_id = c.chat_id
        WHERE {where_clause}
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )


def queue_counts(db_path: Path, pending_minutes: int) -> dict[str, int]:
    row = database.fetchone(
        db_path,
        """
        SELECT
            SUM(CASE WHEN status = 'open' AND unread_count > 0
                THEN 1 ELSE 0 END) AS inbox,
            SUM(CASE WHEN status = 'open' AND unread_count > 0
                 AND last_user_message_at <= datetime('now', ?)
                THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
        FROM conversations
        """,
        (f"-{pending_minutes} minutes",),
    )
    return {
        queue_name: int(row[queue_name] or 0) if row else 0
        for queue_name in QUEUE_NAMES
    }
