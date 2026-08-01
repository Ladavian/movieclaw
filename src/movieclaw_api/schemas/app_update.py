"""应用内更新的接口模型（docs/design/in-app-update.md M3）。"""

from __future__ import annotations

from pydantic import BaseModel


class UpdateStatusView(BaseModel):
    """「设置 → 关于与更新」的状态区。"""

    #: 当前运行的应用版本（代码自带的 __version__，overlay 与基线各有各的值）
    current_version: str
    #: 代码来源：baseline（镜像基线）/ overlay（应用内更新版本）/ dev（源码部署）
    code_source: str
    #: overlay 生效时的版本号；基线/开发环境为 None
    overlay_version: str | None
    #: 镜像的运行时版本（依赖集合代号）；非 Docker 部署为 None
    runtime_version: int | None
    #: 是否支持应用内更新（只有 Docker entrypoint 环境才支持；源码部署请 git pull）
    can_update: bool
    #: 是否存在可回退的上一版本
    has_previous: bool
    #: 被标记为「连续启动失败」的坏版本列表（供 UI 外显兜底事件）
    bad_versions: list[str]


class UpdateCheckView(BaseModel):
    """「检查更新」的结果。"""

    current_version: str
    latest_version: str
    #: 最新版是否比当前新
    update_available: bool
    #: 最新版的 requires_runtime 是否与本镜像匹配；False = 需升级 Docker 镜像
    compatible: bool
    #: 最新版要求的运行时版本
    requires_runtime: int
    #: Release 页的更新说明（GitHub Release body，Markdown）
    changelog: str
    #: Release 发布时间（ISO 8601）
    published_at: str


class UpdateProgressView(BaseModel):
    """更新执行进度（前端轮询）。"""

    #: idle / checking / downloading / verifying / applying / restarting / failed
    phase: str
    #: 当前步骤的中文描述（直接展示给用户）
    detail: str
    #: 下载进度百分比（仅 downloading 阶段有值）
    percent: float | None
    #: 失败时的中文错误信息
    error: str | None
    #: 本次更新的目标版本
    target_version: str | None
