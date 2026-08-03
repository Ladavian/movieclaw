# Jellyfin 兼容播放接口：调研与设计

> 状态：调研定稿 v1（2026-08-03），待实施。
> 源起：让 Infuse / Fileball / VidHub / SenPlayer 等第三方播放器**以 Jellyfin
> 服务器的身份**直接连接 movieclaw——浏览媒体库、直连播放、同步观看进度，
> 播放器侧零改动。
> 协议依据：Jellyfin 服务端源码（`jellyfin/jellyfin` @ `33a8cdf`，12.0.0 dev），
> 三轮源码级调研逐字段核实；与 10.10.x 的差异已在文中标注。
> 关联文档：[library.md](library.md)（媒体库架构）、[metadata.md](metadata.md)
> （元数据自足）、[strm-workflow.md](strm-workflow.md)（网盘 strm 工作流）。

## 0. 定位与硬决策

**为什么选 Jellyfin 协议**（对比 Emby / Plex / WebDAV，2026-08-03 用户决策）：

- Jellyfin 全开源 + OpenAPI 完整，每个字段语义可对源码核实；Emby 4.x 闭源只能
  抓包逆向，Plex 认证/发现绑定官方云无法自托管模仿；
- 主流播放器对 Jellyfin 是一等公民支持，覆盖面不损失；
- WebDAV 虽最简单，但播放器会自行重新刮削，movieclaw 的元数据、选图、
  观看状态全部作废，与做这件事的初衷相悖。

**三条硬边界**（本设计的第一性，实现时不可突破）：

1. **最小可用子集，不是复刻**。只实现 Infuse 类播放器实际会调的接口；
   WebSocket、SyncPlay、QuickConnect、DLNA、LiveTV、转码 HLS 一概不做。
2. **不转码**。所有 MediaSource 声明 `SupportsDirectPlay=true,
   SupportsTranscoding=false`——协议本身允许"不会转码的服务器"（Jellyfin
   用户无转码权限时就是这个状态），全解码播放器（Infuse 等）永远走直连。
   解码能力不足的客户端（网页端、Chromecast）不在支持范围，播放失败是
   预期行为。
3. **网盘 strm 不代理**。strm 条目直接把云端 URL 交给播放器（`Protocol=Http`
   时客户端本来就直连 `Path`，见 6.4），服务器零流量——这是相对真 Jellyfin
   （代理转发）的差异化特色，与 strm-workflow.md 的"零网盘流量"原则一致。

## 1. 协议总览与全局约定

Jellyfin 对外协议 = **HTTP REST（JSON）+ UDP 发现**。一次完整播放会话：

```
UDP 7359 发现（可选）
  → GET /System/Info/Public          确认服务器身份
  → POST /Users/AuthenticateByName   换 AccessToken
  → GET /UserViews                   库列表
  → GET /Items?parentId=...          浏览/搜索
  → POST /Items/{id}/PlaybackInfo    播放协商（拿 MediaSources）
  → GET /Videos/{id}/stream          HTTP Range 直连取流
  → POST /Sessions/Playing[/Progress/Stopped]   进度回报
```

以下全局约定**每个接口都必须遵守**（源码依据：`Jellyfin.Extensions/Json/JsonDefaults.cs`、
`Json/Converters/JsonGuidConverter.cs`）：

| 约定 | 内容 |
|---|---|
| 字段命名 | **PascalCase**（`RunTimeTicks`，不是 camelCase） |
| null 处理 | **null 字段直接不出现**（`WhenWritingNull`）；非可空值类型恒输出 |
| 枚举 | 序列化为**枚举成员名字符串**（`"Movie"`、`"Descending"`） |
| GUID 出参 | **N 格式：32 位无横线小写 hex**（`"a1b2c3..."`）；全部 ID 字段一致 |
| GUID 入参 | 宽松：带横线 D 格式也必须能解析 |
| 时间量 | **ticks = 100 纳秒**；秒 × 10⁷ = ticks（`RunTimeTicks`/`PositionTicks` 同单位） |
| 日期 | ISO8601 UTC，7 位小数：`"2010-07-15T00:00:00.0000000Z"` |
| 错误响应 | **`text/plain` 纯文本**（如 `"Error processing request."`），不是 JSON |
| 状态码 | 401=认证失败/无 token；403=权限不足；400=参数缺失；404=不存在 |
| 路由大小写 | ASP.NET 路由**大小写不敏感**（Starlette 敏感——需归一化中间件，见 9.3） |
| 未知字段 | 请求体反序列化必须**静默忽略未知字段**（客户端会发 `EventName` 等） |
| 未知枚举值 | 逗号分隔列表里无法解析的值**静默丢弃**，不报 400 |
| 列表分隔符 | 数组参数逗号分隔；**例外**：`genres`/`studios`/`tags`/`officialRatings` 用 `\|` |

## 2. 最小接口清单

