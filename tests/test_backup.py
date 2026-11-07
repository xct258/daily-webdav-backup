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
from archive import archive_name  # noqa: E402
from config import EncryptionConfig, Job, load_config, normalize_schedule  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class ScheduleTest(unittest.TestCase):
    def test_short_form(self) -> None:
        self.assertEqual(normalize_schedule("03:05"), "5 3 * * *")

    def test_cron_form(self) -> None:
        self.assertEqual(normalize_schedule("0 */6 * * *"), "0 */6 * * *")


class AutoConfigTest(unittest.TestCase):
    def test_auto_config_is_safe_placeholder(self) -> None:
        config = load_config(os.path.join(ROOT, "config", "config.auto.yml"))
        self.assertFalse(config.notify.enabled)
        self.assertTrue(config.jobs)
        self.assertTrue(all(not job.enabled for job in config.jobs))


class AutoDiscoverTest(unittest.TestCase):
    def test_discover_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "etc"))
            os.makedirs(os.path.join(root, "appdata"))
            with open(os.path.join(root, "ignore.txt"), "w") as handle:
                handle.write("x")

            config_path = os.path.join(root, "config.yml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"""
schedule: "03:00"
timezone: "UTC"
auto_discover: true
discover_root: "{root}"
webdav:
  url: "https://example.com/dav/"
  user: "u"
  password: "p"
  remote_path: "/r"
notify:
  enabled: false
defaults:
  compression: "none"
"""
                )

            config = load_config(config_path)
            self.assertEqual({job.name for job in config.jobs}, {"etc", "appdata"})
            self.assertTrue(all(job.enabled for job in config.jobs))
            self.assertTrue(all(job.archive_root == root for job in config.jobs))

    def test_manual_job_can_override_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "etc"))
            os.makedirs(os.path.join(root, "appdata"))
            config_path = os.path.join(root, "config.yml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"""
schedule: "03:00"
timezone: "UTC"
auto_discover: true
discover_root: "{root}"
webdav:
  url: "https://example.com/dav/"
  user: "u"
  password: "p"
  remote_path: "/r"
notify:
  enabled: false
defaults:
  compression: "none"
jobs:
  - name: "etc"
    enabled: false
    paths:
      - "/tmp"
"""
                )

            config = load_config(config_path)
            jobs = {job.name: job for job in config.jobs}
            self.assertIn("etc", jobs)
            self.assertFalse(jobs["etc"].enabled)
            self.assertTrue(jobs["appdata"].enabled)


class RemoteLayoutTest(unittest.TestCase):
    def test_date_layer(self) -> None:
        config = load_config(os.path.join(ROOT, "config", "config.example.yml"))
        job = config.jobs[0]
        target = config.webdav.targets[0]
        when = datetime(2026, 11, 7, 3, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(
            config.job_remote_path(job, target, when),
            "backups/nas/2026-11-07/etc",
        )
        self.assertEqual(
            config.job_cleanup_root(job, target),
            "backups/nas",
        )
        self.assertEqual(len(config.webdav.targets), 2)
        self.assertEqual(config.webdav.targets[0].interval, 86400)
        self.assertEqual(config.webdav.targets[1].interval, 604800)
        self.assertEqual(config.webdav.targets[1].path, "backups/nas2")


class EncryptionTest(unittest.TestCase):
    def test_age_archive_name(self) -> None:
        job = Job(name="etc", paths=["/tmp/etc"])
        when = datetime(2026, 11, 7, 3, 0, 0)
        encryption = EncryptionConfig(
            enabled=True, method="age", recipient="age1example"
        )
        self.assertEqual(
            archive_name(job, when, encryption),
            "etc-20261107-030000.tar.zst.age",
        )

    def test_encrypted_archive_retention(self) -> None:
        when = datetime(2026, 11, 7, 3, 0, 0, tzinfo=ZoneInfo("UTC"))
        parsed = retention.parse_archive_time(
            "etc-20261107-030000.tar.zst.age", "etc", when.tzinfo
        )
        self.assertEqual(parsed, when)


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

    def test_remote_keep_last_with_date_dirs(self) -> None:
        entries = [
            {"Path": "2026-11-01/etc-20261101-030000.tar.zst"},
            {"Path": "2026-11-02/etc-20261102-030000.tar.zst"},
            {"Path": "2026-11-03/etc-20261103-030000.tar.zst"},
        ]
        plan = retention.remote_cleanup_plan(entries, self.job, self.now)
        self.assertEqual(plan, ["2026-11-01/etc-20261101-030000.tar.zst"])

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
