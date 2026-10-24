import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, NamedTuple

from tg_bot import database


QUEUE_NAMES = {"inbox", "all"}


class CleanupResult(NamedTuple):
    status: str
    archived: int = 0
    preserved: int = 0


def prepare_cleanup(db_path: Path, admin_id: int, *, now: int | None = None) -> tuple[str, int]:
    now = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(12)
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT chat_id,last_inbound_seq FROM conversations WHERE archived_at IS NULL ORDER BY chat_id LIMIT 10001"
        ).fetchall()
        if len(rows) > 10000:
            raise ValueError("會話數超出單次整理上限，請分批整理")
        conn.execute("DELETE FROM conversation_cleanups WHERE expires_at<?", (now - 86400,))
        conn.execute("UPDATE conversation_cleanups SET status='canceled' WHERE admin_id=? AND status='pending'", (admin_id,))
        conn.execute("INSERT INTO conversation_cleanups(token,admin_id,snapshot,expires_at) VALUES(?,?,?,?)",
                     (token, admin_id, json.dumps([(row["chat_id"], row["last_inbound_seq"]) for row in rows]), now + 180))
        conn.commit()
    return token, len(rows)


def finish_cleanup(db_path: Path, admin_id: int, token: str, *, cancel: bool = False,
                   now: int | None = None) -> CleanupResult:
    now = int(time.time()) if now is None else now
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        action = conn.execute("SELECT * FROM conversation_cleanups WHERE token=? AND admin_id=?", (token, admin_id)).fetchone()
        if not action:
            return CleanupResult("missing")
        if action["status"] != "pending":
            return CleanupResult("used")
        if action["expires_at"] <= now:
            return CleanupResult("expired")
        if cancel:
            conn.execute("UPDATE conversation_cleanups SET status='canceled' WHERE token=?", (token,))
            conn.commit()
            return CleanupResult("canceled")
        expected = dict(json.loads(action["snapshot"]))
        active = conn.execute("SELECT chat_id,last_inbound_seq FROM conversations WHERE archived_at IS NULL").fetchall()
        eligible = [(row["chat_id"], row["last_inbound_seq"]) for row in active
                    if expected.get(row["chat_id"]) == row["last_inbound_seq"]]
        # One transaction consumes the confirmation and preserves changed/new conversations.
        conn.executemany(
            "UPDATE conversations SET archived_at=CURRENT_TIMESTAMP,last_read_seq=last_inbound_seq,unread_count=0 "
            "WHERE chat_id=? AND last_inbound_seq=?", eligible,
        )
        conn.executemany("DELETE FROM admin_states WHERE target_chat_id=?", [(chat_id,) for chat_id, _ in eligible])
        conn.execute("UPDATE conversation_cleanups SET status='used' WHERE token=?", (token,))
        preserved = len(active) - len(eligible)
        conn.execute("INSERT INTO admin_audit_logs(admin_id,action,details) VALUES(?,'conversation_archive_all',?)",
                     (admin_id, f"archived={len(eligible)},preserved={preserved}"))
        conn.commit()
        return CleanupResult("archived", len(eligible), preserved)


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
    with database.connect(db_path) as conn:
        record_activity_in_connection(conn, chat_id, increment_unread)
        conn.commit()


def record_activity_in_connection(conn: sqlite3.Connection, chat_id: int, increment_unread: bool = True) -> int:
    conn.execute(
        """
        INSERT INTO conversations (
            chat_id, status, unread_count, last_inbound_seq, last_user_message_at, updated_at
        )
        VALUES (?, 'open', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            status = 'open',
            unread_count = CASE
                WHEN ? THEN conversations.unread_count + 1
                ELSE MAX(conversations.unread_count, 1)
            END,
            last_inbound_seq = conversations.last_inbound_seq + CASE
                WHEN ? OR conversations.unread_count = 0 THEN 1 ELSE 0 END,
            last_user_message_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP,
            archived_at = NULL,
            closed_at = NULL
        """,
        (chat_id, int(increment_unread), int(increment_unread)),
    )
    return int(conn.execute("SELECT last_inbound_seq FROM conversations WHERE chat_id=?", (chat_id,)).fetchone()[0])


