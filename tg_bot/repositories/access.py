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


def is_verified(db_path: Path, chat_id: int) -> bool:
    return database.fetchone(
        db_path,
        """
        SELECT chat_id
        FROM user_verifications
        WHERE chat_id = ?
          AND verified_at IS NOT NULL
          AND expires_at > CURRENT_TIMESTAMP
        """,
        (chat_id,),
    ) is not None


def claim_verification_prompt(db_path: Path, chat_id: int) -> bool:
    with database.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO user_verifications (chat_id, last_prompted_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_prompted_at = CURRENT_TIMESTAMP
            WHERE user_verifications.last_prompted_at IS NULL
               OR user_verifications.last_prompted_at
                  < datetime('now', '-30 seconds')
            """,
            (chat_id,),
        )
        conn.commit()
        return cursor.rowcount == 1


def release_verification_prompt(db_path: Path, chat_id: int) -> None:
    database.execute(
        db_path,
        """
        UPDATE user_verifications
        SET last_prompted_at = NULL
        WHERE chat_id = ? AND verified_at IS NULL
        """,
        (chat_id,),
    )


def mark_verified(db_path: Path, chat_id: int, verify_days: int) -> None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO user_verifications (
                chat_id, verified_at, expires_at, last_prompted_at
            ) VALUES (
                ?, CURRENT_TIMESTAMP, datetime('now', ?), CURRENT_TIMESTAMP
            )
            ON CONFLICT(chat_id) DO UPDATE SET
                verified_at = CURRENT_TIMESTAMP,
                expires_at = excluded.expires_at,
                last_prompted_at = CURRENT_TIMESTAMP
            """,
            (chat_id, f"+{verify_days} days"),
        )
        conn.execute("DELETE FROM user_rate_limits WHERE chat_id = ?", (chat_id,))
        conn.commit()
