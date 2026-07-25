import asyncio
import logging
import os
import sqlite3
import stat
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_IDS", "1,2")

import app

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
    category=Warning,
)
from fastapi.testclient import TestClient


class BotDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp_dir.name) / "test.db"
        app.DB_BACKUP_DIR = Path(self.temp_dir.name) / "backups"
        app.MESSAGE_RETENTION_DAYS = 0
        app.USER_RATE_LIMIT_COUNT = 2
        app.USER_RATE_LIMIT_WINDOW_SECONDS = 60
        app.USER_RATE_LIMIT_COOLDOWN_SECONDS = 300
        app.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_failed_update_can_retry_but_done_update_cannot(self) -> None:
        self.assertTrue(app.claim_update(100))
        self.assertFalse(app.claim_update(100))

        app.fail_update(100, "temporary failure")
        self.assertTrue(app.claim_update(100))

        app.finish_update(100)
        self.assertFalse(app.claim_update(100))

    def test_legacy_schema_migrates_without_data_loss(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{app.DB_PATH}{suffix}").unlink(missing_ok=True)

        conn = sqlite3.connect(app.DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE message_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    sender_type TEXT NOT NULL,
                    sender_id INTEGER NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE pending_broadcasts (
                    id TEXT PRIMARY KEY,
                    admin_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO message_logs (chat_id, sender_type, sender_id, text) "
                "VALUES (1, 'user', 1, 'legacy message')"
            )
            conn.execute("INSERT INTO processed_updates (update_id) VALUES (99)")
            conn.commit()
        finally:
            conn.close()

        app.init_db()

        message = app.db_fetchone("SELECT text FROM message_logs WHERE chat_id = 1")
        update = app.db_fetchone(
            "SELECT status, updated_at FROM processed_updates WHERE update_id = 99"
        )
        broadcast_columns = {
            row["name"] for row in app.db_fetchall("PRAGMA table_info(pending_broadcasts)")
        }
        conversation_columns = {
            row["name"] for row in app.db_fetchall("PRAGMA table_info(conversations)")
        }
        self.assertEqual(message["text"], "legacy message")
        self.assertEqual(update["status"], "done")
        self.assertIsNotNone(update["updated_at"])
        self.assertIn("sent_count", broadcast_columns)
        self.assertIn("unread_count", conversation_columns)
        self.assertIn("last_user_message_at", conversation_columns)
        self.assertIsNotNone(
            app.db_fetchone(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'admin_audit_logs'"
            )
        )

    def test_rate_limit_only_notifies_when_cooldown_starts(self) -> None:
        self.assertEqual(app.check_user_rate_limit(10), (True, False, 0))
        self.assertEqual(app.check_user_rate_limit(10), (True, False, 0))

        allowed, should_notify, retry_after = app.check_user_rate_limit(10)
        self.assertFalse(allowed)
        self.assertTrue(should_notify)
        self.assertEqual(retry_after, 300)

        allowed, should_notify, retry_after = app.check_user_rate_limit(10)
        self.assertFalse(allowed)
        self.assertFalse(should_notify)
        self.assertGreater(retry_after, 0)

    def test_edited_message_updates_history_and_current_message_can_be_excluded(self) -> None:
        app.add_message_log(20, "user", 20, "first", telegram_message_id=1)
        app.add_message_log(20, "user", 20, "old text", telegram_message_id=2)

        self.assertTrue(app.update_user_message_log(20, 2, "new text"))
        previous_history = app.format_history(20, exclude_message_id=2)
        full_history = app.format_history(20)

        self.assertIn("first", previous_history)
        self.assertNotIn("new text", previous_history)
        self.assertIn("new text", full_history)
        self.assertIn("已编辑", full_history)

    def test_takeover_clears_previous_admin_reply_state(self) -> None:
        app.claim_conversation(30, 1)
        app.set_admin_state(1, 30)

        app.claim_conversation(30, 2)
        app.set_admin_state(2, 30)

        self.assertIsNone(app.get_admin_state(1))
        self.assertEqual(app.get_admin_state(2), 30)
        self.assertEqual(app.get_conversation_owner(30), 2)

    def test_conversation_inbox_pending_and_reopen_workflow(self) -> None:
        app.db_execute(
            "INSERT INTO users (chat_id, first_name, last_message) VALUES (?, ?, ?)",
            (31, "Inbox", "hello"),
        )
        app.record_user_activity(31)
        app.record_user_activity(31)

        conversation = app.get_conversation(31)
        self.assertEqual(int(conversation["unread_count"]), 2)
        self.assertEqual(len(app.get_conversation_queue("inbox")), 1)

        app.mark_conversation_replied(31)
        self.assertEqual(app.get_conversation_queue("inbox"), [])

        app.record_user_activity(31, increment_unread=False)
        app.db_execute(
            "UPDATE conversations "
            "SET last_user_message_at = datetime('now', '-60 minutes') "
            "WHERE chat_id = ?",
            (31,),
        )
        self.assertEqual(len(app.get_conversation_queue("pending")), 1)

        app.close_conversation(31)
        self.assertEqual(len(app.get_conversation_queue("closed")), 1)
        app.reopen_conversation(31)
        reopened = app.get_conversation(31)
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(int(reopened["unread_count"]), 0)

    def test_admin_dashboard_and_numbered_queue_controls(self) -> None:
        app.db_execute(
            """
            INSERT INTO users (
                chat_id, username, first_name, last_message
            ) VALUES (?, ?, ?, ?)
            """,
            (32, "queue_user", "Queue", "latest message"),
        )
        app.record_user_activity(32)
        app.db_execute(
            """
            UPDATE conversations
            SET last_user_message_at = datetime('now', '-60 minutes')
            WHERE chat_id = ?
            """,
            (32,),
        )
        app.claim_conversation(32, 2)

        app.db_execute(
            "INSERT INTO users (chat_id, first_name) VALUES (?, ?)",
            (33, "Closed"),
        )
        app.close_conversation(33)

        counts = app.get_queue_counts()
        self.assertEqual(counts, {"inbox": 1, "pending": 1, "closed": 1})
        dashboard = app.format_admin_dashboard(1)
        self.assertIn("<b>留言工作台</b>", dashboard)
        self.assertIn("待处理：<b>1</b>", dashboard)

        rows = app.get_conversation_queue("inbox")
        queue_text = app.format_conversation_queue("inbox", rows)
        queue_keyboard = app.conversation_queue_keyboard(
            rows,
            "inbox",
            viewer_admin_id=1,
        )
        first_row = queue_keyboard["inline_keyboard"][0]

        self.assertIn("<b>1. @queue_user</b>", queue_text)
        self.assertIn("1 条待处理", queue_text)
        self.assertEqual(first_row[0]["text"], "1 接管")
        self.assertEqual(first_row[0]["callback_data"], "takeover:32")
        self.assertEqual(first_row[0]["style"], "primary")
        self.assertEqual(first_row[1]["text"], "1 详情")
        self.assertEqual(first_row[2]["text"], "1 处理")
        self.assertEqual(first_row[2]["style"], "success")
        self.assertEqual(
            queue_keyboard["inline_keyboard"][-1][0]["callback_data"],
            "admin:dashboard",
        )

    def test_queue_callback_reuses_the_current_message(self) -> None:
        callback = {
            "id": "queue-callback",
            "from": {"id": 1},
            "data": "queue:inbox",
            "message": {
                "message_id": 50,
                "chat": {"id": 1, "type": "private"},
            },
        }
        answer = AsyncMock(return_value=True)
        edit = AsyncMock(return_value=True)
        send = AsyncMock(return_value=True)
        with (
            patch.object(app, "answer_callback_query", answer),
            patch.object(app, "edit_message_text", edit),
            patch.object(app, "send_message", send),
        ):
            asyncio.run(app.handle_callback(callback))

        answer.assert_awaited_once_with("queue-callback")
        edit.assert_awaited_once()
        self.assertIn("<b>待处理</b>", edit.await_args.args[2])
        send.assert_not_awaited()

    def test_admin_ui_views_fit_telegram_limits(self) -> None:
        for chat_id in range(70, 80):
            app.db_execute(
                """
                INSERT INTO users (
                    chat_id, username, last_message
                ) VALUES (?, ?, ?)
                """,
                (chat_id, f"user_{chat_id}", "<long>" * 1000),
            )
            app.record_user_activity(chat_id)

        rows = app.get_conversation_queue("inbox")
        queue_text = app.format_conversation_queue("inbox", rows)
        queue_keyboard = app.conversation_queue_keyboard(
            rows,
            "inbox",
            viewer_admin_id=1,
        )
        recent_text = app.format_recent_users(app.get_recent_users())

        self.assertLessEqual(len(queue_text), app.TELEGRAM_TEXT_LIMIT)
        self.assertLessEqual(len(recent_text), app.TELEGRAM_TEXT_LIMIT)
        for keyboard_row in queue_keyboard["inline_keyboard"]:
            for button in keyboard_row:
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)

    def test_blacklist_button_requires_confirmation(self) -> None:
        callback = {
            "id": "blacklist-callback",
            "from": {"id": 1},
            "data": "blacklist:55",
            "message": {
                "message_id": 60,
                "chat": {"id": 1, "type": "private"},
            },
        }
        answer = AsyncMock(return_value=True)
        send = AsyncMock(return_value=True)
        with (
            patch.object(app, "answer_callback_query", answer),
            patch.object(app, "send_message", send),
        ):
            asyncio.run(app.handle_callback(callback))

        self.assertFalse(app.is_blacklisted(55))
        confirmation_markup = send.await_args.kwargs["reply_markup"]
        self.assertEqual(
            confirmation_markup["inline_keyboard"][0][0]["callback_data"],
            "blacklist_confirm:55",
        )
        self.assertEqual(
            confirmation_markup["inline_keyboard"][0][0]["style"],
            "danger",
        )

        callback["id"] = "blacklist-confirm"
        callback["data"] = "blacklist_confirm:55"
        with (
            patch.object(app, "answer_callback_query", answer),
            patch.object(app, "send_message", send),
        ):
            asyncio.run(app.handle_callback(callback))

        self.assertTrue(app.is_blacklisted(55))

    def test_broadcast_queue_snapshots_users_and_tracks_results(self) -> None:
        for chat_id in (40, 41, 42):
            app.db_execute(
                "INSERT INTO users (chat_id, first_name) VALUES (?, ?)",
                (chat_id, f"user-{chat_id}"),
            )
        app.blacklist_user(42, 1, "blocked")

        broadcast_id = app.create_pending_broadcast(1, "hello")
        status, total = app.queue_broadcast(broadcast_id, 1)
        self.assertEqual((status, total), ("queued", 2))

        job = app.claim_next_broadcast_job()
        self.assertIsNotNone(job)
        recipients = []
        while True:
            chat_id = app.claim_next_broadcast_recipient(broadcast_id)
            if chat_id is None:
                break
            recipients.append(chat_id)
            app.finish_broadcast_recipient(
                broadcast_id, chat_id, sent=chat_id == 40, error="failed"
            )

        result = app.complete_broadcast(broadcast_id)
        self.assertEqual(sorted(recipients), [40, 41])
        self.assertIsNotNone(result)
        self.assertEqual(int(result["sent_count"]), 1)
        self.assertEqual(int(result["failed_count"]), 1)

        retry_status, retry_count = app.retry_failed_broadcast(broadcast_id)
        self.assertEqual((retry_status, retry_count), ("queued", 1))
        retried_job = app.claim_next_broadcast_job()
        self.assertEqual(retried_job["id"], broadcast_id)
        self.assertEqual(app.claim_next_broadcast_recipient(broadcast_id), 41)

    def test_html_escape_limit_never_exceeds_budget(self) -> None:
        escaped = app.escape_html_limited("&" * 500, 100)
        self.assertLessEqual(len(escaped), 100)

    def test_inline_button_styles_are_semantic_and_validated(self) -> None:
        welcome_buttons = app.welcome_keyboard()["inline_keyboard"][0]
        self.assertEqual(welcome_buttons[0]["style"], "primary")
        self.assertNotIn("style", welcome_buttons[1])

        reply_buttons = app.exit_reply_keyboard(123)["inline_keyboard"][0]
        self.assertEqual(reply_buttons[0]["text"], "退出回复")
        self.assertEqual(reply_buttons[0]["style"], "danger")
        self.assertEqual(reply_buttons[1]["style"], "success")

        with self.assertRaises(ValueError):
            app.inline_keyboard([[("Invalid", "invalid:1", "neon")]])
        with self.assertRaises(ValueError):
            app.inline_keyboard([[("Too", "many", "primary", "parts")]])

    def test_broadcast_confirmation_uses_primary_and_danger_styles(self) -> None:
        send = AsyncMock(return_value=True)
        with patch.object(app, "send_message", send):
            handled = asyncio.run(
                app.handle_admin_command(1, "/broadcast planned maintenance")
            )

        self.assertTrue(handled)
        buttons = send.await_args.kwargs["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(buttons[0]["style"], "primary")
        self.assertEqual(buttons[1]["text"], "取消群发")
        self.assertEqual(buttons[1]["style"], "danger")

    def test_command_normalization_and_welcome_positioning(self) -> None:
        self.assertEqual(app.normalize_command_text("/START@ExampleBot"), "/start")
        self.assertEqual(
            app.normalize_command_text("/reply@ExampleBot 123 hello"),
            "/reply 123 hello",
        )
        self.assertEqual(
            app.WELCOME_TEXT,
            "<b>统一留言聊天入口</b>\n\n"
            "请直接在这里发送消息，我看到后会通过 Bot 回复你。",
        )

    def test_telegram_http_errors_do_not_expose_bot_token(self) -> None:
        response = httpx.Response(
            403,
            json={"ok": False, "description": "Forbidden: bot was blocked"},
            request=httpx.Request(
                "POST",
                "https://api.telegram.org/bottest-token/sendMessage",
            ),
        )
        client = AsyncMock()
        client.post.return_value = response
        previous_client = app.telegram_client
        app.telegram_client = client
        try:
            with self.assertRaises(RuntimeError) as context:
                asyncio.run(app.tg("sendMessage", {"chat_id": 1, "text": "hello"}))
        finally:
            app.telegram_client = previous_client

        error = str(context.exception)
        self.assertNotIn("test-token", error)
        self.assertNotIn("api.telegram.org", error)
        self.assertIn("Forbidden: bot was blocked", error)
        self.assertTrue(logging.getLogger("httpx").disabled)
        self.assertTrue(logging.getLogger("httpcore").disabled)

    def test_telegram_rate_limit_uses_retry_after(self) -> None:
        rate_limit_error = app.TelegramAPIError(
            "sendMessage",
            429,
            "Too Many Requests",
            retry_after=2,
        )
        tg_mock = AsyncMock(side_effect=[rate_limit_error, {"ok": True}])
        sleep_mock = AsyncMock()
        with (
            patch.object(app, "tg", tg_mock),
            patch.object(app.asyncio, "sleep", sleep_mock),
        ):
            sent = asyncio.run(app.send_message(1, "hello"))

        self.assertTrue(sent)
        self.assertEqual(tg_mock.await_count, 2)
        sleep_mock.assert_awaited_once_with(2)

    def test_refreshing_unchanged_admin_view_is_not_an_error(self) -> None:
        tg_mock = AsyncMock(
            side_effect=app.TelegramAPIError(
                "editMessageText",
                400,
                "Bad Request: message is not modified",
            )
        )
        with patch.object(app, "tg", tg_mock):
            edited = asyncio.run(
                app.edit_message_text(
                    1,
                    50,
                    "<b>留言工作台</b>",
                    reply_markup=app.inline_keyboard(
                        [[("刷新", "admin:dashboard")]]
                    ),
                )
            )

        self.assertTrue(edited)

    def test_owner_only_commands_and_audit_log(self) -> None:
        previous_owners = app.OWNER_IDS
        app.OWNER_IDS = {1}
        send_mock = AsyncMock(return_value=True)
        try:
            with patch.object(app, "send_message", send_mock):
                handled = asyncio.run(
                    app.handle_admin_command(2, "/broadcast forbidden")
                )
            self.assertTrue(handled)
            self.assertIn("OWNER_IDS", send_mock.await_args.args[1])

            send_mock.reset_mock()
            with patch.object(app, "send_message", send_mock):
                handled = asyncio.run(app.handle_admin_command(1, "/broadcast"))
            self.assertTrue(handled)
            self.assertEqual(send_mock.await_args.args[1], "格式：/broadcast 内容")

            app.add_admin_audit(1, "conversation_takeover", 55, "test")
            row = app.db_fetchone(
                "SELECT admin_id, action, target_chat_id "
                "FROM admin_audit_logs ORDER BY id DESC LIMIT 1"
            )
            self.assertEqual(int(row["admin_id"]), 1)
            self.assertEqual(row["action"], "conversation_takeover")
            self.assertEqual(int(row["target_chat_id"]), 55)
        finally:
            app.OWNER_IDS = previous_owners

    def test_non_admin_cannot_execute_management_actions(self) -> None:
        callback = {
            "id": "callback-1",
            "from": {"id": 999},
            "data": "blacklist:55",
            "message": {"chat": {"id": 999, "type": "private"}},
        }
        answer = AsyncMock(return_value=True)
        with patch.object(app, "answer_callback_query", answer):
            asyncio.run(app.handle_callback(callback))

        self.assertFalse(app.is_blacklisted(55))
        self.assertFalse(
            asyncio.run(app.handle_admin_command(999, "/blacklist 55 forged"))
        )
        answer.assert_awaited_once_with("callback-1", "无权限")

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes required")
    def test_database_and_backups_are_private(self) -> None:
        previous_enabled = app.DB_BACKUP_ENABLED
        app.DB_BACKUP_ENABLED = True
        try:
            backup_path = app.backup_database()
        finally:
            app.DB_BACKUP_ENABLED = previous_enabled

        self.assertIsNotNone(backup_path)
        self.assertEqual(stat.S_IMODE(app.DB_PATH.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(app.DB_BACKUP_DIR.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)

    def test_webhook_secret_health_and_update_retry_state(self) -> None:
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        with TestClient(app.app) as client:
            denied = client.post("/tg/webhook", json={"update_id": 200})
            self.assertEqual(denied.status_code, 403)

            first = client.post(
                "/tg/webhook", json={"update_id": 201}, headers=headers
            )
            duplicate = client.post(
                "/tg/webhook", json={"update_id": 201}, headers=headers
            )
            health = client.get("/healthz")

            self.assertEqual(first.status_code, 200)
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["ok"])

            failed = client.post(
                "/tg/webhook",
                json={"update_id": 202, "callback_query": {}},
                headers=headers,
            )
            retried = client.post(
                "/tg/webhook",
                json={"update_id": 202, "callback_query": {}},
                headers=headers,
            )
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(retried.status_code, 500)

        row = app.db_fetchone(
            "SELECT status, attempts FROM processed_updates WHERE update_id = 202"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(int(row["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
