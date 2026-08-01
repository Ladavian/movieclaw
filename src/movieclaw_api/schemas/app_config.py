"""应用设置的请求/响应模型（「设置 → 应用设置」页）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AppConfigPayload(BaseModel):
    """保存请求体：与 ``AppServerSetting`` 配置域字段一一对应（整体覆盖语义）。"""

    port: int = Field(
        default=0,
        description="后端监听端口：0 = 未设置（跟随默认），否则 1024~65535；重启后生效",
    )
    external_url: str = Field(
        default="",
        description="网络可访问到本应用的完整地址（http/https），"
        "如 http://192.168.1.10:3000；空 = 未配置",
    )


class AppConfigView(AppConfigPayload):
    """读取响应：配置本体 + 前端渲染所需的运行时状态。"""

    default_port: int = Field(
        description="端口未设置时的生效默认值（APP_PORT 环境变量或内置 8000）"
    )
    runtime_port: int = Field(description="当前进程实际监听的端口")
    port_env_locked: bool = Field(
        description="端口是否已被 APP_PORT 环境变量钉死（Docker 部署即如此），"
        "为 true 时设置页端口不生效"
    )
    restart_required: bool = Field(
        description="已保存的端口与当前监听端口不一致，需要重启应用才能生效"
    )
