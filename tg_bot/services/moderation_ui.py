import json
from pathlib import Path

from tg_bot.ai_providers import PROVIDERS
from tg_bot.keyboards import inline_keyboard, pagination_navigation_row
from tg_bot.repositories import moderation
from tg_bot.services.moderation import delivery_payload
from tg_bot.text import escape_html_limited


LABELS = {"all": "全部", "spam": "疑似廣告", "error": "檢測故障", "other": "待確認", "pending": "待確認"}


def escaped(text: str, limit: int) -> str:
    return escape_html_limited(text, limit)


def source(job) -> str:
    provider = PROVIDERS.get(job["provider"])
    if provider:
        return f"{provider.name} · {escaped(job['model'], 200)}"
    if job["reason_code"] == "custom_rule":
        return "自訂封鎖規則（未呼叫 AI）"
    if job["reason_code"] == "builtin_rule":
        return "內建本地規則（未呼叫 AI）"
    return "本地策略 / 歷史記錄未記錄模型"


class ModerationInbox:
    def __init__(self, db: Path, admin_ids: set[int], present, send, telegram, answer,
                 delivery_wakeup, moderation_wakeup, enabled: bool):
        self.db, self.admin_ids = db, admin_ids
        self.present, self.send, self.telegram, self.answer = present, send, telegram, answer
        self.delivery_wakeup, self.moderation_wakeup = delivery_wakeup, moderation_wakeup
        self.enabled = enabled

    async def show(self, admin_id: int, page: int = 1, callback=None, category: str = "spam") -> None:
        category = "other" if category == "pending" else category
        jobs, page, pages = moderation.pending_page(self.db, page, category=category)
        lines = [f"<b>暫存區 · {LABELS[category]}</b>", ""]
        buttons = [[(LABELS[key] + (" · 目前" if key == category else ""), f"moderation:1:{key}")
                    for key in ("spam", "other", "error")]]
        for index, job in enumerate(jobs, start=(page - 1) * 5 + 1):
            user = json.loads(job["snapshot"]).get("from", {})
            name = user.get("first_name") or user.get("username") or "未命名聯絡人"
            lines.append(f"<b>{index}. {escaped(name, 40)}</b>\n"
                         f"{escaped(job['reason'], 80)}\n{escaped(job['summary'], 150)}\n")
            buttons.append([(f"{index}. 查看並處理", f"moderation_detail:{job['update_id']}:{page}:{category}")])
        if not jobs:
            lines.append("這裡沒有待處理留言。")
        if pages > 1:
            navigation = pagination_navigation_row("moderation", page, pages)
            buttons.append([(item[0], f"{item[1]}:{category}") for item in navigation])
        buttons.append([("重新整理", f"moderation:{page}:{category}"), ("返回首頁", "admin:dashboard")])
        await self.present(admin_id, "\n".join(lines), inline_keyboard(buttons), callback)

    async def detail(self, admin_id: int, job, page: int, category: str, callback) -> None:
        suffix = f"{job['update_id']}:{page}:{category}"
        user = json.loads(job["snapshot"]).get("from", {})
        name = " ".join(str(user.get(field) or "") for field in ("first_name", "last_name")).strip()
        username = f"@{user['username']}" if user.get("username") else "未設定"
        prompt_version = job["prompt_version"] or ("舊版本未記錄" if job["provider"] else "不適用（本地規則）")
        text = ("<b>暫存留言</b>\n\n"
                f"使用者：{escaped(name, 80)} · <code>{job['chat_id']}</code>\n"
                f"Username：{escaped(username, 80)}\n"
                f"檢測來源：{source(job)}\n"
                f"提示詞版本：{escaped(prompt_version, 80)}\n"
                f"結果：{escaped(job['reason'], 200)}\n\n"
                f"{escaped(job['summary'], 1100)}")
        await self.present(admin_id, text, inline_keyboard([
            [("放行本條", f"moderation_allow:{suffix}", "success"), ("忽略本條", f"moderation_dismiss:{suffix}")],
            [("查看原訊息", f"moderation_preview:{suffix}"), ("重新檢測", f"moderation_retry:{suffix}")],
            [("封鎖聯絡人", f"moderation_block:{suffix}", "danger")],
            [("返回列表", f"moderation:{page}:{category}")],
        ]), callback)

    async def callback(self, callback: dict, action: str, value: str) -> None:
        admin_id, callback_id = int(callback["from"]["id"]), callback["id"]
        parts = value.split(":")
        raw = parts[0]
        if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
            await self.answer(callback_id, "參數無效")
            return
        number = int(raw)
        if action == "moderation":
            if len(parts) > 2 or (len(parts) == 2 and parts[1] not in LABELS):
                await self.answer(callback_id, "分類無效")
                return
            await self.answer(callback_id)
            await self.show(admin_id, number, callback, parts[1] if len(parts) == 2 else "spam")
            return
        if len(parts) not in {1, 3} or (len(parts) == 3 and (
                not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 9 or parts[2] not in LABELS)):
            await self.answer(callback_id, "參數無效")
            return
        page, category = (int(parts[1]), parts[2]) if len(parts) == 3 else (1, "all")
        job = moderation.get(self.db, number)
        if not job or job["status"] != "held":
            await self.answer(callback_id, "已處理或被編輯，請以重新整理後的列表為準")
            await self.show(admin_id, page, callback, category)
            return
        if action == "moderation_detail":
            await self.answer(callback_id)
            await self.detail(admin_id, job, page, category, callback)
            return
        if action == "moderation_block":
            await self.answer(callback_id)
            await self.present(admin_id, f"<b>封鎖聯絡人</b>\n\n使用者 <code>{job['chat_id']}</code> 的所有待處理留言和後續訊息將被攔截。",
                               inline_keyboard([[("確認封鎖", f"moderation_confirm:{number}:{page}:{category}", "danger")],
                                                [("取消", f"moderation_detail:{number}:{page}:{category}")]]), callback)
            return
        if action == "moderation_preview":
            await self.answer(callback_id, "原訊息僅供查看，不會放行")
            try:
                method, payload = delivery_payload(json.loads(job["snapshot"]), admin_id)
                result = await self.telegram(method, payload)
                if not result.get("ok"):
                    raise ValueError("preview failed")
            except Exception:
                await self.send(admin_id, "原訊息暫時無法顯示，暫存記錄未改變。")
            return
        if action == "moderation_allow":
            changed = moderation.approve(self.db, number, self.admin_ids, admin_id)
            if changed and self.delivery_wakeup:
                self.delivery_wakeup.set()
            notice = "已放入會話列表"
        elif action == "moderation_confirm":
            changed = moderation.block(self.db, number, admin_id)
            notice = "已封鎖聯絡人"
        elif action in {"moderation_dismiss", "moderation_retry"}:
            retry = action == "moderation_retry"
            if retry and not self.enabled:
                await self.answer(callback_id, "防廣告已暫停，請先到設定中啟用")
                return
            changed = moderation.decide(self.db, number, admin_id, retry=retry)
            if changed and retry and self.moderation_wakeup:
                self.moderation_wakeup.set()
            notice = "已重新排隊" if retry else "已忽略本條，未封鎖聯絡人"
        else:
            await self.answer(callback_id, "未知操作")
            return
        await self.answer(callback_id, notice if changed else "狀態已變化，請重新整理")
        await self.show(admin_id, page, callback, category)
