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
import rclone  # noqa: E402
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
            "backups/nas/my-server/2026-11-07/etc",
        )
        self.assertEqual(
            config.job_cleanup_root(job, target),
            "backups/nas/my-server",
        )
        self.assertEqual(len(config.webdav.targets), 2)
        self.assertEqual(config.webdav.targets[0].interval, 86400)
        self.assertEqual(config.webdav.targets[1].interval, 604800)
        self.assertEqual(config.webdav.targets[1].path, "backups/nas2/my-server")


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

    def test_remote_keep_last_with_split_parts(self) -> None:
        entries = [
            {"Path": "2026-11-01/etc-20261101-030000.tar.zst.gpg.part001"},
            {"Path": "2026-11-01/etc-20261101-030000.tar.zst.gpg.part002"},
            {"Path": "2026-11-01/etc-20261101-030000.tar.zst.gpg.sha256"},
            {"Path": "2026-11-02/etc-20261102-030000.tar.zst"},
            {"Path": "2026-11-03/etc-20261103-030000.tar.zst"},
        ]
        plan = retention.remote_cleanup_plan(entries, self.job, self.now)
        self.assertEqual(
            plan,
            [
                "2026-11-01/etc-20261101-030000.tar.zst.gpg.part001",
                "2026-11-01/etc-20261101-030000.tar.zst.gpg.part002",
                "2026-11-01/etc-20261101-030000.tar.zst.gpg.sha256",
            ],
        )

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


class SplitFallbackTest(unittest.TestCase):
    def _rclone(self):
        config = load_config(os.path.join(ROOT, "config", "config.example.yml"))
        config.webdav.split_size_mb = 1
        return rclone.Rclone(config)

    def test_413_falls_back_to_parts(self) -> None:
        runner = self._rclone()
        remote_files: dict[str, int] = {}

        def fake_copyto(local: str, remote: str) -> None:
            if remote.endswith(".gpg.part"):
                raise rclone.RcloneError(
                    "<title>413 Payload Too Large</title>: 413 Request Entity Too Large"
                )
            remote_files[remote] = os.path.getsize(local)

        def fake_stat(remote: str, quiet: bool = False):
            size = remote_files.get(remote)
            return {"Size": size} if size is not None else None

        def fake_moveto(src: str, dst: str) -> None:
            remote_files[dst] = remote_files.pop(src)

        runner.copyto = fake_copyto  # type: ignore[assignment]
        runner.stat = fake_stat  # type: ignore[assignment]
        runner.moveto = fake_moveto  # type: ignore[assignment]
        runner.list_files = lambda *a, **k: []  # type: ignore[assignment]
        runner.mkdir = lambda *a, **k: None  # type: ignore[assignment]
        runner.cleanup_parts = lambda *a, **k: None  # type: ignore[assignment]
        runner.delete_file = lambda *a, **k: True  # type: ignore[assignment]
        runner._upload_sidecar = lambda *a, **k: None  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(
                directory, "docker-20261113-000000.tar.zst.gpg"
            )
            with open(path, "wb") as handle:
                handle.write(b"x" * int(2.5 * 1024 * 1024))
            sha_path = path + ".sha256"
            with open(sha_path, "w", encoding="utf-8") as handle:
                handle.write("hash  name\n")

            runner.upload(
                path,
                sha_path,
                "2026-11-13/docker",
                os.path.basename(path),
                os.path.getsize(path),
            )

        parts = sorted(name for name in remote_files if ".part" in name)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0].rsplit("/", 1)[-1].rsplit(".", 1)[-1], "part001")
        self.assertEqual(sum(remote_files.values()), int(2.5 * 1024 * 1024))

    def test_never_keeps_single_upload(self) -> None:
        runner = self._rclone()
        runner.webdav.split_upload = "never"

        def fake_copyto(local: str, remote: str) -> None:
            raise rclone.RcloneError("413 Request Entity Too Large")

        runner.copyto = fake_copyto  # type: ignore[assignment]
        runner.mkdir = lambda *a, **k: None  # type: ignore[assignment]
        runner.cleanup_parts = lambda *a, **k: None  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "docker-20261113-000000.tar.zst")
            with open(path, "wb") as handle:
                handle.write(b"x" * (2 * 1024 * 1024))
            sha_path = path + ".sha256"
            open(sha_path, "w").close()
            with self.assertRaises(rclone.RcloneError):
                runner.upload(
                    path,
                    sha_path,
                    "2026-11-13/docker",
                    os.path.basename(path),
                    os.path.getsize(path),
                )


class Http2RetryTest(unittest.TestCase):
    def test_retries_with_http1_on_stream_error(self) -> None:
        config = load_config(os.path.join(ROOT, "config", "config.example.yml"))
        runner = rclone.Rclone(config)
        calls: list[list[str]] = []

        class Result:
            returncode = 0
            output = ""

        def fake_run(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise rclone.RcloneError(
                    "stream error: stream ID 3; PROTOCOL_ERROR; received from peer"
                )
            return Result()

        runner._run = fake_run  # type: ignore[assignment]
        runner.copyto("/tmp/a", "webdav:x")
        self.assertEqual(len(calls), 2)
        self.assertIn("--disable-http2", calls[1])
        self.assertNotIn("--disable-http2", calls[0])


class SplitUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        base = load_config(os.path.join(ROOT, "config", "config.example.yml"))
        self.webdav = base.webdav

    def test_defaults(self) -> None:
        self.assertEqual(self.webdav.split_upload, "auto")
        self.assertEqual(self.webdav.split_size_mb, 50)

    def test_size_rejection_detection(self) -> None:
        self.assertTrue(
            rclone.looks_like_size_rejection(
                "<title>413 Payload Too Large</title>"
            )
        )
        self.assertTrue(
            rclone.looks_like_size_rejection("413 Request Entity Too Large")
        )
        self.assertFalse(
            rclone.looks_like_size_rejection("connection reset by peer")
        )

    def test_part_names_sort_in_order(self) -> None:
        names = [rclone.part_name("a.tar", i, 12) for i in range(1, 13)]
        self.assertEqual(names[0], "a.tar.part001")
        self.assertEqual(names[-1], "a.tar.part012")
        self.assertEqual(sorted(names, reverse=True)[-1], names[0])

    def test_invalid_split_config(self) -> None:
        import yaml

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.yml")
            with open(os.path.join(ROOT, "config", "config.example.yml"), encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            data["webdav"]["split_upload"] = "sometimes"
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, allow_unicode=True)
            with self.assertRaises(Exception):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
