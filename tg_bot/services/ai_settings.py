import asyncio
import html
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from tg_bot.config import Settings
from tg_bot import database
from tg_bot.keyboards import inline_keyboard
from tg_bot.moderation_config import configure_moderation
from tg_bot.repositories import moderation


INPUT_MARKER = "DeepSeek 配置输入"
KEY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{8,256}")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
POSSIBLE_SECRET = re.compile(r"\bsk-[A-Za-z0-9_.-]{8,}|DEEPSEEK_API_KEY", re.IGNORECASE)


async def probe_key(api_key: str) -> bool:
    async def request() -> bool:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "GET", "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as response:
                if response.status_code != 200:
                    return False
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        return False
                data = json.loads(body)
                return isinstance(data, dict) and isinstance(data.get("data"), list) and bool(data["data"])
    try:
        return await asyncio.wait_for(request(), timeout=6)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError):
        return False


class AISettingsController:
    """Keep credential input outside user-message routing and persistent chat logs."""

    def __init__(
        self, db_path: Callable[[], Path], env_path: Callable[[], Path],
        settings: Callable[[], Settings], apply_settings: Callable[[Settings], None],
        send: Callable[..., Awaitable[Any]], telegram: Callable[..., Awaitable[dict]],
        is_owner: Callable[[int], bool], clear_reply: Callable[[int], Any],
    ):
        self.db_path = db_path
        self.env_path = env_path
        self.settings = settings
        self.apply_settings = apply_settings
        self.send = send
        self.telegram = telegram
        self.is_owner = is_owner
        self.clear_reply = clear_reply
        self.lock = asyncio.Lock()

    async def panel(self, admin_id: int) -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "仅 OWNER_IDS 中的负责人可管理 AI 配置。")
            return
        moderation.cancel_config_input(self.db_path(), admin_id)
        self.clear_reply(admin_id)
        settings = self.settings()
        enabled = settings.ai_moderation_enabled
        await self.send(
            admin_id,
            "<b>DeepSeek 广告审核</b>\n\n"
            f"状态：{'已开启' if enabled else '已关闭'}\n"
            f"API Key：{'已配置（不显示）' if settings.deepseek_api_key else '未配置'}\n"
            f"模型：<code>{html.escape(settings.deepseek_model)}</code>\n"
            f"每日调用上限：{settings.moderation_daily_limit}（UTC）",
            reply_markup=inline_keyboard([
                [("更新 API Key", "ai:key"), ("检查连接", "ai:check")],
                [("更换模型", "ai:model"), ("每日限额", "ai:limit")],
                [("关闭审核" if enabled else "开启审核", "ai:disable" if enabled else "ai:enable",
                  "danger" if enabled else "success")],
                [("待审核消息", "moderation:1"), ("返回", "admin:dashboard")],
            ]),
        )

    async def prompt(self, admin_id: int, kind: str) -> None:
        self.clear_reply(admin_id)
        labels = {"key": "新的 DeepSeek API Key", "model": "模型名称", "limit": "每日调用次数（1 至 1000000）"}
        sent = await self.send(
            admin_id, f"<b>{INPUT_MARKER}</b>\n\n请回复这条消息，输入{labels[kind]}。"
            "\n3 分钟内有效；发送 /cancel 取消。输入内容不会作为聊天回复转发。",
            reply_markup={"force_reply": True, "selective": True},
        )
        prompt_id = getattr(sent, "message_id", None)
        if not sent or prompt_id is None:
            await self.send(admin_id, "无法建立配置输入，请稍后重试 /ai。")
            return
        moderation.begin_config_input(self.db_path(), admin_id, prompt_id, kind)

    def save(self, admin_id: int, new_settings: Settings, action: str) -> None:
        configure_moderation(
            self.env_path(), new_settings.ai_moderation_enabled,
            api_key=new_settings.deepseek_api_key,
            model=new_settings.deepseek_model,
            daily_limit=new_settings.moderation_daily_limit,
        )
        self.apply_settings(new_settings)
        # No secret or provider response is included in the audit trail.
        database.execute(self.db_path(),
                         "INSERT INTO admin_audit_logs(admin_id,action,details) VALUES(?,'ai_settings',?)",
                         (admin_id, action))

    async def callback(self, admin_id: int, action: str) -> None:
        if not self.is_owner(admin_id):
            await self.send(admin_id, "仅 OWNER_IDS 中的负责人可管理 AI 配置。")
            return
        if action == "key":
            self.clear_reply(admin_id)
            await self.send(admin_id,
                "<b>更新 DeepSeek API Key</b>\n\n密钥将经过 Telegram，收到后 Bot 会尝试删除输入消息，"
                "但不能保证清除通知或其他副本。不要在群聊输入密钥。"
                "\n更稳妥的方式仍是在 VPS 使用 sudo tg-bot moderation enable。",
                reply_markup=inline_keyboard([[("继续更新", "ai:key_input"), ("取消", "ai:panel")]]))
            return
        if action in {"key_input", "model", "limit"}:
            if action != "key_input" and not self.settings().deepseek_api_key:
                await self.send(admin_id, "请先配置 API Key。")
                return
            await self.prompt(admin_id, "key" if action == "key_input" else action)
            return
        if action == "enable":
            await self.send(admin_id, "开启后，用户留言的文字和链接会提交给 DeepSeek，并产生 API 费用。确认开启？",
                            reply_markup=inline_keyboard([[("确认开启", "ai:enable_confirm", "success"), ("取消", "ai:panel")]]))
            return
        try:
            async with self.lock:
                settings = self.settings()
                if action in {"check", "enable_confirm"}:
                    if not settings.deepseek_api_key or not await probe_key(settings.deepseek_api_key):
                        await self.send(admin_id, "DeepSeek 鉴权检查失败，原配置未修改。请检查密钥和网络。")
                        return
                if action == "check":
                    await self.send(admin_id, "DeepSeek 鉴权通过；这不代表余额充足或所选模型的审核效果已验证。")
                    return
                if action in {"enable_confirm", "disable"}:
                    self.save(admin_id, replace(settings, ai_moderation_enabled=action == "enable_confirm"), action)
                elif action != "panel":
                    return
            await self.panel(admin_id)
        except Exception:
            await self.send(admin_id, "配置操作未能完成，请用 /ai 核对当前状态；不会回显密钥。")

    async def message(self, message: dict[str, Any], *, edited: bool = False) -> bool:
        admin_id = int(message["from"]["id"])
        if message.get("chat", {}).get("type") != "private" or message["chat"]["id"] != admin_id:
            return False
        text = str(message.get("text") or message.get("caption") or "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        if command == "/cancel":
            moderation.cancel_config_input(self.db_path(), admin_id)
            return False
        if command == "/ai" and len(text.split()) == 1 and not edited:
            await self.panel(admin_id)
            return True
        reply = message.get("reply_to_message") or {}
        prompt_id = reply.get("message_id", 0)
        session = moderation.config_input(self.db_path(), admin_id, prompt_id)
        if not session and INPUT_MARKER not in str(reply.get("text", "")) and not POSSIBLE_SECRET.search(text) and command != "/ai":
            return False
        # Delete before parsing or validating so invalid and expired key inputs are covered too.
        try:
            deleted = await self.telegram("deleteMessage", {"chat_id": admin_id, "message_id": message["message_id"]})
            if not deleted.get("ok"):
                raise ValueError("deletion failed")
        except Exception:
            await self.send(admin_id, "输入消息未能自动删除，未更新配置。请手动删除；若担心泄露，请在 DeepSeek 撤销该密钥。")
            return True
        if not self.is_owner(admin_id) or not session or edited:
            await self.send(admin_id, "这条消息未转发，也未更新配置。请由负责人重新发送 /ai 操作。")
            return True
        if not moderation.consume_config_input(self.db_path(), admin_id, prompt_id):
            await self.send(admin_id, "配置输入已失效或已使用，请重新发送 /ai。")
            return True
        try:
            async with self.lock:
                settings = self.settings()
                kind = session["kind"]
                if kind == "key":
                    if not KEY_PATTERN.fullmatch(text):
                        await self.send(admin_id, "密钥格式无效，原配置未修改。请重新发送 /ai。")
                        return True
                    if not await probe_key(text):
                        await self.send(admin_id, "新密钥鉴权失败，原配置未修改。请检查密钥和网络后重试 /ai。")
                        return True
                    updated = replace(settings, deepseek_api_key=text)
                elif kind == "model" and MODEL_PATTERN.fullmatch(text):
                    updated = replace(settings, deepseek_model=text)
                elif kind == "limit" and text.isascii() and text.isdigit() and len(text) <= 7 and 1 <= int(text) <= 1000000:
                    updated = replace(settings, moderation_daily_limit=int(text))
                else:
                    await self.send(admin_id, "输入格式无效，原配置未修改。请重新发送 /ai。")
                    return True
                self.save(admin_id, updated, f"update_{kind}")
            await self.send(admin_id, "配置已保存，后续审核立即使用新配置，无需重启。更新密钥不会自动开启审核。")
            await self.panel(admin_id)
        except Exception:
            await self.send(admin_id, "保存未能完成，请通过 /ai 核对状态；输入内容不会进入聊天记录或转发。")
        return True

