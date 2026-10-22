import asyncio
import contextlib
import hmac
import html
import json
import logging
import os
import sqlite3
import time
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from tg_bot.config import (
    Settings,
    load_display_timezone,
    load_settings,
)
from tg_bot import database, keyboards
from tg_bot.keyboards import ButtonSpec
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import access as access_repository
from tg_bot.repositories import conversations as conversation_repository
from tg_bot.repositories import deliveries as delivery_repository
from tg_bot.repositories import inbound as inbound_repository
from tg_bot.repositories import messages as message_repository
from tg_bot.repositories import moderation as moderation_repository
from tg_bot.repositories import updates as update_repository
from tg_bot.repositories import users as user_repository
from tg_bot.services import admin_delivery as admin_delivery_service
from tg_bot.services import moderation as moderation_service
from tg_bot.services.ai_settings import AISettingsController
from tg_bot.services.moderation_ui import ModerationInbox
from tg_bot.telegram import TelegramAPIError, request as telegram_request
from tg_bot.text import escape_html_limited, html_to_plain_text, truncate_text, utf16_length

if os.name == "posix":
    os.umask(0o077)

BASE_DIR = Path(__file__).resolve().parent
SETTINGS = load_settings(BASE_DIR)

BOT_TOKEN = SETTINGS.bot_token
WEBHOOK_SECRET = SETTINGS.webhook_secret
ADMIN_IDS = set(SETTINGS.admin_ids)
OWNER_IDS = set(SETTINGS.owner_ids)
DB_BACKUP_ENABLED = SETTINGS.db_backup_enabled
DB_BACKUP_INTERVAL_SECONDS = SETTINGS.db_backup_interval_seconds
DB_BACKUP_KEEP = SETTINGS.db_backup_keep
USER_RATE_LIMIT_COUNT = SETTINGS.user_rate_limit_count
USER_RATE_LIMIT_WINDOW_SECONDS = SETTINGS.user_rate_limit_window_seconds
USER_RATE_LIMIT_COOLDOWN_SECONDS = SETTINGS.user_rate_limit_cooldown_seconds
MESSAGE_RETENTION_DAYS = SETTINGS.message_retention_days
UPDATE_PROCESSING_TIMEOUT_SECONDS = SETTINGS.update_processing_timeout_seconds
ADMIN_REPLY_STATE_TTL_SECONDS = SETTINGS.admin_reply_state_ttl_seconds
TELEGRAM_INLINE_RETRY_MAX_SECONDS = SETTINGS.telegram_inline_retry_max_seconds
WEBHOOK_MAX_BODY_BYTES = 1024 * 1024
AI_MODERATION_ENABLED = SETTINGS.ai_moderation_enabled
DISPLAY_TIMEZONE_NAME = SETTINGS.display_timezone_name
DISPLAY_TIMEZONE = SETTINGS.display_timezone
LOG_LEVEL = SETTINGS.log_level
DB_PATH = SETTINGS.db_path
DB_BACKUP_DIR = SETTINGS.db_backup_dir
API_BASE = SETTINGS.api_base
APP_VERSION = SETTINGS.app_version
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
admin_delivery_worker_task: asyncio.Task[None] | None = None
admin_delivery_wakeup: asyncio.Event | None = None
moderation_worker_task: asyncio.Task[None] | None = None
moderation_wakeup: asyncio.Event | None = None
ai_settings_controller: AISettingsController | None = None
admin_ui_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global telegram_client, backup_task
    global admin_delivery_worker_task, admin_delivery_wakeup
    global moderation_worker_task, moderation_wakeup, ai_settings_controller

    init_db()
    admin_ui_locks.clear()
    ai_settings_controller = None
    recover_interrupted_work()
    moderation_repository.recover(DB_PATH)
    purge_expired_data()
    try:
        await asyncio.to_thread(backup_database)
    except Exception:
        logger.exception("Initial database backup failed")

    telegram_client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    admin_delivery_wakeup = asyncio.Event()
    moderation_wakeup = asyncio.Event()
    moderation_worker_task = asyncio.create_task(
        moderation_service.run_worker(
            DB_PATH, lambda: SETTINGS, ADMIN_IDS, moderation_wakeup,
            admin_delivery_wakeup, notify_moderation_pending, logger,
        ), name="moderation-worker",
    )
    backup_task = asyncio.create_task(periodic_db_backup(), name="database-backup")
    admin_delivery_worker_task = asyncio.create_task(
        periodic_admin_delivery_worker(), name="admin-delivery-worker"
    )

    try:
        yield
    finally:
        for task in (
            moderation_worker_task,
            admin_delivery_worker_task,
            backup_task,
        ):
            if task:
                task.cancel()
        for task in (
            moderation_worker_task,
            admin_delivery_worker_task,
            backup_task,
        ):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if telegram_client:
            await telegram_client.aclose()
        telegram_client = None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

WELCOME_TEXT = (
    "<b>统一留言聊天入口</b>\n\n"
    "你好，欢迎留言。想聊的事、想分享的内容，都可以直接发在这里。\n\n"
    "我看到后会通过这个 Bot 回复你，无需另行私聊。文字和媒体都可以发送，"
    "还没收到回复时，也可以继续补充。"
)
QUEUE_LABELS = {"inbox": "未读", "all": "全部"}
ADMIN_PAGE_SIZE = 10


# =========================
# 数据库
# =========================

def enforce_private_mode(path: Path, mode: int) -> None:
    database.enforce_private_mode(path, mode)


def secure_database_files() -> None:
    database.secure_database_files(DB_PATH)


def init_db() -> None:
    database.initialize(DB_PATH)


def db_connect() -> AbstractContextManager[sqlite3.Connection]:
    return database.connect(DB_PATH)


def db_execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    database.execute(DB_PATH, sql, params)


