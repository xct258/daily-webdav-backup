"""单次备份执行器：打包、重传、上传、保留清理、状态、通知。"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import retention
from archive import ArchiveError, ArchiveResult, create_archive, ensure_sidecar
from config import Config, Job
from notify import Notifier
from process import CommandInterrupted, CommandTimeout, run_command
from rclone import Rclone, RcloneError
from state import GlobalLock, StateStore
from util import human_size, iso, now

LOG = logging.getLogger("backup.runner")

# pre_cmd / post_cmd 的临时目录，和打包 staging 分开
HOOK_BASE_DIR = "/tmp/backup-hooks"


class RunnerError(Exception):
    """任务执行失败。"""


class PreflightError(RunnerError):
    """配置/路径等确定性错误，重试没有意义。"""


class HookError(RunnerError):
    """pre_cmd/post_cmd 失败。"""


@dataclass
class JobResult:
    name: str
    status: str = "failed"
    archive: str = ""
    size: int = 0
    sha256: str = ""
    remote: str = ""
    duration: float = 0.0
    attempts: int = 0
    retryable: bool = True
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class RunResult:
    started_at: str
    finished_at: str
    status: str
    jobs: list[JobResult] = field(default_factory=list)
    duration: float = 0.0
    error: str = ""


class Runner:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        stop_event: threading.Event | None = None,
    ):
        self.config = config
        self.state = state
        self.stop_event = stop_event or threading.Event()
        self.rclone = Rclone(config, self.stop_event)

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def run(
        self,
        job_names: list[str] | None = None,
        dry_run: bool = False,
        no_upload: bool = False,
        notify: bool = True,
    ) -> RunResult:
        tz = self.config.tz
        started_dt = now(tz)
        started_mono = time.monotonic()

        lock: GlobalLock | None = None
        if not dry_run:
            lock = GlobalLock(self.config.lock_file)
            if not lock.acquire():
                LOG.warning("已有备份任务在运行，本次跳过")
                return RunResult(
                    started_at=iso(started_dt),
                    finished_at=iso(now(tz)),
                    status="skipped",
                    error="已有备份任务在运行",
                )

        result = RunResult(
            started_at=iso(started_dt), finished_at="", status="skipped"
        )

        try:
            try:
                self.rclone.ensure_config()
            except (RcloneError, OSError) as exc:
                result.status = "failed"
                result.error = f"生成 rclone 配置失败：{exc}"
                LOG.error(result.error)
                result.finished_at = iso(now(tz))
                result.duration = time.monotonic() - started_mono
                if notify and not dry_run:
                    self._send_notification(result)
                return result

            jobs = self._select_jobs(job_names)
            if not jobs:
                result.status = "skipped"
                result.error = "没有可执行的任务"
                LOG.warning(result.error)
            else:
                if not dry_run:
                    # 有真实任务时才记录 attempt；全部禁用时不记，
                    # 这样用户改完配置重启后仍能触发首次执行。
                    self.state.mark_attempt(result.started_at)

                for job in jobs:
                    if self.stop_event.is_set():
                        LOG.warning("收到停止信号，剩余任务不再执行")
                        break
                    job_result = self._run_job_with_retries(job, dry_run, no_upload)
                    result.jobs.append(job_result)
                    if self.stop_event.is_set():
                        break
                result.status = self._aggregate(result.jobs)

            result.finished_at = iso(now(tz))
            result.duration = time.monotonic() - started_mono

            if not dry_run:
                if result.status == "success":
                    self.state.mark_success(result.finished_at)

            if notify and not dry_run and result.status != "skipped":
                self._send_notification(result)

            return result
        finally:
            if lock is not None:
                lock.release()

    def _select_jobs(self, job_names: list[str] | None) -> list[Job]:
        if job_names:
            wanted = set(job_names)
            jobs = [job for job in self.config.jobs if job.name in wanted]
            missing = wanted - {job.name for job in jobs}
            if missing:
                raise RunnerError(f"找不到任务：{', '.join(sorted(missing))}")
            return jobs
        return [job for job in self.config.jobs if job.enabled]

    # ------------------------------------------------------------------
    # 单个任务
    # ------------------------------------------------------------------
    def _run_job_with_retries(
        self, job: Job, dry_run: bool, no_upload: bool
    ) -> JobResult:
        attempts = self.config.retry_count + 1
        last: JobResult | None = None

        for index in range(attempts):
            last = self._run_job(job, dry_run, no_upload)
            last.attempts = index + 1
            if last.status == "success":
                return last
            if last.status in {"interrupted", "skipped"} or not last.retryable:
                return last
            if index < attempts - 1:
                LOG.warning(
                    "任务 %s 第 %s/%s 次尝试失败：%s；%ss 后重试",
                    job.name,
                    index + 1,
                    attempts,
                    last.error or last.status,
                    self.config.retry_delay,
                )
                if self._sleep(self.config.retry_delay):
                    last.status = "interrupted"
                    last.error = "收到停止信号"
                    return last

        assert last is not None
        return last

    def _run_job(self, job: Job, dry_run: bool, no_upload: bool) -> JobResult:
        tz = self.config.tz
        started_mono = time.monotonic()
        result = JobResult(name=job.name, status="failed")
        pre_done = False
        pending = self.state.pending(job.name)

        try:
            self._preflight(job)

            if dry_run:
                LOG.info(
                    "[dry-run] 任务 %s：将打包 %s -> %s",
                    job.name,
                    ", ".join(job.paths),
                    self.config.job_remote_path(job),
                )
                result.status = "success"
                result.warnings.append("dry-run：未打包、未上传")
                return result

            pre_done = True
            if job.pre_cmd.strip():
                LOG.info("任务 %s 执行 pre_cmd", job.name)
                self._run_hook(job.pre_cmd, "pre", job)

            archive: ArchiveResult = create_archive(
                job, self.config, now(tz), self.stop_event
            )
            result.archive = archive.path
            result.size = archive.size
            result.sha256 = archive.sha256
            result.warnings.extend(archive.warnings)
            ensure_sidecar(archive.path)

            if no_upload:
                result.status = "success"
                result.warnings.append("no-upload：归档仅保留在本地")
                LOG.info("任务 %s 已跳过上传（no-upload）", job.name)
                return result

            uploaded = False

            # 1. 先重传上次失败的归档
            remaining: list[dict] = []
            for item in pending:
                archive_path = str(item.get("archive") or "")
                if not archive_path or not os.path.exists(archive_path):
                    result.warnings.append(
                        f"待重传归档已不存在，已丢弃：{archive_path or '(空路径)'}"
                    )
                    LOG.warning(result.warnings[-1])
                    continue
                try:
                    remote = self._upload_item(item, job)
                    uploaded = True
                    LOG.info("待重传归档上传成功：%s", remote)
                except (RcloneError, CommandInterrupted, CommandTimeout, OSError) as exc:
                    remaining.append(item)
                    result.warnings.append(f"待重传归档上传失败：{exc}")
                    LOG.warning(result.warnings[-1])

            self.state.set_pending(job.name, remaining)
            pending = remaining
            if self.stop_event.is_set():
                raise CommandInterrupted()

            # 2. 上传本次新归档
            item = self._pending_item(job, archive)
            try:
                result.remote = self._upload_item(item, job)
                uploaded = True
            except (RcloneError, CommandInterrupted, CommandTimeout, OSError) as exc:
                pending.append(item)
                self.state.set_pending(job.name, pending)
                raise RunnerError(f"上传失败：{exc}") from exc

            if uploaded:
                self._cleanup_remote(job, os.path.basename(archive.path))

            self._cleanup_local(job, pending)
            result.status = "success"
            return result

        except CommandInterrupted:
            result.status = "interrupted"
            result.error = "收到停止信号，任务中断"
            LOG.warning("任务 %s 中断", job.name)
            return result
        except CommandTimeout as exc:
            result.status = "failed"
            result.error = str(exc)
            LOG.error("任务 %s 超时：%s", job.name, exc)
            return result
        except (PreflightError, HookError) as exc:
            result.status = "failed"
            result.error = str(exc)
            result.retryable = False
            LOG.error("任务 %s 失败：%s", job.name, exc)
            return result
        except (RunnerError, ArchiveError, RcloneError, OSError) as exc:
            result.status = "failed"
            result.error = str(exc)
            LOG.error("任务 %s 失败：%s", job.name, exc)
            return result
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            LOG.exception("任务 %s 出现未预期错误", job.name)
            return result
        finally:
            result.duration = time.monotonic() - started_mono
            if pre_done and job.post_cmd.strip():
                try:
                    LOG.info("任务 %s 执行 post_cmd", job.name)
                    self._run_hook(job.post_cmd, "post", job, result.archive)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"post_cmd 失败：{exc}")
                    LOG.warning(result.warnings[-1])

            if not dry_run:
                fields = {
                    "last_attempt": iso(now(tz)),
                    "last_status": result.status,
                    "last_archive": result.archive,
                    "last_remote": result.remote,
                    "last_error": result.error,
                    "last_duration": round(result.duration, 1),
                }
                if result.status == "success":
                    fields["last_success"] = iso(now(tz))
                self.state.update_job(job.name, **fields)

    # ------------------------------------------------------------------
    # 子步骤
    # ------------------------------------------------------------------
    def _preflight(self, job: Job) -> None:
        # hook 目录先建好，pre_cmd 可以直接往里写临时文件
        os.makedirs(os.path.join(HOOK_BASE_DIR, job.name), exist_ok=True)

        missing = [path for path in job.paths if not os.path.exists(path)]
        if missing:
            raise PreflightError(f"路径不存在：{', '.join(missing)}")
        if not os.path.isdir(job.archive_root):
            raise PreflightError(
                f"archive_root 不存在或不是目录：{job.archive_root}"
            )

        job_dir = os.path.join(self.config.staging_dir, job.name)
        os.makedirs(job_dir, exist_ok=True)

        if self.config.min_free_gb > 0:
            usage = shutil.disk_usage(self.config.staging_dir)
            required = self.config.min_free_gb * 1024**3
            if usage.free < required:
                raise PreflightError(
                    f"staging 剩余空间不足：{human_size(usage.free)} "
                    f"< {self.config.min_free_gb} GB"
                )

    def _run_hook(
        self, command: str, phase: str, job: Job, archive_path: str = ""
    ) -> None:
        hook_dir = os.path.join(HOOK_BASE_DIR, job.name)
        os.makedirs(hook_dir, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "JOB": job.name,
                "JOB_NAME": job.name,
                "PHASE": phase,
                "ARCHIVE_PATH": archive_path,
                "HOOK_DIR": hook_dir,
                "ARCHIVE_ROOT": job.archive_root,
            }
        )
        result = run_command(
            ["/bin/bash", "-o", "pipefail", "-c", command],
            stop_event=self.stop_event,
            timeout=self.config.hook_timeout,
            env=env,
        )
        if result.returncode != 0:
            raise HookError(
                f"{phase}_cmd 失败（退出码 {result.returncode}）："
                f"{result.output[-1000:] if result.output else '无输出'}"
            )

    def _pending_item(self, job: Job, archive: ArchiveResult) -> dict:
        filename = os.path.basename(archive.path)
        return {
            "archive": archive.path,
            "filename": filename,
            "sha256_file": ensure_sidecar(archive.path),
            "size": archive.size,
            "remote_dir": self.config.job_remote_path(job),
            "created_at": iso(now(self.config.tz)),
        }

    def _upload_item(self, item: dict, job: Job) -> str:
        archive_path = str(item.get("archive") or "")
        if not archive_path or not os.path.exists(archive_path):
            raise RcloneError(f"本地归档不存在：{archive_path or '(空路径)'}")

        filename = str(item.get("filename") or os.path.basename(archive_path))
        sha_path = str(item.get("sha256_file") or ensure_sidecar(archive_path))
        if not os.path.exists(sha_path):
            sha_path = ensure_sidecar(archive_path)
        size = int(item.get("size") or os.path.getsize(archive_path))
        remote_dir = str(
            item.get("remote_dir") or self.config.job_remote_path(job)
        )

        return self.rclone.upload(archive_path, sha_path, remote_dir, filename, size)

    def _cleanup_remote(self, job: Job, current_filename: str) -> None:
        remote_dir = self.config.job_remote_path(job)
        entries = self.rclone.list_files(remote_dir)
        plan = retention.remote_cleanup_plan(entries, job, now(self.config.tz))
        deleted = 0
        for name in plan:
            if name == current_filename:
                continue
            if self.rclone.delete_file(self.rclone.target(remote_dir, name)):
                deleted += 1
                self.rclone.delete_file(
                    self.rclone.target(remote_dir, retention.sidecar_name(name))
                )
        if deleted:
            LOG.info("任务 %s 清理远端旧归档 %s 个", job.name, deleted)

    def _cleanup_local(self, job: Job, pending: list[dict]) -> None:
        job_dir = os.path.join(self.config.staging_dir, job.name)
        pending_files = {
            os.path.basename(str(item.get("archive") or "")) for item in pending
        }
        plan = retention.local_cleanup_plan(
            job_dir,
            job,
            self.config.local_keep,
            pending_files,
            now(self.config.tz),
        )
        for name in plan:
            for path in (
                os.path.join(job_dir, name),
                os.path.join(job_dir, retention.sidecar_name(name)),
            ):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError as exc:
                    LOG.warning("删除本地文件失败：%s（%s）", path, exc)
        retention.cleanup_local_temp_files(job_dir, now(self.config.tz))

    def _sleep(self, seconds: float) -> bool:
        """可被停止信号打断的 sleep；返回 True 表示收到停止信号。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_event.is_set():
                return True
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        return self.stop_event.is_set()

    # ------------------------------------------------------------------
    # 汇总与通知
    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate(jobs: list[JobResult]) -> str:
        if not jobs:
            return "skipped"
        statuses = [job.status for job in jobs]
        if all(status == "success" for status in statuses):
            return "success"
        if all(status in {"failed", "interrupted"} for status in statuses):
            return "failed"
        if any(status == "success" for status in statuses):
            return "partial"
        return "failed"

    def _send_notification(self, result: RunResult) -> None:
        status, title, message = self._notification(result)
        try:
            Notifier(self.config.notify).send(status, title, message)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("发送通知失败：%s", exc)

    def _notification(self, result: RunResult) -> tuple[str, str, str]:
        host = self.config.notify.server_name
        title_map = {
            "success": f"备份成功：{host}",
            "partial": f"备份部分失败：{host}",
            "failed": f"备份失败：{host}",
            "skipped": f"备份跳过：{host}",
        }
        status_map = {
            "success": "ok",
            "partial": "warn",
            "failed": "error",
            "skipped": "info",
        }
        title = title_map.get(result.status, f"备份状态：{result.status}")
        status = status_map.get(result.status, "info")

        job_status_text = {
            "success": "成功",
            "failed": "失败",
            "interrupted": "中断",
            "skipped": "跳过",
        }

        status_text = {
            "success": "成功",
            "partial": "部分失败",
            "failed": "失败",
            "skipped": "跳过",
        }
        lines = [
            f"**主机**：`{host}`",
            f"**时间**：{result.started_at}",
            f"**结果**：{status_text.get(result.status, result.status)}",
            f"**耗时**：{result.duration:.1f}s",
        ]
        if self.config.schedule:
            lines.append(f"**调度**：`{self.config.schedule}`（{self.config.timezone}）")
        lines.append("")

        if result.jobs:
            lines.append("| 任务 | 状态 | 归档 | 大小 | 耗时 | 远端 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for job in result.jobs:
                name = job.name.replace("|", "\\|")
                archive = os.path.basename(job.archive) if job.archive else "-"
                size = human_size(job.size) if job.size else "-"
                remote = job.remote.replace("|", "\\|") if job.remote else "-"
                lines.append(
                    f"| {name} | {job_status_text.get(job.status, job.status)} "
                    f"| {archive} | {size} | {job.duration:.1f}s | {remote} |"
                )
        else:
            lines.append("没有任务执行。")

        errors = [job for job in result.jobs if job.error]
        if result.error:
            lines.append("")
            lines.append(f"**错误**：{result.error}")
        if errors:
            lines.append("")
            lines.append("**失败原因**：")
            for job in errors:
                lines.append(f"- `{job.name}`：{job.error}")

        warnings = [
            warning for job in result.jobs for warning in job.warnings
        ]
        if warnings:
            lines.append("")
            lines.append("**告警**：")
            for warning in warnings[:20]:
                lines.append(f"- {warning}")

        return status, title, "\n".join(lines)
