#!/bin/bash
# =============================================================================
# movieclaw 容器入口：一个容器同时跑 FastAPI 后端和 Next.js 前端。
#
# 进程模型：
#   - 后端监听容器内 8000（不对外），前端监听 3000（对外唯一端口）
#   - /api/v1 请求由 Next 服务器反代到后端（构建时固化的 rewrite 规则）
#   - 任意一个进程退出，整个容器退出（交给 Docker 的 restart 策略拉起），
#     避免出现"半死"状态：前端活着但后端已挂
#   - 唯一例外：后端以约定码 42 退出表示「设置页请求的重启」，本脚本原地
#     拉起新的后端进程（前端不中断），重启不依赖用户配没配 restart 策略
# =============================================================================
set -euo pipefail

cd /app

# 数据库迁移由后端启动时自动执行（movieclaw_db/migrations.py），无需在此处理

# 容器内后端端口显式钉死为 8000：它是 Next 反代目标（构建时固化），
# 绝不能被「设置 → 应用设置」里的端口改动影响；容器对外端口请改 compose 的
# ports 映射。设置页会据此环境变量提示「端口已由容器管理」。
start_api() {
    echo "[entrypoint] 启动后端 (FastAPI, 127.0.0.1:8000)……"
    APP_PORT=8000 python -m movieclaw_api.main &
    API_PID=$!
}
start_api

echo "[entrypoint] 启动前端 (Next.js, 0.0.0.0:3000)……"
PORT=3000 HOSTNAME=0.0.0.0 node /app/web/apps/web/server.js &
WEB_PID=$!

# 收到停止信号时把两个子进程都带走，确保容器干净退出
shutdown() {
    kill "$API_PID" "$WEB_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# 后端重启循环：退出码 42 是「设置页请求的应用重启」的约定码（见
# services/app_config.py 的 RESTART_EXIT_CODE），原地拉起新的后端进程、
# 前端不中断，重启不依赖 Docker 的 restart 策略。其他任何退出（前端挂、
# 后端真故障、docker stop）仍旧结束整个容器——故障必须外显为容器退出，
# 交给 Docker 的 restart 策略与 healthcheck 处理，不在容器内静默兜养。
while true; do
    # 「&& … || …」写法让非零退出码不触发顶部的 set -e（否则脚本在此直接终止）
    wait -n "$API_PID" "$WEB_PID" && EXIT_CODE=0 || EXIT_CODE=$?
    if ! kill -0 "$WEB_PID" 2>/dev/null; then
        break # 前端退出：结束容器
    fi
    if kill -0 "$API_PID" 2>/dev/null; then
        break # 两个进程都活着 = wait 被停止信号打断（docker stop）：走停机流程
    fi
    if [ "$EXIT_CODE" -eq 42 ]; then
        echo "[entrypoint] 后端请求重启（exit=42），正在拉起新的后端进程……"
        start_api
        continue
    fi
    break # 后端非重启码退出：真故障，结束容器
done
echo "[entrypoint] 有进程退出（exit=$EXIT_CODE），停止容器……"
shutdown
wait || true
exit "$EXIT_CODE"
