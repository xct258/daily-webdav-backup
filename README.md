# daily-webdav-backup

基于 [xct258/docker-image-template](https://github.com/xct258/docker-image-template) 的定时备份容器：

- 按 cron 定时把配置的路径打包成 `tar.zst`
- 通过 rclone 上传到 WebDAV
- WebDAV 地址、用户名、密码、远端路径全部写在 `config/config.yml`
- 打包前/后可执行自定义命令（适合数据库 dump）
- 上传失败自动重试；容器重启后继续重传未完成的归档
- 远端按份数/天数保留历史备份
- 通过 MQTT 推送中心发送 Markdown 通知
- 可选 age / GPG 归档加密
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

修改 `config/config.yml` 的 `webdav`：

```yaml
webdav:
  url: "https://dav.example.com/dav/"
  user: "backup"
  password: "你的明文密码"
  vendor: "other"
  remote_path: "/backups/nas"
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
<remote_path>/<任务名>/
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
| `remote_path` | 是 | 远端根路径（相对 WebDAV URL），例如 `/backups/nas` |
| `remote_name` | 否 | rclone remote 名，默认 `webdav` |
| `rclone_config` | 否 | 默认 `/data/.rclone.conf` |

每个任务默认上传到：

```text
<remote_name>:<remote_path>/<job.name>/
```

也可以在 job 里用 `remote_path` 单独覆盖。

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
    exclude:
      - "**/cache/**"
    pre_cmd: ""
    post_cmd: ""
```

- `paths` 必须是容器内绝对路径
- 当 `archive_root: "/"` 时，会自动排除 `/proc`、`/sys`、`/dev`、`/run`、`/tmp`
- `archive_root` 决定归档内的路径：
  - 默认 `/`：`/sources/etc` 在归档里是 `sources/etc`
  - 设为 `/sources`：归档里是 `etc`
- `pre_cmd` 在打包前执行，适合数据库 dump
- `post_cmd` 无论成功失败都会执行
- hook 里可以使用环境变量 `JOB_NAME`、`PHASE`、`ARCHIVE_PATH`、`HOOK_DIR`

数据库示例：

```yaml
  - name: "database"
    paths:
      - "/tmp/backup-hooks/database"
    archive_root: "/tmp/backup-hooks"
    pre_cmd: |
      mkdir -p "$HOOK_DIR"
      pg_dump -U app app > "$HOOK_DIR/app.sql"
    post_cmd: |
      rm -f "$HOOK_DIR/app.sql"
```

注意：hook 在容器里执行，`pg_dump` 等命令需要装进镜像。

完整示例（PostgreSQL / MySQL / SQLite / Redis / MongoDB、恢复命令、注意事项）见：
[docs/database-backup.md](docs/database-backup.md)。

### 保留策略

- `keep_last: 7`：每个任务远端保留最近 7 份
- `keep_last: 0`：不按数量清理
- `keep_days: 30`：同时删除 30 天前的归档
- 只处理文件名匹配 `<job>-YYYYmmdd-HHMMSS.tar.zst` 的文件，不会动其他文件
- 上传成功后才清理
- 本地 staging 默认上传成功后删除；失败的归档保留，下次重传

### 加密

加密发生在 tar 打包之后、上传之前。上传到 WebDAV 的是密文：

```text
etc-20261107-030000.tar.zst.age
etc-20261107-030000.tar.zst.age.sha256
```

`.sha256` 校验的是**加密后的文件**。

#### age（推荐）

镜像内置 `age`。在宿主机或离线环境生成密钥：

```bash
age-keygen -o age.key
```

会输出公钥，类似：

```text
Public key: age1...
```

把公钥写进配置，恢复时使用 `age.key` 私钥：

```yaml
encryption:
  enabled: true
  method: "age"
  recipient: "age1..."
```

恢复：

```bash
sha256sum -c etc-20261107-030000.tar.zst.age.sha256
age -d -i age.key -o etc-20261107-030000.tar.zst \
  etc-20261107-030000.tar.zst.age
tar -tf etc-20261107-030000.tar.zst
```

#### GPG 对称加密

```yaml
encryption:
  enabled: true
  method: "gpg"
  passphrase_env: "BACKUP_GPG_PASSPHRASE"
```

compose：

```yaml
environment:
  BACKUP_GPG_PASSPHRASE: "你的加密密码"
```

也可以直接写 `passphrase`，但不推荐。

恢复：

```bash
gpg -d etc-20261107-030000.tar.zst.gpg > etc-20261107-030000.tar.zst
```

注意：

- age 私钥或 GPG 密码丢失后，备份无法恢复
- 加密只保护归档内容，不保护 WebDAV 账号密码
- 修改加密配置后，历史归档的扩展名和加密方式不会改变

### 推送

```yaml
notify:
  enabled: true
  api_url: "https://push-server.o1-1.xct258.top/api/push"
  server_name: "nas-backup"
  category: "service"
  topic: "backup/webdav"
  mode: "append"
  on_success_mqtt: false
  on_failure_mqtt: true
```

对应 `POST /api/push` 字段：

| 字段 | 取值 |
|---|---|
| `server_name` | 配置里的 `server_name` |
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

## 环境变量

配置文件里的值都可以用环境变量覆盖：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CONFIG_FILE` | `/config/config.yml` | 配置文件 |
| `TZ` / `TIMEZONE` | 配置里的 `timezone` | 时区 |
| `SCHEDULE` | 配置里的 `schedule` | cron |
| `AUTO_DISCOVER` | 配置里的 `auto_discover` | 是否自动发现 `/sources` 子目录 |
| `DISCOVER_ROOT` | `/sources` | 自动发现的根目录 |
| `WEBDAV_URL` | 配置值 | WebDAV 地址 |
| `WEBDAV_USER` | 配置值 | 用户名 |
| `WEBDAV_PASSWORD` | 配置值 | 密码 |
| `WEBDAV_REMOTE_PATH` | 配置值 | 远端根路径 |
| `RCLONE_REMOTE` | `webdav` | rclone remote 名 |
| `RCLONE_CONFIG` | `/data/.rclone.conf` | rclone 配置路径 |
| `ENCRYPTION_ENABLED` | 配置值 | 是否加密归档 |
| `ENCRYPTION_METHOD` | `age` | `age` / `gpg` |
| `AGE_RECIPIENT` | 配置值 | age 公钥 |
| `ENCRYPTION_PASSPHRASE` | 空 | gpg 对称密码 |
| `ENCRYPTION_PASSPHRASE_ENV` | `BACKUP_GPG_PASSPHRASE` | gpg 密码所在环境变量名 |
| `STATE_DIR` | `/data/state` | 状态目录 |
| `RUN_MISSED` | `true` | 启动补跑 |
| `RUN_ON_FIRST_START` | `true` | 第一次启动立即跑 |
| `RETRY_COUNT` | `1` | 单任务重试次数 |
| `RETRY_DELAY` | `300` | 重试间隔秒数 |
| `BWLIMIT` | 空 | rclone 限速，如 `10M` |
| `MIN_FREE_GB` | `5` | staging 最低剩余空间 |
| `LOCAL_KEEP` | `0` | 上传成功后本地保留份数 |
| `HOOK_TIMEOUT` | `300` | 单个 hook 超时秒数 |
| `VERIFY_ARCHIVE` | `false` | 是否用 `tar -tf` 校验归档 |
| `PUSH_API_URL` | 配置值 | 推送地址 |
| `PUSH_SERVER_NAME` | 配置值 | 来源服务器 |
| `PUSH_TOPIC` | 配置值 | MQTT 主题 |
| `PUSH_ENABLED` | `true` | 是否启用推送 |
| `PUSH_ON_SUCCESS` | `false` | 成功是否推 MQTT |
| `PUSH_ON_FAILURE` | `true` | 失败是否推 MQTT |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `HEALTH_MAX_AGE` | `300` | 心跳最大秒数 |

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
- 归档使用 tar，不做文件系统快照；数据库请用 `pre_cmd` dump，详见 [docs/database-backup.md](docs/database-backup.md)
- 每天全量备份会占用 `保留份数 × 单份大小` 的远端空间；数据量很大时建议改用 restic
- WebDAV 服务器需要支持大文件上传；部分服务对单文件大小有限制
- 首次启动如果 WebDAV 不可达，容器仍会启动，任务会按重试策略继续尝试
- 容器停止时会终止正在执行的 tar/rclone；已完成的归档保留在 `/tmp/backup-staging`，下次启动继续重传（容器重建会丢失）