def db_fetchone(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return database.fetchone(DB_PATH, sql, params)


def db_fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return database.fetchall(DB_PATH, sql, params)


def claim_update(update_id: int) -> str:
    return update_repository.claim(
        DB_PATH,
        update_id,
        UPDATE_PROCESSING_TIMEOUT_SECONDS,
    )


def recover_interrupted_work() -> None:
    update_repository.recover_interrupted(DB_PATH)


def finish_update(update_id: int) -> None:
    update_repository.finish(DB_PATH, update_id)


def fail_update(update_id: int, error: str) -> None:
    update_repository.fail(DB_PATH, update_id, error)


def purge_expired_data() -> None:
    with db_connect() as conn:
        if MESSAGE_RETENTION_DAYS > 0:
            conn.execute(
                "DELETE FROM message_logs WHERE created_at < datetime('now', ?)",
                (f"-{MESSAGE_RETENTION_DAYS} days",),
            )
            conn.execute(
                "DELETE FROM message_links WHERE created_at < datetime('now', ?)",
                (f"-{MESSAGE_RETENTION_DAYS} days",),
            )
            conn.execute(
                """
                DELETE FROM admin_reply_deliveries
                WHERE status IN ('sent', 'failed', 'unknown', 'canceled')
                  AND updated_at < datetime('now', ?)
                """,
                (f"-{MESSAGE_RETENTION_DAYS} days",),
            )
            conn.execute(
                """
                DELETE FROM admin_deliveries
                WHERE status IN ('sent', 'failed', 'unknown', 'canceled')
                  AND updated_at < datetime('now', ?)
                """,
                (f"-{MESSAGE_RETENTION_DAYS} days",),
            )
            conn.execute(
                """
                DELETE FROM inbound_events
                WHERE created_at < datetime('now', ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM admin_deliveries
                      WHERE admin_deliveries.update_id = inbound_events.update_id
                  )
                """,
                (f"-{MESSAGE_RETENTION_DAYS} days",),
            )
            conn.execute(
                "DELETE FROM moderation_jobs WHERE status IN ('approved','blocked','superseded','dismissed') "
                "AND updated_at < datetime('now', ?) "
                "AND NOT EXISTS (SELECT 1 FROM admin_deliveries d WHERE d.update_id=moderation_jobs.update_id)",
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
            "DELETE FROM user_rate_limits WHERE window_started_at < ?",
            (int(time.time()) - 7 * 86400,),
        )
        conn.execute("DELETE FROM moderation_usage WHERE day < date('now', '-30 days')")
        conn.execute(
            "DELETE FROM moderation_jobs WHERE status IN ('blocked','superseded','dismissed') "
            "AND updated_at < datetime('now','-30 days') "
            "AND update_id NOT IN (SELECT update_id FROM admin_deliveries)"
        )
        conn.commit()


def backup_database() -> Path | None:
    if not DB_BACKUP_ENABLED or not DB_PATH.exists():
        return None

    DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    enforce_private_mode(DB_BACKUP_DIR, 0o700)
    backup_path = DB_BACKUP_DIR / f"bot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"

    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as source:
        with contextlib.closing(sqlite3.connect(backup_path)) as backup:
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
ADMIN_DELIVERY_MAX_ATTEMPTS = 5


def telegram_send_result(data: dict[str, Any]) -> TelegramSendResult:
    result = data.get("result")
    if not isinstance(result, dict):
        return TelegramSendResult(ok=True)
    raw_message_id = result.get("message_id")
    raw_thread_id = result.get("message_thread_id")
    return TelegramSendResult(
        ok=True,
        message_id=(
            int(raw_message_id)
            if isinstance(raw_message_id, int) and not isinstance(raw_message_id, bool)
            else None
        ),
        message_thread_id=(
            int(raw_thread_id)
            if isinstance(raw_thread_id, int) and not isinstance(raw_thread_id, bool)
            else None
        ),
    )


def failed_send_result(exc: Exception) -> TelegramSendResult:
    if isinstance(exc, TelegramAPIError):
        return TelegramSendResult(
            ok=False,
            status_code=exc.status_code,
            description=exc.description,
            retry_after=exc.retry_after,
        )
    return TelegramSendResult(ok=False, description=type(exc).__name__)


def delivery_failure_message(result: Any) -> str:
    if isinstance(result, TelegramSendResult):
        if result.status_code == 403:
            return "发送失败：用户可能已经屏蔽 Bot。"
        if result.status_code == 400:
            return "发送失败：Telegram 无法找到该用户或不接受这类消息。"
        if result.retry_after is not None:
            return f"发送受限：请等待约 {result.retry_after} 秒后重试。"
    return "发送失败：Telegram API 或网络暂时不可用，请稍后重试。"


async def tg(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await telegram_request(telegram_client, API_BASE, method, payload)


async def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
    rate_limit_retries: int = 1,
    rate_limit_max_wait_seconds: int | None = TELEGRAM_INLINE_RETRY_MAX_SECONDS,
) -> TelegramSendResult:
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
            data = await tg("sendMessage", payload)
            return telegram_send_result(data)
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
            return failed_send_result(exc)
        except Exception as exc:
            logger.warning("sendMessage failed chat_id=%s error=%s", chat_id, exc)
            return failed_send_result(exc)


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
) -> TelegramSendResult:
    rate_limit_attempt = 0
    while True:
        try:
            data = await tg(
                "copyMessage",
                {
                    "chat_id": to_chat_id,
                    "from_chat_id": from_chat_id,
                    "message_id": message_id,
                },
            )
            return telegram_send_result(data)
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
            return failed_send_result(exc)
        except Exception as exc:
            logger.warning(
                "copyMessage failed to_chat_id=%s from_chat_id=%s "
                "message_id=%s error=%s",
                to_chat_id,
                from_chat_id,
                message_id,
                exc,
            )
            return failed_send_result(exc)


