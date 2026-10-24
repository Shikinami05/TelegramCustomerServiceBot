import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tg_bot.config import Settings
from tg_bot.ai_providers import PROVIDERS, request_options
from tg_bot.repositories import access, moderation
from tg_bot.services import spam_rules
from tg_bot.services.moderation_prompt import PROMPT_VERSION, REASON_LABELS, SYSTEM_PROMPT, parse_result


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MEDIA_METHODS = {
    "photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument",
    "audio": "sendAudio", "voice": "sendVoice", "animation": "sendAnimation",
    "sticker": "sendSticker", "video_note": "sendVideoNote",
}


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reason: str
    failed: bool = False
    reason_code: str = ""


def snapshot(message: dict[str, Any]) -> dict[str, Any]:
    fields = {"text", "caption", "entities", "caption_entities", "location", "contact", "date", "edit_date",
              "has_protected_content", *MEDIA_METHODS}
    result = {key: message[key] for key in fields if key in message}
    result["from"] = {key: message["from"][key]
                      for key in ("id", "first_name", "last_name", "username")
                      if key in message["from"]}
    return result


def review_text(message: dict[str, Any]) -> str:
    text = str(message.get("text") or message.get("caption") or "")
    # Hidden link targets matter, but user identity and previous chats are not sent.
    links = [entity["url"] for entity in
             message.get("entities", []) + message.get("caption_entities", [])
             if entity.get("type") == "text_link" and isinstance(entity.get("url"), str)]
    return text + ("\nLink targets:\n" + "\n".join(links) if links else "")


async def classify(client: httpx.AsyncClient, api_key: str, model: str,
                   text: str, timeout: int, provider: str = "deepseek", *, rule_hint: str = "") -> Verdict:
    if not text.strip():
        return Verdict("uncertain", "無可審核文字的媒體，請人工確認")
    if len(text) > 8000:
        return Verdict("uncertain", "文字過長，請人工確認")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps({"content": text, "rule_hint": rule_hint}, ensure_ascii=False)}],
        **request_options(provider, model), "stream": False,
        "response_format": {"type": "json_object"}, "max_tokens": 192, "temperature": 0,
    }
    async def read_response() -> tuple[int, bytearray]:
        async with client.stream("POST", PROVIDERS[provider].base_url + "/chat/completions", json=payload,
                                 headers={"Authorization": f"Bearer {api_key}"},
                                 follow_redirects=False) as response:
            body = bytearray()
            if response.status_code == 200:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError("oversized response")
            return response.status_code, body

    try:
        # Bound the whole response, including trickling bytes; compatible with Python 3.10.
        status, body = await asyncio.wait_for(read_response(), timeout=timeout)
        if status != 200:
            hints = {401: "密鑰無效或已失效", 402: "餘額不足", 403: "無訪問權限", 404: "模型不可用", 429: "服務限流，請稍後重試"}
            return Verdict("uncertain", hints.get(status, f"服務異常（HTTP {status}）"), True)
        choice = json.loads(body)["choices"][0]
        if choice.get("finish_reason") != "stop":
            return Verdict("uncertain", "審核響應未完整結束", True)
        try:
            verdict, reason_code = parse_result(choice["message"]["content"])
        except (ValueError, TypeError, KeyError):
            return Verdict("uncertain", "模型回傳格式或原因代碼不符，請重試或人工確認", True)
        return Verdict(verdict, REASON_LABELS[reason_code], reason_code=reason_code)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Never log the request, credentials, response body, or private message.
        return Verdict("uncertain", "服務超時、網路或響應異常，可稍後重新檢測", True)


