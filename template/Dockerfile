# ============================================================
# 通用镜像模板（固定文件，无需修改）
#   构建期 -> install/：下载到 /tmp，安装完即删除
#   运行期 -> startup/：安装到 /opt/startup
#   入口   -> template/start.sh：安装到 /opt/start.sh
#   以上文件构建时从仓库下载（REPO_URL 由 CI 传入）
# ============================================================

FROM xct258/debian-cn:latest

ENV DEBIAN_FRONTEND=noninteractive

# 下载脚本与配置的仓库地址（由 CI 传入，必须提供）
ARG REPO_URL

# 目标架构（buildx 按平台自动注入，供快捷安装脚本选择对应架构的包）
ARG TARGETARCH

# ------------------------------------------------------------
# 下载配置与启动脚本 + 依赖安装（合并为一个 RUN，减少镜像层数）
#   构建不启用缓存（CI 的 no-cache），因此每次都重新下载最新文件
# ------------------------------------------------------------
# 注意：wget 的 CA 证书属于 Recommended，--no-install-recommends 不会带上，
# 缺了会让 https 下载报证书错误（wget exit 5），所以这里固定一起装
# 路径约定：构建期配置放 /tmp（安装完删除），运行期脚本放 /opt/startup，
# 镜像里不占用 /app 等业务目录
RUN set -eux; \
    if ! command -v wget >/dev/null 2>&1 || [ ! -e /etc/ssl/certs/ca-certificates.crt ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends wget ca-certificates; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    fetch() { \
        wget -nv --tries=3 --waitretry=5 --timeout=30 -O "$2" "$1" || { \
            echo "[fetch] 下载失败（exit $?）：$1" >&2; \
            exit 1; \
        }; \
    }; \
    fetch "${REPO_URL}/install/app.conf" /tmp/app.conf; \
    if ! wget -q --tries=3 --timeout=30 -O /tmp/custom-install.sh "${REPO_URL}/install/custom-install.sh"; then \
        rm -f /tmp/custom-install.sh; \
    fi; \
    mkdir -p /opt/startup; \
    for f in prepare.sh once.sh services.sh main.sh; do \
        fetch "${REPO_URL}/startup/${f}" "/opt/startup/${f}"; \
    done; \
    fetch "${REPO_URL}/template/supervisor.sh" /opt/startup/supervisor.sh; \
    if ! wget -q --tries=3 --timeout=30 -O /opt/startup/update.sh "${REPO_URL}/startup/update.sh"; then \
        rm -f /opt/startup/update.sh; \
    fi; \
    log_url=$(sed -n 's/^LOG_LIB_URL=//p' /tmp/app.conf | head -1 | tr -d '"'); \
    if [ -n "$log_url" ]; then \
        fetch "$log_url" /opt/startup/log.sh; \
    fi; \
    fetch "${REPO_URL}/install/install.sh" /tmp/install.sh; \
    chmod +x /tmp/install.sh; \
    /tmp/install.sh; \
    rm -f /tmp/install.sh /tmp/app.conf /tmp/custom-install.sh; \
    fetch "${REPO_URL}/template/start.sh" /opt/start.sh; \
    chmod +x /opt/start.sh

# 工作目录不在这里固定，默认是 /；需要固定时用 compose 的 working_dir 指定
ENTRYPOINT ["/opt/start.sh"]
