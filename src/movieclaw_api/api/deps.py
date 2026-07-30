from __future__ import annotations

import hmac

from fastapi import Cookie, Header

from movieclaw_api.exceptions import UnauthorizedException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.settings.schemas import get_sync_setting


def _extract_bearer(authorization: str | None) -> str | None:
    """从 Authorization 头中取出 Bearer 令牌；格式不符返回 None。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


async def require_sync_token(authorization: str | None = Header(default=None)) -> None:
    """插件侧接口的鉴权依赖：校验请求头里的同步令牌。

    校验流程：
    1. 后端从未生成令牌（同步未启用）→ 401，提示先去后台生成令牌。
    2. 请求未带 Bearer 令牌或与后端不一致 → 401，提示令牌无效/已重置。

    比较使用 ``hmac.compare_digest`` 做常量时间比较，避免时序侧信道。
    错误信息为清晰中文，方便非开发者按提示操作。
    """
    setting = await get_sync_setting()
    if not setting.token:
        raise UnauthorizedException("后端未启用同步，请先在后台生成令牌")

    provided = _extract_bearer(authorization)
    if not provided or not hmac.compare_digest(provided, setting.token):
        raise UnauthorizedException("令牌无效或已重置，请重新填写")


async def require_login(
    session_token: str | None = Cookie(default=None, alias=auth_service.SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> str:
    """业务接口的登录鉴权依赖：会话 Cookie **或** Bearer 令牌，返回身份标识。

    两条通道（docs/design/cli.md §8.1）：
    - Web 端：会话 Cookie（原有机制，不变）；
    - CLI / 产品内 Agent：``Authorization: Bearer <令牌>``——PAT 长期令牌
      或 Agent 短时效签名令牌，同一验签入口。

    全站默认拒绝的执行点——除公开白名单与插件侧接口外，所有路由都必须挂
    本依赖（api/router.py 按组挂载，tests 里有守护测试兜底防漏挂）。
    未登录 / 会话过期 / 令牌无效统一 401。
    """
    if session_token:
        return await auth_service.verify_session_token(session_token)
    if bearer := _extract_bearer(authorization):
        return await auth_service.verify_bearer_token(bearer)
    raise UnauthorizedException("未登录，请先登录")
