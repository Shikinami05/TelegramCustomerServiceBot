#!/usr/bin/env python3
import argparse
import getpass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values, set_key


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"


def validate_https_endpoint(value: str, expected_path: str, label: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    hostname = parsed.hostname or ""
    hostname_labels = hostname.split(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or not hostname.isascii()
        or len(hostname) > 253
        or port == 0
        or any(
            not hostname_label
            or len(hostname_label) > 63
            or hostname_label.startswith("-")
            or hostname_label.endswith("-")
            or any(
                not (character.isalnum() or character == "-")
                for character in hostname_label
            )
            for hostname_label in hostname_labels
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{label} must be an HTTPS {expected_path} URL without "
            "credentials, query, or fragment"
    )
    expected_netloc = hostname
    if port is not None:
        expected_netloc = f"{hostname}:{port}"
    if parsed.netloc.lower() != expected_netloc.lower():
        raise ValueError(f"{label} has an invalid host")
    return value


def validate_verify_url(value: str) -> str:
    return validate_https_endpoint(
        value,
        "/verify",
        "TURNSTILE_VERIFY_URL",
    )


def derive_verify_url(webhook_url: str) -> str:
    webhook_url = validate_https_endpoint(
        webhook_url,
        "/tg/webhook",
        "WEBHOOK_URL",
    )
    parsed = urlsplit(webhook_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/verify", "", ""))


def validate_key(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is required")
    if any(character.isspace() for character in value):
        raise ValueError(f"{label} must not contain whitespace")
    return value


def write_setting(env_path: Path, key: str, value: str) -> None:
    result = set_key(
        str(env_path),
        key,
        value,
        quote_mode="always",
    )
    if not result[0]:
        raise RuntimeError(f"Unable to update {key}")


def configure_turnstile(
    env_path: Path,
    enabled: bool,
    *,
    site_key: str = "",
    secret_key: str = "",
    verify_url: str = "",
) -> None:
    if enabled:
        site_key = validate_key(site_key, "TURNSTILE_SITE_KEY")
        secret_key = validate_key(secret_key, "TURNSTILE_SECRET_KEY")
        verify_url = validate_verify_url(verify_url)
        write_setting(env_path, "TURNSTILE_SITE_KEY", site_key)
        write_setting(env_path, "TURNSTILE_SECRET_KEY", secret_key)
        write_setting(env_path, "TURNSTILE_VERIFY_URL", verify_url)
    write_setting(env_path, "TURNSTILE_ENABLED", "true" if enabled else "false")


def parse_enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("TURNSTILE_ENABLED must be true or false")


def format_status(values: dict[str, str | None]) -> str:
    enabled = parse_enabled(values.get("TURNSTILE_ENABLED") or "")
    verify_url = (values.get("TURNSTILE_VERIFY_URL") or "").strip()
    site_key = (values.get("TURNSTILE_SITE_KEY") or "").strip()
    secret_key = (values.get("TURNSTILE_SECRET_KEY") or "").strip()
    return "\n".join(
        (
            f"Turnstile: {'enabled' if enabled else 'disabled'}",
            f"Verification URL: {verify_url or 'not configured'}",
            f"Site key: {'configured' if site_key else 'not configured'}",
            f"Secret key: {'configured' if secret_key else 'not configured'}",
        )
    )


def load_values(env_path: Path) -> dict[str, str | None]:
    return dict(dotenv_values(env_path))


def enable(env_path: Path) -> None:
    values = load_values(env_path)
    current_site_key = (values.get("TURNSTILE_SITE_KEY") or "").strip()
    current_secret_key = (values.get("TURNSTILE_SECRET_KEY") or "").strip()
    current_verify_url = (values.get("TURNSTILE_VERIFY_URL") or "").strip()

    site_prompt = "Cloudflare Turnstile Site Key"
    if current_site_key:
        site_prompt += " (press Enter to keep the configured value)"
    site_key = input(f"{site_prompt}: ").strip() or current_site_key

    secret_prompt = "Cloudflare Turnstile Secret Key"
    if current_secret_key:
        secret_prompt += " (press Enter to keep the configured value)"
    secret_key = getpass.getpass(f"{secret_prompt}: ").strip() or current_secret_key

    verify_url = current_verify_url
    if not verify_url:
        verify_url = derive_verify_url((values.get("WEBHOOK_URL") or "").strip())

    configure_turnstile(
        env_path,
        True,
        site_key=site_key,
        secret_key=secret_key,
        verify_url=verify_url,
    )
    print("Cloudflare Turnstile is enabled.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Cloudflare Turnstile for Telegram Customer Service Bot."
    )
    parser.add_argument(
        "action",
        choices=("status", "enable", "disable"),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_path = args.env_file.resolve()
    if not env_path.is_file():
        raise SystemExit(f"Environment file not found: {env_path}")

    try:
        if args.action == "status":
            print(format_status(load_values(env_path)))
        elif args.action == "enable":
            enable(env_path)
        else:
            configure_turnstile(env_path, False)
            print("Cloudflare Turnstile is disabled.")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