按实施优先级分三档。P0 缺一不可（登录→浏览→播放→进度闭环）；P1 影响体验
（首页、继续观看、字幕）；P2 兜底兼容（不做时个别客户端可能有小毛病）。

**P0 必做**

| 接口 | 说明 |
|---|---|
| UDP 7359 | 局域网自动发现应答 |
| `GET /System/Info/Public`、`GET/POST /System/Ping` | 匿名，服务器身份 |
| `POST /Users/AuthenticateByName` | 登录换 token |
| `GET /Users/Me`、`GET /Users/{userId}`、`GET /Users/Public` | 用户信息/续验 token |
| `GET /UserViews` | 库视图列表 |
| `GET /Items`（含 `?searchTerm`） | 核心查询：浏览/搜索/筛选/分页 |
| `GET /Items/{itemId}` + 旧 `GET /Users/{userId}/Items/{itemId}` | 单条目详情 |
| `GET /Shows/{seriesId}/Seasons`、`GET /Shows/{seriesId}/Episodes` | 剧集结构 |
| `GET\|HEAD /Items/{itemId}/Images/{type}[/{index}]` | 海报/背景/缩略图 |
| `POST /Items/{itemId}/PlaybackInfo` | 播放协商 |
| `GET\|HEAD /Videos/{itemId}/stream[.{container}]` | 取流（Range/206；strm→302） |
| `POST /Sessions/Playing` `/Progress` `/Stopped` `/Ping` | 进度回报（全部 204） |
| `POST\|DELETE /UserPlayedItems/{itemId}` + 旧 `/Users/{userId}/PlayedItems/{itemId}` | 标记已看/未看 |

**P1 建议**

| 接口 | 说明 |
|---|---|
| `GET /UserItems/Resume` + 旧 `GET /Users/{userId}/Items/Resume` | 继续观看（Infuse 用旧路由） |
| `GET /Shows/NextUp` | 追剧"下一集" |
| `GET /Items/Latest` + 旧 `GET /Users/{userId}/Items/Latest` | 最新入库（返回**扁平数组**非 QueryResult；`groupItems=true` 时新集聚合为剧） |
| `GET /Videos/{itemId}/{msId}/Subtitles/{idx}/{ticks}/Stream.{fmt}`（及不带 ticks 变体） | 外挂字幕 |
| `GET /Branding/Configuration`、`/Branding/Css`、`GET /QuickConnect/Enabled` | 匿名启动期轻量接口（返回固定值） |
| `POST /Sessions/Capabilities[/Full]` → 204、`GET /Sessions` → `[]` | 敷衍实现防客户端卡启动 |

**P2 兜底**：legacy `POST|DELETE /PlayingItems/{itemId}[/Progress]` 别名（参数走
query）；`GET /Search/Hints`（内部转调 /Items 再映射扁平结构）；`/emby/*` 路径
前缀别名（个别客户端探测用）。

**明确不做**：`/socket`（WebSocket）、SyncPlay、QuickConnect 授权流程（Enabled
恒返回 false）、转码（`/master.m3u8` 等 404 即可）、LiveTV/Channels、DLNA、
插件/仪表盘全家桶。

## 3. 发现与握手

### 3.1 UDP 自动发现（AutoDiscoveryHost.cs:25-119）

监听 **UDP 7359**；收到含 `"who is JellyfinServer?"`（大小写不敏感）的报文时，
向来源地址回一个 JSON（字段就这四个，`ServerDiscoveryInfo.cs`）：

```json
{"Address": "http://192.168.1.10:8096", "Id": "<32位hex服务器ID>",
 "Name": "MovieClaw", "EndpointAddress": null}
```

`Address` 要按请求来源网段选本机可达地址（对应 Jellyfin 的 `GetSmartApiUrl`）。
实现为 asyncio DatagramProtocol 后台任务，随 lifespan 启停；端口被占/无权限时
写中文警告日志并跳过（发现失败不影响手动填地址）。

### 3.2 `GET /System/Info/Public`（匿名）

```json
{"LocalAddress": "http://192.168.1.10:8096", "ServerName": "MovieClaw",
 "Version": "10.10.7", "ProductName": "Jellyfin Server",
 "Id": "<服务器ID，首启生成并持久化的32位hex>",
 "StartupWizardCompleted": true, "OperatingSystem": ""}
```

- `Version` **报真实存在的 Jellyfin 版本号**（10.10.x），命中客户端兼容分支；
- `StartupWizardCompleted` 必须 `true`，否则客户端进首次配置流程；
- `ProductName` 保持 `"Jellyfin Server"`（客户端以此识别服务器类型）；
- `OperatingSystem` 是 obsolete 字段但会序列化，给 `""`。
- `GET|POST /System/Ping` 返回裸 JSON 字符串（服务器名）。

## 4. 认证

### 4.1 Authorization 头解析（AuthorizationContext.cs:231-317）

Scheme 为 `MediaBrowser`（大小写不敏感；同时兼容 `Emby`）：

