"""网络与代理设置的请求/响应模型（「设置 → 网络」页）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EgressServiceOption(BaseModel):
    """设置页「走代理」开关列表里的一项。"""

    id: str
    label: str
    description: str = ""


class NetworkConfigPayload(BaseModel):
    """保存请求体：与配置域字段一一对应。"""

    proxy_mode: Literal["off", "env", "manual"] = "env"
    proxy_url: str = ""
    proxy_services: list[str] = Field(default_factory=list)
    tmdb_api_base_url: str = ""
    tmdb_image_base_url: str = ""
    douban_api_base_url: str = ""


class NetworkConfigView(NetworkConfigPayload):
    """读取响应：配置本体 + 前端渲染所需的目录与默认值。"""

    services: list[EgressServiceOption] = Field(default_factory=list)
    mirror_defaults: dict[str, str] = Field(
        default_factory=dict, description="三个镜像地址的生效默认值（设置为空时的回落）"
    )
    env_proxy_detected: str = Field(
        default="", description="环境变量中探测到的代理地址；env 模式下供用户确认"
    )


class NetworkTestPayload(BaseModel):
    service: str = Field(min_length=1, max_length=100)


class NetworkTestResult(BaseModel):
    ok: bool
    latency_ms: int | None = None
    message: str
