import sqlite3
import uuid
from pathlib import Path

from tg_bot import database


def create(db_path: Path, admin_id: int, content: str) -> str:
    broadcast_id = uuid.uuid4().hex[:12]
    database.execute(
        db_path,
        """
        INSERT INTO pending_broadcasts (id, admin_id, content, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (broadcast_id, admin_id, content),
    )
    return broadcast_id


def queue(db_path: Path, broadcast_id: str, admin_id: int) -> tuple[str, int]:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        broadcast = conn.execute(
            "SELECT admin_id, status FROM pending_broadcasts WHERE id = ?",
            (broadcast_id,),
        ).fetchone()
        if not broadcast:
            conn.commit()
            return "missing", 0
        if int(broadcast["admin_id"]) != admin_id:
            conn.commit()
            return "forbidden", 0
        if broadcast["status"] != "pending":
            conn.commit()
            return str(broadcast["status"]), 0

        recipients = conn.execute(
            """
            SELECT u.chat_id
            FROM users u
            LEFT JOIN blacklists b ON b.chat_id = u.chat_id
            WHERE b.chat_id IS NULL
            ORDER BY u.updated_at DESC
            """
        ).fetchall()
        conn.executemany(
            """
            INSERT OR IGNORE INTO broadcast_recipients (
                broadcast_id, chat_id, status
            ) VALUES (?, ?, 'pending')
            """,
            [(broadcast_id, int(row["chat_id"])) for row in recipients],
        )
        total = len(recipients)
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'queued', total_count = ?, sent_count = 0,
                failed_count = 0, unknown_count = 0,
                confirmed_at = CURRENT_TIMESTAMP,
                last_error = ''
            WHERE id = ?
            """,
            (total, broadcast_id),
        )
        conn.commit()
        return "queued", total


def cancel(db_path: Path, broadcast_id: str, admin_id: int) -> str:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        broadcast = conn.execute(
            "SELECT admin_id, status FROM pending_broadcasts WHERE id = ?",
            (broadcast_id,),
        ).fetchone()
        if not broadcast:
            conn.commit()
            return "missing"
        if int(broadcast["admin_id"]) != admin_id:
            conn.commit()
            return "forbidden"
        if broadcast["status"] != "pending":
            conn.commit()
            return str(broadcast["status"])
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'canceled', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (broadcast_id,),
        )
        conn.commit()
        return "canceled"


def retry_failed(db_path: Path, broadcast_id: str) -> tuple[str, int]:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        broadcast = conn.execute(
            "SELECT status FROM pending_broadcasts WHERE id = ?",
            (broadcast_id,),
        ).fetchone()
        if not broadcast:
            conn.commit()
            return "missing", 0
        if broadcast["status"] not in {"completed", "canceled"}:
            conn.commit()
            return str(broadcast["status"]), 0
        failed = conn.execute(
            """
            SELECT COUNT(*) AS total FROM broadcast_recipients
            WHERE broadcast_id = ? AND status = 'failed'
            """,
            (broadcast_id,),
        ).fetchone()
        failed_count = int(failed["total"]) if failed else 0
        if failed_count == 0:
            conn.commit()
            return "nothing", 0
        conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = 'pending', last_error = '', updated_at = CURRENT_TIMESTAMP
            WHERE broadcast_id = ? AND status = 'failed'
            """,
            (broadcast_id,),
        )
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'queued', failed_count = 0,
                confirmed_at = CURRENT_TIMESTAMP,
                completed_at = NULL, last_error = ''
            WHERE id = ?
            """,
            (broadcast_id,),
        )
        conn.commit()
        return "queued", failed_count


def claim_next_job(db_path: Path) -> sqlite3.Row | None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            """
            SELECT id, admin_id, content FROM pending_broadcasts
            WHERE status IN ('queued', 'running')
            ORDER BY confirmed_at, created_at
            LIMIT 1
            """
        ).fetchone()
        if job:
            conn.execute(
                """
                UPDATE pending_broadcasts
                SET status = 'running',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    last_error = ''
                WHERE id = ?
                """,
                (job["id"],),
            )
        conn.commit()
        return job


def claim_next_recipient(db_path: Path, broadcast_id: str) -> int | None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        recipient = conn.execute(
            """
            SELECT chat_id FROM broadcast_recipients
            WHERE broadcast_id = ? AND status = 'pending'
            ORDER BY chat_id
            LIMIT 1
            """,
            (broadcast_id,),
        ).fetchone()
        if not recipient:
            conn.commit()
            return None
        chat_id = int(recipient["chat_id"])
        conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = 'sending', attempts = attempts + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE broadcast_id = ? AND chat_id = ?
            """,
            (broadcast_id, chat_id),
        )
        conn.commit()
        return chat_id


def finish_recipient(
    db_path: Path,
    broadcast_id: str,
    chat_id: int,
    sent: bool,
    error: str = "",
    unknown: bool = False,
) -> None:
    if sent and unknown:
        raise ValueError("a broadcast recipient cannot be sent and unknown")
    if sent:
        status, counter = "sent", "sent_count"
    elif unknown:
        status, counter = "unknown", "unknown_count"
    else:
        status, counter = "failed", "failed_count"
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE broadcast_id = ? AND chat_id = ? AND status = 'sending'
            """,
            (status, error[:500], broadcast_id, chat_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("broadcast recipient is no longer claimed")
        conn.execute(
            f"UPDATE pending_broadcasts SET {counter} = {counter} + 1 WHERE id = ?",
            (broadcast_id,),
        )
        conn.commit()


def complete(db_path: Path, broadcast_id: str) -> sqlite3.Row | None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS total FROM broadcast_recipients
            WHERE broadcast_id = ? AND status IN ('pending', 'sending')
            """,
            (broadcast_id,),
        ).fetchone()
        if remaining and int(remaining["total"]) > 0:
            conn.commit()
            return None
        counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown_count
            FROM broadcast_recipients
            WHERE broadcast_id = ?
            """,
            (broadcast_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'completed', sent_count = ?, failed_count = ?,
                unknown_count = ?, completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                int(counts["sent_count"] or 0),
                int(counts["failed_count"] or 0),
                int(counts["unknown_count"] or 0),
                broadcast_id,
            ),
        )
        result = conn.execute(
            """
            SELECT admin_id, total_count, sent_count, failed_count, unknown_count
            FROM pending_broadcasts WHERE id = ?
            """,
            (broadcast_id,),
        ).fetchone()
        conn.commit()
        return result


def recover(db_path: Path, broadcast_id: str, error: str) -> None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = 'unknown', last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE broadcast_id = ? AND status = 'sending'
            """,
            (error[:500], broadcast_id),
        )
        conn.execute(
            """
            UPDATE pending_broadcasts SET status = 'queued', last_error = ?
            WHERE id = ? AND status = 'running'
            """,
            (error[:1000], broadcast_id),
        )
        conn.commit()


def active_user_count(db_path: Path) -> int:
    row = database.fetchone(
        db_path,
        """
        SELECT COUNT(*) AS total FROM users u
        LEFT JOIN blacklists b ON b.chat_id = u.chat_id
        WHERE b.chat_id IS NULL
        """,
    )
    return int(row["total"]) if row else 0


def list_recent_jobs(
    db_path: Path,
    admin_id: int,
    limit: int = 5,
) -> list[sqlite3.Row]:
    return database.fetchall(
        db_path,
        """
        SELECT id, status, total_count, sent_count, failed_count,
               unknown_count, created_at
        FROM pending_broadcasts
        WHERE admin_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (admin_id, limit),
    )
