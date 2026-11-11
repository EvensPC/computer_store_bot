"""Проверка выбора режима работы (polling / webhook) для Render и VPS.

Настройки читаются при импорте `app.config`, поэтому каждый сценарий
проверяется в отдельном процессе с собственным окружением.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = """
import json
from app import config
print(json.dumps({
    "use_webhook": config.USE_WEBHOOK,
    "webhook_url": config.WEBHOOK_URL,
    "webhook_path": config.WEBHOOK_PATH,
    "webhook_secret": config.WEBHOOK_SECRET,
    "port": config.PORT,
}))
"""


def probe(env_extra: dict[str, str]) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WEBHOOK_", "RENDER_", "PORT"))}
    env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class DeployModeTest(unittest.TestCase):
    def test_default_is_long_polling_for_vps(self):
        data = probe({})
        self.assertFalse(data["use_webhook"])
        self.assertEqual(data["port"], 8080)
        self.assertEqual(data["webhook_path"], "/telegram/webhook")

    def test_render_external_url_enables_webhook(self):
        data = probe({"RENDER_EXTERNAL_URL": "https://bot.onrender.com/"})
        self.assertTrue(data["use_webhook"])
        self.assertEqual(data["webhook_url"], "https://bot.onrender.com")

    def test_explicit_webhook_url_wins_and_port_is_read(self):
        data = probe({"WEBHOOK_URL": "https://example.com", "PORT": "10000"})
        self.assertTrue(data["use_webhook"])
        self.assertEqual(data["port"], 10000)

    def test_webhook_path_is_normalized(self):
        data = probe({"WEBHOOK_URL": "https://example.com", "WEBHOOK_PATH": "hook"})
        self.assertEqual(data["webhook_path"], "/hook")

    def test_webhook_secret_is_auto_generated_and_telegram_safe(self):
        data = probe({"WEBHOOK_URL": "https://example.com"})
        secret = data["webhook_secret"]
        self.assertTrue(secret, "секрет должен генерироваться автоматически")
        self.assertLessEqual(len(secret), 256)
        self.assertRegex(secret, re.compile(r"^[A-Za-z0-9_-]+$"))

    def test_explicit_webhook_secret_is_preserved(self):
        data = probe({"WEBHOOK_URL": "https://example.com", "WEBHOOK_SECRET": "my_secret-123"})
        self.assertEqual(data["webhook_secret"], "my_secret-123")


if __name__ == "__main__":
    unittest.main()
