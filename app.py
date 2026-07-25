import asyncio
import contextlib
import hmac
import html
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

if os.name == "posix":
    os.umask(0o077)

load_dotenv()


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
if not OWNER_IDS:
    OWNER_IDS = set(ADMIN_IDS)
ADMIN_IDS |= OWNER_IDS

DB_BACKUP_ENABLED = os.getenv("DB_BACKUP_ENABLED", "true").lower() == "true"
DB_BACKUP_INTERVAL_SECONDS = env_int("DB_BACKUP_INTERVAL_SECONDS", 86400, 60)
DB_BACKUP_KEEP = env_int("DB_BACKUP_KEEP", 14, 1)
USER_RATE_LIMIT_COUNT = env_int("USER_RATE_LIMIT_COUNT", 8, 1)
USER_RATE_LIMIT_WINDOW_SECONDS = env_int("USER_RATE_LIMIT_WINDOW_SECONDS", 60, 1)
USER_RATE_LIMIT_COOLDOWN_SECONDS = env_int("USER_RATE_LIMIT_COOLDOWN_SECONDS", 300, 1)
MESSAGE_RETENTION_DAYS = env_int("MESSAGE_RETENTION_DAYS", 0, 0)
BROADCAST_SEND_DELAY_SECONDS = env_float("BROADCAST_SEND_DELAY_SECONDS", 0.05, 0.0)
UPDATE_PROCESSING_TIMEOUT_SECONDS = env_int("UPDATE_PROCESSING_TIMEOUT_SECONDS", 300, 30)
PENDING_REMINDER_MINUTES = env_int("PENDING_REMINDER_MINUTES", 30, 1)
TELEGRAM_INLINE_RETRY_MAX_SECONDS = env_int(
    "TELEGRAM_INLINE_RETRY_MAX_SECONDS", 5, 0
)
BROADCAST_RATE_LIMIT_RETRIES = env_int("BROADCAST_RATE_LIMIT_RETRIES", 3, 0)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is missing")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.db"
DB_BACKUP_DIR = Path(os.getenv("DB_BACKUP_DIR", str(BASE_DIR / "backups")))
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tg_bot")
for dependency_logger_name in ("httpx", "httpcore"):
    dependency_logger = logging.getLogger(dependency_logger_name)
    dependency_logger.disabled = True
    dependency_logger.propagate = False

