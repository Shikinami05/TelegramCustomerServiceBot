import asyncio
import json
import os
import sqlite3
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
from scripts import manage_moderation
from tg_bot import database
from tg_bot.ai_providers import PROVIDERS, model_available
from tg_bot.config import load_settings
from tg_bot.models import TelegramSendResult
from tg_bot.repositories import moderation as repository
from tg_bot.services import ai_settings, moderation as service
from tg_bot.services.moderation_ui import ModerationInbox


class AdminExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "bot.db"
        self.env = self.root / ".env"
        self.env.write_text("BOT_TOKEN=keep-token\nWEBHOOK_SECRET=keep-secret\nADMIN_IDS=1\n", encoding="utf-8")
        (self.root / "VERSION").write_text("1.8.0", encoding="ascii")
        database.initialize(self.db)
        self.settings = replace(app.SETTINGS, ai_provider="deepseek", ai_api_key="", ai_model="",
                                deepseek_api_key="sk-test-private", deepseek_model="deepseek-flash",
                                ai_moderation_enabled=True, moderation_daily_limit=10, moderation_media_policy="hold")
        self.send = AsyncMock(return_value=TelegramSendResult(True, message_id=100))
        self.telegram = AsyncMock(return_value={"ok": True})
        self.present, self.answer = AsyncMock(), AsyncMock()
        self.controller = ai_settings.AISettingsController(lambda: self.db, lambda: self.env,
            lambda: self.settings, self.apply, self.send, self.telegram, lambda admin: admin == 1,
            lambda admin: None, self.present)
        self.inbox = ModerationInbox(self.db, {1, 2}, self.present, self.send, self.telegram,
                                     self.answer, asyncio.Event(), asyncio.Event(), True)
        for key, value in {"DB_PATH": self.db, "ADMIN_IDS": {1, 2}, "OWNER_IDS": {1},
                           "AI_MODERATION_ENABLED": True, "USER_RATE_LIMIT_COUNT": 100,
                           "admin_delivery_wakeup": None, "moderation_wakeup": None}.items():
            patcher = patch.object(app, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def apply(self, settings):
        self.settings = settings

    def message(self, user=50, text="hello", message_id=10, **fields):
        return {"from": {"id": user, "first_name": "<Alice>"}, "chat": {"id": user, "type": "private"},
                "message_id": message_id, "text": text, **fields}

    def callback(self, data="moderation:1", message_id=500):
        return {"id": "cb", "from": {"id": 1}, "data": data,
                "message": {"message_id": message_id, "chat": {"id": 1, "type": "private"}}}

    def held(self, update=100, user=50, verdict="spam"):
        message = self.message(user=user, text="<promotion>")
        repository.enqueue(self.db, update, user, 10, service.snapshot(message), message["text"], False)
        repository.claim_next(self.db)
        repository.record_result(self.db, update, "deepseek", "historical-model", verdict, "test")
        repository.hold(self.db, update, "test", verdict)

    async def test_user_receipt_and_welcome_do_not_expose_filter_or_provider(self):
        with patch.object(app, "send_message", self.send):
            await app.send_welcome(50)
            await app.handle_user_message(self.message(), 100)
        for call in self.send.await_args_list:
            self.assertEqual(call.args[0], 50)
            for word in ("DeepSeek", "广告", "审核", "模型", "疑似"):
                self.assertNotIn(word, call.args[1])
        self.assertIn("留言已收到", self.send.await_args.args[1])

    async def test_edit_does_not_generate_another_receipt(self):
        with patch.object(app, "send_message", self.send):
            await app.enqueue_moderation(self.message(), 100, "hello", edited=True)
        self.send.assert_not_awaited()

    async def test_provider_switch_clears_secret_disables_filter_and_invalidates_prompts(self):
        await self.controller.prompt(1, "key")
        await self.controller.callback(1, "provider_set:siliconflow")
        self.assertEqual(self.settings.ai_provider, "siliconflow")
        self.assertEqual(self.settings.moderation_key, "")
        self.assertFalse(self.settings.ai_moderation_enabled)
        values = dotenv_values(self.env)
        self.assertEqual(values["AI_API_KEY"], "")
        self.assertEqual(values["DEEPSEEK_API_KEY"], "")
        message = self.message(user=1, text="sk-old-provider-key", reply_to_message={"message_id": 100})
        with patch.object(ai_settings, "probe_key", new_callable=AsyncMock) as probe:
            self.assertTrue(await self.controller.message(message))
        probe.assert_not_awaited()
        self.assertEqual(values["BOT_TOKEN"], "keep-token")

    async def test_settings_edit_existing_panel_and_inputs_return_to_it(self):
        callback = self.callback("ai:model_input")
        await self.controller.callback(1, "model_input", callback)
        self.assertEqual(self.present.await_args.args[3], callback)
        message = self.message(user=1, text="model-v2", reply_to_message={"message_id": 100})
        await self.controller.message(message)
        self.assertEqual(self.present.await_args.args[3]["message"]["message_id"], 500)
        self.assertEqual(self.settings.moderation_model, "model-v2")

    async def test_key_without_native_reply_stays_in_config_flow(self):
        await self.controller.prompt(1, "key")
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)):
            self.assertTrue(await self.controller.message(self.message(user=1, text="sk-new-provider-key")))
        self.assertEqual(self.settings.moderation_key, "sk-new-provider-key")
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM message_logs"), [])

    async def test_old_prompt_marker_is_still_intercepted(self):
        message = self.message(user=1, text="key-without-prefix", reply_to_message={"message_id": 99, "text": "DeepSeek 配置输入"})
        self.assertTrue(await self.controller.message(message))
        self.telegram.assert_awaited_once()
        self.assertEqual(self.settings.moderation_key, "sk-test-private")

    async def test_pending_key_input_never_falls_into_reply_on_wrong_message(self):
        await self.controller.prompt(1, "key")
        message = self.message(user=1, text="hex-key-without-prefix", reply_to_message={"message_id": 500})
        self.assertTrue(await self.controller.message(message))
        self.assertEqual(self.settings.moderation_key, "sk-test-private")
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM message_logs"), [])

    async def test_new_admin_command_cancels_pending_configuration(self):
        repository.begin_config_input(self.db, 1, 100, "key")
        with patch.object(app, "ai_settings_controller", self.controller), patch.object(app, "send_message", self.send):
            await app.handle_admin_message(self.message(user=1, text="/myid"))
        self.assertIsNone(repository.pending_config_input(self.db, 1))

    async def test_old_provider_model_buttons_do_not_change_current_model(self):
        await self.controller.callback(1, "provider_set:siliconflow")
        await self.controller.callback(1, "model_set:deepseek:0")
        self.assertEqual(self.settings.moderation_model, "Qwen/Qwen3-32B")
        with patch.object(ai_settings, "probe_key", new_callable=AsyncMock) as probe:
            await self.controller.callback(1, "enable_confirm:deepseek")
        probe.assert_not_awaited()
        self.assertFalse(self.settings.ai_moderation_enabled)

    async def test_malformed_model_buttons_do_not_mutate_configuration(self):
        for action in ("model_set:deepseek:99", "model_set:deepseek:-1", "model_set:deepseek:0:extra", "provider_set:attacker"):
            await self.controller.callback(1, action)
        self.assertEqual(self.settings.moderation_key, "sk-test-private")
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM admin_audit_logs"), [])

    async def test_check_validates_selected_model_not_just_authentication(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"id": "available-model"}]}))
        original = httpx.AsyncClient
        with patch.object(ai_settings.httpx, "AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
            self.assertFalse(await ai_settings.probe_key("sk-key", "deepseek", "missing-model"))
            self.assertTrue(await ai_settings.probe_key("sk-key", "deepseek", "available-model"))

    async def test_synthetic_test_is_budgeted_and_never_uses_private_history(self):
        self.settings = replace(self.settings, moderation_daily_limit=1)
        with patch.object(ai_settings, "probe_key", AsyncMock(return_value=True)), \
             patch.object(ai_settings, "classify", AsyncMock(return_value=service.Verdict("allow", "ok"))) as classify:
            await self.controller.callback(1, "test_confirm:deepseek")
            await self.controller.callback(1, "test_confirm:deepseek")
        self.assertEqual(classify.await_count, 1)
        self.assertEqual(classify.await_args.args[3], "你好，想和你聊聊天。")
        self.assertEqual(database.fetchone(self.db, "SELECT requests FROM moderation_usage")[0], 1)

    async def test_models_containing_slashes_survive_restart(self):
        self.settings = replace(self.settings, ai_provider="siliconflow", ai_api_key="sk-siliconflow-key", ai_model="Qwen/Qwen3-32B")
        self.controller.save(1, self.settings, "test")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self.root)
        self.assertEqual(settings.moderation_model, "Qwen/Qwen3-32B")
        self.assertEqual(settings.moderation_key, "sk-siliconflow-key")
        self.assertNotIn(settings.moderation_key, repr(settings))

    async def test_provider_payloads_use_fixed_hosts_and_only_supported_options(self):
        for provider in PROVIDERS:
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"allow"}'}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await service.classify(client, "sk-isolated-key", PROVIDERS[provider].default_model, "hello", 1, provider)
            self.assertEqual(result.verdict, "allow")
            payload = json.loads(requests[0].content)
            self.assertEqual(str(requests[0].url), PROVIDERS[provider].base_url + "/chat/completions")
            self.assertEqual("thinking" in payload, provider == "deepseek")
            self.assertEqual("enable_thinking" in payload, provider.startswith("siliconflow"))

    async def test_no_text_media_policy_is_explicit_and_never_calls_ai(self):
        for index, policy in enumerate(("allow", "hold")):
            message = self.message(user=50 + index, text="", sticker={"file_id": "sticker-id"})
            repository.enqueue(self.db, 100 + index, 50 + index, 10, service.snapshot(message), "sticker", False)
            job = repository.claim_next(self.db)
            async with httpx.AsyncClient() as client:
                with patch.object(service, "classify", new_callable=AsyncMock) as classify:
                    result = await service.process_job(self.db, job, client, replace(self.settings, moderation_media_policy=policy), {1})
            self.assertEqual(result, policy == "allow")
            classify.assert_not_awaited()
            self.assertEqual(repository.get(self.db, 100 + index)["provider"], "")

    async def test_api_faults_and_spam_have_separate_categories_and_stable_sources(self):
        for index, result in enumerate((service.Verdict("spam", "promotion"), service.Verdict("uncertain", "HTTP 429", True))):
            message = self.message(user=50 + index)
            repository.enqueue(self.db, 100 + index, 50 + index, 10, service.snapshot(message), "hello", False)
            job = repository.claim_next(self.db)
            async with httpx.AsyncClient() as client:
                with patch.object(service, "classify", AsyncMock(return_value=result)):
                    await service.process_job(self.db, job, client, self.settings, {1})
        self.assertEqual(repository.pending_page(self.db, 1, category="spam")[0][0]["update_id"], 100)
        self.assertEqual(repository.pending_page(self.db, 1, category="error")[0][0]["update_id"], 101)
        self.settings = replace(self.settings, ai_provider="siliconflow", ai_model="new-model")
        await self.inbox.callback(self.callback(), "moderation_detail", "100:1:spam")
        text = self.present.await_args.args[1]
        self.assertIn("DeepSeek", text)
        self.assertIn("deepseek-flash", text)
        self.assertNotIn("new-model", text)

    async def test_dismiss_does_not_blacklist_or_deliver(self):
        self.held()
        await self.inbox.callback(self.callback(), "moderation_dismiss", "100:1:spam")
        self.assertEqual(repository.get(self.db, 100)["status"], "dismissed")
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM blacklists"), [])
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM admin_deliveries"), [])
        self.assertFalse(repository.decide(self.db, 100, 2, retry=True))

    async def test_retry_preserves_snapshot_and_is_idempotent(self):
        self.held(verdict="error")
        snapshot = repository.get(self.db, 100)["snapshot"]
        await self.inbox.callback(self.callback(), "moderation_retry", "100:1:error")
        self.assertEqual(repository.get(self.db, 100)["status"], "queued")
        self.assertEqual(repository.get(self.db, 100)["snapshot"], snapshot)
        self.assertFalse(repository.decide(self.db, 100, 2, retry=True))
        self.assertTrue(self.inbox.moderation_wakeup.is_set())

    async def test_disabled_filter_cannot_retry_and_fabricated_categories_cannot_mutate(self):
        self.held()
        self.inbox.enabled = False
        await self.inbox.callback(self.callback(), "moderation_retry", "100:1:all")
        for action, value in (("moderation", "1:invalid"), ("moderation_allow", "100:1:invalid"), ("moderation_allow", "100:１:all")):
            await self.inbox.callback(self.callback(), action, value)
        self.assertEqual(repository.get(self.db, 100)["status"], "held")

    async def test_approve_preserves_filtered_page_and_other_admin_sees_refresh(self):
        for index in range(7):
            self.held(100 + index, 50 + index)
        await self.inbox.callback(self.callback(), "moderation_allow", "105:2:spam")
        markup = self.present.await_args.args[2]
        callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
        self.assertIn("moderation:2:spam", callbacks)
        await self.inbox.callback(self.callback(), "moderation_allow", "105:2:spam")
        self.assertIn("已处理", self.answer.await_args.args[1])
        self.assertEqual(len(database.fetchall(self.db, "SELECT * FROM inbound_events")), 1)

    async def test_preview_failure_is_visible_and_cannot_be_used_as_reply_target(self):
        self.held()
        self.telegram.return_value = {"ok": False}
        await self.inbox.callback(self.callback(), "moderation_preview", "100:1:all")
        self.assertIn("无法显示", self.send.await_args.args[1])
        self.assertEqual(database.fetchall(self.db, "SELECT * FROM message_links"), [])

    async def test_unchanged_held_queue_does_not_keep_notifying(self):
        self.held()
        self.assertEqual(repository.claim_notice(self.db), 1)
        database.execute(self.db, "UPDATE moderation_notices SET sent_at=datetime('now','-1 day')")
        self.assertEqual(repository.claim_notice(self.db), 0)
        self.held(101, 51)
        self.assertEqual(repository.claim_notice(self.db), 2)

    async def test_original_notification_is_never_overwritten_by_a_menu(self):
        database.execute(self.db, "INSERT INTO message_links(user_chat_id,user_message_id,admin_chat_id,admin_message_id,direction,link_kind) "
                         "VALUES(50,10,1,500,'user_to_admin','notification')")
        with patch.object(app, "edit_message_text", AsyncMock(return_value=True)) as edit, patch.object(app, "send_message", self.send):
            await app.present_admin_view(1, "menu", {"inline_keyboard": []}, self.callback())
        edit.assert_not_awaited()
        self.send.assert_awaited_once()
        self.assertEqual(app.get_message_link(1, 500)["user_chat_id"], 50)

    async def test_unmapped_panel_edits_in_place_and_cannot_edit_other_admin_chat(self):
        with patch.object(app, "edit_message_text", AsyncMock(return_value=True)) as edit, patch.object(app, "send_message", self.send):
            await app.present_admin_view(1, "menu", {"inline_keyboard": []}, self.callback())
            callback = self.callback()
            callback["message"]["chat"]["id"] = 2
            await app.present_admin_view(1, "menu", {"inline_keyboard": []}, callback)
        self.assertEqual(edit.await_count, 1)
        self.assertEqual(self.send.await_count, 1)

    async def test_dashboard_navigation_clears_persistent_reply_and_config_input(self):
        app.upsert_user({"id": 50}, "hello")
        app.set_admin_state(1, 50)
        repository.begin_config_input(self.db, 1, 100, "key")
        with patch.object(app, "present_admin_view", self.present):
            await app.show_admin_dashboard(1, self.callback())
        self.assertIsNone(app.get_admin_state(1))
        self.assertIsNone(repository.pending_config_input(self.db, 1))

    async def test_conversation_detail_has_return_to_original_page(self):
        app.upsert_user({"id": 50}, "hello")
        with patch.object(app, "present_admin_view", self.present), patch.object(app, "answer_callback_query", self.answer):
            await app.handle_callback(self.callback("user:50:inbox:3"))
        data = [button["callback_data"] for row in self.present.await_args.args[2]["inline_keyboard"] for button in row]
        self.assertIn("queue:inbox:3", data)

    async def test_ui_text_and_callbacks_stay_bounded_and_escape_content(self):
        for index in range(5):
            self.held(100 + index, 50 + index)
        database.execute(self.db, "UPDATE moderation_jobs SET summary=?,reason=?", ("<&" * 2000, "<&" * 500))
        await self.inbox.show(1)
        await self.inbox.detail(1, repository.get(self.db, 100), 1, "all", self.callback())
        for call in self.present.await_args_list:
            self.assertLess(len(call.args[1]), 4096)
            self.assertNotIn("<Alice>", call.args[1])
            for row in call.args[2]["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_legacy_deepseek_config_and_official_aliases_are_supported(self):
        self.env.write_text(self.env.read_text() + "DEEPSEEK_API_KEY=sk-legacy-key\nDEEPSEEK_MODEL=deepseek-v4-flash\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self.root)
        self.assertEqual(settings.moderation_key, "sk-legacy-key")
        self.assertEqual(settings.moderation_model, "deepseek-v4-flash")
        self.assertTrue(model_available("deepseek", "deepseek-v4-flash", {"deepseek-flash"}))
        self.assertFalse(model_available("siliconflow", "deepseek-v4-flash", {"deepseek-flash"}))

    def test_backup_connections_are_closed_before_returning(self):
        original = sqlite3.connect
        connections = []
        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch.object(app, "DB_BACKUP_ENABLED", True), patch.object(app, "DB_BACKUP_DIR", self.root / "backups"), \
             patch.object(app.sqlite3, "connect", side_effect=connect):
            backup = app.backup_database()
        self.assertTrue(backup.is_file())
        self.assertEqual(len(connections), 2)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_cli_switch_does_not_reuse_other_provider_key(self):
        self.env.write_text(self.env.read_text() + "DEEPSEEK_API_KEY=sk-old-deepseek\n", encoding="utf-8")
        before = self.env.read_bytes()
        with patch("sys.argv", ["manage_moderation.py", "enable", "--env-file", str(self.env)]), \
             patch("builtins.input", side_effect=["siliconflow", "", "", ""]), \
             patch.object(manage_moderation.getpass, "getpass", return_value=""), patch("builtins.print"):
            self.assertEqual(manage_moderation.main(), 1)
        self.assertEqual(self.env.read_bytes(), before)
