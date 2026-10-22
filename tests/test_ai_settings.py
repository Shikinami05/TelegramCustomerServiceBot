import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from dotenv import dotenv_values

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_IDS", "1,2")

import app
from scripts import manage_webhook
from tg_bot import database
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import moderation
from tg_bot.services import ai_settings
from tg_bot.services import moderation as moderation_service


class AISettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.db"
        self.env = self.root / ".env"
        self.env.write_text("BOT_TOKEN=keep-bot\nWEBHOOK_SECRET=keep-webhook\nAI_MODERATION_ENABLED=false\n", encoding="utf-8")
        database.initialize(self.db)
        self.settings = replace(app.SETTINGS, ai_moderation_enabled=False, deepseek_api_key="", deepseek_model="deepseek-flash")
        self.send = AsyncMock(return_value=TelegramSendResult(ok=True, message_id=100))
        self.telegram = AsyncMock(return_value={"ok": True, "result": True})
        self.clear_reply = lambda admin: None
        self.controller = self.new_controller()

    def apply(self, settings):
        self.settings = settings

    def new_controller(self):
        return ai_settings.AISettingsController(
            lambda: self.db, lambda: self.env, lambda: self.settings, self.apply,
            self.send, self.telegram, lambda admin: admin == 1, lambda admin: self.clear_reply(admin),
        )

    def message(self, text="sk-new-test-private", admin=1, reply=100, **extra):
        return {"from": {"id": admin}, "chat": {"id": admin, "type": "private"},
                "text": text, "message_id": 200,
                "reply_to_message": {"message_id": reply, "text": ai_settings.INPUT_MARKER}, **extra}

    async def test_owner_menu_is_not_in_regular_admin_or_user_scope(self):
        for menu in (manage_webhook.USER_COMMANDS, manage_webhook.ADMIN_COMMANDS):
            self.assertNotIn("ai", {entry["command"] for entry in menu})
        self.assertEqual({entry["command"] for entry in manage_webhook.OWNER_COMMANDS}, {"start", "cancel", "myid", "settings"})
        await self.controller.callback(2, "key_input")
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM admin_config_inputs"), [])

    async def test_ai_command_opens_settings_when_ai_is_disabled_and_key_is_empty(self):
        message = {"from": {"id": 1}, "chat": {"id": 1, "type": "private"},
                   "text": "/ai", "message_id": 201}
        with patch.object(app, "ADMIN_IDS", {1, 2}), \
             patch.object(app, "ai_settings_controller", self.controller), \
             patch.object(app, "send_message", new_callable=AsyncMock) as fallback:
            await app.handle_admin_message(message)
        fallback.assert_not_awaited()
        self.assertEqual(self.send.await_count, 1)
        buttons = self.send.await_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertIn("ai:advanced", {button["callback_data"] for row in buttons for button in row})
        self.telegram.assert_not_awaited()

    async def test_key_update_deletes_input_persists_and_takes_effect_without_restart(self):
        await self.controller.prompt(1, "key")
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)) as probe:
            self.assertTrue(await self.controller.message(self.message()))
        probe.assert_awaited_once_with("sk-new-test-private", "deepseek")
        self.telegram.assert_awaited_once_with("deleteMessage", {"chat_id": 1, "message_id": 200})
        self.assertEqual(self.settings.deepseek_api_key, "sk-new-test-private")
        self.assertFalse(self.settings.ai_moderation_enabled)
        values = dotenv_values(self.env)
        self.assertEqual(values["DEEPSEEK_API_KEY"], "sk-new-test-private")
        self.assertEqual(values["BOT_TOKEN"], "keep-bot")
        self.assertEqual(values["WEBHOOK_SECRET"], "keep-webhook")
        self.assertNotIn("sk-new-test-private", str(self.send.await_args_list))
        with database.connect(self.db) as conn:
            self.assertNotIn("sk-new-test-private", "\n".join(conn.iterdump()))

    async def test_failed_delete_never_checks_or_saves_key(self):
        await self.controller.prompt(1, "key")
        self.telegram.side_effect = RuntimeError("delete failed")
        with patch.object(ai_settings, "probe_key", new_callable=AsyncMock) as probe:
            await self.controller.message(self.message())
        probe.assert_not_awaited()
        self.assertEqual(self.settings.deepseek_api_key, "")
        self.assertIn("未能自動刪除", str(self.send.await_args_list))

    async def test_invalid_key_and_failed_probe_preserve_previous_key(self):
        self.settings = replace(self.settings, deepseek_api_key="sk-old-test-private")
        await self.controller.prompt(1, "key")
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=False)):
            await self.controller.message(self.message())
        self.assertEqual(self.settings.deepseek_api_key, "sk-old-test-private")
        self.assertNotIn("DEEPSEEK_API_KEY", dotenv_values(self.env))

    async def test_expired_and_replayed_inputs_are_deleted_without_saving(self):
        await self.controller.prompt(1, "key")
        database.execute(self.db, "UPDATE admin_config_inputs SET expires_at=0")
        with patch.object(ai_settings, "probe_key", new_callable=AsyncMock) as probe:
            await self.controller.message(self.message())
        probe.assert_not_awaited()
        self.assertEqual(self.settings.deepseek_api_key, "")
        self.assertEqual(self.telegram.await_count, 1)

    async def test_restart_keeps_prompt_metadata_but_no_secret(self):
        await self.controller.prompt(1, "key")
        database.initialize(self.db)
        controller = self.new_controller()
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)) as probe:
            await controller.message(self.message())
            await controller.message(self.message())
        self.assertEqual(probe.await_count, 1)

    async def test_cancel_invalidates_input(self):
        await self.controller.prompt(1, "key")
        self.assertTrue(await self.controller.message(self.message(text="/cancel")))
        with patch.object(ai_settings, "probe_key", new_callable=AsyncMock) as probe:
            await self.controller.message(self.message())
        probe.assert_not_awaited()

    async def test_nonowner_and_edited_secret_never_update(self):
        await self.controller.prompt(1, "key")
        with patch.object(ai_settings, "probe_key", new_callable=AsyncMock) as probe:
            await self.controller.message(self.message(admin=2))
            await self.controller.message(self.message(), edited=True)
        probe.assert_not_awaited()
        self.assertEqual(self.settings.deepseek_api_key, "")

    async def test_key_never_enters_active_reply_route_or_chat_history(self):
        patchers = [patch.object(app, "DB_PATH", self.db), patch.object(app, "ADMIN_IDS", {1, 2}),
                    patch.object(app, "OWNER_IDS", {1}), patch.object(app, "ai_settings_controller", self.controller)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        app.upsert_user({"id": 50, "first_name": "User"}, "hello")
        app.set_admin_state(1, 50)
        with patch.object(app, "copy_message", new_callable=AsyncMock) as copy, \
             patch.object(app, "send_message", new_callable=AsyncMock):
            await app.handle_admin_message(self.message(reply=500))
        copy.assert_not_awaited()
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM message_logs"), [])

    async def test_private_group_callback_cannot_change_configuration(self):
        with patch.object(app, "OWNER_IDS", {1}), patch.object(app, "ADMIN_IDS", {1, 2}), \
             patch.object(app, "ai_settings_controller", self.controller), \
             patch.object(app, "answer_callback_query", new_callable=AsyncMock):
            await app.handle_callback({"id": "cb", "from": {"id": 1}, "data": "ai:disable",
                                       "message": {"message_id": 1, "chat": {"id": -100, "type": "supergroup"}}})
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM admin_audit_logs"), [])

    async def test_model_limit_and_toggle_apply_to_runtime(self):
        self.settings = replace(self.settings, deepseek_api_key="sk-existing-private")
        await self.controller.prompt(1, "model")
        await self.controller.message(self.message(text="deepseek-v4-pro"))
        self.assertEqual(self.settings.deepseek_model, "deepseek-v4-pro")
        self.send.return_value = TelegramSendResult(ok=True, message_id=101)
        await self.controller.prompt(1, "limit")
        await self.controller.message(self.message(text="200", reply=101))
        self.assertEqual(self.settings.moderation_daily_limit, 200)
        await self.controller.callback(1, "enable")
        self.assertFalse(self.settings.ai_moderation_enabled)
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)):
            await self.controller.callback(1, "enable_confirm:deepseek")
        self.assertTrue(self.settings.ai_moderation_enabled)
        await self.controller.callback(1, "disable")
        self.assertFalse(self.settings.ai_moderation_enabled)

    async def test_concurrent_duplicate_input_updates_only_once(self):
        await self.controller.prompt(1, "key")
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)) as probe:
            await asyncio.gather(self.controller.message(self.message()), self.controller.message(self.message()))
        self.assertEqual(probe.await_count, 1)
        self.assertEqual(len(database.fetchall(self.db, "SELECT * FROM admin_audit_logs")), 1)

    async def test_worker_reads_current_settings_per_job(self):
        self.settings = replace(self.settings, deepseek_api_key="sk-runtime-private", ai_moderation_enabled=True)
        job = {"update_id": 1}
        async def process(*args):
            self.assertEqual(args[3].deepseek_api_key, "sk-runtime-private")
            raise asyncio.CancelledError()
        with patch.object(moderation_service.moderation, "claim_next", return_value=job), \
             patch.object(moderation_service, "process_job", side_effect=process):
            with self.assertRaises(asyncio.CancelledError):
                await moderation_service.run_worker(self.db, lambda: self.settings, {1}, asyncio.Event(),
                                                    asyncio.Event(), AsyncMock(), app.logger)

    async def test_probe_does_not_follow_redirect_or_leak_errors(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(307, headers={"location": "https://other.example"}))
        original_client = httpx.AsyncClient
        with patch.object(ai_settings.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=transport, **kwargs)):
            self.assertFalse(await ai_settings.probe_key("sk-private"))

    async def test_failed_save_does_not_change_runtime(self):
        await self.controller.prompt(1, "key")
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)), \
             patch.object(ai_settings, "configure_moderation", side_effect=OSError("write failed")):
            await self.controller.message(self.message())
        self.assertEqual(self.settings.deepseek_api_key, "")

    async def test_simple_panel_offers_three_modes_without_separate_switches(self):
        await self.controller.panel(1)
        buttons = {button["callback_data"] for row in self.send.await_args.kwargs["reply_markup"]["inline_keyboard"] for button in row}
        self.assertTrue({"ai:mode:rules", "ai:mode:ai", "ai:mode:off", "ai:setup", "ai:rules", "ai:advanced"} <= buttons)
        self.assertFalse({"ai:rules_on", "ai:rules_off", "ai:enable", "ai:disable"} & buttons)
        self.assertIn("不會自動封鎖", self.send.await_args.args[1])
        self.assertLess(len(self.send.await_args.args[1]), 2000)

    async def test_original_rules_page_escapes_custom_text_and_fits_telegram(self):
        from tg_bot.services import spam_rules

        spam_rules.replace_rules(self.db, "\n".join(f"词语：{index}" + "\U0001f600<&" * 25 for index in range(30)))
        await self.controller.callback(1, "rules")
        text = self.send.await_args.args[1]
        self.assertIn("本專案獨立編寫", text)
        self.assertIn("幸运哈希开桌", text)
        self.assertNotIn("\U0001f600<&", text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertEqual(text.count("<b>"), text.count("</b>"))
        for row in self.send.await_args.kwargs["reply_markup"]["inline_keyboard"]:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)

    async def test_quick_setup_requires_notice_and_checks_model_before_enabling(self):
        await self.controller.callback(1, "mode:ai")
        self.assertIn("可能產生 API 費用", self.send.await_args.args[1])
        self.assertIn("無法保證清除", self.send.await_args.args[1])
        self.assertIsNone(moderation.pending_config_input(self.db, 1))
        await self.controller.callback(1, "setup_input")
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)) as probe:
            await self.controller.message(self.message())
        probe.assert_awaited_once_with("sk-new-test-private", "deepseek", self.settings.moderation_model)
        self.assertTrue(self.settings.ai_moderation_enabled)
        self.assertTrue(self.settings.spam_rules_enabled)
        self.assertEqual(dotenv_values(self.env)["AI_MODERATION_ENABLED"], "true")
        self.assertEqual(dotenv_values(self.env)["SPAM_RULES_ENABLED"], "true")
        self.assertNotIn("sk-new-test-private", str(self.send.await_args_list))
        with database.connect(self.db) as conn:
            self.assertNotIn("sk-new-test-private", "\n".join(conn.iterdump()))

    async def test_failed_quick_setup_and_nonowner_input_leave_mode_and_key_unchanged(self):
        original = self.settings
        await self.controller.callback(2, "setup_input")
        self.assertIsNone(moderation.pending_config_input(self.db, 2))
        for index, key in enumerate(("invalid", "sk-new-test-private")):
            self.send.return_value = TelegramSendResult(True, message_id=100 + index)
            await self.controller.callback(1, "setup_input")
            with patch.object(ai_settings, "probe_key", AsyncMock(return_value=False)):
                await self.controller.message(self.message(text=key, reply=100 + index))
            self.assertEqual(self.settings, original)

    async def test_mode_changes_are_persistent_and_require_current_provider_for_ai(self):
        self.settings = replace(self.settings, deepseek_api_key="sk-existing-private", spam_rules_enabled=False)
        await self.controller.callback(1, "mode:ai")
        self.assertFalse(self.settings.ai_moderation_enabled)
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)) as probe:
            for action in ("mode_ai_confirm:other", "enable_confirm", "mode_ai_confirm"):
                await self.controller.callback(1, action)
            probe.assert_not_awaited()
            self.assertFalse(self.settings.ai_moderation_enabled)
            await self.controller.callback(1, "mode_ai_confirm:deepseek")
        self.assertEqual(self.settings.moderation_mode_label, "規則 + AI")
        await self.controller.callback(1, "mode:rules")
        self.assertFalse(self.settings.ai_moderation_enabled)
        self.assertTrue(self.settings.spam_rules_enabled)
        await self.controller.callback(1, "mode:off")
        self.assertFalse(self.settings.spam_rules_enabled)
        self.assertEqual(dotenv_values(self.env)["SPAM_RULES_ENABLED"], "false")
        self.assertEqual(self.settings.deepseek_api_key, "sk-existing-private")

    async def test_quick_setup_expiry_cancel_and_provider_change_never_save_key(self):
        for index, state in enumerate(("expired", "canceled", "provider_changed")):
            with self.subTest(state=state):
                self.settings = replace(self.settings, ai_provider="deepseek", deepseek_api_key="", ai_api_key="")
                self.send.return_value = TelegramSendResult(True, message_id=100 + index)
                await self.controller.callback(1, "setup_input")
                if state == "expired":
                    database.execute(self.db, "UPDATE admin_config_inputs SET expires_at=0")
                elif state == "canceled":
                    await self.controller.callback(1, "panel")
                else:
                    await self.controller.callback(1, "provider_set:siliconflow")
                with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)) as probe:
                    await self.controller.message(self.message(reply=100 + index))
                probe.assert_not_awaited()
                self.assertEqual(self.settings.moderation_key, "")
                self.assertFalse(self.settings.ai_moderation_enabled)
