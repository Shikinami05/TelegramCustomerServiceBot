import argparse
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


USER_COMMANDS = [
    {"command": "start", "description": "打开留言入口"},
]

ADMIN_COMMANDS = [
    {"command": "start", "description": "打开管理入口"},
    {"command": "myid", "description": "查看自己的 Telegram ID"},
    {"command": "users", "description": "查看最近联系用户"},
    {"command": "reply", "description": "回复指定用户：用户ID 内容"},
    {"command": "send", "description": "主动发送消息：用户ID 内容"},
    {"command": "broadcast", "description": "创建群发任务：内容"},
    {"command": "broadcast_status", "description": "查看最近群发进度"},
    {"command": "cancel", "description": "退出持续回复模式"},
    {"command": "takeover", "description": "接管会话：用户ID"},
    {"command": "close", "description": "关闭会话：用户ID"},
    {"command": "blacklist", "description": "加入黑名单：用户ID 原因"},
    {"command": "unblacklist", "description": "解除黑名单：用户ID"},
    {"command": "blacklist_list", "description": "查看最近黑名单"},
]


def api_post(api_base: str, method: str, payload: dict) -> dict:
    response = httpx.post(f"{api_base}/{method}", json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok", False):
        raise RuntimeError(f"Telegram API {method} failed: {result}")
    return result


def configure_command_menus(api_base: str, admin_ids: set[int]) -> None:
    api_post(
        api_base,
        "setMyCommands",
        {
            "commands": USER_COMMANDS,
            "scope": {"type": "all_private_chats"},
        },
    )
    for admin_id in sorted(admin_ids):
        api_post(
            api_base,
            "setMyCommands",
            {
                "commands": ADMIN_COMMANDS,
                "scope": {"type": "chat", "chat_id": admin_id},
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the Telegram webhook")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--info", action="store_true", help="show current webhook info")
    mode.add_argument(
        "--commands-only",
        action="store_true",
        help="configure Telegram command menus without changing the webhook",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    load_dotenv(project_dir / ".env")

    token = os.getenv("BOT_TOKEN")
    secret = os.getenv("WEBHOOK_SECRET")
    webhook_url = os.getenv("WEBHOOK_URL")
    admin_ids = {
        int(value.strip())
        for value in os.getenv("ADMIN_IDS", "").split(",")
        if value.strip().lstrip("-").isdigit()
    }
    if not token:
        raise SystemExit("BOT_TOKEN is missing from .env")
    if not args.info and not admin_ids:
        raise SystemExit("ADMIN_IDS is missing from .env")

    api_base = f"https://api.telegram.org/bot{token}"
    if args.info:
        response = httpx.get(f"{api_base}/getWebhookInfo", timeout=20)
        response.raise_for_status()
        result = response.json()
    elif args.commands_only:
        configure_command_menus(api_base, admin_ids)
        result = {"ok": True, "command_menus_configured": True}
    else:
        if not secret:
            raise SystemExit("WEBHOOK_SECRET is missing from .env")
        if not webhook_url:
            raise SystemExit("WEBHOOK_URL is missing from .env")
        result = api_post(
            api_base,
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": secret,
                "drop_pending_updates": False,
                "allowed_updates": ["message", "edited_message", "callback_query"],
                "max_connections": 20,
            },
        )
        configure_command_menus(api_base, admin_ids)
        result["command_menus_configured"] = True

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
