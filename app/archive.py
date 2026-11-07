"""打包：GNU tar + zstd/gzip，生成 sha256 校验文件。"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime

from config import Config, EncryptionConfig, Job
from process import CommandInterrupted, CommandTimeout, run_command

LOG = logging.getLogger("backup.archive")


class ArchiveError(Exception):
    """打包失败。"""


@dataclass
class ArchiveResult:
    path: str
    size: int
    sha256: str
    duration: float
    warnings: list[str] = field(default_factory=list)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_sidecar(archive_path: str, sha256: str | None = None) -> str:
    """写 <archive>.sha256，格式兼容 sha256sum -c。"""
    if sha256 is None:
        sha256 = _sha256_file(archive_path)
    sidecar = archive_path + ".sha256"
    with open(sidecar, "w", encoding="utf-8") as handle:
        handle.write(f"{sha256}  {os.path.basename(archive_path)}\n")
    os.chmod(sidecar, 0o644)
    return sidecar


def ensure_sidecar(archive_path: str) -> str:
    sidecar = archive_path + ".sha256"
    if not os.path.exists(sidecar):
        LOG.info("校验文件不存在，重新生成：%s", sidecar)
        write_sidecar(archive_path)
    return sidecar


def archive_name(
    job: Job,
    when: datetime,
    encryption: EncryptionConfig | None = None,
) -> str:
    suffix = {
        "zstd": ".tar.zst",
        "gzip": ".tar.gz",
        "none": ".tar",
    }[job.compression]
    if encryption is not None and encryption.enabled:
        suffix += ".age" if encryption.method == "age" else ".gpg"
    return f"{job.name}-{when.strftime('%Y%m%d-%H%M%S')}{suffix}"


def _relative_path(path: str, root: str) -> str:
    if root == "/":
        return path.lstrip("/") or "."
    return os.path.relpath(path, root)


def _exclude_patterns(job: Job, config: Config) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()

    def add(pattern: str) -> None:
        pattern = pattern.strip()
        if pattern and pattern not in seen:
            seen.add(pattern)
            patterns.append(pattern)

    for item in job.exclude:
        add(item)

    # 备份 / 时跳过虚拟文件系统和易变目录，避免 tar 卡住或报错
    if job.archive_root == "/":
        for name in ("proc", "sys", "dev", "run", "tmp"):
            add(name)
            add(name + "/*")

    # 永远不要把备份程序自己的临时目录、状态、日志、密钥打进去
    for path in (
        config.staging_dir,
        config.state_dir,
        os.path.join(os.path.dirname(config.state_dir) or "/data", "logs"),
        config.webdav.rclone_config,
        config.config_file,
    ):
        try:
            rel = _relative_path(path, job.archive_root)
        except ValueError:
            continue
        if rel.startswith(".."):
            continue
        add(rel)
        add(rel.rstrip("/") + "/*")

    lock_rel = _relative_path(config.lock_file, job.archive_root)
    if not lock_rel.startswith(".."):
        add(lock_rel)

    return patterns


def _write_exclude_file(path: str, patterns: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for pattern in patterns:
            handle.write(pattern + "\n")


def _compression_args(job: Job) -> list[str]:
    if job.compression == "zstd":
        return ["-I", f"zstd -T0 -{job.zstd_level}"]
    if job.compression == "gzip":
        return ["-z"]
    return []


def _verify(archive_path: str, stop_event: threading.Event | None) -> None:
    result = run_command(
        ["tar", "-tf", archive_path],
        stop_event=stop_event,
        timeout=3600,
        log_output=False,
        collect_output=False,
    )
    if result.returncode != 0:
        raise ArchiveError(f"归档完整性检查失败（tar -tf 退出码 {result.returncode}）")


def _encrypt_archive(
    plain_path: str,
    encrypted_path: str,
    encryption: EncryptionConfig,
    stop_event: threading.Event | None,
) -> None:
    if encryption.method == "age":
        if not encryption.recipient:
            raise ArchiveError("age 加密缺少 recipient 公钥")
        cmd = [
            "age",
            "--encrypt",
            "--recipient",
            encryption.recipient,
            "--output",
            encrypted_path,
            plain_path,
        ]
        result = run_command(
            cmd,
            stop_event=stop_event,
            timeout=3600,
            log_output=True,
        )
    else:
        passphrase = encryption.passphrase or os.environ.get(
            encryption.passphrase_env, ""
        )
        if not passphrase:
            raise ArchiveError(
                "GPG 加密缺少密码：配置 encryption.passphrase 或环境变量 "
                f"{encryption.passphrase_env}"
            )
        cmd = [
            "gpg",
            "--batch",
            "--yes",
            "--no-tty",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "--output",
            encrypted_path,
            plain_path,
        ]
        result = run_command(
            cmd,
            stop_event=stop_event,
            timeout=3600,
            log_output=False,
            input_text=passphrase + "\n",
        )

    if result.returncode != 0:
        raise ArchiveError(
            f"{encryption.method} 加密失败（退出码 {result.returncode}）："
            f"{result.output[-1000:] if result.output else '无输出'}"
        )
    if not os.path.exists(encrypted_path) or os.path.getsize(encrypted_path) <= 0:
        raise ArchiveError(f"{encryption.method} 加密后文件不存在或为空")
    LOG.info("加密完成：%s", os.path.basename(encrypted_path))


def create_archive(
    job: Job,
    config: Config,
    when: datetime,
    stop_event: threading.Event | None = None,
) -> ArchiveResult:
    job_dir = os.path.join(config.staging_dir, job.name)
    os.makedirs(job_dir, exist_ok=True)

    plain_filename = archive_name(job, when)
    plain_path = os.path.join(job_dir, plain_filename)
    final_filename = archive_name(job, when, config.encryption)
    archive_path = os.path.join(job_dir, final_filename)
    exclude_file = os.path.join(job_dir, f".exclude-{when.strftime('%Y%m%d-%H%M%S')}.txt")
    warnings: list[str] = []
    success = False

    try:
        _write_exclude_file(exclude_file, _exclude_patterns(job, config))

        cmd = [
            "tar",
            "-C",
            job.archive_root,
            "--numeric-owner",
            "--sparse",
            f"--exclude-from={exclude_file}",
        ]
        if job.xattrs:
            cmd.append("--xattrs")
        if job.one_file_system:
            cmd.append("--one-file-system")
        cmd.extend(_compression_args(job))
        cmd.extend(["-cf", plain_path])
        cmd.extend(_relative_path(path, job.archive_root) for path in job.paths)

        LOG.info("开始打包 %s：%s", job.name, " ".join(job.paths))
        result = run_command(
            cmd,
            stop_event=stop_event,
            log_output=True,
            log_level=logging.INFO,
        )
        if result.returncode not in (0, 1):
            raise ArchiveError(
                f"tar 执行失败（退出码 {result.returncode}）："
                f"{result.output[-2000:] if result.output else '无输出'}"
            )
        if result.returncode == 1:
            warnings.append("tar 退出码 1：部分文件在读取时发生变化，归档仍可用")
            LOG.warning(warnings[-1])

        if not os.path.exists(plain_path):
            raise ArchiveError("tar 执行结束，但归档文件不存在")
        if os.path.getsize(plain_path) <= 0:
            raise ArchiveError("归档文件大小为 0")

        if config.verify_archive:
            LOG.info("检查归档完整性：%s", plain_filename)
            _verify(plain_path, stop_event)

        if config.encryption.enabled:
            LOG.info(
                "加密归档：%s -> %s", plain_filename, final_filename
            )
            _encrypt_archive(
                plain_path, archive_path, config.encryption, stop_event
            )
            try:
                os.unlink(plain_path)
            except OSError as exc:
                raise ArchiveError(f"加密成功但删除明文归档失败：{exc}") from exc
        else:
            archive_path = plain_path

        if not os.path.exists(archive_path):
            raise ArchiveError("加密后归档文件不存在")
        size = os.path.getsize(archive_path)
        if size <= 0:
            raise ArchiveError("归档文件大小为 0")

        sha256 = _sha256_file(archive_path)
        write_sidecar(archive_path, sha256)

        checksum_result = run_command(
            ["sha256sum", "-c", os.path.basename(archive_path) + ".sha256"],
            cwd=job_dir,
            stop_event=stop_event,
            timeout=3600,
            log_output=False,
        )
        if checksum_result.returncode != 0:
            raise ArchiveError(
                "sha256 自检失败："
                + (checksum_result.output[-500:] if checksum_result.output else "无输出")
            )

        LOG.info(
            "打包完成 %s：%s（%.1f MB，%.1fs）",
            job.name,
            final_filename,
            size / 1024 / 1024,
            result.duration,
        )
        success = True
        return ArchiveResult(
            path=archive_path,
            size=size,
            sha256=sha256,
            duration=result.duration,
            warnings=warnings,
        )
    except (CommandInterrupted, CommandTimeout):
        raise
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError(f"打包 {job.name} 失败：{exc}") from exc
    finally:
        try:
            if os.path.exists(exclude_file):
                os.unlink(exclude_file)
        except OSError:
            pass
        # 失败时清掉不完整的明文/加密归档和校验文件
        if not success:
            for path in {
                plain_path,
                archive_path,
                plain_path + ".sha256",
                archive_path + ".sha256",
            }:
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass
