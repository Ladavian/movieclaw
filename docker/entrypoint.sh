#!/bin/bash
# =============================================================================
# movieclaw 容器入口：一个容器同时跑 FastAPI 后端和 Next.js 前端。
#
# 进程模型：
#   - 后端监听容器内 8000（不对外），前端监听 3000（对外唯一端口）
#   - /api/v1 请求由 Next 服务器反代到后端（构建时固化的 rewrite 规则）
#   - 任意一个进程退出，整个容器退出（交给 Docker 的 restart 策略拉起），
#     避免出现"半死"状态：前端活着但后端已挂
#   - 例外一：后端以约定码 42 退出表示「设置页请求的重启」，原地拉起新的
#     后端进程（前端不中断）
#   - 例外二：后端以约定码 43 退出表示「应用内更新/回退后的全量重启」，
#     前后端一起重启，并重新解析代码来源（可能切到新的 overlay 版本）
#
# 代码来源解析（应用内更新机制，docs/design/in-app-update.md）：
#   镜像内 /app/src、/app/web 是构建时烧入的基线，永远完整可运行；
#   /app/data/updates/versions/<ver>/ 是应用内更新下载的 overlay（data 卷上，
#   容器重建不丢）。本脚本启动时按 current → previous → 基线 的顺序解析出
#   实际启动的代码目录——更新从不覆盖镜像内文件，只改变启动指向。
#   overlay 必须通过完整性与 runtime 兼容校验（requires_runtime 与镜像的
#   /etc/movieclaw-runtime 一致）才会被采用；启动后短时间内连续失败 2 次的
#   overlay 会被标记为 bad 并自动回落，保证坏更新永远不会让容器起不来。
#
# 本脚本烧在镜像里、无法应用内更新，因此只保留最小且稳定的逻辑：
# 解析启动指向、拉起进程、处理重启约定码与失败兜底。版本相关的复杂逻辑
# （下载/校验/切换/回退）都在可更新的后端代码里（services/app_update.py）。
#
# 测试钩子：`entrypoint.sh resolve` 只打印解析结果不拉进程；路径可用
# MOVIECLAW_APP_ROOT / MOVIECLAW_DATA_DIR / MOVIECLAW_RUNTIME_FILE 覆盖，
# 供 tests/docker/ 在临时目录里验证解析矩阵。
# =============================================================================
set -euo pipefail

APP_ROOT="${MOVIECLAW_APP_ROOT:-/app}"
DATA_DIR="${MOVIECLAW_DATA_DIR:-$APP_ROOT/data}"
RUNTIME_FILE="${MOVIECLAW_RUNTIME_FILE:-/etc/movieclaw-runtime}"
UPDATES_DIR="$DATA_DIR/updates"
STATE_DIR="$UPDATES_DIR/state"
# overlay 启动失败的判定窗口与次数：启动后不足 GRACE 秒即退出算「启动失败」，
# 同一版本连续失败 MAX 次即标记 bad 并回落
STARTUP_GRACE_SECONDS="${MOVIECLAW_STARTUP_GRACE_SECONDS:-60}"
MAX_STARTUP_FAILURES=2

PYTHON_BIN="${MOVIECLAW_PYTHON_BIN:-/venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

# 镜像的运行时版本（依赖集合的代号，见 docker/runtime-version）。
# 读不到时置 0：任何 overlay 都不会匹配，等价于禁用 overlay、只走基线。
if [ -r "$RUNTIME_FILE" ]; then
    RUNTIME_VERSION="$(tr -d '[:space:]' < "$RUNTIME_FILE")"
else
    RUNTIME_VERSION=0
fi
export MOVIECLAW_RUNTIME_VERSION="$RUNTIME_VERSION"

# ---------------------------------------------------------------------------
# overlay 解析
# ---------------------------------------------------------------------------

# 读 manifest.json 的一个字段；文件缺失/损坏一律输出空串（调用方按无效处理）
read_manifest_field() {
    "$PYTHON_BIN" - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    value = json.load(open(sys.argv[1])).get(sys.argv[2], "")
    print("" if value is None else value)
except Exception:
    pass
PY
}

# 版本号转文件名安全形式（bad 标记 / 失败计数的文件名）
sanitize() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'
}

