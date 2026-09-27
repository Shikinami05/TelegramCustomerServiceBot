import os
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key
from tg_bot.ai_providers import KEY_PATTERN, MODEL_PATTERN, PROVIDERS, normalize_model


DEFAULT_MODEL = PROVIDERS["deepseek"].default_model


def configure_moderation(env_path: Path, enabled: bool, *, api_key: str = "",
                         model: str = DEFAULT_MODEL, daily_limit: int = 500,
                         provider: str | None = None, media_policy: str | None = None) -> None:
    if env_path.is_symlink() or not env_path.is_file():
        raise ValueError("A regular .env file is required")
    selected = provider or (dotenv_values(env_path, interpolate=False).get("AI_PROVIDER") or "deepseek")
    if selected not in PROVIDERS or media_policy not in {None, "hold", "allow"}:
        raise ValueError("Invalid provider or media policy")
    model = normalize_model(selected, model)
    if enabled or api_key or provider is not None:
        if (enabled or api_key) and not KEY_PATTERN.fullmatch(api_key):
            raise ValueError("AI_API_KEY is missing or contains invalid characters")
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("AI_MODEL is invalid")
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
        if enabled or api_key or provider is not None:
            values.update(AI_PROVIDER=selected, AI_API_KEY=api_key, AI_MODEL=model,
                          DEEPSEEK_API_KEY=api_key if selected == "deepseek" else "",
                          DEEPSEEK_MODEL=model if selected == "deepseek" else DEFAULT_MODEL,
                          MODERATION_DAILY_LIMIT=str(daily_limit))
        if media_policy is not None:
            values["MODERATION_MEDIA_POLICY"] = media_policy
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