def mark_read_in_connection(conn: sqlite3.Connection, chat_id: int, through_seq: int) -> None:
    conn.execute(
        "UPDATE conversations SET last_read_seq=MAX(last_read_seq, MIN(last_inbound_seq, ?)), "
        "unread_count=MAX(0, last_inbound_seq-MAX(last_read_seq, MIN(last_inbound_seq, ?))) "
        "WHERE chat_id=?", (through_seq, through_seq, chat_id),
    )


def mark_read(db_path: Path, chat_id: int, through_seq: int) -> None:
    with database.connect(db_path) as conn:
        mark_read_in_connection(conn, chat_id, through_seq)
        conn.commit()


def mark_all_read(db_path: Path) -> int:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE conversations SET last_read_seq=last_inbound_seq, unread_count=0 "
            "WHERE unread_count>0"
        )
        conn.commit()
        return cursor.rowcount


def archive(db_path: Path, chat_id: int, through_seq: int) -> str:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_inbound_seq,archived_at FROM conversations WHERE chat_id=?", (chat_id,),
        ).fetchone()
        if not row:
            return "missing"
        if row["archived_at"] is not None:
            return "archived"
        if int(row["last_inbound_seq"]) != through_seq:
            return "changed"
        mark_read_in_connection(conn, chat_id, through_seq)
        conn.execute("UPDATE conversations SET archived_at=CURRENT_TIMESTAMP WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM admin_states WHERE target_chat_id=?", (chat_id,))
        conn.commit()
        return "archived"


def get(db_path: Path, chat_id: int) -> sqlite3.Row | None:
    return database.fetchone(
        db_path,
        """
        SELECT chat_id, owner_admin_id, status, unread_count, last_inbound_seq, last_read_seq, archived_at,
               last_user_message_at, last_admin_reply_at,
               updated_at, closed_at
        FROM conversations
        WHERE chat_id = ?
        """,
        (chat_id,),
    )


def queue_filter(
    queue_name: str,
) -> tuple[str, str, tuple[Any, ...]]:
    params: tuple[Any, ...] = ()
    if queue_name == "inbox":
        where_clause = "c.archived_at IS NULL AND c.unread_count > 0 AND NOT EXISTS (SELECT 1 FROM blacklists b WHERE b.chat_id=c.chat_id)"
        order_clause = "c.last_user_message_at DESC, c.chat_id DESC"
    elif queue_name == "all":
        where_clause = "c.archived_at IS NULL"
        order_clause = "c.updated_at DESC, c.chat_id DESC"
    else:
        raise ValueError(f"unsupported queue: {queue_name}")
    return where_clause, order_clause, params


def count_queue(db_path: Path, queue_name: str) -> int:
    where_clause, _, params = queue_filter(queue_name)
    row = database.fetchone(
        db_path,
        f"SELECT COUNT(*) AS total FROM conversations c WHERE {where_clause}",
        params,
    )
    return int(row["total"]) if row else 0


def list_queue(
    db_path: Path,
    queue_name: str,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    where_clause, order_clause, params = queue_filter(
        queue_name,
    )
    return database.fetchall(
        db_path,
        f"""
        SELECT c.chat_id, c.owner_admin_id, c.status, c.unread_count, c.last_inbound_seq,
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


def queue_counts(db_path: Path) -> dict[str, int]:
    row = database.fetchone(
        db_path,
        """
        SELECT
            SUM(CASE WHEN unread_count > 0 AND NOT EXISTS (
                SELECT 1 FROM blacklists b WHERE b.chat_id=c.chat_id)
                THEN 1 ELSE 0 END) AS inbox,
            SUM(CASE WHEN NOT EXISTS (
                SELECT 1 FROM blacklists b WHERE b.chat_id=c.chat_id)
                THEN unread_count ELSE 0 END) AS unread_messages,
            COUNT(*) AS "all"
        FROM conversations c
        WHERE c.archived_at IS NULL
        """,
    )
    return {
        queue_name: int(row[queue_name] or 0) if row else 0
        for queue_name in (*QUEUE_NAMES, "unread_messages")
    }
