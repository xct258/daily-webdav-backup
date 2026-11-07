"""配置加载与校验。

配置文件只有一个：/config/config.yml。
WebDAV 地址、用户名、密码、远端路径都写在配置里；
程序启动时自动生成 rclone 配置（密码用 rclone obscure 处理）。
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter

from util import expand_env


class ConfigError(Exception):
    """配置错误。"""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前值：{value}") from exc


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: object, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数，当前值：{value!r}") from exc


def _norm_path(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise ConfigError("路径不能为空")
    if not os.path.isabs(value):
        raise ConfigError(f"路径必须是绝对路径：{value}")
    return os.path.normpath(value)


def _strip_slashes(value: str) -> str:
    return str(value).strip().strip("/")


def _is_under(path: str, root: str) -> bool:
    if root == "/":
        return True
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def normalize_schedule(value: str) -> str:
    """支持标准 5 段 cron，也支持 "03:00" 这种每天一次的简写。"""
    value = str(value).strip()
    short = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if short:
        hour, minute = int(short.group(1)), int(short.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ConfigError(f"时间格式不正确：{value}（应为 HH:MM）")
        value = f"{minute} {hour} * * *"

    if len(value.split()) != 5:
        raise ConfigError(f"schedule 必须是 5 段 cron 或 HH:MM，当前值：{value!r}")
    if not croniter.is_valid(value):
        raise ConfigError(f"无效的 cron 表达式：{value!r}")
    return value


@dataclass
class WebDAVConfig:
    url: str
    user: str
    password: str
    remote_path: str
    vendor: str = "other"
    remote_name: str = "webdav"
    rclone_config: str = "/data/.rclone.conf"


@dataclass
class NotifyConfig:
    enabled: bool = True
    api_url: str = "https://push-server.o1-1.xct258.top/api/push"
    server_name: str = ""
    category: str = "service"
    topic: str = "backup/webdav"
    mode: str = "append"
    on_success_mqtt: bool = False
    on_failure_mqtt: bool = True
    timeout: int = 10


@dataclass
class EncryptionConfig:
    enabled: bool = False
    method: str = "age"  # age | gpg
    recipient: str = ""
    passphrase: str = ""
    passphrase_env: str = "BACKUP_GPG_PASSPHRASE"


@dataclass
class Defaults:
    compression: str = "zstd"
    zstd_level: int = 3
    keep_last: int = 7
    keep_days: int = 0
    exclude: list[str] = field(default_factory=list)
    xattrs: bool = False
    one_file_system: bool = False
    archive_root: str = "/"


@dataclass
class Job:
    name: str
    paths: list[str]
    enabled: bool = True
    compression: str = "zstd"
    zstd_level: int = 3
    keep_last: int = 7
    keep_days: int = 0
    exclude: list[str] = field(default_factory=list)
    xattrs: bool = False
    one_file_system: bool = False
    archive_root: str = "/"
    remote_path: str = ""
    pre_cmd: str = ""
    post_cmd: str = ""


@dataclass
class Config:
    config_file: str
    schedule: str
    timezone: str
    webdav: WebDAVConfig
    notify: NotifyConfig
    defaults: Defaults
    jobs: list[Job]
    encryption: EncryptionConfig = field(default_factory=EncryptionConfig)
    auto_discover: bool = False
    discover_root: str = "/sources"
    staging_dir: str = "/tmp/backup-staging"
    state_dir: str = "/data/state"
    lock_file: str = "/data/.backup.lock"
    run_missed: bool = True
    run_on_first_start: bool = True
    retry_count: int = 1
    retry_delay: int = 300
    bwlimit: str = ""
    min_free_gb: int = 5
    local_keep: int = 0
    dry_run: bool = False
    log_level: str = "INFO"
    verify_archive: bool = False
    hook_timeout: int = 300

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"时区不存在：{self.timezone}") from exc

    def job_remote_path(self, job: Job) -> str:
        if job.remote_path:
            return _strip_slashes(job.remote_path)
        base = _strip_slashes(self.webdav.remote_path)
        return f"{base}/{job.name}" if base else job.name

    def enabled_jobs(self) -> list[Job]:
        return [job for job in self.jobs if job.enabled]


def _parse_defaults(data: dict) -> Defaults:
    raw = data.get("defaults") or {}
    if not isinstance(raw, dict):
        raise ConfigError("defaults 必须是对象")

    compression = str(raw.get("compression", "zstd")).strip().lower()
    if compression not in {"zstd", "gzip", "none"}:
        raise ConfigError("defaults.compression 只能是 zstd / gzip / none")
    level = _as_int(raw.get("zstd_level"), 3, "defaults.zstd_level")
    if not (1 <= level <= 22):
        raise ConfigError("defaults.zstd_level 必须在 1-22 之间")

    keep_last = _as_int(raw.get("keep_last"), 7, "defaults.keep_last")
    keep_days = _as_int(raw.get("keep_days"), 0, "defaults.keep_days")
    if keep_last < 0 or keep_days < 0:
        raise ConfigError("keep_last / keep_days 不能为负数")

    exclude = raw.get("exclude") or []
    if not isinstance(exclude, list):
        raise ConfigError("defaults.exclude 必须是数组")
    exclude = [str(item) for item in exclude if str(item).strip()]

    archive_root = _norm_path(str(raw.get("archive_root", "/")))

    return Defaults(
        compression=compression,
        zstd_level=level,
        keep_last=keep_last,
        keep_days=keep_days,
        exclude=exclude,
        xattrs=_as_bool(raw.get("xattrs"), False),
        one_file_system=_as_bool(raw.get("one_file_system"), False),
        archive_root=archive_root,
    )


def _parse_webdav(data: dict | None) -> WebDAVConfig:
    if not isinstance(data, dict):
        raise ConfigError("缺少 webdav 配置段")

    url = str(data.get("url") or "").strip()
    user = str(data.get("user") or "").strip()
    password = str(data.get("password") or "")
    remote_path = str(data.get("remote_path") or "").strip()

    if not url:
        raise ConfigError("webdav.url 不能为空")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("webdav.url 必须以 http:// 或 https:// 开头")
    if not user:
        raise ConfigError("webdav.user 不能为空")
    if not password:
        raise ConfigError("webdav.password 不能为空")
    if not remote_path:
        raise ConfigError("webdav.remote_path 不能为空")

    remote_name = str(data.get("remote_name") or "webdav").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", remote_name):
        raise ConfigError("webdav.remote_name 只能包含字母、数字、下划线、点和横线")

    vendor = str(data.get("vendor") or "other").strip()

    return WebDAVConfig(
        url=url,
        user=user,
        password=password,
        remote_path=_strip_slashes(remote_path),
        vendor=vendor,
        remote_name=remote_name,
        rclone_config=_norm_path(str(data.get("rclone_config") or "/data/.rclone.conf")),
    )


def _parse_notify(data: dict | None, tz_name: str) -> NotifyConfig:
    raw = data or {}
    if not isinstance(raw, dict):
        raise ConfigError("notify 必须是对象")

    enabled = _as_bool(raw.get("enabled"), True)
    api_url = str(raw.get("api_url") or "https://push-server.o1-1.xct258.top/api/push").strip()
    server_name = str(raw.get("server_name") or socket.gethostname()).strip()
    topic = str(raw.get("topic") or "backup/webdav").strip()
    category = str(raw.get("category") or "service").strip()
    mode = str(raw.get("mode") or "append").strip().lower()
    if mode not in {"append", "overwrite"}:
        raise ConfigError("notify.mode 只能是 append / overwrite")
    if category not in {"service", "todo"}:
        raise ConfigError("notify.category 只能是 service / todo")

    timeout = _as_int(raw.get("timeout"), 10, "notify.timeout")
    if timeout <= 0:
        raise ConfigError("notify.timeout 必须大于 0")

    return NotifyConfig(
        enabled=enabled,
        api_url=api_url,
        server_name=server_name,
        category=category,
        topic=topic,
        mode=mode,
        on_success_mqtt=_as_bool(raw.get("on_success_mqtt"), False),
        on_failure_mqtt=_as_bool(raw.get("on_failure_mqtt"), True),
        timeout=timeout,
    )


def _parse_job(raw: dict, defaults: Defaults) -> Job:
    if not isinstance(raw, dict):
        raise ConfigError("jobs 里的每一项都必须是对象")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ConfigError("job.name 不能为空")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ConfigError(f"job.name 只能包含字母、数字、下划线、点和横线：{name}")

    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, list) or not paths_raw:
        raise ConfigError(f"job {name} 的 paths 不能为空")
    paths = [_norm_path(str(item)) for item in paths_raw if str(item).strip()]
    if not paths:
        raise ConfigError(f"job {name} 的 paths 不能为空")

    compression = str(raw.get("compression", defaults.compression)).strip().lower()
    if compression not in {"zstd", "gzip", "none"}:
        raise ConfigError(f"job {name} 的 compression 只能是 zstd / gzip / none")

    level = _as_int(raw.get("zstd_level"), defaults.zstd_level, f"job {name} 的 zstd_level")
    if not (1 <= level <= 22):
        raise ConfigError(f"job {name} 的 zstd_level 必须在 1-22 之间")

    keep_last = _as_int(raw.get("keep_last"), defaults.keep_last, f"job {name} 的 keep_last")
    keep_days = _as_int(raw.get("keep_days"), defaults.keep_days, f"job {name} 的 keep_days")
    if keep_last < 0 or keep_days < 0:
        raise ConfigError(f"job {name} 的 keep_last / keep_days 不能为负数")

    archive_root = _norm_path(str(raw.get("archive_root", defaults.archive_root)))
    for path in paths:
        if not _is_under(path, archive_root):
            raise ConfigError(
                f"job {name} 的路径 {path} 不在 archive_root {archive_root} 下"
            )

    exclude = list(defaults.exclude)
    job_exclude = raw.get("exclude") or []
    if not isinstance(job_exclude, list):
        raise ConfigError(f"job {name} 的 exclude 必须是数组")
    for item in job_exclude:
        item = str(item)
        if item.strip() and item not in exclude:
            exclude.append(item)

    remote_path = str(raw.get("remote_path") or "").strip()
    if remote_path:
        remote_path = _strip_slashes(remote_path)

    return Job(
        name=name,
        paths=paths,
        enabled=_as_bool(raw.get("enabled"), True),
        compression=compression,
        zstd_level=level,
        keep_last=keep_last,
        keep_days=keep_days,
        exclude=exclude,
        xattrs=_as_bool(raw.get("xattrs"), defaults.xattrs),
        one_file_system=_as_bool(raw.get("one_file_system"), defaults.one_file_system),
        archive_root=archive_root,
        remote_path=remote_path,
        pre_cmd=str(raw.get("pre_cmd") or ""),
        post_cmd=str(raw.get("post_cmd") or ""),
    )


def _discover_jobs(
    root: str,
    defaults: Defaults,
    manual_jobs: list[Job],
) -> list[Job]:
    """把 discover_root 下的每个一级子目录自动变成一个任务。

    同名的手动任务优先，可以用于禁用/覆盖某个自动发现的任务。
    """
    if not os.path.isdir(root):
        raise ConfigError(
            f"auto_discover=true，但 discover_root 不存在或不是目录：{root}"
        )

    manual_names = {job.name for job in manual_jobs}
    discovered: list[Job] = []

    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError as exc:
        raise ConfigError(f"扫描 discover_root 失败：{root}（{exc}）") from exc

    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        name = entry.name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ConfigError(
                f"自动发现的任务名不合法：{name!r}；"
                "只允许字母、数字、下划线、点和横线，请改名或改用手动 jobs"
            )
        if name in manual_names:
            # 手动任务同名时优先，可以用于 enabled: false 或自定义排除
            continue
        discovered.append(
            Job(
                name=name,
                paths=[os.path.normpath(entry.path)],
                enabled=True,
                compression=defaults.compression,
                zstd_level=defaults.zstd_level,
                keep_last=defaults.keep_last,
                keep_days=defaults.keep_days,
                exclude=list(defaults.exclude),
                xattrs=defaults.xattrs,
                one_file_system=defaults.one_file_system,
                archive_root=root,
            )
        )

    if not discovered and not manual_jobs:
        raise ConfigError(
            f"auto_discover=true，但 {root} 下没有可备份的子目录；"
            f"请在 compose 里把宿主机目录挂载到 {root}/<名字>，例如：- /etc:{root}/etc:ro"
        )

    return manual_jobs + discovered


def _parse_encryption(data: dict | None) -> EncryptionConfig:
    raw = data or {}
    if not isinstance(raw, dict):
        raise ConfigError("encryption 必须是对象")

    enabled = _env_bool("ENCRYPTION_ENABLED", _as_bool(raw.get("enabled"), False))
    method = (
        _env("ENCRYPTION_METHOD", str(raw.get("method") or "age"))
        or "age"
    ).strip().lower()
    if method not in {"age", "gpg"}:
        raise ConfigError("encryption.method 只能是 age / gpg")

    recipient = _env("AGE_RECIPIENT", str(raw.get("recipient") or "")) or ""
    recipient = recipient.strip()
    passphrase = _env("ENCRYPTION_PASSPHRASE", str(raw.get("passphrase") or "")) or ""
    passphrase_env = (
        _env(
            "ENCRYPTION_PASSPHRASE_ENV",
            str(raw.get("passphrase_env") or "BACKUP_GPG_PASSPHRASE"),
        )
        or "BACKUP_GPG_PASSPHRASE"
    ).strip()

    # 这里只做结构校验；recipient / passphrase 在 validate 和真正加密时检查，
    # 这样 list 等只读命令不需要提供加密密钥也能用。
    return EncryptionConfig(
        enabled=enabled,
        method=method,
        recipient=recipient,
        passphrase=passphrase,
        passphrase_env=passphrase_env,
    )


def load_config(path: str | None = None) -> Config:
    config_file = path or _env("CONFIG_FILE", "/config/config.yml")
    assert config_file is not None

    if not os.path.isfile(config_file):
        raise ConfigError(f"配置文件不存在：{config_file}")

    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败：{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是对象")

    defaults = _parse_defaults(data)

    webdav_raw = data.get("webdav")
    if isinstance(webdav_raw, dict):
        # 支持在密码/地址里用 ${ENV_VAR} 引用环境变量
        webdav_raw = {
            key: expand_env(value) if isinstance(value, str) else value
            for key, value in webdav_raw.items()
        }
    webdav = _parse_webdav(webdav_raw)

    # 环境变量覆盖，方便用 compose 注入密钥
    webdav.url = _env("WEBDAV_URL", webdav.url) or webdav.url
    webdav.user = _env("WEBDAV_USER", webdav.user) or webdav.user
    webdav.password = _env("WEBDAV_PASSWORD", webdav.password) or webdav.password
    webdav.remote_path = _strip_slashes(_env("WEBDAV_REMOTE_PATH", webdav.remote_path) or "")
    webdav.vendor = _env("WEBDAV_VENDOR", webdav.vendor) or webdav.vendor
    webdav.remote_name = _env("RCLONE_REMOTE", webdav.remote_name) or webdav.remote_name
    webdav.rclone_config = _norm_path(
        _env("RCLONE_CONFIG", webdav.rclone_config) or "/data/.rclone.conf"
    )

    # 环境变量覆盖后再校验一次，避免把明显错误带进运行期
    if not webdav.url.startswith(("http://", "https://")):
        raise ConfigError("WEBDAV_URL / webdav.url 必须以 http:// 或 https:// 开头")
    if not webdav.user:
        raise ConfigError("WEBDAV_USER / webdav.user 不能为空")
    if not webdav.password:
        raise ConfigError("WEBDAV_PASSWORD / webdav.password 不能为空")
    if not webdav.remote_path:
        raise ConfigError("WEBDAV_REMOTE_PATH / webdav.remote_path 不能为空")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", webdav.remote_name):
        raise ConfigError("RCLONE_REMOTE / webdav.remote_name 含有非法字符")

    # 优先级：TIMEZONE 环境变量 > 配置里的 timezone > 系统 TZ
    tz_name = (
        _env("TIMEZONE")
        or str(data.get("timezone") or "").strip()
        or _env("TZ")
        or "Asia/Shanghai"
    )
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"时区不存在：{tz_name}") from exc

    schedule = normalize_schedule(
        _env("SCHEDULE", str(data.get("schedule") or "0 3 * * *")) or "0 3 * * *"
    )

    notify = _parse_notify(data.get("notify"), tz_name)
    notify.api_url = _env("PUSH_API_URL", notify.api_url) or notify.api_url
    notify.server_name = _env("PUSH_SERVER_NAME", notify.server_name) or notify.server_name
    notify.topic = _env("PUSH_TOPIC", notify.topic) or notify.topic
    notify.enabled = _env_bool("PUSH_ENABLED", notify.enabled)
    notify.on_success_mqtt = _env_bool("PUSH_ON_SUCCESS", notify.on_success_mqtt)
    notify.on_failure_mqtt = _env_bool("PUSH_ON_FAILURE", notify.on_failure_mqtt)
    encryption = _parse_encryption(data.get("encryption"))
    if notify.enabled and not notify.api_url:
        raise ConfigError("notify.enabled=true 时 notify.api_url 不能为空")
    if notify.enabled and not notify.server_name:
        raise ConfigError("notify.enabled=true 时 notify.server_name 不能为空")

    auto_discover = _env_bool(
        "AUTO_DISCOVER", _as_bool(data.get("auto_discover"), False)
    )
    discover_root = _norm_path(
        _env("DISCOVER_ROOT", str(data.get("discover_root") or "/sources"))
        or "/sources"
    )

    jobs_raw = data.get("jobs")
    if jobs_raw is None:
        jobs_raw = []
    if not isinstance(jobs_raw, list):
        raise ConfigError("jobs 必须是数组")
    jobs = [_parse_job(item, defaults) for item in jobs_raw]
    names = [job.name for job in jobs]
    if len(names) != len(set(names)):
        raise ConfigError("job.name 不能重复")

    if auto_discover:
        jobs = _discover_jobs(discover_root, defaults, jobs)

    if not jobs:
        raise ConfigError("jobs 不能为空（或设置 auto_discover: true）")

    # 固定使用容器内部临时目录：避免归档出现在被备份的宿主机挂载目录中，
    # 也避免 tar 读到正在生成的归档文件。
    staging_dir = "/tmp/backup-staging"
    state_dir = _norm_path(_env("STATE_DIR", "/data/state") or "/data/state")
    lock_file = _norm_path(_env("LOCK_FILE", "/data/.backup.lock") or "/data/.backup.lock")

    config = Config(
        config_file=config_file,
        schedule=schedule,
        timezone=tz_name,
        webdav=webdav,
        notify=notify,
        defaults=defaults,
        jobs=jobs,
        encryption=encryption,
        auto_discover=auto_discover,
        discover_root=discover_root,
        staging_dir=staging_dir,
        state_dir=state_dir,
        lock_file=lock_file,
        run_missed=_env_bool("RUN_MISSED", True),
        run_on_first_start=_env_bool("RUN_ON_FIRST_START", True),
        retry_count=max(0, _env_int("RETRY_COUNT", 1)),
        retry_delay=max(0, _env_int("RETRY_DELAY", 300)),
        bwlimit=_env("BWLIMIT", "") or "",
        min_free_gb=max(0, _env_int("MIN_FREE_GB", 5)),
        local_keep=max(0, _env_int("LOCAL_KEEP", 0)),
        dry_run=_env_bool("DRY_RUN", False),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        verify_archive=_env_bool("VERIFY_ARCHIVE", False),
        hook_timeout=max(1, _env_int("HOOK_TIMEOUT", 300)),
    )

    if config.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("LOG_LEVEL 只能是 DEBUG / INFO / WARNING / ERROR / CRITICAL")

    return config


def describe(config: Config) -> str:
    lines = [
        f"配置文件：{config.config_file}",
        f"调度：{config.schedule}（{config.timezone}）",
        f"WebDAV：{config.webdav.url}",
        f"WebDAV 用户：{config.webdav.user}",
        f"WebDAV 远端根：{config.webdav.remote_path}",
        f"推送：{'启用' if config.notify.enabled else '关闭'} {config.notify.api_url}",
        f"staging：{config.staging_dir}（固定）",
        f"state：{config.state_dir}",
        f"加密：{'开启（' + config.encryption.method + '）' if config.encryption.enabled else '关闭'}",
        f"自动发现：{'开启' if config.auto_discover else '关闭'}"
        + (f"（{config.discover_root}）" if config.auto_discover else ""),
        "任务：",
    ]
    for job in config.jobs:
        status = "启用" if job.enabled else "禁用"
        lines.append(
            f"  - {job.name} [{status}] 路径={', '.join(job.paths)} "
            f"压缩={job.compression} 保留={job.keep_last} 份"
        )
    return "\n".join(lines)
