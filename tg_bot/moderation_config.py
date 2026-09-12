import os
import re
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


DEFAULT_MODEL = "deepseek-flash"


def configure_moderation(env_path: Path, enabled: bool, *, api_key: str = "",
                         model: str = DEFAULT_MODEL, daily_limit: int = 500) -> None:
    if env_path.is_symlink() or not env_path.is_file():
        raise ValueError("A regular .env file is required")
    if enabled or api_key:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,256}", api_key):
            raise ValueError("DEEPSEEK_API_KEY is missing or contains invalid characters")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
            raise ValueError("DEEPSEEK_MODEL is invalid")
        if not 1 <= daily_limit <= 1000000:
            raise ValueError("Daily limit must be between 1 and 1000000")
    # Stage every change together, so failed configuration cannot leave half a key set.
    staging: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=env_path.parent,
                                         prefix=".moderation-", delete=False) as handle:
            staging = Path(handle.name)
            handle.write(env_path.read_text(encoding="utf-8"))
        staging.chmod(0o600)
        values = {"AI_MODERATION_ENABLED": "true" if enabled else "false"}
        if enabled or api_key:
            values.update(DEEPSEEK_API_KEY=api_key, DEEPSEEK_MODEL=model,
                          MODERATION_DAILY_LIMIT=str(daily_limit))
        for key, value in values.items():
            if not set_key(str(staging), key, value, quote_mode="always")[0]:
                raise RuntimeError("Unable to save moderation configuration")
        for key in dotenv_values(staging, interpolate=False):
            if key.startswith("TURNSTILE_"):
                unset_key(str(staging), key)
        staging.chmod(0o600)
        os.replace(staging, env_path)
    finally:
        if staging is not None and staging.exists():
            staging.unlink()

