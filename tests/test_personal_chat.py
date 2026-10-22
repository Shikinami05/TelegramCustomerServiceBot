import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_IDS", "1,2")

import app
from scripts import manage_webhook
from tg_bot import database, keyboards
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import conversations, deliveries, inbound, moderation
from tg_bot.services import spam_rules
from tg_bot.services.ai_settings import AISettingsController
from tg_bot.services.moderation import Verdict, process_job, snapshot


class PersonalChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.db"
        database.initialize(self.db)
        self.settings = replace(app.SETTINGS, ai_moderation_enabled=False, spam_rules_enabled=True)
        self.send = AsyncMock(return_value=TelegramSendResult(True, message_id=900))
        self.edit = AsyncMock(return_value=True)
        self.answer = AsyncMock(return_value=True)
        self.telegram = AsyncMock(return_value={"ok": True})
        for name, value in {
            "DB_PATH": self.db, "SETTINGS": self.settings, "ADMIN_IDS": {1, 2}, "OWNER_IDS": {1},
            "AI_MODERATION_ENABLED": False, "USER_RATE_LIMIT_COUNT": 100,
            "send_message": self.send, "edit_message_text": self.edit,
            "answer_callback_query": self.answer, "tg": self.telegram,
            "admin_delivery_wakeup": None, "moderation_wakeup": None,
            "ai_settings_controller": None, "admin_ui_locks": {},
        }.items():
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def message(self, user=50, message_id=10, text="hello", **extra):
        return {"from": {"id": user, "first_name": f"User {user}"},
                "chat": {"id": user, "type": "private"}, "message_id": message_id, "text": text, **extra}

    def receive(self, user=50, update=100, message_id=10):
        message = self.message(user, message_id)
        inbound.persist_event(self.db, update, message["from"], message["text"], user, message_id,
                              event_type="message", title="message", edited=False,
                              admin_ids={1, 2}, include_content_delivery=False)

    def callback(self, data, admin=1, message_id=700):
        return {"id": "callback", "from": {"id": admin}, "data": data,
                "message": {"message_id": message_id, "chat": {"id": admin, "type": "private"}}}

    def enqueue(self, text, update=100, user=50, **extra):
        message = self.message(user=user, text=text, **extra)
        moderation.enqueue(self.db, update, user, message["message_id"], snapshot(message), text, False)
        return moderation.claim_next(self.db)

    async def test_reply_completion_does_not_clear_a_new_arrival(self):
        self.receive()
        delivery = deliveries.claim_next_admin(self.db)
        deliveries.complete_admin(self.db, delivery, TelegramSendResult(True, message_id=700))

        async def copy(*args):
            self.receive(update=101, message_id=11)
            return TelegramSendResult(True, message_id=800)

        with patch.object(app, "copy_message", AsyncMock(side_effect=copy)):
            await app.handle_admin_message(self.message(user=1, message_id=701, text="reply",
                                                        reply_to_message={"message_id": 700}), 200)
        row = conversations.get(self.db, 50)
        self.assertEqual((row["last_read_seq"], row["unread_count"]), (1, 1))
        database.initialize(self.db)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertEqual(app.get_message_link(1, 701)["user_chat_id"], 50)

    async def test_failed_reply_does_not_mark_the_original_message_read(self):
        self.receive()
        delivery = deliveries.claim_next_admin(self.db)
        deliveries.complete_admin(self.db, delivery, TelegramSendResult(True, message_id=700))
        with patch.object(app, "copy_message", AsyncMock(return_value=TelegramSendResult(False, status_code=403))):
            await app.handle_admin_message(self.message(user=1, message_id=701,
                                                        reply_to_message={"message_id": 700}), 200)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertIn("屏蔽", self.send.await_args.args[1])

    async def test_old_mark_read_button_cannot_clear_a_new_arrival(self):
        self.receive()
        self.receive(update=101, message_id=11)
        await app.handle_callback(self.callback("read:50:1"))
        await app.handle_callback(self.callback("read:50:1"))
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertEqual(conversations.count_queue(self.db, "all"), 1)

    async def test_opening_contact_preserves_messages_arriving_during_display(self):
        self.receive()

        async def present(*args):
            self.receive(update=101, message_id=11)
            return True

        with patch.object(app, "present_admin_view", AsyncMock(side_effect=present)):
            await app.show_contact(1, 50, activate_reply=True)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertEqual(app.get_admin_state(1), 50)

    async def test_failed_contact_view_neither_marks_read_nor_selects_a_new_target(self):
        self.receive()
        app.set_admin_state(1, 60)
        with patch.object(app, "present_admin_view", AsyncMock(return_value=False)):
            await app.show_contact(1, 50, activate_reply=True)
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertIsNone(app.get_admin_state(1))

    async def test_same_contact_details_preserve_continuous_reply_and_show_id(self):
        self.receive()
        app.set_admin_state(1, 50)
        await app.handle_callback(self.callback("detail:50"))
        self.assertEqual(app.get_admin_state(1), 50)
        self.assertIn("ID：<code>50</code>", self.edit.await_args.args[2])
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 0)
        button = self.edit.await_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual((button["callback_data"], button["style"]), ("cancel:50", "danger"))

    async def test_opening_a_different_contact_has_an_explicit_new_target(self):
        self.receive(user=50)
        self.receive(user=60, update=101)
        app.set_admin_state(1, 50)
        await app.handle_callback(self.callback("user:60:all:1"))
        self.assertEqual(app.get_admin_state(1), 60)
        self.assertIn("User 60", self.edit.await_args.args[2])

    async def test_two_administrators_keep_independent_reply_targets(self):
        for user, admin in ((50, 1), (60, 2)):
            self.receive(user=user, update=100 + user)
            app.set_admin_state(admin, user)
        targets = []

        async def copy(chat_id, admin_id, message_id):
            await asyncio.sleep(0)
            targets.append((admin_id, chat_id))
            return TelegramSendResult(True, message_id=800 + admin_id)

        with patch.object(app, "copy_message", AsyncMock(side_effect=copy)):
            await asyncio.gather(app.handle_admin_message(self.message(user=1, message_id=701), 201),
                                 app.handle_admin_message(self.message(user=2, message_id=701), 202))
        self.assertCountEqual(targets, [(1, 50), (2, 60)])
        self.assertEqual(app.get_message_link(1, 701)["user_chat_id"], 50)
        self.assertEqual(app.get_message_link(2, 701)["user_chat_id"], 60)

    async def test_duplicate_admin_reply_does_not_send_or_notify_again(self):
        self.receive()
        app.set_admin_state(1, 50)
        message = self.message(user=1, message_id=701)
        with patch.object(app, "copy_message", AsyncMock(return_value=TelegramSendResult(True, message_id=800))) as copy:
            await app.handle_admin_message(message, 200)
            await app.handle_admin_message(message, 200)
        copy.assert_awaited_once()
        self.telegram.assert_awaited_once()
        self.assertEqual(self.telegram.await_args.args[0], "setMessageReaction")
        self.send.assert_not_awaited()

    async def test_failed_reaction_falls_back_to_a_short_status(self):
        self.receive()
        self.telegram.return_value = {"ok": False}
        await app.send_admin_reply_status(1, 50, "state", message_id=701)
        self.assertLess(len(self.send.await_args.args[1]), 100)
        self.assertNotIn("reply_markup", self.send.await_args.kwargs)

    async def test_removed_commands_and_old_buttons_cannot_execute_broadcasts(self):
        for command in ("/broadcast hello", "/broadcast_retry old", "/audit", "/takeover 50", "/reply 50 hello"):
            self.assertTrue(await app.handle_admin_command(1, command))
        for data in ("broadcast_confirm:old", "broadcast_retry:old", "resolve:50", "reopen:50", "takeover:50"):
            await app.handle_callback(self.callback(data))
        self.telegram.assert_not_awaited()
        self.assertIsNone(database.fetchone(self.db, "SELECT 1 FROM sqlite_master WHERE name='pending_broadcasts'"))

    async def test_non_admin_cannot_read_or_block_or_change_settings(self):
        self.receive()
        for data in ("read:50:1", "blacklist_confirm:50", "ai:rules_off", "settings:panel"):
            await app.handle_callback(self.callback(data, admin=99))
            self.assertEqual(self.answer.await_args.args[1], "无权限")
        self.assertEqual(conversations.get(self.db, 50)["unread_count"], 1)
        self.assertFalse(app.is_blacklisted(50))

    async def test_menu_and_welcome_keep_the_requested_entries(self):
        self.assertEqual({entry["command"] for entry in manage_webhook.ADMIN_COMMANDS},
                         {"start", "cancel", "myid", "settings"})
        await app.handle_admin_command(1, "/myid")
        self.assertIn("<code>1</code>", self.send.await_args.args[1])
        callbacks = {button["callback_data"] for row in keyboards.welcome_keyboard()["inline_keyboard"] for button in row}
        self.assertEqual(callbacks, {"user_help", "user_guide"})
        self.assertNotIn("<code>1</code>", app.format_admin_dashboard(1))

    async def test_chat_list_has_only_unread_and_all_and_keeps_pagination(self):
        for index in range(25):
            self.receive(user=500000 + index, update=100 + index)
        await app.show_conversation_queue(1, "all", 2)
        text = self.send.await_args.args[1]
        self.assertIn("11. ", text)
        self.assertIn("20. ", text)
        self.assertNotIn("<code>500", text)
        buttons = self.send.await_args.kwargs["reply_markup"]["inline_keyboard"]
        values = {button["callback_data"] for row in buttons for button in row}
        self.assertIn("queue:all:3", values)
        self.assertFalse(any("pending" in value or "closed" in value for value in values))

    async def test_notification_mark_read_boundary_is_the_delivered_event(self):
        self.receive()
        self.receive(update=101, message_id=11)
        delivery = deliveries.claim_next_admin(self.db)
        await app.process_admin_delivery(delivery)
        buttons = self.send.await_args.kwargs["reply_markup"]["inline_keyboard"]
        values = {button["callback_data"] for row in buttons for button in row}
        self.assertIn("read:50:1", values)
        self.assertNotIn("read:50:2", values)

    async def test_hash_ad_is_held_locally_without_ai_or_a_spam_notification(self):
        await app.handle_user_message(self.message(text="哈\u200b 希 竞猜，注册送金，联系 @example"), 100)
        job = moderation.claim_next(self.db)
        with patch("tg_bot.services.moderation.classify", AsyncMock()) as classify:
            async with httpx.AsyncClient() as client:
                self.assertFalse(await process_job(self.db, job, client, self.settings, {1, 2}))
        classify.assert_not_awaited()
        result = moderation.get(self.db, 100)
        self.assertEqual((result["status"], result["verdict"], result["provider"]), ("held", "spam", ""))
        self.send.reset_mock()
        await app.notify_moderation_pending()
        self.send.assert_not_awaited()
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM admin_deliveries"), [])

    async def test_ad_reports_and_ordinary_hash_discussions_are_not_keyword_blocked(self):
        for text in ("最近有人给我发哈希竞猜返佣广告，怎么屏蔽？", "什么是哈希算法？", "USDT 是什么意思？", "想和你聊聊合作"):
            self.assertIsNone(spam_rules.check(self.db, text))
        job = self.enqueue("有人给我发哈希竞猜返佣广告")
        settings = replace(self.settings, ai_moderation_enabled=True, deepseek_api_key="sk-test")
        with patch("tg_bot.services.moderation.classify", AsyncMock(return_value=Verdict("allow", "normal"))) as classify:
            async with httpx.AsyncClient() as client:
                self.assertTrue(await process_job(self.db, job, client, settings, {1, 2}))
        classify.assert_awaited_once()

    async def test_custom_domain_rules_match_hidden_links_but_not_lookalike_domains(self):
        spam_rules.replace_rules(self.db, "域名：ads.example.com\n词语：出售成品号")
        self.assertIsNotNone(spam_rules.check(self.db, "https://sub.ads.example.com/promo"))
        self.assertIsNone(spam_rules.check(self.db, "https://ads.example.com.evil.test"))
        self.assertIsNotNone(spam_rules.check(self.db, "ads.example.com"))
        self.assertIsNotNone(spam_rules.check(self.db, "https://ads.exa\u200bmple.com/path"))
        self.assertIsNone(spam_rules.check(self.db, "https://ads.example.com@ordinary.test"))
        message = self.message(text="点这里", entities=[{"type": "text_link", "offset": 0, "length": 3,
                                                          "url": "https://ads.example.com/path"}])
        from tg_bot.services.moderation import review_text
        self.assertIsNotNone(spam_rules.check(self.db, review_text(message)))

    async def test_invalid_rule_changes_are_atomic_and_never_compile_user_regex(self):
        spam_rules.replace_rules(self.db, "域名：ads.example.com")
        before = spam_rules.read_rules(self.db)
        for value in ("域名：https://ads.example.com/path", "词语：a", "词语：\u200b\u200b", "x" * 3001):
            with self.assertRaises(ValueError):
                spam_rules.replace_rules(self.db, value)
            self.assertEqual(spam_rules.read_rules(self.db), before)
        spam_rules.replace_rules(self.db, "词语：.*")
        self.assertIsNone(spam_rules.check(self.db, "ordinary message"))

    async def test_local_rules_can_be_disabled_and_normal_chat_still_delivers(self):
        job = self.enqueue("哈希竞猜，注册送金")
        settings = replace(self.settings, spam_rules_enabled=False)
        async with httpx.AsyncClient() as client:
            self.assertTrue(await process_job(self.db, job, client, settings, {1, 2}))
        self.assertEqual(moderation.get(self.db, 100)["status"], "approved")

    async def test_review_failure_is_not_spam_and_alerts_only_once(self):
        job = self.enqueue("hello")
        settings = replace(self.settings, ai_moderation_enabled=True, deepseek_api_key="sk-test")
        with patch("tg_bot.services.moderation.classify", AsyncMock(return_value=Verdict("uncertain", "timeout", True))):
            async with httpx.AsyncClient() as client:
                await process_job(self.db, job, client, settings, {1, 2})
        self.assertEqual(moderation.get(self.db, 100)["verdict"], "error")
        await app.notify_moderation_pending()
        await app.notify_moderation_pending()
        self.assertEqual(self.send.await_count, 2)  # One notification to each authorized administrator.
        self.assertFalse(app.is_blacklisted(50))

    async def test_false_positive_release_preserves_reply_mapping_and_restart(self):
        job = self.enqueue("哈希竞猜，注册送金")
        async with httpx.AsyncClient() as client:
            await process_job(self.db, job, client, self.settings, {1, 2})
        self.assertTrue(moderation.approve(self.db, 100, {1, 2}, admin_id=1))
        delivery = deliveries.claim_next_admin(self.db)
        deliveries.complete_admin(self.db, delivery, TelegramSendResult(True, message_id=700))
        database.initialize(self.db)
        self.assertEqual(app.get_message_link(1, 700)["user_chat_id"], 50)
        self.assertFalse(moderation.approve(self.db, 100, {1, 2}, admin_id=1))

    async def test_blacklisting_cancels_pending_work_and_unblock_is_reversible(self):
        self.receive()
        app.set_admin_state(1, 50)
        job = self.enqueue("hello", update=101)
        await app.handle_callback(self.callback("blacklist_confirm:50"))
        self.assertTrue(app.is_blacklisted(50))
        self.assertIsNone(app.get_admin_state(1))
        self.assertEqual(moderation.get(self.db, job["update_id"])["status"], "blocked")
        self.assertEqual(conversations.count_queue(self.db, "inbox"), 0)
        self.assertEqual(conversations.count_queue(self.db, "all"), 1)
        self.assertIsNone(deliveries.claim_next_admin(self.db))
        await app.handle_callback(self.callback("unblacklist:50"))
        self.assertFalse(app.is_blacklisted(50))

    async def test_custom_rule_editor_is_owner_only_and_never_forwards_input(self):
        env = Path(self.temp.name) / ".env"
        env.write_text("BOT_TOKEN=test-token\nWEBHOOK_SECRET=test-secret\nADMIN_IDS=1\n", encoding="utf-8")
        controller = AISettingsController(lambda: self.db, lambda: env, lambda: self.settings,
            lambda settings: None, self.send, self.telegram, lambda admin: admin == 1,
            lambda admin: None, AsyncMock())
        await controller.callback(2, "rules_off")
        self.assertNotIn("SPAM_RULES_ENABLED", env.read_text(encoding="utf-8"))
        await controller.callback(1, "rules_input", self.callback("ai:rules_input"))
        self.assertTrue(await controller.message(self.message(user=1, message_id=901, text="词语：出售成品号")))
        self.assertEqual(spam_rules.read_rules(self.db), [("phrase", "出售成品号")])
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM message_logs"), [])

    async def test_rule_switch_persists_without_touching_existing_credentials(self):
        from dotenv import dotenv_values
        env = Path(self.temp.name) / ".env"
        env.write_text("BOT_TOKEN=test-token\nWEBHOOK_SECRET=test-secret\nADMIN_IDS=1\n", encoding="utf-8")
        applied = []
        controller = AISettingsController(lambda: self.db, lambda: env, lambda: self.settings,
            applied.append, self.send, self.telegram, lambda admin: admin == 1,
            lambda admin: None, AsyncMock())
        await controller.callback(1, "rules_off")
        self.assertFalse(applied[-1].spam_rules_enabled)
        values = dotenv_values(env)
        self.assertEqual(values["SPAM_RULES_ENABLED"], "false")
        self.assertEqual(values["BOT_TOKEN"], "test-token")
        self.assertEqual(values["WEBHOOK_SECRET"], "test-secret")

    async def test_rule_editor_rejects_credentials_and_bounds_long_rule_views(self):
        env = Path(self.temp.name) / ".env"
        env.write_text("BOT_TOKEN=test-token\nWEBHOOK_SECRET=test-secret\nADMIN_IDS=1\n", encoding="utf-8")
        present = AsyncMock()
        controller = AISettingsController(lambda: self.db, lambda: env, lambda: self.settings,
            lambda settings: None, self.send, self.telegram, lambda admin: admin == 1,
            lambda admin: None, present)
        await controller.callback(1, "rules_input", self.callback("ai:rules_input"))
        text = "词语：sk-do-not-store-this-key"
        self.assertTrue(await controller.message(self.message(user=1, message_id=901, text=text)))
        self.assertEqual(spam_rules.read_rules(self.db), [])
        self.assertNotIn(text, str(database.fetchall(self.db, "SELECT * FROM admin_audit_logs")))
        spam_rules.replace_rules(self.db, "\n".join("词语：" + "<" * 75 + str(index) for index in range(30)))
        await controller.callback(1, "rules")
        self.assertLessEqual(len(present.await_args.args[1]), 4096)

    async def test_successive_contact_selections_are_serialized(self):
        self.receive(user=50)
        self.receive(user=60, update=101)
        displayed = []

        async def edit(chat_id, message_id, text, **kwargs):
            await asyncio.sleep(0)
            displayed.append(text)
            return True

        self.edit.side_effect = edit
        await asyncio.gather(app.handle_callback(self.callback("user:50:all:1")),
                             app.handle_callback(self.callback("user:60:all:1")))
        self.assertEqual(app.get_admin_state(1), 60)
        self.assertIn("User 50", displayed[0])
        self.assertIn("User 60", displayed[1])

    async def test_api_errors_are_not_hidden_by_the_spam_filter(self):
        job = self.enqueue("哈希是什么意思？")
        settings = replace(self.settings, ai_moderation_enabled=True, deepseek_api_key="sk-test")
        transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"error": "rate limited"}))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertFalse(await process_job(self.db, job, client, settings, {1, 2}))
        row = moderation.get(self.db, 100)
        self.assertEqual(row["verdict"], "error")
        self.assertEqual(row["status"], "held")
        self.assertNotIn("rate limited", row["reason"])


