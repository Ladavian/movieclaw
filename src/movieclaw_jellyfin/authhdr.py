"""``Authorization: MediaBrowser ...`` 头解析（设计文档 4.1）。

照抄 Jellyfin AuthorizationContext.cs:229-317 的状态机语义，而非简单 split：
- 头值无空格 → 整头作废；scheme 取第一个空格前部分，MediaBrowser/Emby
  大小写不敏感；
- 引号内的逗号不是分隔符（``x="123,123"`` → 值 ``123,123``）；
- 键 Trim 去空白且**大小写敏感**（精确 Client/Device/DeviceId/Version/Token）；
- 值先去首尾引号再 URL 解码；空值片段丢弃；值不要求带引号。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote_plus

_SCHEMES = ("mediabrowser", "emby")


@dataclass(frozen=True)
class AuthorizationInfo:
    """从头里解析出的客户端标识。缺失的键为空串。"""

    client: str = ""
    device: str = ""
    device_id: str = ""
    version: str = ""
    token: str = ""

    @property
    def has_identity(self) -> bool:
        """登录接口要求四键齐全（缺任一 → 400，SessionManager.cs:1643-1646）。"""
        return bool(self.client and self.device and self.device_id and self.version)


def parse_authorization_header(header: str | None) -> AuthorizationInfo | None:
    """解析 Authorization / X-Emby-Authorization 头；scheme 不认识返回 None。"""
    if not header:
        return None
    space = header.find(" ")
    if space < 0:
        return None
    if header[:space].lower() not in _SCHEMES:
        return None

    parts = _split_parts(header[space + 1 :])
    return AuthorizationInfo(
        client=parts.get("Client", ""),
        device=parts.get("Device", ""),
        device_id=parts.get("DeviceId", ""),
        version=parts.get("Version", ""),
        token=parts.get("Token", ""),
    )


def _split_parts(value: str) -> dict[str, str]:
    """键值对切分状态机（AuthorizationContext.GetParts 的 Python 移植）。

    `escaped` 在遇到引号时翻转、在引号内遇到逗号时保持——引号内的逗号
    不作分隔符；键区分大小写。
    """
    result: dict[str, str] = {}
    escaped = False
    key = ""
    start = 0
    for i, ch in enumerate(value):
        if ch in ('"', ","):
            # 引号翻转 escape 态；引号内的逗号维持 escape 态（原文的 XOR 写法）
            escaped = (not escaped) == (ch == '"')
            if ch == "," and not escaped:
                if start < i and key:
                    result[key.strip()] = unquote_plus(value[start:i].strip('"'))
                key = ""
                start = i + 1
        elif not escaped and ch == "=":
            key = value[start:i]
            start = i + 1
    if start < len(value) and key:
        result[key.strip()] = unquote_plus(value[start:].strip('"'))
    return result
