from typing import Any

from tg_bot import ui
from tg_bot.text import truncate_text

ButtonSpec = tuple[str, str] | tuple[str, str, str]
INLINE_BUTTON_STYLES = {"primary", "success", "danger"}
REPLY_MODE_BUTTON_STYLE = "primary"


def inline_keyboard(rows: list[list[ButtonSpec]]) -> dict[str, Any]:
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        keyboard_row: list[dict[str, str]] = []
        for button_spec in row:
            if len(button_spec) not in {2, 3}:
                raise ValueError(
                    "inline button must contain text, data, and optional style"
                )
            text, data = button_spec[:2]
            if not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
                raise ValueError("callback data must contain 1 to 64 UTF-8 bytes")
            button = {"text": text, "callback_data": data}
            if len(button_spec) == 3:
                style = button_spec[2]
                if style not in INLINE_BUTTON_STYLES:
                    raise ValueError(f"unsupported inline button style: {style}")
                button["style"] = style
            keyboard_row.append(button)
        keyboard.append(keyboard_row)
    return {"inline_keyboard": keyboard}


def admin_user_keyboard(
    chat_id: int, *, blacklisted: bool = False, active_reply: bool = False,
    read_through_seq: int = 0, read_done: bool = False,
) -> dict[str, Any]:
    if blacklisted:
        rows: list[list[ButtonSpec]] = [
            [("使用者詳情", f"detail:{chat_id}"), ("解除封鎖", f"unblacklist:{chat_id}", "success")],
            [("更多操作", f"more:{chat_id}")],
        ]
    else:
        reply: ButtonSpec = (
            ("退出回覆", f"cancel:{chat_id}", "danger") if active_reply
            else ("持續回覆", f"reply:{chat_id}", REPLY_MODE_BUTTON_STYLE)
        )
        rows = [
            [reply, ("此則已讀" if read_done else "標為已讀", f"read:{chat_id}:{read_through_seq}")],
            [("使用者詳情", f"detail:{chat_id}"), ("更多操作", f"more:{chat_id}")],
        ]
    rows.append([("返回會話列表", "queue:all:1")])
    return inline_keyboard(rows)


def admin_dashboard_keyboard(counts: dict[str, int], *, show_ai_settings: bool = False,
                             active_chat_id: int | None = None) -> dict[str, Any]:
    rows: list[list[ButtonSpec]] = [
        [(f"未讀留言 · {counts['inbox']}", "queue:inbox:1", "primary"),
         (f"所有會話 · {counts['all']}", "queue:all:1")],
        [(ui.ALL_READ, "read_all:dashboard:1", "success")],
        [("設定", "settings:panel"), (ui.REFRESH, "admin:dashboard")],
    ]
    if active_chat_id is not None:
        rows.insert(0, [("查看目前會話", f"detail:{active_chat_id}"),
                        ("退出回覆", f"cancel:{active_chat_id}", "danger")])
    return inline_keyboard(rows)


def pagination_navigation_row(callback_prefix: str, page: int, total_pages: int) -> list[ButtonSpec]:
    row: list[ButtonSpec] = []
    if page > 1:
        row.append(("上一頁", f"{callback_prefix}:{page - 1}"))
    row.append((f"第 {page}/{total_pages} 頁", f"{callback_prefix}:{page}"))
    if page < total_pages:
        row.append(("下一頁", f"{callback_prefix}:{page + 1}"))
    return row


def queue_navigation_rows(queue_name: str, page: int, total_pages: int,
                          counts: dict[str, int]) -> list[list[ButtonSpec]]:
    rows: list[list[ButtonSpec]] = [
        [(f"未讀 {counts['inbox']}", "queue:inbox:1"), (f"全部 {counts['all']}", "queue:all:1")],
        [(ui.ALL_READ, f"read_all:{queue_name}:{page}", "success"),
         (ui.REFRESH, f"queue:{queue_name}:{page}")],
    ]
    if total_pages > 1:
        rows.append(pagination_navigation_row(f"queue:{queue_name}", page, total_pages))
    rows.append([(ui.HOME, "admin:dashboard")])
    return rows


def conversation_queue_keyboard(rows: list[Any], queue_name: str, *, viewer_admin_id: int | None,
                                page: int, total_pages: int, page_size: int,
                                counts: dict[str, int], can_cleanup: bool = False) -> dict[str, Any]:
    navigation = queue_navigation_rows(queue_name, page, total_pages, counts)
    buttons: list[list[ButtonSpec]] = navigation[:2]
    for index, row in enumerate(rows, start=(page - 1) * page_size + 1):
        name = " ".join(str(row[key] or "") for key in ("first_name", "last_name")).strip()
        name = name or (f"@{row['username']}" if row["username"] else "未命名聯絡人")
        name = " ".join(name.split())
        buttons.append([(f"{index}. 查看 {truncate_text(name, 22)}", f"user:{row['chat_id']}:{queue_name}:{page}")])
    buttons.extend(navigation[2:])
    if can_cleanup and queue_name == "all" and counts["all"]:
        buttons.insert(-1, [("刪除所有會話", "cleanup:all", "danger")])
    return inline_keyboard(buttons)


def contact_back_callback(queue_name: str, page: int) -> str:
    return f"settings:blocked:{page}" if queue_name == "blocked" else f"queue:{queue_name}:{page}"


def contact_keyboard(chat_id: int, *, queue_name: str, page: int, active_reply: bool,
                     blacklisted: bool, read_through_seq: int, read_done: bool) -> dict[str, Any]:
    suffix = f"{chat_id}:{queue_name}:{page}"
    rows: list[list[ButtonSpec]] = []
    if not blacklisted:
        rows.append([("退出回覆", f"reply_end:{suffix}", "danger")] if active_reply else
                    [("持續回覆", f"reply_start:{suffix}", REPLY_MODE_BUTTON_STYLE)])
    rows.extend([
        [("已讀" if read_done else "標為已讀", f"read:{chat_id}:{read_through_seq}:{queue_name}:{page}"),
         (ui.REFRESH, f"contact:{suffix}")],
        [("更多操作", f"more:{suffix}")],
        [("返回封鎖名單" if queue_name == "blocked" else "返回會話列表", contact_back_callback(queue_name, page)),
         (ui.HOME, "admin:dashboard")],
    ])
    return inline_keyboard(rows)


def exit_reply_keyboard(chat_id: int) -> dict[str, Any]:
    return inline_keyboard([
        [("退出回覆", f"cancel:{chat_id}", "danger"), ("使用者詳情", f"detail:{chat_id}")],
        [("更多操作", f"more:{chat_id}"), ("返回會話列表", "queue:all:1")],
    ])


def welcome_keyboard() -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("如何留言", "user_help", "primary"),
                ("支援格式", "user_guide"),
            ]
        ]
    )
