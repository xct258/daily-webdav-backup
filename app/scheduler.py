"""常驻调度器：按 cron 执行，支持启动补跑、配置热加载。"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from croniter import croniter

from config import Config, ConfigError, load_config
from runner import Runner
from state import Heartbeat, StateStore
from util import iso, now, parse_iso

LOG = logging.getLogger("backup.scheduler")


class Scheduler:
    def __init__(self, config_path: str | None, stop_event: threading.Event):
        self.config_path = config_path
        self.stop_event = stop_event
        self.state: StateStore | None = None
        self.heartbeat: Heartbeat | None = None

    def run(self) -> None:
        config = load_config(self.config_path)
        os.makedirs(config.state_dir, exist_ok=True)
        os.makedirs(config.staging_dir, exist_ok=True)

        self.state = StateStore(os.path.join(config.state_dir, "state.json"))
        self.heartbeat = Heartbeat(
            os.path.join(config.state_dir, "heartbeat"), interval=30
        )
        self.heartbeat.start()

        try:
            LOG.info(
                "调度器启动：cron=%s 时区=%s 配置=%s",
                config.schedule,
                config.timezone,
                config.config_file,
            )
            self._maybe_run_missed(config)

            while not self.stop_event.is_set():
                try:
                    config = load_config(self.config_path)
                except ConfigError as exc:
                    LOG.error("配置读取失败：%s；60 秒后重试", exc)
                    if self.stop_event.wait(60):
                        break
                    continue

                os.environ["TZ"] = config.timezone
                try:
                    time.tzset()
                except AttributeError:
                    pass

                next_run = self._next_run(config)
                LOG.info("下次执行：%s", iso(next_run))
                if self._wait_until(next_run):
                    break
                if self.stop_event.is_set():
                    break

                self._execute(config)
        finally:
            if self.heartbeat is not None:
                self.heartbeat.stop()
            LOG.info("调度器已退出")

    # ------------------------------------------------------------------
    def _maybe_run_missed(self, config: Config) -> None:
        assert self.state is not None
        last_attempt_raw = self.state.data.get("last_attempt")

        if not last_attempt_raw:
            if config.run_on_first_start:
                LOG.info("没有历史执行记录，启动后立即执行一次")
                self._execute(config)
            else:
                LOG.info("没有历史执行记录，等待下一个计划点")
            return

        if not config.run_missed:
            return

        last_attempt = parse_iso(str(last_attempt_raw))
        if last_attempt is None:
            LOG.warning("last_attempt 无法解析：%s", last_attempt_raw)
            return

        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=config.tz)

        try:
            next_after = croniter(config.schedule, last_attempt).get_next(datetime)
        except (ValueError, KeyError) as exc:
            LOG.warning("计算补跑时间失败：%s", exc)
            return
        if next_after.tzinfo is None:
            next_after = next_after.replace(tzinfo=config.tz)

        current = now(config.tz)
        if current >= next_after:
            LOG.info(
                "发现错过的计划点（%s），启动后补跑一次", iso(next_after)
            )
            self._execute(config)

    def _next_run(self, config: Config) -> datetime:
        base = now(config.tz)
        next_run = croniter(config.schedule, base).get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=config.tz)
        return next_run

    def _wait_until(self, target: datetime) -> bool:
        """返回 True 表示收到停止信号。"""
        while not self.stop_event.is_set():
            current = now(target.tzinfo) if target.tzinfo else datetime.now().astimezone()
            remaining = (target - current).total_seconds()
            if remaining <= 0:
                return False
            if self.stop_event.wait(min(remaining, 30.0)):
                return True
        return True

    def _execute(self, config: Config) -> None:
        assert self.state is not None
        LOG.info("开始执行一次备份")
        runner = Runner(config, self.state, self.stop_event)
        try:
            result = runner.run()
        except Exception as exc:  # noqa: BLE001 - 调度器不能因为单次异常退出
            LOG.exception("执行备份失败：%s", exc)
            return

        if result.status == "skipped":
            LOG.warning("本次跳过：%s", result.error)
        else:
            LOG.info(
                "本次执行完成：%s（%s 个任务，%.1fs）",
                result.status,
                len(result.jobs),
                result.duration,
            )
