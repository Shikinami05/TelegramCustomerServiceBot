import argparse
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the Telegram webhook")
    parser.add_argument("--info", action="store_true", help="show current webhook info")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    load_dotenv(project_dir / ".env")

    token = os.getenv("BOT_TOKEN")
    secret = os.getenv("WEBHOOK_SECRET")
    webhook_url = os.getenv("WEBHOOK_URL")
    if not token:
        raise SystemExit("BOT_TOKEN is missing from .env")

    api_base = f"https://api.telegram.org/bot{token}"
    if args.info:
        response = httpx.get(f"{api_base}/getWebhookInfo", timeout=20)
    else:
        if not secret:
            raise SystemExit("WEBHOOK_SECRET is missing from .env")
        if not webhook_url:
            raise SystemExit("WEBHOOK_URL is missing from .env")
        response = httpx.post(
            f"{api_base}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret,
                "drop_pending_updates": False,
                "allowed_updates": ["message", "edited_message", "callback_query"],
                "max_connections": 20,
            },
            timeout=20,
        )

    response.raise_for_status()
    result = response.json()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
