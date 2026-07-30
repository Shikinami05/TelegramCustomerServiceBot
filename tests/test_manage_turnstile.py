import tempfile
import unittest
from pathlib import Path

from dotenv import dotenv_values

from scripts import manage_turnstile


class ManageTurnstileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.env_path.write_text(
            "WEBHOOK_URL=https://bot.example.com:8443/tg/webhook\n"
            "TURNSTILE_ENABLED=false\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_derives_and_validates_verification_url(self) -> None:
        self.assertEqual(
            manage_turnstile.derive_verify_url(
                "https://bot.example.com:8443/tg/webhook"
            ),
            "https://bot.example.com:8443/verify",
        )
        for invalid_url in (
            "http://bot.example.com/tg/webhook",
            "https://user@bot.example.com/tg/webhook",
            "https://bot.example.com/wrong",
            "https://bot.example.com/tg/webhook?source=test",
            "https://bot.example.com:invalid/tg/webhook",
            "https://bot.example.com:0/tg/webhook",
            "https://bot example.com/tg/webhook",
            "https://-bot.example.com/tg/webhook",
        ):
            with self.subTest(invalid_url=invalid_url):
                with self.assertRaises(ValueError):
                    manage_turnstile.derive_verify_url(invalid_url)

    def test_enable_and_disable_preserve_keys(self) -> None:
        manage_turnstile.configure_turnstile(
            self.env_path,
            True,
            site_key="site-key",
            secret_key="secret-key",
            verify_url="https://bot.example.com:8443/verify",
        )
        enabled = dotenv_values(self.env_path)
        self.assertEqual(enabled["TURNSTILE_ENABLED"], "true")
        self.assertEqual(enabled["TURNSTILE_SITE_KEY"], "site-key")
        self.assertEqual(enabled["TURNSTILE_SECRET_KEY"], "secret-key")
        self.assertEqual(
            enabled["TURNSTILE_VERIFY_URL"],
            "https://bot.example.com:8443/verify",
        )

        manage_turnstile.configure_turnstile(self.env_path, False)
        disabled = dotenv_values(self.env_path)
        self.assertEqual(disabled["TURNSTILE_ENABLED"], "false")
        self.assertEqual(disabled["TURNSTILE_SITE_KEY"], "site-key")
        self.assertEqual(disabled["TURNSTILE_SECRET_KEY"], "secret-key")

    def test_status_never_displays_secret_values(self) -> None:
        status = manage_turnstile.format_status(
            {
                "TURNSTILE_ENABLED": "true",
                "TURNSTILE_VERIFY_URL": "https://bot.example.com/verify",
                "TURNSTILE_SITE_KEY": "visible-site-value",
                "TURNSTILE_SECRET_KEY": "private-secret-value",
            }
        )
        self.assertIn("Turnstile: enabled", status)
        self.assertIn("Site key: configured", status)
        self.assertIn("Secret key: configured", status)
        self.assertNotIn("visible-site-value", status)
        self.assertNotIn("private-secret-value", status)

    def test_rejects_whitespace_in_keys(self) -> None:
        with self.assertRaises(ValueError):
            manage_turnstile.configure_turnstile(
                self.env_path,
                True,
                site_key="site key",
                secret_key="secret-key",
                verify_url="https://bot.example.com/verify",
            )


if __name__ == "__main__":
    unittest.main()
