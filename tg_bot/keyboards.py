from typing import Any


ButtonSpec = tuple[str, str] | tuple[str, str, str]
INLINE_BUTTON_STYLES = {"primary", "success", "danger"}


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
    chat_id: int,
    *,
    blacklisted: bool,
    closed: bool,
    owner_admin_id: int | None,
    viewer_admin_id: int | None,
) -> dict[str, Any]:
    if blacklisted:
        return inline_keyboard(
            [
                [
                    ("用户详情", f"detail:{chat_id}"),
                    ("解除黑名单", f"unblacklist:{chat_id}", "success"),
                ],
                [("返回工作台", "admin:dashboard")],
            ]
        )

    if closed:
        return inline_keyboard(
            [
                [
                    ("重新打开", f"reopen:{chat_id}", "primary"),
                    ("用户详情", f"detail:{chat_id}"),
                ],
                [("加入黑名单", f"blacklist:{chat_id}")],
                [("返回工作台", "admin:dashboard")],
            ]
        )

    if owner_admin_id and viewer_admin_id and owner_admin_id != viewer_admin_id:
        reply_button: ButtonSpec = ("接管", f"takeover:{chat_id}", "primary")
    else:
        reply_button = ("回复", f"reply:{chat_id}", "primary")
    return inline_keyboard(
        [
            [
                reply_button,
                ("标记已处理", f"resolve:{chat_id}", "success"),
            ],
            [
                ("用户详情", f"detail:{chat_id}"),
                ("加入黑名单", f"blacklist:{chat_id}"),
            ],
            [("返回工作台", "admin:dashboard")],
        ]
    )


def admin_dashboard_keyboard(counts: dict[str, int]) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                (f"待处理 {counts['inbox']}", "queue:inbox:1", "primary"),
                (f"超时 {counts['pending']}", "queue:pending:1"),
            ],
            [
                (f"已处理 {counts['closed']}", "queue:closed:1", "success"),
                ("最近用户", "admin:users:1"),
            ],
            [("刷新", "admin:dashboard")],
        ]
    )


def pagination_navigation_row(
    callback_prefix: str,
    page: int,
    total_pages: int,
) -> list[ButtonSpec]:
    row: list[ButtonSpec] = []
    if page > 1:
        row.append(("上一页", f"{callback_prefix}:{page - 1}"))
    row.append((f"第 {page}/{total_pages} 页", f"{callback_prefix}:{page}"))
    if page < total_pages:
        row.append(("下一页", f"{callback_prefix}:{page + 1}"))
    return row


def queue_navigation_rows(
    queue_name: str,
    page: int,
    total_pages: int,
    counts: dict[str, int],
) -> list[list[ButtonSpec]]:
    rows: list[list[ButtonSpec]] = [
        [
            (f"待处理 {counts['inbox']}", "queue:inbox:1", "primary"),
            (f"超时 {counts['pending']}", "queue:pending:1"),
            (f"已处理 {counts['closed']}", "queue:closed:1", "success"),
        ],
    ]
    if total_pages > 1:
        rows.append(
            pagination_navigation_row(f"queue:{queue_name}", page, total_pages)
        )
    rows.append([("返回工作台", "admin:dashboard")])
    return rows


def recent_users_keyboard(
    rows: list[Any],
    *,
    page: int,
    total_pages: int,
    page_size: int,
) -> dict[str, Any]:
    keyboard_rows: list[list[ButtonSpec]] = []
    current_row: list[ButtonSpec] = []
    start_index = (page - 1) * page_size
    for index, row in enumerate(rows, start=start_index + 1):
        current_row.append((f"{index} 详情", f"detail:{row['chat_id']}"))
        if len(current_row) == 2:
            keyboard_rows.append(current_row)
            current_row = []
    if current_row:
        keyboard_rows.append(current_row)
    if total_pages > 1:
        keyboard_rows.append(
            pagination_navigation_row("admin:users", page, total_pages)
        )
    keyboard_rows.append([("返回工作台", "admin:dashboard")])
    return inline_keyboard(keyboard_rows)


def conversation_queue_keyboard(
    rows: list[Any],
    queue_name: str,
    *,
    viewer_admin_id: int | None,
    page: int,
    total_pages: int,
    page_size: int,
    counts: dict[str, int],
) -> dict[str, Any]:
    keyboard_rows: list[list[ButtonSpec]] = []
    start_index = (page - 1) * page_size
    for index, row in enumerate(rows, start=start_index + 1):
        chat_id = int(row["chat_id"])
        if queue_name == "closed":
            keyboard_rows.append(
                [
                    (f"{index} 重开", f"reopen:{chat_id}", "primary"),
                    (f"{index} 详情", f"detail:{chat_id}"),
                ]
            )
            continue

        owner_admin_id = row["owner_admin_id"]
        if (
            owner_admin_id is not None
            and viewer_admin_id is not None
            and int(owner_admin_id) != viewer_admin_id
        ):
            primary_button: ButtonSpec = (
                f"{index} 接管",
                f"takeover:{chat_id}",
                "primary",
            )
        else:
            primary_button = (
                f"{index} 回复",
                f"reply:{chat_id}",
                "primary",
            )
        keyboard_rows.append(
            [
                primary_button,
                (f"{index} 详情", f"detail:{chat_id}"),
                (f"{index} 处理", f"resolve:{chat_id}", "success"),
            ]
        )
    keyboard_rows.extend(
        queue_navigation_rows(queue_name, page, total_pages, counts)
    )
    return inline_keyboard(keyboard_rows)


def exit_reply_keyboard(chat_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("退出回复", f"cancel:{chat_id}", "danger"),
                ("标记已处理", f"resolve:{chat_id}", "success"),
            ],
            [("用户详情", f"detail:{chat_id}")],
        ]
    )


def welcome_keyboard() -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                ("如何留言", "user_help", "primary"),
                ("支持格式", "user_guide"),
            ]
        ]
    )


def verification_keyboard(verify_url: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "完成人机验证",
                    "web_app": {"url": verify_url},
                    "style": "primary",
                }
            ]
        ]
    }
