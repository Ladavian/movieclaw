import logging
import time

from fastapi import FastAPI, Request

from movieclaw_api.core.config import Settings
from movieclaw_api.spec_state import SPEC_HASH_HEADER, get_spec_hash

logger = logging.getLogger("movieclaw_api.access")


def register_middlewares(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def spec_hash_header_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """所有 API 响应携带 spec 指纹头，CLI 借此零成本发现版本偏斜。"""
        response = await call_next(request)
        if request.url.path.startswith(settings.api_v1_prefix):
            response.headers[SPEC_HASH_HEADER] = get_spec_hash(request.app)
        return response

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        started_at = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            if settings.access_log_enabled:
                logger.error(
                    "method=%s path=%s status_code=%s duration_ms=%.2f message=%s",
                    request.method,
                    request.url.path,
                    500,
                    duration_ms,
                    "request failed",
                )
            raise

        duration_ms = (time.perf_counter() - started_at) * 1000

        if settings.access_log_enabled:
            log = logger.info
            if response.status_code >= 500:
                log = logger.error
            elif response.status_code >= 400:
                log = logger.warning

            log(
                "method=%s path=%s status_code=%s duration_ms=%.2f message=%s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                "request completed",
            )

        return response
