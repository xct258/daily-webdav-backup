# 数据库完整备份

本项目本身是“文件打包 + 上传”工具，不做数据库快照。
直接 tar/7z 运行中的数据库数据目录，备份任务会成功，但归档可能不一致，恢复时可能失败。

要得到可恢复的数据库备份，必须使用数据库自己的备份方式，再让本项目负责：

- 打包
- 上传 WebDAV
- 远端保留
- 失败重试
- 消息推送

---

## 总体流程

```text
pre_cmd
  └─ 数据库原生 dump（一致性备份）
       ↓
tar + zstd 打包 dump 文件
       ↓
上传 WebDAV
       ↓
post_cmd 清理临时 dump
```

`pre_cmd` 是同步执行的：

- dump 成功 → 继续打包
- dump 失败 → 整个任务失败，不会上传半份备份

---

## 通用配置要点

### 1. 安装数据库客户端

修改 `install/app.conf`，按需加入：

```bash
APT_PACKAGES=(
  ca-certificates
  curl
  tzdata
  zstd
  tar
  gzip
  unzip
  postgresql-client
  # default-mysql-client
  # sqlite3
  # redis-tools
)
```

改完重新触发 GitHub Actions 构建镜像。

MongoDB 的 `mongodump` / `mongorestore` 不在 Debian 默认源里，需要在
`install/custom-install.sh` 里从 MongoDB 官方下载 `mongodb-database-tools`。

### 2. 网络可达

如果数据库在同一个 compose 项目里，直接用服务名：

```yaml
pre_cmd: |
  pg_dump -h db -U app -d app ...
```

如果数据库在宿主机：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

然后使用 `-h host.docker.internal`。

### 3. 账号权限

- PostgreSQL：`pg_dump` 需要读取目标库；`pg_dumpall` 导出角色需要足够权限
- MySQL：`--all-databases` 需要相应权限
- MongoDB：需要能执行 `mongodump` 的账号
- Redis：`--rdb` 需要能执行同步命令

### 4. 临时空间和超时

dump 会写到 `/tmp/backup-hooks/<job>/`，默认超时 300 秒。

数据库较大时在 compose 里加：

```yaml
environment:
  HOOK_TIMEOUT: "3600"
  MIN_FREE_GB: "20"
```

`MIN_FREE_GB` 要能容纳 dump + 压缩后的归档。

### 5. archive_root 很重要

数据库 dump 任务建议使用：

```yaml
paths:
  - "/tmp/backup-hooks/postgres"
archive_root: "/tmp/backup-hooks"
```

这样归档里是：

```text
postgres/all.sql.gz
```

不要把 `archive_root` 设成 `/`，否则 `/tmp/backup-staging` 会被当成备份程序自己的临时目录排除掉。

### 6. 推荐拆成独立任务

应用文件一个任务，数据库 dump 一个任务：

- 恢复时互不影响
- 排除规则、保留份数可以分别设置
- 一个失败不会影响另一个

---

## PostgreSQL

### 完整备份（所有库 + 角色）

`pg_dumpall` 输出纯 SQL，包含角色和所有数据库，适合“完整恢复”。

```yaml
jobs:
  - name: "postgres"
    enabled: true
    paths:
      - "/tmp/backup-hooks/postgres"
    archive_root: "/tmp/backup-hooks"
    pre_cmd: |
      set -euo pipefail
      mkdir -p "$HOOK_DIR"
      export PGPASSWORD="$PGPASSWORD"
      pg_dumpall --clean --if-exists | gzip > "$HOOK_DIR/all.sql.gz"
    post_cmd: |
      rm -f "$HOOK_DIR/all.sql.gz"
```

compose：

```yaml
environment:
  PGPASSWORD: "数据库密码"
  HOOK_TIMEOUT: "3600"
```

恢复：

```bash
gunzip -c all.sql.gz | psql -h 新库地址 -U postgres
```

### 单库自定义格式（恢复更快）

```yaml
pre_cmd: |
  set -euo pipefail
  mkdir -p "$HOOK_DIR"
  export PGPASSWORD="$PGPASSWORD"
  pg_dump -h db -U app -d app -Fc -f "$HOOK_DIR/app.dump"
post_cmd: |
  rm -f "$HOOK_DIR/app.dump"
```

恢复：

```bash
pg_restore -h 新库地址 -U app -d app app.dump
```

注意：`pg_dump` 单库不包含角色/全局对象。需要完整恢复时额外导出：

```bash
pg_dumpall --globals-only > "$HOOK_DIR/globals.sql"
```

---

## MySQL / MariaDB

### 完整备份（所有库）