def delivery_payload(message: dict[str, Any], chat_id: int) -> tuple[str, dict[str, Any]]:
    """Send the reviewed snapshot, never mutable content via copyMessage."""
    payload: dict[str, Any] = {"chat_id": chat_id}
    if message.get("has_protected_content"):
        payload["protect_content"] = True
    if message.get("text"):
        payload.update(text=message["text"], entities=message.get("entities", []),
                       link_preview_options={"is_disabled": True})
        return "sendMessage", payload
    for kind, method in MEDIA_METHODS.items():
        if kind not in message:
            continue
        media = message[kind][-1] if kind == "photo" else message[kind]
        payload[kind] = media["file_id"]
        if kind not in {"sticker", "video_note"} and message.get("caption"):
            payload.update(caption=message["caption"], caption_entities=message.get("caption_entities", []))
        return method, payload
    if "location" in message:
        payload.update({key: message["location"][key] for key in ("latitude", "longitude")})
        return "sendLocation", payload
    if "contact" in message:
        payload.update({key: message["contact"][key] for key in
                        ("phone_number", "first_name", "last_name", "vcard") if key in message["contact"]})
        return "sendContact", payload
    raise ValueError("unsupported message snapshot")


async def process_job(db_path: Path, job: sqlite3.Row, client: httpx.AsyncClient,
                      settings: Settings, admin_ids: set[int]) -> bool:
    update_id = int(job["update_id"])
    if access.is_blacklisted(db_path, int(job["chat_id"])):
        moderation.hold(db_path, update_id, "使用者已在封鎖名單中")
        return False
    text = review_text(json.loads(job["snapshot"]))
    provider = ""
    model = ""
    explicit = spam_rules.check_custom(db_path, text) if settings.spam_rules_enabled else None
    hint = spam_rules.check_builtin(text) if settings.spam_rules_enabled else None
    if explicit:
        result = Verdict("spam", explicit, reason_code="custom_rule")
    elif hint and not settings.ai_moderation_enabled:
        result = Verdict("spam", hint, reason_code="builtin_rule")
    elif not settings.ai_moderation_enabled:
        result = Verdict("allow", "本地規則未命中；AI 審核未啟用")
    elif not text.strip() and settings.moderation_media_policy == "allow":
        result = Verdict("allow", "按無文字媒體策略接收；未進行圖像或語音辨識")
    elif not text.strip() or len(text) > 8000:
        result = Verdict("uncertain", "無可審核文字或文字過長，請人工確認")
    elif not moderation.reserve_request(db_path, update_id, settings.moderation_daily_limit):
        result = Verdict("uncertain", "已達到每日審核呼叫上限", True)
    else:
        provider, model = settings.ai_provider, settings.moderation_model
        moderation.record_result(db_path, update_id, provider, model, "", "檢測中", prompt_version=PROMPT_VERSION)
        result = await classify(client, settings.moderation_key, model,
                                text, settings.moderation_timeout_seconds, provider=provider, rule_hint=hint or "")
    verdict = "error" if result.failed else result.verdict
    moderation.record_result(db_path, update_id, provider, model, verdict, result.reason,
                             reason_code=result.reason_code, prompt_version=PROMPT_VERSION if provider else "")
    if result.verdict == "allow":
        return moderation.approve(db_path, update_id, admin_ids)
    moderation.hold(db_path, update_id, result.reason, verdict)
    return False


async def run_worker(db_path: Path, get_settings: Callable[[], Settings], admin_ids: set[int],
                     wakeup: asyncio.Event, delivery_wakeup: asyncio.Event,
                     notify: Callable[[], Awaitable[None]], logger: logging.Logger) -> None:
    async with httpx.AsyncClient(timeout=30,
                                 follow_redirects=False, trust_env=False) as client:
        while True:
            job = None
            try:
                job = moderation.claim_next(db_path)
                if job and await process_job(db_path, job, client, get_settings(), admin_ids):
                    delivery_wakeup.set()
                await notify()
                if job:
                    continue
                wakeup.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Exception messages could contain provider responses or private data.
                logger.error("Moderation worker failed update_id=%s", job["update_id"] if job else None)
                if job:
                    with contextlib.suppress(Exception):
                        moderation.hold(db_path, int(job["update_id"]), "審核異常，請人工確認")
                await asyncio.sleep(2)
