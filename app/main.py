"""备份程序入口。

用法：
  python3 main.py schedule            # 常驻调度（容器主进程）
  python3 main.py run                 # 立即执行一次
  python3 main.py run --job etc       # 只执行指定任务
  python3 main.py validate            # 校验配置和路径
  python3 main.py test-connection     # 测试 WebDAV
  python3 main.py list                # 列出远端归档
  python3 main.py health              # healthcheck
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time

from config import Config, ConfigError, describe, load_config
from rclone import Rclone, RcloneError
from runner import Runner, RunnerError, RunResult
from scheduler import Scheduler
from state import StateStore, heartbeat_healthy
from util import human_size

LOG = logging.getLogger("backup.main")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def set_timezone(config: Config) -> None:
    os.environ["TZ"] = config.timezone
    try:
        time.tzset()
    except AttributeError:
        pass


def install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum: int, _frame) -> None:
        LOG.warning("收到信号 %s，准备退出", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _prepare_dirs(config: Config) -> None:
    os.makedirs(config.staging_dir, exist_ok=True)
    os.makedirs(config.state_dir, exist_ok=True)


# ----------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------
def cmd_schedule(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    set_timezone(config)
    setup_logging(config.log_level)
    _prepare_dirs(config)

    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    Scheduler(args.config, stop_event).run()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    set_timezone(config)
    setup_logging(config.log_level)
    _prepare_dirs(config)

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    state = StateStore(os.path.join(config.state_dir, "state.json"))
    runner = Runner(config, state, stop_event)
    dry_run = args.dry_run or config.dry_run

    try:
        result = runner.run(
            job_names=args.job,
            dry_run=dry_run,
            no_upload=args.no_upload,
            notify=not args.no_notify,
        )
    except RunnerError as exc:
        LOG.error("%s", exc)
        return 1

    _print_run_result(result)
    if result.status == "success":
        return 0
    if result.status == "partial":
        return 2
    return 1


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    set_timezone(config)
    setup_logging(config.log_level)
    _prepare_dirs(config)

    errors: list[str] = []

    for binary in ("tar", "rclone"):
        if shutil.which(binary) is None:
            errors.append(f"缺少命令：{binary}")

    if any(
        job.enabled and job.compression == "zstd" for job in config.jobs
    ) and shutil.which("zstd") is None:
        errors.append("缺少命令：zstd（有任务使用 zstd 压缩）")

    if config.encryption.enabled:
        binary = "age" if config.encryption.method == "age" else "gpg"
        if shutil.which(binary) is None:
            errors.append(
                f"缺少命令：{binary}（encryption.method={config.encryption.method}）"
            )

    for job in config.jobs:
        if not job.enabled:
            continue
        for path in job.paths:
            if not os.path.exists(path):
                errors.append(f"任务 {job.name} 的路径不存在：{path}")
        if not os.path.isdir(job.archive_root):
            errors.append(
                f"任务 {job.name} 的 archive_root 不存在或不是目录：{job.archive_root}"
            )

    rclone = Rclone(config)
    try:
        rclone.ensure_config()
    except (RcloneError, OSError) as exc:
        errors.append(f"生成 rclone 配置失败：{exc}")

    if errors:
        print("配置校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("配置校验通过")
    print(describe(config))
    return 0


def cmd_test_connection(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    set_timezone(config)
    setup_logging(config.log_level)

    rclone = Rclone(config)
    try:
        rclone.ensure_config()
    except (RcloneError, OSError) as exc:
        print(f"生成 rclone 配置失败：{exc}", file=sys.stderr)
        return 1

    ok, message = rclone.test_connection()
    print(message)
    return 0 if ok else 1


def cmd_health(args: argparse.Namespace) -> int:
    state_dir = os.environ.get("STATE_DIR", "/data/state")
    path = os.path.join(state_dir, "heartbeat")
    max_age = int(os.environ.get("HEALTH_MAX_AGE", "300"))
    ok, message = heartbeat_healthy(path, max_age)
    print(message)
    return 0 if ok else 1


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    set_timezone(config)
    setup_logging(config.log_level)

    rclone = Rclone(config)
    try:
        rclone.ensure_config()
    except (RcloneError, OSError) as exc:
        print(f"生成 rclone 配置失败：{exc}", file=sys.stderr)
        return 1

    jobs = config.jobs
    if args.job:
        wanted = set(args.job)
        jobs = [job for job in jobs if job.name in wanted]

    for job in jobs:
        remote_dir = config.job_remote_path(job)
        entries = rclone.list_files(remote_dir)
        print(f"\n[{job.name}] {config.webdav.remote_name}:{remote_dir}")
        if not entries:
            print("  （空）")
            continue
        for item in sorted(
            entries, key=lambda value: str(value.get("Name") or ""), reverse=True
        ):
            name = str(item.get("Name") or "")
            if name.endswith(".sha256"):
                continue
            size = int(item.get("Size") or 0)
            modtime = str(item.get("ModTime") or "")
            print(f"  {name}  {human_size(size):>10}  {modtime}")
    return 0


# ----------------------------------------------------------------------
def _print_run_result(result: RunResult) -> None:
    print(f"执行结果：{result.status}（{result.duration:.1f}s）")
    if result.error:
        print(f"错误：{result.error}")
    for job in result.jobs:
        line = f"  - {job.name}: {job.status}"
        if job.archive:
            line += f"  归档={job.archive}"
        if job.size:
            line += f"  大小={human_size(job.size)}"
        if job.remote:
            line += f"  远端={job.remote}"
        if job.error:
            line += f"  错误={job.error}"
        print(line)
        for warning in job.warnings:
            print(f"      告警：{warning}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认 /config/config.yml）",
    )

    parser = argparse.ArgumentParser(
        description="定时打包并上传到 rclone WebDAV",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("schedule", parents=[common], help="常驻调度（容器主进程）")

    run = sub.add_parser("run", parents=[common], help="立即执行一次")
    run.add_argument("--job", action="append", help="只执行指定任务，可重复")
    run.add_argument("--dry-run", action="store_true", help="只检查，不打包不上传")
    run.add_argument("--no-upload", action="store_true", help="只打包，不上传")
    run.add_argument("--no-notify", action="store_true", help="不发送推送")

    sub.add_parser("validate", parents=[common], help="校验配置和路径")
    sub.add_parser("test-connection", parents=[common], help="测试 WebDAV 连接")
    sub.add_parser("health", parents=[common], help="healthcheck")

    ls = sub.add_parser("list", parents=[common], help="列出远端归档")
    ls.add_argument("--job", action="append", help="只看指定任务，可重复")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "schedule":
            return cmd_schedule(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "test-connection":
            return cmd_test_connection(args)
        if args.command == "health":
            return cmd_health(args)
        if args.command == "list":
            return cmd_list(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