```
Authorization: MediaBrowser Client="Infuse", Device="Apple TV", DeviceId="xxx", Version="8.2", Token="<hex>"
```

解析规则（**不是简单 split，需状态机**）：

- 引号内的逗号不是分隔符（`x="123,123"` → 值 `123,123`）；
- 键 `.Trim()` 去空白；值先 `Trim('"')` 再 **URL 解码**；值不要求带引号；
- 键名**大小写敏感**，精确五个：`Client` / `Device` / `DeviceId` / `Version` / `Token`。

token 的全部合法位置（都要支持）：`Authorization` 头 `Token=`、旧头
`X-Emby-Authorization`（同格式）、`X-Emby-Token`、`X-MediaBrowser-Token`、
query `?ApiKey=`（流媒体 URL 用这个）、query `?api_key=`。

### 4.2 `POST /Users/AuthenticateByName`

请求体只有两个字段（**没有 `Password`**，键名大小写不敏感）：

```json
{"Username": "admin", "Pw": "明文密码"}
```

前置校验：`Client`/`Device`/`DeviceId`/`Version` 四键必须都能从 Authorization
头解析出来，缺任一 → **400**。密码错 → **401**（`text/plain` body）。

响应 `AuthenticationResult`：

```json
{"User": {<UserDto>}, "SessionInfo": {<SessionInfoDto>},
 "AccessToken": "<32位无横线hex，随机生成>", "ServerId": "<服务器ID>"}
```

**UserDto 最小合法形态**（省略 = null 不输出）：

```json
{"Name": "admin", "Id": "<用户GUID(N)>", "ServerId": "<服务器ID>",
 "HasPassword": true, "HasConfiguredPassword": true, "HasConfiguredEasyPassword": false,
 "EnableAutoLogin": false,
 "Configuration": {"PlayDefaultAudioTrack": true, "SubtitleLanguagePreference": "",
   "DisplayMissingEpisodes": false, "SubtitleMode": "Default",
   "EnableNextEpisodeAutoPlay": true, "RememberAudioSelections": true,
   "RememberSubtitleSelections": true, "HidePlayedInLatest": true,
   "DisplayCollectionsView": false, "EnableLocalPassword": false,
   "GroupedFolders": [], "OrderedViews": [], "LatestItemsExcludes": [],
   "MyMediaExcludes": [], "CastReceiverId": null},
 "Policy": {"IsAdministrator": true, "IsHidden": false, "IsDisabled": false,
   "EnableMediaPlayback": true, "EnableAudioPlaybackTranscoding": true,
   "EnableVideoPlaybackTranscoding": true, "EnablePlaybackRemuxing": true,
   "EnableContentDownloading": true, "EnableRemoteAccess": true,
   "EnableAllFolders": true, "EnabledFolders": [], "EnableAllDevices": true,
   "EnableAllChannels": true, "BlockedTags": [], "AllowedTags": [],
   "BlockUnratedItems": [], "AccessSchedules": [], "EnabledDevices": [],
   "EnableContentDeletionFromFolders": [],
   "LoginAttemptsBeforeLockout": -1, "MaxActiveSessions": 0,
   "InvalidLoginAttemptCount": 0, "RemoteClientBitrateLimit": 0,
   "SyncPlayAccess": "CreateAndJoinGroups",
   "AuthenticationProviderId": "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider",
   "PasswordResetProviderId": "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider"}}
```

关键点：`Policy.EnableMediaPlayback/EnableAllFolders/EnableRemoteAccess/
EnableContentDownloading` 必须 true、`IsDisabled` false；两个 ProviderId 必须
非空字符串。`SessionInfo` 最小集：`Id`（32 hex）、`UserId`（必出现）、
`UserName/Client/DeviceId/DeviceName/ApplicationVersion/ServerId`，外加非可空
必出现的 `PlayableMediaTypes: []`、`SupportedCommands: []`、`LastActivityDate`、
`LastPlaybackCheckIn`、`IsActive: true`、`SupportsMediaControl: false`、
`SupportsRemoteControl: false`、`HasCustomDeviceName: false`。

### 4.3 movieclaw 侧账号模型

movieclaw 是单管理员账号（`AdminAccountSetting`）。映射：

- Jellyfin"用户" = 这一个管理员账号，用户 GUID 用固定编码（见 8.1）；
- `AuthenticateByName` 校验直接复用 `auth_service.authenticate`；
- **新表 `jellyfin_device`**：每个播放器设备一行
  `(token唯一, client, device_name, device_id, version, last_seen_at)`。
  与 Web 控制台的会话 token 体系**分开**——播放器 token 长期有效（Jellyfin
  语义：设备级凭据，用户不会频繁在电视上重登录），控制台 token 有过期策略，
  混用会互相伤害。`GET /Devices` 类管理接口不做，控制台后续可加"已连接
  播放器"页面直接读这张表（非本期）。
- `GET /Users/Public` 返回 `[UserDto]`（单元素数组，电视端登录页选用户用）。

