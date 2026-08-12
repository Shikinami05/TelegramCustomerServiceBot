import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
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


def turnstile_verify_hostname(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Turnstile verification URL") from exc
    hostname = parsed.hostname or ""
    hostname_labels = hostname.split(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or not hostname.isascii()
        or len(hostname) > 253
        or port == 0
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                not (character.isalnum() or character == "-")
                for character in label
            )
            for label in hostname_labels
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/verify"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Turnstile verification URL must be an HTTPS /verify URL "
            "without credentials, query, or fragment"
        )
    expected_netloc = hostname
    if port is not None:
        expected_netloc = f"{hostname}:{port}"
    if parsed.netloc.lower() != expected_netloc.lower():
        raise ValueError("invalid Turnstile verification URL host")
    return hostname.lower()


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
    bot_token: str
    webhook_secret: str
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
    turnstile_enabled: bool
    turnstile_site_key: str
    turnstile_secret_key: str
    turnstile_verify_url: str
    turnstile_verify_days: int
    turnstile_init_data_max_age_seconds: int
    display_timezone_name: str
    display_timezone: ZoneInfo
    log_level: str
    db_path: Path
    db_backup_dir: Path
    api_base: str
    app_version: str


def load_settings(base_dir: Path) -> Settings:
    load_dotenv(base_dir / ".env")

    bot_token = os.getenv("BOT_TOKEN") or ""
    webhook_secret = os.getenv("WEBHOOK_SECRET") or ""
    admin_ids = parse_id_set("ADMIN_IDS")
    owner_ids = parse_id_set("OWNER_IDS") or set(admin_ids)
    admin_ids |= owner_ids

    turnstile_enabled = env_bool("TURNSTILE_ENABLED", False)
    turnstile_site_key = os.getenv("TURNSTILE_SITE_KEY", "").strip()
    turnstile_secret_key = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    turnstile_verify_url = os.getenv("TURNSTILE_VERIFY_URL", "").strip()
    display_timezone_name = os.getenv(
        "DISPLAY_TIMEZONE", "Asia/Hong_Kong"
    ).strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")
    if not webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is missing")
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS is missing")
    if turnstile_enabled:
        if (
            not turnstile_site_key
            or not turnstile_secret_key
            or not turnstile_verify_url
        ):
            raise RuntimeError(
                "TURNSTILE_SITE_KEY, TURNSTILE_SECRET_KEY and "
                "TURNSTILE_VERIFY_URL are required when Turnstile is enabled"
            )
        try:
            turnstile_verify_hostname(turnstile_verify_url)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

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
        turnstile_enabled=turnstile_enabled,
        turnstile_site_key=turnstile_site_key,
        turnstile_secret_key=turnstile_secret_key,
        turnstile_verify_url=turnstile_verify_url,
        turnstile_verify_days=env_int("TURNSTILE_VERIFY_DAYS", 30, 1),
        turnstile_init_data_max_age_seconds=env_int(
            "TURNSTILE_INIT_DATA_MAX_AGE_SECONDS", 600, 60
        ),
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
