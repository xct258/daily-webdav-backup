"""子进程执行：支持超时、并行读取输出、收到停止信号时终止子进程组。"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

LOG = logging.getLogger("backup.command")


class CommandInterrupted(Exception):
    """收到停止信号，命令被终止。"""


class CommandTimeout(Exception):
    """命令执行超时。"""


@dataclass
class CommandResult:
    cmd: list[str]
    returncode: int
    output: str
    duration: float


def _terminate_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_command(
    cmd: list[str],
    *,
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    log_output: bool = True,
    log_level: int = logging.INFO,
    collect_output: bool = True,
    input_text: str | None = None,
) -> CommandResult:
    LOG.debug("执行命令：%s", " ".join(cmd))
    started = time.monotonic()

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    assert proc.stdout is not None

    if input_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    lines: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if collect_output:
                    with lock:
                        lines.append(line)
                if log_output and line.strip():
                    LOG.log(log_level, line)
        except (ValueError, OSError):
            pass

    thread = threading.Thread(target=reader, name="cmd-reader", daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout if timeout else None
    try:
        while True:
            if proc.poll() is not None:
                break
            if stop_event is not None and stop_event.is_set():
                _terminate_group(proc)
                raise CommandInterrupted("收到停止信号，已终止命令")
            if deadline is not None and time.monotonic() > deadline:
                _terminate_group(proc)
                raise CommandTimeout(f"命令执行超时（{timeout:.0f}s）：{cmd[0]}")
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            _terminate_group(proc)
        thread.join(timeout=2)

    with lock:
        output = "\n".join(lines)
    return CommandResult(
        cmd=cmd,
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=output,
        duration=time.monotonic() - started,
    )
