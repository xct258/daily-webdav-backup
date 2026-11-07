"""远端/本地保留策略。只处理匹配 <job>-YYYYmmdd-HHMMSS.tar* 的文件。"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, tzinfo

from config import Job

LOG = logging.getLogger("backup.retention")


def _archive_re(job_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(job_name)}-(\d{{8}})-(\d{{6}})\.tar(?:\.zst|\.gz)?"
        rf"(?:\.age|\.gpg)?$"
    )


def parse_archive_time(name: str, job_name: str, tz: tzinfo) -> datetime | None:
    match = _archive_re(job_name).match(name)
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group(1) + match.group(2), "%Y%m%d%H%M%S"
        ).replace(tzinfo=tz)
    except ValueError:
        return None


def remote_cleanup_plan(
    entries: list[dict],
    job: Job,
    now: datetime,
) -> list[str]:
    """返回需要删除的远端归档相对路径（不含 .sha256）。

    entries 可以是 rclone lsjson 的递归结果：
      Path 是相对于任务根目录的路径，Name 是文件名。
    """
    matches: list[tuple[str, datetime]] = []
    for item in entries:
        path = str(item.get("Path") or item.get("Name") or "")
        if not path:
            continue
        name = str(item.get("Name") or path.rsplit("/", 1)[-1])
        when = parse_archive_time(name, job.name, now.tzinfo)
        if when is not None:
            matches.append((path, when))

    matches.sort(key=lambda pair: pair[1], reverse=True)

    delete: set[str] = set()
    if job.keep_last > 0:
        for path, _ in matches[job.keep_last:]:
            delete.add(path)

    if job.keep_days > 0:
        cutoff = now - timedelta(days=job.keep_days)
        for path, when in matches:
            if when < cutoff:
                delete.add(path)

    return sorted(delete)


def local_cleanup_plan(
    job_dir: str,
    job: Job,
    keep: int,
    pending_files: set[str],
    now: datetime,
) -> list[str]:
    """返回需要删除的本地归档文件名（不含 .sha256）。"""
    if not os.path.isdir(job_dir):
        return []

    names: list[tuple[str, datetime]] = []
    for name in os.listdir(job_dir):
        when = parse_archive_time(name, job.name, now.tzinfo)
        if when is not None:
            names.append((name, when))
    names.sort(key=lambda pair: pair[1], reverse=True)

    protect = set(pending_files)
    for name, _ in names[:keep]:
        protect.add(name)

    return sorted(name for name, _ in names if name not in protect)


def sidecar_name(name: str) -> str:
    return name + ".sha256"


def cleanup_local_temp_files(job_dir: str, now: datetime) -> None:
    """清理一天前的 .exclude-*.txt。"""
    if not os.path.isdir(job_dir):
        return
    cutoff = now.timestamp() - 86400
    for name in os.listdir(job_dir):
        if not (name.startswith(".exclude-") and name.endswith(".txt")):
            continue
        path = os.path.join(job_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass
