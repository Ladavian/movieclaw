"""服务器/用户身份 DTO（设计文档 3.2/4.2，字段清单逐项对照源码核实）。

UserDto/Policy/Configuration 里列出的键都是"真 Jellyfin 必然输出"的字段
（非可空或带非 null 默认值）；可空且为 null 的字段按协议约定不输出。
"""

from __future__ import annotations

from typing import Any

from movieclaw_api.services import auth as auth_service
from movieclaw_api.settings.schemas import JellyfinCompatSetting, get_jellyfin_compat
from movieclaw_jellyfin.ids import user_guid

# 对外报的 Jellyfin 版本：真实存在的 10.10 系版本号，命中客户端兼容分支
REPORTED_VERSION = "10.10.7"
PRODUCT_NAME = "Jellyfin Server"


def _now_iso() -> str:
    from movieclaw_db.models.base import utcnow

    return format_datetime(utcnow())


def format_datetime(value) -> str:
    """ISO8601 UTC 7 位小数 + Z（协议约定的日期形态，统一收敛）。"""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond:06d}0Z"


async def get_compat_settings() -> JellyfinCompatSetting:
    return await get_jellyfin_compat()


def user_configuration() -> dict[str, Any]:
    return {
        "PlayDefaultAudioTrack": True,
        "SubtitleLanguagePreference": "",
        "DisplayMissingEpisodes": False,
        "GroupedFolders": [],
        "SubtitleMode": "Default",
        "DisplayCollectionsView": False,
        "EnableLocalPassword": False,
        "OrderedViews": [],
        "LatestItemsExcludes": [],
        "MyMediaExcludes": [],
        "HidePlayedInLatest": True,
        "RememberAudioSelections": True,
        "RememberSubtitleSelections": True,
        "EnableNextEpisodeAutoPlay": True,
    }


def user_policy() -> dict[str, Any]:
    return {
        "IsAdministrator": True,
        "IsHidden": False,  # /Users/Public 按 hidden 过滤，true 会让登录页空列表
        "IsDisabled": False,
        "BlockedTags": [],
        "AllowedTags": [],
        "EnableUserPreferenceAccess": True,
        "AccessSchedules": [],
        "BlockUnratedItems": [],
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": True,
        "EnableRemoteAccess": True,
        "EnableLiveTvManagement": True,
        "EnableLiveTvAccess": True,
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "EnablePlaybackRemuxing": True,
        # 置 true 会逼客户端对远程源（strm）走转码，击穿"不转码"硬边界
        "ForceRemoteSourceTranscoding": False,
        "EnableContentDeletion": False,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": True,
        "EnableSyncTranscoding": True,
        "EnableMediaConversion": True,
        "EnabledDevices": [],
        "EnableAllDevices": True,
        "EnabledChannels": [],
        "EnableAllChannels": True,
        "EnabledFolders": [],
        "EnableAllFolders": True,
        "InvalidLoginAttemptCount": 0,
        "LoginAttemptsBeforeLockout": -1,
        "MaxActiveSessions": 0,
        "EnablePublicSharing": True,
        "BlockedMediaFolders": [],
        "BlockedChannels": [],
        "RemoteClientBitrateLimit": 0,
        "AuthenticationProviderId": (
            "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
        ),
        "PasswordResetProviderId": (
            "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider"
        ),
        "SyncPlayAccess": "CreateAndJoinGroups",
        "EnableCollectionManagement": False,
        "EnableSubtitleManagement": False,
        "EnableLyricManagement": False,
    }


async def user_dto(server_id: str) -> dict[str, Any]:
    account = await auth_service.get_admin_account()
    return {
        "Name": account.username,
        "ServerId": server_id,
        "Id": user_guid(),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "Configuration": user_configuration(),
        "Policy": user_policy(),
    }


def session_info_dto(
    server_id: str,
    session_id: str,
    *,
    client: str,
    device_id: str,
    device_name: str,
    version: str,
    user_name: str,
) -> dict[str, Any]:
    now = _now_iso()
    return {
        "Id": session_id,
        "UserId": user_guid(),
        "UserName": user_name,
        "Client": client,
        "DeviceId": device_id,
        "DeviceName": device_name,
        "ApplicationVersion": version,
        "ServerId": server_id,
        "PlayableMediaTypes": [],
        "SupportedCommands": [],
        "LastActivityDate": now,
        "LastPlaybackCheckIn": now,
        "IsActive": True,
        "SupportsMediaControl": False,
        "SupportsRemoteControl": False,
        "HasCustomDeviceName": False,
    }