## 5. 媒体库浏览

### 5.1 `GET /UserViews`

每个启用的 movieclaw 库 → 一个视图。响应 `QueryResult`（`Items` /
`TotalRecordCount` / `StartIndex` 三字段恒出现，下同）。视图 DTO：

```json
{"Id": "<库GUID>", "Name": "电影", "ServerId": "...",
 "Type": "CollectionFolder", "CollectionType": "movies",
 "IsFolder": true, "ImageTags": {}, "BackdropImageTags": [],
 "UserData": {<见5.4>}}
```

- `CollectionType`：**小写**，电影库 `"movies"`、剧集库 `"tvshows"`
  （对应 `Library.kind`）；
- 此接口是"全字段"语义（客户端传 fields 无效），但我们没有的字段靠
  null 省略规则天然合法；
- 库封面（`ImageTags.Primary`）可后续增强，空对象合法。

### 5.2 `GET /Items` —— 核心查询

必须支持的参数（约 90 个参数里的关键子集，其余接受但忽略）：

| 参数 | 语义 |
|---|---|
| `userId` | 缺失且非 API key 时 400 |
| `parentId` | 缺省 = 根（返回视图列表级）；库 GUID → 库内容；剧 GUID → 季；季 GUID → 集 |
| `includeItemTypes` / `excludeItemTypes` | `Movie` `Series` `Season` `Episode`（大小写不敏感解析） |
| `recursive` | 默认 false；**特例**：parentId 是库且传了 includeItemTypes 但没传 recursive 时自动 true（12.0 行为，必须照抄，否则 Infuse 在剧集库拿到空列表） |
| `startIndex` / `limit` | 分页；`TotalRecordCount` = 过滤后总数 |
| `sortBy` / `sortOrder` | 至少支持 `SortName` `Name` `DateCreated` `PremiereDate` `ProductionYear` `CommunityRating` `Runtime` `Random` `DatePlayed` `AiredEpisodeOrder` `ParentIndexNumber,IndexNumber`；`Ascending`/`Descending` 按位配对 |
| `fields` | 字段门控，见 5.3 |
| `filters` | `IsPlayed` `IsUnplayed` `IsResumable` `IsFavorite`（favorite 本期无数据，恒空结果集也合法） |
| `searchTerm` | 标题/别名模糊匹配（走 media_item.title/aliases） |
| `genres` `years` `officialRatings` | 筛选；注意 genres 用 `\|` 分隔 |
| `ids` | 批量取条目 |
| `isPlayed` | 与 filters 等价的另一入口 |
| `enableUserData` `enableImages` `imageTypeLimit` `enableImageTypes` `enableTotalRecordCount` | 输出控制 |

排序需要 SortName 语义：用 movieclaw 标题的拼音/原文排序键（首期直接用
title 的 NOCASE 排序即可，不引入拼音库）。

### 5.3 BaseItemDto 字段映射

**无条件输出**（不受 fields 控制）：`Id` `Name` `ServerId` `Type` `IsFolder`
`MediaType`（Movie/Episode=`"Video"`，Series/Season=`"Unknown"`）`IndexNumber`
`ParentIndexNumber` `PremiereDate` `ProductionYear` `RunTimeTicks`
`OfficialRating` `CommunityRating`（仅 >0 输出）`ImageTags` `BackdropImageTags`
`CollectionType` `UserData`，以及剧集族的 `SeriesId` `SeasonId` `SeriesName`
`SeasonName` `SeriesPrimaryImageTag` `ParentPrimaryImageItemId/Tag`
`ParentBackdropItemId/ImageTags`（集/季无图时继承季/剧海报，Infuse 靠这个
显示卡片）。

**fields 门控**（传了才输出）：`Overview` `Genres` `People` `MediaSources`
`MediaStreams` `Path` `DateCreated` **`ParentId`**（陷阱：不传 fields=ParentId
就不输出，而 SeriesId/SeasonId 是无条件的）`Studios` `ProviderIds` `Taglines`
`OriginalTitle` `ChildCount` `RecursiveItemCount` `PrimaryImageAspectRatio`。

四种类型 ↔ movieclaw 数据源：

| Jellyfin 类型 | 数据源 | 关键字段来源 |
|---|---|---|
| `Movie` | `media_item(kind=movie)` + 其 `library_file` 行 | 标题/年份←media_item；简介/类型/评分/分级/时长←media_metadata；RunTimeTicks←file.duration_seconds×10⁷（缺则 metadata.runtime_minutes×60×10⁷）；多版本文件→多 MediaSources |
| `Series` | `media_item(kind=tv)` | `Status`：TMDB status 映射 `Returning Series→"Continuing"`、`Ended/Canceled→"Ended"`；ChildCount=有文件的季数 |
| `Season` | `media_season` | IndexNumber=season_number（0=Specials）；SeriesId/SeriesName 无条件输出 |
| `Episode` | `media_episode` ⋈ `library_file`（按 (item,season,episode) 数字对） | IndexNumber=集号、ParentIndexNumber=季号（Infuse 强依赖）；PremiereDate←air_date；名称←episode.name |

