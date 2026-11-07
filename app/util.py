"""通用小工具：时间、大小格式化、ISO 时间解析。"""
from __future__ import annotations

import os
import re
from datetime import datetime, tzinfo


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def now(tz: tzinfo) -> datetime:
    return datetime.now(tz)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def human_size(num: int | float) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def expand_env(value: str) -> str:
    """把配置里的 ${VAR} 替换成环境变量；未设置时替换为空字符串。"""

    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_RE.sub(repl, value)


def yes_no(value: bool) -> str:
    return "是" if value else "否"
