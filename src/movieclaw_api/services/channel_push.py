"""跨通道主动推送:把系统事件文本扇出到所有已绑定的 IM 通道。

调用方(订阅投递/入库对账等)只管 fire-and-forget:``notify_channels(text)``
内部起后台任务,任何通道失败都只记日志,绝不影响业务主链路。

微信通道的绑定用户与 TG/Discord 一样从 channel_account.bound_user_id 取,
出站统一经各账号 dispatcher 的发送泵(顺序与限流集中一处)。
"""

from __future__ import annotations

import asyncio
import logging

from movieclaw_channel.types import OutboundEnvelope, ReplyContext
from movieclaw_db.engine import get_database
from movieclaw_db.models.channel_account import ChannelAccountStatus
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

logger = logging.getLogger("movieclaw_api.channel_push")


async def push_to_all_channels(text: str) -> int:
    """推送到所有通道(微信 + Telegram + Discord),返回入队的账号数。"""
    count = 0

    # IM 通道(telegram/discord):服务内存里有现成的推送地址簿
    try:
        from movieclaw_api.services.im_channel import get_im_channels

        count += await get_im_channels().push_text(text)
    except RuntimeError:
        pass  # 服务未初始化(启动早期/测试)

    # 微信通道:从库里取绑定用户,经运行中的 dispatcher 入队
    try:
        from movieclaw_api.services.weixin_channel import get_weixin_channel
        from movieclaw_channel.weixin.adapter import CHANNEL_ID as WEIXIN_CHANNEL_ID

        service = get_weixin_channel()
        async with get_database().session() as session:
            rows = await ChannelAccountRepository(session).list_by_channel(WEIXIN_CHANNEL_ID)
        for row in rows:
            bound = (row.bound_user_id or "").strip()
            if not bound or row.status != ChannelAccountStatus.ACTIVE:
                continue
            dispatcher = service.manager.get_dispatcher(WEIXIN_CHANNEL_ID, row.account_id)
            if dispatcher is None:
                continue
            reply = ReplyContext(
                channel_id=WEIXIN_CHANNEL_ID, account_id=row.account_id, user_id=bound
            )
            await dispatcher.push_outbound(OutboundEnvelope(reply=reply, text=text, origin="push"))
            count += 1
    except RuntimeError:
        pass

    return count


def notify_channels(text: str, event: str = "") -> None:
    """业务侧唯一入口:后台推送,失败只记日志,不阻塞、不抛错。

    ``event`` 对应 ChannelPushSetting 的开关字段(``push_<event>``):
    dispatch=投递下载,imported=入库完成。空 event(测试推送)不受开关限制。
    """

    async def _run() -> None:
        try:
            if event:
                from movieclaw_api.settings import ChannelPushSetting, get_setting_store

                setting = await get_setting_store().get(ChannelPushSetting)
                if not getattr(setting, f"push_{event}", True):
                    logger.debug("推送事件 %s 已被用户关闭,跳过", event)
                    return
            sent = await push_to_all_channels(text)
            if sent:
                logger.info("已推送到 %d 个通道账号:%s", sent, text[:60])
        except Exception:  # noqa: BLE001 -- 推送绝不能影响业务主链路
            logger.exception("通道推送失败(已忽略)")

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.debug("无运行中的事件循环,推送已跳过")
