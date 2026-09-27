import json
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from tg_bot import database
from tg_bot.repositories import inbound


def enqueue(db_path: Path, update_id: int, chat_id: int, message_id: int,
            snapshot: dict[str, Any], summary: str, edited: bool) -> str:
    version_at = int(snapshot.get("edit_date") or snapshot.get("date") or 0)
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute(
            "SELECT update_id, version_at FROM moderation_jobs WHERE chat_id=? AND message_id=? "
            "ORDER BY version_at DESC, update_id DESC LIMIT 1", (chat_id, message_id),
        ).fetchone()
        if latest and (latest["version_at"], latest["update_id"]) >= (version_at, update_id):
            return "duplicate"
        count = conn.execute("SELECT COUNT(*) FROM moderation_jobs WHERE status IN ('queued','running','held')").fetchone()[0]
        if count >= 2000:
            return "full"
        conn.execute(
            "UPDATE moderation_jobs SET status='superseded', updated_at=CURRENT_TIMESTAMP "
            "WHERE chat_id=? AND message_id=? AND status IN ('queued','running','held')",
            (chat_id, message_id),
        )
        conn.execute(
            "UPDATE admin_deliveries SET status='canceled', updated_at=CURRENT_TIMESTAMP "
            "WHERE user_chat_id=? AND source_message_id=? AND status IN ('pending','failed')",
            (chat_id, message_id),
        )
        conn.execute(
            "INSERT INTO moderation_jobs(update_id,chat_id,message_id,snapshot,summary,edited,version_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (update_id, chat_id, message_id, json.dumps(snapshot, ensure_ascii=False), summary, int(edited), version_at),
        )
        conn.commit()
        return "queued"


def recover(db_path: Path) -> None:
    database.execute(db_path, "UPDATE moderation_jobs SET status='held', verdict='error', reason='服务重启，等待人工确认' "
                     "WHERE status='running'")


def claim_next(db_path: Path) -> sqlite3.Row | None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM moderation_jobs WHERE status='queued' ORDER BY update_id LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE moderation_jobs SET status='running', updated_at=CURRENT_TIMESTAMP WHERE update_id=?",
                         (row["update_id"],))
        conn.commit()
        return row


def reserve_request(db_path: Path, update_id: int, daily_limit: int) -> bool:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM moderation_jobs WHERE update_id=?", (update_id,)).fetchone()
        if not row or row["status"] != "running":
            return False
        conn.execute("INSERT OR IGNORE INTO moderation_usage(day) VALUES(date('now'))")
        cursor = conn.execute("UPDATE moderation_usage SET requests=requests+1 WHERE day=date('now') AND requests<?",
                              (daily_limit,))
        if not cursor.rowcount:
            return False
        conn.execute("UPDATE moderation_jobs SET requested_at=CURRENT_TIMESTAMP WHERE update_id=?", (update_id,))
        conn.commit()
        return True


def hold(db_path: Path, update_id: int, reason: str, verdict: str = "error") -> None:
    database.execute(db_path, "UPDATE moderation_jobs SET status='held',reason=?,verdict=?,updated_at=CURRENT_TIMESTAMP "
                     "WHERE update_id=? AND status='running'", (reason, verdict, update_id))


def record_result(db_path: Path, update_id: int, provider: str, model: str, verdict: str, reason: str) -> None:
    database.execute(db_path, "UPDATE moderation_jobs SET provider=?,model=?,verdict=?,reason=? "
                     "WHERE update_id=? AND status='running'", (provider, model, verdict, reason, update_id))


def get(db_path: Path, update_id: int) -> sqlite3.Row | None:
    return database.fetchone(db_path, "SELECT * FROM moderation_jobs WHERE update_id=?", (update_id,))


FILTERS = {"all": "1=1", "spam": "verdict='spam'", "error": "verdict='error'",
           "other": "verdict NOT IN ('spam','error')"}


def pending_page(db_path: Path, page: int, size: int = 5, category: str = "all") -> tuple[list[sqlite3.Row], int, int]:
    condition = FILTERS[category]
    with database.connect(db_path) as conn:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM moderation_jobs WHERE status='held' AND {condition}").fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, page), pages)
        rows = conn.execute(f"SELECT * FROM moderation_jobs WHERE status='held' AND {condition} ORDER BY created_at,update_id LIMIT ? OFFSET ?",
                            (size, (page - 1) * size)).fetchall()
        return rows, page, pages


def decide(db_path: Path, update_id: int, admin_id: int, *, retry: bool = False) -> bool:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT chat_id FROM moderation_jobs WHERE update_id=? AND status='held'",
                           (update_id,)).fetchone()
        if not row:
            return False
        if retry and conn.execute("SELECT 1 FROM blacklists WHERE chat_id=?", (row[0],)).fetchone():
            return False
        conn.execute("UPDATE moderation_jobs SET status=?,decided_by=?,updated_at=CURRENT_TIMESTAMP WHERE update_id=?",
                     ("queued" if retry else "dismissed", admin_id, update_id))
        conn.execute("INSERT INTO admin_audit_logs(admin_id,action,target_chat_id,details) VALUES(?,?,?,?)",
                     (admin_id, "moderation_retry" if retry else "moderation_dismiss", row[0], f"update_id={update_id}"))
        conn.commit()
        return True


