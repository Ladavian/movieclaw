"""应用设置的业务服务（routes/app_config 的实现层）。

职责：
- 配置视图装配：配置本体 + 端口生效状态（默认值 / 当前监听端口 / 是否被
  环境变量钉死 / 是否需重启）；
- 保存编排：校验（端口区间、外部地址格式）→ 落库；
- 重启调度：延迟片刻后向自身进程发 SIGTERM，uvicorn 优雅停机后由进程
  守护方拉起——Docker 部署下任一进程退出即整容器退出，配合
  ``restart: unless-stopped`` 自动重启；裸进程部署需 systemd 等守护，
  否则退出后须手动再启动（设置页有相应提示文案）。

端口为何不能热切换：uvicorn 在启动时一次性绑定监听端口，运行期无法换绑，
这是「改端口需重启」的根因；外部访问地址纯属落库数据，保存即生效。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from urllib.parse import urlsplit

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.app_config import AppConfigPayload, AppConfigView
from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_api.settings.app_server import RUNTIME_PORT_ENV

logger = logging.getLogger("movieclaw_api.app_config")

# 重启前的缓冲时间：留给 HTTP 响应写回客户端，避免前端拿不到「已开始重启」的确认
_RESTART_DELAY_SECONDS = 1.0
# 优雅停机的等待窗口：SIGTERM 后超过此时长仍未退出则强制退出。
# 必须有这个兜底——uvicorn 用 signal.signal 注册停机处理器，在 uvloop 的 C 事件
# 循环下信号处理可能长时间得不到执行（实测偶发），没有兜底重启会悬空。
_FORCE_EXIT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


def _runtime_port() -> int:
    """当前进程实际监听的端口（main.run 启动时记录；测试等场景回落配置值）。"""
    raw = os.environ.get(RUNTIME_PORT_ENV, "")
    return int(raw) if raw.isdigit() else get_settings().port


def _build_view(setting: AppServerSetting) -> AppConfigView:
    env_settings = get_settings()
    env_locked = "APP_PORT" in os.environ
    runtime_port = _runtime_port()
    # 已保存配置在下次启动时的生效端口：被环境变量钉死时始终是环境变量值
    effective_port = env_settings.port if env_locked else (setting.port or env_settings.port)
    return AppConfigView(
        port=setting.port,
        external_url=setting.external_url,
        default_port=env_settings.port,
        runtime_port=runtime_port,
        port_env_locked=env_locked,
        restart_required=effective_port != runtime_port,
    )


async def build_config_view() -> AppConfigView:
    """装配设置页所需的完整视图。"""
    setting = await get_setting_store().get(AppServerSetting)
    return _build_view(setting)


def _validate_payload(payload: AppConfigPayload) -> None:
    """保存前校验，错误信息中文直达前端。"""
    if payload.port != 0 and not 1024 <= payload.port <= 65535:
        raise BadRequestException(
            "端口必须在 1024~65535 之间（1024 以下为系统保留端口）；留空则使用默认端口"
        )
    url = payload.external_url.strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise BadRequestException(
                "外部访问地址必须是完整的 http(s) 地址，如 http://192.168.1.10:3000"
            )


async def save_config(payload: AppConfigPayload) -> AppConfigView:
    """校验并保存应用设置。外部地址保存即生效；端口改动需重启（见返回的 restart_required）。"""
    _validate_payload(payload)
    setting = AppServerSetting(
        port=payload.port,
        # 规范化：去掉尾部斜杠，后续拼接路径时不用再处理
        external_url=payload.external_url.strip().rstrip("/"),
    )
    await get_setting_store().set(setting)
    return _build_view(setting)


# ---------------------------------------------------------------------------
# 重启
# ---------------------------------------------------------------------------


def _terminate_self() -> None:
    """向自身进程发 SIGTERM 触发 uvicorn 优雅停机；超时未退出则强制退出。"""
    logger.info("正在按用户请求重启应用：优雅关闭当前进程，等待进程守护（Docker）拉起……")
    os.kill(os.getpid(), signal.SIGTERM)
    # 优雅停机成功时进程直接消失，走不到下面；只有信号被事件循环拖住才会兜底
    time.sleep(_FORCE_EXIT_SECONDS)
    logger.warning("优雅停机超时（%.0f 秒），强制退出进程以完成重启", _FORCE_EXIT_SECONDS)
    logging.shutdown()  # 强制退出不走解释器清理，先冲刷日志缓冲，保住上面这行警告
    os._exit(1)


def schedule_restart() -> None:
    """调度一次应用重启：延迟片刻（先让响应回到前端）后优雅退出进程。"""
    timer = threading.Timer(_RESTART_DELAY_SECONDS, _terminate_self)
    timer.daemon = True
    timer.start()
