#!/bin/bash
# ============================================================
# 本地自查：push 前跑一遍，提前发现路径/语法/配置问题
#   用法：bash check.sh
#   CI 的 Validate 步骤调用同一个脚本，本地和线上结果一致
#   只做静态检查，不需要 Docker
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

pass=0
fail=0
ok() { echo "  [OK]   $*"; pass=$((pass + 1)); }
bad() { echo "  [FAIL] $*"; fail=$((fail + 1)); }
skip() { echo "  [SKIP] $*"; }
warn() { echo "  [WARN] $*"; }
section() { printf '\n== %s ==\n' "$*"; }

# ------------------------------------------------------------
# 1. 必备文件
# ------------------------------------------------------------
section "必备文件"
files=(
    install/app.conf
    install/install.sh
    startup/prepare.sh
    startup/once.sh
    startup/services.sh
    startup/main.sh
    template/Dockerfile
    template/start.sh
    template/supervisor.sh
    docker-compose.yml
    .github/workflows/build-image.yml
)
for f in "${files[@]}"; do
    if [ -f "$f" ]; then ok "$f"; else bad "缺少 $f"; fi
done

# ------------------------------------------------------------
# 2. 脚本语法
# ------------------------------------------------------------
section "脚本语法"
for f in check.sh install/*.sh startup/*.sh template/*.sh; do
    [ -f "$f" ] || continue
    if err=$(bash -n "$f" 2>&1); then
        ok "$f"
    else
        bad "$f 语法错误：${err}"
    fi
done

# ------------------------------------------------------------
# 3. install/app.conf 内容
# ------------------------------------------------------------
section "install/app.conf"
err_file=$(mktemp)
if source install/app.conf 2>"$err_file"; then
    ok "可正常 source"
else
    bad "source 失败：$(cat "$err_file")"
fi
rm -f "$err_file"

[ -n "${IMAGE_REPO:-}" ] && ok "IMAGE_REPO=${IMAGE_REPO}" || bad "IMAGE_REPO 未设置"
[ -n "${IMAGE_TAG:-}" ] && ok "IMAGE_TAG=${IMAGE_TAG}" || bad "IMAGE_TAG 未设置"

case "${PLATFORMS:-}" in
    linux/*) ok "PLATFORMS=${PLATFORMS}" ;;
    "") bad "PLATFORMS 未设置" ;;
    *) bad "PLATFORMS 应以 linux/ 开头（当前：${PLATFORMS}）" ;;
esac

for k in INSTALL_7Z INSTALL_CUSTOM; do
    case "${!k:-false}" in
        true | false) ok "$k=${!k:-false}" ;;
        *) bad "$k 只能是 true / false（当前：${!k:-}）" ;;
    esac
done

if [ "${INSTALL_CUSTOM:-false}" = "true" ]; then
    if [ ! -f install/custom-install.sh ]; then
        bad "INSTALL_CUSTOM=true，但缺少 install/custom-install.sh"
    elif err=$(bash -n install/custom-install.sh 2>&1); then
        ok "install/custom-install.sh"
    else
        bad "custom-install.sh 语法错误：${err}"
    fi
fi

# ------------------------------------------------------------
# 3.5 多进程契约
# ------------------------------------------------------------
section "多进程契约"
if [ -f startup/services.sh ]; then
    contract_err=$(mktemp)
    if (
        declare -A SERVICE_STOP_TIMEOUT=()
        # shellcheck disable=SC1091
        . startup/services.sh
        [ "${#SERVICE_NAMES[@]}" -gt 0 ] || {
            echo "SERVICE_NAMES 不能为空" >&2
            exit 1
        }
        for name in "${SERVICE_NAMES[@]}"; do
            declare -F "start_${name}" >/dev/null 2>&1 || {
                echo "缺少 start_${name}()" >&2
                exit 1
            }
        done
    ) 2>"$contract_err"; then
        ok "startup/services.sh 服务契约有效"
    else
        bad "startup/services.sh 契约错误：$(cat "$contract_err")"
    fi
    rm -f "$contract_err"
else
    bad "缺少 startup/services.sh（多进程模板必需）"
fi

if grep -qF 'supervisor.sh' startup/main.sh && grep -qF 'services.sh' startup/main.sh; then
    ok "main.sh 加载 supervisor.sh + services.sh"
else
    bad "startup/main.sh 必须加载 template/supervisor.sh 和 startup/services.sh"
fi

if grep -q '^supervise()' template/supervisor.sh 2>/dev/null; then
    ok "supervisor.sh 提供 supervise()"
else
    bad "template/supervisor.sh 缺少 supervise()"
fi

# ------------------------------------------------------------
# 4. Dockerfile 与仓库文件是否对得上
# ------------------------------------------------------------
section "Dockerfile 引用"
while read -r p; do
    [ -n "$p" ] || continue
    # 带变量的路径（如 startup/${f}）在下面单独校验
    case "$p" in *'${'*) continue ;; esac
    # 可选文件：Dockerfile 下载失败会跳过，仓库里没有也算正常
    case "$p" in
        startup/update.sh)
            if [ -f "$p" ]; then
                ok "可选下载 $p"
            else
                warn "可选文件 $p 不存在，启动时自动更新会跳过"
            fi
            continue
            ;;
    esac
    if [ -f "$p" ]; then ok "下载 $p"; else bad "Dockerfile 下载的 $p 在仓库中不存在"; fi
done < <(grep -o '\${REPO_URL}/[^" ]*' template/Dockerfile | sed 's#\${REPO_URL}/##')

dl_names=$(sed -n 's/.*for f in \(.*\); do.*/\1/p' template/Dockerfile | xargs -n1 2>/dev/null | sort)
# update.sh 是可选脚本，不参与「必备 startup 列表」比对
repo_names=$(ls startup/*.sh 2>/dev/null | xargs -n1 basename 2>/dev/null | grep -vx 'update.sh' | sort)
if [ "$dl_names" = "$repo_names" ]; then
    ok "startup 列表一致：$(echo "$repo_names" | tr '\n' ' ')"
else
    bad "Dockerfile 的 startup 列表与 startup/ 目录不一致"
    echo "         Dockerfile: $(echo "$dl_names" | tr '\n' ' ')" >&2
    echo "         仓库目录  : $(echo "$repo_names" | tr '\n' ' ')" >&2
fi

# 运行期脚本目录：start.sh 的 DIR 必须与 Dockerfile 的安装路径一致
start_dir=$(sed -n 's/^DIR=//p' template/start.sh)
if [ -n "$start_dir" ] && grep -q "\"${start_dir}/" template/Dockerfile; then
    ok "start.sh DIR=${start_dir} 与 Dockerfile 安装路径一致"
else
    bad "start.sh 的 DIR=${start_dir} 在 Dockerfile 里找不到对应安装路径"
fi

# 构建期配置：install.sh 读的 app.conf 必须是 Dockerfile 下载到的位置
conf_path=$(sed -n 's/^CONF=//p' install/install.sh)
if [ -n "$conf_path" ] && grep -q -- "$conf_path" template/Dockerfile; then
    ok "install.sh CONF=${conf_path} 在 Dockerfile 中有对应下载"
else
    bad "install.sh 的 CONF=${conf_path} 在 Dockerfile 里找不到对应下载"
fi

# 入口：ENTRYPOINT 指向的路径必须是 Dockerfile 里 start.sh 的安装位置
entry=$(sed -n 's/^ENTRYPOINT \["\(.*\)"\]$/\1/p' template/Dockerfile)
if [ -n "$entry" ] && grep -qF "template/start.sh\" ${entry}" template/Dockerfile; then
    ok "ENTRYPOINT ${entry} 与 start.sh 安装路径一致"
else
    bad "ENTRYPOINT 指向 ${entry:-未定义}，但 Dockerfile 没有把 start.sh 安装到该路径"
fi

# 日志模块：Dockerfile 从 app.conf 取出 LOG_LIB_URL，下载到 start.sh 引用的路径
# 不能用 `. app.conf`：RUN 跑在 /bin/sh（dash）下，app.conf 里有 bash 数组会直接语法错误
if grep -q '^[[:space:]]*\. /tmp/app.conf' template/Dockerfile; then
    bad "Dockerfile 里 source 了 app.conf：dash 不认数组语法，应改用 sed 取需要的键"
else
    log_dest=$(sed -n 's#.*fetch "\$log_url" \([^;]*\);.*#\1#p' template/Dockerfile | head -1)
    if [ -z "$log_dest" ]; then
        bad "Dockerfile 里找不到从 app.conf 取 LOG_LIB_URL 并下载 log.sh 的语句"
    elif [ "$log_dest" = "${start_dir}/log.sh" ] && grep -qF '${DIR}/log.sh' template/start.sh; then
        ok "log.sh 下载到 ${log_dest}，start.sh 从 \${DIR}/log.sh 引入"
    else
        bad "log.sh 下载到 ${log_dest}，但 start.sh 没有从 \${DIR}/log.sh（DIR=${start_dir}）引入"
    fi
fi
if [ -z "${LOG_LIB_URL:-}" ]; then
    bad "LOG_LIB_URL 为空：镜像里没有 log.sh，容器启动会直接失败（start.sh exit 1）"
else
    ok "LOG_LIB_URL 已配置（固定到提交 SHA）"
fi

# 自动更新：Dockerfile 可选下载 update.sh，start.sh 用 ${DIR}/update.sh 调度
if grep -qF 'startup/update.sh' template/Dockerfile && grep -qF '${DIR}/update.sh' template/start.sh; then
    ok "自动更新入口一致：startup/update.sh -> \${DIR}/update.sh"
else
    bad "自动更新入口不一致：Dockerfile 需下载 startup/update.sh，start.sh 需调用 \${DIR}/update.sh"
fi

# ------------------------------------------------------------
# 5. docker-compose.yml 与 app.conf 的镜像名是否一致
# ------------------------------------------------------------
section "docker-compose.yml"
compose_img=$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' docker-compose.yml | head -1)
compose_img="${compose_img//\"/}"
compose_img="${compose_img//\'/}"
expected_img="${IMAGE_REPO:-}:${IMAGE_TAG:-}"
if [ "$compose_img" = "$expected_img" ]; then
    ok "镜像名与 app.conf 一致：${expected_img}"
else
    bad "compose 镜像 ${compose_img} ≠ app.conf 的 ${expected_img}"
fi

compose_name=$(sed -n 's/^[[:space:]]*container_name:[[:space:]]*//p' docker-compose.yml | head -1)
compose_name="${compose_name//\"/}"
compose_name="${compose_name//\'/}"
if [ "$compose_name" = "my-image" ]; then
    warn "container_name 还是模板默认值 my-image，部署前记得改（不强制校验）"
else
    ok "container_name=${compose_name}（不校验唯一性）"
fi

# ------------------------------------------------------------
# 6. YAML 解析
# ------------------------------------------------------------
section "YAML"
yamls=(docker-compose.yml .github/workflows/build-image.yml)
if python3 -c 'import yaml' 2>/dev/null; then
    for f in "${yamls[@]}"; do
        if python3 -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' "$f" 2>/dev/null; then
            ok "$f"
        else
            bad "$f 解析失败"
        fi
    done
elif command -v node >/dev/null 2>&1 && node -e 'require("yaml")' 2>/dev/null; then
    if node -e 'const y=require("yaml"),fs=require("fs");for(const f of process.argv.slice(1))y.parse(fs.readFileSync(f,"utf8"))' "${yamls[@]}" 2>/dev/null; then
        ok "YAML 解析通过"
    else
        bad "YAML 解析失败"
    fi
else
    skip "没有 python3-yaml / node-yaml，跳过"
fi

# ------------------------------------------------------------
# 7. 可选：装了才检查
# ------------------------------------------------------------
section "可选工具"
# 只提示，不算失败（不同环境装不装、版本不同，避免影响构建）
if command -v shellcheck >/dev/null 2>&1; then
    if out=$(shellcheck -S error install/*.sh startup/*.sh template/start.sh 2>&1); then
        ok "shellcheck"
    else
        warn "shellcheck 报错：${out}"
    fi
else
    skip "未安装 shellcheck"
fi

# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
    echo
    echo "全部通过（${pass} 项）"
    exit 0
else
    echo
    echo "失败 ${fail} 项，通过 ${pass} 项"
    exit 1
fi
