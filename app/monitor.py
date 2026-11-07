"""容器内磁盘空间监控。

默认只检查容器根文件系统 `/`。
需要监控其它目录时，在 config.yml 的 monitor.paths 里单独配置。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import ConfigError, MonitorPath, load_config
from notify import Notifier
from util import human_size, iso, parse_iso

LOG = logging.getLogger("backup.monitor")


def is_low(
    free: int,
    total: int,
    min_free_gb: float,
    min_free_percent: float,
) -> bool:
    """空闲字节数低于任一阈值时返回 True。"""
    if total <= 0:
        return False
    free_gb = free / 1024**3
    free_percent = free / total * 100
    return (min_free_gb > 0 and free_gb < min_free_gb) or (
        min_free_percent > 0 and free_percent < min_free_percent
    )


def should_notify(
    low: bool,
    previous_status: str | None,
    last_alert: datetime | None,
    now_dt: datetime,
    cooldown_minutes: int,
) -> bool:
    """是否应该推送。

    - 首次不足：推
    - 持续不足：cooldown 到了才再推
    - 恢复：由调用方结合 notify_recovery 决定
    """
    if low:
        if previous_status != "low" or last_alert is None:
            return True
        return now_dt - last_alert >= timedelta(minutes=cooldown_minutes)
    return previous_status == "low"


@dataclass
class DiskSnapshot:
    device: str
    name: str
    path: str
    total: int
    free: int
    free_percent: float


class Monitor:
    def __init__(
        self,
        config_file: str,
        state_path: str,
        stop_event: threading.Event | None = None,
    ):
        self.config_file = config_file
        self.state_path = state_path
        self.stop_event = stop_event or threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="disk-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        self.check_once()
        while not self._stop.is_set() and not self.stop_event.is_set():
            interval = self._interval()
            if self._stop.wait(interval):
                break
            self.check_once()

    def _interval(self) -> int:
        try:
            config = load_config(self.config_file)
            return max(1, config.monitor.interval)
        except Exception:  # noqa: BLE001
            return 300

    # ------------------------------------------------------------------
    # 检查
    # ------------------------------------------------------------------
    def check_once(self) -> None:
        try:
            config = load_config(self.config_file)
        except ConfigError as exc:
            LOG.warning("监控读取配置失败：%s", exc)
            return
        except Exception as exc:  # noqa: BLE001
            LOG.warning("监控读取配置异常：%s", exc)
            return

        monitor = config.monitor
        if not monitor.enabled:
            return

        state = self._load_state()
        filesystems = state.setdefault("filesystems", {})
        seen_devices: set[str] = set()
        now_dt = datetime.now(config.tz)

        for item in monitor.paths:
            snapshot = self._snapshot(item)
            if snapshot is None:
                continue
            if snapshot.device in seen_devices:
                LOG.debug(
                    "监控路径 %s 与已检查路径属于同一文件系统，跳过",
                    snapshot.path,
                )
                continue
            seen_devices.add(snapshot.device)

            min_gb = (
                item.min_free_gb
                if item.min_free_gb is not None
                else monitor.min_free_gb
            )
            min_pct = (
                item.min_free_percent
                if item.min_free_percent is not None
                else monitor.min_free_percent
            )
            low = is_low(snapshot.free, snapshot.total, min_gb, min_pct)

            previous = filesystems.get(snapshot.device, {})
            previous_status = previous.get("status")
            last_alert = parse_iso(str(previous.get("last_alert") or ""))
            if last_alert is not None and last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=config.tz)

            notify = should_notify(
                low,
                previous_status,
                last_alert,
                now_dt,
                monitor.cooldown_minutes,
            )
            if not low and not monitor.notify_recovery:
                notify = False

            if notify:
                self._notify(config, snapshot, min_gb, min_pct, low)

            filesystems[snapshot.device] = {
                "name": snapshot.name,
                "path": snapshot.path,
                "status": "low" if low else "ok",
                "total": snapshot.total,
                "free": snapshot.free,
                "free_percent": round(snapshot.free_percent, 2),
                "last_check": iso(now_dt),
                "last_alert": (
                    iso(now_dt) if notify and low
                    else previous.get("last_alert")
                ),
            }

            if low:
                LOG.warning(
                    "磁盘空间不足：%s %s 剩余 %s / %s（%.1f%%）",
                    snapshot.name,
                    snapshot.path,
                    human_size(snapshot.free),
                    human_size(snapshot.total),
                    snapshot.free_percent,
                )
            else:
                LOG.info(
                    "磁盘空间正常：%s %s 剩余 %s / %s（%.1f%%）",
                    snapshot.name,
                    snapshot.path,
                    human_size(snapshot.free),
                    human_size(snapshot.total),
                    snapshot.free_percent,
                )

        state["last_check"] = iso(now_dt)
        self._save_state(state)

    def _snapshot(self, item: MonitorPath) -> DiskSnapshot | None:
        try:
            stat = os.stat(item.path)
            vfs = os.statvfs(item.path)
        except OSError as exc:
            LOG.warning("监控路径不可访问：%s（%s）", item.path, exc)
            return None

        block_size = vfs.f_frsize or vfs.f_bsize
        total = vfs.f_blocks * block_size
        free = vfs.f_bavail * block_size
        percent = (free / total * 100) if total else 0.0
        return DiskSnapshot(
            device=str(stat.st_dev),
            name=item.name or item.path,
            path=item.path,
            total=total,
            free=free,
            free_percent=percent,
        )

    def _notify(
        self,
        config,
        snapshot: DiskSnapshot,
        min_gb: float,
        min_pct: float,
        low: bool,
    ) -> None:
        space = (
            f"{human_size(snapshot.free)} / {human_size(snapshot.total)}"
            f"（{snapshot.free_percent:.1f}%）"
        )
        if low:
            title = f"磁盘空间不足：{snapshot.name}（剩余 {space}）"
            status = "warn"
            message = (
                f"**路径**：`{snapshot.path}`\n\n"
                f"**剩余**：{space}\n\n"
                f"**阈值**：{min_gb:g} GB 或 {min_pct:g}%"
            )
            push = None
        else:
            title = f"磁盘空间恢复：{snapshot.name}（剩余 {space}）"
            status = "ok"
            message = (
                f"**路径**：`{snapshot.path}`\n\n"
                f"**当前剩余**：{space}"
            )
            # 恢复通知默认也推到 MQTT，不受 on_success_mqtt 影响
            push = True

        try:
            Notifier(config.notify).send(
                status, title, message, push_to_mqtt=push
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("磁盘监控推送失败：%s", exc)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return {"version": 1, "filesystems": {}}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("根节点不是对象")
            if not isinstance(data.get("filesystems"), dict):
                data["filesystems"] = {}
            return data
        except (OSError, ValueError) as exc:
            LOG.warning("读取监控状态失败：%s", exc)
            return {"version": 1, "filesystems": {}}

    def _save_state(self, data: dict) -> None:
        directory = os.path.dirname(self.state_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".monitor-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
