import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from tg_bot import database, keyboards
from tg_bot.config import load_settings
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import (
    access,
    broadcasts,
    conversations,
    deliveries,
    inbound,
    messages,
    users,
)
from tg_bot.repositories import updates
from tg_bot.services import broadcast as broadcast_service
from tg_bot.telegram import TelegramAPIError, request
from tg_bot.text import escape_html_limited, html_to_plain_text, truncate_text


class SettingsTests(unittest.TestCase):
    def test_valid_settings_are_typed_and_owner_defaults_to_admins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "VERSION").write_text("1.4.0\n", encoding="ascii")
            environment = {
                "BOT_TOKEN": "test-token",
                "WEBHOOK_SECRET": "test-secret",
                "ADMIN_IDS": "1,2",
                "DISPLAY_TIMEZONE": "UTC",
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = load_settings(base_dir)

        self.assertEqual(settings.admin_ids, frozenset({1, 2}))
        self.assertEqual(settings.owner_ids, frozenset({1, 2}))
        self.assertEqual(settings.display_timezone.key, "UTC")
        self.assertEqual(settings.app_version, "1.4.0")
        self.assertEqual(settings.db_path, base_dir / "bot.db")
        self.assertEqual(settings.admin_reply_state_ttl_seconds, 1800)

    def test_missing_security_settings_fail_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "VERSION").write_text("1.4.0\n", encoding="ascii")
            with patch.dict(os.environ, {"BOT_TOKEN": "test-token"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "WEBHOOK_SECRET"):
                    load_settings(base_dir)

    def test_invalid_owner_id_does_not_expand_owner_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "VERSION").write_text("1.4.0\n", encoding="ascii")
            environment = {
                "BOT_TOKEN": "test-token",
                "WEBHOOK_SECRET": "test-secret",
                "ADMIN_IDS": "1,2",
                "OWNER_IDS": "1,not-an-id",
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "OWNER_IDS"):
                    load_settings(base_dir)

    def test_invalid_boolean_fails_instead_of_silently_disabling_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "VERSION").write_text("1.4.0\n", encoding="ascii")
            environment = {
                "BOT_TOKEN": "test-token",
                "WEBHOOK_SECRET": "test-secret",
                "ADMIN_IDS": "1",
                "DB_BACKUP_ENABLED": "sometimes",
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "DB_BACKUP_ENABLED"):
                    load_settings(base_dir)


