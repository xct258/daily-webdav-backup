"""基础单元测试：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import retention  # noqa: E402
from config import Job, normalize_schedule  # noqa: E402


class ScheduleTest(unittest.TestCase):
    def test_short_form(self) -> None:
        self.assertEqual(normalize_schedule("03:05"), "5 3 * * *")

    def test_cron_form(self) -> None:
        self.assertEqual(normalize_schedule("0 */6 * * *"), "0 */6 * * *")


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=self.tz)
        self.job = Job(
            name="etc",
            paths=["/tmp/etc"],
            keep_last=2,
            keep_days=0,
        )

    def test_remote_keep_last(self) -> None:
        entries = [
            {"Name": "etc-20261101-030000.tar.zst"},
            {"Name": "etc-20261102-030000.tar.zst"},
            {"Name": "etc-20261103-030000.tar.zst"},
            {"Name": "other-20261103-030000.tar.zst"},
        ]
        plan = retention.remote_cleanup_plan(entries, self.job, self.now)
        self.assertEqual(plan, ["etc-20261101-030000.tar.zst"])

    def test_local_protects_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name in (
                "etc-20261101-030000.tar.zst",
                "etc-20261102-030000.tar.zst",
                "etc-20261103-030000.tar.zst",
            ):
                open(os.path.join(directory, name), "w").close()
            plan = retention.local_cleanup_plan(
                directory,
                self.job,
                keep=0,
                pending_files={"etc-20261101-030000.tar.zst"},
                now=self.now,
            )
            self.assertEqual(
                sorted(plan),
                [
                    "etc-20261102-030000.tar.zst",
                    "etc-20261103-030000.tar.zst",
                ],
            )


if __name__ == "__main__":
    unittest.main()
