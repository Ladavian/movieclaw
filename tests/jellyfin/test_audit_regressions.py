"""终验发现缺陷的回归测试（对照修复清单，防止翻案倒退）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jellyfin.helpers import jf_login
from movieclaw_jellyfin.ids import episode_guid, item_guid, library_guid, season_guid

TICKS_PER_MS = 10_000


def test_pascalcase_query_params(client: TestClient, seeded: dict) -> None:
    """致命缺陷#1：PascalCase 客户端的 query 键必须被归一化。"""
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "ParentId": library_guid(seeded["tv_lib"]),  # 大写 P
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "SortBy": "ParentIndexNumber,IndexNumber",
            "Limit": "2",
        },
    ).json()
    assert body["TotalRecordCount"] == 3
    assert len(body["Items"]) == 2
    assert body["Items"][0]["Type"] == "Episode"

    # Static/MediaSourceId 大小写混写也必须能播
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"apikey": token}).json()
    local = next(s for s in info["MediaSources"] if s["Protocol"] == "File")
    resp = client.get(
        f"/Videos/{guid}/stream",
        params={"APIKEY": token, "Static": "true", "MediaSourceID": local["Id"]},
    )
    assert resp.status_code == 200


def test_stopped_without_position_marks_played(client: TestClient, seeded: dict) -> None:
    """缺陷#2：Stopped 不带 PositionTicks = 播到结尾，标已看且不清别人的进度。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    ep = episode_guid(seeded["show"], 2, 1)
    assert (
        client.post(
            "/Sessions/Playing/Stopped", params=auth, json={"ItemId": ep}
        ).status_code
        == 204
    )
    ud = client.get(f"/Items/{ep}", params=auth).json()["UserData"]
    assert ud["Played"] is True and ud["PlaybackPositionTicks"] == 0


def test_progress_zero_clears_resume_point(client: TestClient, seeded: dict) -> None:
    """拖回开头（position=0）要抹掉续播点，已看状态不变。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    ep = episode_guid(seeded["show"], 1, 1)
    runtime_ticks = 47 * 60 * 1000 * TICKS_PER_MS
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": runtime_ticks // 2},
    )
    assert client.get("/UserItems/Resume", params=auth).json()["TotalRecordCount"] == 1
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": 0},
    )
    assert client.get("/UserItems/Resume", params=auth).json()["TotalRecordCount"] == 0


def test_series_favorite_visible_on_folder(client: TestClient, seeded: dict) -> None:
    """缺陷#7：收藏整剧/整季要在对应条目的 UserData 回显，且不污染 S00E00。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    show = item_guid(seeded["show"])
    season1 = season_guid(seeded["show"], 1)

    resp = client.post(f"/UserFavoriteItems/{show}", params=auth).json()
    assert resp["IsFavorite"] is True
    assert "UnplayedItemCount" in resp  # 文件夹形态的聚合响应
    assert client.get(f"/Items/{show}", params=auth).json()["UserData"]["IsFavorite"] is True

    assert client.post(f"/UserFavoriteItems/{season1}", params=auth).json()["IsFavorite"] is True
    seasons = client.get(f"/Shows/{show}/Seasons", params=auth).json()["Items"]
    by_index = {s["IndexNumber"]: s for s in seasons}
    assert by_index[1]["UserData"]["IsFavorite"] is True
    assert by_index[2]["UserData"]["IsFavorite"] is False


def test_sort_date_created_without_fields(client: TestClient, seeded: dict) -> None:
    """缺陷#3：sortBy=DateCreated 不带 fields 也必须生效（排序键取自数据）。"""
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["tv_lib"]),
            "includeItemTypes": "Episode",
            "sortBy": "DateCreated",
            "sortOrder": "Descending",
        },
    ).json()
    assert body["TotalRecordCount"] == 3
    # 不带 fields=DateCreated 时 DTO 里没有该字段，但顺序依然确定
    assert "DateCreated" not in body["Items"][0]


def test_users_bad_guid_is_400_not_401(client: TestClient) -> None:
    """终验1#1：非法 userId 是 400，绝不能 401（会触发客户端登录循环）。"""
    token = jf_login(client)
    resp = client.get("/Users/not-a-guid", params={"ApiKey": token})
    assert resp.status_code == 400
    resp = client.get(f"/Users/{'0' * 32}", params={"ApiKey": token})
    assert resp.status_code == 404


def test_emby_prefix_case_insensitive(client: TestClient) -> None:
    """终验1#3：/Emby/... 也要归一化命中。"""
    assert client.get("/Emby/System/Info/Public").status_code == 200


def test_library_view_parent_id_navigable(client: TestClient, seeded: dict) -> None:
    """终验#13：库视图 ParentId 指向根，且拿它回打 /Items 能得到视图列表。"""
    token = jf_login(client)
    views = client.get("/UserViews", params={"ApiKey": token}).json()["Items"]
    parent = views[0]["ParentId"]
    body = client.get("/Items", params={"ApiKey": token, "parentId": parent}).json()
    assert body["TotalRecordCount"] == 2  # 根 → 视图列表


def test_episode_parent_id_gated_by_fields(client: TestClient, seeded: dict) -> None:
    """ParentId 受 fields 门控：不传不出现，传了指向季。"""
    token = jf_login(client)
    ep = episode_guid(seeded["show"], 1, 1)
    plain = client.get(
        "/Items",
        params={"ApiKey": token, "ids": ep},
    ).json()["Items"][0]
    assert "ParentId" not in plain
    with_field = client.get(
        "/Items",
        params={"ApiKey": token, "ids": ep, "fields": "ParentId"},
    ).json()["Items"][0]
    assert with_field["ParentId"] == season_guid(seeded["show"], 1)


def test_nextup_anchor_is_latest_activity(client: TestClient, seeded: dict) -> None:
    """缺陷#5：NextUp 锚定最近活动，而非很久前弃坑的半集。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    e11 = episode_guid(seeded["show"], 1, 1)
    e12 = episode_guid(seeded["show"], 1, 2)
    runtime_ticks = 47 * 60 * 1000 * TICKS_PER_MS
    # 先在 E01 留半截进度（弃坑），再看完 E02
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": e11, "PositionTicks": runtime_ticks // 2},
    )
    client.post(f"/UserPlayedItems/{e12}", params=auth)
    nextup = client.get("/Shows/NextUp", params=auth).json()
    # 最近活动是"看完 E02" → NextUp 是 S02E01，不是弃坑的 E01
    assert nextup["Items"][0]["Id"] == episode_guid(seeded["show"], 2, 1)


def test_bad_pagination_params_do_not_500(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    resp = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["movie_lib"]),
            "startIndex": "abc",
            "limit": "xyz",
        },
    )
    assert resp.status_code == 200


def test_default_audio_stream_prefers_default_flag(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    local = next(s for s in info["MediaSources"] if s["Protocol"] == "File")
    default_audio = next(
        s for s in local["MediaStreams"] if s["Type"] == "Audio" and s["IsDefault"]
    )
    assert local["DefaultAudioStreamIndex"] == default_audio["Index"]