# 校验一个 overlay 目录是否可用；可用则输出其版本号，否则输出空
overlay_version_if_valid() {
    local dir="$1"
    [ -d "$dir" ] || return 0
    local manifest="$dir/manifest.json"
    # 布局完整性：后端入口、迁移配置、前端入口缺一不可
    local f
    for f in "$manifest" "$dir/backend/src/movieclaw_api/main.py" \
             "$dir/backend/alembic.ini" "$dir/web/apps/web/server.js"; do
        if [ ! -f "$f" ]; then
            echo "[entrypoint] overlay $dir 缺少 $f，忽略该版本" >&2
            return 0
        fi
    done
    local version requires
    version="$(read_manifest_field "$manifest" version)"
    requires="$(read_manifest_field "$manifest" requires_runtime)"
    if [ -z "$version" ] || [ -z "$requires" ]; then
        echo "[entrypoint] overlay $dir 的 manifest.json 不完整，忽略该版本" >&2
        return 0
    fi
    if [ "$requires" != "$RUNTIME_VERSION" ]; then
        echo "[entrypoint] overlay v$version 需要 runtime=$requires，镜像为 runtime=$RUNTIME_VERSION，忽略（需升级 Docker 镜像）" >&2
        return 0
    fi
    if [ -f "$STATE_DIR/bad-$(sanitize "$version")" ]; then
        echo "[entrypoint] overlay v$version 曾连续启动失败已标记为坏版本，忽略" >&2
        return 0
    fi
    echo "$version"
}

# 解析启动指向，结果写入全局变量并导出给子进程（后端与 Agent 都靠这些感知）：
#   ACTIVE_SOURCE=overlay|baseline  ACTIVE_VERSION（overlay 时非空）
#   BACKEND_ROOT（含 src/ alembic/ alembic.ini 的项目根） WEB_ROOT（含 apps/web/server.js）
resolve_code() {
    ACTIVE_SOURCE=baseline
    ACTIVE_VERSION=""
    BACKEND_ROOT="$APP_ROOT"
    WEB_ROOT="$APP_ROOT/web"
    local link target version
    for link in current previous; do
        target="$UPDATES_DIR/$link"
        [ -e "$target" ] || continue
        version="$(overlay_version_if_valid "$target")"
        if [ -n "$version" ]; then
            ACTIVE_SOURCE=overlay
            ACTIVE_VERSION="$version"
            BACKEND_ROOT="$(readlink -f "$target")/backend"
            WEB_ROOT="$(readlink -f "$target")/web"
            if [ "$link" = "previous" ]; then
                echo "[entrypoint] current 版本不可用，回退使用上一版本 v$version" >&2
            fi
            break
        fi
    done

    export MOVIECLAW_CODE_SOURCE="$ACTIVE_SOURCE"
    export MOVIECLAW_CODE_ROOT="$BACKEND_ROOT"
    # 更新/模型目录以 entrypoint 的 DATA_DIR 为唯一事实源导出给后端：
    # 后端配置里的同名变量默认值只是「约定一致」，显式导出彻底堵死
    # 「更新装到 A 目录、启动解析 B 目录」的 split-brain（哪怕用户只覆盖了其一）
    export MOVIECLAW_UPDATES_DIR="$UPDATES_DIR"
    export MOVIECLAW_MODELS_DIR="$DATA_DIR/models/ner"
    if [ -n "$ACTIVE_VERSION" ]; then
        export MOVIECLAW_OVERLAY_VERSION="$ACTIVE_VERSION"
    else
        unset MOVIECLAW_OVERLAY_VERSION || true
    fi

    # NER 模型指针：data 卷上有完整的模型目录则用它（应用内模型更新），
    # 否则回落镜像内置模型（Dockerfile 的 ENV MOVIECLAW_NER_DIR）
    local model_dir="$DATA_DIR/models/ner/current"
    if [ -f "$model_dir/model.int8.onnx" ] && [ -f "$model_dir/tokenizer.json" ] \
        && [ -f "$model_dir/labels.json" ]; then
        MOVIECLAW_NER_DIR="$(readlink -f "$model_dir")"
        export MOVIECLAW_NER_DIR
    fi
}

