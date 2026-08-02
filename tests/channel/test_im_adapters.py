"""Telegram / Discord 适配器的消息归一化与配对码生命周期测试。"""

from __future__ import annotations

import time

from movieclaw_channel.discord.adapter import DiscordAdapter
from movieclaw_channel.discord.client import DiscordClient
from movieclaw_channel.telegram.adapter import TelegramAdapter
from movieclaw_channel.telegram.client import TelegramClient


def _tg_adapter() -> TelegramAdapter:
    return TelegramAdapter(TelegramClient("dummy-token"), "bot1")


def _dc_adapter() -> DiscordAdapter:
    return DiscordAdapter(DiscordClient("dummy-token"), "bot1")


class TestTelegramNormalize:
    def test_private_text_message(self) -> None:
        msg = _tg_adapter()._normalize(
            {
                "update_id": 100,
                "message": {
                    "message_id": 7,
                    "date": 1700000000,
                    "text": "帮我找沙丘2",
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123, "is_bot": False},
                },
            }
        )
        assert msg is not None
        assert msg.user_id == "123"
        assert msg.text == "帮我找沙丘2"
        assert msg.reply.token["chat_id"] == 123
        assert msg.provider_message_id == "123:7"

    def test_group_message_ignored(self) -> None:
        msg = _tg_adapter()._normalize(
            {
                "message": {
                    "text": "hi",
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": 123, "is_bot": False},
                }
            }
        )
        assert msg is None

    def test_bot_message_ignored(self) -> None:
        msg = _tg_adapter()._normalize(
            {
                "message": {
                    "text": "hi",
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 9, "is_bot": True},
                }
            }
        )
        assert msg is None


class TestDiscordNormalize:
    def test_dm_message(self) -> None:
        msg = _dc_adapter()._normalize(
            {
                "id": "555",
                "channel_id": "888",
                "content": "订阅怪奇物语",
                "author": {"id": "42", "bot": False},
            }
        )
        assert msg is not None
        assert msg.user_id == "42"
        assert msg.text == "订阅怪奇物语"
        assert msg.reply.token["dm_channel_id"] == "888"

    def test_guild_message_ignored(self) -> None:
        msg = _dc_adapter()._normalize(
            {
                "id": "555",
                "channel_id": "888",
                "guild_id": "1",
                "content": "hi",
                "author": {"id": "42"},
            }
        )
        assert msg is None

    def test_bot_author_ignored(self) -> None:
        msg = _dc_adapter()._normalize(
            {"id": "5", "channel_id": "8", "content": "x", "author": {"id": "9", "bot": True}}
        )
        assert msg is None


class TestPairChallengeExpiry:
    def test_expired_challenge_flips_status(self) -> None:
        from movieclaw_api.services.im_channel import ImChannelService, PairChallenge

        service = ImChannelService()
        challenge = PairChallenge(
            challenge_id="c1",
            channel_id="telegram",
            pair_code="123456",
            bot_id="b1",
            bot_name="bot",
            expires_at=time.monotonic() - 1,  # 已过期
        )
        service._challenges["c1"] = challenge
        got = service.get_challenge("c1")
        assert got is not None
        assert got.status == "expired"
