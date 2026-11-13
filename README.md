# daily-webdav-backup

基于 [xct258/docker-image-template](https://github.com/xct258/docker-image-template) 的定时备份容器：

- 按 cron 定时把配置的路径打包成 `tar.zst`
- 通过 rclone 上传到 WebDAV
- WebDAV 地址、用户名、密码、远端路径全部写在 `config/config.yml`
- 打包前/后可执行自定义命令
- 上传失败自动重试；容器重启后继续重传未完成的归档
- 大文件被服务端/代理拒绝（413）时自动分段上传，避免备份失败
- 远端按份数/天数保留历史备份
- 通过 MQTT 推送中心发送 Markdown 通知
- 可选 age / GPG 归档加密
- 可选磁盘空间监控，低于阈值推送
- 提供手动执行、配置校验、连接测试、远端列表、healthcheck

---

## 目录结构

```text
.
├── app/                      # Python 备份程序
│   ├── main.py               # 命令行入口
│   ├── config.py             # 配置解析/校验
│   ├── scheduler.py          # cron 调度、补跑
│   ├── runner.py             # 单次备份执行
│   ├── archive.py            # tar + zstd 打包
│   ├── rclone.py             # rclone 封装
│   ├── retention.py          # 远端/本地保留策略
│   ├── state.py              # 状态、锁、心跳
│   ├── notify.py             # MQTT 推送中心
│   └── process.py            # 子进程管理
├── config/
│   ├── config.example.yml    # 完整配置示例
│   └── config.auto.yml       # 首次启动自动生成的占位配置
├── install/
│   ├── app.conf              # 镜像名、依赖
│   ├── install.sh            # 模板自带
│   └── custom-install.sh     # 安装 rclone + app 代码
├── startup/
│   ├── prepare.sh            # 启动校验
│   ├── once.sh
│   ├── services.sh           # 启动调度器
│   ├── update.sh             # 运行期自动更新（默认空操作）
│   └── main.sh               # 模板固定
├── template/                 # 模板固定文件
├── docker-compose.yml
└── check.sh
```

---

## 快速开始

### 1. 构建镜像

1. 修改 `install/app.conf` 里的 `IMAGE_REPO`，例如：
   ```bash
   IMAGE_REPO=你的账号/daily-webdav-backup
   ```
   同时把 `docker-compose.yml` 里的 `image` 改成同一个镜像名。
2. 在 GitHub 仓库的 `Settings → Secrets and variables → Actions` 添加：
   - `DOCKERACC`：Docker Hub 账号
   - `DOCKERPWD`：Docker Hub 密码或 Access Token
3. Actions 页面手动触发 `Build and Push Docker Image`。

> 模板构建时通过 raw 地址下载仓库文件，仓库需要是公开的。

### 2. 准备配置

```bash
cp config/config.example.yml config/config.yml
```

如果跳过这一步，容器首次启动会自动从 `config.auto.yml` 生成一个占位 `config/config.yml`：

- 占位任务默认 `enabled: false`，不会真的备份
- 推送默认关闭，避免误发
- 修改 WebDAV 和 `jobs` 后执行 `docker compose restart`
- 自动生成要求 `./config` 挂载为可写（默认 compose 已改成 `./config:/config`）

先在 `config/config.yml` 顶部写一次服务器名，`remote_path` 和推送都会复用它：

```yaml
server_name: "my-server"        # 推送和远端目录共用
webdav:
  url: "https://dav.example.com/dav/"
  user: "backup"
  password: "你的明文密码"
  vendor: "other"
  remote_path: "/backups/nas"   # 实际为 /backups/nas/my-server
```

密码可以直接写明文，程序启动时会自动执行 `rclone obscure`，生成 `/data/.rclone.conf`（权限 600）。
也可以用环境变量避免明文落盘：

```yaml
webdav:
  password: "${WEBDAV_PASSWORD}"
```

或在 compose 里设置 `WEBDAV_PASSWORD`。

### 3. 配置要备份的目录

Docker 必须先知道“宿主机路径挂到容器哪里”，所以 compose 里的 volume 映射是省不掉的。
但推荐开启**自动发现**，这样 `config.yml` 里就不用再写一遍容器路径。

修改 `docker-compose.yml` 的 `volumes`：

```yaml
    volumes:
      - ./config:/config
      - ./data:/data
      - /etc:/sources/etc:ro
      - /opt/appdata:/sources/appdata:ro
      - /var/www:/sources/www:ro
```

然后在 `config.yml` 里只写：

```yaml
auto_discover: true
discover_root: "/sources"
# jobs 可以省略
```

程序会自动把 `/sources` 下的每个一级子目录变成一个任务：

```text
/sources/etc      -> 任务 etc
/sources/appdata  -> 任务 appdata
/sources/www      -> 任务 www
```

每个任务生成一个归档，上传到：

```text
<remote_path>/<server_name>/<时间>/<任务名>/
```

如果某个目录需要单独的排除规则、保留策略、`pre_cmd` 等，再写手动 job。
同名的手动 job 会覆盖自动发现的任务，也可以用来禁用某个目录。

手动模式示例（`auto_discover: false`）：

```yaml
jobs:
  - name: "etc"
    paths:
      - "/sources/etc"
    archive_root: "/sources"
```

### 4. 启动

```bash
docker compose up -d
docker compose logs -f          # 启动摘要
tail -f data/logs/service/scheduler.log   # 备份执行日志
```

首次启动如果 `RUN_ON_FIRST_START=true`（默认），会立即执行一次备份。

---

## 配置说明

### 调度

```yaml
schedule: "0 3 * * *"       # 每天 03:00
timezone: "Asia/Shanghai"
```

也支持：

```yaml
schedule: "03:00"
```

`RUN_MISSED=true` 时，容器启动会检查是否错过计划点，错过则补跑一次；`RUN_ON_FIRST_START=true` 时第一次启动会立即跑一次。

### WebDAV

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | WebDAV 地址 |
| `user` | 是 | 用户名 |
| `password` | 是 | 明文密码，程序自动 obscure |
| `vendor` | 否 | 默认 `other`；Nextcloud 用 `nextcloud` |
| `remote_path` | 是 | 远端基础路径；程序自动追加 `/<server_name>`，字符串或列表 |
| `append_server_name` | 否 | 是否追加 server_name 子目录，默认 `true` |
| `date_format` | 否 | 时间目录格式，默认 `%Y-%m-%d`；空字符串或 `none` 关闭 |
| `remote_name` | 否 | rclone remote 名，默认 `webdav` |
| `rclone_config` | 否 | 默认 `/data/.rclone.conf` |
| `split_upload` | 否 | `auto`（默认，被拒时自动分段）/ `always` / `never` |
| `split_size_mb` | 否 | 每段大小，默认 `90` MB |

### 大文件分段上传

有些服务端或前面的反向代理会限制单次请求体大小，例如 Cloudflare 免费版上限 100MB，
超过就直接返回 **413 Payload Too Large**，整个备份任务会失败。

`split_upload` 默认是 `auto`：整包上传遇到 413 时，自动把归档切成
`split_size_mb`（默认 90MB）大小的段，逐段上传，备份继续完成。

```yaml
webdav:
  # ...
  split_upload: "auto"    # auto / always / never
  split_size_mb: 90
```

- `auto`：只有整包上传被尺寸限制拒绝时才分段，正常情况仍是单文件
- `always`：归档超过 `split_size_mb` 就始终分段，适合已知有上限的服务
- `never`：关闭分段，失败即报错

分段命名固定为 `<归档名>.part001`、`.part002`……，字典序即原始顺序；
`.sha256` 仍然校验拼接后的完整归档。分段上传支持跳过远端已存在且大小一致的段，
中断后重试不会重复传输。

**恢复分段的备份**：

```bash
rclone copy "webdav:/path/to/docker" /tmp/restore
cd /tmp/restore
cat docker-*.tar.zst.gpg.part* > docker-20261107-175000.tar.zst.gpg
sha256sum -c docker-20261107-175000.tar.zst.gpg.sha256
gpg -d docker-20261107-175000.tar.zst.gpg | zstd -d | tar -xf - -C /
```

保留策略按“归档”计数，一个归档的所有分段和 `.sha256` 会作为一组一起清理。

### 多目标

同一个 WebDAV 账号，可以配置多个远端目录：

```yaml
webdav:
  url: "https://dav.example.com/dav/"
  user: "backup"
  password: "change-me"
  remote_path:
    - path: "/backups/nas"
      interval: "1d"
      keep_last: 7
      keep_days: 0
    - path: "/backups/nas2"
      interval: "7d"
      keep_last: 4
      keep_days: 0
```

- `interval`：该目标的上传间隔，支持 `30m` / `12h` / `1d` / `7d` / `30d`；`0` 或 `always` 表示每次调度都上传
- `keep_last` / `keep_days`：该目标自己的保留策略；不写则继承 job / defaults
- 每次执行时，只上传到已经到间隔的目标
- 没有目标到期时：不创建归档、不上传、**不检查源路径和 staging 空间**
- 多个目标都到期时，同一份归档上传到全部目标
- 某个目标失败时，其他目标继续上传；失败目标进入 pending，下次重试
- 每个目标独立执行保留清理

最终目录结构：

```text
<target.path>/<date_format>/<job.name>/<archive>
```

例如：

```text
backups/nas/2026-11-07/docker/docker-20261107-175000.tar.zst.age
backups/nas2/2026-11-07/docker/docker-20261107-175000.tar.zst.age
```

保留策略会递归扫描任务目录，因此可以跨多个时间目录执行 `keep_last` / `keep_days`。

> `webdav.remote_path` 配置成多个目标时，不支持 `job.remote_path` 覆盖。

### 自动发现

```yaml
auto_discover: true
discover_root: "/sources"
```

规则：

- 只扫描 `discover_root` 的**一级子目录**
- 每个子目录自动生成一个任务，任务名 = 目录名
- 任务路径 = 该子目录，`archive_root` = `discover_root`
- 继承 `defaults` 里的压缩、保留、排除规则
- 目录名只能包含字母、数字、下划线、点、横线
- 同名的手动 job 优先，可以用来单独配置或禁用某个目录
- `discover_root` 不存在、没有子目录时会直接报配置错误

优点：宿主机路径只需要在 `docker-compose.yml` 里写一次。

限制：自动发现的任务都用 `defaults`，不能单独写 `pre_cmd` / `post_cmd`。
需要这些能力时，把手动 job 写成同名即可覆盖。

### 任务

```yaml
jobs:
  - name: "appdata"
    enabled: true
    paths:
      - "/sources/appdata"
      - "/sources/www"
    archive_root: "/sources"
    compression: "zstd"       # zstd / gzip / none
    zstd_level: 3
    keep_last: 7
    keep_days: 0
    # 默认不排除；需要时改成列表：
    # exclude:
    #   - "**/.cache/**"
    #   - "**/node_modules/**"
    #   - "**/*.tmp"
    pre_cmd: ""
    post_cmd: ""
```

- `paths` 必须是容器内绝对路径
- 当 `archive_root: "/"` 时，会自动排除 `/proc`、`/sys`、`/dev`、`/run`、`/tmp`
- `archive_root` 决定归档内的路径：
  - 默认 `/`：`/sources/etc` 在归档里是 `sources/etc`
  - 设为 `/sources`：归档里是 `etc`
- `pre_cmd` 在打包前执行，可用 `$HOOK_DIR`
- `post_cmd` 无论成功失败都会执行
- hook 里可以使用环境变量 `JOB_NAME`、`PHASE`、`ARCHIVE_PATH`、`HOOK_DIR`
- hook 在容器里执行，所需命令需要装进镜像

### 保留策略

- `keep_last: 7`：每个任务/目标远端保留最近 7 份
- `keep_last: 0`：不按数量清理
- 多目标时每个目标独立执行保留清理
- `keep_days: 30`：同时删除 30 天前的归档
- 只处理文件名匹配 `<job>-YYYYmmdd-HHMMSS.tar.zst` 的文件，不会动其他文件
- 上传成功后才清理
- 本地 staging 默认上传成功后删除；失败的归档保留，下次重传

### 加密

默认使用 **GPG 对称加密**。加密发生在 tar 打包之后、上传之前：

```text
etc-20261107-030000.tar.zst.gpg
etc-20261107-030000.tar.zst.gpg.sha256
```

`.sha256` 校验的是加密后的文件。

#### GPG（默认）

```yaml
encryption:
  enabled: true
  method: "gpg"
  passphrase: "你的加密密码"
  # passphrase_env: "BACKUP_GPG_PASSPHRASE"
```

- 使用 AES256-OCB（AEAD）
- 不使用 gpg-agent 缓存密码
- 解密需要 GnuPG >= 2.2.21

恢复：

```bash
gpg -d etc-20261107-030000.tar.zst.gpg > etc-20261107-030000.tar.zst
tar -tf etc-20261107-030000.tar.zst
```

#### age（可选）

```yaml
encryption:
  enabled: true
  method: "age"
  recipient: "age1..."
```

生成密钥：

```bash
age-keygen -o age.key
```

恢复：

```bash
sha256sum -c etc-20261107-030000.tar.zst.age.sha256
age -d -i age.key -o etc-20261107-030000.tar.zst \
  etc-20261107-030000.tar.zst.age
```

注意：

- age 私钥或 GPG 密码丢失后，备份无法恢复
- 加密只保护归档内容，不保护 WebDAV 账号密码
- 修改加密配置后，历史归档的扩展名和加密方式不会改变

### 磁盘空间监控

默认只监控容器根文件系统 `/`。需要监控数据盘时单独加路径：

```yaml
monitor:
  enabled: true
  interval: "5m"
  min_free_gb: 10
  min_free_percent: 10
  cooldown_minutes: 360
  notify_recovery: true

  paths:
    - name: "root"
      path: "/"
    # - name: "data"
    #   path: "/data"
    #   min_free_gb: 20
```

规则：

- 不写 `paths` 时只检查 `/`
- 路径不存在时跳过
- 多个路径属于同一文件系统时按 `st_dev` 去重
- `free_gb < min_free_gb` 或 `free_percent < min_free_percent` 时告警
- 首次低于阈值推送一次
- 之后每 `cooldown_minutes`（默认 360 分钟 = 6 小时）如果仍然不足，再推送一次
- 空间恢复后推送一次 `ok`
- 状态保存在 `/data/state/monitor.json`
- 监控线程独立运行，不阻塞备份

推送示例：

```text
标题：磁盘空间不足：root，可用 8.4 GB / 总量 100 GB（剩余 8.4%）
路径：/
可用：8.4 GB
已用：91.6 GB（91.6%）
总量：100 GB
剩余比例：8.4%
阈值：10 GB 或 10%
```

> 容器根 `/` 通常是 Docker data-root 所在文件系统。  
> `/data`、`/sources/*` 可能是其它宿主机磁盘，需要时在 `monitor.paths` 中单独添加。

### 推送

```yaml
notify:
  enabled: true
  api_url: "https://push-server.o1-1.xct258.top/api/push"
  server_name: ""        # 留空使用顶层 server_name
  category: "service"
  topic: "backup/webdav"
  mode: "append"
  on_success_mqtt: false
  on_failure_mqtt: true
```

对应 `POST /api/push` 字段：

| 字段 | 取值 |
|---|---|
| `server_name` | 顶层 `server_name`（`notify.server_name` 留空时）|
| `category` | `service` / `todo` |
| `topic` | 配置里的 `topic` |
| `title` | 例如 `备份失败：nas-backup` |
| `message` | Markdown 汇总 |
| `status` | 成功 `ok`、部分失败 `warn`、失败 `error`、跳过 `info` |
| `push_to_mqtt` | 成功看 `on_success_mqtt`，失败看 `on_failure_mqtt` |
| `msg_type` | 固定 `markdown` |
| `mode` | `append` / `overwrite` |

推送失败只写日志，不影响备份。

---

## 固定路径

compose 不再提供 `environment` 配置。以下路径在程序中固定：

| 用途 | 路径 |
|---|---|
| 配置文件 | `/config/config.yml` |
| 日志 | `/data/logs` |
| 状态 | `/data/state` |
| 临时归档 | `/tmp/backup-staging` |
| 锁文件 | `/data/.backup.lock` |
| rclone 配置 | `/data/.rclone.conf` |

备份参数全部在 `config/config.yml` 中配置。

`STARTUP_RETRY` / `STARTUP_RETRY_DELAY` 由模板启动脚本使用，默认 `0` / `5`，一般不需要设置。

> 临时归档固定使用容器内部的 `/tmp/backup-staging`，不能通过环境变量修改，也不要挂载它。  
> 打包和上传都在容器内完成，上传成功后自动删除。容器重建后 `/tmp` 会清空，未完成的上传会丢失；`docker restart` 不受影响。

---

## 手动操作

```bash
# 立即执行全部任务
docker exec daily-backup python3 /opt/backup/main.py run

# 只执行某个任务
docker exec daily-backup python3 /opt/backup/main.py run --job etc

# 只检查配置和路径
docker exec daily-backup python3 /opt/backup/main.py validate

# 测试 WebDAV
docker exec daily-backup python3 /opt/backup/main.py test-connection

# 列出远端归档
docker exec daily-backup python3 /opt/backup/main.py list

# 只打包不上传
docker exec daily-backup python3 /opt/backup/main.py run --no-upload

# 不发送通知
docker exec daily-backup python3 /opt/backup/main.py run --no-notify
```

日志：

```bash
tail -f data/logs/service/scheduler.log
```

运行状态：

```bash
cat data/state/state.json
```

---

## 恢复

```bash
# 1. 下载归档和校验文件
rclone copy webdav:backups/nas/etc/etc-20261107-030001.tar.zst /tmp/
rclone copy webdav:backups/nas/etc/etc-20261107-030001.tar.zst.sha256 /tmp/

# 2. 校验
cd /tmp
sha256sum -c etc-20261107-030001.tar.zst.sha256

# 3. 查看内容
tar -tf etc-20261107-030001.tar.zst | head

# 4. 解压（按 archive_root 决定落点）
tar -xf etc-20261107-030001.tar.zst -C /restore
```

如果 `archive_root: /sources`，归档内是 `etc/...`；如果默认 `/`，归档内是 `sources/etc/...`。

---

## 构建与自查

```bash
# 模板静态检查
bash check.sh

# 单元测试（需要 PyYAML + croniter）
python3 -m unittest discover -s tests -v
```

### 本地 / 测试服务器构建（仅测试）

```bash
# 构建测试标签
IMAGE=xct258/daily-webdav-backup:test bash scripts/build.sh

# 构建 :latest（仅本机）
bash scripts/build.sh

# 不使用缓存
NO_CACHE=1 bash scripts/build.sh
```

`template/Dockerfile` 会从 GitHub raw 下载 `app/`、`startup/` 等文件，
所以构建前需要先 commit + push。未提交的改动不会进入镜像。

本地构建只用于测试和快速验证，不推送到 Docker Hub。

### GitHub Actions（生产发布）

生产环境使用 GitHub Actions 构建并推送 Docker Hub：

1. Actions 页面手动触发 `Build and Push Docker Image`
2. 构建多架构镜像
3. 推送 `xct258/daily-webdav-backup:latest`
4. 生产机器执行 `docker compose pull && docker compose up -d`

测试迭代用本地构建，测试完成后再触发一次 Actions 发布生产镜像。

---

## 注意事项

- 容器以 root 运行，才能读取任意挂载路径；源目录建议全部 `:ro`
- 首次启动没有 `config/config.yml` 时会自动生成占位配置；占位任务禁用、推送关闭，修改后重启即可
- 如果 `./config` 是只读挂载，自动生成会失败并停止启动，此时先在宿主机创建配置文件
- `config.yml` 里有 WebDAV 密码，宿主机上建议 `chmod 600`
- 归档使用 tar，不做文件系统快照
- 每天全量备份会占用 `保留份数 × 单份大小` 的远端空间；数据量很大时建议改用 restic
- WebDAV 服务器需要支持大文件上传；部分服务对单文件大小有限制
- 首次启动如果 WebDAV 不可达，容器仍会启动，任务会按重试策略继续尝试
- 容器停止时会终止正在执行的 tar/rclone；已完成的归档保留在 `/tmp/backup-staging`，下次启动继续重传（容器重建会丢失）
