from pathlib import Path

from tg_bot import database


def claim(db_path: Path, update_id: int, timeout_seconds: int) -> str:
    stale_modifier = f"-{timeout_seconds} seconds"
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT status,
                   CASE
                       WHEN updated_at < datetime('now', ?) THEN 1
                       ELSE 0
                   END AS is_stale
            FROM processed_updates
            WHERE update_id = ?
            """,
            (stale_modifier, update_id),
        ).fetchone()

        if row and row["status"] == "done":
            conn.commit()
            return "done"
        if row and row["status"] == "processing" and not row["is_stale"]:
            conn.commit()
            return "processing"

        if row:
            conn.execute(
                """
                UPDATE processed_updates
                SET status = 'processing', attempts = attempts + 1,
                    last_error = '', updated_at = CURRENT_TIMESTAMP
                WHERE update_id = ?
                """,
                (update_id,),
            )
        else:
            conn.execute(
                """
                INSERT INTO processed_updates (
                    update_id, status, attempts, updated_at, processed_at
                ) VALUES (?, 'processing', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (update_id,),
            )
        conn.commit()
        return "claimed"


def recover_interrupted(db_path: Path) -> None:
    """Recover work owned by the previous single-worker service process."""
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE processed_updates
            SET status = 'failed',
                last_error = 'service restarted during update processing',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
            """
        )
        conn.execute(
            """
            UPDATE admin_deliveries
            SET status = 'unknown',
                last_error = 'service restarted during Telegram delivery',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'sending'
            """
        )
        conn.execute(
            """
            UPDATE admin_reply_deliveries
            SET status = 'unknown',
                last_error = 'service restarted during Telegram delivery',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'sending'
            """
        )
        conn.commit()


def finish(db_path: Path, update_id: int) -> None:
    database.execute(
        db_path,
        """
        UPDATE processed_updates
        SET status = 'done', last_error = '',
            updated_at = CURRENT_TIMESTAMP, processed_at = CURRENT_TIMESTAMP
        WHERE update_id = ?
        """,
        (update_id,),
    )


def fail(db_path: Path, update_id: int, error: str) -> None:
    database.execute(
        db_path,
        """
        UPDATE processed_updates
        SET status = 'failed', last_error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE update_id = ?
        """,
        (error[:1000], update_id),
    )