def approve(db_path: Path, update_id: int, admin_ids: set[int], admin_id: int | None = None) -> bool:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM moderation_jobs WHERE update_id=?", (update_id,)).fetchone()
        expected = "held" if admin_id is not None else "running"
        if not row or row["status"] != expected:
            return False
        if conn.execute("SELECT 1 FROM blacklists WHERE chat_id=?", (row["chat_id"],)).fetchone():
            conn.execute("UPDATE moderation_jobs SET status='blocked' WHERE update_id=?", (update_id,))
            conn.commit()
            return False
        message = json.loads(row["snapshot"])
        edited = bool(row["edited"])
        has_media = bool({"photo", "video", "document", "audio", "voice", "animation",
                          "sticker", "video_note", "location", "contact"}.intersection(message))
        inbound.persist_event(
            db_path, update_id, message["from"], row["summary"], row["chat_id"], row["message_id"],
            event_type="edited_message" if edited else "message", title="用户修改了消息" if edited else "收到用户消息",
            edited=edited, admin_ids=admin_ids,
            include_content_delivery=has_media and not bool(message.get("text")), connection=conn,
        )
        conn.execute("UPDATE moderation_jobs SET status='approved',decided_by=?,updated_at=CURRENT_TIMESTAMP WHERE update_id=?",
                     (admin_id, update_id))
        if admin_id is not None:
            conn.execute("INSERT INTO admin_audit_logs(admin_id,action,target_chat_id,details) VALUES(?,'moderation_allow',?,?)",
                         (admin_id, row["chat_id"], f"update_id={update_id}"))
        conn.commit()
        return True


def block(db_path: Path, update_id: int, admin_id: int) -> bool:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT chat_id FROM moderation_jobs WHERE update_id=? AND status='held'", (update_id,)).fetchone()
        if not row:
            return False
        chat_id = row["chat_id"]
        conn.execute("INSERT INTO blacklists(chat_id,reason,created_by) VALUES(?,'广告审核确认',?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET reason=excluded.reason,created_by=excluded.created_by",
                     (chat_id, admin_id))
        conn.execute("UPDATE moderation_jobs SET status='blocked',decided_by=?,updated_at=CURRENT_TIMESTAMP "
                     "WHERE chat_id=? AND status IN ('held','queued','running')", (admin_id, chat_id))
        conn.execute("UPDATE admin_deliveries SET status='canceled' WHERE user_chat_id=? AND status IN ('pending','failed')", (chat_id,))
        conn.execute("DELETE FROM admin_states WHERE target_chat_id=?", (chat_id,))
        conn.execute("INSERT INTO admin_audit_logs(admin_id,action,target_chat_id,details) VALUES(?,'moderation_block',?,?)",
                     (admin_id, chat_id, f"update_id={update_id}"))
        conn.commit()
        return True


def is_current(db_path: Path, update_id: int) -> bool:
    return database.fetchone(db_path,
        "SELECT 1 FROM moderation_jobs j WHERE j.update_id=? AND j.status='approved' "
        "AND NOT EXISTS(SELECT 1 FROM moderation_jobs n WHERE n.chat_id=j.chat_id AND n.message_id=j.message_id "
        "AND (n.version_at>j.version_at OR (n.version_at=j.version_at AND n.update_id>j.update_id))) "
        "AND NOT EXISTS(SELECT 1 FROM blacklists b WHERE b.chat_id=j.chat_id)",
        (update_id,)) is not None


def claim_notice(db_path: Path) -> int:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT update_id,updated_at FROM moderation_jobs WHERE status='held' ORDER BY update_id").fetchall()
        count = len(rows)
        if not count:
            return 0
        signature = hashlib.sha256(str([tuple(row) for row in rows]).encode()).hexdigest()
        cursor = conn.execute(
            "INSERT INTO moderation_notices(id,sent_at,signature) VALUES(1,CURRENT_TIMESTAMP,?) "
            "ON CONFLICT(id) DO UPDATE SET sent_at=CURRENT_TIMESTAMP,signature=excluded.signature "
            "WHERE sent_at < datetime('now','-10 minutes') AND signature<>excluded.signature", (signature,)
        )
        conn.commit()
        return count if cursor.rowcount else 0


def begin_config_input(db_path: Path, admin_id: int, prompt_id: int, kind: str,
                       provider: str = "deepseek", panel_id: int | None = None) -> None:
    with database.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE admin_config_inputs SET status='canceled' WHERE admin_id=? AND status='pending'", (admin_id,))
        conn.execute("INSERT INTO admin_config_inputs(admin_id,prompt_id,kind,expires_at,provider,panel_id) VALUES(?,?,?,?,?,?)",
                     (admin_id, prompt_id, kind, int(time.time()) + 180, provider, panel_id))
        conn.commit()


def config_input(db_path: Path, admin_id: int, prompt_id: int) -> sqlite3.Row | None:
    return database.fetchone(db_path, "SELECT * FROM admin_config_inputs WHERE admin_id=? AND prompt_id=?",
                             (admin_id, prompt_id))


def pending_config_input(db_path: Path, admin_id: int) -> sqlite3.Row | None:
    return database.fetchone(db_path, "SELECT * FROM admin_config_inputs WHERE admin_id=? AND status='pending' "
                             "ORDER BY prompt_id DESC LIMIT 1", (admin_id,))


def consume_config_input(db_path: Path, admin_id: int, prompt_id: int) -> bool:
    with database.connect(db_path) as conn:
        cursor = conn.execute("UPDATE admin_config_inputs SET status='used' "
                              "WHERE admin_id=? AND prompt_id=? AND status='pending' AND expires_at>=?",
                              (admin_id, prompt_id, int(time.time())))
        conn.commit()
        return cursor.rowcount == 1


def cancel_config_input(db_path: Path, admin_id: int) -> None:
    database.execute(db_path, "UPDATE admin_config_inputs SET status='canceled' WHERE admin_id=? AND status='pending'", (admin_id,))
