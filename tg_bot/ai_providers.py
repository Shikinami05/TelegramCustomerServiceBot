"""Fixed provider endpoints; credentials must never follow redirects or custom URLs."""

import re
from dataclasses import dataclass


KEY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{8,256}")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}")


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    default_model: str
    models: tuple[str, ...]


PROVIDERS = {
    "deepseek": Provider("DeepSeek", "https://api.deepseek.com", "deepseek-flash",
                         ("deepseek-flash", "deepseek-v4-pro")),
    "siliconflow": Provider("硅基流动（中国站）", "https://api.siliconflow.cn/v1", "Qwen/Qwen3-32B",
                            ("Qwen/Qwen3-32B", "Qwen/Qwen2.5-7B-Instruct")),
    "siliconflow-intl": Provider("SiliconFlow（国际站）", "https://api.siliconflow.com/v1", "Qwen/Qwen3-32B",
                                 ("Qwen/Qwen3-32B", "Qwen/Qwen2.5-7B-Instruct")),
}


def normalize_model(provider: str, model: str) -> str:
    return model or PROVIDERS[provider].default_model


def model_available(provider: str, model: str, available: set[str]) -> bool:
    if provider == "deepseek" and model in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        return model in available or "deepseek-flash" in available
    return model in available


def request_options(provider: str, model: str) -> dict:
    if provider == "deepseek":
        return {"thinking": {"type": "disabled"}}
    if provider.startswith("siliconflow") and model in {"Qwen/Qwen3-8B", "Qwen/Qwen3-14B", "Qwen/Qwen3-32B"}:
        return {"enable_thinking": False}
    return {}
