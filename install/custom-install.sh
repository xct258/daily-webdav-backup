#!/bin/bash
# ============================================================
# 构建时执行：
#   1. 下载并安装最新稳定版 rclone
#   2. 从当前仓库下载 app/ 下的 Python 代码到 /opt/backup
# ============================================================
set -euo pipefail

if [ -z "${REPO_URL:-}" ]; then
    echo "[custom] 错误：REPO_URL 未设置，无法下载 app/ 代码" >&2
    exit 1
fi

target_arch="${TARGETARCH:-}"
if [ -z "$target_arch" ]; then
    case "$(uname -m)" in
        x86_64) target_arch=amd64 ;;
        aarch64|arm64) target_arch=arm64 ;;
        *) target_arch="$(uname -m)" ;;
    esac
fi

arch_name() {
    case "$target_arch" in
        amd64) echo "$1" ;;
        arm64) echo "$2" ;;
        *)
            echo "[custom] 不支持的架构: ${target_arch}" >&2
            return 1
            ;;
    esac
}

rclone_arch=$(arch_name amd64 arm64)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# ------------------------------------------------------------
# rclone
# ------------------------------------------------------------
echo "[custom] 安装 rclone（linux-${rclone_arch}）"
rclone_url="https://downloads.rclone.org/rclone-current-linux-${rclone_arch}.zip"
curl -fsSL --retry 3 --retry-delay 5 -o "${tmp}/rclone.zip" "$rclone_url"
unzip -q "${tmp}/rclone.zip" -d "${tmp}/rclone"
rclone_bin=$(find "${tmp}/rclone" -type f -name rclone | head -1)
if [ -z "$rclone_bin" ]; then
    echo "[custom] 错误：rclone 压缩包里没有找到可执行文件" >&2
    exit 1
fi
install -m 755 "$rclone_bin" /usr/local/bin/rclone
rclone version

# ------------------------------------------------------------
# 应用代码
# ------------------------------------------------------------
base="${REPO_URL%/}"
app_files=(
    main.py
    config.py
    scheduler.py
    runner.py
    archive.py
    rclone.py
    retention.py
    state.py
    notify.py
    process.py
    util.py
)

mkdir -p /opt/backup
for file in "${app_files[@]}"; do
    echo "[custom] 下载 app/${file}"
    curl -fsSL --retry 3 --retry-delay 5 "${base}/app/${file}" -o "/opt/backup/${file}"
done

echo "[custom] 下载 config/config.example.yml"
curl -fsSL --retry 3 --retry-delay 5 \
    "${base}/config/config.example.yml" -o /opt/backup/config.example.yml

chmod 644 /opt/backup/*.py /opt/backup/config.example.yml

# ------------------------------------------------------------
# 自检
# ------------------------------------------------------------
python3 -c 'import yaml, croniter'
python3 -m py_compile /opt/backup/*.py
rm -rf /opt/backup/__pycache__

echo "[custom] 安装完成"
