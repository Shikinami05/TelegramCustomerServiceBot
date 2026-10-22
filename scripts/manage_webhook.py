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
    {"command": "start", "description": "打开聊天首页"},
    {"command": "cancel", "description": "退出持续回复或取消输入"},
    {"command": "myid", "description": "查看自己的 Telegram ID"},
    {"command": "settings", "description": "打开设置"},
]
OWNER_COMMANDS = list(ADMIN_COMMANDS)


def parse_api_response(method: str, response: httpx.Response) -> dict:
    try:
        result = response.json()
    except ValueError:
        result = {}
    if (
        response.is_error
        or not isinstance(result, dict)
        or not result.get("ok", False)
    ):
        description = (
            str(result.get("description", "unexpected response"))
            if isinstance(result, dict)
            else "unexpected response"
        )
        raise RuntimeError(
            f"Telegram API {method} failed with HTTP "
            f"{response.status_code}: {description[:300]}"
        ) from None
    return result


def api_post(api_base: str, method: str, payload: dict) -> dict:
    try:
        response = httpx.post(f"{api_base}/{method}", json=payload, timeout=20)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Telegram API {method} request failed ({type(exc).__name__})"
        ) from None
    return parse_api_response(method, response)


def api_get(api_base: str, method: str) -> dict:
    try:
        response = httpx.get(f"{api_base}/{method}", timeout=20)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Telegram API {method} request failed ({type(exc).__name__})"
        ) from None
    return parse_api_response(method, response)


def configure_command_menus(
    api_base: str,
    admin_ids: set[int],
    owner_ids: set[int],
) -> None:
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
                "commands": (
                    OWNER_COMMANDS if admin_id in owner_ids else ADMIN_COMMANDS
                ),
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
    owner_ids = {
        int(value.strip())
        for value in os.getenv("OWNER_IDS", "").split(",")
        if value.strip().lstrip("-").isdigit()
    }
    if not owner_ids:
        owner_ids = set(admin_ids)
    admin_ids |= owner_ids
    if not token:
        raise SystemExit("BOT_TOKEN is missing from .env")
    if not args.info and not admin_ids:
        raise SystemExit("ADMIN_IDS is missing from .env")

    api_base = f"https://api.telegram.org/bot{token}"
    if args.info:
        result = api_get(api_base, "getWebhookInfo")
    elif args.commands_only:
        configure_command_menus(api_base, admin_ids, owner_ids)
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
        configure_command_menus(api_base, admin_ids, owner_ids)
        result["command_menus_configured"] = True

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