telegram_client: httpx.AsyncClient | None = None
backup_task: asyncio.Task[None] | None = None
broadcast_worker_task: asyncio.Task[None] | None = None
broadcast_wakeup: asyncio.Event | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global telegram_client, backup_task, broadcast_worker_task, broadcast_wakeup

    init_db()
    purge_expired_data()
    try:
        await asyncio.to_thread(backup_database)
    except Exception:
        logger.exception("Initial database backup failed")

    telegram_client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    broadcast_wakeup = asyncio.Event()
    backup_task = asyncio.create_task(periodic_db_backup(), name="database-backup")
    broadcast_worker_task = asyncio.create_task(
        periodic_broadcast_worker(), name="broadcast-worker"
    )

    try:
        yield
    finally:
        for task in (broadcast_worker_task, backup_task):
            if task:
                task.cancel()
        for task in (broadcast_worker_task, backup_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if telegram_client:
            await telegram_client.aclose()
        telegram_client = None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

WELCOME_TEXT = (
    "<b>统一留言聊天入口</b>\n\n"
    "请直接在这里发送消息，我看到后会通过 Bot 回复你。"
)

QUEUE_LABELS = {
    "inbox": "待处理",
    "pending": "超时",
    "closed": "已处理",
}

BROADCAST_STATUS_LABELS = {
    "pending": "等待确认",
    "queued": "等待发送",
    "running": "发送中",
    "completed": "已完成",
    "canceled": "已取消",
}

AUDIT_ACTION_LABELS = {
    "reply_mode_enter": "进入回复",
    "reply_mode_exit": "退出回复",
    "message_sent": "发送回复",
    "conversation_takeover": "接管会话",
    "conversation_resolved": "标记已处理",
    "conversation_reopened": "重新打开",
    "blacklist_add": "加入黑名单",
    "blacklist_remove": "解除黑名单",
    "broadcast_created": "创建群发",
    "broadcast_confirmed": "确认群发",
    "broadcast_canceled": "取消群发",
    "broadcast_retry": "重试群发",
}

ButtonSpec = tuple[str, str] | tuple[str, str, str]
INLINE_BUTTON_STYLES = {"primary", "success", "danger"}


# =========================
# 数据库
# =========================

def enforce_private_mode(path: Path, mode: int) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(mode)


def secure_database_files() -> None:
    for path in (
        DB_PATH,
        Path(f"{DB_PATH}-wal"),
        Path(f"{DB_PATH}-shm"),
        Path(f"{DB_PATH}-journal"),
    ):
        enforce_private_mode(path, 0o600)


def init_db() -> None:
    with db_connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                last_message TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_states (
                admin_id INTEGER PRIMARY KEY,
                target_chat_id INTEGER,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,
                sender_id INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                telegram_message_id INTEGER,
                edited_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklists (
                chat_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT '',
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id INTEGER PRIMARY KEY,
                owner_admin_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                unread_count INTEGER NOT NULL DEFAULT 0,
                last_user_message_at DATETIME,
                last_admin_reply_at DATETIME,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_chat_id INTEGER,
                details TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_broadcasts (
                id TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                total_count INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                confirmed_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                broadcast_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (broadcast_id, chat_id),
                FOREIGN KEY (broadcast_id) REFERENCES pending_broadcasts(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'done',
                attempts INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_rate_limits (
                chat_id INTEGER PRIMARY KEY,
                window_started_at INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                blocked_until INTEGER NOT NULL DEFAULT 0,
                last_notified_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        ensure_column(conn, "message_logs", "telegram_message_id", "INTEGER")
        ensure_column(conn, "message_logs", "edited_at", "DATETIME")
        ensure_column(conn, "conversations", "unread_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "conversations", "last_user_message_at", "DATETIME")
        ensure_column(conn, "conversations", "last_admin_reply_at", "DATETIME")
        ensure_column(conn, "pending_broadcasts", "status", "TEXT NOT NULL DEFAULT 'pending'")
        ensure_column(conn, "pending_broadcasts", "total_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pending_broadcasts", "sent_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pending_broadcasts", "failed_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pending_broadcasts", "confirmed_at", "DATETIME")
        ensure_column(conn, "pending_broadcasts", "started_at", "DATETIME")
        ensure_column(conn, "pending_broadcasts", "completed_at", "DATETIME")
        ensure_column(conn, "pending_broadcasts", "last_error", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "processed_updates", "status", "TEXT NOT NULL DEFAULT 'done'")
        ensure_column(conn, "processed_updates", "attempts", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "processed_updates", "last_error", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "processed_updates", "updated_at", "DATETIME")
        conn.execute(
            "UPDATE processed_updates SET updated_at = processed_at WHERE updated_at IS NULL"
        )
        conn.execute(
            "UPDATE broadcast_recipients SET status = 'pending' WHERE status = 'sending'"
        )
        conn.execute(
            "UPDATE pending_broadcasts SET status = 'queued' WHERE status = 'running'"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_logs_chat_id_id "
            "ON message_logs(chat_id, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status "
            "ON broadcast_recipients(broadcast_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_broadcasts_status "
            "ON pending_broadcasts(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_inbox "
            "ON conversations(status, unread_count, last_user_message_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created "
            "ON admin_audit_logs(created_at DESC)"
        )
        conn.commit()
    secure_database_files()


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def db_execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with db_connect() as conn:
        conn.execute(sql, params)
        conn.commit()


def db_fetchone(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute(sql, params).fetchone()


def db_fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(sql, params).fetchall()


def claim_update(update_id: int) -> bool:
    stale_modifier = f"-{UPDATE_PROCESSING_TIMEOUT_SECONDS} seconds"
    with db_connect() as conn:
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
            return False
        if row and row["status"] == "processing" and not row["is_stale"]:
            conn.commit()
            return False

        if row:
            conn.execute(
                """
                UPDATE processed_updates
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_error = '',
                    updated_at = CURRENT_TIMESTAMP
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
        return True


def finish_update(update_id: int) -> None:
    db_execute(
        """
        UPDATE processed_updates
        SET status = 'done', last_error = '',
            updated_at = CURRENT_TIMESTAMP, processed_at = CURRENT_TIMESTAMP
        WHERE update_id = ?
        """,
        (update_id,),
    )


def fail_update(update_id: int, error: str) -> None:
    db_execute(
        """
        UPDATE processed_updates
        SET status = 'failed', last_error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE update_id = ?
        """,
        (error[:1000], update_id),
    )


def purge_expired_data() -> None:
    with db_connect() as conn:
        if MESSAGE_RETENTION_DAYS > 0:
            conn.execute(
                "DELETE FROM message_logs WHERE created_at < datetime('now', ?)",
                (f"-{MESSAGE_RETENTION_DAYS} days",),
            )
        conn.execute(
            """
            DELETE FROM processed_updates
            WHERE status = 'done'
              AND processed_at < datetime('now', '-7 days')
            """
        )
        conn.execute(
            """
            DELETE FROM admin_audit_logs
            WHERE created_at < datetime('now', '-365 days')
            """
        )
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'canceled', completed_at = CURRENT_TIMESTAMP,
                last_error = 'confirmation expired'
            WHERE status = 'pending'
              AND created_at < datetime('now', '-1 day')
            """
        )
        conn.execute(
            """
            DELETE FROM broadcast_recipients
            WHERE broadcast_id IN (
                SELECT id FROM pending_broadcasts
                WHERE status IN ('completed', 'canceled')
                  AND created_at < datetime('now', '-30 days')
            )
            """
        )
        conn.execute(
            """
            DELETE FROM pending_broadcasts
            WHERE status IN ('completed', 'canceled')
              AND created_at < datetime('now', '-30 days')
            """
        )
        conn.execute(
            "DELETE FROM user_rate_limits WHERE window_started_at < ?",
            (int(time.time()) - 7 * 86400,),
        )
        conn.commit()


def backup_database() -> Path | None:
    if not DB_BACKUP_ENABLED or not DB_PATH.exists():
        return None

    DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    enforce_private_mode(DB_BACKUP_DIR, 0o700)
    backup_path = DB_BACKUP_DIR / f"bot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"

    with sqlite3.connect(DB_PATH, timeout=30) as source:
        with sqlite3.connect(backup_path) as backup:
            source.backup(backup)
    enforce_private_mode(backup_path, 0o600)
    secure_database_files()

    if DB_BACKUP_KEEP > 0:
        backups = sorted(DB_BACKUP_DIR.glob("bot-*.db"), key=lambda p: p.stat().st_mtime)
        for existing_backup in backups:
            enforce_private_mode(existing_backup, 0o600)
        for old_backup in backups[:-DB_BACKUP_KEEP]:
            old_backup.unlink(missing_ok=True)

    return backup_path


async def periodic_db_backup() -> None:
    if not DB_BACKUP_ENABLED:
        return

    while True:
        await asyncio.sleep(DB_BACKUP_INTERVAL_SECONDS)
        try:
            backup_path = await asyncio.to_thread(backup_database)
            await asyncio.to_thread(purge_expired_data)
            if backup_path:
                logger.info("Database backup created: %s", backup_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic database backup failed")


# =========================
# Telegram API
# =========================

TELEGRAM_TEXT_LIMIT = 4096


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        method: str,
        status_code: int,
        description: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            f"Telegram API {method} failed with HTTP "
            f"{status_code}: {truncate_text(description, 300)}"
        )
        self.method = method
        self.status_code = status_code
        self.description = description
        self.retry_after = retry_after


class PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_plain_text(value: str) -> str:
    parser = PlainTextHTMLParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


def escape_html_limited(value: str, limit: int) -> str:
    escaped = html.escape(value)
    if len(escaped) <= limit:
        return escaped

    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = html.escape(value[:middle]) + "…"
        if len(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return html.escape(value[:low]) + "…"


async def tg(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        if telegram_client is None:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"{API_BASE}/{method}", json=payload)
        else:
            response = await telegram_client.post(f"{API_BASE}/{method}", json=payload)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Telegram API {method} request failed ({type(exc).__name__})"
        ) from None

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.is_error or not isinstance(data, dict) or not data.get("ok", False):
        description = (
            str(data.get("description", "unexpected response"))
            if isinstance(data, dict)
            else "unexpected response"
        )
        parameters = data.get("parameters") if isinstance(data, dict) else None
        raw_retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
        retry_after = (
            int(raw_retry_after)
            if isinstance(raw_retry_after, int) and raw_retry_after >= 0
            else None
        )
        raise TelegramAPIError(
            method,
            response.status_code,
            description,
            retry_after=retry_after,
        ) from None
    return data


async def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
    rate_limit_retries: int = 1,
    rate_limit_max_wait_seconds: int | None = TELEGRAM_INLINE_RETRY_MAX_SECONDS,
) -> bool:
    if len(text) > TELEGRAM_TEXT_LIMIT:
        logger.warning(
            "Telegram message exceeded limit; falling back to truncated plain text chat_id=%s",
            chat_id,
        )
        text = html_to_plain_text(text) if parse_mode == "HTML" else text
        text = truncate_text(text, TELEGRAM_TEXT_LIMIT)
        parse_mode = None

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    rate_limit_attempt = 0
    while True:
        try:
            await tg("sendMessage", payload)
            return True
        except TelegramAPIError as exc:
            can_retry = (
                exc.retry_after is not None
                and rate_limit_attempt < rate_limit_retries
                and (
                    rate_limit_max_wait_seconds is None
                    or exc.retry_after <= rate_limit_max_wait_seconds
                )
            )
            if can_retry:
                rate_limit_attempt += 1
                logger.warning(
                    "Telegram flood control chat_id=%s retry_after=%s attempt=%s",
                    chat_id,
                    exc.retry_after,
                    rate_limit_attempt,
                )
                await asyncio.sleep(exc.retry_after)
                continue
            logger.warning("sendMessage failed chat_id=%s error=%s", chat_id, exc)
            return False
        except Exception as exc:
            logger.warning("sendMessage failed chat_id=%s error=%s", chat_id, exc)
            return False


async def answer_callback_query(callback_query_id: str, text: str = "") -> bool:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        await tg("answerCallbackQuery", payload)
    except Exception as exc:
        logger.warning("answerCallbackQuery failed error=%s", exc)
        return False
    return True


async def edit_message_text(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        await tg("editMessageText", payload)
    except TelegramAPIError as exc:
        if "message is not modified" in exc.description.lower():
            return True
        logger.warning(
            "editMessageText failed chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "editMessageText failed chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            exc,
        )
        return False
    return True


async def copy_message(
    to_chat_id: int,
    from_chat_id: int,
    message_id: int,
    rate_limit_retries: int = 1,
    rate_limit_max_wait_seconds: int | None = TELEGRAM_INLINE_RETRY_MAX_SECONDS,
) -> bool:
    rate_limit_attempt = 0
    while True:
        try:
            await tg(
                "copyMessage",
                {
                    "chat_id": to_chat_id,
                    "from_chat_id": from_chat_id,
                    "message_id": message_id,
                },
            )
            return True
        except TelegramAPIError as exc:
            can_retry = (
                exc.retry_after is not None
                and rate_limit_attempt < rate_limit_retries
                and (
                    rate_limit_max_wait_seconds is None
                    or exc.retry_after <= rate_limit_max_wait_seconds
                )
            )
            if can_retry:
                rate_limit_attempt += 1
                logger.warning(
                    "Telegram copy flood control to_chat_id=%s retry_after=%s attempt=%s",
                    to_chat_id,
                    exc.retry_after,
                    rate_limit_attempt,
                )
                await asyncio.sleep(exc.retry_after)
                continue
            logger.warning(
                "copyMessage failed to_chat_id=%s from_chat_id=%s "
                "message_id=%s error=%s",
                to_chat_id,
                from_chat_id,
                message_id,
                exc,
            )
            return False
        except Exception as exc:
            logger.warning(
                "copyMessage failed to_chat_id=%s from_chat_id=%s "
                "message_id=%s error=%s",
                to_chat_id,
                from_chat_id,
                message_id,
                exc,
            )
            return False


def inline_keyboard(rows: list[list[ButtonSpec]]) -> dict[str, Any]:
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        keyboard_row: list[dict[str, str]] = []
        for button_spec in row:
            if len(button_spec) not in {2, 3}:
                raise ValueError("inline button must contain text, data, and optional style")
            text, data = button_spec[:2]
            button = {"text": text, "callback_data": data}
            if len(button_spec) == 3:
                style = button_spec[2]
                if style not in INLINE_BUTTON_STYLES:
                    raise ValueError(f"unsupported inline button style: {style}")
                button["style"] = style
            keyboard_row.append(button)
        keyboard.append(keyboard_row)
    return {"inline_keyboard": keyboard}


# =========================
# 用户、历史、黑名单
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def add_admin_audit(
    admin_id: int,
    action: str,
    target_chat_id: int | None = None,
    details: str = "",
) -> None:
    db_execute(
        """
        INSERT INTO admin_audit_logs (
            admin_id, action, target_chat_id, details
        ) VALUES (?, ?, ?, ?)
        """,
        (admin_id, action[:80], target_chat_id, details[:500]),
    )


def user_label(user: dict[str, Any]) -> str:
    username = user.get("username")
    name = " ".join(
        x for x in [user.get("first_name"), user.get("last_name")] if x
    ).strip()
    if username:
        return f"@{username}"
    return name or str(user.get("id"))


def compact_timestamp(value: Any) -> str:
    if not value:
        return "-"
    return str(value).replace("T", " ")[:16]


def row_user_label(row: sqlite3.Row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    name = " ".join(
        value for value in (row["first_name"], row["last_name"]) if value
    ).strip()
    return name or str(row["chat_id"])


def message_content(message: dict[str, Any]) -> str:
    if text := message.get("text"):
        return text
    caption = message.get("caption") or ""
    if "photo" in message:
        kind = "[图片]"
        return f"{kind} {caption}".strip()
    if "document" in message:
        document = message["document"]
        filename = document.get("file_name") or "文件"
        return f"[文件] {filename} {caption}".strip()
    if "voice" in message:
        return f"[语音] {caption}".strip()
    if "video" in message:
        return f"[视频] {caption}".strip()
    if "audio" in message:
        return f"[音频] {caption}".strip()
    if "video_note" in message:
        return "[视频消息]"
    if "sticker" in message:
        return "[贴纸]"
    return "[非文本消息]"


def message_kind(message: dict[str, Any]) -> str:
    for kind in (
        "text",
        "photo",
        "document",
        "voice",
        "video",
        "audio",
        "video_note",
        "sticker",
    ):
        if kind in message:
            return kind
    return "other"


def can_copy_message(message: dict[str, Any]) -> bool:
    return "message_id" in message and "chat" in message


def is_private_chat_message(message: dict[str, Any]) -> bool:
    return message.get("chat", {}).get("type") == "private"


def is_private_callback(callback: dict[str, Any]) -> bool:
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    return chat.get("type") == "private"


def upsert_user(user: dict[str, Any], text: str) -> None:
    db_execute(
        """
        INSERT INTO users (chat_id, username, first_name, last_name, last_message, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            last_message = excluded.last_message,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            user["id"],
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
            text,
        ),
    )


def add_message_log(
    chat_id: int,
    sender_type: str,
    sender_id: int,
    text: str,
    telegram_message_id: int | None = None,
) -> None:
    db_execute(
        """
        INSERT INTO message_logs (
            chat_id, sender_type, sender_id, text, telegram_message_id
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, sender_type, sender_id, text, telegram_message_id),
    )


def update_user_message_log(chat_id: int, message_id: int, text: str) -> bool:
    with db_connect() as conn:
        cursor = conn.execute(
            """
            UPDATE message_logs
            SET text = ?, edited_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM message_logs
                WHERE chat_id = ?
                  AND sender_type = 'user'
                  AND telegram_message_id = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (text, chat_id, message_id),
        )
        conn.commit()
        return cursor.rowcount == 1


def get_history(
    chat_id: int,
    limit: int = 5,
    exclude_message_id: int | None = None,
) -> list[sqlite3.Row]:
    if exclude_message_id is None:
        rows = db_fetchall(
            """
            SELECT sender_type, text, edited_at, created_at
            FROM message_logs
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
    else:
        rows = db_fetchall(
            """
            SELECT sender_type, text, edited_at, created_at
            FROM message_logs
            WHERE chat_id = ?
              AND (telegram_message_id IS NULL OR telegram_message_id != ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, exclude_message_id, limit),
        )
    return rows[::-1]


def format_history(
    chat_id: int,
    limit: int = 5,
    exclude_message_id: int | None = None,
    max_chars: int = 1800,
) -> str:
    rows = get_history(chat_id, limit, exclude_message_id)
    if not rows:
        return "暂无历史消息"

    lines: list[str] = []
    used_chars = 0
    for row in rows:
        sender = "用户" if row["sender_type"] == "user" else "管理员"
        edited = "（已编辑）" if row["edited_at"] else ""
        text = escape_html_limited(row["text"], 300)
        line = (
            f"<b>{sender}{edited}</b> · {compact_timestamp(row['created_at'])}\n"
            f"{text}"
        )
        if lines and used_chars + len(line) + 2 > max_chars:
            break
        lines.append(line)
        used_chars += len(line) + 2
    return "\n\n".join(lines)


def check_user_rate_limit(chat_id: int) -> tuple[bool, bool, int]:
    now = int(time.time())
    with db_connect() as conn:
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
                (chat_id, now),
            )
            conn.commit()
            return True, False, 0

        blocked_until = int(row["blocked_until"])
        if blocked_until > now:
            conn.commit()
            return False, False, blocked_until - now

        window_started_at = int(row["window_started_at"])
        if now - window_started_at >= USER_RATE_LIMIT_WINDOW_SECONDS:
            conn.execute(
                """
                UPDATE user_rate_limits
                SET window_started_at = ?, message_count = 1, blocked_until = 0
                WHERE chat_id = ?
                """,
                (now, chat_id),
            )
            conn.commit()
            return True, False, 0

        message_count = int(row["message_count"]) + 1
        if message_count > USER_RATE_LIMIT_COUNT:
            blocked_until = now + USER_RATE_LIMIT_COOLDOWN_SECONDS
            conn.execute(
                """
                UPDATE user_rate_limits
                SET message_count = ?, blocked_until = ?, last_notified_at = ?
                WHERE chat_id = ?
                """,
                (message_count, blocked_until, now, chat_id),
            )
            conn.commit()
            return False, True, USER_RATE_LIMIT_COOLDOWN_SECONDS

        conn.execute(
            "UPDATE user_rate_limits SET message_count = ? WHERE chat_id = ?",
            (message_count, chat_id),
        )
        conn.commit()
        return True, False, 0


def is_blacklisted(chat_id: int) -> bool:
    return db_fetchone(
        "SELECT chat_id FROM blacklists WHERE chat_id = ?",
        (chat_id,),
    ) is not None


def blacklist_user(chat_id: int, admin_id: int, reason: str = "") -> None:
    db_execute(
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


def unblacklist_user(chat_id: int) -> None:
    db_execute("DELETE FROM blacklists WHERE chat_id = ?", (chat_id,))


def set_admin_state(admin_id: int, target_chat_id: int) -> None:
    db_execute(
        """
        INSERT INTO admin_states (admin_id, target_chat_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(admin_id) DO UPDATE SET
            target_chat_id = excluded.target_chat_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (admin_id, target_chat_id),
    )


def get_admin_state(admin_id: int) -> int | None:
    row = db_fetchone(
        "SELECT target_chat_id FROM admin_states WHERE admin_id = ?",
        (admin_id,),
    )
    return int(row["target_chat_id"]) if row else None


def clear_admin_state(
    admin_id: int, expected_target_chat_id: int | None = None
) -> int | None:
    target_chat_id = get_admin_state(admin_id)
    if (
        expected_target_chat_id is not None
        and target_chat_id != expected_target_chat_id
    ):
        return None
    db_execute("DELETE FROM admin_states WHERE admin_id = ?", (admin_id,))
    return target_chat_id


def clear_admin_states_for_target(chat_id: int) -> None:
    db_execute("DELETE FROM admin_states WHERE target_chat_id = ?", (chat_id,))


def record_user_activity(chat_id: int, increment_unread: bool = True) -> None:
    db_execute(
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


def mark_conversation_replied(chat_id: int) -> None:
    db_execute(
        """
        INSERT INTO conversations (
            chat_id, status, unread_count, last_admin_reply_at, updated_at
        )
        VALUES (?, 'open', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            status = 'open',
            unread_count = 0,
            last_admin_reply_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP,
            closed_at = NULL
        """,
        (chat_id,),
    )


def reopen_conversation(chat_id: int) -> None:
    db_execute(
        """
        INSERT INTO conversations (chat_id, status, unread_count, updated_at)
        VALUES (?, 'open', 0, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            status = 'open',
            updated_at = CURRENT_TIMESTAMP,
            closed_at = NULL
        """,
        (chat_id,),
    )


def get_conversation(chat_id: int) -> sqlite3.Row | None:
    return db_fetchone(
        """
        SELECT chat_id, owner_admin_id, status, unread_count,
               last_user_message_at, last_admin_reply_at,
               updated_at, closed_at
        FROM conversations
        WHERE chat_id = ?
        """,
        (chat_id,),
    )


def get_conversation_owner(chat_id: int) -> int | None:
    row = get_conversation(chat_id)
    if not row or row["status"] != "open" or row["owner_admin_id"] is None:
        return None
    return int(row["owner_admin_id"])


def claim_conversation(chat_id: int, admin_id: int) -> None:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
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
                status = 'open',
                updated_at = CURRENT_TIMESTAMP,
                closed_at = NULL
            """,
            (chat_id, admin_id),
        )
        conn.commit()


def close_conversation(chat_id: int) -> None:
    db_execute(
        """
        INSERT INTO conversations (chat_id, owner_admin_id, status, updated_at, closed_at)
        VALUES (?, NULL, 'closed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            owner_admin_id = NULL,
            status = 'closed',
            unread_count = 0,
            updated_at = CURRENT_TIMESTAMP,
            closed_at = CURRENT_TIMESTAMP
        """,
        (chat_id,),
    )
    clear_admin_states_for_target(chat_id)


def get_conversation_queue(queue_name: str, limit: int = 10) -> list[sqlite3.Row]:
    where_clause: str
    order_clause: str
    params: tuple[Any, ...]
    if queue_name == "inbox":
        where_clause = "c.status = 'open' AND c.unread_count > 0"
        order_clause = "c.last_user_message_at DESC"
        params = (limit,)
    elif queue_name == "pending":
        where_clause = (
            "c.status = 'open' AND c.unread_count > 0 "
            "AND c.last_user_message_at <= datetime('now', ?)"
        )
        order_clause = "c.last_user_message_at ASC"
        params = (f"-{PENDING_REMINDER_MINUTES} minutes", limit)
    elif queue_name == "closed":
        where_clause = "c.status = 'closed'"
        order_clause = "c.closed_at DESC"
        params = (limit,)
    else:
        raise ValueError(f"unsupported queue: {queue_name}")

    return db_fetchall(
        f"""
        SELECT c.chat_id, c.owner_admin_id, c.status, c.unread_count,
               c.last_user_message_at, c.last_admin_reply_at,
               c.updated_at, c.closed_at,
               u.username, u.first_name, u.last_name, u.last_message
        FROM conversations c
        LEFT JOIN users u ON u.chat_id = c.chat_id
        WHERE {where_clause}
        ORDER BY {order_clause}
        LIMIT ?
        """,
        params,
    )


def get_queue_counts() -> dict[str, int]:
    row = db_fetchone(
        """
        SELECT
            SUM(CASE
                WHEN status = 'open' AND unread_count > 0 THEN 1
                ELSE 0
            END) AS inbox,
            SUM(CASE
                WHEN status = 'open' AND unread_count > 0
                 AND last_user_message_at <= datetime('now', ?)
                THEN 1 ELSE 0
            END) AS pending,
            SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
        FROM conversations
        """,
        (f"-{PENDING_REMINDER_MINUTES} minutes",),
    )
    return {
        queue_name: int(row[queue_name] or 0) if row else 0
        for queue_name in QUEUE_LABELS
    }


def format_admin_dashboard(admin_id: int) -> str:
    counts = get_queue_counts()
    role = "负责人" if is_owner(admin_id) else "管理员"
    target_chat_id = get_admin_state(admin_id)
    reply_state = (
        f"\n\n<b>正在回复</b>\n用户 <code>{target_chat_id}</code>"
        if target_chat_id is not None
        else ""
    )
    return (
        "<b>留言工作台</b>\n"
        f"<code>{admin_id}</code> · {role}\n\n"
        f"待处理：<b>{counts['inbox']}</b>\n"
        f"其中超时：<b>{counts['pending']}</b>\n"
        f"已处理：<b>{counts['closed']}</b>"
        f"{reply_state}"
    )


def format_conversation_queue(queue_name: str, rows: list[sqlite3.Row]) -> str:
    titles = {
        "inbox": "待处理",
        "pending": f"超时待处理（超过 {PENDING_REMINDER_MINUTES} 分钟）",
        "closed": "已处理",
    }
    lines = [f"<b>{titles[queue_name]}</b>", f"当前显示 {len(rows)} 个会话"]
    if not rows:
        empty_messages = {
            "inbox": "当前没有待处理消息。",
            "pending": "当前没有超时消息。",
            "closed": "当前没有已处理会话。",
        }
        lines.append(empty_messages[queue_name])
        return "\n\n".join(lines)

    for index, row in enumerate(rows, start=1):
        name = escape_html_limited(row_user_label(row), 80)
        owner_admin_id = row["owner_admin_id"]
        owner = (
            f"管理员 <code>{owner_admin_id}</code>"
            if owner_admin_id is not None
            else "未接管"
        )
        unread = int(row["unread_count"] or 0)
        timestamp = (
            row["closed_at"]
            if queue_name == "closed"
            else row["last_user_message_at"]
        )
        if queue_name == "closed":
            state_line = f"已处理 · {compact_timestamp(timestamp)}"
        else:
            state_line = (
                f"{unread} 条待处理 · {owner} · {compact_timestamp(timestamp)}"
            )
        lines.append(
            f"<b>{index}. {name}</b>\n"
            f"<code>{row['chat_id']}</code> · {state_line}\n"
            f"{escape_html_limited(row['last_message'] or '无内容摘要', 160)}"
        )
    return "\n\n".join(lines)


def get_recent_users(limit: int = 10) -> list[sqlite3.Row]:
    return db_fetchall(
        """
        SELECT u.chat_id, u.username, u.first_name, u.last_name, u.last_message,
               u.updated_at, b.chat_id AS blocked,
               COALESCE(c.unread_count, 0) AS unread_count,
               c.status AS conversation_status
        FROM users u
        LEFT JOIN blacklists b ON b.chat_id = u.chat_id
        LEFT JOIN conversations c ON c.chat_id = u.chat_id
        ORDER BY u.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def format_recent_users(rows: list[sqlite3.Row]) -> str:
    lines = ["<b>最近用户</b>", f"当前显示 {len(rows)} 位用户"]
    if not rows:
        lines.append("暂无用户记录。")
        return "\n\n".join(lines)

    for index, row in enumerate(rows, start=1):
        unread = int(row["unread_count"] or 0)
        if row["blocked"]:
            state = "黑名单"
        elif unread > 0:
            state = f"待处理 {unread}"
        elif row["conversation_status"] == "closed":
            state = "已处理"
        else:
            state = "暂无待处理"
        lines.append(
            f"<b>{index}. {escape_html_limited(row_user_label(row), 80)}</b>\n"
            f"<code>{row['chat_id']}</code> · {state} · "
            f"{compact_timestamp(row['updated_at'])}\n"
            f"{escape_html_limited(row['last_message'] or '无内容摘要', 140)}"
        )
    return "\n\n".join(lines)


def format_user_detail(chat_id: int) -> str:
    user = db_fetchone("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    if not user:
        return f"<b>用户详情</b>\n\n未找到用户 <code>{chat_id}</code>。"

    conversation = get_conversation(chat_id)
    unread = int(conversation["unread_count"] or 0) if conversation else 0
    owner_admin_id = conversation["owner_admin_id"] if conversation else None
    if is_blacklisted(chat_id):
        state = "黑名单"
    elif conversation and conversation["status"] == "closed":
        state = "已处理"
    elif unread > 0:
        state = f"待处理 {unread}"
    else:
        state = "暂无待处理"
    owner = (
        f"<code>{owner_admin_id}</code>" if owner_admin_id is not None else "未接管"
    )
    last_user_message_at = (
        compact_timestamp(conversation["last_user_message_at"])
        if conversation
        else "-"
    )
    last_admin_reply_at = (
        compact_timestamp(conversation["last_admin_reply_at"])
        if conversation
        else "-"
    )
    return (
        "<b>用户详情</b>\n\n"
        f"<b>{escape_html_limited(row_user_label(user), 100)}</b>\n"
        f"ID：<code>{chat_id}</code>\n"
        f"状态：{state}\n"
        f"接管：{owner}\n"
        f"最后留言：{last_user_message_at}\n"
        f"最后回复：{last_admin_reply_at}\n\n"
        "<b>最近记录</b>\n"
        f"{format_history(chat_id, limit=10)}"
    )


def create_pending_broadcast(admin_id: int, content: str) -> str:
    broadcast_id = uuid.uuid4().hex[:12]
    db_execute(
        """
        INSERT INTO pending_broadcasts (id, admin_id, content, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (broadcast_id, admin_id, content),
    )
    return broadcast_id


def queue_broadcast(broadcast_id: str, admin_id: int) -> tuple[str, int]:
    with db_connect() as conn:
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
                failed_count = 0, confirmed_at = CURRENT_TIMESTAMP,
                last_error = ''
            WHERE id = ?
            """,
            (total, broadcast_id),
        )
        conn.commit()
        return "queued", total


def cancel_pending_broadcast(broadcast_id: str, admin_id: int) -> str:
    with db_connect() as conn:
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


def retry_failed_broadcast(broadcast_id: str) -> tuple[str, int]:
    with db_connect() as conn:
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
            SELECT COUNT(*) AS total
            FROM broadcast_recipients
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
            SET status = 'pending', last_error = '',
                updated_at = CURRENT_TIMESTAMP
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


def claim_next_broadcast_job() -> sqlite3.Row | None:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            """
            SELECT id, admin_id, content
            FROM pending_broadcasts
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


def claim_next_broadcast_recipient(broadcast_id: str) -> int | None:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        recipient = conn.execute(
            """
            SELECT chat_id
            FROM broadcast_recipients
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


def finish_broadcast_recipient(
    broadcast_id: str, chat_id: int, sent: bool, error: str = ""
) -> None:
    recipient_status = "sent" if sent else "failed"
    counter = "sent_count" if sent else "failed_count"
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE broadcast_id = ? AND chat_id = ?
            """,
            (recipient_status, error[:500], broadcast_id, chat_id),
        )
        conn.execute(
            f"UPDATE pending_broadcasts SET {counter} = {counter} + 1 WHERE id = ?",
            (broadcast_id,),
        )
        conn.commit()


def complete_broadcast(broadcast_id: str) -> sqlite3.Row | None:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM broadcast_recipients
            WHERE broadcast_id = ? AND status IN ('pending', 'sending')
            """,
            (broadcast_id,),
        ).fetchone()
        if remaining and int(remaining["total"]) > 0:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (broadcast_id,),
        )
        result = conn.execute(
            """
            SELECT admin_id, total_count, sent_count, failed_count
            FROM pending_broadcasts
            WHERE id = ?
            """,
            (broadcast_id,),
        ).fetchone()
        conn.commit()
        return result


def recover_broadcast_job(broadcast_id: str, error: str) -> None:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE broadcast_recipients
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE broadcast_id = ? AND status = 'sending'
            """,
            (broadcast_id,),
        )
        conn.execute(
            """
            UPDATE pending_broadcasts
            SET status = 'queued', last_error = ?
            WHERE id = ? AND status = 'running'
            """,
            (error[:1000], broadcast_id),
        )
        conn.commit()


def active_user_count() -> int:
    row = db_fetchone(
        """
        SELECT COUNT(*) AS total
        FROM users u
        LEFT JOIN blacklists b ON b.chat_id = u.chat_id
        WHERE b.chat_id IS NULL
        """
    )
    return int(row["total"]) if row else 0


async def process_broadcast_job(job: sqlite3.Row) -> None:
    broadcast_id = str(job["id"])
    content = str(job["content"])

    while True:
        chat_id = claim_next_broadcast_recipient(broadcast_id)
        if chat_id is None:
            result = complete_broadcast(broadcast_id)
            if result:
                failed_count = int(result["failed_count"])
                reply_markup = (
                    inline_keyboard(
                        [
                            [
                                (
                                    "重试失败用户",
                                    f"broadcast_retry:{broadcast_id}",
                                    "primary",
                                )
                            ]
                        ]
                    )
                    if failed_count > 0
                    else None
                )
                await send_message(
                    int(result["admin_id"]),
                    "<b>群发完成</b>\n\n"
                    f"成功：<b>{result['sent_count']}</b>\n"
                    f"失败：<b>{failed_count}</b>\n"
                    f"总计：{result['total_count']}",
                    reply_markup=reply_markup,
                )
            return

        if is_blacklisted(chat_id):
            finish_broadcast_recipient(
                broadcast_id, chat_id, False, "user is blacklisted"
            )
            continue

        sent = False
        for attempt in range(2):
            if await send_message(
                chat_id,
                content,
                parse_mode=None,
                rate_limit_retries=BROADCAST_RATE_LIMIT_RETRIES,
                rate_limit_max_wait_seconds=None,
            ):
                sent = True
                break
            if attempt == 0:
                await asyncio.sleep(1)
        finish_broadcast_recipient(
            broadcast_id,
            chat_id,
            sent,
            "Telegram send failed" if not sent else "",
        )
        if BROADCAST_SEND_DELAY_SECONDS:
            await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)


async def periodic_broadcast_worker() -> None:
    while True:
        job: sqlite3.Row | None = None
        try:
            job = claim_next_broadcast_job()
            if job:
                await process_broadcast_job(job)
                continue

            if broadcast_wakeup is None:
                await asyncio.sleep(2)
                continue
            broadcast_wakeup.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(broadcast_wakeup.wait(), timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Broadcast worker failed")
            if job:
                recover_broadcast_job(str(job["id"]), str(exc))
            await asyncio.sleep(2)


def admin_user_keyboard(chat_id: int, viewer_admin_id: int | None = None) -> dict[str, Any]:
    conversation = get_conversation(chat_id)
    is_closed = bool(conversation and conversation["status"] == "closed")
    owner_admin_id = get_conversation_owner(chat_id)
    if is_blacklisted(chat_id):
        return inline_keyboard(
            [
                [
                    ("用户详情", f"detail:{chat_id}"),
                    ("解除黑名单", f"unblacklist:{chat_id}", "success"),
                ],
                [("返回工作台", "admin:dashboard")],
            ]
        )

    if is_closed:
        return inline_keyboard(
            [
                [
                    ("重新打开", f"reopen:{chat_id}", "primary"),
                    ("用户详情", f"detail:{chat_id}"),
                ],
                [("加入黑名单", f"blacklist:{chat_id}")],
                [("返回工作台", "admin:dashboard")],
            ]
        )

    if owner_admin_id and viewer_admin_id and owner_admin_id != viewer_admin_id:
        reply_button: ButtonSpec = ("接管", f"takeover:{chat_id}", "primary")
    else:
        reply_button = ("回复", f"reply:{chat_id}", "primary")
    return inline_keyboard(
        [
            [
                reply_button,
                ("标记已处理", f"resolve:{chat_id}", "success"),
            ],
            [
                ("用户详情", f"detail:{chat_id}"),
                ("加入黑名单", f"blacklist:{chat_id}"),
            ],
            [("返回工作台", "admin:dashboard")],
        ]
    )


def admin_dashboard_keyboard() -> dict[str, Any]:
    counts = get_queue_counts()
    return inline_keyboard(
        [
            [
                (f"待处理 {counts['inbox']}", "queue:inbox", "primary"),
                (f"超时 {counts['pending']}", "queue:pending"),
            ],
            [
                (f"已处理 {counts['closed']}", "queue:closed", "success"),
                ("最近用户", "admin:users"),
            ],
            [("刷新", "admin:dashboard")],
        ]
    )


def queue_navigation_rows() -> list[list[ButtonSpec]]:
    counts = get_queue_counts()
    return [
        [
            (f"待处理 {counts['inbox']}", "queue:inbox", "primary"),
            (f"超时 {counts['pending']}", "queue:pending"),
            (f"已处理 {counts['closed']}", "queue:closed", "success"),
        ],
        [("返回工作台", "admin:dashboard")],
    ]


def recent_users_keyboard(rows: list[sqlite3.Row]) -> dict[str, Any]:
    keyboard_rows: list[list[ButtonSpec]] = []
    current_row: list[ButtonSpec] = []
    for index, row in enumerate(rows, start=1):
        current_row.append((f"{index} 详情", f"detail:{row['chat_id']}"))
        if len(current_row) == 2:
            keyboard_rows.append(current_row)
            current_row = []
    if current_row:
        keyboard_rows.append(current_row)
    keyboard_rows.append([("返回工作台", "admin:dashboard")])
    return inline_keyboard(keyboard_rows)


def conversation_queue_keyboard(
    rows: list[sqlite3.Row],
    queue_name: str,
    viewer_admin_id: int | None = None,
) -> dict[str, Any]:
    keyboard_rows: list[list[ButtonSpec]] = []
    for index, row in enumerate(rows, start=1):
        chat_id = int(row["chat_id"])
        if queue_name == "closed":
            keyboard_rows.append(
                [
                    (f"{index} 重开", f"reopen:{chat_id}", "primary"),
                    (f"{index} 详情", f"detail:{chat_id}"),
                ]
            )
            continue

        owner_admin_id = row["owner_admin_id"]
        if (
            owner_admin_id is not None
            and viewer_admin_id is not None
            and int(owner_admin_id) != viewer_admin_id
        ):
            primary_button: ButtonSpec = (
                f"{index} 接管",
                f"takeover:{chat_id}",
                "primary",
            )
        else:
            primary_button = (
                f"{index} 回复",
                f"reply:{chat_id}",
                "primary",
            )
        keyboard_rows.append(
            [
                primary_button,
                (f"{index} 详情", f"detail:{chat_id}"),
                (f"{index} 处理", f"resolve:{chat_id}", "success"),
            ]
        )
    keyboard_rows.extend(queue_navigation_rows())
    return inline_keyboard(keyboard_rows)


def exit_reply_keyboard(chat_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("退出回复", f"cancel:{chat_id}"),
                ("标记已处理", f"resolve:{chat_id}", "success"),
            ],
            [("用户详情", f"detail:{chat_id}")],
        ]
    )


def welcome_keyboard() -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("如何留言", "user_help", "primary"),
                ("支持格式", "user_guide"),
            ]
        ]
    )


async def present_admin_view(
    admin_id: int,
    text: str,
    reply_markup: dict[str, Any],
    callback: dict[str, Any] | None = None,
) -> None:
    if callback:
        callback_message = callback.get("message") or {}
        callback_chat = callback_message.get("chat") or {}
        callback_chat_id = callback_chat.get("id")
        callback_message_id = callback_message.get("message_id")
        if isinstance(callback_chat_id, int) and isinstance(callback_message_id, int):
            if await edit_message_text(
                callback_chat_id,
                callback_message_id,
                text,
                reply_markup=reply_markup,
            ):
                return
    await send_message(admin_id, text, reply_markup=reply_markup)


async def show_admin_dashboard(
    admin_id: int,
    callback: dict[str, Any] | None = None,
) -> None:
    await present_admin_view(
        admin_id,
        format_admin_dashboard(admin_id),
        admin_dashboard_keyboard(),
        callback,
    )


async def show_conversation_queue(
    admin_id: int,
    queue_name: str,
    callback: dict[str, Any] | None = None,
) -> None:
    rows = get_conversation_queue(queue_name)
    await present_admin_view(
        admin_id,
        format_conversation_queue(queue_name, rows),
        conversation_queue_keyboard(
            rows,
            queue_name,
            viewer_admin_id=admin_id,
        ),
        callback,
    )


async def show_recent_users(
    admin_id: int,
    callback: dict[str, Any] | None = None,
) -> None:
    rows = get_recent_users()
    await present_admin_view(
        admin_id,
        format_recent_users(rows),
        recent_users_keyboard(rows),
        callback,
    )


# =========================
# 业务逻辑
# =========================

async def notify_admins(
    user: dict[str, Any],
    text: str,
    source_message: dict[str, Any] | None = None,
    title: str = "收到用户消息",
    exclude_message_id: int | None = None,
) -> None:
    chat_id = int(user["id"])
    history = format_history(
        chat_id,
        limit=6,
        exclude_message_id=exclude_message_id,
        max_chars=1800,
    )
    conversation = get_conversation(chat_id)
    owner_admin_id = get_conversation_owner(chat_id)
    unread_count = int(conversation["unread_count"]) if conversation else 0
    owner_state = (
        f"管理员 <code>{owner_admin_id}</code> 接管"
        if owner_admin_id
        else "未接管"
    )
    msg = (
        f"<b>{html.escape(title)}</b>\n"
        f"{unread_count} 条待处理 · {owner_state}\n\n"
        f"<b>{escape_html_limited(user_label(user), 100)}</b>\n"
        f"ID：<code>{chat_id}</code>\n\n"
        "<b>本条内容</b>\n"
        f"{escape_html_limited(text, 1200)}\n\n"
        "<b>最近记录</b>\n"
        f"{history}"
    )

    for admin_id in ADMIN_IDS:
        notification_sent = await send_message(
            admin_id,
            msg,
            reply_markup=admin_user_keyboard(chat_id, viewer_admin_id=admin_id),
        )
        if notification_sent and source_message and can_copy_message(source_message):
            if source_message.get("text") is None:
                await copy_message(
                    admin_id,
                    int(source_message["chat"]["id"]),
                    int(source_message["message_id"]),
                )


async def send_welcome(chat_id: int) -> None:
    await send_message(
        chat_id,
        WELCOME_TEXT,
        reply_markup=welcome_keyboard(),
    )


def normalize_command_text(text: str) -> str:
    if not text.startswith("/"):
        return text
    first, separator, remainder = text.partition(" ")
    command = first.split("@", 1)[0].lower()
    return f"{command} {remainder}" if separator else command


async def handle_user_message(message: dict[str, Any]) -> None:
    user = message["from"]
    chat_id = int(message["chat"]["id"])
    text = message_content(message)
    message_id = int(message["message_id"])

    upsert_user(user, text)

    if is_blacklisted(chat_id):
        allowed, _, _ = check_user_rate_limit(chat_id)
        if allowed:
            add_message_log(
                chat_id,
                "user",
                chat_id,
                f"[黑名单拦截] {text}",
                telegram_message_id=message_id,
            )
        return

    command_text = normalize_command_text(message.get("text") or "")
    if command_text == "/start" or command_text.startswith("/start "):
        await send_welcome(chat_id)
        return

    allowed, should_notify, retry_after = check_user_rate_limit(chat_id)
    if not allowed:
        if should_notify:
            add_message_log(
                chat_id,
                "user",
                chat_id,
                f"[频率限制] {text}",
                telegram_message_id=message_id,
            )
            await send_message(
                chat_id,
                f"发送得太快了，请等待约 {retry_after} 秒后再试。",
                parse_mode=None,
            )
        return

    record_user_activity(chat_id)
    add_message_log(
        chat_id,
        "user",
        chat_id,
        text,
        telegram_message_id=message_id,
    )
    await send_message(chat_id, "留言已收到，我看到后会通过 Bot 回复你。")
    await notify_admins(
        user,
        text,
        source_message=message,
        exclude_message_id=message_id,
    )


async def handle_user_edited_message(message: dict[str, Any]) -> None:
    user = message["from"]
    chat_id = int(message["chat"]["id"])
    message_id = int(message["message_id"])
    text = message_content(message)

    upsert_user(user, text)
    if is_blacklisted(chat_id) or text == "/start":
        return

    allowed, should_notify, retry_after = check_user_rate_limit(chat_id)
    if not allowed:
        if should_notify:
            await send_message(
                chat_id,
                f"修改得太频繁了，请等待约 {retry_after} 秒后再试。",
                parse_mode=None,
            )
        return

    record_user_activity(chat_id, increment_unread=False)
    if not update_user_message_log(chat_id, message_id, text):
        add_message_log(
            chat_id,
            "user",
            chat_id,
            text,
            telegram_message_id=message_id,
        )
    await notify_admins(
        user,
        text,
        source_message=message,
        title="用户修改了消息",
        exclude_message_id=message_id,
    )


async def handle_admin_command(admin_id: int, text: str) -> bool:
    if not is_admin(admin_id):
        return False

    owner_only_command = (
        text == "/broadcast"
        or text.startswith("/broadcast ")
        or text == "/broadcast_status"
        or text == "/broadcast_retry"
        or text.startswith("/broadcast_retry ")
        or text == "/audit"
    )
    if owner_only_command and not is_owner(admin_id):
        await send_message(admin_id, "该指令仅限 OWNER_IDS 中的负责人使用。")
        return True

    if text == "/start" or text.startswith("/start "):
        await show_admin_dashboard(admin_id)
        return True

    if text == "/myid":
        role = "负责人" if is_owner(admin_id) else "管理员"
        await send_message(
            admin_id,
            f"你的 Telegram 数字 ID：<code>{admin_id}</code>\n角色：{role}",
        )
        return True

    if text == "/cancel":
        target_chat_id = clear_admin_state(admin_id)
        if target_chat_id:
            add_admin_audit(admin_id, "reply_mode_exit", target_chat_id)
            await send_message(
                admin_id,
                "已退出持续回复模式。",
                reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
            )
        else:
            await send_message(admin_id, "你当前不在持续回复模式。")
        return True

    if text in {"/inbox", "/pending", "/closed"}:
        await show_conversation_queue(admin_id, text[1:])
        return True

    if text == "/users":
        await show_recent_users(admin_id)
        return True

    if (
        text in {"/reply", "/send"}
        or text.startswith("/reply ")
        or text.startswith("/send ")
    ):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
            await send_message(admin_id, f"格式：{parts[0]} 用户ID 内容")
            return True
        target_chat_id = int(parts[1])
        content = parts[2]
        if is_blacklisted(target_chat_id):
            await send_message(admin_id, "这个用户在黑名单中，请先解除黑名单。")
            return True
        owner_admin_id = get_conversation_owner(target_chat_id)
        if owner_admin_id and owner_admin_id != admin_id:
            await send_message(
                admin_id,
                f"这个会话当前由 <code>{owner_admin_id}</code> 接管。请先接管后再发送。",
                reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
            )
            return True
        claim_conversation(target_chat_id, admin_id)
        if not await send_message(target_chat_id, content, parse_mode=None):
            await send_message(admin_id, "发送失败：用户可能已屏蔽 Bot 或 Telegram API 暂时不可用。")
            return True
        add_message_log(target_chat_id, "admin", admin_id, content)
        mark_conversation_replied(target_chat_id)
        add_admin_audit(
            admin_id,
            "message_sent",
            target_chat_id,
            f"command={parts[0]} length={len(content)}",
        )
        await send_message(
            admin_id,
            "<b>消息已发送</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return True

    if text == "/broadcast" or text.startswith("/broadcast "):
        content = text[len("/broadcast") :].strip()
        if not content:
            await send_message(admin_id, "格式：/broadcast 内容")
            return True
        if len(content) > TELEGRAM_TEXT_LIMIT:
            await send_message(admin_id, "群发内容不能超过 4096 个字符。")
            return True
        broadcast_id = create_pending_broadcast(admin_id, content)
        add_admin_audit(
            admin_id,
            "broadcast_created",
            details=f"id={broadcast_id} length={len(content)}",
        )
        total = active_user_count()
        await send_message(
            admin_id,
            "<b>确认群发</b>\n\n"
            f"接收用户：<b>{total}</b>\n\n"
            "<b>发送内容</b>\n"
            f"{html.escape(content[:800])}",
            reply_markup=inline_keyboard(
                [
                    [
                        (
                            "确认群发",
                            f"broadcast_confirm:{broadcast_id}",
                            "primary",
                        ),
                        ("取消", f"broadcast_cancel:{broadcast_id}"),
                    ],
                ]
            ),
        )
        return True

    if text == "/broadcast_status":
        rows = db_fetchall(
            """
            SELECT id, status, total_count, sent_count, failed_count, created_at
            FROM pending_broadcasts
            WHERE admin_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (admin_id,),
        )
        if not rows:
            await send_message(admin_id, "暂无群发记录。")
            return True
        lines = ["<b>最近群发</b>"]
        for index, row in enumerate(rows, start=1):
            status_label = BROADCAST_STATUS_LABELS.get(
                str(row["status"]),
                str(row["status"]),
            )
            lines.append(
                f"<b>{index}. {html.escape(status_label)}</b>\n"
                f"任务：<code>{row['id']}</code>\n"
                f"进度：{row['sent_count']}/{row['total_count']} · "
                f"失败 {row['failed_count']}\n"
                f"创建：{compact_timestamp(row['created_at'])}"
            )
        await send_message(admin_id, "\n\n".join(lines))
        return True

    if text == "/broadcast_retry" or text.startswith("/broadcast_retry "):
        parts = text.split(maxsplit=1)
        broadcast_id = parts[1].strip() if len(parts) == 2 else ""
        if not broadcast_id:
            await send_message(admin_id, "格式：/broadcast_retry 任务ID")
            return True
        status, retry_count = retry_failed_broadcast(broadcast_id)
        if status == "queued":
            if broadcast_wakeup:
                broadcast_wakeup.set()
            add_admin_audit(
                admin_id,
                "broadcast_retry",
                details=f"id={broadcast_id} recipients={retry_count}",
            )
            await send_message(
                admin_id,
                f"已重新排队失败用户：{retry_count} 人。",
            )
        elif status == "missing":
            await send_message(admin_id, "群发任务不存在。")
        elif status == "nothing":
            await send_message(admin_id, "这个群发任务没有可重试的失败用户。")
        else:
            await send_message(admin_id, f"任务当前状态为 {status}，暂时不能重试。")
        return True

    if text == "/audit":
        rows = db_fetchall(
            """
            SELECT admin_id, action, target_chat_id, details, created_at
            FROM admin_audit_logs
            ORDER BY id DESC
            LIMIT 15
            """
        )
        if not rows:
            await send_message(admin_id, "暂无管理员操作记录。")
            return True
        lines = ["<b>最近管理员操作</b>"]
        for index, row in enumerate(rows, start=1):
            target = (
                f" · 用户 <code>{row['target_chat_id']}</code>"
                if row["target_chat_id"] is not None
                else ""
            )
            details = (
                f"\n{escape_html_limited(row['details'], 120)}"
                if row["details"]
                else ""
            )
            action_label = AUDIT_ACTION_LABELS.get(
                str(row["action"]),
                str(row["action"]),
            )
            lines.append(
                f"<b>{index}. {html.escape(action_label)}</b> · "
                f"{compact_timestamp(row['created_at'])}\n"
                f"管理员 <code>{row['admin_id']}</code>{target}{details}"
            )
        await send_message(admin_id, "\n\n".join(lines))
        return True

    if text == "/takeover" or text.startswith("/takeover "):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await send_message(admin_id, "格式：/takeover 用户ID")
            return True
        target_chat_id = int(parts[1])
        claim_conversation(target_chat_id, admin_id)
        set_admin_state(admin_id, target_chat_id)
        add_admin_audit(admin_id, "conversation_takeover", target_chat_id)
        await send_message(
            admin_id,
            "<b>会话已接管</b>\n\n"
            f"目标用户：<code>{target_chat_id}</code>\n"
            "接下来发送的消息会转发给该用户。",
            reply_markup=exit_reply_keyboard(target_chat_id),
        )
        return True

    if text == "/close" or text.startswith("/close "):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await send_message(admin_id, "格式：/close 用户ID")
            return True
        target_chat_id = int(parts[1])
        close_conversation(target_chat_id)
        add_admin_audit(admin_id, "conversation_resolved", target_chat_id)
        await send_message(
            admin_id,
            "<b>已标记处理</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return True

    if text == "/blacklist" or text.startswith("/blacklist "):
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await send_message(admin_id, "格式：/blacklist 用户ID 可选原因")
            return True
        reason = parts[2] if len(parts) >= 3 else ""
        target_chat_id = int(parts[1])
        blacklist_user(target_chat_id, admin_id, reason)
        close_conversation(target_chat_id)
        add_admin_audit(
            admin_id,
            "blacklist_add",
            target_chat_id,
            f"reason={reason}" if reason else "",
        )
        await send_message(
            admin_id,
            "<b>已加入黑名单</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(
                target_chat_id,
                viewer_admin_id=admin_id,
            ),
        )
        return True

    if text == "/unblacklist" or text.startswith("/unblacklist "):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await send_message(admin_id, "格式：/unblacklist 用户ID")
            return True
        target_chat_id = int(parts[1])
        unblacklist_user(target_chat_id)
        add_admin_audit(admin_id, "blacklist_remove", target_chat_id)
        await send_message(
            admin_id,
            "<b>已解除黑名单</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(
                target_chat_id,
                viewer_admin_id=admin_id,
            ),
        )
        return True

    if text == "/blacklist_list":
        rows = db_fetchall(
            """
            SELECT chat_id, reason, created_at
            FROM blacklists
            ORDER BY created_at DESC
            LIMIT 20
            """
        )
        if not rows:
            await send_message(admin_id, "黑名单为空。")
            return True
        lines = ["黑名单："]
        for row in rows:
            lines.append(
                f"<code>{row['chat_id']}</code> "
                f"{html.escape(truncate_text(row['reason'] or '', 80))} {row['created_at']}"
            )
        await send_message(admin_id, "\n".join(lines))
        return True

    return False


async def handle_admin_message(message: dict[str, Any]) -> None:
    admin_id = int(message["from"]["id"])
    if not is_admin(admin_id):
        return

    text = normalize_command_text(message.get("text") or message.get("caption") or "")
    content = message_content(message)

    if await handle_admin_command(admin_id, text):
        return

    if text.startswith("/"):
        await send_message(
            admin_id,
            "<b>未知管理员指令</b>\n\n请从命令菜单选择，或返回工作台。",
            reply_markup=admin_dashboard_keyboard(),
        )
        return

    target_chat_id = get_admin_state(admin_id)
    if not target_chat_id:
        await send_message(
            admin_id,
            "<b>尚未选择回复对象</b>\n\n"
            "请从待处理队列选择用户，或使用 /reply 用户ID 内容。",
            reply_markup=admin_dashboard_keyboard(),
        )
        return

    if is_blacklisted(target_chat_id):
        await send_message(
            admin_id,
            "这个用户在黑名单中。请先解除黑名单后再回复。",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    conversation = get_conversation(target_chat_id)
    if conversation and conversation["status"] == "closed":
        await send_message(
            admin_id,
            "这个会话已经关闭。如需继续，请先接管会话。",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    owner_admin_id = get_conversation_owner(target_chat_id)
    if owner_admin_id and owner_admin_id != admin_id:
        await send_message(
            admin_id,
            f"这个会话当前由 <code>{owner_admin_id}</code> 接管。点击接管后再回复。",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    if not owner_admin_id:
        claim_conversation(target_chat_id, admin_id)

    if not await copy_message(
        target_chat_id,
        int(message["chat"]["id"]),
        int(message["message_id"]),
    ):
        await send_message(admin_id, "发送失败：用户可能已屏蔽 Bot 或 Telegram API 暂时不可用。")
        return
    add_message_log(target_chat_id, "admin", admin_id, content)
    mark_conversation_replied(target_chat_id)
    add_admin_audit(
        admin_id,
        "message_sent",
        target_chat_id,
        f"type={message_kind(message)}",
    )
    await send_message(
        admin_id,
        "<b>消息已发送</b>\n\n"
        f"目标用户：<code>{target_chat_id}</code>\n"
        "回复模式仍然开启。",
        reply_markup=exit_reply_keyboard(target_chat_id),
    )


async def handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback["id"]
    from_user = callback["from"]
    admin_id = int(from_user["id"])
    data = callback.get("data", "")

    if not is_private_callback(callback):
        await answer_callback_query(callback_id, "请在私聊中使用")
        return

    if data in {"user_help", "user_guide"}:
        chat_id = int(callback["message"]["chat"]["id"])
        if is_blacklisted(chat_id):
            await answer_callback_query(callback_id, "当前不可用")
            return
        await answer_callback_query(callback_id)
        if data == "user_help":
            text = "直接在当前对话发送消息即可。消息会通过 Bot 转交，无需私聊其他账号。"
        else:
            text = "支持文字、图片、文件、语音和视频。发送后请等待回复，避免短时间重复发送。"
        await send_message(chat_id, text)
        return

    if not is_admin(admin_id):
        await answer_callback_query(callback_id, "无权限")
        return

    if ":" not in data:
        await answer_callback_query(callback_id)
        return

    action, raw_chat_id = data.split(":", 1)
    if action == "admin":
        if raw_chat_id == "dashboard":
            await answer_callback_query(callback_id)
            await show_admin_dashboard(admin_id, callback)
        elif raw_chat_id == "users":
            await answer_callback_query(callback_id)
            await show_recent_users(admin_id, callback)
        else:
            await answer_callback_query(callback_id, "未知页面")
        return

    if action == "queue":
        if raw_chat_id not in QUEUE_LABELS:
            await answer_callback_query(callback_id, "未知队列")
            return
        await answer_callback_query(callback_id)
        await show_conversation_queue(admin_id, raw_chat_id, callback)
        return

    if action in {"broadcast_confirm", "broadcast_cancel", "broadcast_retry"}:
        if not is_owner(admin_id):
            await answer_callback_query(callback_id, "仅负责人可操作群发")
            return
        if action == "broadcast_retry":
            status, retry_count = retry_failed_broadcast(raw_chat_id)
            if status == "queued":
                if broadcast_wakeup:
                    broadcast_wakeup.set()
                add_admin_audit(
                    admin_id,
                    "broadcast_retry",
                    details=f"id={raw_chat_id} recipients={retry_count}",
                )
                await answer_callback_query(callback_id, "失败用户已重新排队")
                await send_message(
                    admin_id,
                    f"已重新排队失败用户：{retry_count} 人。",
                )
            elif status == "nothing":
                await answer_callback_query(callback_id, "没有可重试用户")
            elif status == "missing":
                await answer_callback_query(callback_id, "群发不存在")
            else:
                await answer_callback_query(callback_id, "任务暂时不能重试")
            return

        if action == "broadcast_cancel":
            status = cancel_pending_broadcast(raw_chat_id, admin_id)
            if status == "canceled":
                add_admin_audit(
                    admin_id,
                    "broadcast_canceled",
                    details=f"id={raw_chat_id}",
                )
                await answer_callback_query(callback_id, "已取消")
                await send_message(admin_id, "已取消群发。")
            elif status == "forbidden":
                await answer_callback_query(callback_id, "只能由创建者取消")
            elif status == "missing":
                await answer_callback_query(callback_id, "群发不存在")
            else:
                await answer_callback_query(callback_id, "群发已开始，无法取消")
            return

        status, total = queue_broadcast(raw_chat_id, admin_id)
        if status == "queued":
            if broadcast_wakeup:
                broadcast_wakeup.set()
            add_admin_audit(
                admin_id,
                "broadcast_confirmed",
                details=f"id={raw_chat_id} recipients={total}",
            )
            await answer_callback_query(callback_id, "已加入发送队列")
            await send_message(
                admin_id,
                f"群发任务已加入队列，接收用户 {total}。\n"
                "完成后会自动通知；可用 /broadcast_status 查看进度。",
            )
        elif status == "forbidden":
            await answer_callback_query(callback_id, "只能由创建者确认")
        elif status == "missing":
            await answer_callback_query(callback_id, "群发不存在")
        else:
            await answer_callback_query(callback_id, "群发已处理")
        return

    if not raw_chat_id.lstrip("-").isdigit():
        await answer_callback_query(callback_id)
        return

    target_chat_id = int(raw_chat_id)

    if action == "reply":
        owner_admin_id = get_conversation_owner(target_chat_id)
        if owner_admin_id and owner_admin_id != admin_id:
            await answer_callback_query(callback_id, "会话已被其他管理员接管")
            await send_message(
                admin_id,
                f"这个会话当前由 <code>{owner_admin_id}</code> 接管。需要回复的话请先接管。",
                reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
            )
            return
        claim_conversation(target_chat_id, admin_id)
        set_admin_state(admin_id, target_chat_id)
        add_admin_audit(admin_id, "reply_mode_enter", target_chat_id)
        await answer_callback_query(callback_id, "已进入回复模式")
        await send_message(
            admin_id,
            "<b>回复模式已开启</b>\n\n"
            f"目标用户：<code>{target_chat_id}</code>\n"
            "接下来发送的消息会转发给该用户。",
            reply_markup=exit_reply_keyboard(target_chat_id),
        )
        return

    if action == "cancel":
        cleared_target = clear_admin_state(
            admin_id, expected_target_chat_id=target_chat_id
        )
        if cleared_target is None:
            await answer_callback_query(callback_id, "这不是当前回复会话")
        else:
            add_admin_audit(admin_id, "reply_mode_exit", target_chat_id)
            await answer_callback_query(callback_id, "已退出")
            await send_message(
                admin_id,
                "已退出持续回复模式。",
                reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
            )
        return

    if action == "takeover":
        claim_conversation(target_chat_id, admin_id)
        set_admin_state(admin_id, target_chat_id)
        add_admin_audit(admin_id, "conversation_takeover", target_chat_id)
        await answer_callback_query(callback_id, "已接管")
        await send_message(
            admin_id,
            "<b>会话已接管</b>\n\n"
            f"目标用户：<code>{target_chat_id}</code>\n"
            "接下来发送的消息会转发给该用户。",
            reply_markup=exit_reply_keyboard(target_chat_id),
        )
        return

    if action in {"close", "resolve"}:
        close_conversation(target_chat_id)
        add_admin_audit(admin_id, "conversation_resolved", target_chat_id)
        await answer_callback_query(callback_id, "已标记处理")
        await send_message(
            admin_id,
            "<b>已标记处理</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    if action == "reopen":
        reopen_conversation(target_chat_id)
        add_admin_audit(admin_id, "conversation_reopened", target_chat_id)
        await answer_callback_query(callback_id, "已重新打开")
        await send_message(
            admin_id,
            "<b>会话已重新打开</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    if action == "detail":
        await answer_callback_query(callback_id)
        await send_message(
            admin_id,
            format_user_detail(target_chat_id),
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    if action == "blacklist":
        await answer_callback_query(callback_id)
        await send_message(
            admin_id,
            "<b>确认加入黑名单</b>\n\n"
            f"用户：<code>{target_chat_id}</code>\n"
            "该用户的新留言将被拦截，不再通知管理员。",
            reply_markup=inline_keyboard(
                [
                    [
                        (
                            "确认加入",
                            f"blacklist_confirm:{target_chat_id}",
                            "danger",
                        ),
                        ("取消", f"blacklist_cancel:{target_chat_id}"),
                    ]
                ]
            ),
        )
        return

    if action == "blacklist_cancel":
        await answer_callback_query(callback_id, "已取消")
        return

    if action == "blacklist_confirm":
        blacklist_user(target_chat_id, admin_id, "管理员按钮添加")
        close_conversation(target_chat_id)
        add_admin_audit(
            admin_id,
            "blacklist_add",
            target_chat_id,
            "reason=button",
        )
        await answer_callback_query(callback_id, "已加入黑名单")
        await send_message(
            admin_id,
            "<b>已加入黑名单</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    if action == "unblacklist":
        unblacklist_user(target_chat_id)
        add_admin_audit(admin_id, "blacklist_remove", target_chat_id)
        await answer_callback_query(callback_id, "已解除黑名单")
        await send_message(
            admin_id,
            "<b>已解除黑名单</b>\n\n"
            f"用户：<code>{target_chat_id}</code>",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    await answer_callback_query(callback_id)


@app.get("/healthz")
async def healthz() -> dict[str, str | bool]:
    try:
        db_fetchone("SELECT 1 AS ok")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="db unavailable") from exc
    if broadcast_worker_task is None or broadcast_worker_task.done():
        raise HTTPException(status_code=503, detail="broadcast worker unavailable")
    return {"ok": True, "db": "ok", "broadcast_worker": "ok"}


@app.post("/tg/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if (
        x_telegram_bot_api_secret_token is None
        or not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET)
    ):
        raise HTTPException(status_code=403, detail="invalid secret token")

    update = await request.json()
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="invalid update")

    update_id = update.get("update_id")
    claimed = False
    if isinstance(update_id, int):
        if not claim_update(update_id):
            return {"ok": True}
        claimed = True

    try:
        if "callback_query" in update:
            await handle_callback(update["callback_query"])
        else:
            edited = "edited_message" in update
            message = update.get("edited_message") if edited else update.get("message")
            if message and "from" in message and is_private_chat_message(message):
                from_id = int(message["from"]["id"])
                if is_admin(from_id):
                    if not edited:
                        await handle_admin_message(message)
                elif edited:
                    await handle_user_edited_message(message)
                else:
                    await handle_user_message(message)

        if claimed:
            finish_update(update_id)
        return {"ok": True}
    except Exception as exc:
        if claimed:
            with contextlib.suppress(Exception):
                fail_update(update_id, str(exc))
        logger.exception("Webhook update processing failed update_id=%s", update_id)
        raise HTTPException(status_code=500, detail="update processing failed") from exc
