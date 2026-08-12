import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


class DeploymentConfigTests(unittest.TestCase):
    def test_service_and_proxy_keep_webhook_private(self) -> None:
        service = (PROJECT_DIR / "deploy" / "tg-bot.service.example").read_text(
            encoding="utf-8"
        )
        nginx = (PROJECT_DIR / "deploy" / "nginx.conf.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("--host 127.0.0.1", service)
        self.assertNotIn("--host 0.0.0.0", service)
        self.assertIn("UMask=0077", service)
        self.assertIn(
            "proxy_set_header X-Telegram-Bot-Api-Secret-Token "
            "$http_x_telegram_bot_api_secret_token;",
            nginx,
        )

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
        self.assertIn("location = /verify", template)
        self.assertIn("location = /verify/complete", template)
        self.assertIn("client_max_body_size 1m", template)
        self.assertIn("client_max_body_size 16k", template)
        self.assertNotIn("location = /healthz", template)

    def test_updates_refresh_scoped_command_menus(self) -> None:
        script = (PROJECT_DIR / "scripts" / "update.sh").read_text(encoding="utf-8")
        self.assertIn("manage_webhook.py", script)
        self.assertIn("--commands-only", script)
        self.assertIn("deploy/tg-bot.service.example", script)
        self.assertIn("systemctl daemon-reload", script)

    def test_installer_seeds_owner_and_queue_defaults(self) -> None:
        script = (PROJECT_DIR / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("OWNER_IDS=%s", script)
        self.assertIn("PENDING_REMINDER_MINUTES=30", script)
        self.assertIn("ADMIN_REPLY_STATE_TTL_SECONDS=1800", script)
        self.assertIn("BROADCAST_RATE_LIMIT_RETRIES=3", script)
        self.assertIn("DISPLAY_TIMEZONE=Asia/Hong_Kong", script)
        self.assertIn("Enable Cloudflare Turnstile", script)
        self.assertIn("TURNSTILE_ENABLED=%s", script)
        self.assertIn("TURNSTILE_SECRET_KEY=%s", script)
        self.assertIn("TURNSTILE_VERIFY_URL=%s", script)

        nginx_script = (
            PROJECT_DIR / "scripts" / "configure-nginx.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'TURNSTILE_VERIFY_URL_VALUE="https://${DOMAIN_NAME}/verify"',
            nginx_script,
        )
        self.assertIn(
            'TURNSTILE_VERIFY_URL_VALUE="https://${DOMAIN_NAME}:${HTTPS_PORT}/verify"',
            nginx_script,
        )
        self.assertIn('systemctl restart "$SERVICE_NAME"', nginx_script)

    def test_one_line_installer_and_management_command_are_wired(self) -> None:
        bootstrap = (PROJECT_DIR / "scripts" / "bootstrap.sh").read_text(
            encoding="utf-8"
        )
        command = (PROJECT_DIR / "scripts" / "tg-bot.sh").read_text(
            encoding="utf-8"
        )
        install_script = (PROJECT_DIR / "scripts" / "install.sh").read_text(
            encoding="utf-8"
        )
        update_script = (PROJECT_DIR / "scripts" / "update.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "https://github.com/Shikinami05/TelegramCustomerServiceBot.git",
            bootstrap,
        )
        self.assertIn("--version latest", bootstrap)
        self.assertIn("</dev/tty", bootstrap)
        self.assertIn("INSTALL_WEBHOOK_URL", install_script)
        self.assertIn("/usr/local/bin/tg-bot", install_script)
        self.assertIn("/usr/local/bin/tg-bot", update_script)
        for subcommand in (
            "update)",
            "backup)",
            "status)",
            "restart)",
            "logs)",
            "version)",
            "webhook)",
            "turnstile)",
            "configure)",
        ):
            self.assertIn(subcommand, command)
        self.assertIn("--kind manual", command)
        self.assertIn("manage_turnstile.py", command)
        self.assertIn("restoring the previous .env", command)

    def test_release_install_update_and_rollback_controls_exist(self) -> None:
        install_script = (PROJECT_DIR / "scripts" / "install.sh").read_text(
            encoding="utf-8"
        )
        update_script = (PROJECT_DIR / "scripts" / "update.sh").read_text(
            encoding="utf-8"
        )
        common_script = (PROJECT_DIR / "scripts" / "common.sh").read_text(
            encoding="utf-8"
        )
        version_script = (PROJECT_DIR / "scripts" / "version.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("--version latest|v1.2.3", install_script)
        self.assertIn("resolve_release_tag", install_script)
        self.assertIn("--version latest|v1.2.3", update_script)
        self.assertIn("ROLLBACK_ARMED", update_script)
        self.assertIn("rollback_update", update_script)
        self.assertIn("manage_backup.py", update_script)
        self.assertIn("reset --hard", update_script)
        self.assertIn("validate_release_tag", common_script)
        self.assertIn("Version:", version_script)

    def test_release_workflow_matches_the_declared_version(self) -> None:
        version = (PROJECT_DIR / "VERSION").read_text(encoding="ascii").strip()
        workflow = (
            PROJECT_DIR / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        release_script = (
            PROJECT_DIR / "scripts" / "create-release.sh"
        ).read_text(encoding="utf-8")

        self.assertRegex(version, r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
        self.assertIn("contents: write", workflow)
        self.assertIn("branches:", workflow)
        self.assertIn("- main", workflow)
        self.assertIn("- VERSION", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("Prepare annotated release tag", workflow)
        self.assertIn('git tag -a "${release_tag}"', workflow)
        self.assertIn("should_release=false", workflow)
        self.assertIn(
            '"refs/tags/${RELEASE_TAG}:refs/tags/${RELEASE_TAG}"',
            workflow,
        )
        self.assertIn('gh release create "${RELEASE_TAG}"', workflow)
        self.assertIn("does not match VERSION", release_script)
        self.assertIn("scripts/manage_turnstile.py", release_script)


if __name__ == "__main__":
    unittest.main()
