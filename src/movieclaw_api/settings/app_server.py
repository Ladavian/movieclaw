"""应用服务配置域（「设置 → 应用设置」页的数据模型）。

当前只有一个字段——外部访问地址（``external_url``）：从网络上能访问到本应用
的完整地址（如 ``http://192.168.1.10:3000`` 或反代后的
``https://movie.example.com``），供生成通知链接、回调地址等绝对 URL 的场景
使用（Agent 系统提示词的环境段也注入它来拼页面链接）。

为什么没有「端口」设置：用户视角的访问入口是前端（Docker 默认 3000，对外
端口由 compose 的 ports 映射决定），后端 8000 只在容器内被 Next 反代，
两者都不是后端进程能有意义地配置的——前端端口它控制不了，改自己的监听
端口对外部访问没有意义。后端监听端口如需调整（源码部署），用 ``APP_PORT``
环境变量。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

APP_SERVER_NAMESPACE = "app.server"


@register_setting(namespace=APP_SERVER_NAMESPACE, title="应用设置")
class AppServerSetting(SettingSchema):
    """应用服务配置：外部访问地址。

    历史字段说明：早期版本曾有 ``port``（后端监听端口）字段，后因「改后端
    端口对外部访问没有意义」而移除；基类 ``extra="ignore"`` 保证带旧字段的
    存量记录读取不报错。
    """

    external_url: str = Field(
        default="",
        description="网络可访问到本应用的完整地址（http/https），供生成绝对链接使用；空 = 未配置",
    )
