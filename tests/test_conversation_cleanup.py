import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_IDS", "1,2")

import app
from tg_bot import database
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import conversations, inbound


class ConversationCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "bot.db"
        database.initialize(self.db)
        self.present = AsyncMock(return_value=True)
        self.answer = AsyncMock(return_value=True)
        for key, value in {"DB_PATH": self.db, "ADMIN_IDS": {1, 2}, "OWNER_IDS": {1},
                           "present_admin_view": self.present, "answer_callback_query": self.answer,
                           "admin_ui_locks": {}}.items():
            patcher = patch.object(app, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def receive(self, user=50, update=100, message_id=10):
        inbound.persist_event(self.db, update, {"id": user, "first_name": f"User {user}"},
                              "private test message", user, message_id, event_type="message",
                              title="message", edited=False, admin_ids={1, 2}, include_content_delivery=False)

    def callback(self, data, admin=1):
        return {"id": "callback", "from": {"id": admin}, "data": data,
                "message": {"message_id": 900, "chat": {"id": admin, "type": "private"}}}

    async def test_owner_entry_is_only_on_all_list_and_requires_confirmation(self):
        self.receive()
        for admin, queue, visible in ((1, "all", True), (1, "inbox", False), (2, "all", False)):
            await app.show_conversation_queue(admin, queue)
            callbacks = {button["callback_data"] for row in self.present.await_args.args[2]["inline_keyboard"] for button in row}
            self.assertEqual("cleanup:all" in callbacks, visible)
        app.set_admin_state(1, 50)
        await app.handle_callback(self.callback("cleanup:all"))
        self.assertIsNone(app.get_admin_state(1))
        self.assertIsNone(conversations.get(self.db, 50)["archived_at"])
        text, markup = self.present.await_args.args[1:3]
        self.assertIn("Telegram 訊息都會保留", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["style"], "danger")
        self.assertLessEqual(len(markup["inline_keyboard"][0][0]["callback_data"].encode()), 64)
        await app.handle_callback(self.callback(markup["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(conversations.count_queue(self.db, "all"), 0)

    async def test_confirmation_survives_restart_and_preserves_history_mapping_and_blocks(self):
        self.receive()
        self.receive(user=60, update=101)
        app.set_admin_state(1, 50)
        app.set_admin_state(2, 60)
        app.blacklist_user(60, 1, "test")
        app.add_message_link(50, 10, 1, 700, "user_to_admin", "notification")
        token, count = conversations.prepare_cleanup(self.db, 1, now=100)
        self.assertEqual(count, 2)
        database.initialize(self.db)
        result = conversations.finish_cleanup(self.db, 1, token, now=101)
        self.assertEqual(result, conversations.CleanupResult("archived", 2, 0))
        self.assertEqual(conversations.count_queue(self.db, "all"), 0)
        self.assertEqual(database.fetchone(self.db, "SELECT COUNT(*) FROM message_logs")[0], 2)
        self.assertEqual(app.get_message_link(1, 700)["user_chat_id"], 50)
        self.assertTrue(app.is_blacklisted(60))
        self.assertIsNone(app.get_admin_state(1))
        self.assertIsNone(app.get_admin_state(2))
        snapshot = database.fetchone(self.db, "SELECT snapshot FROM conversation_cleanups")[0]
        self.assertNotIn("private", snapshot)
        self.receive(update=102, message_id=11)
        self.assertEqual(conversations.count_queue(self.db, "inbox"), 1)

    async def test_changed_and_new_conversations_are_not_deleted_or_marked_read(self):
        self.receive()
        self.receive(user=60, update=101)
        token, _ = conversations.prepare_cleanup(self.db, 1, now=100)
        self.receive(user=60, update=102, message_id=11)
        self.receive(user=70, update=103)
        app.set_admin_state(2, 60)
        result = conversations.finish_cleanup(self.db, 1, token, now=101)
        self.assertEqual(result, conversations.CleanupResult("archived", 1, 2))
        self.assertIsNotNone(conversations.get(self.db, 50)["archived_at"])
        self.assertEqual(conversations.get(self.db, 60)["unread_count"], 2)
        self.assertEqual(conversations.get(self.db, 70)["unread_count"], 1)
        self.assertEqual(app.get_admin_state(2), 60)

    async def test_expired_canceled_replaced_and_cross_admin_tokens_do_not_delete(self):
        self.receive()
        token, _ = conversations.prepare_cleanup(self.db, 1, now=100)
        self.assertEqual(conversations.finish_cleanup(self.db, 2, token, now=101).status, "missing")
        self.assertEqual(conversations.finish_cleanup(self.db, 1, token, now=280).status, "expired")
        newer, _ = conversations.prepare_cleanup(self.db, 1, now=101)
        self.assertEqual(conversations.finish_cleanup(self.db, 1, token, now=102).status, "used")
        self.assertEqual(conversations.finish_cleanup(self.db, 1, newer, cancel=True, now=102).status, "canceled")
        self.assertEqual(conversations.finish_cleanup(self.db, 1, newer, now=103).status, "used")
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertIsNone(conversations.get(self.db, 50)["archived_at"])

    async def test_duplicate_concurrent_confirmation_is_consumed_only_once(self):
        self.receive()
        token, _ = conversations.prepare_cleanup(self.db, 1, now=100)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: conversations.finish_cleanup(self.db, 1, token, now=101).status, range(2)))
        self.assertCountEqual(results, ["archived", "used"])
        self.assertEqual(database.fetchone(self.db, "SELECT COUNT(*) FROM admin_audit_logs WHERE action='conversation_archive_all'")[0], 1)

    async def test_callback_rejects_nonowner_group_and_fabricated_tokens(self):
        self.receive()
        for data in ("cleanup:all", "cleanup:confirm:" + "A" * 16):
            await app.handle_callback(self.callback(data, 2))
            self.assertIn("僅負責人", self.answer.await_args.args[1])
            await app.handle_callback(self.callback(data, 99))
            self.assertEqual(self.answer.await_args.args[1], "無權限")
        for token in ("a", "字" * 16, "x" * 17, "!" * 16, "A" * 16):
            await app.handle_callback(self.callback("cleanup:confirm:" + token))
        callback = self.callback("cleanup:all")
        callback["message"]["chat"] = {"id": -100, "type": "supergroup"}
        await app.handle_callback(callback)
        self.assertEqual(conversations.count_queue(self.db, "all"), 1)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)

    async def test_cancel_and_new_arrival_during_telegram_ack_are_safe(self):
        self.receive()
        token, _ = conversations.prepare_cleanup(self.db, 1)
        await app.handle_callback(self.callback(f"cleanup:cancel:{token}"))
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        token, _ = conversations.prepare_cleanup(self.db, 1)

        async def answer(*args):
            self.receive(update=101, message_id=11)
            return True

        self.answer.side_effect = answer
        await app.handle_callback(self.callback(f"cleanup:confirm:{token}"))
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertIsNone(conversations.get(self.db, 50)["archived_at"])
