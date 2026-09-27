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
from tg_bot.services.moderation import classify


INPUT_MARKER = "AI 配置输入"
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
            await self.send(admin_id, "仅 OWNER_IDS 中的负责人可管理 AI 配置。")
            return
        moderation.cancel_config_input(self.db_path(), admin_id)
        self.clear_reply(admin_id)
        settings = self.settings()
        provider = PROVIDERS[settings.ai_provider]
        usage = database.fetchone(self.db_path(), "SELECT requests FROM moderation_usage WHERE day=date('now')")
        enabled = settings.ai_moderation_enabled
        await self.view(admin_id, "<b>防广告设置</b>\n\n"
            f"状态：{'已开启' if enabled else '已暂停'}\n服务商：{provider.name}\n"
            f"模型：<code>{html.escape(settings.moderation_model)}</code>\n"
            f"密钥：{'已配置（不显示）' if settings.moderation_key else '未配置'}\n"
            f"今日调用：{usage[0] if usage else 0} / {settings.moderation_daily_limit}（UTC）\n"
            f"无文字媒体：{'直接接收' if settings.moderation_media_policy == 'allow' else '暂存待确认'}\n"
            "疑似广告或检测失败：暂存，由管理员决定。不会自动封禁用户。"
            + ("\n\n" + html.escape(notice) if notice else ""), [
                [("服务商 · " + provider.name, "ai:providers")],
                [("API 密钥", "ai:key"), ("模型", "ai:model")],
                [("连接检查", "ai:check"), ("测试检测", "ai:test")],
                [("每日限额", "ai:limit"), ("无文字媒体", "ai:media")],
                [("暂停拦截" if enabled else "开启拦截", "ai:disable" if enabled else "ai:enable",
                  "danger" if enabled else "success")],
                [("暂存箱", "moderation:1"), ("返回工作台", "admin:dashboard")],
            ], callback)

    async def prompt(self, admin_id: int, kind: str, callback: dict | None = None) -> None:
        self.clear_reply(admin_id)
        settings = self.settings()
        labels = {"key": f"{PROVIDERS[settings.ai_provider].name} API Key", "model": "完整模型 ID", "limit": "每日调用次数（1 至 1000000）"}
        if callback:
            await self.view(admin_id, f"<b>等待输入</b>\n\n{labels[kind]}\n3 分钟内有效。",
                            [[("取消输入", "ai:panel")]], callback)
        sent = await self.send(admin_id, f"<b>{INPUT_MARKER}</b>\n\n请输入{labels[kind]}。"
                              "\n回复此消息或直接发送均可；/cancel 取消。输入内容不会转发给用户。",
                              reply_markup={"force_reply": True, "selective": True})
        prompt_id = getattr(sent, "message_id", None)
        if not sent or prompt_id is None:
            await self.panel(admin_id, callback, "无法建立输入，请重试。")
            return
        panel_id = (callback or {}).get("message", {}).get("message_id")
        moderation.begin_config_input(self.db_path(), admin_id, prompt_id, kind, settings.ai_provider, panel_id)

    def save(self, admin_id: int, settings: Settings, action: str) -> None:
        configure_moderation(self.env_path(), settings.ai_moderation_enabled,
                             api_key=settings.moderation_key, model=settings.moderation_model,
                             daily_limit=settings.moderation_daily_limit, provider=settings.ai_provider,
                             media_policy=settings.moderation_media_policy)
        self.apply_settings(settings)
        database.execute(self.db_path(), "INSERT INTO admin_audit_logs(admin_id,action,details) VALUES(?,'ai_settings',?)",
                         (admin_id, action))

    async def callback(self, admin_id: int, action: str, callback: dict | None = None) -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "仅 OWNER_IDS 中的负责人可管理 AI 配置。")
            return
        self.clear_reply(admin_id)
        moderation.cancel_config_input(self.db_path(), admin_id)
        settings = self.settings()
        provider = PROVIDERS[settings.ai_provider]
        back = [("返回设置", "ai:panel")]
        if action == "providers":
            await self.view(admin_id, "<b>选择 AI 服务商</b>\n\n切换服务商将暂停拦截并清空旧密钥，需重新配置。",
                            [[(item.name, f"ai:provider:{key}")] for key, item in PROVIDERS.items()] + [back], callback)
            return
        if action.startswith("provider:"):
            selected = action.split(":", 1)[1]
            if selected not in PROVIDERS:
                return
            await self.view(admin_id, f"切换至 {PROVIDERS[selected].name}？\n将暂停拦截并清空原密钥。",
                            [[("确认切换", f"ai:provider_set:{selected}", "danger")], back], callback)
            return
        if action == "key":
            await self.view(admin_id, f"<b>{provider.name} · API 密钥</b>\n\n"
                            "密钥会经过 Telegram；收到后尝试删除输入，但无法清除通知和其他副本。"
                            "\n也可在 VPS 使用 sudo tg-bot moderation enable 配置。",
                            [[("继续输入", "ai:key_input", "primary")], back], callback)
            return
        if action == "model":
            await self.view(admin_id, f"<b>{provider.name} · 模型</b>\n\n请选择或输入完整模型 ID。",
                            [[(model, f"ai:model_set:{settings.ai_provider}:{index}")]
                             for index, model in enumerate(provider.models)] +
                            [[("输入其他模型", "ai:model_input")], back], callback)
            return
        if action in {"key_input", "model_input", "limit"}:
            await self.prompt(admin_id, {"key_input": "key", "model_input": "model"}.get(action, action), callback)
            return
        if action == "media":
            await self.view(admin_id, "<b>无文字媒体</b>\n\n当前只检测文字、说明和隐藏链接，不识别图片内容或语音。"
                            "\n直接接收更方便，但纯图片广告也可能进入收件箱。带说明的媒体仍会检测。",
                            [[("暂存待确认", "ai:media_set:hold"), ("直接接收", "ai:media_set:allow")], back], callback)
            return
        if action in {"enable", "test"}:
            text = (f"<b>开启防广告</b>\n\n服务商：{provider.name}\n模型：{html.escape(settings.moderation_model)}\n"
                    "留言文字和链接将发送至该服务商，会产生 API 费用。" if action == "enable" else
                    "<b>测试检测</b>\n\n发送一条内置测试文本，验证当前模型的实际调用和 JSON 返回。"
                    "不使用私人留言；计入每日限额，可能产生少量费用。")
            await self.view(admin_id, text, [[("确认", f"ai:{action}_confirm:{settings.ai_provider}", "success")], back], callback)
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
                    notice = "服务商已切换，请配置新密钥。旧密钥不会发送至新服务商。"
                elif action.startswith("model_set:"):
                    _, selected, index = action.split(":")
                    if selected != settings.ai_provider or not index.isdigit() or len(index) > 2:
                        return
                    model = PROVIDERS[selected].models[int(index)]
                    self.save(admin_id, replace(settings, ai_model=model, deepseek_model=model if selected == "deepseek" else settings.deepseek_model), "update_model")
                    notice = "模型已更新；可使用连接检查确认可用性。"
                elif action.startswith("media_set:"):
                    policy = action.split(":", 1)[1]
                    if policy not in {"allow", "hold"}:
                        return
                    self.save(admin_id, replace(settings, moderation_media_policy=policy), "update_media_policy")
                elif action == "check" or action.startswith(("enable_confirm", "test_confirm")):
                    if ":" in action and action.split(":", 1)[1] != settings.ai_provider:
                        await self.panel(admin_id, callback, "服务商已变化，请重新确认。")
                        return
                    if not settings.moderation_key or not await probe_key(settings.moderation_key, settings.ai_provider, settings.moderation_model):
                        await self.panel(admin_id, callback, "检查失败：请核对服务商、密钥、模型 ID 和网络。原配置未修改。")
                        return
                    notice = "鉴权通过，模型在服务商列表中。实际调用可使用「测试检测」。"
                    if action.startswith("test_confirm"):
                        with database.connect(self.db_path()) as conn:
                            conn.execute("INSERT OR IGNORE INTO moderation_usage(day) VALUES(date('now'))")
                            reserved = conn.execute("UPDATE moderation_usage SET requests=requests+1 WHERE day=date('now') AND requests<?",
                                                    (settings.moderation_daily_limit,)).rowcount
                            conn.commit()
                        if not reserved:
                            notice = "今日调用额度已用完，未发送测试请求。"
                        else:
                            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                                result = await classify(client, settings.moderation_key, settings.moderation_model,
                                                        "你好，想和你聊聊天。", settings.moderation_timeout_seconds, settings.ai_provider)
                            notice = ("测试调用失败：" if result.failed else "模型调用与解析成功：") + result.reason
                    elif action.startswith("enable_confirm"):
                        self.save(admin_id, replace(settings, ai_moderation_enabled=True), "enable")
                        notice = "防广告已开启，用户只会收到普通留言回执。"
                elif action == "disable":
                    self.save(admin_id, replace(settings, ai_moderation_enabled=False), "disable")
                    notice = "已暂停。历史暂存内容仍保留，请在暂存箱处理。"
                elif action != "panel":
                    return
            await self.panel(admin_id, callback, notice)
        except Exception:
            await self.panel(admin_id, callback, "配置操作未完成，请核对当前状态。不会回显密钥。")

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
                await self.panel(admin_id, notice="已取消输入。")
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
        marked = any(marker in str(reply.get("text", "")) for marker in (INPUT_MARKER, "DeepSeek 配置输入"))
        if not session and not input_pending and not marked and not POSSIBLE_SECRET.search(text) and command != "/ai":
            return False
        try:
            deleted = await self.telegram("deleteMessage", {"chat_id": admin_id, "message_id": message["message_id"]})
            if not deleted.get("ok"):
                raise ValueError("deletion failed")
        except Exception:
            await self.send(admin_id, "输入消息未能自动删除，未更新配置。请手动删除；若担心泄露，请到服务商撤销密钥。")
            return True
        if not self.is_owner(admin_id) or not session or edited:
            await self.send(admin_id, "这条消息未转发，也未更新配置。请由负责人重新发送 /ai。")
            return True
        callback = {"message": {"message_id": session["panel_id"], "chat": {"id": admin_id}}} if session["panel_id"] else None
        try:
            async with self.lock:
                settings = self.settings()
                if (not moderation.consume_config_input(self.db_path(), admin_id, session["prompt_id"])
                        or session["provider"] != settings.ai_provider):
                    await self.panel(admin_id, callback, "配置输入已失效或服务商已变化，请重新操作。")
                    return True
                kind = session["kind"]
                if kind == "key":
                    if not KEY_PATTERN.fullmatch(text) or not await probe_key(text, settings.ai_provider):
                        await self.panel(admin_id, callback, "密钥格式或鉴权检查失败，原配置未修改。")
                        return True
                    updated = replace(settings, ai_api_key=text, deepseek_api_key=text if settings.ai_provider == "deepseek" else "")
                elif kind == "model" and MODEL_PATTERN.fullmatch(text):
                    updated = replace(settings, ai_model=text, deepseek_model=text if settings.ai_provider == "deepseek" else settings.deepseek_model)
                elif kind == "limit" and text.isascii() and text.isdigit() and len(text) <= 7 and 1 <= int(text) <= 1000000:
                    updated = replace(settings, moderation_daily_limit=int(text))
                else:
                    await self.panel(admin_id, callback, "输入格式无效，原配置未修改。")
                    return True
                self.save(admin_id, updated, f"update_{kind}")
            await self.panel(admin_id, callback, "已保存并即时生效，无需重启。更新密钥不会自动开启拦截。")
        except Exception:
            await self.panel(admin_id, callback, "保存未完成，请核对当前状态。输入不会进入聊天记录或转发。")
        return True
