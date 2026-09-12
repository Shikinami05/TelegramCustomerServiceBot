"""Keep pre-1.7 updaters' syntax checks working after Turnstile removal."""


if __name__ == "__main__":
    raise SystemExit(
        "Cloudflare Turnstile has been removed. "
        "Use 'sudo tg-bot moderation status' or Telegram /ai for DeepSeek settings."
    )