**只输出"有文件"的内容**：季/集列表以 `library_file` 存在为准（缺集不虚构
Missing 条目，`DisplayMissingEpisodes=false` 与之呼应）；`missing_since` 非空
或 `media_item_id` 为 NULL 的文件行不进任何列表。

### 5.4 UserData 与观看状态

每个条目都带 `UserData`（enableUserData=false 除外）：

```json
{"PlaybackPositionTicks": 0, "PlayCount": 0, "Played": false,
 "IsFavorite": false, "Key": "<条目GUID>", "ItemId": "<条目GUID>",
 "PlayedPercentage": 43.5, "LastPlayedDate": "...", "UnplayedItemCount": 3}
```

- `PlaybackPositionTicks/PlayCount/Played/IsFavorite` 非可空恒输出；`Key` 必填；
- `PlayedPercentage` = position/runtime×100（**0-100**），仅结果 >0 时输出；
- 文件夹类（Series/Season）：`UnplayedItemCount` = 未看集数，
  `Played` = 已看集数 ≥ 总集数，`PlayedPercentage` = 已看/总×100。

**新表 `playback_state`**：`(media_item_id, season_number, episode_number)` 唯一
（电影 (0,0) 哨兵，与 wanted/library_file 同约定），列：`position_ticks`、
`played`、`play_count`、`last_played_at`。单用户，不带 user 维度（多用户
永不在目标内；真要做时加列即可向前兼容）。

### 5.5 剧集接口、继续观看、首页

- `GET /Shows/{seriesId}/Seasons`：无分页；`isSpecialSeason=false` 时滤掉 0 季；
  非剧集 GUID → 404 纯文本。
- `GET /Shows/{seriesId}/Episodes`：`seasonId` 优先于 `season` 号；都不传 =
  全剧集数（按季集序）；`TotalRecordCount` 是分页前总数。
- `GET /UserItems/Resume` + 旧路由：服务端强制语义（客户端参数不能覆盖）——
  `position_ticks>0 AND NOT played`，按 `last_played_at` 降序，Recursive。
- `GET /Shows/NextUp`：对每部"看过至少一集"的剧，取最后已看集之后的下一集
  （跳过 0 季），按该剧最后观看时间降序。
- `GET /Items/Latest`：按 `library_file.created_at` 降序；`groupItems=true`
  （默认）时同剧新集聚合为一个 Series 条目；**返回扁平 `[BaseItemDto]`**，
  不是 QueryResult。

### 5.6 图片

路由：`GET|HEAD /Items/{itemId}/Images/{type}[/{index}]`（index 也可走
`?imageIndex=`；Backdrop 数组下标即 index，本设计每条目至多 1 张背景，
只需支持 index=0）。

| Jellyfin 图 | movieclaw 资产 |
|---|---|
| Movie/Series `Primary` | `media_metadata.poster_file` |
| Movie/Series `Backdrop/0` | `media_metadata.backdrop_file` |
| Season `Primary` | `media_season.poster_file`（无 → 404，客户端自动退剧海报） |
| Episode `Primary` | `media_episode.still_file` |
| `Logo` / `Thumb` / `Banner` | 无资产，404（合法降级） |

- 参数：`maxWidth/maxHeight/quality` 按需缩放（复用现有图片缓存管线的思路，
  产物落 `data/` 缓存目录）；`tag` **纯缓存语义**——服务端不校验，回显进
  `ETag` 并给 `Cache-Control: public, max-age=31536000, immutable`；
- tag 生成：`md5(资产相对路径 + 文件mtime)`，图变则 tag 变即可；
- 支持 `If-None-Match` → 304；无该类型图 → 404；
- DTO 侧：`ImageTags: {"Primary": "<tag>"}`、`BackdropImageTags: ["<tag>"]`，
  有资产才给键（客户端只对有 tag 的类型发请求）。

## 6. 播放链路

### 6.1 `POST /Items/{itemId}/PlaybackInfo`

- body 可为空（`EmptyBodyBehavior.Allow`），同名 query 参数优先；
- **`DeviceProfile` 整体不解析**（Jellyfin 自己在 profile 为 null 时就跳过
  全部设备适配逻辑，直接原样返回 MediaSources——正是我们要的行为）；
- 需要处理的入参只有 `MediaSourceId`（筛选单个版本）；
- 响应：

```json
{"MediaSources": [<MediaSourceInfo>...],
 "PlaySessionId": "<每次请求新生成的32位hex>"}
```

有可播源时**不得**输出 `ErrorCode`；无源时 `MediaSources: []` +
`ErrorCode: "NoCompatibleStream"`（此时不给 PlaySessionId）。

### 6.2 MediaSourceInfo（每个 library_file 一个）

本地文件版本：

