#!/usr/bin/env python3
import argparse
import getpass
import sys
from pathlib import Path

from dotenv import dotenv_values


DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
sys.path.insert(0, str(DEFAULT_ENV_PATH.parent))
from tg_bot.moderation_config import DEFAULT_MODEL, configure_moderation


def format_status(values: dict[str, str | None]) -> str:
    enabled = (values.get("AI_MODERATION_ENABLED") or "false").lower() in {"true", "1", "yes", "on"}
    return "\n".join((f"DeepSeek moderation: {'enabled' if enabled else 'disabled'}",
                       f"Model: {values.get('DEEPSEEK_MODEL') or DEFAULT_MODEL}",
                       f"API key: {'configured' if values.get('DEEPSEEK_API_KEY') else 'not configured'}",
                       f"Daily request limit (UTC): {values.get('MODERATION_DAILY_LIMIT') or '500'}",
                       "Suspicious messages and API failures require manual review: /moderation"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage optional DeepSeek advertisement filtering.")
    parser.add_argument("action", choices=("status", "enable", "disable"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = args.env_file.absolute()
    try:
        if not path.is_file():
            raise ValueError(".env file not found")
        values = dict(dotenv_values(path, interpolate=False))
        if args.action == "enable":
            print("Message text and hidden link targets will be sent to DeepSeek. API usage is billed by DeepSeek.")
            key = getpass.getpass("DeepSeek API key (Enter to keep existing): ").strip() or values.get("DEEPSEEK_API_KEY") or ""
            model = input(f"DeepSeek model [{values.get('DEEPSEEK_MODEL') or DEFAULT_MODEL}]: ").strip() or values.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
            limit = input(f"Daily request limit [{values.get('MODERATION_DAILY_LIMIT') or '500'}]: ").strip() or values.get("MODERATION_DAILY_LIMIT") or "500"
            configure_moderation(path, True, api_key=key, model=model, daily_limit=int(limit))
        elif args.action == "disable":
            configure_moderation(path, False)
        print(format_status(dict(dotenv_values(path, interpolate=False))))
    except (OSError, ValueError, RuntimeError):
        print("Configuration failed. Check the .env file, key format, model, and daily limit; no credentials are printed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

