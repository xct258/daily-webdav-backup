#!/bin/bash
# ============================================================
# 常驻服务：Python 调度器
#   停止时给 45 秒收尾：会终止正在执行的 tar/rclone，
#   已完成的本地归档保留，下次启动继续重传。
# ============================================================
# shellcheck disable=SC2034  # 这些变量由 supervisor.sh 读取
SERVICE_NAMES=(scheduler)
SERVICE_STOP_TIMEOUT=([scheduler]=45)
SERVICE_LOG_KEEP_DAYS=30

start_scheduler() {
    run_service scheduler \
        env PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
        python3 /opt/backup/main.py schedule
}
