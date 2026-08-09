import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from tg_bot import database, keyboards
from tg_bot.config import load_settings
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
        self.assertIn(("index", "idx_message_links_user_message"), objects)


class KeyboardModuleTests(unittest.TestCase):
    def test_keyboard_styles_and_callback_values_are_preserved(self) -> None:
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

        exit_button = keyboards.exit_reply_keyboard(123)["inline_keyboard"][0][0]
        self.assertEqual(exit_button["callback_data"], "cancel:123")
        self.assertEqual(exit_button["style"], "danger")

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
