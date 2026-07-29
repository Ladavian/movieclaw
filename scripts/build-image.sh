#!/usr/bin/env bash
# =============================================================================
# movieclaw Docker 镜像构建脚本
#
# 用法：
#   ./scripts/build-image.sh                  # 构建 movieclaw:latest（本机架构）
#   TAG=v0.1.0 ./scripts/build-image.sh       # 指定标签
#   PLATFORM=linux/amd64 ./scripts/build-image.sh   # 交叉构建 NAS 常见的 x86_64
#   CN_MIRROR=1 ./scripts/build-image.sh      # 国内镜像源加速（pip 清华源 + npmmirror）
#
# TMDB Key 读取顺序：环境变量 TMDB_API_KEY > 仓库根目录 .env 中的 TMDB_API_KEY。
# Key 只通过 --build-arg 传入，不进入构建上下文（.env 已被 .dockerignore 排除）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TAG="${TAG:-latest}"
IMAGE="movieclaw:${TAG}"

# 读取 TMDB Key
TMDB_KEY="${TMDB_API_KEY:-}"
if [[ -z "$TMDB_KEY" && -f .env ]]; then
    TMDB_KEY="$(grep -E '^TMDB_API_KEY=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]"'"'" || true)"
fi
if [[ -z "$TMDB_KEY" ]]; then
    echo "错误：未找到 TMDB_API_KEY（设置环境变量，或写入仓库根目录 .env）" >&2
    exit 1
fi

# --shm-size：Docker 默认只给 /dev/shm 64MB，Next.js 构建的并行 worker
# 靠共享内存通信，页面数量上来后会静默死锁（日志停在 "Creating an optimized
# production build"、CPU 掉到空闲）。给足 2G 是这一现象的根治手段。
BUILD_ARGS=(--build-arg "TMDB_API_KEY=$TMDB_KEY" --shm-size=2g)

# 国内网络加速
if [[ "${CN_MIRROR:-0}" == "1" ]]; then
    BUILD_ARGS+=(
        --build-arg "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
        --build-arg "NPM_REGISTRY=https://registry.npmmirror.com"
        # apt 也要换源：ffmpeg 的依赖链很长，直连 deb.debian.org 经代理时
        # 常在半路 400/超时，整个构建就废在最后一层
        --build-arg "APT_MIRROR=mirrors.tuna.tsinghua.edu.cn"
    )
fi

# 交叉构建（如在 Apple Silicon 上给 x86_64 NAS 出镜像）
if [[ -n "${PLATFORM:-}" ]]; then
    BUILD_ARGS+=(--platform "$PLATFORM")
fi

# 离线构建：只用本地已有的基础镜像，不去 registry 查元数据。
# 适用于 Docker 守护进程出网不畅的环境（如 fake-ip 代理模式下，dockerd 自身
# 的请求不经宿主代理，会卡在解析出的 198.18.x 地址上）。
# 前提：node/python/debian 三个基础镜像本地已存在。
if [[ "${OFFLINE_BASE:-0}" == "1" ]]; then
    BUILD_ARGS+=(--pull=false)
fi

echo "构建 $IMAGE ……"
docker build "${BUILD_ARGS[@]}" -t "$IMAGE" .
echo "完成：$IMAGE"
