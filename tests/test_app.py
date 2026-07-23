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
        self.assertEqual(message["text"], "legacy message")
        self.assertEqual(update["status"], "done")
        self.assertIsNotNone(update["updated_at"])
        self.assertIn("sent_count", broadcast_columns)

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

    def test_html_escape_limit_never_exceeds_budget(self) -> None:
        escaped = app.escape_html_limited("&" * 500, 100)
        self.assertLessEqual(len(escaped), 100)

    def test_command_normalization_and_welcome_positioning(self) -> None:
        self.assertEqual(app.normalize_command_text("/START@ExampleBot"), "/start")
        self.assertEqual(
            app.normalize_command_text("/reply@ExampleBot 123 hello"),
            "/reply 123 hello",
        )
        self.assertEqual(
            app.WELCOME_TEXT,
            "你好，这里是统一留言聊天入口。\n\n"
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
