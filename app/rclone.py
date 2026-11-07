"""rclone 封装：自动生成配置、上传、校验、列目录、删除。"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from config import Config
from process import CommandInterrupted, CommandTimeout, run_command

LOG = logging.getLogger("backup.rclone")


class RcloneError(Exception):
    """rclone 操作失败。"""


def _join_remote(base: str, *parts: str) -> str:
    items = [base.strip("/")] if base.strip("/") else []
    items.extend(part.strip("/") for part in parts if part and part.strip("/"))
    return "/".join(items)


class Rclone:
    def __init__(self, config: Config, stop_event: threading.Event | None = None):
        self.config = config
        self.webdav = config.webdav
        self.stop_event = stop_event
        self.base = [
            "rclone",
            "--config",
            self.webdav.rclone_config,
            "--log-level",
            "NOTICE",
            "--stats",
            "30s",
            "--stats-one-line",
        ]

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def ensure_config(self) -> None:
        """把 config.yml 里的 WebDAV 连接信息写成 rclone.conf（密码自动 obscure）。"""
        if not os.path.exists(self.webdav.rclone_config):
            os.makedirs(os.path.dirname(self.webdav.rclone_config) or ".", exist_ok=True)

        try:
            obscured = subprocess.run(
                ["rclone", "obscure", "-"],
                input=self.webdav.password,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RcloneError("找不到 rclone 命令，请检查镜像构建") from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneError("rclone obscure 超时") from exc

        if obscured.returncode != 0 or not obscured.stdout.strip():
            raise RcloneError(
                "rclone obscure 失败："
                + (obscured.stderr.strip() or obscured.stdout.strip() or "未知错误")
            )

        url = self.webdav.url.rstrip("/") + "/"
        content = (
            f"[{self.webdav.remote_name}]\n"
            f"type = webdav\n"
            f"url = {url}\n"
            f"vendor = {self.webdav.vendor}\n"
            f"user = {self.webdav.user}\n"
            f"pass = {obscured.stdout.strip()}\n"
        )

        directory = os.path.dirname(self.webdav.rclone_config) or "."
        fd, tmp = tempfile.mkstemp(prefix=".rclone-", suffix=".conf", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.webdav.rclone_config)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        LOG.info("已生成 rclone 配置：%s", self.webdav.rclone_config)

    # ------------------------------------------------------------------
    # 基础命令
    # ------------------------------------------------------------------
    def _run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        check: bool = True,
        log_output: bool = True,
    ):
        cmd = self.base + args
        try:
            result = run_command(
                cmd,
                stop_event=self.stop_event,
                timeout=timeout,
                log_output=log_output,
                log_level=logging.INFO,
            )
        except (CommandInterrupted, CommandTimeout):
            raise
        except FileNotFoundError as exc:
            raise RcloneError("找不到 rclone 命令") from exc

        if check and result.returncode != 0:
            raise RcloneError(
                f"rclone 执行失败（退出码 {result.returncode}）："
                f"{result.output[-2000:] if result.output else '无输出'}"
            )
        return result

    def target(self, remote_dir: str, name: str = "") -> str:
        path = _join_remote(remote_dir, name) if name else remote_dir.strip("/")
        return f"{self.webdav.remote_name}:{path}"

    def mkdir(self, remote_dir: str) -> None:
        self._run(["mkdir", self.target(remote_dir)], timeout=120)

    def copyto(self, local: str, remote: str) -> None:
        args = [
            "copyto",
            local,
            remote,
            "--size-only",
            "--retries",
            "3",
            "--low-level-retries",
            "10",
            "--timeout",
            "5m",
            "--contimeout",
            "30s",
        ]
        if self.config.bwlimit:
            args.extend(["--bwlimit", self.config.bwlimit])
        self._run(args, timeout=None)

    def moveto(self, src: str, dst: str) -> None:
        self._run(["moveto", src, dst, "--retries", "3"], timeout=3600)

    def stat(self, remote: str, *, quiet: bool = False) -> dict | None:
        result = self._run(
            [
                "lsjson",
                "--stat",
                "--no-mimetype",
                "--log-level",
                "ERROR",
                "--stats",
                "0",
                remote,
            ],
            timeout=120,
            check=False,
            log_output=not quiet,
        )
        if result.returncode != 0:
            return None
        text = result.output.strip()
        # rclone 偶尔会把提示行混进来，从每个 { 开始尝试解析
        for start, char in enumerate(text):
            if char != "{":
                continue
            try:
                data = json.loads(text[start:])
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def list_files(self, remote_dir: str) -> list[dict]:
        result = self._run(
            [
                "lsjson",
                "--files-only",
                "--no-mimetype",
                "--log-level",
                "ERROR",
                "--stats",
                "0",
                self.target(remote_dir),
            ],
            timeout=300,
            check=False,
            log_output=False,
        )
        if result.returncode != 0:
            LOG.warning(
                "列出远端目录失败：%s（%s）",
                remote_dir,
                result.output[-500:] if result.output else "无输出",
            )
            return []
        text = result.output.strip()
        data = None
        for start, char in enumerate(text):
            if char != "[":
                continue
            try:
                candidate = json.loads(text[start:])
            except ValueError:
                continue
            if isinstance(candidate, list):
                data = candidate
                break
        if data is None:
            LOG.warning("解析远端目录失败：%s", remote_dir)
            return []
        return [item for item in data if isinstance(item, dict)]

    def delete_file(self, remote: str) -> bool:
        result = self._run(
            ["deletefile", remote, "--retries", "1"],
            timeout=120,
            check=False,
            log_output=False,
        )
        if result.returncode != 0:
            LOG.warning("删除远端文件失败：%s（%s）", remote, result.output[-500:])
            return False
        return True

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    def upload(
        self,
        local_archive: str,
        sha_path: str,
        remote_dir: str,
        filename: str,
        size: int,
    ) -> str:
        """上传归档和 .sha256；先传 .part，校验大小后再 moveto 正式文件名。"""
        self.mkdir(remote_dir)
        self.cleanup_parts(remote_dir)

        final_remote = self.target(remote_dir, filename)
        temp_remote = self.target(remote_dir, "." + filename + ".part")

        LOG.info("上传 %s -> %s（%.1f MB）", filename, remote_dir, size / 1024 / 1024)
        self.copyto(local_archive, temp_remote)

        remote_stat = self.stat(temp_remote, quiet=True)
        if remote_stat is None:
            raise RcloneError(f"上传后无法获取远端文件信息：{temp_remote}")
        remote_size = remote_stat.get("Size")
        if remote_size != size:
            raise RcloneError(
                f"上传大小校验失败：本地 {size} 字节，远端 {remote_size} 字节"
            )

        self.moveto(temp_remote, final_remote)

        sha_name = filename + ".sha256"
        temp_sha = self.target(remote_dir, "." + sha_name + ".part")
        final_sha = self.target(remote_dir, sha_name)
        try:
            self.copyto(sha_path, temp_sha)
            self.moveto(temp_sha, final_sha)
        except (RcloneError, CommandInterrupted, CommandTimeout) as exc:
            # 归档本身已经上传成功，校验文件失败只告警，不把整个任务判失败
            LOG.warning("上传校验文件失败：%s", exc)

        LOG.info("上传完成：%s", final_remote)
        return final_remote

    def cleanup_parts(self, remote_dir: str, max_age_hours: int = 24) -> None:
        """清理超过 24 小时的 .part 临时文件，避免中断后长期堆积。"""
        entries = self.list_files(remote_dir)
        if not entries:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        for item in entries:
            name = str(item.get("Name") or "")
            if not (name.startswith(".") and name.endswith(".part")):
                continue
            modtime = str(item.get("ModTime") or "")
            if not modtime:
                continue
            try:
                dt = datetime.fromisoformat(modtime.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < cutoff:
                LOG.info("清理过期临时文件：%s", name)
                self.delete_file(self.target(remote_dir, name))

    # ------------------------------------------------------------------
    # 连接测试
    # ------------------------------------------------------------------
    def test_connection(self) -> tuple[bool, str]:
        result = self._run(
            ["lsd", f"{self.webdav.remote_name}:"],
            timeout=20,
            check=False,
            log_output=False,
        )
        if result.returncode == 0:
            return True, "WebDAV 连接正常"
        return False, result.output[-1000:] if result.output else "WebDAV 连接失败"