class PersonalChatMigrationTests(unittest.TestCase):
    def test_legacy_unread_counts_are_preserved_and_read_cursor_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "old.db"
            with database.connect(db) as conn:
                conn.execute("CREATE TABLE conversations(chat_id INTEGER PRIMARY KEY, owner_admin_id INTEGER, "
                             "status TEXT DEFAULT 'open', unread_count INTEGER DEFAULT 0, last_user_message_at DATETIME, "
                             "last_admin_reply_at DATETIME,updated_at DATETIME,closed_at DATETIME)")
                conn.execute("INSERT INTO conversations(chat_id,unread_count) VALUES(50,3)")
                conn.commit()
            database.initialize(db)
            self.assertEqual(conversations.get(db, 50)["last_inbound_seq"], 3)
            conversations.mark_read(db, 50, 2)
            database.initialize(db)
            self.assertEqual(conversations.get(db, 50)["unread_count"], 1)
            self.assertEqual(conversations.get(db, 50)["last_read_seq"], 2)

    def test_old_broadcast_jobs_are_retired_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "old.db"
            with database.connect(db) as conn:
                conn.execute("CREATE TABLE pending_broadcasts(id TEXT PRIMARY KEY,status TEXT,content TEXT)")
                conn.execute("CREATE TABLE broadcast_recipients(broadcast_id TEXT,chat_id INTEGER,status TEXT)")
                conn.executemany("INSERT INTO pending_broadcasts VALUES(?,?,?)",
                                 [("old", "completed", "history"), ("new", "queued", "do not send")])
                conn.executemany("INSERT INTO broadcast_recipients VALUES('new',?,?)", [(50, "pending"), (60, "sending")])
                conn.commit()
            database.initialize(db)
            self.assertEqual(database.fetchone(db, "SELECT content FROM pending_broadcasts WHERE id='old'")[0], "history")
            self.assertEqual(database.fetchone(db, "SELECT status FROM pending_broadcasts WHERE id='new'")[0], "canceled")
            self.assertEqual({row[0] for row in database.fetchall(db, "SELECT status FROM broadcast_recipients")}, {"canceled", "unknown"})
