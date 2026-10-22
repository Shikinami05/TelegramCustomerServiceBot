from typing import Any


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
    read_through_seq: int = 0,
) -> dict[str, Any]:
    if blacklisted:
        rows: list[list[ButtonSpec]] = [
            [("用户详情", f"detail:{chat_id}"), ("解除拉黑", f"unblacklist:{chat_id}", "success")],
            [("更多", f"more:{chat_id}")],
        ]
    else:
        reply: ButtonSpec = (
            ("退出回复", f"cancel:{chat_id}", "danger") if active_reply
            else ("持续回复", f"reply:{chat_id}", REPLY_MODE_BUTTON_STYLE)
        )
        rows = [
            [reply, ("标记已读", f"read:{chat_id}:{read_through_seq}")],
            [("用户详情", f"detail:{chat_id}"), ("更多", f"more:{chat_id}")],
        ]
    rows.append([("返回聊天", "queue:all:1")])
    return inline_keyboard(rows)


def admin_dashboard_keyboard(counts: dict[str, int], *, show_ai_settings: bool = False) -> dict[str, Any]:
    rows: list[list[ButtonSpec]] = [
        [(f"打开聊天 · 未读 {counts['inbox']}", "queue:all:1", "primary")],
        [(f"垃圾箱 {counts.get('moderation', 0)}", "moderation:1")],
        [("设置", "settings:panel")],
    ]
    return inline_keyboard(rows)


def pagination_navigation_row(callback_prefix: str, page: int, total_pages: int) -> list[ButtonSpec]:
    row: list[ButtonSpec] = []
    if page > 1:
        row.append(("上一页", f"{callback_prefix}:{page - 1}"))
    row.append((f"第 {page}/{total_pages} 页", f"{callback_prefix}:{page}"))
    if page < total_pages:
        row.append(("下一页", f"{callback_prefix}:{page + 1}"))
    return row


def queue_navigation_rows(queue_name: str, page: int, total_pages: int,
                          counts: dict[str, int]) -> list[list[ButtonSpec]]:
    rows: list[list[ButtonSpec]] = [
        [(f"未读 {counts['inbox']}", "queue:inbox:1"), ("全部", "queue:all:1")],
    ]
    if total_pages > 1:
        rows.append(pagination_navigation_row(f"queue:{queue_name}", page, total_pages))
    if counts["inbox"] > 0:
        rows.append([("全部已读", f"read_all:{queue_name}:{page}")])
    rows.append([("返回首页", "admin:dashboard")])
    return rows


def conversation_queue_keyboard(rows: list[Any], queue_name: str, *, viewer_admin_id: int | None,
                                page: int, total_pages: int, page_size: int,
                                counts: dict[str, int]) -> dict[str, Any]:
    buttons: list[list[ButtonSpec]] = []
    for index, row in enumerate(rows, start=(page - 1) * page_size + 1):
        buttons.append([(f"{index} 打开聊天", f"user:{row['chat_id']}:{queue_name}:{page}", "primary")])
    buttons.extend(queue_navigation_rows(queue_name, page, total_pages, counts))
    return inline_keyboard(buttons)


def exit_reply_keyboard(chat_id: int) -> dict[str, Any]:
    return inline_keyboard([
        [("退出回复", f"cancel:{chat_id}", "danger"), ("用户详情", f"detail:{chat_id}")],
        [("更多", f"more:{chat_id}"), ("返回聊天", "queue:all:1")],
    ])


def welcome_keyboard() -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("如何留言", "user_help", "primary"),
                ("支持格式", "user_guide"),
            ]
        ]
    )
