"""磁盘监控单元测试。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import load_config  # noqa: E402
from monitor import Monitor, is_low, should_notify  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class MonitorConfigTest(unittest.TestCase):
    def test_default_only_root(self) -> None:
        config = load_config(os.path.join(ROOT, "config", "config.example.yml"))
        self.assertTrue(config.monitor.enabled)
        self.assertEqual(
            [item.path for item in config.monitor.paths], ["/"]
        )


class MonitorCooldownTest(unittest.TestCase):
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    def test_first_low_alert(self) -> None:
        self.assertTrue(should_notify(True, None, None, self.now, 360))

    def test_low_within_six_hours(self) -> None:
        self.assertFalse(
            should_notify(
                True,
                "low",
                self.now - timedelta(hours=1),
                self.now,
                360,
            )
        )

    def test_low_after_six_hours(self) -> None:
        self.assertTrue(
            should_notify(
                True,
                "low",
                self.now - timedelta(hours=6),
                self.now,
                360,
            )
        )

    def test_recovery(self) -> None:
        self.assertTrue(should_notify(False, "low", None, self.now, 360))
        self.assertFalse(should_notify(False, "ok", None, self.now, 360))


class MonitorMessageTest(unittest.TestCase):
    def test_title_contains_free_space(self) -> None:
        config_text = """
schedule: "03:00"
timezone: "Asia/Shanghai"
webdav:
  url: "https://example.invalid/dav/"
  user: "backup"
  password: "change-me"
  remote_path: "/backups/nas"
notify:
  enabled: true
encryption:
  enabled: false
monitor:
  enabled: true
  interval: "5m"
  min_free_gb: 99999
  min_free_percent: 99
  paths:
    - name: "root"
      path: "/"
defaults:
  compression: "none"
jobs:
  - name: "placeholder"
    enabled: false
    paths: ["/sources"]
"""
        calls = []

        class FakeNotifier:
            def __init__(self, _config):
                pass

            def send(self, status, title, message, push_to_mqtt=None):
                calls.append((status, title, message, push_to_mqtt))
                return True

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(config_text)
            monitor = Monitor(
                config_path,
                os.path.join(directory, "monitor.json"),
            )
            with patch("monitor.Notifier", FakeNotifier):
                monitor.check_once()

        self.assertTrue(calls)
        status, title, message, _ = calls[0]
        self.assertEqual(status, "warn")
        self.assertIn("剩余", title)
        self.assertIn("剩余", message)


class DiskThresholdTest(unittest.TestCase):
    def test_low_by_gb(self) -> None:
        self.assertTrue(
            is_low(
                free=5 * 1024**3,
                total=100 * 1024**3,
                min_free_gb=10,
                min_free_percent=1,
            )
        )

    def test_low_by_percent(self) -> None:
        self.assertTrue(
            is_low(
                free=9 * 1024**3,
                total=100 * 1024**3,
                min_free_gb=1,
                min_free_percent=10,
            )
        )

    def test_ok(self) -> None:
        self.assertFalse(
            is_low(
                free=50 * 1024**3,
                total=100 * 1024**3,
                min_free_gb=10,
                min_free_percent=10,
            )
        )

    def test_zero_thresholds_disable(self) -> None:
        self.assertFalse(
            is_low(
                free=1,
                total=100 * 1024**3,
                min_free_gb=0,
                min_free_percent=0,
            )
        )


if __name__ == "__main__":
    unittest.main()
