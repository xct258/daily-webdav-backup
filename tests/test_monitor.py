"""磁盘监控单元测试。"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import load_config  # noqa: E402
from monitor import is_low, should_notify  # noqa: E402

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
