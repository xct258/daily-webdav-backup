#!/bin/bash
# ============================================================
# 服务管理入口（固定模板文件，无需修改）
#   多进程启动/停止逻辑在 template/supervisor.sh
#   项目要启动哪些服务在 startup/services.sh 声明
# ============================================================
set -euo pipefail

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# 日志模块（构建时下载到 /opt/startup/log.sh）
. "$DIR/log.sh"

# 通用多进程 supervisor
. "$DIR/supervisor.sh"

# 项目专属：SERVICE_NAMES + start_<name> / stop_<name>
. "$DIR/services.sh"

supervise