```json
{"Id": "<文件GUID(N)>", "Path": "/media/movies/Inception (2010)/xxx.mkv",
 "Protocol": "File", "Type": "Default", "Container": "mkv",
 "Size": 1234567890, "Name": "2160p HEVC", "IsRemote": false,
 "ETag": "<md5(mtime.ticks)>", "RunTimeTicks": 88800000000,
 "Bitrate": 25000000, "VideoType": "VideoFile",
 "SupportsDirectPlay": true, "SupportsDirectStream": true,
 "SupportsTranscoding": false, "SupportsProbing": false,
 "IsInfiniteStream": false, "RequiresOpening": false, "RequiresClosing": false,
 "RequiredHttpHeaders": {}, "Formats": [], "MediaAttachments": [],
 "MediaStreams": [<见6.3>],
 "DefaultAudioStreamIndex": 1, "DefaultSubtitleStreamIndex": null}
```

不转码的正确姿势（源码确认合法，无客户端报错风险）：

- `SupportsTranscoding: false` + **不输出** `TranscodingUrl`；
- `SupportsDirectPlay: true` 必须同时成立（客户端三选一：DirectPlay →
  DirectStream → Transcode，前两者可用就不会碰转码）；
- `SupportsProbing: false`、`RequiresOpening: false`（否则客户端会调
  `/LiveStreams/Open`，我们没有）；
- `DefaultSubtitleStreamIndex` 无字幕时**省略/null，不要给 -1**。

### 6.3 MediaStream（来自 ffprobe 台账）

`library_file` 的探测字段直接映射；`Index` 与 ffprobe 流序号一致，同一
MediaSource 内全局唯一，外挂字幕排在内嵌流之后。

视频流：`Type:"Video"`、`Codec`（hevc/h264/av1，小写）、`Width/Height`、
`BitRate`、`BitDepth`、`VideoRange`/`VideoRangeType` 直接输出字符串——
SDR→`"SDR"/"SDR"`；HDR10→`"HDR"/"HDR10"`；HLG→`"HDR"/"HLG"`；
杜比视界→`"HDR"/"DOVI"`（由 `library_file.hdr` 映射）。

音频流（`audio_streams` JSON 逐条映射）：`Type:"Audio"`、`Codec`、`Language`
（ISO 639-2 三字母）、`Channels`、`ChannelLayout`、`SampleRate`、
`IsDefault`。

`DisplayTitle` 需服务端拼好（客户端直接展示），照 Jellyfin 规则：
音频 `"Chinese - AAC - 5.1 - Default"`；视频 `"1080p HEVC HDR"`；
字幕 `"Chinese - SUBRIP - External"`。

外挂字幕流的声明（客户端单独拉取的关键）：

```json
{"Type": "Subtitle", "Index": 3, "Codec": "subrip", "Language": "chi",
 "IsExternal": true, "SupportsExternalStream": true, "IsTextSubtitleStream": true,
 "DeliveryMethod": "External", "IsExternalUrl": false,
 "DeliveryUrl": "/Videos/{itemGuid}/{msGuid}/Subtitles/3/0/Stream.srt?ApiKey=<token>"}
```

`DeliveryUrl` 是**相对路径**；`DeliveryMethod` 必须 `"External"` 才会被使用。
（首期范围：`subtitle_streams` 只有内封轨——内封字幕 DirectPlay 时由播放器
自行解封装，无需服务端参与；**外挂字幕文件的发现与台账是新需求**，列入
P1 的实施前提，见第 10 节。）

### 6.4 取流：`GET|HEAD /Videos/{itemId}/stream[.{container}]`

参数：`static=true`（直连必带；无此参数本应转码 → 我们返回 400）、
`mediaSourceId`（缺省取第一个版本；值等于 itemId 时也回落第一个）、
`?ApiKey=` 认证。`.{container}` 后缀路由与不带后缀完全同一处理
（后缀只为帮助部分播放器按扩展名选解封装器）。

**本地文件**：完整实现 HTTP Range 语义——`Accept-Ranges: bytes`、
`Range: bytes=x-y` → `206 Partial Content` + `Content-Range`、`If-Range`、
HEAD 支持、正确 `Content-Type`（mkv→`video/x-matroska`、mp4→`video/mp4`、
ts→`video/mp2t`）。**拖进度条完全依赖这套，是播放体验的生命线**；用
Starlette `FileResponse`（0.36+ 原生支持 Range/206）并以抓包对照验证。

**strm 网盘条目（本设计的差异化，不代理）**：

Jellyfin 对 strm 的原生行为（源码确认）：读 strm 内容得到 URL 后，
`MediaSourceInfo.Path = <该URL>`、`Protocol = "Http"`、`IsRemote = true`——
**客户端选择 DirectPlay 远程源时直接播 Path，根本不经过服务器**（Jellyfin
源码注释明确把"直接播放远程 URL"当正常路径）。我们照此办理，两层保障：

