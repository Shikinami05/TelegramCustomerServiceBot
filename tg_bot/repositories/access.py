import sqlite3
import time
from pathlib import Path

from tg_bot import database


def check_rate_limit(
    db_path: Path,
    chat_id: int,
    limit_count: int,
    window_seconds: int,
    cooldown_seconds: int,
    now: int | None = None,
) -> tuple[bool, bool, int]:
    current_time = int(time.time()) if now is None else now
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT window_started_at, message_count, blocked_until
            FROM user_rate_limits
            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()

        if not row:
            conn.execute(
                """
                INSERT INTO user_rate_limits (
                    chat_id, window_started_at, message_count, blocked_until
                ) VALUES (?, ?, 1, 0)
                """,
                (chat_id, current_time),
            )
            conn.commit()
            return True, False, 0

        blocked_until = int(row["blocked_until"])
        if blocked_until > current_time:
            conn.commit()
            return False, False, blocked_until - current_time

        window_started_at = int(row["window_started_at"])
        if current_time - window_started_at >= window_seconds:
            conn.execute(
                """
                UPDATE user_rate_limits
                SET window_started_at = ?, message_count = 1, blocked_until = 0
                WHERE chat_id = ?
                """,
                (current_time, chat_id),
            )
            conn.commit()
            return True, False, 0

        message_count = int(row["message_count"]) + 1
        if message_count > limit_count:
            blocked_until = current_time + cooldown_seconds
            conn.execute(
                """
                UPDATE user_rate_limits
                SET message_count = ?, blocked_until = ?, last_notified_at = ?
                WHERE chat_id = ?
                """,
                (message_count, blocked_until, current_time, chat_id),
            )
            conn.commit()
            return False, True, cooldown_seconds

        conn.execute(
            "UPDATE user_rate_limits SET message_count = ? WHERE chat_id = ?",
            (message_count, chat_id),
        )
        conn.commit()
        return True, False, 0


def is_blacklisted(db_path: Path, chat_id: int) -> bool:
    return database.fetchone(
        db_path,
        "SELECT chat_id FROM blacklists WHERE chat_id = ?",
        (chat_id,),
    ) is not None


def blacklist(
    db_path: Path,
    chat_id: int,
    admin_id: int,
    reason: str = "",
) -> None:
    database.execute(
        db_path,
        """
        INSERT INTO blacklists (chat_id, reason, created_by)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            reason = excluded.reason,
            created_by = excluded.created_by,
            created_at = CURRENT_TIMESTAMP
        """,
        (chat_id, reason, admin_id),
    )


def unblacklist(db_path: Path, chat_id: int) -> None:
    database.execute(
        db_path,
        "DELETE FROM blacklists WHERE chat_id = ?",
        (chat_id,),
    )


def list_blacklist(db_path: Path, limit: int = 20) -> list[sqlite3.Row]:
    return database.fetchall(
        db_path,
        """
        SELECT chat_id, reason, created_at
        FROM blacklists
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )

