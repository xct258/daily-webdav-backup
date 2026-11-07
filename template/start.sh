#!/bin/bash
# ============================================================
# 容器启动入口
#   1. startup/prepare.sh / update.sh / once.sh 依次执行
#   2. 启动后台定时更新（UPDATE_INTERVAL_DAYS > 0 时）
#   3. exec main.sh：服务管理脚本，负责启动服务并常驻
# main.sh 是容器主进程，
# 更新装好后给它发 USR1，它自己停掉服务再启动。
# ============================================================
set -euo pipefail

DIR=/opt/startup
ONCE_MARK=/data/.startup-once.done
# exec main.sh 之后 PID 不变，后台循环用它给服务管理脚本发信号
MAIN_PID=$$
export MAIN_PID

mkdir -p /data

# ------------------------------------------------------------
# 日志模块（构建时从 common 仓库下载到 /opt/startup/log.sh）
#   log <级别> <消息>     只写文件
#   log -f <级别> <消息>  同时打印到控制台（docker logs）
# ------------------------------------------------------------
LOG_BASE_DIR=${LOG_BASE_DIR:-/data/logs}
export LOG_BASE_DIR
if [ ! -f "${DIR}/log.sh" ]; then
    echo "[start] 缺少 ${DIR}/log.sh（检查 install/app.conf 的 LOG_LIB_URL）" >&2
    exit 1
fi
. "${DIR}/log.sh"

# ------------------------------------------------------------
# 补全 PATH：安装器把可执行文件放在 $HOME 下的 bin 目录，
# 只写进 ~/.bashrc，容器里的非交互 shell 不会加载。
# ------------------------------------------------------------
path_dirs=(
    "${HOME:-/root}/.local/bin"
    "${HOME:-/root}/bin"
    "${HOME:-/root}/.opencode/bin"
    "${HOME:-/root}/.bun/bin"
    "${HOME:-/root}/.cargo/bin"
)
if [ -n "${EXTRA_PATH_DIRS:-}" ]; then
    IFS=: read -r -a extra_dirs <<<"$EXTRA_PATH_DIRS"
    path_dirs+=("${extra_dirs[@]}")
fi
path_prefix=""
for dir in "${path_dirs[@]}"; do
    [ -n "$dir" ] && [ -d "$dir" ] || continue
    case ":${path_prefix}:${PATH}:" in *":${dir}:"*) continue ;; esac
    path_prefix="${path_prefix:+${path_prefix}:}${dir}"
done
[ -z "$path_prefix" ] || PATH="${path_prefix}:${PATH}"
export PATH

# ------------------------------------------------------------
# 启动失败与重试
# ------------------------------------------------------------
STARTUP_RETRY=${STARTUP_RETRY:-0}
case "$STARTUP_RETRY" in '' | *[!0-9]*) STARTUP_RETRY=0 ;; esac
STARTUP_RETRY_DELAY=${STARTUP_RETRY_DELAY:-5}
case "$STARTUP_RETRY_DELAY" in '' | *[!0-9]*) STARTUP_RETRY_DELAY=5 ;; esac
ATTEMPT_FILE=/data/.startup-attempts

fail_start() {
    local name=$1 code=$2 used=0
    if [ -f "$ATTEMPT_FILE" ]; then
        used=$(cat "$ATTEMPT_FILE" 2>/dev/null || echo 0)
    fi
    case "$used" in '' | *[!0-9]*) used=0 ;; esac
    used=$((used + 1))
    echo "$used" > "$ATTEMPT_FILE"

    log -f error "${name} 失败（exit ${code}）"
    if [ "$used" -le "$STARTUP_RETRY" ]; then
        log -f warn "第 ${used}/${STARTUP_RETRY} 次重试：${STARTUP_RETRY_DELAY}s 后退出，等待 restart 策略重新拉起"
        sleep "$STARTUP_RETRY_DELAY"
        exit "$code"
    fi
    log -f error "不再重试（STARTUP_RETRY=${STARTUP_RETRY}）；修复后执行 docker compose restart"
    exec sleep infinity
}

run_step() {
    local name=$1 script=$2 code=0
    bash "$script" || code=$?
    [ "$code" -eq 0 ] || fail_start "$name" "$code"
}

# ------------------------------------------------------------
# 自动更新参数
#   UPDATE_ON_START       启动时是否检查，默认 1
#   UPDATE_INTERVAL_DAYS  运行期每隔多少天检查一次，0 = 关闭（默认）
#   UPDATE_TIMEOUT        单次检查最长秒数，默认 60
#   UPDATE_STRICT         启动时检查失败是否阻塞启动，默认 0
#   UPDATE_RESTART        装到新版本后是否重启服务，默认 1
#   UPDATE_CHECK_AT       运行期检查时间 HH:MM，默认 04:00（容器本地时间）
# ------------------------------------------------------------
UPDATE_ON_START=${UPDATE_ON_START:-1}
case "$UPDATE_ON_START" in '' | 0 | false | no | off) UPDATE_ON_START=0 ;; *) UPDATE_ON_START=1 ;; esac
UPDATE_INTERVAL_DAYS=${UPDATE_INTERVAL_DAYS:-0}
case "$UPDATE_INTERVAL_DAYS" in '' | *[!0-9]*) UPDATE_INTERVAL_DAYS=0 ;; esac
UPDATE_TIMEOUT=${UPDATE_TIMEOUT:-60}
case "$UPDATE_TIMEOUT" in '' | *[!0-9]*) UPDATE_TIMEOUT=60 ;; esac
UPDATE_STRICT=${UPDATE_STRICT:-0}
UPDATE_RESTART=${UPDATE_RESTART:-1}
case "$UPDATE_RESTART" in 0 | false | no | off) UPDATE_RESTART=0 ;; *) UPDATE_RESTART=1 ;; esac
UPDATE_CHECK_AT=${UPDATE_CHECK_AT:-04:00}
if ! date -d "today ${UPDATE_CHECK_AT}" +%s >/dev/null 2>&1; then
    log -f warn "UPDATE_CHECK_AT=${UPDATE_CHECK_AT} 不是有效的 HH:MM，改用 04:00"
    UPDATE_CHECK_AT=04:00