```yaml
jobs:
  - name: "mysql"
    enabled: true
    paths:
      - "/tmp/backup-hooks/mysql"
    archive_root: "/tmp/backup-hooks"
    pre_cmd: |
      set -euo pipefail
      mkdir -p "$HOOK_DIR"
      MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump \
        -h db -u root \
        --all-databases \
        --single-transaction \
        --routines --triggers --events \
        --hex-blob \
        | gzip > "$HOOK_DIR/all.sql.gz"
    post_cmd: |
      rm -f "$HOOK_DIR/all.sql.gz"
```

compose：

```yaml
environment:
  MYSQL_ROOT_PASSWORD: "数据库密码"
  HOOK_TIMEOUT: "3600"
```

恢复：

```bash
gunzip -c all.sql.gz | mysql -h 新库地址 -u root -p
```

注意：

- `--single-transaction` 只保证 InnoDB 的一致性
- MyISAM 表不支持事务，需要 `--lock-tables` 或停写
- DDL 变更期间仍可能产生不一致，严格场景用 `mariabackup` / `xtrabackup` 或快照

### 单库备份

```yaml
pre_cmd: |
  set -euo pipefail
  mkdir -p "$HOOK_DIR"
  MYSQL_PWD="$MYSQL_PASSWORD" mysqldump \
    -h db -u app \
    --single-transaction \
    --routines --triggers \
    app | gzip > "$HOOK_DIR/app.sql.gz"
post_cmd: |
  rm -f "$HOOK_DIR/app.sql.gz"
```

---

## SQLite

```yaml
jobs:
  - name: "sqlite"
    enabled: true
    paths:
      - "/tmp/backup-hooks/sqlite"
    archive_root: "/tmp/backup-hooks"
    pre_cmd: |
      set -euo pipefail
      mkdir -p "$HOOK_DIR"
      sqlite3 /sources/app/data.db ".backup '$HOOK_DIR/data.db'"
    post_cmd: |
      rm -f "$HOOK_DIR/data.db"
```

或者：

```sql
VACUUM INTO '/tmp/backup-hooks/sqlite/data.db';
```

不要把正在写入的 `.db` 和 `-wal` / `-shm` 直接 tar。

恢复：

1. 停止应用
2. 把 `data.db` 放回原位置
3. 启动应用

---

## Redis

```yaml
jobs:
  - name: "redis"
    enabled: true
    paths:
      - "/tmp/backup-hooks/redis"
    archive_root: "/tmp/backup-hooks"
    pre_cmd: |
      set -euo pipefail
      mkdir -p "$HOOK_DIR"
      redis-cli -h redis -a "$REDIS_PASSWORD" --no-auth-warning \
        --rdb "$HOOK_DIR/dump.rdb"
    post_cmd: |
      rm -f "$HOOK_DIR/dump.rdb"
```

`redis-cli --rdb` 会触发一次完整同步，得到一致的时间点快照。

恢复：

1. 停止 Redis
2. 用备份的 `dump.rdb` 替换数据目录里的文件
3. 启动 Redis

---

## MongoDB

需要先安装 `mongodb-database-tools`。

```yaml
jobs:
  - name: "mongodb"
    enabled: true
    paths:
      - "/tmp/backup-hooks/mongodb"
    archive_root: "/tmp/backup-hooks"
    pre_cmd: |
      set -euo pipefail
      mkdir -p "$HOOK_DIR"
      mongodump --uri="$MONGO_URI" \
        --archive="$HOOK_DIR/mongo.archive" \
        --gzip
    post_cmd: |
      rm -f "$HOOK_DIR/mongo.archive"
```

恢复：

```bash
mongorestore --uri="mongodb://..." --gzip --archive=mongo.archive
```

`mongodump` 是逻辑备份。对副本集/分片集群要求严格时间点一致性时，应使用快照或 MongoDB 官方备份方案。

---

## 恢复测试

备份没有经过恢复测试，就不能算“完整可用”。

建议定期：

1. 从 WebDAV 下载最新归档
2. 校验 sha256
3. 在测试数据库上恢复
4. 检查表数量、关键数据、应用能否启动

PostgreSQL：

```bash
pg_restore -l app.dump | head
```

MySQL：

```bash
gzip -t all.sql.gz
```

SQLite：

```bash
sqlite3 data.db "PRAGMA integrity_check;"
```

---

## 总结

| 数据库 | 项目里可行吗 | 推荐方式 |
|---|---|---|
| PostgreSQL | 可以 | `pg_dumpall` / `pg_dump -Fc` |
| MySQL / MariaDB | 可以 | `mysqldump --single-transaction` |
| SQLite | 可以 | `.backup` / `VACUUM INTO` |
| Redis | 可以 | `redis-cli --rdb` |
| MongoDB | 可以 | `mongodump --gzip --archive` |

核心结论：

- 项目可以负责打包、上传、保留、通知
- 数据库一致性必须由 `pre_cmd` 里的数据库原生工具保证
- tar 还是 7z 只影响压缩方式，不影响一致性
- 最可靠的方式：**数据库 dump → 打包 dump → 上传**
