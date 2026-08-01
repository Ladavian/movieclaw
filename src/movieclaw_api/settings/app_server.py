"""应用服务配置域（「设置 → 应用设置」页的数据模型）。

两个字段对应设置页的两块内容：
1. 监听端口（``port``）：后端 HTTP 服务的监听端口，0 = 未设置、跟随默认
   （``APP_PORT`` 环境变量或内置 8000）。**改动需重启后生效**——端口在
   uvicorn 启动时一次性绑定，无法热切换。
2. 外部访问地址（``external_url``）：从网络上能访问到本应用的完整地址
   （如 ``http://192.168.1.10:3000`` 或反代后的 ``https://movie.example.com``），
   供后续生成通知链接、回调地址等绝对 URL 的场景使用。

端口的生效优先级（``resolve_boot_port``）
----------------------------------------
    显式设置的 APP_PORT 环境变量  >  本配置域落库的端口  >  内置默认 8000

Docker 镜像的 entrypoint 显式钉死 ``APP_PORT=8000``：容器内后端端口是
Next 反代的固化目标，绝不能被设置页改动；容器对外端口请改 compose 的
ports 映射。因此本设置实际面向源码/裸机部署者。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

logger = logging.getLogger("movieclaw_api.settings.app_server")

APP_SERVER_NAMESPACE = "app.server"

# 启动时实际监听端口的记录位（环境变量形式：dev 热重载的子进程也能继承），
# 供设置页对比「已保存端口 ≠ 当前监听端口」判断是否需要重启。
RUNTIME_PORT_ENV = "MOVIECLAW_RUNTIME_PORT"


@register_setting(namespace=APP_SERVER_NAMESPACE, title="应用设置")
class AppServerSetting(SettingSchema):
    """应用服务配置：监听端口 + 外部访问地址。"""

    port: int = Field(
        default=0,
        description="后端监听端口；0 = 未设置（跟随 APP_PORT 环境变量或默认 8000），重启后生效",
    )
    external_url: str = Field(
        default="",
        description="网络可访问到本应用的完整地址（http/https），供生成绝对链接使用；空 = 未配置",
    )


def resolve_boot_port(database_url: str, default_port: int) -> int:
    """启动阶段解析实际监听端口（在 uvicorn 绑定端口前调用，见 main.run）。

    此时应用上下文（SettingStore / 全局 Database）尚未初始化，故用一次性
    引擎直查 ``app_setting`` 表。首次启动表还不存在、或数据库暂不可达时，
    静默回落默认端口——端口解析失败绝不能挡住应用启动。
    """
    if "APP_PORT" in os.environ:
        # 部署者显式钉死端口（Docker entrypoint 即如此），库里的设置不生效
        return default_port

    port = _read_db_port(database_url)
    if port is not None:
        logger.info("使用设置页配置的监听端口：%d（来自数据库 app.server）", port)
        return port
    return default_port


def _read_db_port(database_url: str) -> int | None:
    """从数据库读取已落库的端口设置；无记录/未设置/读取失败返回 None。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _read() -> int | None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT value_json FROM app_setting WHERE namespace = :ns"),
                        {"ns": APP_SERVER_NAMESPACE},
                    )
                ).first()
        finally:
            await engine.dispose()
        if row is None:
            return None
        port = json.loads(row[0]).get("port")
        return port if isinstance(port, int) and port > 0 else None

    try:
        return asyncio.run(_read())
    except Exception as exc:  # noqa: BLE001
        logger.info("启动时读取端口设置失败（首次启动属正常现象），使用默认端口：%s", exc)
        return None