1. PlaybackInfo 里 strm 条目输出 `Protocol:"Http"` + `Path:<strm 内容 URL>` +
   `IsRemote:true`，主流客户端直连云端，服务器零流量；
2. 兜底：若客户端仍请求 `/Videos/{id}/stream`（部分实现不看 Protocol），
   该接口对 strm 条目读文件内容后返回 **302 重定向**到 URL——不做真
   Jellyfin 的反向代理。302 注意：HEAD 请求同样 302；不塞 body；重定向
   目标须自己支持 Range（CloudDrive/Alist 直链均支持）。

strm 条目的 `Container` 从 URL 扩展名猜（猜不到省略）、`ETag` 不输出、
`Size/Bitrate/MediaStreams` 按台账有啥给啥（strm 不探测云端内容是既有
原则，`MediaStreams` 可为空数组——DirectPlay 下播放器自行探测，合法）。

### 6.5 外挂字幕接口（P1）

```
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/{startPositionTicks}/Stream.{format}
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.{format}
```

DeliveryUrl 用的是带 ticks 的那条（ticks 恒传 0）。format 支持 `srt`/`vtt`/
`ass`：与源文件同格式 → 原样吐字节；`srt→vtt` 做纯文本转换（加 `WEBVTT`
头、逗号改点）；`ass` 不做跨格式转换（Jellyfin 同样不支持 ass 互转）。
Content-Type：srt→`application/x-subrip`、vtt→`text/vtt`。

## 7. 播放进度回报

| 接口 | 请求体关键字段 | 响应 |
|---|---|---|
| `POST /Sessions/Playing` | 同 Progress（PlaybackStartInfo 继承自它，无新增字段） | 204 |
| `POST /Sessions/Playing/Progress` | `ItemId`、`PositionTicks`、`IsPaused`、`PlaySessionId`、`MediaSourceId`、`PlayMethod` | 204 |
| `POST /Sessions/Playing/Stopped` | `ItemId`、`PositionTicks`、`PlaySessionId`、`MediaSourceId`、`Failed` | 204 |
| `POST /Sessions/Playing/Ping` | query `playSessionId`（转码续命用，我们无事可做） | 204 |
| `POST /UserPlayedItems/{itemId}` | query `datePlayed?` | **200 + UserItemDataDto**（非 204） |
| `DELETE /UserPlayedItems/{itemId}` | — | 200 + UserItemDataDto |

落库逻辑（写 `playback_state`）：

- Progress：更新 `position_ticks`、`last_played_at`；
- Stopped：更新位置；若 `position/runtime ≥ 90%` → `played=true`、
  `play_count+=1`、`position_ticks=0`（Jellyfin MaxResumePct 默认 90 的语义）；
  `< 5%` 或 `runtime<300s` 视为未开始，位置清零不记续播（MinResumePct=5、
  MinResumeDurationSeconds=300）；
- UserPlayedItems：`played=true, position=0, play_count+=1`（DELETE 反向）；
  作用于 Series/Season GUID 时级联到全部子集；
- 请求体必须容忍未知字段（jellyfin-web 会发服务端模型里不存在的
  `EventName`），**不要用严格模式解析**。

## 8. movieclaw 侧数据映射与工程设计

### 8.1 GUID 编码方案（无映射表，双向可逆）

Jellyfin 的一切 ID 都是 32 位 hex GUID；movieclaw 是整型主键 + 数字对。用
**结构化编码**取代映射表（无状态、稳定、可逆）：

```
16 字节 = [魔数 "MC" 2B][类型 1B][保留 1B][载荷 12B]，hex 后即 32 位 GUID

类型 0x01 库视图      载荷 = library.id (8B)
类型 0x02 电影/剧集   载荷 = media_item.id (8B)
类型 0x03 季          载荷 = media_item.id (8B) + season (2B)
类型 0x04 集          载荷 = media_item.id (8B) + season (2B) + episode (2B)
类型 0x05 媒体源      载荷 = library_file.id (8B)
类型 0x00 用户/服务器  载荷 = 固定常量
```

- 集的 GUID 编码 `(item, season, episode)` 数字对而非 `media_episode.id`——
  集行随元数据刷新可能增删重建，行 id 不稳定，数字对才是 movieclaw 全局
  约定的稳定引用（与 wanted/library_file 同源）；
- 入参解析剥横线后按魔数+类型分发；魔数不匹配 → 404；
- 服务器 ID、用户 GUID 首启生成/固定编码，持久化在 `app_setting`。

### 8.2 新增持久化（迁移向前兼容，遵守发布规范第 3 条）

| 表 | 用途 | 结构 |
|---|---|---|
| `jellyfin_device` | 播放器设备 token | token(唯一)、client、device_name、device_id、version、last_seen_at |
| `playback_state` | 观看状态 | (media_item_id, season, episode) 唯一；position_ticks、played、play_count、last_played_at |

纯增表迁移，可自由向前兼容；不动任何既有表。

### 8.3 模块与挂载

