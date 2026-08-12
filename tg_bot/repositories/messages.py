import sqlite3
from pathlib import Path

from tg_bot import database


VALID_DIRECTIONS = {"user_to_admin", "admin_to_user"}
VALID_LINK_KINDS = {"notification", "content", "admin_reply"}


def add_link(
    db_path: Path,
    user_chat_id: int,
    user_message_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    direction: str,
    link_kind: str,
    admin_message_thread_id: int | None = None,
) -> None:
    with database.connect(db_path) as conn:
        add_link_in_connection(
            conn,
            user_chat_id,
            user_message_id,
            admin_chat_id,
            admin_message_id,
            direction,
            link_kind,
            admin_message_thread_id,
        )
        conn.commit()


def add_link_in_connection(
    conn: sqlite3.Connection,
    user_chat_id: int,
    user_message_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    direction: str,
    link_kind: str,
    admin_message_thread_id: int | None = None,
) -> None:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"unsupported message link direction: {direction}")
    if link_kind not in VALID_LINK_KINDS:
        raise ValueError(f"unsupported message link kind: {link_kind}")

    values = (
        user_chat_id,
        user_message_id,
        admin_chat_id,
        admin_message_id,
        admin_message_thread_id,
        direction,
        link_kind,
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO message_links (
            user_chat_id, user_message_id,
            admin_chat_id, admin_message_id,
            admin_message_thread_id, direction, link_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    if cursor.rowcount != 0:
        return

    existing = conn.execute(
        """
        SELECT user_chat_id, user_message_id,
               admin_message_thread_id, direction, link_kind
        FROM message_links
        WHERE admin_chat_id = ? AND admin_message_id = ?
        """,
        (admin_chat_id, admin_message_id),
    ).fetchone()
    expected = (
        user_chat_id,
        user_message_id,
        admin_message_thread_id,
        direction,
        link_kind,
    )
    actual = tuple(existing) if existing else None
    if actual != expected:
        raise RuntimeError("refusing to remap an existing administrator message")


def find_link(
    db_path: Path,
    admin_chat_id: int,
    admin_message_id: int,
) -> sqlite3.Row | None:
    return database.fetchone(
        db_path,
        """
        SELECT user_chat_id, user_message_id,
               admin_chat_id, admin_message_id,
               admin_message_thread_id, direction, link_kind, created_at
        FROM message_links
        WHERE admin_chat_id = ? AND admin_message_id = ?
        """,
        (admin_chat_id, admin_message_id),
    )
