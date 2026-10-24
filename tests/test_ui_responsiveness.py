import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_IDS", "1,2")

import app
from tg_bot import database
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import conversations, inbound


class UIResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "bot.db"
        database.initialize(self.db)
        self.edit = AsyncMock(return_value=True)
        self.send = AsyncMock(return_value=TelegramSendResult(True, message_id=900))
        self.telegram = AsyncMock(return_value={"ok": True})
        for key, value in {"DB_PATH": self.db, "ADMIN_IDS": {1, 2}, "OWNER_IDS": {1},
                           "edit_message_text": self.edit, "send_message": self.send,
                           "tg": self.telegram, "admin_ui_locks": {},
                           "CALLBACK_ACK_TIMEOUT_SECONDS": 0.02,
                           "ADMIN_VIEW_TIMEOUT_SECONDS": 0.2}.items():
            patcher = patch.object(app, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def callback(self, data="refresh:all:1", admin=1, **extra):
        return {"id": "callback", "from": {"id": admin}, "data": data,
                "message": {"message_id": 700, "chat": {"id": admin, "type": "private"}, **extra}}

    def receive(self, user=50, update=100):
        inbound.persist_event(self.db, update, {"id": user, "first_name": "Test"}, "hello", user, 10,
                              event_type="message", title="message", edited=False,
                              admin_ids={1, 2}, include_content_delivery=False)

    async def test_callback_ack_timeout_does_not_block_list_rendering(self):
        canceled = asyncio.Event()

        async def stalled(*args):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.telegram.side_effect = stalled
        await asyncio.wait_for(app.handle_callback(self.callback()), timeout=1)
        self.assertTrue(canceled.is_set())
        self.edit.assert_awaited_once()
        self.assertFalse(app.admin_ui_locks[1].locked())

    async def test_refresh_while_another_action_is_busy_returns_without_queueing(self):
        self.receive()
        app.set_admin_state(1, 50)
        lock = app.admin_ui_locks.setdefault(1, asyncio.Lock())
        async with lock:
            with patch.object(app, "dispatch_callback", new_callable=AsyncMock) as dispatch:
                for data in ("refresh:all:1", "queue:all:1", "contact:50:all:1"):
                    await asyncio.wait_for(app.handle_callback(self.callback(data)), timeout=1)
                dispatch.assert_not_awaited()
        self.assertIn("上一項操作仍在處理", self.telegram.await_args.args[1]["text"])
        self.assertEqual(app.get_admin_state(1), 50)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)

    async def test_timed_out_edit_releases_lock_without_sending_duplicate_panel(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        self.edit.side_effect = stalled
        with patch.object(app, "ADMIN_VIEW_TIMEOUT_SECONDS", 0.02):
            await asyncio.wait_for(app.handle_callback(self.callback()), timeout=1)
        self.send.assert_not_awaited()
        self.assertFalse(app.admin_ui_locks[1].locked())
        self.edit.side_effect = None
        await app.handle_callback(self.callback())
        self.assertEqual(self.edit.await_count, 2)

    async def test_same_refresh_content_and_keyboard_skip_unnecessary_telegram_edit(self):
        markup = app.inline_keyboard([[("重新整理", "refresh:all:1")]])
        callback = self.callback(text="聊天 & 記錄", reply_markup=markup)
        self.assertTrue(await app.present_admin_view(1, "<b>聊天 &amp; 記錄</b>", markup, callback))
        self.edit.assert_not_awaited()
        self.send.assert_not_awaited()
        changed = app.inline_keyboard([[("刪除所有會話", "cleanup:all", "danger")]])
        self.assertTrue(await app.present_admin_view(1, "<b>聊天 &amp; 記錄</b>", changed, callback))
        self.edit.assert_awaited_once()

    async def test_matching_mapped_notification_is_never_treated_as_editable_panel(self):
        self.receive()
        app.add_message_link(50, 10, 1, 700, "user_to_admin", "notification")
        markup = app.inline_keyboard([[("重新整理", "refresh:all:1")]])
        await app.present_admin_view(1, "content", markup, self.callback(text="content", reply_markup=markup))
        self.edit.assert_not_awaited()
        self.send.assert_awaited_once()
        self.assertEqual(app.get_message_link(1, 700)["user_chat_id"], 50)

    async def test_queue_uses_one_count_query_and_joined_block_flags(self):
        for index in range(10):
            self.receive(50 + index, 100 + index)
        app.blacklist_user(55, 1, "test")
        with patch.object(app, "get_queue_counts", wraps=app.get_queue_counts) as counts, \
             patch.object(app, "is_blacklisted", side_effect=AssertionError("N+1 lookup")):
            await app.show_conversation_queue(1, "all")
        counts.assert_called_once_with()
        self.assertIn("已封鎖", self.send.await_args.args[1])

    async def test_dashboard_refresh_keeps_reply_target_and_unread_state(self):
        self.receive()
        app.set_admin_state(1, 50)
        with patch.object(app, "get_queue_counts", wraps=app.get_queue_counts) as counts:
            await app.handle_callback(self.callback("refresh:dashboard:1"))
        counts.assert_called_once_with()
        self.assertEqual(app.get_admin_state(1), 50)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertIn("正在重新整理", self.telegram.await_args.args[1]["text"])

    async def test_invalid_and_unauthorized_refresh_cannot_change_read_or_reply_state(self):
        self.receive()
        app.set_admin_state(1, 50)
        for data in ("refresh:dashboard:2", "refresh:all:０", "refresh:other:1"):
            await app.handle_callback(self.callback(data))
        await app.handle_callback(self.callback(admin=99))
        self.assertEqual(self.telegram.await_args.args[1]["text"], "無權限")
        self.edit.assert_not_awaited()
        self.assertEqual(app.get_admin_state(1), 50)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)

    async def test_settings_show_running_version_and_role_without_credentials(self):
        for admin, role in ((1, "負責人"), (2, "管理員")):
            await app.show_settings(admin)
            text = self.send.await_args.args[1]
            self.assertIn(f"執行版本：{app.APP_VERSION}", text)
            self.assertIn(f"帳號角色：{role}", text)
            self.assertNotIn(app.BOT_TOKEN, text)
