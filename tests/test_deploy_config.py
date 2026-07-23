import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


class DeploymentConfigTests(unittest.TestCase):
    def test_nginx_setup_supports_8443_without_certbot_rewriting_ports(self) -> None:
        script = (PROJECT_DIR / "scripts" / "configure-nginx.sh").read_text(
            encoding="utf-8"
        )
        template = (PROJECT_DIR / "deploy" / "nginx.conf.example").read_text(
            encoding="utf-8"
        )

        self.assertIn('HTTPS_PORT="${3:-${HTTPS_PORT:-443}}"', script)
        self.assertIn("certbot certonly --webroot", script)
        self.assertNotIn("certbot --nginx", script)
        self.assertIn("listen __HTTPS_PORT__ ssl;", template)
        self.assertIn("__HTTPS_REDIRECT_PORT__", template)
        self.assertIn("/.well-known/acme-challenge/", template)

        rendered = (
            template.replace("__DOMAIN__", "bot.example.com")
            .replace("__ACME_WEBROOT__", "/var/www/tg-bot-acme")
            .replace("__HTTPS_PORT__", "8443")
            .replace("__HTTPS_REDIRECT_PORT__", ":8443")
        )
        self.assertNotIn("__", rendered)
        self.assertIn("listen 8443 ssl;", rendered)
        self.assertIn(
            "return 301 https://$host:8443$request_uri;",
            rendered,
        )

    def test_updates_refresh_scoped_command_menus(self) -> None:
        script = (PROJECT_DIR / "scripts" / "update.sh").read_text(encoding="utf-8")
        self.assertIn("manage_webhook.py", script)
        self.assertIn("--commands-only", script)


if __name__ == "__main__":
    unittest.main()
