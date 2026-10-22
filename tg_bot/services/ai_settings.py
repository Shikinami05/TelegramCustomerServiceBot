import asyncio
import html
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from tg_bot import database
from tg_bot.ai_providers import KEY_PATTERN, MODEL_PATTERN, PROVIDERS, model_available
from tg_bot.config import Settings
from tg_bot.keyboards import inline_keyboard
from tg_bot.moderation_config import configure_moderation
from tg_bot.repositories import moderation
from tg_bot.services import spam_rules
from tg_bot.services.moderation import classify
from tg_bot.text import escape_html_limited


INPUT_MARKER = "AI 設定輸入"
LEGACY_INPUT_MARKERS = ("AI 配置输入", "DeepSeek 配置输入")
POSSIBLE_SECRET = re.compile(r"\bsk-[A-Za-z0-9_.-]{8,}|(?:DEEPSEEK|AI)_API_KEY", re.IGNORECASE)


async def probe_key(api_key: str, provider: str = "deepseek", model: str | None = None) -> bool:
    async def request() -> bool:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", PROVIDERS[provider].base_url + "/models",
                                     headers={"Authorization": f"Bearer {api_key}"}) as response:
                if response.status_code != 200:
                    return False
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 262144:
                        return False
                data = json.loads(body)
                entries = data.get("data") if isinstance(data, dict) else None
                return (isinstance(entries, list) and bool(entries) and
                        (model is None or model_available(provider, model, {
                            item["id"] for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str)
                        })))
    try:
        return await asyncio.wait_for(request(), timeout=6)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
        return False