class DatabaseModuleTests(unittest.TestCase):
    def test_message_link_survives_reconnect_and_admin_message_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            database.initialize(db_path)
            database.execute(
                db_path,
                """
                INSERT INTO message_links (
                    user_chat_id, user_message_id,
                    admin_chat_id, admin_message_id,
                    direction, link_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (1001, 41, 2001, 91, "user_to_admin", "notification"),
            )

            row = database.fetchone(
                db_path,
                "SELECT user_chat_id, user_message_id FROM message_links "
                "WHERE admin_chat_id = ? AND admin_message_id = ?",
                (2001, 91),
            )
            self.assertIsNotNone(row)
            self.assertEqual((row["user_chat_id"], row["user_message_id"]), (1001, 41))

            with self.assertRaises(sqlite3.IntegrityError):
                database.execute(
                    db_path,
                    """
                    INSERT INTO message_links (
                        user_chat_id, user_message_id,
                        admin_chat_id, admin_message_id,
                        direction, link_kind
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (1002, 42, 2001, 91, "user_to_admin", "notification"),
                )

    def test_initialize_creates_operational_tables_and_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            database.initialize(db_path)
            with database.connect(db_path) as conn:
                objects = {
                    (row["type"], row["name"])
                    for row in conn.execute(
                        "SELECT type, name FROM sqlite_master "
                        "WHERE type IN ('table', 'index')"
                    )
                }

        self.assertIn(("table", "users"), objects)
        self.assertIn(("table", "message_links"), objects)
        self.assertIn(("table", "processed_updates"), objects)
        self.assertIn(("table", "pending_broadcasts"), objects)
        self.assertIn(("table", "inbound_events"), objects)
        self.assertIn(("table", "admin_deliveries"), objects)
        self.assertIn(("table", "admin_reply_deliveries"), objects)
        self.assertIn(("index", "idx_message_links_user_message"), objects)
        self.assertIn(("index", "idx_admin_deliveries_pending"), objects)
        self.assertIn(("index", "idx_admin_deliveries_alerts"), objects)
        self.assertIn(("index", "idx_admin_reply_deliveries_status"), objects)


class RepositoryModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bot.db"
        database.initialize(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_update_claim_is_persistent_and_idempotent(self) -> None:
        self.assertEqual(updates.claim(self.db_path, 100, 30), "claimed")
        self.assertEqual(updates.claim(self.db_path, 100, 30), "processing")
        updates.finish(self.db_path, 100)
        self.assertEqual(updates.claim(self.db_path, 100, 30), "done")

    def test_inbound_delivery_records_message_links_in_order(self) -> None:
        persisted = inbound.persist_event(
            self.db_path,
            101,
            {"id": 10, "username": "user"},
            "photo caption",
            10,
            20,
            event_type="message",
            title="收到用户消息",
            edited=False,
            admin_ids={200},
            include_content_delivery=True,
        )
        self.assertTrue(persisted)
        self.assertFalse(
            inbound.persist_event(
                self.db_path,
                101,
                {"id": 10},
                "duplicate",
                10,
                20,
                event_type="message",
                title="收到用户消息",
                edited=False,
                admin_ids={200},
                include_content_delivery=True,
            )
        )

        notification = deliveries.claim_next_admin(self.db_path, now=100)
        self.assertEqual(notification["delivery_kind"], "notification")
        deliveries.complete_admin(
            self.db_path,
            notification,
            TelegramSendResult(True, message_id=301),
        )
        content = deliveries.claim_next_admin(self.db_path, now=100)
        self.assertEqual(content["delivery_kind"], "content")
        deliveries.complete_admin(
            self.db_path,
            content,
            TelegramSendResult(True, message_id=302),
        )

        self.assertEqual(
            int(messages.find_link(self.db_path, 200, 301)["user_chat_id"]),
            10,
        )
        self.assertEqual(
            int(messages.find_link(self.db_path, 200, 302)["user_message_id"]),
            20,
        )
        self.assertIsNone(deliveries.claim_next_admin(self.db_path, now=100))

    def test_conversation_claim_and_clear_are_consistent(self) -> None:
        conversations.record_user_activity(self.db_path, 10)
        acquired = conversations.claim(
            self.db_path,
            10,
            200,
            activate_reply=True,
        )
        self.assertEqual(acquired.status, "acquired")
        conflict = conversations.claim(self.db_path, 10, 201)
        self.assertEqual((conflict.status, conflict.owner_admin_id), ("conflict", 200))
        self.assertIsNone(
            conversations.clear_admin_state(
                self.db_path,
                200,
                1800,
                expected_target_chat_id=11,
            )
        )
        self.assertEqual(conversations.get_admin_state(self.db_path, 200, 1800), 10)
        self.assertEqual(
            conversations.clear_admin_state(self.db_path, 200, 1800, 10),
            10,
        )

    def test_broadcast_recipient_can_only_finish_once(self) -> None:
        database.execute(
            self.db_path,
            "INSERT INTO users (chat_id, first_name) VALUES (?, ?)",
            (10, "User"),
        )
        broadcast_id = broadcasts.create(self.db_path, 200, "hello")
        self.assertEqual(
            broadcasts.queue(self.db_path, broadcast_id, 200),
            ("queued", 1),
        )
        self.assertIsNotNone(broadcasts.claim_next_job(self.db_path))
        self.assertEqual(broadcasts.claim_next_recipient(self.db_path, broadcast_id), 10)
        broadcasts.finish_recipient(self.db_path, broadcast_id, 10, True)
        with self.assertRaisesRegex(RuntimeError, "no longer claimed"):
            broadcasts.finish_recipient(self.db_path, broadcast_id, 10, True)
        result = broadcasts.complete(self.db_path, broadcast_id)
        self.assertEqual(int(result["sent_count"]), 1)

    def test_interrupted_broadcast_recipient_is_not_replayed(self) -> None:
        database.execute(
            self.db_path,
            "INSERT INTO users (chat_id, first_name) VALUES (?, ?)",
            (10, "User"),
        )
        broadcast_id = broadcasts.create(self.db_path, 200, "hello")
        broadcasts.queue(self.db_path, broadcast_id, 200)
        broadcasts.claim_next_job(self.db_path)
        self.assertEqual(broadcasts.claim_next_recipient(self.db_path, broadcast_id), 10)

        database.initialize(self.db_path)

        self.assertIsNotNone(broadcasts.claim_next_job(self.db_path))
        self.assertIsNone(broadcasts.claim_next_recipient(self.db_path, broadcast_id))
        result = broadcasts.complete(self.db_path, broadcast_id)
        self.assertEqual(int(result["sent_count"]), 0)
        self.assertEqual(int(result["failed_count"]), 0)
        self.assertEqual(int(result["unknown_count"]), 1)

    def test_user_history_and_access_state_are_persistent(self) -> None:
        users.upsert(
            self.db_path,
            {"id": 10, "username": "before", "first_name": "User"},
            "first",
        )
        users.upsert(
            self.db_path,
            {"id": 10, "username": "after", "first_name": "User"},
            "second",
        )
        users.add_message_log(self.db_path, 10, "user", 10, "hello", 20)
        self.assertEqual(users.get(self.db_path, 10)["username"], "after")
        self.assertEqual(users.get_history(self.db_path, 10, 5)[0]["text"], "hello")

        self.assertEqual(
            access.check_rate_limit(self.db_path, 10, 1, 60, 30, now=100),
            (True, False, 0),
        )
        self.assertEqual(
            access.check_rate_limit(self.db_path, 10, 1, 60, 30, now=101),
            (False, True, 30),
        )
        access.blacklist(self.db_path, 10, 200, "spam")
        self.assertTrue(access.is_blacklisted(self.db_path, 10))
        self.assertEqual(access.list_blacklist(self.db_path)[0]["reason"], "spam")
        access.unblacklist(self.db_path, 10)
        self.assertFalse(access.is_blacklisted(self.db_path, 10))


class BroadcastServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_network_result_is_not_retried(self) -> None:
        recipients = iter((10, None))
        finished: list[tuple[str, int, bool, str, bool]] = []
        send = AsyncMock(
            return_value=TelegramSendResult(
                False,
                description="connection closed before response",
            )
        )

        await broadcast_service.process_job(
            {"id": "job-1", "content": "hello"},
            lambda _: next(recipients),
            lambda _: None,
            lambda _: False,
            lambda *args: finished.append(args),
            send,
            rate_limit_retries=3,
            send_delay_seconds=0,
        )

        send.assert_awaited_once()
        self.assertEqual(finished[0][:3], ("job-1", 10, False))
        self.assertTrue(finished[0][4])

    async def test_server_error_is_treated_as_uncertain(self) -> None:
        recipients = iter((10, None))
        finished: list[tuple[str, int, bool, str, bool]] = []
        send = AsyncMock(
            return_value=TelegramSendResult(
                False,
                status_code=502,
                description="bad gateway",
            )
        )

        await broadcast_service.process_job(
            {"id": "job-2", "content": "hello"},
            lambda _: next(recipients),
            lambda _: None,
            lambda _: False,
            lambda *args: finished.append(args),
            send,
            rate_limit_retries=3,
            send_delay_seconds=0,
        )

        send.assert_awaited_once()
        self.assertTrue(finished[0][4])


class KeyboardModuleTests(unittest.TestCase):
    def test_reply_mode_actions_share_the_same_style(self) -> None:
        markup = keyboards.admin_user_keyboard(
            123,
            blacklisted=False,
            closed=False,
            owner_admin_id=8,
            viewer_admin_id=9,
        )
        first_row = markup["inline_keyboard"][0]
        self.assertEqual(first_row[0]["text"], "接管")
        self.assertEqual(first_row[0]["callback_data"], "takeover:123")
        self.assertEqual(first_row[0]["style"], "primary")
        self.assertEqual(first_row[1]["style"], "success")

        owned_markup = keyboards.admin_user_keyboard(
            123,
            blacklisted=False,
            closed=False,
            owner_admin_id=8,
            viewer_admin_id=8,
        )
        self.assertEqual(
            owned_markup["inline_keyboard"][0][0]["text"], "持续回复"
        )
        continue_button = owned_markup["inline_keyboard"][0][0]

        exit_button = keyboards.exit_reply_keyboard(123)["inline_keyboard"][0][0]
        self.assertEqual(exit_button["callback_data"], "cancel:123")
        self.assertEqual(exit_button["style"], continue_button["style"])
        self.assertEqual(
            exit_button["style"],
            keyboards.REPLY_MODE_BUTTON_STYLE,
        )

        queue_markup = keyboards.conversation_queue_keyboard(
            [{"chat_id": 123, "owner_admin_id": 8}],
            "inbox",
            viewer_admin_id=8,
            page=1,
            total_pages=1,
            page_size=10,
            counts={"inbox": 1, "pending": 0, "closed": 0},
        )
        queue_button = queue_markup["inline_keyboard"][0][0]
        self.assertEqual(queue_button["style"], continue_button["style"])

    def test_pagination_and_verification_buttons_are_stable(self) -> None:
        navigation = keyboards.pagination_navigation_row("queue:inbox", 2, 3)
        self.assertEqual(
            navigation,
            [
                ("上一页", "queue:inbox:1"),
                ("第 2/3 页", "queue:inbox:2"),
                ("下一页", "queue:inbox:3"),
            ],
        )
        verification = keyboards.verification_keyboard(
            "https://bot.example.com/verify"
        )
        button = verification["inline_keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://bot.example.com/verify")
        self.assertEqual(button["style"], "primary")

    def test_unknown_button_style_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported inline button style"):
            keyboards.inline_keyboard([[('测试', 'test:1', 'neon')]])


class TelegramTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_request_returns_api_payload(self) -> None:
        def handler(request_message: httpx.Request) -> httpx.Response:
            self.assertEqual(request_message.url.path, "/bot-token/getMe")
            return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await request(
                client,
                "https://api.telegram.org/bot-token",
                "getMe",
                {},
            )

        self.assertEqual(result["result"]["id"], 1)

    async def test_retry_after_is_exposed_without_leaking_request_url(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 4},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(TelegramAPIError) as raised:
                await request(
                    client,
                    "https://example.invalid/bot-secret",
                    "sendMessage",
                    {},
                )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, 4)
        self.assertNotIn("bot-secret", str(raised.exception))


class TextModuleTests(unittest.TestCase):
    def test_html_and_length_helpers_preserve_telegram_limits(self) -> None:
        self.assertEqual(html_to_plain_text("<b>A &amp; B</b>"), "A & B")
        self.assertEqual(truncate_text("abcdef", 4), "abc…")
        escaped = escape_html_limited("<&abcdef", 8)
        self.assertLessEqual(len(escaped), 8)
        self.assertTrue(escaped.endswith("…"))


if __name__ == "__main__":
    unittest.main()