# 测试钩子：只解析并打印结果，不拉起任何进程
if [ "${1:-}" = "resolve" ]; then
    resolve_code
    echo "source=$ACTIVE_SOURCE"
    echo "version=$ACTIVE_VERSION"
    echo "backend_root=$BACKEND_ROOT"
    echo "web_root=$WEB_ROOT"
    echo "runtime=$RUNTIME_VERSION"
    echo "ner_dir=${MOVIECLAW_NER_DIR:-}"
    exit 0
fi

# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------

cd "$APP_ROOT"

# 数据库迁移由后端启动时自动执行（movieclaw_db/migrations.py），无需在此处理

# 容器内后端端口显式钉死为 8000：它是 Next 反代目标（构建时固化），
# 绝不能被「设置 → 应用设置」里的端口改动影响；容器对外端口请改 compose 的
# ports 映射。设置页会据此环境变量提示「端口已由容器管理」。
start_api() {
    echo "[entrypoint] 启动后端 (FastAPI, 127.0.0.1:8000)……来源：$ACTIVE_SOURCE${ACTIVE_VERSION:+ v$ACTIVE_VERSION}"
    API_START_TS="$(date +%s)"
    PYTHONPATH="$BACKEND_ROOT/src" APP_PORT=8000 "$PYTHON_BIN" -m movieclaw_api.main &
    API_PID=$!
}

start_web() {
    echo "[entrypoint] 启动前端 (Next.js, 0.0.0.0:3000)……来源：$ACTIVE_SOURCE${ACTIVE_VERSION:+ v$ACTIVE_VERSION}"
    WEB_START_TS="$(date +%s)"
    PORT=3000 HOSTNAME=0.0.0.0 node "$WEB_ROOT/apps/web/server.js" &
    WEB_PID=$!
}

start_all() {
    resolve_code
    start_api
    start_web
}

kill_remaining() {
    kill "$API_PID" "$WEB_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
    wait "$WEB_PID" 2>/dev/null || true
}

# overlay 启动失败兜底：短时间内退出计一次失败，连续 MAX 次标记 bad。
# 返回 0 表示「已处理，调用方应重启全部进程再试」；返回 1 表示按真故障处理。
# 只对 overlay 生效——基线是镜像烧入的，起不来属于环境问题，必须外显。
handle_startup_failure() {
    local uptime="$1"
    if [ "$ACTIVE_SOURCE" != "overlay" ]; then
        return 1
    fi
    local marker
    marker="$(sanitize "$ACTIVE_VERSION")"
    if [ "$uptime" -ge "$STARTUP_GRACE_SECONDS" ]; then
        # 稳定运行过一段时间后才挂：不是坏更新，清掉启动失败计数，按真故障处理
        rm -f "$STATE_DIR/failures-$marker"
        return 1
    fi
    mkdir -p "$STATE_DIR"
    local fail_file="$STATE_DIR/failures-$marker"
    # 只统计时间窗内的失败（1 小时）：不洁关机（断电/OOM/docker kill）没有
    # wake 事件来清零计数，陈旧记录若被原样累计，相隔数周的两次孤立故障
    # 会把好版本误标成坏版本——按时间戳过滤让「连续」语义不依赖清零时机
    local now cutoff
    now="$(date +%s)"
    cutoff=$(( now - 3600 ))
    if [ -f "$fail_file" ]; then
        awk -v cutoff="$cutoff" '$1 >= cutoff' "$fail_file" > "$fail_file.tmp" \
            && mv "$fail_file.tmp" "$fail_file"
    fi
    echo "$now" >> "$fail_file"
    local count
    count="$(wc -l < "$fail_file")"
    if [ "$count" -ge "$MAX_STARTUP_FAILURES" ]; then
        touch "$STATE_DIR/bad-$marker"
        echo "[entrypoint] overlay v$ACTIVE_VERSION 启动后 ${uptime}s 内退出，已连续失败 $count 次：标记为坏版本并回落。" >&2
    else
        echo "[entrypoint] overlay v$ACTIVE_VERSION 启动后 ${uptime}s 内退出（第 $count 次），重试……" >&2
    fi
    return 0
}

