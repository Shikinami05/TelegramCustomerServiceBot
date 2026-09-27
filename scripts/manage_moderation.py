#!/usr/bin/env python3
import argparse
import getpass
import sys
from pathlib import Path

from dotenv import dotenv_values


DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
sys.path.insert(0, str(DEFAULT_ENV_PATH.parent))
from tg_bot.moderation_config import DEFAULT_MODEL, configure_moderation
from tg_bot.ai_providers import PROVIDERS, normalize_model


def format_status(values: dict[str, str | None]) -> str:
    enabled = (values.get("AI_MODERATION_ENABLED") or "false").lower() in {"true", "1", "yes", "on"}
    provider = values.get("AI_PROVIDER") or "deepseek"
    model = values.get("AI_MODEL") or (values.get("DEEPSEEK_MODEL") if provider == "deepseek" else "") or ""
    key = values.get("AI_API_KEY") or (values.get("DEEPSEEK_API_KEY") if provider == "deepseek" else "")
    return "\n".join((f"AI moderation: {'enabled' if enabled else 'disabled'}",
                       f"Provider: {provider}",
                       f"Model: {normalize_model(provider, model)}",
                       f"API key: {'configured' if key else 'not configured'}",
                       f"Daily request limit (UTC): {values.get('MODERATION_DAILY_LIMIT') or '500'}",
                       f"Media without text: {values.get('MODERATION_MEDIA_POLICY') or 'hold'}",
                       "Held messages: /moderation; settings: /ai"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage optional AI advertisement filtering.")
    parser.add_argument("action", choices=("status", "enable", "disable"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = args.env_file.absolute()
    try:
        if not path.is_file():
            raise ValueError(".env file not found")
        values = dict(dotenv_values(path, interpolate=False))
        if args.action == "enable":
            old_provider = values.get("AI_PROVIDER") or "deepseek"
            print("Providers: deepseek, siliconflow (China), siliconflow-intl (international)")
            provider = input(f"AI provider [{old_provider}]: ").strip() or old_provider
            if provider not in PROVIDERS:
                raise ValueError("Invalid provider")
            print(f"Message text and hidden links will be sent to {PROVIDERS[provider].name}. API fees may apply.")
            existing_key = (values.get("AI_API_KEY") or (values.get("DEEPSEEK_API_KEY") if provider == "deepseek" else "")) if provider == old_provider else ""
            key = getpass.getpass("API key (Enter to keep existing for this provider): ").strip() or existing_key or ""
            old_model = (values.get("AI_MODEL") or values.get("DEEPSEEK_MODEL") or "") if provider == old_provider else ""
            default_model = normalize_model(provider, old_model)
            model = input(f"Model [{default_model}]: ").strip() or default_model
            limit = input(f"Daily request limit [{values.get('MODERATION_DAILY_LIMIT') or '500'}]: ").strip() or values.get("MODERATION_DAILY_LIMIT") or "500"
            media_policy = input(f"Media without text: hold or allow [{values.get('MODERATION_MEDIA_POLICY') or 'hold'}]: ").strip() or values.get("MODERATION_MEDIA_POLICY") or "hold"
            configure_moderation(path, True, api_key=key, model=model, daily_limit=int(limit), provider=provider, media_policy=media_policy)
        elif args.action == "disable":
            configure_moderation(path, False)
        print(format_status(dict(dotenv_values(path, interpolate=False))))
    except (OSError, ValueError, RuntimeError):
        print("Configuration failed. Check the .env file, key format, model, and daily limit; no credentials are printed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
