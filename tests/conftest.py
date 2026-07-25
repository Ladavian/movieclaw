"""测试全局配置：从仓库根目录的 .env 加载环境变量。

集成测试使用的真实站点 Cookie 通过环境变量传入，避免敏感凭据进入
git 历史。本地开发时复制 .env.example 为 .env 并填写即可，CI 环境
中保持环境变量为空，相关测试会自动跳过。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

# 测试期间产生的运行日志统一写进临时目录：create_app 会装配按天落盘的
# 日志 Handler（见 core/logging.py），不隔离的话测试会在仓库 data/logs
# 下留下日志文件。先于 .env 加载设置，测试环境始终生效。
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="movieclaw-test-logs-"))


def _load_env_file(path: Path) -> None:
    """加载简单的 .env 文件，将 KEY=VALUE 注入 os.environ。

    设计要点：
    - 已存在的环境变量优先，不会被 .env 覆盖（CI/手动 export 的值更权威）；
    - 仅支持 KEY=VALUE 形式，忽略空行与 # 开头的注释；
    - 自动剥离值两端的成对单/双引号，方便粘贴含分号的 cookie 串。

    避免新增依赖（python-dotenv），保持测试侧零依赖。
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(_ENV_FILE)


import pytest  # noqa: E402  须在环境变量装配之后导入


@pytest.fixture(autouse=True)
def _mute_instant_search_kick(monkeypatch):
    """默认打桩订阅写操作触发的即时缺口搜索（fire-and-forget）。

    service 层收口后，创建/调整/恢复/缺失重下都会踢一次 search_wanted；
    单测环境没有可用站点，放任它跑会写入"搜索失败"活动、污染断言。
    需要验证搜索行为的测试请显式 ``await search_wanted()``（管线测试即如此）；
    需要验证"踢了没踢"的测试可再次 monkeypatch 覆盖本桩。
    """
    monkeypatch.setattr(
        "movieclaw_api.services.wanted_search.kick_search_soon", lambda: None
    )


@pytest.fixture(autouse=True)
def _offline_image_proxy(monkeypatch):
    """刮削管线的图片资产下载在测试环境一律快速失败（不访问外网图床）。

    管线对单张失败本就优雅降级（字段保持 NULL、下次刷新自愈），这里只是
    把"真实网络超时"换成即时异常，保证测试快速且不依赖外网。直接构造
    ImageProxy 实例（注入 MockTransport）的专项测试不经单例，不受影响。
    """

    class _Offline:
        async def fetch(self, url: str):
            raise RuntimeError("测试环境不访问外网图床")

    monkeypatch.setattr(
        "movieclaw_api.services.image_proxy.get_image_proxy", lambda: _Offline()
    )