class AISettingsController:
    """Owner-only settings, bounded input sessions and credential-safe provider switching."""

    def __init__(self, db_path: Callable[[], Path], env_path: Callable[[], Path],
                 settings: Callable[[], Settings], apply_settings: Callable[[Settings], None],
                 send: Callable[..., Awaitable[Any]], telegram: Callable[..., Awaitable[dict]],
                 is_owner: Callable[[int], bool], clear_reply: Callable[[int], Any],
                 present: Callable[..., Awaitable[Any]] | None = None):
        self.db_path, self.env_path = db_path, env_path
        self.settings, self.apply_settings = settings, apply_settings
        self.send, self.telegram = send, telegram
        self.is_owner, self.clear_reply, self.present = is_owner, clear_reply, present
        self.lock = asyncio.Lock()

    async def view(self, admin_id: int, text: str, rows: list, callback: dict | None = None) -> None:
        if self.present:
            await self.present(admin_id, text, inline_keyboard(rows), callback)
        else:
            await self.send(admin_id, text, reply_markup=inline_keyboard(rows))

    async def panel(self, admin_id: int, callback: dict | None = None, notice: str = "") -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "僅負責人可管理防廣告設定。")
            return
        moderation.cancel_config_input(self.db_path(), admin_id)
        self.clear_reply(admin_id)
        settings = self.settings()
        provider = PROVIDERS[settings.ai_provider]
        text = (
            "<b>防廣告設定</b>\n\n"
            f"<b>目前模式：{settings.moderation_mode_label}</b>\n"
            "疑似廣告會安靜地放進暫存區，不通知對方檢測結果，也不會自動封鎖。\n\n"
            "<b>選擇保護方式</b>\n"
            "僅規則：本地辨識明顯的博彩、刷單和招募話術，不呼叫 AI。\n"
            "規則 + AI：先篩選明顯廣告，再讓 AI 判斷剩餘文字，需設定密鑰。\n"
            "暫停防廣告：直接接收新留言，已有暫存記錄不變。\n\n"
            f"<b>AI 設定</b>\n服務商：{provider.name}\n"
            f"模型：<code>{html.escape(settings.moderation_model)}</code>\n"
            f"密鑰：{'已設定' if settings.moderation_key else '未設定'}"
        )
        if notice:
            text += "\n\n" + html.escape(notice)
        await self.view(admin_id, text, [
            [("僅規則", "ai:mode:rules"), ("規則 + AI", "ai:mode:ai", "success")],
            [("暫停防廣告", "ai:mode:off", "danger")],
            [("設定 AI", "ai:setup"), ("廣告規則", "ai:rules")],
            [("更多設定", "ai:advanced")],
            [("暫存區", "moderation:1"), ("返回設定", "settings:panel")],
        ], callback)

    async def advanced(self, admin_id: int, callback: dict | None = None) -> None:
        settings = self.settings()
        provider = PROVIDERS[settings.ai_provider]
        usage = database.fetchone(self.db_path(), "SELECT requests FROM moderation_usage WHERE day=date('now')")
        await self.view(admin_id,
            f"<b>防廣告 · 進階設定</b>\n\n服務商：{provider.name}\n"
            f"模型：<code>{html.escape(settings.moderation_model)}</code>\n"
            f"今日呼叫：{usage[0] if usage else 0} / {settings.moderation_daily_limit}（UTC）\n"
            f"無文字媒體：{'直接接收' if settings.moderation_media_policy == 'allow' else '暫存待確認'}",
            [
                [("服務商", "ai:providers"), ("API 密鑰", "ai:key")],
                [("模型", "ai:model"), ("測試檢測", "ai:test")],
                [("廣告規則", "ai:rules"), ("每日限額", "ai:limit")],
                [("無文字媒體", "ai:media"), ("連線檢查", "ai:check")],
                [("返回防廣告", "ai:panel")],
            ], callback)

    async def prompt(self, admin_id: int, kind: str, callback: dict | None = None) -> None:
        self.clear_reply(admin_id)
        settings = self.settings()
        labels = {"rules": "自訂廣告規則，每行填寫「詞語：短語」或「域名：example.com」；傳送「清空」刪除自訂規則", "key": f"{PROVIDERS[settings.ai_provider].name} API Key", "setup": f"{PROVIDERS[settings.ai_provider].name} API Key（驗證成功後啟用規則 + AI）", "model": "完整模型 ID", "limit": "每日呼叫次數（1 至 1000000）"}
        if callback:
            await self.view(admin_id, f"<b>等待輸入</b>\n\n{labels[kind]}\n3 分鐘內有效。",
                            [[("取消輸入", "ai:panel")]], callback)
        sent = await self.send(admin_id, f"<b>{INPUT_MARKER}</b>\n\n請輸入{labels[kind]}。"
                              "\n回覆此訊息或直接傳送均可；/cancel 取消。輸入內容不會轉發給使用者。",
                              reply_markup={"force_reply": True, "selective": True})
        prompt_id = getattr(sent, "message_id", None)
        if not sent or prompt_id is None:
            await self.panel(admin_id, callback, "無法建立輸入，請重試。")
            return
        panel_id = (callback or {}).get("message", {}).get("message_id")
        moderation.begin_config_input(self.db_path(), admin_id, prompt_id, kind, settings.ai_provider, panel_id)

    def save(self, admin_id: int, settings: Settings, action: str) -> None:
        configure_moderation(self.env_path(), settings.ai_moderation_enabled,
                             api_key=settings.moderation_key, model=settings.moderation_model,
                             daily_limit=settings.moderation_daily_limit, provider=settings.ai_provider,
                             media_policy=settings.moderation_media_policy, rules_enabled=settings.spam_rules_enabled)
        self.apply_settings(settings)
        database.execute(self.db_path(), "INSERT INTO admin_audit_logs(admin_id,action,details) VALUES(?,'ai_settings',?)",
                         (admin_id, action))

    async def callback(self, admin_id: int, action: str, callback: dict | None = None) -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "僅 OWNER_IDS 中的負責人可管理 AI 設定。")
            return
        self.clear_reply(admin_id)
        moderation.cancel_config_input(self.db_path(), admin_id)
        settings = self.settings()
        provider = PROVIDERS[settings.ai_provider]
        back = [("返回設定", "ai:panel")]
        if action in {"setup", "mode:ai"}:
            configured = bool(settings.moderation_key) and action == "mode:ai"
            text = (
                "<b>設定規則 + AI</b>\n\n"
                f"服務商：{provider.name}\n模型：<code>{html.escape(settings.moderation_model)}</code>\n\n"
                "啟用後，未被本地規則攔截的留言文字、媒體說明和連結會傳送給這家服務商，"
                "可能產生 API 費用；圖片和語音本身不會上傳。\n\n"
                + ("將先驗證現有密鑰和模型；成功後立即啟用。" if configured else
                   "下一步傳送密鑰，驗證密鑰與模型成功後立即啟用，無需再找開關。"
                   "密鑰會經過 Telegram，輸入訊息將嘗試刪除，但通知和其他副本無法保證清除。")
            )
            confirm = (("驗證並啟用", f"ai:mode_ai_confirm:{settings.ai_provider}", "success") if configured else
                       ("輸入密鑰並啟用", "ai:setup_input", "primary"))
            await self.view(admin_id, text, [[confirm], [("更換服務商 / 模型", "ai:advanced")], back], callback)
            return
        if action == "advanced":
            await self.advanced(admin_id, callback)
            return
        if action == "rules":
            rules = spam_rules.format_rules(self.db_path()) or "暫無自訂規則"
            await self.view(admin_id, "<b>廣告規則</b>\n\n" + spam_rules.BUILTIN_RULES_TEXT +
                            "\n\n<b>自訂補充</b>\n詞語按完整短語包含匹配；域名也匹配其子域名。"
                            "這些規則僅在本地規則啟用時生效，命中後暫存，不自動封鎖。\n\n"
                            + escape_html_limited(rules, 2200),
                            [[("編輯自訂規則", "ai:rules_input")], back], callback)
            return
        if action == "providers":
            await self.view(admin_id, "<b>選擇 AI 服務商</b>\n\n切換服務商將暫停攔截並清空舊密鑰，需重新設定。",
                            [[(item.name, f"ai:provider:{key}")] for key, item in PROVIDERS.items()] + [back], callback)
            return
        if action.startswith("provider:"):
            selected = action.split(":", 1)[1]
            if selected not in PROVIDERS:
                return
            await self.view(admin_id, f"切換至 {PROVIDERS[selected].name}？\n將暫停攔截並清空原密鑰。",
                            [[("確認切換", f"ai:provider_set:{selected}", "danger")], back], callback)
            return
        if action == "key":
            await self.view(admin_id, f"<b>{provider.name} · API 密鑰</b>\n\n"
                            "密鑰會經過 Telegram；收到後嘗試刪除輸入，但無法清除通知和其他副本。"
                            "\n也可在 VPS 使用 sudo tg-bot moderation enable 設定。",
                            [[("繼續輸入", "ai:key_input", "primary")], back], callback)
            return
        if action == "model":
            await self.view(admin_id, f"<b>{provider.name} · 模型</b>\n\n請選擇或輸入完整模型 ID。",
                            [[(model, f"ai:model_set:{settings.ai_provider}:{index}")]
                             for index, model in enumerate(provider.models)] +
                            [[("輸入其他模型", "ai:model_input")], back], callback)
            return
        if action in {"setup_input", "key_input", "model_input", "rules_input", "limit"}:
            await self.prompt(admin_id, {"setup_input": "setup", "key_input": "key", "model_input": "model", "rules_input": "rules"}.get(action, action), callback)
            return
        if action == "media":
            await self.view(admin_id, "<b>無文字媒體</b>\n\n目前只檢測文字、說明和隱藏連結，不辨識圖片內容或語音。"
                            "\n直接接收更方便，但純圖片廣告也可能進入會話列表。帶說明的媒體仍會檢測。",
                            [[("暫存待確認", "ai:media_set:hold"), ("直接接收", "ai:media_set:allow")], back], callback)
            return
        if action in {"enable", "test"}:
            text = (f"<b>啟用防廣告</b>\n\n服務商：{provider.name}\n模型：{html.escape(settings.moderation_model)}\n"
                    "留言文字和連結將傳送至該服務商，會產生 API 費用。" if action == "enable" else
                    "<b>測試檢測</b>\n\n傳送一條內建測試文字，驗證目前模型的實際呼叫和 JSON 返回。"
                    "不使用私人留言；計入每日限額，可能產生少量費用。")
            await self.view(admin_id, text, [[("確認", f"ai:{action}_confirm:{settings.ai_provider}", "success")], back], callback)
            return
        try:
            async with self.lock:
                settings = self.settings()
                notice = ""
                if action.startswith("provider_set:"):
                    selected = action.split(":", 1)[1]
                    if selected not in PROVIDERS:
                        return
                    updated = replace(settings, ai_provider=selected, ai_api_key="", ai_model=PROVIDERS[selected].default_model,
                                      deepseek_api_key="", ai_moderation_enabled=False)
                    self.save(admin_id, updated, "change_provider")
                    database.execute(self.db_path(), "UPDATE admin_config_inputs SET status='canceled' WHERE status='pending'")
                    notice = "服務商已切換，請設定新密鑰。舊密鑰不會傳送至新服務商。"
                elif action.startswith("model_set:"):
                    _, selected, index = action.split(":")
                    if selected != settings.ai_provider or not index.isdigit() or len(index) > 2:
                        return
                    model = PROVIDERS[selected].models[int(index)]
                    self.save(admin_id, replace(settings, ai_model=model, deepseek_model=model if selected == "deepseek" else settings.deepseek_model), "update_model")
                    notice = "模型已更新；可使用連線檢查確認可用性。"
                elif action.startswith("media_set:"):
                    policy = action.split(":", 1)[1]
                    if policy not in {"allow", "hold"}:
                        return
                    self.save(admin_id, replace(settings, moderation_media_policy=policy), "update_media_policy")
                elif action == "check" or action.startswith(("enable_confirm:", "test_confirm:", "mode_ai_confirm:")):
                    if ":" in action and action.split(":", 1)[1] != settings.ai_provider:
                        await self.panel(admin_id, callback, "服務商已變化，請重新確認。")
                        return
                    if not settings.moderation_key or not await probe_key(settings.moderation_key, settings.ai_provider, settings.moderation_model):
                        await self.panel(admin_id, callback, "檢查失敗：請核對服務商、密鑰、模型 ID 和網路。原設定未修改。")
                        return
                    notice = "鑑權通過，模型在服務商列表中。實際呼叫可使用「測試檢測」。"
                    if action.startswith("test_confirm"):
                        with database.connect(self.db_path()) as conn:
                            conn.execute("INSERT OR IGNORE INTO moderation_usage(day) VALUES(date('now'))")
                            reserved = conn.execute("UPDATE moderation_usage SET requests=requests+1 WHERE day=date('now') AND requests<?",
                                                    (settings.moderation_daily_limit,)).rowcount
                            conn.commit()
                        if not reserved:
                            notice = "今日呼叫額度已用完，未傳送測試請求。"
                        else:
                            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                                result = await classify(client, settings.moderation_key, settings.moderation_model,
                                                        "你好，想和你聊聊天。", settings.moderation_timeout_seconds, settings.ai_provider)
                            notice = ("測試呼叫失敗：" if result.failed else "模型呼叫與解析成功：") + result.reason
                    elif action.startswith(("enable_confirm:", "mode_ai_confirm:")):
                        self.save(admin_id, replace(settings, ai_moderation_enabled=True,
                                                   spam_rules_enabled=True if action.startswith("mode_ai_confirm:") else settings.spam_rules_enabled), "enable")
                        notice = "已啟用，對方不會收到廣告判定、服務商或模型資訊。"
                elif action in {"mode:rules", "mode:off"}:
                    self.save(admin_id, replace(settings, ai_moderation_enabled=False,
                                               spam_rules_enabled=action == "mode:rules"), "change_mode")
                    notice = "已切換為僅本地規則，不會呼叫 AI。" if action == "mode:rules" else "防廣告已暫停；已有暫存區記錄仍會保留。"
                elif action in {"rules_on", "rules_off"}:
                    self.save(admin_id, replace(settings, spam_rules_enabled=action == "rules_on"), "update_rules_switch")
                    notice = "本地規則已更新。"
                elif action == "disable":
                    self.save(admin_id, replace(settings, ai_moderation_enabled=False), "disable")
                    notice = "AI 審核已暫停，本地規則按其獨立開關生效。歷史暫存仍保留在暫存區。"
                elif action != "panel":
                    return
            await self.panel(admin_id, callback, notice)
        except Exception:
            await self.panel(admin_id, callback, "設定操作未完成，請核對目前狀態。不會回顯密鑰。")

    async def message(self, message: dict[str, Any], *, edited: bool = False) -> bool:
        admin_id = int(message["from"]["id"])
        if message.get("chat", {}).get("type") != "private" or message["chat"]["id"] != admin_id:
            return False
        text = str(message.get("text") or message.get("caption") or "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        pending = moderation.pending_config_input(self.db_path(), admin_id)
        if command == "/cancel":
            moderation.cancel_config_input(self.db_path(), admin_id)
            if pending and not edited:
                await self.panel(admin_id, notice="已取消輸入。")
                return True
            return False
        if command == "/ai" and len(text.split()) == 1 and not edited:
            await self.panel(admin_id)
            return True
        reply = message.get("reply_to_message") or {}
        session = moderation.config_input(self.db_path(), admin_id, reply.get("message_id", 0))
        if not session and not reply and pending and not command.startswith("/"):
            session = pending
        input_pending = pending is not None and not command.startswith("/")
        marked = any(marker in str(reply.get("text", "")) for marker in (INPUT_MARKER, *LEGACY_INPUT_MARKERS))
        if not session and not input_pending and not marked and not POSSIBLE_SECRET.search(text) and command != "/ai":
            return False
        try:
            deleted = await self.telegram("deleteMessage", {"chat_id": admin_id, "message_id": message["message_id"]})
            if not deleted.get("ok"):
                raise ValueError("deletion failed")
        except Exception:
            await self.send(admin_id, "輸入訊息未能自動刪除，未更新設定。請手動刪除；若擔心泄露，請到服務商撤銷密鑰。")
            return True
        if not self.is_owner(admin_id) or not session or edited:
            await self.send(admin_id, "這條訊息未轉發，也未更新設定。請由負責人重新傳送 /ai。")
            return True
        callback = {"message": {"message_id": session["panel_id"], "chat": {"id": admin_id}}} if session["panel_id"] else None
        try:
            async with self.lock:
                settings = self.settings()
                if (not moderation.consume_config_input(self.db_path(), admin_id, session["prompt_id"])
                        or session["provider"] != settings.ai_provider):
                    await self.panel(admin_id, callback, "設定輸入已失效或服務商已變化，請重新操作。")
                    return True
                kind = session["kind"]
                if kind in {"key", "setup"}:
                    valid = False
                    if KEY_PATTERN.fullmatch(text):
                        valid = (await probe_key(text, settings.ai_provider, settings.moderation_model) if kind == "setup" else
                                 await probe_key(text, settings.ai_provider))
                    if not valid:
                        await self.panel(admin_id, callback, "密鑰格式或鑑權檢查失敗，原設定未修改。")
                        return True
                    updated = replace(settings, ai_api_key=text, deepseek_api_key=text if settings.ai_provider == "deepseek" else "")
                    if kind == "setup":
                        updated = replace(updated, ai_moderation_enabled=True, spam_rules_enabled=True)
                elif kind == "rules":
                    if POSSIBLE_SECRET.search(text):
                        await self.panel(admin_id, callback, "輸入含有疑似 API 密鑰，未儲存為廣告規則。請通過 API 密鑰入口設定。")
                        return True
                    try:
                        count = spam_rules.replace_rules(self.db_path(), text)
                    except ValueError as exc:
                        await self.panel(admin_id, callback, str(exc))
                        return True
                    database.execute(self.db_path(), "INSERT INTO admin_audit_logs(admin_id,action,details) VALUES(?,'moderation_rules',?)",
                                     (admin_id, f"count={count}"))
                    await self.panel(admin_id, callback, f"已儲存 {count} 條自訂規則。")
                    return True
                elif kind == "model" and MODEL_PATTERN.fullmatch(text):
                    updated = replace(settings, ai_model=text, deepseek_model=text if settings.ai_provider == "deepseek" else settings.deepseek_model)
                elif kind == "limit" and text.isascii() and text.isdigit() and len(text) <= 7 and 1 <= int(text) <= 1000000:
                    updated = replace(settings, moderation_daily_limit=int(text))
                else:
                    await self.panel(admin_id, callback, "輸入格式無效，原設定未修改。")
                    return True
                self.save(admin_id, updated, f"update_{kind}")
            notice = ("已儲存並啟用規則 + AI，無需重新啟動。疑似廣告會進入暫存區。" if kind == "setup" else
                      "已儲存並即時生效，無需重新啟動。單獨更新密鑰不改變目前保護模式。")
            await self.panel(admin_id, callback, notice)
        except Exception:
            await self.panel(admin_id, callback, "儲存未完成，請核對目前狀態。輸入不會進入聊天記錄或轉發。")
        return True
