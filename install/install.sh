#!/bin/bash
# ============================================================
# 依赖安装（构建时执行一次）
#   读取 /tmp/app.conf：APT_PACKAGES / INSTALL_7Z / PYTHON_PACKAGES / INSTALL_CUSTOM
#   自定义安装脚本 /tmp/custom-install.sh（仓库里同在 install/ 目录）
#   这两个文件只在构建期存在，安装完由 Dockerfile 删除
# ============================================================
set -euo pipefail

CONF=/tmp/app.conf
if [ ! -f "$CONF" ]; then
    echo "[install] 错误：未找到 ${CONF}" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$CONF"

# 目标架构（buildx 提供 TARGETARCH，本地构建时回退 uname）
target_arch="${TARGETARCH:-}"
if [ -z "$target_arch" ]; then
    case "$(uname -m)" in
        x86_64) target_arch=amd64 ;;
        aarch64|arm64) target_arch=arm64 ;;
        *) target_arch="$(uname -m)" ;;
    esac
fi

# ------------------------------------------------------------
# 常用软件快捷安装
# ------------------------------------------------------------
install_7z() {
    echo "[7z] 正在获取最新版本信息..."
    local release version pattern url tmp
    release=$(curl -fsSL --retry 3 --retry-delay 5 https://api.github.com/repos/ip7z/7zip/releases/latest)
    version=$(echo "$release" | jq -r '.tag_name')
    case "$target_arch" in
        amd64) pattern="linux-x64.tar.xz" ;;
        arm64) pattern="linux-arm64.tar.xz" ;;
        *) echo "[7z] 不支持的架构: ${target_arch}" >&2; return 1 ;;
    esac
    url=$(echo "$release" | jq -r --arg p "$pattern" '.assets[] | select(.name | test($p)) | .browser_download_url')
    if [ -z "$url" ] || [ "$url" = "null" ]; then
        echo "[7z] 未找到匹配 ${pattern} 的资源" >&2
        return 1
    fi
    echo "[7z] 最新版本: ${version}"
    tmp=$(mktemp -d)
    curl -fsSL --retry 3 --retry-delay 5 -o "${tmp}/7z.tar.xz" "$url"
    tar -xJf "${tmp}/7z.tar.xz" -C "$tmp"
    install -m 755 "${tmp}/7zz" /usr/local/bin/7zz
    ln -sf /usr/local/bin/7zz /usr/local/bin/7z
    ln -sf /usr/local/bin/7zz /usr/local/bin/7za
    rm -rf "$tmp"
    echo "[7z] 安装完成"
}

# ------------------------------------------------------------
# 1. 收集 apt 依赖（系统依赖 + 快捷软件依赖 + Python 运行时）
# ------------------------------------------------------------
py_pkgs=()
if declare -p PYTHON_PACKAGES >/dev/null 2>&1; then
    for p in "${PYTHON_PACKAGES[@]}"; do
        if [ -n "$p" ]; then py_pkgs+=("$p"); fi
    done
fi

apt_pkgs=()
if declare -p APT_PACKAGES >/dev/null 2>&1; then
    for p in "${APT_PACKAGES[@]}"; do
        if [ -n "$p" ]; then apt_pkgs+=("$p"); fi
    done
fi
# 有 pip 依赖时自动安装 Python
if [ "${#py_pkgs[@]}" -gt 0 ]; then
    apt_pkgs+=(python3 python3-pip python-is-python3)
fi
# 快捷软件所需工具
if [ "${INSTALL_7Z:-false}" = "true" ]; then
    apt_pkgs+=(curl jq xz-utils)
fi

if [ "${#apt_pkgs[@]}" -gt 0 ]; then
    echo "[install] apt 安装: ${apt_pkgs[*]}"
    apt-get update
    apt-get install -y --no-install-recommends "${apt_pkgs[@]}"
    rm -rf /var/lib/apt/lists/*
fi

# ------------------------------------------------------------
# 2. 常用软件快捷安装
# ------------------------------------------------------------
if [ "${INSTALL_7Z:-false}" = "true" ]; then
    install_7z
fi

# ------------------------------------------------------------
# 3. Python 依赖（pip）
# ------------------------------------------------------------
if [ "${#py_pkgs[@]}" -gt 0 ]; then
    echo "[install] pip 安装: ${py_pkgs[*]}"
    # Debian 12+ 受 PEP 668 限制，需 --break-system-packages（容器内可接受）
    if ls /usr/lib/python3*/EXTERNALLY-MANAGED >/dev/null 2>&1; then
        pip3 install --no-cache-dir --break-system-packages "${py_pkgs[@]}"
    else
        pip3 install --no-cache-dir "${py_pkgs[@]}"
    fi
fi

# ------------------------------------------------------------
# 4. 自定义安装脚本（apt / pip 之外的操作）
#    app.conf 的 INSTALL_CUSTOM=true 时执行 /tmp/custom-install.sh
# ------------------------------------------------------------
if [ "${INSTALL_CUSTOM:-false}" = "true" ]; then
    script=/tmp/custom-install.sh
    if [ ! -f "$script" ]; then
        echo "[install] 错误：INSTALL_CUSTOM=true，但未找到 ${script}" >&2
        exit 1
    fi
    echo "[install] 执行自定义安装脚本: ${script}"
    # 传给脚本，脚本内可直接用 ${TARGETARCH}
    export TARGETARCH="$target_arch"
    bash "$script"
fi

echo "[install] 依赖安装完成"
