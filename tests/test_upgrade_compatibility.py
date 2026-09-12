import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


class UpgradeCompatibilityTests(unittest.TestCase):
    def test_pre_ai_updater_can_compile_the_new_release(self) -> None:
        # This is the validation command still running after v1.6.1 checks out a tag.
        legacy_files = (
            "app.py",
            "scripts/manage_webhook.py",
            "scripts/manage_backup.py",
            "scripts/manage_turnstile.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in legacy_files:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PROJECT_DIR / name, target)
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", *legacy_files],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_removed_turnstile_command_cannot_modify_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            original = "BOT_TOKEN=test-token\nAI_MODERATION_ENABLED=false\n"
            env_file.write_text(original, encoding="utf-8")
            for command in ("enable", "disable", "status"):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, str(PROJECT_DIR / "scripts/manage_turnstile.py"), command],
                        cwd=root, capture_output=True, text=True, timeout=30,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("has been removed", result.stderr)
                    self.assertEqual(env_file.read_text(encoding="utf-8"), original)

    def test_installer_ai_opt_in_invokes_one_complete_command(self) -> None:
        bash = shutil.which("bash")
        if not bash and os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
        if not bash:
            self.skipTest("Bash is required for the installer command test")
        script = (PROJECT_DIR / "scripts/install.sh").read_text(encoding="utf-8")
        opt_in = script.split('case "${AI_CHOICE,,}" in', 1)[1].split("y|yes)", 1)[1].split(";;", 1)[0]
        probe = (
            "set -eu\n"
            "APP_USER=installer-test\n"
            "PROJECT_DIR=/nonexistent-tg-bot-test\n"
            "SCRIPT_DIR=$PROJECT_DIR/scripts\n"
            "runuser() { printf '%s\\0' \"$@\"; }\n"
            + opt_in
        )
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", probe],
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.split(b"\0")[:-1], [
            b"-u", b"installer-test", b"--",
            b"/nonexistent-tg-bot-test/venv/bin/python",
            b"/nonexistent-tg-bot-test/scripts/manage_moderation.py", b"enable",
        ])
