#!/bin/bash
# ============================================================
# 在本地/测试服务器构建镜像，不触发 GitHub Actions
#
# 用法：
#   bash scripts/build.sh
#   IMAGE=xct258/daily-webdav-backup:test bash scripts/build.sh
#   NO_CACHE=1 bash scripts/build.sh
#
# 注意：template/Dockerfile 会从 GitHub raw 下载 app/ 和 startup/。
#       未提交、未 push 的改动不会进入镜像。
# ============================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REPO="${GITHUB_REPOSITORY:-xct258/daily-webdav-backup}"
SHA="${GIT_SHA:-$(git rev-parse HEAD)}"
IMAGE="${IMAGE:-xct258/daily-webdav-backup:latest}"
REPO_URL="https://raw.githubusercontent.com/${REPO}/${SHA}"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "警告：工作区有未提交改动；镜像会使用 GitHub 上的 ${SHA}，不包含未提交内容" >&2
fi

echo "仓库：${REPO}"
echo "提交：${SHA}"
echo "镜像：${IMAGE}"
echo "REPO_URL：${REPO_URL}"

args=(
    build
    -f template/Dockerfile
    --build-arg "REPO_URL=${REPO_URL}"
    -t "${IMAGE}"
)
if [ "${NO_CACHE:-0}" = "1" ]; then
    args+=(--no-cache)
fi

docker "${args[@]}" .

echo "构建完成：${IMAGE}"
