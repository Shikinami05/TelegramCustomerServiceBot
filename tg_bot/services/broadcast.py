import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from tg_bot.keyboards import inline_keyboard
from tg_bot.models import TelegramSendResult


SendMessage = Callable[..., Awaitable[TelegramSendResult]]


async def process_job(
    job: sqlite3.Row,
    claim_recipient: Callable[[str], int | None],
    complete_job: Callable[[str], sqlite3.Row | None],
    is_blacklisted: Callable[[int], bool],
    finish_recipient: Callable[[str, int, bool, str, bool], None],
    send_message: SendMessage,
    rate_limit_retries: int,
    send_delay_seconds: float,
) -> None:
    broadcast_id = str(job["id"])
    content = str(job["content"])

    while True:
        chat_id = claim_recipient(broadcast_id)
        if chat_id is None:
            result = complete_job(broadcast_id)
            if result:
                failed_count = int(result["failed_count"])
                unknown_count = int(result["unknown_count"])
                reply_markup = (
                    inline_keyboard(
                        [[
                            (
                                "重试失败用户",
                                f"broadcast_retry:{broadcast_id}",
                                "primary",
                            )
                        ]]
                    )
                    if failed_count > 0
                    else None
                )
                await send_message(
                    int(result["admin_id"]),
                    "<b>群发完成</b>\n\n"
                    f"成功：<b>{result['sent_count']}</b>\n"
                    f"失败：<b>{failed_count}</b>\n"
                    f"不确定：<b>{unknown_count}</b>\n"
                    f"总计：{result['total_count']}",
                    reply_markup=reply_markup,
                )
            return

        if is_blacklisted(chat_id):
            finish_recipient(
                broadcast_id,
                chat_id,
                False,
                "user is blacklisted",
                False,
            )
            continue

        result = await send_message(
            chat_id,
            content,
            parse_mode=None,
            rate_limit_retries=rate_limit_retries,
            rate_limit_max_wait_seconds=None,
        )
        sent = bool(result)
        unknown = not sent and (
            result.status_code is None or result.status_code >= 500
        )
        finish_recipient(
            broadcast_id,
            chat_id,
            sent,
            result.description or ("Telegram send failed" if not sent else ""),
            unknown,
        )
        if send_delay_seconds:
            await asyncio.sleep(send_delay_seconds)


async def run_worker(
    wakeup: asyncio.Event | None,
    claim_job: Callable[[], sqlite3.Row | None],
    process: Callable[[sqlite3.Row], Awaitable[None]],
    recover_job: Callable[[str, str], None],
    logger: logging.Logger,
) -> None:
    while True:
        job: sqlite3.Row | None = None
        try:
            job = claim_job()
            if job:
                await process(job)
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
            logger.exception("Broadcast worker failed")
            if job:
                recover_job(str(job["id"]), str(exc))
            await asyncio.sleep(2)
