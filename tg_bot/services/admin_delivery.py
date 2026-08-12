import asyncio
import contextlib
import html
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from tg_bot.keyboards import inline_keyboard
from tg_bot.models import TelegramSendResult
from tg_bot.text import escape_html_limited


SendMessage = Callable[..., Awaitable[TelegramSendResult]]
CopyMessage = Callable[[int, int, int], Awaitable[TelegramSendResult]]


async def alert_failure(
    delivery: sqlite3.Row,
    owner_ids: set[int],
    send_message: SendMessage,
) -> bool:
    uncertain = delivery["status"] == "unknown"
    state_text = "投递结果不确定" if uncertain else "投递失败"
    warning = (
        "Telegram 可能已经收到过该消息，手动重试可能产生重复。"
        if uncertain
        else "可以确认后手动重试。"
    )
    text = (
        f"<b>管理员消息{state_text}</b>\n\n"
        f"用户：<code>{delivery['user_chat_id']}</code>\n"
        f"管理员：<code>{delivery['admin_chat_id']}</code>\n"
        f"类型：<code>{html.escape(str(delivery['delivery_kind']))}</code>\n"
        f"原因：{escape_html_limited(str(delivery['last_error']), 300)}\n\n"
        f"{warning}"
    )
    markup = inline_keyboard(
        [[("重试投递", f"delivery_retry:{delivery['id']}", "danger")]]
    )
    delivered = False
    for owner_id in sorted(owner_ids):
        result = await send_message(owner_id, text, reply_markup=markup)
        delivered = bool(result) or delivered
    return delivered


async def process_delivery(
    delivery: sqlite3.Row,
    send_message: SendMessage,
    copy_message: CopyMessage,
    format_message: Callable[[sqlite3.Row], str],
    user_keyboard: Callable[[int, int | None], dict[str, Any]],
    complete: Callable[[sqlite3.Row, TelegramSendResult], None],
    mark_unknown: Callable[[int, str], None],
    defer_or_fail: Callable[[sqlite3.Row, TelegramSendResult], str],
) -> None:
    if delivery["delivery_kind"] == "notification":
        admin_chat_id = int(delivery["admin_chat_id"])
        result = await send_message(
            admin_chat_id,
            format_message(delivery),
            reply_markup=user_keyboard(
                int(delivery["user_chat_id"]),
                admin_chat_id,
            ),
        )
    else:
        result = await copy_message(
            int(delivery["admin_chat_id"]),
            int(delivery["user_chat_id"]),
            int(delivery["source_message_id"]),
        )
    if result:
        try:
            complete(delivery, result)
        except Exception as exc:
            with contextlib.suppress(Exception):
                mark_unknown(int(delivery["id"]), str(exc))
            raise
        return
    defer_or_fail(delivery, result)


async def run_worker(
    wakeup: asyncio.Event | None,
    claim_unalerted: Callable[[], sqlite3.Row | None],
    alert: Callable[[sqlite3.Row], Awaitable[bool]],
    defer_alert: Callable[[sqlite3.Row], None],
    mark_alerted: Callable[[int], None],
    claim_next: Callable[[], sqlite3.Row | None],
    process: Callable[[sqlite3.Row], Awaitable[None]],
    mark_unknown: Callable[[int, str], None],
    logger: logging.Logger,
) -> None:
    while True:
        delivery: sqlite3.Row | None = None
        try:
            failed_delivery = claim_unalerted()
            if failed_delivery:
                try:
                    alerted = await alert(failed_delivery)
                except Exception:
                    logger.exception(
                        "Administrator delivery failure alert failed delivery_id=%s",
                        failed_delivery["id"],
                    )
                    defer_alert(failed_delivery)
                else:
                    if alerted:
                        mark_alerted(int(failed_delivery["id"]))
                    else:
                        defer_alert(failed_delivery)

            delivery = claim_next()
            if delivery:
                await process(delivery)
                continue
            if failed_delivery:
                continue
            if wakeup is None:
                await asyncio.sleep(2)
                continue
            wakeup.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wakeup.wait(), timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Administrator delivery worker failed")
            if delivery:
                with contextlib.suppress(Exception):
                    mark_unknown(int(delivery["id"]), str(exc))
            await asyncio.sleep(2)
