import os
import sys

import uvicorn
from fastapi import FastAPI

from movieclaw_api.app import create_app
from movieclaw_api.core.config import get_settings
from movieclaw_api.core.logging import configure_logging
from movieclaw_api.services.app_config import (
    RESTART_EXIT_CODE,
    register_uvicorn_server,
    restart_requested,
)
from movieclaw_api.settings.app_server import RUNTIME_PORT_ENV, resolve_boot_port


def app() -> FastAPI:
    return create_app()


def run() -> None:
    settings = get_settings()
    # 启动 uvicorn 前先装配好根 logger：让「Started server process」等启动日志
    # 也进入按天落盘的日志文件（设置页「系统日志」的数据来源）。
    configure_logging(settings.log_level, settings.log_dir, settings.log_retention_days)
    # 监听端口三级解析：APP_PORT 环境变量 > 设置页落库值 > 默认 8000。
    # 实际生效端口记进环境变量，供设置页判断「改了端口是否还需重启」。
    port = resolve_boot_port(settings.database_url, settings.port)
    os.environ[RUNTIME_PORT_ENV] = str(port)
    # 两个分支的公共 uvicorn 参数：
    # - log_config=None：不让 uvicorn 接管日志配置，它的 logger 直接向根 logger
    #   传播，与业务日志走同一套「控制台 + 按天落盘」Handler、同一格式。
    # - access_log=False：uvicorn 自带的访问日志与 middleware.py 的重复，且后者
    #   更详细（含耗时）并受 APP_ACCESS_LOG_ENABLED 开关控制，故关掉前者。
    if settings.reload:
        # 开发热重载：沿用 uvicorn 的 reloader 进程模型（reloader 父进程 +
        # 应用子进程），拿不到应用子进程里的 Server 实例，设置页重启走
        # SIGTERM 退回路径即可——开发场景本就有热重载兜底。
        uvicorn.run(
            "movieclaw_api.main:app",
            factory=True,
            host=settings.host,
            port=port,
            reload=True,
            log_config=None,
            access_log=False,
        )
        return

    # 生产：自持 Server 实例并注册给重启服务（services/app_config）。
    # 设置页重启由此直接置 should_exit 优雅停机（不经信号投递，绕开 uvloop 下
    # signal.signal 处理器可能长时间不执行的问题），停机后以约定退出码 42
    # 告知 entrypoint「这是重启请求」——由其原地拉起新的后端进程；
    # 其他退出码（含 docker stop 的信号路径）仍是整容器退出。
    config = uvicorn.Config(
        "movieclaw_api.main:app",
        factory=True,
        host=settings.host,
        port=port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    register_uvicorn_server(server)
    server.run()
    if restart_requested():
        sys.exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    run()
