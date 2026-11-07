"""状态文件、全局锁、心跳。"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Any

from util import iso

LOG = logging.getLogger("backup.state")


class StateStore:
    """轻量 JSON 状态，原子写入，权限 600。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = self._load()

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "last_attempt": None,
            "last_success": None,
            "jobs": {},
        }

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("根节点不是对象")
            data.setdefault("version", 1)
            data.setdefault("jobs", {})
            return data
        except (OSError, ValueError) as exc:
            LOG.warning("读取状态文件失败，将重新开始：%s", exc)
            return self._empty()

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def job(self, name: str) -> dict[str, Any]:
        jobs = self.data.setdefault("jobs", {})
        return jobs.setdefault(name, {})

    def pending(self, name: str) -> list[dict[str, Any]]:
        value = self.job(name).get("pending") or []
        return [item for item in value if isinstance(item, dict)]

    def set_pending(self, name: str, pending: list[dict[str, Any]]) -> None:
        with self._lock:
            self.job(name)["pending"] = pending
            self.save()

    def update_job(self, name: str, **fields: Any) -> None:
        with self._lock:
            self.job(name).update(fields)
            self.save()

    def mark_attempt(self, when: str) -> None:
        with self._lock:
            self.data["last_attempt"] = when
            self.save()

    def mark_success(self, when: str) -> None:
        with self._lock:
            self.data["last_success"] = when
            self.save()


class GlobalLock:
    """flock 全局锁，避免定时任务和手动任务重叠。"""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "GlobalLock":
        if not self.acquire():
            raise RuntimeError("已有备份任务在运行")
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class Heartbeat:
    """后台线程定期更新心跳文件，供 healthcheck 使用。"""

    def __init__(self, path: str, interval: int = 30):
        self.path = path
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.touch()
        self._thread.start()

    def touch(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(iso(datetime.now().astimezone()))
        except OSError as exc:
            LOG.warning("写入心跳失败：%s", exc)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.touch()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)


def heartbeat_healthy(path: str, max_age: int = 300) -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, f"心跳文件不存在：{path}"
    age = time.time() - os.path.getmtime(path)
    if age > max_age:
        return False, f"心跳已过期：{age:.0f}s > {max_age}s"
    return True, f"心跳正常：{age:.0f}s"
