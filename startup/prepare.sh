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

export LOG_BASE_DIR="${LOG_BASE_DIR:-/data/logs}"
export CONFIG_FILE="${CONFIG_FILE:-/config/config.yml}"
export STATE_DIR="${STATE_DIR:-/data/state}"
export STAGING_DIR="${STAGING_DIR:-/data/staging}"

mkdir -p "$STATE_DIR" "$STAGING_DIR" "$LOG_BASE_DIR/service"

if [ ! -f "$CONFIG_FILE" ]; then
    log -f error "缺少配置文件：${CONFIG_FILE}"
    log -f error "请复制 config/config.example.yml 为 config/config.yml 并修改"
    exit 1
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
if python3 /opt/backup/main.py test-connection >"$out" 2>&1; then
    log info "WebDAV 连接正常"
else
    log -f warn "WebDAV 连接失败，服务仍会启动并在定时任务中重试"
    while IFS= read -r line; do
        echo "[prepare] $line" >&2
    done < "$out"
fi

log info "准备完成"