fi

# ------------------------------------------------------------
# 检查更新
#   update.sh 退出码：0 = 无变化，10 = 已装新版本，其它 = 失败
# ------------------------------------------------------------
check_update_once() {
    local code=0
    [ -f "${DIR}/update.sh" ] || return 0

    if [ "$UPDATE_TIMEOUT" -eq 0 ]; then
        log info "检查更新（不限时）"
    else
        log info "检查更新（最长 ${UPDATE_TIMEOUT}s）"
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout "$UPDATE_TIMEOUT" bash "${DIR}/update.sh" || code=$?
    else
        bash "${DIR}/update.sh" || code=$?
    fi
    return "$code"
}

# 启动时检查：失败默认不阻塞启动
run_update() {
    local code=0

    if [ "$UPDATE_ON_START" -eq 0 ]; then
        log info "UPDATE_ON_START=0，跳过启动时检查"
        return 0
    fi
    if [ ! -f "${DIR}/update.sh" ]; then
        log info "没有 update.sh，跳过自动更新"
        return 0
    fi

    check_update_once || code=$?

    # 10 = 已更新，此时 main.sh 还没启动，新版本直接生效
    if [ "$code" -eq 0 ] || [ "$code" -eq 10 ]; then
        log success "更新检查完成"
        return 0
    fi
    if [ "$UPDATE_STRICT" = "1" ]; then
        fail_start update "$code"
        return 0
    fi
    if [ "$code" -eq 124 ]; then
        log -f warn "更新检查超时（${UPDATE_TIMEOUT}s），继续用当前版本启动"
    else
        log -f warn "更新检查失败（exit ${code}），继续用当前版本启动"
    fi
    return 0
}

# 睡到下一个 HH:MM
sleep_until_next_check() {
    local now target
    now=$(date +%s)
    target=$(date -d "today ${UPDATE_CHECK_AT}" +%s 2>/dev/null || echo 0)
    if [ "$target" -le 0 ]; then
        sleep 86400
        return 0
    fi
    if [ "$target" -le "$now" ]; then
        target=$(date -d "tomorrow ${UPDATE_CHECK_AT}" +%s 2>/dev/null || echo 0)
    fi
    if [ "$target" -gt "$now" ]; then
        sleep "$((target - now))"
    fi
    return 0
}

# 每 UPDATE_INTERVAL_DAYS 天在 UPDATE_CHECK_AT 检查一次；有更新立即重启
update_loop() {
    local code=0
    while :; do
        sleep_until_next_check

        code=0
        check_update_once || code=$?
        if [ "$code" -eq 10 ]; then
            if [ "$UPDATE_RESTART" = "1" ]; then
                log -f success "已更新，重启服务应用新版本"
                kill -USR1 "$MAIN_PID" 2>/dev/null || true
            else
                log -f success "已更新；当前进程仍是旧版本，下次重启容器后生效"
            fi
        elif [ "$code" -ne 0 ]; then
            log -f warn "定时更新检查失败（exit ${code}），继续使用当前版本"
        fi

        # 检查间隔 > 1 天时，先跳过中间的整天，下一轮再睡到 HH:MM
        if [ "$UPDATE_INTERVAL_DAYS" -gt 1 ]; then
            sleep "$(( (UPDATE_INTERVAL_DAYS - 1) * 86400 ))"
        fi
    done
}

# ------------------------------------------------------------
# 1. 启动前的准备：每次启动都执行
# ------------------------------------------------------------
log info "prepare"
run_step prepare "${DIR}/prepare.sh"

# ------------------------------------------------------------
# 2. 启动时检查更新（可选）
# ------------------------------------------------------------
log info "update"
run_update

# ------------------------------------------------------------
# 3. 只执行一次：初始化数据等
# ------------------------------------------------------------
if [ -f "$ONCE_MARK" ]; then
    log info "once 已执行过，跳过"
else
    log info "once"
    run_step once "${DIR}/once.sh"
    touch "$ONCE_MARK"
fi

# ------------------------------------------------------------
# 4. 启动后台任务，然后把容器交给 main.sh
# ------------------------------------------------------------
rm -f "$ATTEMPT_FILE"

if [ "$UPDATE_INTERVAL_DAYS" -gt 0 ] && [ -f "${DIR}/update.sh" ]; then
    log info "启用定时更新检查：每 ${UPDATE_INTERVAL_DAYS} 天，${UPDATE_CHECK_AT} 检查；有更新立即重启"
    update_loop &
fi

log info "main（prepare/update/once 完成，启动 main.sh）"
exec bash "${DIR}/main.sh"