# 收到停止信号时把两个子进程都带走，确保容器干净退出。
# SHUTTING_DOWN 标志让主循环把「停机导致的进程退出」与故障区分开——
# 否则 overlay 启动后 60 秒内 docker stop 会被误计为一次「启动失败」，
# 连续两次正常停容器就可能把好版本错标成坏版本。
# trap 必须先于 start_all 安装：启动窗口内到达的 TERM 不能被 PID 1 默认忽略。
SHUTTING_DOWN=0
shutdown() {
    SHUTTING_DOWN=1
    kill "${API_PID:-}" "${WEB_PID:-}" 2>/dev/null || true
}
trap shutdown TERM INT

start_all

# 启动失败计数的衰减：进程稳定运行超过宽限期后清零该版本的计数。
# 否则计数在 data 卷上跨周跨月累计，两次相隔很久的孤立故障会把好版本
# 误标成坏版本——设计语义是「连续」失败，不是「累计」。
clear_failures_if_seasoned() {
    if [ "$ACTIVE_SOURCE" = "overlay" ] && [ -n "$ACTIVE_VERSION" ] \
        && [ "$(( NOW - API_START_TS ))" -ge "$STARTUP_GRACE_SECONDS" ] \
        && [ "$(( NOW - WEB_START_TS ))" -ge "$STARTUP_GRACE_SECONDS" ]; then
        rm -f "$STATE_DIR/failures-$(sanitize "$ACTIVE_VERSION")"
    fi
}

# 主循环：处理重启约定码（42 后端 / 43 全量）、overlay 启动失败兜底、
# 真故障与停机（整容器退出，交给 Docker 的 restart 策略与 healthcheck，
# 不在容器内静默兜养）。
# EXIT_CODE 预置为 143（SIGTERM 的约定码）：TERM 落在首次 wait 之前时主循环
# 会在顶部直接 break，此时若变量未定义，set -u 会让脚本在结尾崩掉、跳过收尾
EXIT_CODE=143
while true; do
    if [ "$SHUTTING_DOWN" -eq 1 ]; then
        break # 停机信号在分支处理期间到达：不再进入 wait（新进程由结尾的 shutdown 收拾）
    fi
    # bash ≤5.2 的 wait -n 会忽略「调用前已被收割」的进程（打印 no such job
    # 后继续等另一个），特定竞态下会漏掉一侧进程的死亡。先同步探测再兜底：
    # 「&& … || …」写法让非零退出码不触发顶部的 set -e（否则脚本在此直接终止）
    if ! kill -0 "$API_PID" 2>/dev/null; then
        wait "$API_PID" && EXIT_CODE=0 || EXIT_CODE=$?
    elif ! kill -0 "$WEB_PID" 2>/dev/null; then
        wait "$WEB_PID" && EXIT_CODE=0 || EXIT_CODE=$?
    else
        wait -n "$API_PID" "$WEB_PID" && EXIT_CODE=0 || EXIT_CODE=$?
    fi
    NOW="$(date +%s)"
    clear_failures_if_seasoned
    if [ "$SHUTTING_DOWN" -eq 1 ]; then
        break # 停机信号已到：无论进程处于什么状态都直接走停机流程
    fi
    if kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; then
        break # 两个进程都活着 = wait 被停止信号打断（docker stop）：走停机流程
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        # 后端退出：先看重启约定码
        if [ "$EXIT_CODE" -eq 42 ]; then
            echo "[entrypoint] 后端请求重启（exit=42），正在拉起新的后端进程……"
            start_api
            continue
        fi
        if [ "$EXIT_CODE" -eq 43 ]; then
            echo "[entrypoint] 应用请求全量重启（exit=43），重新解析代码来源并重启前后端……"
            kill "$WEB_PID" 2>/dev/null || true
            wait "$WEB_PID" 2>/dev/null || true
            start_all
            continue
        fi
        if handle_startup_failure "$(( NOW - API_START_TS ))"; then
            kill_remaining
            start_all
            continue
        fi
        break # 后端真故障：结束容器
    fi
    # 前端退出
    if handle_startup_failure "$(( NOW - WEB_START_TS ))"; then
        kill_remaining
        start_all
        continue
    fi
    break # 前端真故障：结束容器
done
echo "[entrypoint] 有进程退出（exit=$EXIT_CODE），停止容器……"
shutdown
wait || true
exit "$EXIT_CODE"
