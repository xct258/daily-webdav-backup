"""rclone 封装：自动生成配置、上传、校验、列目录、删除。"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from config import Config
from process import CommandInterrupted, CommandTimeout, run_command

LOG = logging.getLogger("backup.rclone")


class RcloneError(Exception):
    """rclone 操作失败。"""


# 服务端/代理拒绝超大请求体的典型错误
_SIZE_REJECT_RE = re.compile(
    r"\b413\b|payload too large|request entity too large|entity too large",
    re.IGNORECASE,
)
# 分段文件名：<归档名>.part001
_PART_SUFFIX_RE = re.compile(r"\.part\d+$")


def looks_like_size_rejection(message: str) -> bool:
    return bool(_SIZE_REJECT_RE.search(message or ""))


def part_name(filename: str, index: int, total: int) -> str:
    digits = max(3, len(str(total)))
    return f"{filename}.part{index:0{digits}d}"


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

    def list_files(self, remote_dir: str, recursive: bool = False) -> list[dict]:
        args = [
            "lsjson",
            "--files-only",
            "--no-mimetype",
            "--log-level",
            "ERROR",
            "--stats",
            "0",
        ]
        if recursive:
            args.append("--recursive")
        args.append(self.target(remote_dir))
        result = self._run(
            args,
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
        """上传归档和 .sha256；先传 .part，校验大小后再 moveto 正式文件名。

        当服务端或前面的代理拒绝大请求体（例如 Cloudflare 的 413）时，
        自动改用分段上传，避免整个备份任务失败。
        """
        self.mkdir(remote_dir)
        self.cleanup_parts(remote_dir)

        part_bytes = max(1, self.webdav.split_size_mb) * 1024 * 1024
        final_remote = self.target(remote_dir, filename)
        temp_remote = self.target(remote_dir, "." + filename + ".part")

        if self.webdav.split_upload == "always" and size > part_bytes:
            LOG.info("按配置使用分段上传：%s", filename)
            return self._upload_split(
                local_archive, sha_path, remote_dir, filename, size, part_bytes
            )

        LOG.info("上传 %s -> %s（%.1f MB）", filename, remote_dir, size / 1024 / 1024)
        try:
            self.copyto(local_archive, temp_remote)
        except RcloneError as exc:
            if (
                self.webdav.split_upload == "auto"
                and size > part_bytes
                and looks_like_size_rejection(str(exc))
            ):
                LOG.warning(
                    "整包上传被拒绝（超过服务端/代理限制），改用分段上传：%s", exc
                )
                self.delete_file(temp_remote)
                return self._upload_split(
                    local_archive, sha_path, remote_dir, filename, size, part_bytes
                )
            raise

        remote_stat = self.stat(temp_remote, quiet=True)
        if remote_stat is None:
            raise RcloneError(f"上传后无法获取远端文件信息：{temp_remote}")
        remote_size = remote_stat.get("Size")
        if remote_size != size:
            raise RcloneError(
                f"上传大小校验失败：本地 {size} 字节，远端 {remote_size} 字节"
            )

        self.moveto(temp_remote, final_remote)
        self._upload_sidecar(sha_path, remote_dir, filename)

        LOG.info("上传完成：%s", final_remote)
        return final_remote

    def _upload_sidecar(self, sha_path: str, remote_dir: str, filename: str) -> None:
        sha_name = filename + ".sha256"
        temp_sha = self.target(remote_dir, "." + sha_name + ".part")
        final_sha = self.target(remote_dir, sha_name)
        try:
            self.copyto(sha_path, temp_sha)
            self.moveto(temp_sha, final_sha)
        except (RcloneError, CommandInterrupted, CommandTimeout) as exc:
            # 归档本身已经上传成功，校验文件失败只告警，不把整个任务判失败
            LOG.warning("上传校验文件失败：%s", exc)

    def _upload_split(
        self,
        local_archive: str,
        sha_path: str,
        remote_dir: str,
        filename: str,
        size: int,
        part_bytes: int,
    ) -> str:
        """把归档切成若干段依次上传，每段都小于服务端限制。

        段名固定为 <归档名>.part001、.part002……，按名称排序即为原始顺序；
        恢复时 cat 拼接后校验 .sha256。
        """
        total = max(1, (size + part_bytes - 1) // part_bytes)
        names = [part_name(filename, index, total) for index in range(1, total + 1)]
        LOG.info(
            "分段上传 %s -> %s：%d 段，每段 %.1f MB",
            filename,
            remote_dir,
            total,
            part_bytes / 1024 / 1024,
        )

        entries = self.list_files(remote_dir)
        remote_sizes = {
            str(item.get("Name") or ""): item.get("Size") for item in entries
        }

        # 清掉同名的整包文件和多余旧分段，避免上次中断留下的残片混进来
        if filename in remote_sizes:
            self.delete_file(self.target(remote_dir, filename))
        for name in remote_sizes:
            if (
                name.startswith(filename + ".part")
                and _PART_SUFFIX_RE.search(name)
                and name not in names
            ):
                LOG.info("清理多余旧分段：%s", name)
                self.delete_file(self.target(remote_dir, name))

        split_dir = tempfile.mkdtemp(
            prefix=".split-", dir=os.path.dirname(local_archive) or "."
        )
        try:
            with open(local_archive, "rb") as handle:
                for index, name in enumerate(names, 1):
                    handle.seek((index - 1) * part_bytes)
                    data = handle.read(part_bytes)
                    if not data:
                        raise RcloneError(f"读取分段失败：{name} 为空")
                    if remote_sizes.get(name) == len(data):
                        LOG.info("分段 %d/%d 已存在，跳过：%s", index, total, name)
                        continue
                    part_path = os.path.join(split_dir, name)
                    with open(part_path, "wb") as out:
                        out.write(data)
                    temp_remote = self.target(remote_dir, "." + name + ".part")
                    self.copyto(part_path, temp_remote)
                    remote_stat = self.stat(temp_remote, quiet=True)
                    if remote_stat is None or remote_stat.get("Size") != len(data):
                        raise RcloneError(
                            f"分段大小校验失败：{name} 本地 {len(data)} 字节，"
                            f"远端 {remote_stat.get('Size') if remote_stat else '未知'}"
                        )
                    self.moveto(temp_remote, self.target(remote_dir, name))
                    os.unlink(part_path)
                    LOG.info(
                        "分段 %d/%d 上传完成：%s（%.1f MB）",
                        index,
                        total,
                        name,
                        len(data) / 1024 / 1024,
                    )
        finally:
            shutil.rmtree(split_dir, ignore_errors=True)

        self._upload_sidecar(sha_path, remote_dir, filename)

        LOG.info("分段上传完成：%s（%d 段）", filename, total)
        return f"{remote_dir}/{names[0]}…{names[-1]}（{total} 段）"

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
