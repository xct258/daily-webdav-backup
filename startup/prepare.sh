#!/bin/bash
# ============================================================
# 每次启动执行：
#   - 创建数据目录
#   - 校验配置和路径
#   - 生成 rclone 配置
#   - 测试 WebDAV（失败只告警，调度器会继续重试）
# ============================================================
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/log.sh"

export LOG_BASE_DIR=/data/logs
export CONFIG_FILE=/config/config.yml
export STATE_DIR=/data/state
# 临时归档固定放容器内部，不落到被备份的挂载目录里；不要挂载这个路径
STAGING_DIR=/tmp/backup-staging

mkdir -p "$STATE_DIR" "$STAGING_DIR" "$LOG_BASE_DIR/service"

generated_config=0
if [ ! -f "$CONFIG_FILE" ]; then
    auto_template=/opt/backup/config.auto.yml
    if [ ! -f "$auto_template" ]; then
        log -f error "缺少配置文件：${CONFIG_FILE}"
        log -f error "镜像内也没有占位模板 ${auto_template}"
        exit 1
    fi

    mkdir -p "$(dirname "$CONFIG_FILE")" 2>/dev/null || true
    if cp "$auto_template" "$CONFIG_FILE" 2>/dev/null; then
        chmod 600 "$CONFIG_FILE" 2>/dev/null || true
        generated_config=1
        log -f warn "未找到配置文件，已生成占位配置：${CONFIG_FILE}"
        log -f warn "占位任务默认禁用；修改 WebDAV 和备份路径后执行 docker compose restart"
    else
        log -f error "缺少配置文件，且无法写入 ${CONFIG_FILE}"
        log -f error "请把 ./config 挂载为可写，或手动创建 config/config.yml"
        exit 1
    fi
fi

if ! command -v python3 >/dev/null 2>&1; then
    log -f error "缺少 python3"
    exit 1
fi
if ! command -v rclone >/dev/null 2>&1; then
    log -f error "缺少 rclone"
    exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
    log -f error "缺少 tar"
    exit 1
fi

out=$(mktemp)
trap 'rm -f "$out"' EXIT

# ------------------------------------------------------------
# 配置校验：配置、路径、rclone 配置生成
# ------------------------------------------------------------
if ! python3 /opt/backup/main.py validate >"$out" 2>&1; then
    log -f error "配置校验失败，容器停止启动"
    while IFS= read -r line; do
        echo "[prepare] $line" >&2
    done < "$out"
    exit 1
fi
log info "配置校验通过"

# ------------------------------------------------------------
# WebDAV 连接测试：网络问题不阻塞启动
# ------------------------------------------------------------
if [ "$generated_config" -eq 1 ]; then
    log info "当前是自动生成的占位配置，跳过 WebDAV 连接测试"
else
    if python3 /opt/backup/main.py test-connection >"$out" 2>&1; then
        log info "WebDAV 连接正常"
    else
        log -f warn "WebDAV 连接失败，服务仍会启动并在定时任务中重试"
        while IFS= read -r line; do
            echo "[prepare] $line" >&2
        done < "$out"
    fi
fi

log info "准备完成"