- 新包 `src/movieclaw_jellyfin/`：协议层（DTO 序列化、GUID 编解码、
  Authorization 解析）+ 路由层；查询复用 `movieclaw_media` / `movieclaw_db`
  的服务与仓储，不直接裸写 SQL 散落各处；
- 路由挂载在**同一 FastAPI 应用、同一端口**的根路径（`/System/*`、
  `/Users/*`、`/Items/*`……），注册在 SPA catch-all 之前；`/emby/*` 前缀
  别名一并注册。选择同端口的理由：单容器单端口是部署契约，开新端口要动
  compose/文档/runtime-version，收益不成比例；Jellyfin 的根路径命名空间
  （System/Users/Items/Videos/Shows/Sessions/Branding/QuickConnect）与
  movieclaw 现有 `/api/*` 及前端路由无冲突（实施第一步先做一次冲突清点）；
- 响应模型独立一套 Pydantic（PascalCase alias + exclude_none），与业务
  接口的 `success/code/message/data` 规范**彻底隔离**——这是模仿外部协议，
  不是 movieclaw 业务接口，两套规范互不渗透；错误响应按 Jellyfin 语义
  返回纯文本状态码，不走统一异常处理器。

### 8.4 运行时与发布

纯 Python 实现，无新增运行时依赖 → 不触发 `docker/runtime-version` bump；
UDP 7359 需要容器多暴露一个端口（compose/文档更新，属部署文档变更）。

## 9. 风险与对策

1. **路由大小写**：ASP.NET 大小写不敏感，Starlette 敏感。对策：ASGI 中间件
   把命中 Jellyfin 命名空间前缀（大小写不敏感匹配 system/users/items/videos/
   shows/sessions/…）的路径归一化为注册时的规范大小写。
2. **客户端行为差异**：各播放器请求序列不同，静态调研覆盖不了全部。对策：
   验收以真实抓包为准（见第 10 节），首发支持列表明确写 Infuse/Fileball/
   VidHub，其余"理论兼容"。
3. **未识别/缺元数据条目**：`media_item_id IS NULL` 的文件不出现在兼容接口
   里（待识别清单是控制台的事）；有条目无 metadata 行时靠 null 省略降级。
4. **strm 直链时效**：strm 内容若是带签名的临时直链，客户端缓存 Path 过期
   会播放失败。对策：strm 场景 Path 也可指向我们的 stream 端点（302 每次
   现读 strm 文件），牺牲一跳换稳定；两种模式做成库级开关，默认直连。
5. **性能**：/Items 大库分页 + fields 门控天然限量；图片缩放有缓存；台账
   查询已有热路径索引。无新增风险面。

## 10. 分期实施与验收

前置（P0 开工前）：mitmproxy 抓取 Infuse ↔ 真实 Jellyfin 10.10 的完整会话
（发现→登录→浏览电影库/剧集库→播放本地文件→拖进度→退出），请求序列
脱敏后存 `tests/fixtures/jellyfin_compat/`，作为集成测试剧本与字段对照的
最终裁判。

| 期 | 内容 | 验收 |
|---|---|---|
| P0-a | GUID 编解码、Authorization 解析、序列化基建、System/Auth/Branding、UDP 发现、新表迁移 | 单测：解析/编解码边界；Infuse 能发现并登录成功 |
| P0-b | UserViews / Items / Shows / 单条目 / 图片 | Infuse 完整浏览两类库，海报/详情/季集结构正确 |
| P0-c | PlaybackInfo / stream(Range+302) / 进度回报 / 已看标记 | Infuse 播放本地文件可拖动进度条；strm 条目直连云端（服务器观察零媒体流量）；退出后控制台与 Infuse 双向可见进度 |
| P1 | Resume / NextUp / Latest / 字幕接口 / Sessions 敷衍接口 / 外挂字幕台账 | Infuse 首页三区正确；外挂字幕可选可显 |
| P2 | legacy 别名、/emby 前缀、Search/Hints、Fileball/VidHub 回归 | 三款播放器全链路手测通过 |

合并前照例全绿：`pytest`、`ruff check .`、`pnpm web:lint`、`pnpm web:typecheck`。

## 11. 开放问题（实施前需拍板）

1. **兼容层开关**：默认开启还是控制台显式开启？倾向默认关闭 + 控制台一键
   开启（开启时生成服务器 ID、提示防火墙放行 7359/UDP），符合最小暴露面。
2. **外挂字幕台账**：library_file 目前只有内封 `subtitle_streams`；同目录
   `.srt/.ass` 的发现、命名解析（语言后缀）、台账落位是独立小设计，P1 前
   补一节到 library.md 或单开短文档。
3. **收藏（IsFavorite）**：播放器可发收藏请求（`POST /UserFavoriteItems/{id}`）,
   本期未列。若做，playback_state 加一列即可，接口顺手；不做则该请求 404，
   Infuse 会隐藏收藏按钮或忽略失败。倾向 P1 顺手做。
