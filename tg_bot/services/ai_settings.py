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
from tg_bot.services.moderation_prompt import PROMPT_VERSION
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
        usage = database.fetchone(self.db_path(), "SELECT requests FROM moderation_usage WHERE day=date('now')")
        primary = (("暫停 AI 審核", "ai:disable", "danger") if settings.ai_moderation_enabled else
                   ("啟用 AI 語意審核", "ai:mode:ai", "success") if settings.moderation_key else
                   ("設定密鑰並啟用", "ai:setup", "primary"))
        text = (
            "<b>AI 防廣告</b>\n\n"
            f"<b>目前模式：{settings.moderation_mode_label}</b>\n"
            "正常留言繼續聊天；廣告安靜暫存，不向對方展示判定，也不自動封鎖。\n\n"
            f"<b>連線設定</b>\n服務商：{provider.name}\n"
            f"模型：<code>{html.escape(settings.moderation_model)}</code>\n"
            f"密鑰：{'已設定（不代表已通過連線檢查）' if settings.moderation_key else '未設定'}\n\n"
            f"<b>今日用量</b>\n呼叫：{usage[0] if usage else 0} / {settings.moderation_daily_limit}（UTC）\n"
            "檢測故障與內容不確定會分開記錄；暫存留言可由你放行。"
        )
        if notice:
            text += "\n\n" + html.escape(notice)
        await self.view(admin_id, text, [
            [primary],
            [("連線設定", "ai:connection"), ("檢查連線", "ai:check")],
            [("檢視暫存", "moderation:1"), ("判定標準", "ai:policy")],
            [("更多設定", "ai:advanced")],
            [("返回設定", "settings:panel")],
        ], callback)

    async def advanced(self, admin_id: int, callback: dict | None = None) -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "僅負責人可管理防廣告設定。")
            return
        settings = self.settings()
        provider = PROVIDERS[settings.ai_provider]
        usage = database.fetchone(self.db_path(), "SELECT requests FROM moderation_usage WHERE day=date('now')")
        await self.view(admin_id,
            f"<b>防廣告 · 進階設定</b>\n\n服務商：{provider.name}\n"
            f"模型：<code>{html.escape(settings.moderation_model)}</code>\n"
            f"今日呼叫：{usage[0] if usage else 0} / {settings.moderation_daily_limit}（UTC）\n"
            f"無文字媒體：{'直接接收' if settings.moderation_media_policy == 'allow' else '暫存待確認'}",
            [
                [("僅本地規則", "ai:mode:rules"), ("暫停所有審核", "ai:mode:off", "danger")],
                [("自訂封鎖規則", "ai:rules"), ("每日限額", "ai:limit")],
                [("無文字媒體", "ai:media"), ("合成樣例測試", "ai:test")],
                [("連線設定", "ai:connection")],
                [("返回防廣告", "ai:panel")],
            ], callback)

    async def prompt(self, admin_id: int, kind: str, callback: dict | None = None) -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "僅負責人可管理防廣告設定。")
            return
        self.clear_reply(admin_id)
        settings = self.settings()
        labels = {"rules": "自訂封鎖規則，每行填寫「詞語：短語」或「域名：example.com」；命中後直接暫存；傳送「清空」刪除自訂規則", "key": f"{PROVIDERS[settings.ai_provider].name} API Key", "setup": f"{PROVIDERS[settings.ai_provider].name} API Key（驗證成功後啟用 AI 語意審核）", "model": "完整模型 ID", "limit": "每日呼叫次數（1 至 1000000）"}
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
        back = [("返回防廣告", "ai:panel")]
        if action == "connection":
            await self.view(admin_id, f"<b>AI 連線設定</b>\n\n服務商：{provider.name}\n"
                            f"模型：<code>{html.escape(settings.moderation_model)}</code>\n"
                            f"密鑰：{'已設定' if settings.moderation_key else '未設定'}\n\n"
                            "變更服務商會清空舊密鑰並暫停 AI；更新密鑰不會改變目前模式。"
                            "檢查連線驗證鑑權與模型；合成樣例測試才會呼叫實際審核。",
                            [[("服務商", "ai:providers"), ("模型", "ai:model")],
                             [("更新密鑰", "ai:key"), ("檢查連線", "ai:check")], back], callback)
            return
        if action == "policy":
            await self.view(admin_id, "<b>AI 判定標準</b>\n\n"
                            "<b>判斷意圖，不只看詞語</b>\n一般聊天、合理接洽、分享與舉報優先保留。"
                            "單獨出現哈希、USDT、兼職、連結或 @帳號不等於廣告；明確招攬投注、充值、"
                            "導流或可疑任務才會暫存。\n\n"
                            "<b>規則與 AI</b>\nAI 模式下，內建規則只是提示，由模型複核；"
                            "本地規則啟用時，你自訂的封鎖詞與域名仍直接暫存，不交給 AI 推翻。\n\n"
                            "<b>資料範圍</b>\n只送這則留言的文字、說明與隱藏連結，不送歷史、ID、暱稱，"
                            "不下載媒體或開啟連結。輸入中的指令視為資料，不允許模型執行操作。\n\n"
                            "放行只處理這則留言，不會自動訓練模型。\n"
                            f"提示詞版本：<code>{PROMPT_VERSION}</code>",
                            [[("合成樣例測試", "ai:test")], back], callback)
            return
        if action == "mode:ai" and settings.ai_moderation_enabled:
            await self.panel(admin_id, callback, "AI 語意審核目前已啟用。")
            return
        if action in {"setup", "mode:ai"}:
            configured = bool(settings.moderation_key) and action == "mode:ai"
            text = (
                "<b>啟用 AI 語意審核</b>\n\n"
                f"服務商：{provider.name}\n模型：<code>{html.escape(settings.moderation_model)}</code>\n\n"
                "啟用後，除自訂封鎖規則命中的內容，留言文字、媒體說明和連結會傳送給這家服務商，"
                "可能產生 API 費用；圖片和語音本身不會上傳。\n\n"
                + ("將先驗證現有密鑰和模型；成功後立即啟用。" if configured else
                   "下一步傳送密鑰，驗證密鑰與模型成功後立即啟用，無需再找開關。"
                   "密鑰會經過 Telegram，輸入訊息將嘗試刪除，但通知和其他副本無法保證清除。")
            )
            confirm = (("驗證並啟用", f"ai:mode_ai_confirm:{settings.ai_provider}", "success") if configured else
                       ("輸入密鑰並啟用", "ai:setup_input", "primary"))
            await self.view(admin_id, text, [[confirm], [("更換服務商 / 模型", "ai:connection")], back], callback)
            return
        if action == "advanced":
            await self.advanced(admin_id, callback)
            return
        if action == "rules":
            rules = spam_rules.format_rules(self.db_path()) or "暫無自訂規則"
            await self.view(admin_id, "<b>廣告規則</b>\n\n" + spam_rules.BUILTIN_RULES_TEXT +
                            "\n\n<b>自訂補充</b>\n詞語按完整短語包含匹配；域名也匹配其子域名。"
                            "自訂規則啟用時，命中後直接暫存，AI 不會推翻；內建規則在 AI 模式只提供線索。"
                            "僅本地規則模式則由本地規則判定，不自動封鎖。\n\n"
                            + escape_html_limited(rules, 2200),
                            [[("編輯自訂規則", "ai:rules_input")], back], callback)
            return
        if action == "providers":
            await self.view(admin_id, "<b>選擇 AI 服務商</b>\n\n切換服務商將暫停 AI 並清空舊密鑰，需重新設定。本地規則仍按目前開關生效。",
                            [[(item.name, f"ai:provider:{key}")] for key, item in PROVIDERS.items()] + [back], callback)
            return
        if action.startswith("provider:"):
            selected = action.split(":", 1)[1]
            if selected not in PROVIDERS:
                return
            await self.view(admin_id, f"切換至 {PROVIDERS[selected].name}？\n將暫停 AI 並清空原密鑰，本地規則開關不變。",
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
                    "<b>合成樣例測試</b>\n\n傳送兩條本專案原創的合成文字：一般聊天與明確博彩招攬。"
                    "檢查實際呼叫、JSON 解析與兩例是否符合預期，不代表整體準確率。"
                    "不使用私人留言；占用 2 次每日額度，可能產生少量費用。")
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
                    notice = "鑑權通過，模型在服務商列表中。實際呼叫可使用「合成樣例測試」。"
                    if action.startswith("test_confirm"):
                        with database.connect(self.db_path()) as conn:
                            conn.execute("INSERT OR IGNORE INTO moderation_usage(day) VALUES(date('now'))")
                            reserved = conn.execute("UPDATE moderation_usage SET requests=requests+2 WHERE day=date('now') AND requests<=?",
                                                    (settings.moderation_daily_limit - 2,)).rowcount
                            conn.commit()
                        if not reserved:
                            notice = "測試需要 2 次額度；今日剩餘不足，未傳送測試請求。"
                        else:
                            results = []
                            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                                for label, text, expected in (
                                    ("一般聊天", "你好，想和你聊聊天。", "allow"),
                                    ("博彩招攬", "哈希投注限時加碼，充值領獎，私訊報名進群。", "spam"),
                                ):
                                    result = await classify(client, settings.moderation_key, settings.moderation_model,
                                                            text, settings.moderation_timeout_seconds, settings.ai_provider)
                                    outcome = "呼叫未完成" if result.failed else "符合預期" if result.verdict == expected else "與預期不符"
                                    results.append(f"{label}：{outcome}（{result.reason}）")
                            notice = "；".join(results) + "。只代表這兩個樣例，未更改審核開關。"
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
                    notice = ("AI 已暫停，已回到僅本地規則；不再傳送新留言給服務商。" if settings.spam_rules_enabled else
                              "AI 已暫停；本地規則也未啟用，新留言直接接收。") + "歷史暫存仍保留。"
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
            notice = ("已儲存並啟用 AI 語意審核，無需重新啟動。疑似廣告會進入暫存區。" if kind == "setup" else
                      "已儲存並即時生效，無需重新啟動。單獨更新密鑰不改變目前保護模式。")
            await self.panel(admin_id, callback, notice)
        except Exception:
            await self.panel(admin_id, callback, "儲存未完成，請核對目前狀態。輸入不會進入聊天記錄或轉發。")
        return True
