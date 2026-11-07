#!/bin/bash
# ============================================================
# 多进程 supervisor（固定模板文件，无需修改）
#
# 项目在 startup/services.sh 中声明服务：
#   SERVICE_NAMES=(mqtt api)
#   SERVICE_STOP_TIMEOUT=([mqtt]=5 [api]=15)
#
#   start_mqtt() {
#       run_service mqtt mosquitto -c /data/mosquitto.conf
#   }
#   stop_mqtt() { :; }
#
#   start_api() {
#       run_service api uvicorn main:app --app-dir /data --port 8383
#   }
#   stop_api() { :; }
#
# 行为：
#   - 启动全部服务；任一服务退出 → 停掉全部 → main.sh 退出 → Docker 重启容器
#   - docker stop 收到 TERM → 依次停止全部服务，超时后 SIGKILL
#   - 更新装好后收到 USR1 → 停掉全部，再重新启动全部（容器不动）
#   - 每个服务的 stdout/stderr 写到 /data/logs/service/<名称>.log
# ============================================================

SERVICE_LOG_DIR=${SERVICE_LOG_DIR:-/data/logs/service}
SERVICE_LOG_KEEP_DAYS=${SERVICE_LOG_KEEP_DAYS:-15}
SERVICE_STOP_TIMEOUT_DEFAULT=${SERVICE_STOP_TIMEOUT_DEFAULT:-10}
# 默认用 setsid 启动，停止时按进程组杀，避免 uvicorn --workers 之类的子进程残留。
# 不需要时设为 0。
SERVICE_USE_PROCESS_GROUP=${SERVICE_USE_PROCESS_GROUP:-1}
mkdir -p "$SERVICE_LOG_DIR"

declare -gA SERVICE_PIDS=()
declare -gA SERVICE_PGIDS=()
declare -gA SERVICE_STOP_TIMEOUT=()

# 启动一个服务：run_service <名称> <命令...>
run_service() {
    local name=$1
    shift
    if [ -z "$name" ] || [ "$#" -eq 0 ]; then
        log -f error "run_service 用法：run_service <名称> <命令...>"
        return 1
    fi

    if [ "$SERVICE_USE_PROCESS_GROUP" = "1" ] && command -v setsid >/dev/null 2>&1; then
        setsid "$@" >> "$SERVICE_LOG_DIR/${name}.log" 2>&1 &
        SERVICE_PGIDS[$name]=1
    else
        "$@" >> "$SERVICE_LOG_DIR/${name}.log" 2>&1 &
        SERVICE_PGIDS[$name]=0
    fi
    SERVICE_PIDS[$name]=$!
    log info "启动 ${name}（PID ${SERVICE_PIDS[$name]}）"
}

service_timeout() {
    local name=$1
    printf '%s\n' "${SERVICE_STOP_TIMEOUT[$name]:-$SERVICE_STOP_TIMEOUT_DEFAULT}"
}

# 给服务（或它的整个进程组）发信号
kill_service() {
    local name=$1
    local signal=$2
    local pid=${SERVICE_PIDS[$name]:-}
    [ -n "$pid" ] || return 0

    if [ "${SERVICE_PGIDS[$name]:-0}" = "1" ]; then
        kill "-${signal}" -- "-${pid}" 2>/dev/null || kill "-${signal}" "$pid" 2>/dev/null || true
    else
        kill "-${signal}" "$pid" 2>/dev/null || true
    fi
}

# 停止一个服务：先 TERM，超时后 KILL；用 wait 回收，避免僵尸进程
stop_one() {
    local name=$1
    local pid=${SERVICE_PIDS[$name]:-}
    [ -n "$pid" ] || return 0

    local timeout
    timeout=$(service_timeout "$name")
    local killer=""

    kill_service "$name" TERM

    (
        sleep "$timeout"
        kill_service "$name" KILL
    ) &
    killer=$!

    wait "$pid" 2>/dev/null || true
    kill "$killer" 2>/dev/null || true
    wait "$killer" 2>/dev/null || true

    unset 'SERVICE_PIDS[$name]'
    unset 'SERVICE_PGIDS[$name]'
}

