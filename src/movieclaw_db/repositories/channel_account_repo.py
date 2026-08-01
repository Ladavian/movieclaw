from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.crypto import get_secret_box
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus


class ChannelAccountRepository:
    """IM 通道账号表的数据访问层。

    敏感字段加解密收口在本层(与站点凭据/下载器同款约定):
    - 落库时用 SecretBox 加密 token;
    - ``decrypted_token`` 仅在装配通道客户端时调用,明文不随 ORM 对象传递。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, account_id: str) -> ChannelAccount | None:
        result = await self._session.execute(
            select(ChannelAccount).where(ChannelAccount.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_by_channel(self, channel_id: str) -> list[ChannelAccount]:
        result = await self._session.execute(
            select(ChannelAccount)
            .where(ChannelAccount.channel_id == channel_id)
            .order_by(ChannelAccount.created_at)
        )
        return list(result.scalars().all())

    async def upsert(
        self,
        *,
        channel_id: str,
        account_id: str,
        token: str,
        base_url: str,
        bound_user_id: str | None,
    ) -> ChannelAccount:
        """绑定成功后落库(重复绑定同一 bot 时覆盖凭据并复位状态/游标)。

        游标一并清空:新凭据对应服务端新会话,旧游标已无意义,从头开始
        拉取即可(服务端只投递未消费的消息)。
        """
        token_enc = get_secret_box().encrypt(token)
        row = await self.get(account_id)
        if row is None:
            row = ChannelAccount(
                channel_id=channel_id,
                account_id=account_id,
                token=token_enc,
                base_url=base_url,
                bound_user_id=bound_user_id,
            )
        else:
            row.token = token_enc
            row.base_url = base_url
            row.bound_user_id = bound_user_id
            row.cursor = None
            row.status = ChannelAccountStatus.ACTIVE
            row.last_error = None
            row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def save_cursor(self, account_id: str, cursor: str) -> None:
        """持久化收消息游标(每轮长轮询有新值就覆盖,高频小写入)。"""
        row = await self.get(account_id)
        if row is None:
            return
        row.cursor = cursor
        row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()

    async def set_agent_session(self, account_id: str, session_id: str | None) -> None:
        """绑定/清空该账号当前的 Agent 会话(None = /reset,下条消息建新会话)。"""
        row = await self.get(account_id)
        if row is None:
            return
        row.agent_session_id = session_id
        row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()

    async def mark_stale(self, account_id: str, reason: str) -> None:
        """凭据失效:标记 stale,绑定页据此引导用户重新扫码。"""
        row = await self.get(account_id)
        if row is None:
            return
        row.status = ChannelAccountStatus.STALE
        row.last_error = reason
        row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()

    async def delete(self, account_id: str) -> bool:
        row = await self.get(account_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

    @staticmethod
    def decrypted_token(row: ChannelAccount) -> str:
        return get_secret_box().decrypt(row.token)
