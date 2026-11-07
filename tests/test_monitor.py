"""磁盘监控单元测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import load_config  # noqa: E402
from monitor import is_low  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class MonitorConfigTest(unittest.TestCase):
    def test_default_only_root(self) -> None:
        config = load_config(os.path.join(ROOT, "config", "config.example.yml"))
        self.assertTrue(config.monitor.enabled)
        self.assertEqual(
            [item.path for item in config.monitor.paths], ["/"]
        )


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