def inline_keyboard(rows: list[list[ButtonSpec]]) -> dict[str, Any]:
    return keyboards.inline_keyboard(rows)


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
    user_repository.add_admin_audit(
        DB_PATH,
        admin_id,
        action,
        target_chat_id,
        details,
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
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return normalized.replace("T", " ")[:16]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def row_user_label(row: sqlite3.Row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    name = " ".join(
        value for value in (row["first_name"], row["last_name"]) if value
    ).strip()
    return name or "未命名联系人"


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
    if "animation" in message:
        return f"[动图] {caption}".strip()
    if "video_note" in message:
        return "[视频消息]"
    if "sticker" in message:
        emoji = str(message.get("sticker", {}).get("emoji") or "")
        return f"[贴纸] {emoji}".strip()
    if "location" in message:
        return "[位置]"
    if "contact" in message:
        contact = message.get("contact") or {}
        name = " ".join(
            value
            for value in (contact.get("first_name"), contact.get("last_name"))
            if value
        ).strip()
        return f"[联系人] {name}".strip()
    return "[非文本消息]"


def message_kind(message: dict[str, Any]) -> str:
    for kind in (
        "text",
        "photo",
        "document",
        "voice",
        "video",
        "audio",
        "animation",
        "video_note",
        "sticker",
        "location",
        "contact",
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
    user_repository.upsert(DB_PATH, user, text)


def add_message_log(
    chat_id: int,
    sender_type: str,
    sender_id: int,
    text: str,
    telegram_message_id: int | None = None,
) -> None:
    user_repository.add_message_log(
        DB_PATH,
        chat_id,
        sender_type,
        sender_id,
        text,
        telegram_message_id,
    )


def add_message_link(
    user_chat_id: int,
    user_message_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    direction: str,
    link_kind: str,
    admin_message_thread_id: int | None = None,
) -> None:
    message_repository.add_link(
        DB_PATH,
        user_chat_id,
        user_message_id,
        admin_chat_id,
        admin_message_id,
        direction,
        link_kind,
        admin_message_thread_id,
    )


def add_message_link_in_connection(
    conn: sqlite3.Connection,
    user_chat_id: int,
    user_message_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    direction: str,
    link_kind: str,
    admin_message_thread_id: int | None = None,
) -> None:
    message_repository.add_link_in_connection(
        conn,
        user_chat_id,
        user_message_id,
        admin_chat_id,
        admin_message_id,
        direction,
        link_kind,
        admin_message_thread_id,
    )


def get_message_link(
    admin_chat_id: int,
    admin_message_id: int,
) -> sqlite3.Row | None:
    return message_repository.find_link(DB_PATH, admin_chat_id, admin_message_id)


def resolve_admin_message_target(
    admin_id: int,
    message: dict[str, Any],
) -> tuple[int | None, str]:
    replied_message = message.get("reply_to_message")
    if isinstance(replied_message, dict):
        replied_message_id = replied_message.get("message_id")
        admin_chat_id = message.get("chat", {}).get("id")
        if (
            not isinstance(replied_message_id, int)
            or isinstance(replied_message_id, bool)
            or not isinstance(admin_chat_id, int)
            or isinstance(admin_chat_id, bool)
        ):
            return None, "unmapped_reply"
        link = get_message_link(admin_chat_id, replied_message_id)
        if not link:
            return None, "unmapped_reply"
        return int(link["user_chat_id"]), "reply"
    return get_admin_state(admin_id), "state"


def update_user_message_log(chat_id: int, message_id: int, text: str) -> bool:
    return user_repository.update_user_message_log(
        DB_PATH,
        chat_id,
        message_id,
        text,
    )


def get_history(
    chat_id: int,
    limit: int = 5,
    exclude_message_id: int | None = None,
) -> list[sqlite3.Row]:
    return user_repository.get_history(
        DB_PATH,
        chat_id,
        limit,
        exclude_message_id,
    )


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
        line_length = utf16_length(line)
        if lines and used_chars + line_length + 2 > max_chars:
            break
        lines.append(line)
        used_chars += line_length + 2
    return "\n\n".join(lines)


def check_user_rate_limit(chat_id: int) -> tuple[bool, bool, int]:
    return access_repository.check_rate_limit(
        DB_PATH,
        chat_id,
        USER_RATE_LIMIT_COUNT,
        USER_RATE_LIMIT_WINDOW_SECONDS,
        USER_RATE_LIMIT_COOLDOWN_SECONDS,
    )


def is_blacklisted(chat_id: int) -> bool:
    return access_repository.is_blacklisted(DB_PATH, chat_id)


def blacklist_user(chat_id: int, admin_id: int, reason: str = "") -> None:
    access_repository.blacklist(DB_PATH, chat_id, admin_id, reason)


def unblacklist_user(chat_id: int) -> None:
    access_repository.unblacklist(DB_PATH, chat_id)


def set_admin_state(admin_id: int, target_chat_id: int) -> None:
    conversation_repository.set_admin_state(DB_PATH, admin_id, target_chat_id)


def get_admin_state(admin_id: int) -> int | None:
    return conversation_repository.get_admin_state(
        DB_PATH,
        admin_id,
        ADMIN_REPLY_STATE_TTL_SECONDS,
    )


def clear_admin_state(
    admin_id: int, expected_target_chat_id: int | None = None
) -> int | None:
    return conversation_repository.clear_admin_state(
        DB_PATH,
        admin_id,
        ADMIN_REPLY_STATE_TTL_SECONDS,
        expected_target_chat_id,
    )


def clear_admin_states_for_target(chat_id: int) -> None:
    conversation_repository.clear_admin_states_for_target(DB_PATH, chat_id)


def record_user_activity(chat_id: int, increment_unread: bool = True) -> None:
    conversation_repository.record_user_activity(
        DB_PATH,
        chat_id,
        increment_unread,
    )


def get_conversation(chat_id: int) -> sqlite3.Row | None:
    return conversation_repository.get(DB_PATH, chat_id)


def conversation_queue_filter(
    queue_name: str,
) -> tuple[str, str, tuple[Any, ...]]:
    return conversation_repository.queue_filter(
        queue_name,
    )


def count_conversation_queue(queue_name: str) -> int:
    return conversation_repository.count_queue(
        DB_PATH,
        queue_name,
    )


def get_conversation_queue(
    queue_name: str,
    limit: int = ADMIN_PAGE_SIZE,
    offset: int = 0,
) -> list[sqlite3.Row]:
    return conversation_repository.list_queue(
        DB_PATH,
        queue_name,
        limit,
        offset,
    )


def paginate(
    page: int,
    total_count: int,
    page_size: int = ADMIN_PAGE_SIZE,
) -> tuple[int, int, int]:
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    normalized_page = min(max(page, 1), total_pages)
    offset = (normalized_page - 1) * page_size
    return normalized_page, total_pages, offset


def parse_callback_page(raw_page: str | None) -> int | None:
    if raw_page is None:
        return 1
    if (
        not raw_page
        or len(raw_page) > 9
        or not raw_page.isascii()
        or not raw_page.isdigit()
    ):
        return None
    page = int(raw_page)
    return page if page >= 1 else None


def get_queue_counts() -> dict[str, int]:
    return conversation_repository.queue_counts(
        DB_PATH,
    )


def chat_label_html(chat_id: int) -> str:
    user = user_repository.get(DB_PATH, chat_id)
    return escape_html_limited(row_user_label(user) if user else "未命名联系人", 90)


def format_admin_dashboard(admin_id: int) -> str:
    counts = get_queue_counts()
    target = get_admin_state(admin_id)
    held = db_fetchone("SELECT COUNT(*) AS count FROM moderation_jobs WHERE status='held'")["count"]
    state = f"正在回复：{chat_label_html(target)}" if target is not None else "当前未选择回复对象"
    return (
        "<b>聊天中转</b>\n统一接收留言，在这里继续聊天。\n\n"
        f"<b>消息概览</b>\n未读聊天：{counts['inbox']}　全部聊天：{counts['all']}\n"
        f"垃圾箱暂存：{held}\n\n"
        f"<b>防广告</b>\n当前模式：{SETTINGS.moderation_mode_label}\n\n"
        f"<b>回复状态</b>\n{state}"
    )


def format_conversation_queue(
    queue_name: str, rows: list[sqlite3.Row], page: int = 1,
    total_pages: int = 1, total_count: int | None = None,
) -> str:
    total = len(rows) if total_count is None else total_count
    lines = [f"<b>聊天 · {QUEUE_LABELS[queue_name]}</b>",
             f"共 {total} 个聊天 · 第 {page}/{total_pages} 页\n时间：{DISPLAY_TIMEZONE_NAME}"]
    if not rows:
        lines.append("没有未读消息。" if queue_name == "inbox" else "还没有聊天记录。")
    for index, row in enumerate(rows, start=(page - 1) * ADMIN_PAGE_SIZE + 1):
        unread = int(row["unread_count"] or 0)
        state = "已拉黑" if is_blacklisted(int(row["chat_id"])) else (f"未读 {unread}" if unread else "已读")
        lines.append(
            f"<b>{index}. {escape_html_limited(row_user_label(row), 70)}</b>\n"
            f"{state} · {compact_timestamp(row['last_user_message_at'])}\n"
            f"{escape_html_limited(row['last_message'] or '无文字内容', 150)}"
        )
    return "\n\n".join(lines)


def format_user_detail(chat_id: int) -> str:
    user = user_repository.get(DB_PATH, chat_id)
    if not user:
        return "<b>用户详情</b>\n\n未找到该联系人。"
    username = f"@{escape_html_limited(str(user['username']), 80)}" if user["username"] else "未设置"
    conversation = get_conversation(chat_id)
    status = "已拉黑" if is_blacklisted(chat_id) else (
        "已从聊天列表移除" if conversation and conversation["archived_at"] else "正常接收留言"
    )
    return (
        f"<b>{chat_label_html(chat_id)}</b>\n"
        f"ID：<code>{chat_id}</code>\n用户名：{username}\n"
        f"状态：{status}\n"
        f"最近留言：{compact_timestamp(conversation['last_user_message_at']) if conversation else '暂无'}\n\n"
        f"<b>聊天记录</b>\n{format_history(chat_id, limit=10, max_chars=2400)}"
    )


def admin_user_keyboard(chat_id: int, viewer_admin_id: int | None = None, *, read_through_seq: int | None = None) -> dict[str, Any]:
    conversation = get_conversation(chat_id)
    return keyboards.admin_user_keyboard(
        chat_id, blacklisted=is_blacklisted(chat_id),
        active_reply=viewer_admin_id is not None and get_admin_state(viewer_admin_id) == chat_id,
        read_through_seq=(read_through_seq if read_through_seq is not None
                          else int(conversation["last_inbound_seq"]) if conversation else 0),
    )


def admin_dashboard_keyboard(admin_id: int | None = None) -> dict[str, Any]:
    counts = get_queue_counts()
    row = db_fetchone("SELECT COUNT(*) AS count FROM moderation_jobs WHERE status='held'")
    counts["moderation"] = int(row["count"]) if row else 0
    return keyboards.admin_dashboard_keyboard(counts)


def pagination_navigation_row(callback_prefix: str, page: int, total_pages: int) -> list[ButtonSpec]:
    return keyboards.pagination_navigation_row(callback_prefix, page, total_pages)


def conversation_queue_keyboard(
    rows: list[sqlite3.Row], queue_name: str, viewer_admin_id: int | None = None,
    page: int = 1, total_pages: int = 1,
) -> dict[str, Any]:
    return keyboards.conversation_queue_keyboard(
        rows, queue_name, viewer_admin_id=viewer_admin_id,
        page=page, total_pages=total_pages, page_size=ADMIN_PAGE_SIZE, counts=get_queue_counts(),
    )


def exit_reply_keyboard(chat_id: int) -> dict[str, Any]:
    return keyboards.exit_reply_keyboard(chat_id)


def welcome_keyboard() -> dict[str, Any]:
    return keyboards.welcome_keyboard()


async def present_admin_view(
    admin_id: int, text: str, reply_markup: dict[str, Any],
    callback: dict[str, Any] | None = None,
) -> bool:
    if callback:
        message = callback.get("message") or {}
        if (message.get("chat", {}).get("id") == admin_id and isinstance(message.get("message_id"), int)
                and get_message_link(admin_id, message["message_id"]) is None):
            if await edit_message_text(admin_id, message["message_id"], text, reply_markup=reply_markup):
                return True
    return bool(await send_message(admin_id, text, reply_markup=reply_markup))


async def show_admin_dashboard(admin_id: int, callback: dict[str, Any] | None = None) -> None:
    clear_admin_state(admin_id)
    moderation_repository.cancel_config_input(DB_PATH, admin_id)
    await present_admin_view(admin_id, format_admin_dashboard(admin_id), admin_dashboard_keyboard(admin_id), callback)


async def show_conversation_queue(
    admin_id: int, queue_name: str, page: int = 1, callback: dict[str, Any] | None = None,
) -> None:
    clear_admin_state(admin_id)
    moderation_repository.cancel_config_input(DB_PATH, admin_id)
    total = count_conversation_queue(queue_name)
    page, pages, offset = paginate(page, total)
    rows = get_conversation_queue(queue_name, limit=ADMIN_PAGE_SIZE, offset=offset)
    await present_admin_view(
        admin_id, format_conversation_queue(queue_name, rows, page, pages, total),
        conversation_queue_keyboard(rows, queue_name, admin_id, page, pages), callback,
    )


async def show_contact(
    admin_id: int, chat_id: int, callback: dict[str, Any] | None = None,
    *, queue_name: str = "all", page: int = 1, activate_reply: bool = False,
) -> None:
    if not user_repository.get(DB_PATH, chat_id):
        await send_message(admin_id, "未找到该联系人。")
        return
    previous_target = get_admin_state(admin_id)
    active = not is_blacklisted(chat_id) and (activate_reply or previous_target == chat_id)
    if previous_target not in {None, chat_id}:
        clear_admin_state(admin_id)
    row = get_conversation(chat_id)
    through = int(row["last_inbound_seq"]) if row else 0
    markup = keyboards.admin_user_keyboard(
        chat_id, blacklisted=is_blacklisted(chat_id), active_reply=active, read_through_seq=through,
    )
    markup["inline_keyboard"][-1] = [{"text": "返回聊天", "callback_data": f"queue:{queue_name}:{page}"}]
    text = format_user_detail(chat_id)
    if active:
        text = "<b>正在回复该联系人</b>\n\n" + text
    if await present_admin_view(admin_id, text, markup, callback):
        if active:
            set_admin_state(admin_id, chat_id)
        conversation_repository.mark_read(DB_PATH, chat_id, through)


async def show_settings(admin_id: int, callback: dict[str, Any] | None = None) -> None:
    clear_admin_state(admin_id)
    rows: list[list[ButtonSpec]] = []
    if is_owner(admin_id):
        rows.append([("防广告", "ai:panel")])
    rows.extend([[("已拉黑联系人", "settings:blocked:1")], [("返回首页", "admin:dashboard")]])
    blocked = db_fetchone("SELECT COUNT(*) AS count FROM blacklists")["count"]
    await present_admin_view(
        admin_id, "<b>聊天设置</b>\n\n"
        f"<b>留言保护</b>\n防广告：{SETTINGS.moderation_mode_label}\n已拉黑联系人：{blocked}\n\n"
        "广告暂存在垃圾箱，不会自动拉黑联系人；已拉黑用户的后续留言不会进入聊天列表。",
        inline_keyboard(rows), callback,
    )


async def show_blocked_contacts(admin_id: int, page: int, callback=None) -> None:
    total = int(db_fetchone("SELECT COUNT(*) AS n FROM blacklists")["n"])
    page, pages, offset = paginate(page, total)
    rows = db_fetchall(
        "SELECT b.chat_id, u.username, u.first_name, u.last_name FROM blacklists b "
        "LEFT JOIN users u ON u.chat_id=b.chat_id ORDER BY b.created_at DESC, b.chat_id LIMIT ? OFFSET ?",
        (ADMIN_PAGE_SIZE, offset),
    )
    lines = ["<b>已拉黑联系人</b>"]
    buttons: list[list[ButtonSpec]] = []
    for index, row in enumerate(rows, start=(page - 1) * ADMIN_PAGE_SIZE + 1):
        lines.append(f"{index}. {escape_html_limited(row_user_label(row), 80)}")
        buttons.append([(f"{index} 用户详情", f"detail:{row['chat_id']}")])
    if not rows:
        lines.append("没有已拉黑联系人。")
    if pages > 1:
        buttons.append(pagination_navigation_row("settings:blocked", page, pages))
    buttons.append([("返回设置", "settings:panel")])
    await present_admin_view(admin_id, "\n\n".join(lines), inline_keyboard(buttons), callback)


# =========================
# 业务逻辑
# =========================

def persist_user_event(
    update_id: int,
    user: dict[str, Any],
    text: str,
    source_message: dict[str, Any],
    *,
    event_type: str,
    title: str,
    edited: bool = False,
) -> bool:
    chat_id = int(source_message["chat"]["id"])
    message_id = int(source_message["message_id"])
    return inbound_repository.persist_event(
        DB_PATH,
        update_id,
        user,
        text,
        chat_id,
        message_id,
        event_type=event_type,
        title=title,
        edited=edited,
        admin_ids=ADMIN_IDS,
        include_content_delivery=(
            source_message.get("text") is None
            and can_copy_message(source_message)
        ),
    )


def format_admin_delivery_message(delivery: sqlite3.Row) -> str:
    chat_id = int(delivery["user_chat_id"])
    user = user_repository.get(DB_PATH, chat_id)
    if not user:
        raise RuntimeError("administrator delivery user record is missing")
    history = format_history(chat_id, limit=3, exclude_message_id=int(delivery["source_message_id"]), max_chars=1000)
    return (
        f"<b>{escape_html_limited(row_user_label(user), 100)}</b>\n\n"
        f"{escape_html_limited(str(delivery['content_summary']), 1700)}\n\n"
        f"<b>最近聊天</b>\n{history}"
    )


def claim_next_admin_delivery() -> sqlite3.Row | None:
    return delivery_repository.claim_next_admin(DB_PATH)


def complete_admin_delivery(
    delivery: sqlite3.Row,
    result: TelegramSendResult,
) -> None:
    delivery_repository.complete_admin(DB_PATH, delivery, result)


def mark_admin_delivery_unknown(delivery_id: int, error: str) -> None:
    delivery_repository.mark_admin_unknown(DB_PATH, delivery_id, error)


def defer_or_fail_admin_delivery(
    delivery: sqlite3.Row,
    result: TelegramSendResult,
) -> str:
    return delivery_repository.defer_or_fail_admin(
        DB_PATH,
        delivery,
        result,
        ADMIN_DELIVERY_MAX_ATTEMPTS,
    )


def claim_unalerted_admin_delivery() -> sqlite3.Row | None:
    return delivery_repository.claim_unalerted_admin(DB_PATH)


def mark_admin_delivery_alerted(delivery_id: int) -> None:
    delivery_repository.mark_admin_alerted(DB_PATH, delivery_id)


def defer_admin_delivery_alert(delivery: sqlite3.Row) -> None:
    delivery_repository.defer_admin_alert(DB_PATH, delivery)


def retry_admin_delivery(delivery_id: int) -> str:
    return delivery_repository.retry_admin(DB_PATH, delivery_id)


async def alert_admin_delivery_failure(delivery: sqlite3.Row) -> bool:
    return await admin_delivery_service.alert_failure(
        delivery,
        OWNER_IDS,
        send_message,
    )


async def process_admin_delivery(delivery: sqlite3.Row) -> None:
    event = db_fetchone("SELECT conversation_seq FROM inbound_events WHERE update_id=?", (delivery["update_id"],))
    read_through = int(event["conversation_seq"]) if event else 0
    reviewed = moderation_repository.get(DB_PATH, int(delivery["update_id"]))
    if reviewed and not moderation_repository.is_current(DB_PATH, int(delivery["update_id"])):
        db_execute("UPDATE admin_deliveries SET status='canceled' WHERE id=?", (delivery["id"],))
        return

    async def copy_reviewed(to_chat_id: int, from_chat_id: int, message_id: int) -> TelegramSendResult:
        if not reviewed:
            return await copy_message(to_chat_id, from_chat_id, message_id)
        try:
            method, payload = moderation_service.delivery_payload(json.loads(reviewed["snapshot"]), to_chat_id)
            return telegram_send_result(await tg(method, payload))
        except Exception as exc:
            return failed_send_result(exc)

    await admin_delivery_service.process_delivery(
        delivery,
        send_message,
        copy_reviewed,
        format_admin_delivery_message,
        lambda chat_id, viewer_admin_id: admin_user_keyboard(
            chat_id,
            viewer_admin_id=viewer_admin_id,
            read_through_seq=read_through,
        ),
        complete_admin_delivery,
        mark_admin_delivery_unknown,
        defer_or_fail_admin_delivery,
    )


async def periodic_admin_delivery_worker() -> None:
    await admin_delivery_service.run_worker(
        admin_delivery_wakeup,
        claim_unalerted_admin_delivery,
        alert_admin_delivery_failure,
        defer_admin_delivery_alert,
        mark_admin_delivery_alerted,
        claim_next_admin_delivery,
        process_admin_delivery,
        mark_admin_delivery_unknown,
        logger,
    )


async def send_welcome(chat_id: int) -> None:
    await send_message(
        chat_id,
        WELCOME_TEXT,
        reply_markup=welcome_keyboard(),
    )


def apply_ai_settings(settings: Settings) -> None:
    global SETTINGS, AI_MODERATION_ENABLED
    SETTINGS = settings
    AI_MODERATION_ENABLED = settings.ai_moderation_enabled
    if moderation_wakeup:
        moderation_wakeup.set()


def get_ai_settings_controller() -> AISettingsController:
    global ai_settings_controller
    if ai_settings_controller is None:
        ai_settings_controller = AISettingsController(
            lambda: DB_PATH, lambda: BASE_DIR / ".env", lambda: SETTINGS,
            apply_ai_settings, send_message, tg, is_owner, clear_admin_state, present_admin_view,
        )
    return ai_settings_controller


async def enqueue_moderation(message: dict[str, Any], update_id: int, text: str, *, edited: bool) -> None:
    chat_id = int(message["chat"]["id"])
    status = moderation_repository.enqueue(
        DB_PATH, update_id, chat_id, int(message["message_id"]),
        moderation_service.snapshot(message), text, edited,
    )
    if status == "full":
        await send_message(chat_id, "留言队列暂时已满，这条消息尚未接收，请稍后重新发送。")
        return
    if status == "queued":
        if moderation_wakeup:
            moderation_wakeup.set()
        if not edited:
            await send_message(chat_id, "留言已收到，我看到后会通过 Bot 回复你。")


async def notify_moderation_pending() -> None:
    if moderation_repository.claim_error_notice(DB_PATH):
        for admin_id in sorted(ADMIN_IDS):
            await send_message(
                admin_id, "防广告检测出现故障，部分留言已暂存，请检查设置。",
                reply_markup=inline_keyboard([[("查看暂存", "moderation:1:error")]]),
            )


def moderation_inbox() -> ModerationInbox:
    return ModerationInbox(DB_PATH, ADMIN_IDS, present_admin_view, send_message, tg,
                           answer_callback_query, admin_delivery_wakeup, moderation_wakeup,
                           AI_MODERATION_ENABLED or SETTINGS.spam_rules_enabled)


async def show_moderation_queue(admin_id: int, page: int = 1,
                                callback: dict[str, Any] | None = None) -> None:
    if not is_admin(admin_id):
        return
    clear_admin_state(admin_id)
    moderation_repository.cancel_config_input(DB_PATH, admin_id)
    await moderation_inbox().show(admin_id, page, callback)


async def handle_moderation_callback(callback: dict[str, Any], action: str, value: str) -> None:
    admin_id = int(callback["from"]["id"])
    if not is_admin(admin_id) or not is_private_callback(callback):
        await answer_callback_query(callback["id"], "无权限")
        return
    clear_admin_state(admin_id)
    await moderation_inbox().callback(callback, action, value)


def normalize_command_text(text: str) -> str:
    if not text.startswith("/"):
        return text
    first, separator, remainder = text.partition(" ")
    command = first.split("@", 1)[0].lower()
    return f"{command} {remainder}" if separator else command


async def handle_user_message(message: dict[str, Any], update_id: int) -> None:
    user = message["from"]
    chat_id = int(message["chat"]["id"])
    text = message_content(message)
    message_id = int(message["message_id"])

    if is_blacklisted(chat_id):
        upsert_user(user, text)
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

    upsert_user(user, "[待接收]" if AI_MODERATION_ENABLED or SETTINGS.spam_rules_enabled else text)

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

    if AI_MODERATION_ENABLED or SETTINGS.spam_rules_enabled:
        await enqueue_moderation(message, update_id, text, edited=False)
        return

    persist_user_event(
        update_id,
        user,
        text,
        message,
        event_type="message",
        title="收到用户消息",
    )
    await send_message(chat_id, "留言已收到，我看到后会通过 Bot 回复你。")
    if admin_delivery_wakeup:
        admin_delivery_wakeup.set()


async def handle_user_edited_message(message: dict[str, Any], update_id: int) -> None:
    user = message["from"]
    chat_id = int(message["chat"]["id"])
    message_id = int(message["message_id"])
    text = message_content(message)

    if is_blacklisted(chat_id) or text == "/start":
        return
    upsert_user(user, "[待接收]" if AI_MODERATION_ENABLED or SETTINGS.spam_rules_enabled else text)

    allowed, should_notify, retry_after = check_user_rate_limit(chat_id)
    if not allowed:
        if should_notify:
            await send_message(
                chat_id,
                f"修改得太频繁了，请等待约 {retry_after} 秒后再试。",
                parse_mode=None,
            )
        return

    if AI_MODERATION_ENABLED or SETTINGS.spam_rules_enabled:
        await enqueue_moderation(message, update_id, text, edited=True)
        return

    persist_user_event(
        update_id,
        user,
        text,
        message,
        event_type="edited_message",
        title="用户修改了消息",
        edited=True,
    )
    if admin_delivery_wakeup:
        admin_delivery_wakeup.set()


def prepare_admin_reply_delivery(
    update_id: int | None,
    admin_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    user_chat_id: int,
    route: str,
    read_through_seq: int = 0,
) -> str:
    return delivery_repository.prepare_reply(
        DB_PATH,
        update_id,
        admin_id,
        admin_chat_id,
        admin_message_id,
        user_chat_id,
        route,
        read_through_seq,
    )


def mark_admin_reply_sending(admin_chat_id: int, admin_message_id: int) -> bool:
    return delivery_repository.mark_reply_sending(
        DB_PATH,
        admin_chat_id,
        admin_message_id,
    )


def fail_admin_reply_delivery(
    admin_chat_id: int,
    admin_message_id: int,
    result: TelegramSendResult,
) -> None:
    delivery_repository.fail_reply(
        DB_PATH,
        admin_chat_id,
        admin_message_id,
        result,
    )


def mark_admin_reply_unknown(
    admin_chat_id: int,
    admin_message_id: int,
    error: str,
) -> None:
    delivery_repository.mark_reply_unknown(
        DB_PATH,
        admin_chat_id,
        admin_message_id,
        error,
    )


def complete_admin_reply_delivery(
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
    delivery_repository.complete_reply(
        DB_PATH,
        admin_chat_id,
        admin_message_id,
        user_chat_id,
        admin_id,
        route,
        content,
        kind,
        result,
        admin_message_thread_id,
    )


async def send_admin_reply_status(
    admin_id: int, target_chat_id: int, route: str, *, duplicate: bool = False,
    message_id: int | None = None,
) -> None:
    if duplicate:
        return
    if message_id is not None:
        try:
            result = await tg("setMessageReaction", {
                "chat_id": admin_id, "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": "\U0001f44d"}],
            })
            if result.get("ok"):
                return
        except Exception:
            pass
    await send_message(admin_id, f"已发送给 {chat_label_html(target_chat_id)}。")


async def send_admin_reply_uncertain(admin_id: int, target_chat_id: int) -> None:
    await send_message(
        admin_id,
        "<b>投递结果不确定</b>\n\n"
        f"联系人：{chat_label_html(target_chat_id)}\n"
        "为避免重复发送，Bot 没有自动重放。请先确认用户侧，再重新发送一条新消息。",
        reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
    )


async def handle_admin_command(
    admin_id: int, text: str, message: dict[str, Any] | None = None, update_id: int | None = None,
) -> bool:
    if not is_admin(admin_id):
        return False
    command = text.split(maxsplit=1)[0] if text else ""
    if text == "/ai":
        await get_ai_settings_controller().panel(admin_id)
    elif command == "/start":
        await show_admin_dashboard(admin_id)
    elif text == "/myid":
        await send_message(admin_id, f"你的 Telegram ID：<code>{admin_id}</code>")
    elif text == "/cancel":
        target = clear_admin_state(admin_id)
        await send_message(
            admin_id, "已退出持续回复。" if target is not None else "当前没有正在回复的联系人。",
            reply_markup=admin_dashboard_keyboard(admin_id),
        )
    elif text == "/settings":
        await show_settings(admin_id)
    elif text == "/moderation":
        await show_moderation_queue(admin_id)
    elif text == "/inbox":
        await show_conversation_queue(admin_id, "inbox")
    elif text == "/users":
        await show_conversation_queue(admin_id, "all")
    elif command in {
        "/broadcast", "/broadcast_status", "/broadcast_retry", "/audit", "/reply", "/send",
        "/takeover", "/close", "/pending", "/closed", "/blacklist", "/unblacklist", "/blacklist_list",
    }:
        await send_message(admin_id, "该旧指令已移除，请从聊天列表选择联系人。", reply_markup=admin_dashboard_keyboard(admin_id))
    else:
        return False
    return True


async def handle_admin_message(message: dict[str, Any], update_id: int | None = None) -> None:
    admin_id = int(message["from"]["id"])
    if not is_admin(admin_id):
        return

    if await get_ai_settings_controller().message(message):
        return

    text = normalize_command_text(message.get("text") or message.get("caption") or "")
    content = message_content(message)
    if text.startswith("/"):
        moderation_repository.cancel_config_input(DB_PATH, admin_id)

    if await handle_admin_command(admin_id, text, message, update_id):
        return

    if text.startswith("/"):
        await send_message(
            admin_id,
            "<b>未知管理员指令</b>\n\n请从命令菜单选择，或返回首页。",
            reply_markup=admin_dashboard_keyboard(admin_id),
        )
        return

    target_chat_id, target_source = resolve_admin_message_target(admin_id, message)
    if target_source == "unmapped_reply":
        clear_admin_state(admin_id)
        await send_message(
            admin_id,
            "<b>无法识别回复对象</b>\n\n"
            "这条被回复的消息没有用户映射。为防止发错人，本次消息没有发送。\n"
            "请 Reply 用户通知，或点击下方“持续回复”后重试。",
            reply_markup=admin_dashboard_keyboard(admin_id),
        )
        return
    if not target_chat_id:
        await send_message(
            admin_id,
            "<b>尚未选择回复对象</b>\n\n"
            "请打开收件箱选择用户，或直接 Reply 该用户的留言通知。",
            reply_markup=admin_dashboard_keyboard(admin_id),
        )
        return

    if target_source == "reply" and get_admin_state(admin_id) != target_chat_id:
        clear_admin_state(admin_id)

    if is_blacklisted(target_chat_id):
        await send_message(
            admin_id,
            "这个用户在黑名单中。请先解除黑名单后再回复。",
            reply_markup=admin_user_keyboard(target_chat_id, viewer_admin_id=admin_id),
        )
        return

    admin_chat_id = int(message["chat"]["id"])
    admin_message_id = int(message["message_id"])
    conversation = get_conversation(target_chat_id)
    read_through = int(conversation["last_read_seq"]) if conversation else 0
    if target_source == "reply":
        source_id = message["reply_to_message"]["message_id"]
        event = db_fetchone(
            "SELECT e.conversation_seq FROM admin_deliveries d JOIN inbound_events e ON e.update_id=d.update_id "
            "WHERE d.admin_chat_id=? AND d.admin_message_id=? AND d.user_chat_id=?",
            (admin_chat_id, source_id, target_chat_id),
        )
        if event:
            read_through = int(event["conversation_seq"])
    delivery_status = prepare_admin_reply_delivery(
        update_id,
        admin_id,
        admin_chat_id,
        admin_message_id,
        target_chat_id,
        target_source,
        read_through,
    )
    if delivery_status == "sent":
        await send_admin_reply_status(
            admin_id, target_chat_id, target_source, duplicate=True
        )
        return
    if delivery_status == "unknown":
        await send_admin_reply_uncertain(admin_id, target_chat_id)
        return
    if delivery_status == "failed":
        await send_message(
            admin_id,
            "上一轮发送已经明确失败。请检查原因后发送一条新的回复。",
        )
        return
    if not mark_admin_reply_sending(admin_chat_id, admin_message_id):
        await send_admin_reply_uncertain(admin_id, target_chat_id)
        return

    delivery_result = await copy_message(
        target_chat_id,
        admin_chat_id,
        admin_message_id,
    )
    if not delivery_result:
        fail_admin_reply_delivery(
            admin_chat_id, admin_message_id, delivery_result
        )
        await send_message(admin_id, delivery_failure_message(delivery_result))
        return
    try:
        complete_admin_reply_delivery(
            admin_chat_id,
            admin_message_id,
            target_chat_id,
            admin_id,
            target_source,
            content,
            message_kind(message),
            delivery_result,
            message.get("message_thread_id"),
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            mark_admin_reply_unknown(admin_chat_id, admin_message_id, str(exc))
        await send_admin_reply_uncertain(admin_id, target_chat_id)
        return
    await send_admin_reply_status(admin_id, target_chat_id, target_source, message_id=admin_message_id)


async def handle_callback(callback: dict[str, Any]) -> None:
    admin_id = int(callback.get("from", {}).get("id", 0))
    if is_admin(admin_id) and is_private_callback(callback):
        async with admin_ui_locks.setdefault(admin_id, asyncio.Lock()):
            await dispatch_callback(callback)
    else:
        await dispatch_callback(callback)


async def dispatch_callback(callback: dict[str, Any]) -> None:
    callback_id = callback["id"]
    admin_id = int(callback["from"]["id"])
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
        text = ("直接在当前对话发送消息即可。消息会通过 Bot 转交，无需私聊其他账号。" if data == "user_help"
                else "支持文字、图片、文件、语音和视频。发送后请等待回复，避免短时间重复发送。")
        await send_message(chat_id, text)
        return
    if not is_admin(admin_id):
        await answer_callback_query(callback_id, "无权限")
        return
    if ":" not in data:
        await answer_callback_query(callback_id, "该按钮已失效，请打开首页")
        return
    action, value = data.split(":", 1)
    if action == "ai":
        if not is_owner(admin_id):
            await answer_callback_query(callback_id, "仅负责人可管理防广告")
            return
        await answer_callback_query(callback_id)
        await get_ai_settings_controller().callback(admin_id, value, callback)
        return
    moderation_repository.cancel_config_input(DB_PATH, admin_id)
    if action.startswith("broadcast") or action in {"takeover", "close", "resolve", "reopen"}:
        await answer_callback_query(callback_id, "该功能已移除，请使用聊天列表")
        return
    if action.startswith("moderation"):
        await handle_moderation_callback(callback, action, value)
        return
    if action == "settings":
        parts = value.split(":")
        if parts == ["panel"]:
            await answer_callback_query(callback_id)
            await show_settings(admin_id, callback)
        elif len(parts) == 2 and parts[0] == "blocked" and parse_callback_page(parts[1]) is not None:
            clear_admin_state(admin_id)
            await answer_callback_query(callback_id)
            await show_blocked_contacts(admin_id, int(parts[1]), callback)
        else:
            await answer_callback_query(callback_id, "页面无效")
        return
    if action == "admin":
        if value == "dashboard":
            await answer_callback_query(callback_id)
            await show_admin_dashboard(admin_id, callback)
        elif value == "users" or value.startswith("users:"):
            page = parse_callback_page(value.partition(":")[2] or None)
            if page is None:
                await answer_callback_query(callback_id, "页码无效")
                return
            await answer_callback_query(callback_id)
            await show_conversation_queue(admin_id, "all", page, callback)
        else:
            await answer_callback_query(callback_id, "页面无效")
        return
    if action in {"queue", "read_all"}:
        queue_name, _, raw_page = value.partition(":")
        page = parse_callback_page(raw_page or None)
        if page is None:
            await answer_callback_query(callback_id, "页码无效")
            return
        if queue_name not in QUEUE_LABELS:
            await answer_callback_query(callback_id, "该列表已更新，请返回首页")
            return
        clear_admin_state(admin_id)
        if action == "read_all":
            count = conversation_repository.mark_all_read(DB_PATH)
            add_admin_audit(admin_id, "mark_all_read", details=f"conversations={count}")
            await answer_callback_query(callback_id, f"已将 {count} 个聊天标记为已读")
        else:
            await answer_callback_query(callback_id)
        await show_conversation_queue(admin_id, queue_name, page, callback)
        return
    if action == "user":
        parts = value.split(":")
        if (len(parts) != 3 or not parts[0].isascii() or not parts[0].isdigit() or len(parts[0]) > 18
                or parts[1] not in QUEUE_LABELS or parse_callback_page(parts[2]) is None):
            await answer_callback_query(callback_id, "页面无效")
            return
        await answer_callback_query(callback_id)
        await show_contact(admin_id, int(parts[0]), callback, queue_name=parts[1], page=int(parts[2]), activate_reply=True)
        return
    if action == "read":
        parts = value.split(":")
        if len(parts) != 2 or any(not part.isascii() or not part.isdigit() or len(part) > 18 for part in parts):
            await answer_callback_query(callback_id, "参数无效")
            return
        conversation_repository.mark_read(DB_PATH, int(parts[0]), int(parts[1]))
        await answer_callback_query(callback_id, "已标记所显示的消息为已读")
        return
    if action == "delete_confirm":
        parts = value.split(":")
        if len(parts) != 2 or any(not part.isascii() or not part.isdigit() or len(part) > 18 for part in parts):
            await answer_callback_query(callback_id, "参数无效")
            return
        chat_id, through = map(int, parts)
        result = conversation_repository.archive(DB_PATH, chat_id, through)
        if result == "changed":
            await answer_callback_query(callback_id, "有新留言，未删除会话")
            await present_admin_view(
                admin_id, "<b>会话已更新</b>\n\n确认期间收到了新留言，本次未删除，也未标记新留言为已读。",
                inline_keyboard([[("查看新留言", f"detail:{chat_id}")], [("返回聊天", "queue:all:1")]]), callback,
            )
        elif result == "archived":
            add_admin_audit(admin_id, "conversation_archive", chat_id)
            await answer_callback_query(callback_id, "会话已从列表移除")
            await show_conversation_queue(admin_id, "all", callback=callback)
        else:
            await answer_callback_query(callback_id, "会话不存在或已更新")
        return
    if action == "delivery_retry":
        if not is_owner(admin_id) or not value.isascii() or not value.isdigit() or len(value) > 18:
            await answer_callback_query(callback_id, "无权限或参数无效")
            return
        status = retry_admin_delivery(int(value))
        if status == "pending" and admin_delivery_wakeup:
            admin_delivery_wakeup.set()
        await answer_callback_query(callback_id, "已重新排队" if status == "pending" else "当前不能重试")
        return
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        await answer_callback_query(callback_id, "参数无效")
        return
    chat_id = int(value)
    if not user_repository.get(DB_PATH, chat_id) and not is_blacklisted(chat_id):
        await answer_callback_query(callback_id, "联系人不存在")
        return
    if action in {"reply", "detail"}:
        if action == "reply" and is_blacklisted(chat_id):
            await answer_callback_query(callback_id, "请先解除拉黑")
            return
        await answer_callback_query(callback_id)
        await show_contact(admin_id, chat_id, callback, activate_reply=action == "reply")
    elif action == "cancel":
        cleared = clear_admin_state(admin_id, expected_target_chat_id=chat_id)
        await answer_callback_query(callback_id, "已退出回复" if cleared is not None else "这不是当前回复会话")
        if cleared is not None:
            await present_admin_view(admin_id, "已退出持续回复。", admin_user_keyboard(chat_id, admin_id), callback)
    elif action == "more":
        if get_admin_state(admin_id) not in {None, chat_id}:
            clear_admin_state(admin_id)
        button: ButtonSpec = (("解除拉黑", f"unblacklist:{chat_id}", "success") if is_blacklisted(chat_id)
                              else ("拉黑联系人", f"blacklist:{chat_id}", "danger"))
        await answer_callback_query(callback_id)
        await present_admin_view(admin_id, f"<b>{chat_label_html(chat_id)} · 会话操作</b>\n\n"
                                 "<b>整理聊天</b>\n删除会话只移出列表，保留历史；对方再次留言时会重新出现。\n\n"
                                 "<b>阻止联系</b>\n拉黑后不再接收该联系人的留言，可在设置中解除。", inline_keyboard([
            [("删除会话", f"delete:{chat_id}", "danger")], [button],
            [("用户详情 / 聊天记录", f"detail:{chat_id}")], [("返回聊天", "queue:all:1")],
        ]), callback)
    elif action == "delete":
        row = get_conversation(chat_id)
        if not row:
            await answer_callback_query(callback_id, "会话不存在")
            return
        await answer_callback_query(callback_id)
        await present_admin_view(
            admin_id, f"<b>删除与 {chat_label_html(chat_id)} 的会话？</b>\n\n"
            "会从所有管理员的聊天列表移除，并退出针对该联系人的持续回复。\n\n"
            "聊天记录与原生 Reply 对应关系仍会保留；不会删除 Telegram 消息或拉黑对方。"
            "对方再次留言后，会话会自动重新出现。",
            inline_keyboard([[("确认删除会话", f"delete_confirm:{chat_id}:{row['last_inbound_seq']}", "danger")],
                             [("保留会话", f"more:{chat_id}")]]), callback,
        )
    elif action == "blacklist":
        await answer_callback_query(callback_id)
        await present_admin_view(
            admin_id, f"拉黑 {chat_label_html(chat_id)}？\n后续消息将被拦截。",
            inline_keyboard([[("确认拉黑", f"blacklist_confirm:{chat_id}", "danger"),
                              ("取消", f"blacklist_cancel:{chat_id}")]]), callback,
        )
    elif action == "blacklist_cancel":
        await answer_callback_query(callback_id, "已取消")
        await show_contact(admin_id, chat_id, callback)
    elif action == "blacklist_confirm":
        blacklist_user(chat_id, admin_id, "管理员确认")
        clear_admin_states_for_target(chat_id)
        add_admin_audit(admin_id, "blacklist_add", chat_id)
        await answer_callback_query(callback_id, "已拉黑")
        await present_admin_view(admin_id, f"已拉黑 {chat_label_html(chat_id)}。", admin_user_keyboard(chat_id, admin_id), callback)
    elif action == "unblacklist":
        unblacklist_user(chat_id)
        add_admin_audit(admin_id, "blacklist_remove", chat_id)
        await answer_callback_query(callback_id, "已解除拉黑")
        await show_contact(admin_id, chat_id, callback)
    else:
        await answer_callback_query(callback_id, "该按钮已失效，请打开首页")


async def read_limited_request_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid content length",
            ) from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="invalid content length")
        if declared_length > max_bytes:
            raise HTTPException(status_code=413, detail="request too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="request too large")
    return bytes(body)


@app.get("/healthz")
async def healthz() -> dict[str, str | bool]:
    try:
        db_fetchone("SELECT 1 AS ok")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="db unavailable") from exc
    if admin_delivery_worker_task is None or admin_delivery_worker_task.done():
        raise HTTPException(
            status_code=503,
            detail="administrator delivery worker unavailable",
        )
    if moderation_worker_task is None or moderation_worker_task.done():
        raise HTTPException(status_code=503, detail="moderation worker unavailable")
    return {
        "ok": True,
        "version": APP_VERSION,
        "moderation_worker": "ok",
        "db": "ok",
        "admin_delivery_worker": "ok",
    }


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

    content_type = (
        request.headers.get("content-type", "")
        .partition(";")[0]
        .strip()
        .lower()
    )
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="JSON request required")
    body = await read_limited_request_body(request, WEBHOOK_MAX_BODY_BYTES)
    try:
        update = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid update JSON") from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="invalid update")

    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        raise HTTPException(status_code=400, detail="invalid update ID")
    claim_status = claim_update(update_id)
    if claim_status == "done":
        return {"ok": True}
    if claim_status == "processing":
        raise HTTPException(status_code=503, detail="update is already processing")
    claimed = True

    try:
        if "callback_query" in update:
            await handle_callback(update["callback_query"])
        else:
            edited = "edited_message" in update
            message = update.get("edited_message") if edited else update.get("message")
            if (message and "from" in message and not message["from"].get("is_bot")
                    and is_private_chat_message(message)):
                from_id = int(message["from"]["id"])
                if is_admin(from_id):
                    if edited:
                        await get_ai_settings_controller().message(message, edited=True)
                    else:
                        await handle_admin_message(message, update_id)
                elif edited:
                    await handle_user_edited_message(message, update_id)
                else:
                    await handle_user_message(message, update_id)

        if claimed:
            finish_update(update_id)
        return {"ok": True}
    except Exception as exc:
        if claimed:
            with contextlib.suppress(Exception):
                fail_update(update_id, str(exc))
        logger.exception("Webhook update processing failed update_id=%s", update_id)
        raise HTTPException(status_code=500, detail="update processing failed") from exc
