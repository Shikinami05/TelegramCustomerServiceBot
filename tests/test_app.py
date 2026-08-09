import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
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

    @staticmethod
    def telegram_init_data(chat_id: int, auth_date: int | None = None) -> str:
        fields = {
            "auth_date": str(auth_date or int(time.time())),
            "query_id": "test-query",
            "user": json.dumps(
                {"id": chat_id, "first_name": "Verified"},
                separators=(",", ":"),
            ),
        }
        data_check_string = "\n".join(
            f"{key}={fields[key]}" for key in sorted(fields)
        )
        secret_key = hmac.new(
            b"WebAppData",
            app.BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        fields["hash"] = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return urlencode(fields)

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

    def test_turnstile_init_data_and_verification_expiry(self) -> None:
        init_data = self.telegram_init_data(12)
        self.assertEqual(app.validate_telegram_init_data(init_data), 12)

        with self.assertRaises(ValueError):
            app.validate_telegram_init_data(init_data.replace("Verified", "Forged"))
        with self.assertRaises(ValueError):
            app.validate_telegram_init_data(
                self.telegram_init_data(
                    12,
                    auth_date=int(time.time())
                    - app.TURNSTILE_INIT_DATA_MAX_AGE_SECONDS
                    - 1,
                )
            )

        with patch.object(app, "TURNSTILE_ENABLED", True):
            self.assertFalse(app.is_user_verified(12))
            app.mark_user_verified(12)
            self.assertTrue(app.is_user_verified(12))
            app.db_execute(
                "UPDATE user_verifications "
                "SET expires_at = datetime('now', '-1 second') "
                "WHERE chat_id = ?",
                (12,),
            )
            self.assertFalse(app.is_user_verified(12))

    def test_turnstile_siteverify_checks_hostname_and_action(self) -> None:
        self.assertEqual(
            app.turnstile_verify_hostname(
                "https://bot.example.com:8443/verify"
            ),
            "bot.example.com",
        )
        for invalid_url in (
            "http://bot.example.com/verify",
            "https://user@bot.example.com/verify",
            "https://bot.example.com/other",
            "https://bot.example.com/verify?next=/",
            "https://bot.example.com:invalid/verify",
            "https://bot.example.com:0/verify",
            "https://bot example.com/verify",
            "https://-bot.example.com/verify",
        ):
            with self.subTest(invalid_url=invalid_url):
                with self.assertRaises(ValueError):
                    app.turnstile_verify_hostname(invalid_url)

        response = httpx.Response(
            200,
            request=httpx.Request("POST", app.TURNSTILE_SITEVERIFY_URL),
            json={
                "success": True,
                "hostname": "bot.example.com",
                "action": app.TURNSTILE_VERIFY_ACTION,
            },
        )
        client = AsyncMock()
        client.post.return_value = response
        with (
            patch.object(app, "telegram_client", client),
            patch.object(
                app,
                "TURNSTILE_VERIFY_URL",
                "https://bot.example.com:8443/verify",
            ),
            patch.object(app, "TURNSTILE_SECRET_KEY", "secret-key"),
        ):
            self.assertTrue(asyncio.run(app.verify_turnstile_token("valid-token")))
            submitted = client.post.await_args.kwargs["json"]
            self.assertEqual(submitted["secret"], "secret-key")
            self.assertEqual(submitted["response"], "valid-token")
            self.assertIn("idempotency_key", submitted)

            response._content = json.dumps(
                {
                    "success": True,
                    "hostname": "attacker.example",
                    "action": app.TURNSTILE_VERIFY_ACTION,
                }
            ).encode()
            self.assertFalse(asyncio.run(app.verify_turnstile_token("wrong-host")))

            response._content = json.dumps(
                {
                    "success": True,
                    "hostname": "bot.example.com",
                    "action": "different_action",
                }
            ).encode()
            self.assertFalse(asyncio.run(app.verify_turnstile_token("wrong-action")))

        self.assertFalse(asyncio.run(app.verify_turnstile_token("x" * 2049)))

    def test_unverified_user_is_prompted_before_message_delivery(self) -> None:
        message = {
            "from": {"id": 13, "first_name": "Pending"},
            "chat": {"id": 13, "type": "private"},
            "message_id": 1,
            "text": "advertisement",
        }
        with (
            patch.object(app, "TURNSTILE_ENABLED", True),
            patch.object(
                app,
                "TURNSTILE_VERIFY_URL",
                "https://bot.example.com/verify",
            ),
            patch.object(
                app,
                "send_message",
                new=AsyncMock(return_value=True),
            ) as send_mock,
            patch.object(app, "notify_admins", new=AsyncMock()) as notify_mock,
        ):
            asyncio.run(app.handle_user_message(message))

            self.assertEqual(send_mock.await_count, 1)
            reply_markup = send_mock.await_args.kwargs["reply_markup"]
            self.assertEqual(
                reply_markup["inline_keyboard"][0][0]["web_app"]["url"],
                "https://bot.example.com/verify",
            )
            notify_mock.assert_not_awaited()
            self.assertIsNone(
                app.db_fetchone(
                    "SELECT id FROM message_logs WHERE chat_id = ?",
                    (13,),
                )
            )
            user = app.db_fetchone(
                "SELECT last_message FROM users WHERE chat_id = ?",
                (13,),
            )
            self.assertEqual(user["last_message"], "[等待人机验证]")

            send_mock.reset_mock()
            asyncio.run(app.handle_user_message(message))
            send_mock.assert_not_awaited()

            app.mark_user_verified(13)
            asyncio.run(app.handle_user_message(message))
            notify_mock.assert_awaited_once()

    def test_failed_verification_prompt_can_be_retried_immediately(self) -> None:
        send_mock = AsyncMock(side_effect=[False, True])
        with patch.object(app, "send_message", send_mock):
            asyncio.run(app.send_verification_prompt(15))
            row = app.db_fetchone(
                "SELECT last_prompted_at FROM user_verifications "
                "WHERE chat_id = ?",
                (15,),
            )
            self.assertIsNone(row["last_prompted_at"])

            asyncio.run(app.send_verification_prompt(15))

        self.assertEqual(send_mock.await_count, 2)
        row = app.db_fetchone(
            "SELECT last_prompted_at FROM user_verifications WHERE chat_id = ?",
            (15,),
        )
        self.assertIsNotNone(row["last_prompted_at"])

    def test_turnstile_page_and_completion_endpoint(self) -> None:
        init_data = self.telegram_init_data(14)
        with (
            patch.object(app, "TURNSTILE_ENABLED", True),
            patch.object(app, "TURNSTILE_SITE_KEY", "site-key"),
            patch.object(app, "TURNSTILE_SECRET_KEY", "secret-key"),
            patch.object(
                app,
                "TURNSTILE_VERIFY_URL",
                "https://bot.example.com/verify",
            ),
            patch.object(
                app,
                "verify_turnstile_token",
                new=AsyncMock(return_value=True),
            ) as verify_mock,
            patch.object(app, "send_welcome", new=AsyncMock()) as welcome_mock,
        ):
            client = TestClient(app.app)
            try:
                page = client.get("/verify")
                self.assertEqual(page.status_code, 200)
                self.assertIn("site-key", page.text)
                self.assertNotIn("secret-key", page.text)
                self.assertIn(
                    "https://challenges.cloudflare.com",
                    page.headers["content-security-policy"],
                )
                self.assertIn(
                    'size: challenge.clientWidth >= 300 ? "flexible" : "compact"',
                    page.text,
                )
                self.assertEqual(page.headers["cache-control"], "no-store")

                completed = client.post(
                    "/verify/complete",
                    json={
                        "init_data": init_data,
                        "turnstile_token": "turnstile-token",
                    },
                )
                self.assertEqual(completed.status_code, 200)
                self.assertTrue(completed.json()["ok"])
                verify_mock.assert_awaited_once_with("turnstile-token")
                welcome_mock.assert_awaited_once_with(14)
                self.assertTrue(app.is_user_verified(14))

                rejected = client.post(
                    "/verify/complete",
                    json={
                        "init_data": f"{init_data}tampered",
                        "turnstile_token": "unused-token",
                    },
                )
                self.assertEqual(rejected.status_code, 403)
                verify_mock.assert_awaited_once_with("turnstile-token")

                wrong_content_type = client.post(
                    "/verify/complete",
                    content=b"{}",
                    headers={"Content-Type": "text/plain"},
                )
                self.assertEqual(wrong_content_type.status_code, 415)

                oversized = client.post(
                    "/verify/complete",
                    content=b"{" + b" " * 16384 + b"}",
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(oversized.status_code, 413)
            finally:
                client.close()

        with patch.object(app, "TURNSTILE_ENABLED", False):
            client = TestClient(app.app)
            try:
                self.assertEqual(client.get("/verify").status_code, 404)
                self.assertEqual(
                    client.post("/verify/complete", json={}).status_code,
                    404,
                )
            finally:
                client.close()

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
        first_claim = app.claim_conversation(30, 1, activate_reply=True)

        second_claim = app.claim_conversation(
            30,
            2,
            force=True,
            reopen=True,
            activate_reply=True,
        )

        self.assertTrue(first_claim)
        self.assertTrue(second_claim)
        self.assertIsNone(app.get_admin_state(1))
        self.assertEqual(app.get_admin_state(2), 30)
        self.assertEqual(app.get_conversation_owner(30), 2)

    def test_message_links_survive_database_reinitialization(self) -> None:
        app.add_message_link(
            user_chat_id=200,
            user_message_id=15,
            admin_chat_id=1,
            admin_message_id=500,
            direction="user_to_admin",
            link_kind="notification",
        )

        app.init_db()

        link = app.get_message_link(1, 500)
        self.assertIsNotNone(link)
        self.assertEqual(int(link["user_chat_id"]), 200)
        self.assertEqual(int(link["user_message_id"]), 15)
        self.assertEqual(link["direction"], "user_to_admin")

    def test_existing_admin_message_link_cannot_be_remapped(self) -> None:
        app.add_message_link(
            user_chat_id=200,
            user_message_id=15,
            admin_chat_id=1,
            admin_message_id=501,
            direction="user_to_admin",
            link_kind="notification",
        )

        with self.assertRaises(RuntimeError):
            app.add_message_link(
                user_chat_id=999,
                user_message_id=99,
                admin_chat_id=1,
                admin_message_id=501,
                direction="user_to_admin",
                link_kind="notification",
            )

        link = app.get_message_link(1, 501)
        self.assertEqual(int(link["user_chat_id"]), 200)

    def test_notification_records_text_and_media_message_links(self) -> None:
        user = {"id": 201, "username": "mapped_user", "first_name": "Mapped"}
        source_message = {
            "from": user,
            "chat": {"id": 201, "type": "private"},
            "message_id": 16,
            "photo": [{"file_id": "photo"}],
            "caption": "mapped photo",
        }
        app.upsert_user(user, app.message_content(source_message))
        app.record_user_activity(201)
        app.add_message_log(
            201,
            "user",
            201,
            app.message_content(source_message),
            telegram_message_id=16,
        )
        send = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=600)
        )
        copy = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=601)
        )

        with (
            patch.object(app, "ADMIN_IDS", {1}),
            patch.object(app, "send_message", send),
            patch.object(app, "copy_message", copy),
        ):
            asyncio.run(
                app.notify_admins(
                    user,
                    app.message_content(source_message),
                    source_message=source_message,
                )
            )

        notification_link = app.get_message_link(1, 600)
        content_link = app.get_message_link(1, 601)
        self.assertEqual(int(notification_link["user_chat_id"]), 201)
        self.assertEqual(notification_link["link_kind"], "notification")
        self.assertEqual(int(content_link["user_chat_id"]), 201)
        self.assertEqual(content_link["link_kind"], "content")

    def test_native_reply_uses_mapping_instead_of_stale_reply_state(self) -> None:
        app.set_admin_state(1, 100)
        app.add_message_link(
            user_chat_id=202,
            user_message_id=20,
            admin_chat_id=1,
            admin_message_id=700,
            direction="user_to_admin",
            link_kind="notification",
        )
        message = {
            "from": {"id": 1},
            "chat": {"id": 1, "type": "private"},
            "message_id": 701,
            "text": "reply to mapped user",
            "reply_to_message": {"message_id": 700},
        }
        copy = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=21)
        )
        send = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=702)
        )

        with (
            patch.object(app, "copy_message", copy),
            patch.object(app, "send_message", send),
        ):
            asyncio.run(app.handle_admin_message(message))

        self.assertEqual(copy.await_args.args[0], 202)
        self.assertEqual(app.get_admin_state(1), 202)
        self.assertEqual(app.get_conversation_owner(202), 1)
        reply_link = app.get_message_link(1, 701)
        self.assertEqual(int(reply_link["user_chat_id"]), 202)
        self.assertEqual(reply_link["direction"], "admin_to_user")

    def test_unmapped_native_reply_never_falls_back_to_stale_state(self) -> None:
        app.set_admin_state(1, 203)
        message = {
            "from": {"id": 1},
            "chat": {"id": 1, "type": "private"},
            "message_id": 801,
            "text": "must not be sent",
            "reply_to_message": {"message_id": 800},
        }
        copy = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=22)
        )
        send = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=802)
        )

        with (
            patch.object(app, "copy_message", copy),
            patch.object(app, "send_message", send),
        ):
            asyncio.run(app.handle_admin_message(message))

        copy.assert_not_awaited()
        self.assertEqual(app.get_admin_state(1), 203)
        self.assertIn("无法识别回复对象", send.await_args.args[1])

    def test_atomic_claim_allows_only_one_admin(self) -> None:
        barrier = threading.Barrier(2)

        def claim(admin_id: int) -> app.ConversationClaimResult:
            barrier.wait()
            return app.claim_conversation(204, admin_id, activate_reply=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (1, 2)))

        self.assertEqual(sum(bool(result) for result in results), 1)
        owner_admin_id = app.get_conversation_owner(204)
        self.assertIn(owner_admin_id, {1, 2})
        self.assertEqual(app.get_admin_state(owner_admin_id), 204)
        losing_admin_id = 1 if owner_admin_id == 2 else 2
        self.assertIsNone(app.get_admin_state(losing_admin_id))

    def test_requested_media_types_have_explicit_summaries(self) -> None:
        cases = (
            ({"text": "hello"}, "text", "hello"),
            ({"photo": [{}], "caption": "photo"}, "photo", "[图片] photo"),
            ({"video": {}, "caption": "video"}, "video", "[视频] video"),
            (
                {"document": {"file_name": "a.txt"}, "caption": "doc"},
                "document",
                "[文件] a.txt doc",
            ),
            ({"audio": {}, "caption": "audio"}, "audio", "[音频] audio"),
            ({"voice": {}, "caption": "voice"}, "voice", "[语音] voice"),
            (
                {"animation": {}, "caption": "animation"},
                "animation",
                "[动图] animation",
            ),
            ({"sticker": {"emoji": "ok"}}, "sticker", "[贴纸] ok"),
            ({"location": {"latitude": 1, "longitude": 2}}, "location", "[位置]"),
            (
                {"contact": {"first_name": "Example", "last_name": "User"}},
                "contact",
                "[联系人] Example User",
            ),
        )

        for message, expected_kind, expected_content in cases:
            with self.subTest(expected_kind=expected_kind):
                self.assertEqual(app.message_kind(message), expected_kind)
                self.assertEqual(app.message_content(message), expected_content)

    def test_requested_admin_media_types_are_copied_to_the_selected_user(self) -> None:
        app.claim_conversation(205, 1, activate_reply=True)
        payloads = (
            {"text": "text"},
            {"photo": [{}], "caption": "photo"},
            {"video": {}, "caption": "video"},
            {"document": {"file_name": "file.txt"}, "caption": "document"},
            {"audio": {}, "caption": "audio"},
            {"voice": {}, "caption": "voice"},
            {"animation": {}, "caption": "animation"},
            {"sticker": {"emoji": "ok"}},
            {"location": {"latitude": 1, "longitude": 2}},
            {"contact": {"first_name": "Example"}},
        )
        copy = AsyncMock(
            side_effect=[
                app.TelegramSendResult(ok=True, message_id=1000 + index)
                for index in range(len(payloads))
            ]
        )
        send = AsyncMock(
            return_value=app.TelegramSendResult(ok=True, message_id=2000)
        )

        with (
            patch.object(app, "copy_message", copy),
            patch.object(app, "send_message", send),
        ):
            for index, payload in enumerate(payloads, start=1):
                message = {
                    "from": {"id": 1},
                    "chat": {"id": 1, "type": "private"},
                    "message_id": 900 + index,
                    **payload,
                }
                asyncio.run(app.handle_admin_message(message))

        self.assertEqual(copy.await_count, len(payloads))
        self.assertTrue(
            all(call.args[0] == 205 for call in copy.await_args_list)
        )
        for index in range(1, len(payloads) + 1):
            link = app.get_message_link(1, 900 + index)
            self.assertEqual(int(link["user_chat_id"]), 205)

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

    def test_display_timezone_converts_utc_and_rejects_invalid_names(self) -> None:
        previous_timezone = app.DISPLAY_TIMEZONE
        try:
            app.DISPLAY_TIMEZONE = app.load_display_timezone("Asia/Hong_Kong")
            self.assertEqual(
                app.compact_timestamp("2026-07-26 00:15:00"),
                "2026-07-26 08:15",
            )
            self.assertEqual(
                app.compact_timestamp(
                    datetime(2026, 7, 26, 0, 15, tzinfo=timezone.utc)
                ),
                "2026-07-26 08:15",
            )
        finally:
            app.DISPLAY_TIMEZONE = previous_timezone

        with self.assertRaises(RuntimeError):
            app.load_display_timezone("Invalid/Timezone")
        with self.assertRaises(RuntimeError):
            app.load_display_timezone("")

    def test_queue_and_recent_users_paginate_with_global_numbering(self) -> None:
        for index in range(25):
            chat_id = 1000 + index
            app.db_execute(
                """
                INSERT INTO users (
                    chat_id, username, last_message, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    f"page_user_{index}",
                    f"message {index}",
                    f"2026-07-26 00:{index:02d}:00",
                ),
            )
            app.record_user_activity(chat_id)

        self.assertEqual(app.count_conversation_queue("inbox"), 25)
        self.assertEqual(app.count_recent_users(), 25)
        self.assertEqual(app.paginate(999, 25), (3, 3, 20))

        queue_rows = app.get_conversation_queue("inbox", limit=10, offset=10)
        queue_text = app.format_conversation_queue(
            "inbox",
            queue_rows,
            page=2,
            total_pages=3,
            total_count=25,
        )
        queue_keyboard = app.conversation_queue_keyboard(
            queue_rows,
            "inbox",
            viewer_admin_id=1,
            page=2,
            total_pages=3,
        )
        self.assertEqual(len(queue_rows), 10)
        self.assertIn("共 25 个会话 · 第 2/3 页", queue_text)
        self.assertIn("<b>11.", queue_text)
        self.assertEqual(
            queue_keyboard["inline_keyboard"][0][0]["text"],
            "11 回复",
        )
        queue_callbacks = {
            button["callback_data"]
            for row in queue_keyboard["inline_keyboard"]
            for button in row
        }
        self.assertIn("queue:inbox:1", queue_callbacks)
        self.assertIn("queue:inbox:3", queue_callbacks)

        recent_rows = app.get_recent_users(limit=10, offset=20)
        recent_text = app.format_recent_users(
            recent_rows,
            page=3,
            total_pages=3,
            total_count=25,
        )
        recent_keyboard = app.recent_users_keyboard(
            recent_rows,
            page=3,
            total_pages=3,
        )
        self.assertEqual(len(recent_rows), 5)
        self.assertIn("共 25 位用户 · 第 3/3 页", recent_text)
        self.assertIn("<b>21.", recent_text)
        recent_callbacks = {
            button["callback_data"]
            for row in recent_keyboard["inline_keyboard"]
            for button in row
        }
        self.assertIn("admin:users:2", recent_callbacks)

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

    def test_paginated_callbacks_require_admin_and_validate_ascii_page(self) -> None:
        show_queue = AsyncMock()
        answer = AsyncMock(return_value=True)
        callback = {
            "id": "queue-page-denied",
            "from": {"id": 999},
            "data": "queue:inbox:2",
            "message": {"chat": {"id": 999, "type": "private"}},
        }
        with (
            patch.object(app, "answer_callback_query", answer),
            patch.object(app, "show_conversation_queue", show_queue),
        ):
            asyncio.run(app.handle_callback(callback))

        answer.assert_awaited_once_with("queue-page-denied", "无权限")
        show_queue.assert_not_awaited()

        answer.reset_mock()
        callback.update(
            {
                "id": "queue-page-invalid",
                "from": {"id": 1},
                "data": "queue:inbox:１２",
                "message": {"chat": {"id": 1, "type": "private"}},
            }
        )
        with (
            patch.object(app, "answer_callback_query", answer),
            patch.object(app, "show_conversation_queue", show_queue),
        ):
            asyncio.run(app.handle_callback(callback))

        answer.assert_awaited_once_with("queue-page-invalid", "页码无效")
        show_queue.assert_not_awaited()

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

    def test_send_results_preserve_message_ids_and_error_types(self) -> None:
        tg_mock = AsyncMock(
            side_effect=[
                {
                    "ok": True,
                    "result": {"message_id": 301, "message_thread_id": 9},
                },
                {"ok": True, "result": {"message_id": 302}},
                app.TelegramAPIError(
                    "sendMessage",
                    403,
                    "Forbidden: bot was blocked",
                ),
            ]
        )
        with patch.object(app, "tg", tg_mock):
            sent = asyncio.run(app.send_message(1, "hello"))
            copied = asyncio.run(app.copy_message(1, 2, 3))
            rejected = asyncio.run(app.send_message(1, "blocked"))

        self.assertTrue(sent)
        self.assertEqual(sent.message_id, 301)
        self.assertEqual(sent.message_thread_id, 9)
        self.assertEqual(copied.message_id, 302)
        self.assertFalse(rejected)
        self.assertEqual(rejected.status_code, 403)
        self.assertIn("屏蔽", app.delivery_failure_message(rejected))

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

            unsupported = client.post(
                "/tg/webhook",
                content=b"{}",
                headers={
                    **headers,
                    "Content-Type": "text/plain",
                },
            )
            self.assertEqual(unsupported.status_code, 415)

            malformed = client.post(
                "/tg/webhook",
                content=b"{",
                headers={
                    **headers,
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(malformed.status_code, 400)

            with patch.object(app, "WEBHOOK_MAX_BODY_BYTES", 16):
                oversized = client.post(
                    "/tg/webhook",
                    content=b'{"update_id": 200}',
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                    },
                )
            self.assertEqual(oversized.status_code, 413)

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
            self.assertEqual(health.json()["version"], app.APP_VERSION)

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
