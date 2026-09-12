import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def load_display_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(f"DISPLAY_TIMEZONE is invalid: {name}") from exc


def parse_id_set(name: str) -> set[int]:
    values = [value.strip() for value in os.getenv(name, "").split(",")]
    values = [value for value in values if value]
    if any(not value.lstrip("-").isdigit() for value in values):
        raise RuntimeError(f"{name} must contain comma-separated Telegram IDs")
    return {int(value) for value in values}


@dataclass(frozen=True, slots=True)
class Settings:
    base_dir: Path
    bot_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    admin_ids: frozenset[int]
    owner_ids: frozenset[int]
    db_backup_enabled: bool
    db_backup_interval_seconds: int
    db_backup_keep: int
    user_rate_limit_count: int
    user_rate_limit_window_seconds: int
    user_rate_limit_cooldown_seconds: int
    message_retention_days: int
    broadcast_send_delay_seconds: float
    update_processing_timeout_seconds: int
    pending_reminder_minutes: int
    admin_reply_state_ttl_seconds: int
    telegram_inline_retry_max_seconds: int
    broadcast_rate_limit_retries: int
    ai_moderation_enabled: bool
    deepseek_api_key: str = field(repr=False)
    deepseek_model: str
    moderation_timeout_seconds: int
    moderation_daily_limit: int
    display_timezone_name: str
    display_timezone: ZoneInfo
    log_level: str
    db_path: Path
    db_backup_dir: Path
    api_base: str = field(repr=False)
    app_version: str


def load_settings(base_dir: Path) -> Settings:
    load_dotenv(base_dir / ".env")

    bot_token = os.getenv("BOT_TOKEN") or ""
    webhook_secret = os.getenv("WEBHOOK_SECRET") or ""
    admin_ids = parse_id_set("ADMIN_IDS")
    owner_ids = parse_id_set("OWNER_IDS") or set(admin_ids)
    admin_ids |= owner_ids

    ai_moderation_enabled = env_bool("AI_MODERATION_ENABLED", False)
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
    display_timezone_name = os.getenv(
        "DISPLAY_TIMEZONE", "Asia/Hong_Kong"
    ).strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")
    if not webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is missing")
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS is missing")
    if ai_moderation_enabled and not deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required when AI moderation is enabled")
    if any(character.isspace() for character in deepseek_api_key):
        raise RuntimeError("DEEPSEEK_API_KEY must not contain whitespace")
    if not deepseek_model or len(deepseek_model) > 100 or any(
        not (character.isascii() and (character.isalnum() or character in "-_."))
        for character in deepseek_model
    ):
        raise RuntimeError("DEEPSEEK_MODEL is invalid")

    return Settings(
        base_dir=base_dir,
        bot_token=bot_token,
        webhook_secret=webhook_secret,
        admin_ids=frozenset(admin_ids),
        owner_ids=frozenset(owner_ids),
        db_backup_enabled=env_bool("DB_BACKUP_ENABLED", True),
        db_backup_interval_seconds=env_int(
            "DB_BACKUP_INTERVAL_SECONDS", 86400, 60
        ),
        db_backup_keep=env_int("DB_BACKUP_KEEP", 14, 1),
        user_rate_limit_count=env_int("USER_RATE_LIMIT_COUNT", 8, 1),
        user_rate_limit_window_seconds=env_int(
            "USER_RATE_LIMIT_WINDOW_SECONDS", 60, 1
        ),
        user_rate_limit_cooldown_seconds=env_int(
            "USER_RATE_LIMIT_COOLDOWN_SECONDS", 300, 1
        ),
        message_retention_days=env_int("MESSAGE_RETENTION_DAYS", 0, 0),
        broadcast_send_delay_seconds=env_float(
            "BROADCAST_SEND_DELAY_SECONDS", 0.05, 0.0
        ),
        update_processing_timeout_seconds=env_int(
            "UPDATE_PROCESSING_TIMEOUT_SECONDS", 300, 30
        ),
        pending_reminder_minutes=env_int("PENDING_REMINDER_MINUTES", 30, 1),
        admin_reply_state_ttl_seconds=env_int(
            "ADMIN_REPLY_STATE_TTL_SECONDS", 1800, 60
        ),
        telegram_inline_retry_max_seconds=env_int(
            "TELEGRAM_INLINE_RETRY_MAX_SECONDS", 5, 0
        ),
        broadcast_rate_limit_retries=env_int(
            "BROADCAST_RATE_LIMIT_RETRIES", 3, 0
        ),
        ai_moderation_enabled=ai_moderation_enabled,
        deepseek_api_key=deepseek_api_key,
        deepseek_model=deepseek_model,
        moderation_timeout_seconds=env_int("MODERATION_TIMEOUT_SECONDS", 15, 1),
        moderation_daily_limit=env_int("MODERATION_DAILY_LIMIT", 500, 1),
        display_timezone_name=display_timezone_name,
        display_timezone=load_display_timezone(display_timezone_name),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        db_path=base_dir / "bot.db",
        db_backup_dir=Path(
            os.getenv("DB_BACKUP_DIR", str(base_dir / "backups"))
        ),
        api_base=f"https://api.telegram.org/bot{bot_token}",
        app_version=(base_dir / "VERSION").read_text(encoding="ascii").strip(),
    )