stop_all() {
    local i name
    # 反向停止，先停后启动的服务（通常是依赖方）
    for ((i = ${#SERVICE_NAMES[@]} - 1; i >= 0; i--)); do
        name=${SERVICE_NAMES[$i]}
        if declare -F "stop_${name}" >/dev/null 2>&1; then
            "stop_${name}" || true
        fi
        stop_one "$name"
    done
}

start_all() {
    local name
    for name in "${SERVICE_NAMES[@]}"; do
        if ! declare -F "start_${name}" >/dev/null 2>&1; then
            log -f error "缺少 start_${name}()（检查 startup/services.sh）"
            return 1
        fi
        "start_${name}"
        if [ -z "${SERVICE_PIDS[$name]:-}" ]; then
            log -f error "start_${name} 没有调用 run_service 启动进程"
            return 1
        fi
    done
}

# 等待任意一个服务退出
wait_any() {
    [ "${#SERVICE_PIDS[@]}" -gt 0 ] || return 1
    local code=0
    wait -n "${SERVICE_PIDS[@]}" || code=$?
    return "$code"
}

# 找出已退出的服务名（wait -n 已经回收了其中一个）
exited_service() {
    local name pid
    for name in "${SERVICE_NAMES[@]}"; do
        pid=${SERVICE_PIDS[$name]:-}
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            echo "$name"
            return 0
        fi
    done
    return 1
}

# ------------------------------------------------------------
# 服务日志：每天 00:00 把 <name>.log copytruncate 成 <name>.log-YYYY-MM-DD
# ------------------------------------------------------------
rotate_service_logs() {
    local f archive
    for f in "$SERVICE_LOG_DIR"/*.log; do
        [ -f "$f" ] && [ -s "$f" ] || continue
        archive="${f}-$(date -d yesterday '+%Y-%m-%d')"
        cp "$f" "$archive" && : > "$f"
        ls -1t -- "${f}"-* 2>/dev/null \
            | tail -n +"$((SERVICE_LOG_KEEP_DAYS + 1))" \
            | xargs -r rm -f --
    done
}

sleep_until_midnight() {
    local now target
    now=$(date +%s)
    target=$(date -d "tomorrow 00:00" +%s 2>/dev/null || echo 0)
    if [ "$target" -gt "$now" ]; then
        sleep "$((target - now))"
    else
        sleep 86400
    fi
}

log_rotate_loop() {
    while :; do
        sleep_until_midnight
        rotate_service_logs
    done
}

print_start_info() {
    log -f success "服务已启动：${SERVICE_NAMES[*]}（PID ${SERVICE_PIDS[*]}）"
    [ -z "${SERVICE_PORT:-}" ] || log -f info "访问端口: ${SERVICE_PORT}"
    [ -z "${SERVICE_PASSWORD:-}" ] || log -f info "密码: ${SERVICE_PASSWORD}"
}

# ------------------------------------------------------------
# 主循环：startup/main.sh 最后调用
# ------------------------------------------------------------
supervise() {
    if [ "${#SERVICE_NAMES[@]}" -eq 0 ]; then
        log -f error "SERVICE_NAMES 为空（检查 startup/services.sh）"
        exit 1
    fi

    local first_start=1
    local restart_requested=0
    local code=0
    local name=""

    trap 'restart_requested=1; stop_all' USR1
    trap 'stop_all; exit 143' TERM INT

    log_rotate_loop &

    while :; do
        restart_requested=0
        start_all || { stop_all; exit 1; }

        if [ "$first_start" -eq 1 ]; then
            print_start_info
            first_start=0
        else
            log info "服务已重启：${SERVICE_NAMES[*]}"
        fi

        code=0
        wait_any || code=$?

        if [ "$restart_requested" -eq 1 ]; then
            log -f success "重启全部服务（应用更新）"
            continue
        fi

        name=$(exited_service || true)
        log -f error "服务 ${name:-unknown} 退出（exit ${code}），停止全部并结束容器"
        stop_all
        exit "$code"
    done
}
