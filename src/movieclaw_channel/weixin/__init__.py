"""微信 iLink Bot 网关适配器。

对接腾讯官方 iLink 机器人开放网关(HTTPS + JSON),不逆向微信客户端协议:

- ``client``   iLink CGI 客户端(getupdates 长轮询 / sendmessage / 二维码接口);
- ``adapter``  实现通道协议的 WeixinAdapter(收消息循环 + 发文本);
- ``binding``  扫码绑定状态机(Web 化:challenge 注册表 + 后台轮询任务)。

协议参考:Tencent/openclaw-weixin 开源实现(MIT)。
"""

from movieclaw_channel.weixin.adapter import WeixinAdapter
from movieclaw_channel.weixin.binding import (
    BindingChallenge,
    BindingResult,
    WeixinBindingRegistry,
)
from movieclaw_channel.weixin.client import DEFAULT_BASE_URL, WeixinClient

__all__ = [
    "DEFAULT_BASE_URL",
    "BindingChallenge",
    "BindingResult",
    "WeixinAdapter",
    "WeixinBindingRegistry",
    "WeixinClient",
]
