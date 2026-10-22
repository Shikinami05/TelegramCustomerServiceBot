"""Traditional Chinese copy and pure layouts for the chat interface."""

from tg_bot.text import escape_html_limited


ALL_READ = "全部標為已讀"
REFRESH = "重新整理"
HOME = "返回首頁"
QUEUE_LABELS = {"inbox": "未讀留言", "all": "所有會話"}

WELCOME_TEXT = (
    "<b>統一留言聊天入口</b>\n\n"
    "你好，歡迎留言。想聊的事、想分享的內容，都可以直接傳到這裡。\n\n"
    "我看到後會透過這個 Bot 回覆你，不必另行私聊。文字、圖片與其他媒體都可以傳送；"
    "還沒收到回覆時，也可以繼續補充。"
)
USER_HELP = (
    "<b>如何留言</b>\n\n"
    "直接在這個對話傳送訊息即可，不需要提供其他聯絡方式。\n\n"
    "回覆會出現在同一個 Bot 對話裡。想補充內容時可以繼續傳送，不必重新開始。"
)
USER_GUIDE = (
    "<b>支援格式</b>\n\n"
    "文字、圖片、影片、文件、音訊、語音、動畫、貼圖、位置與聯絡人都可以傳送，"
    "也可以附上文字說明。\n\n"
    "請只分享你願意提供的內容，不要傳送密碼、驗證碼或其他敏感資料。"
)


def dashboard(counts: dict[str, int], target_label: str | None = None) -> str:
    state = (f"正在回覆：{target_label}\n持續回覆已開啟，直接傳送的訊息會交給這位聯絡人。"
             if target_label else "尚未選取回覆對象\n目前直接輸入的訊息不會轉送給任何聯絡人。")
    return (
        "<b>留言中心</b>\n統一接收留言，繼續你的私人聊天。\n\n"
        f"<b>留言概況</b>\n未讀會話：{counts['inbox']} 個\n"
        f"未讀訊息：{counts['unread_messages']} 則\n所有會話：{counts['all']} 個\n\n"
        f"<b>目前回覆狀態</b>\n{state}\n\n"
        "<b>閱讀與整理</b>\n全部標為已讀只清除未讀計數，不會刪除聊天記錄或封鎖聯絡人。"
    )


def queue_heading(queue_name: str, count: int, page: int, pages: int,
                  counts: dict[str, int], timezone: str) -> str:
    return (
        f"<b>聊天 · {QUEUE_LABELS[queue_name]}</b>\n\n"
        f"未讀會話：{counts['inbox']} 個 · 未讀訊息：{counts['unread_messages']} 則\n"
        f"本列表共 {count} 個會話 · 第 {page}/{pages} 頁\n"
        f"顯示時區：{timezone}"
    )


def contact(*, name: str, chat_id: int, username: str, status: str,
            last_message_at: str, unread: int, active_reply: bool, history: str) -> str:
    state = ("持續回覆中：直接傳送的訊息會交給這位聯絡人。" if active_reply else
             "僅查看會話：尚未開啟對這位聯絡人的持續回覆。")
    return (
        f"<b>會話 · {name}</b>\n\n"
        f"<b>使用者詳情</b>\nID：<code>{chat_id}</code>\n使用者名稱：{username}\n"
        f"狀態：{status}\n最近留言：{last_message_at}\n未讀訊息：{unread} 則\n\n"
        f"<b>回覆狀態</b>\n{state}\n\n"
        f"<b>聊天記錄</b>\n{history}"
    )


def all_read_notice(count: int) -> str:
    return f"已將 {count} 個會話標為已讀，聊天記錄保留。" if count else "目前沒有未讀留言，聊天記錄未變更。"


def notice(text: str) -> str:
    return f"<b>操作結果</b>\n{escape_html_limited(text, 300)}\n\n" if text else ""
