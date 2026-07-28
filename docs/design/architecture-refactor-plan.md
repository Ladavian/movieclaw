# 架构治理与重构计划

> 状态：v2 执行完毕（2026-07-28）。本文档基于一次全仓分层审计（路由/服务/仓储/领域包/前端/调度
> 六个切面）产出，先给总体结论，再按优先级列出改造项。每一项都给出**现状证据（文件:行号）、
> 目标形态、验证标准**，可直接拆成独立 PR 执行。
> 关联文档：[subscription.md](subscription.md)、[library.md](library.md)、[library-routing.md](library-routing.md)
>
> **执行记录（2026-07-28）**：P0-1/2/3、P1-1/2/3/4、P2-1/3 已全部落地；P2-2 完成子包迁移
> （`services/library/`，11 个模块），其中两个子项**有意递延**：
> - **scan/ingest 识别编排合并**与 **scan.py 按阶段拆分**——识别链的回归测试依赖
>   自训 NER 模型文件（GitHub Release 分发，CI/沙箱环境不可得），相关测试在无模型
>   环境全数跳红、没有安全网；这两项应在能跑全量识别测试的环境里单独立项执行。
> - P2-4（外观纳入导出）按计划本身的定位（可选，最低优先级）暂不做。
> 文中各项的"现状"描述保留审计时点原文，行号以当时代码为准。

## 0. 总体结论：分层方向正确，不推翻，只做定向收口

审计确认了以下资产**保持不动**，它们是本次重构的"地基"而非对象：

- **包依赖单向无环**：api → {db, tracker, downloader, matcher, enrich, media, llm, net, cache,
  scheduler, agent}，非 api 包中无一处反向 import；matcher/enrich 保持纯函数无 IO。
- **横切设施统一**：响应信封 `ApiResponse[T]` + `ok()` 覆盖全部业务端点；异常收敛在
  `handlers.py`；鉴权三区挂载 + 守护测试默认拒绝（`api/router.py`）。
- **调度与 HTTP 共用同一套服务函数**：9 个定时任务全部直调 services 里与路由相同的函数
  （如 `scan_library`、`scrape_media_item`、`evaluate_and_dispatch`），无逻辑分叉。
- **两条核心链路已收敛**：下载三入口合流于 `torrent_submit.submit_torrent`；刮削七入口
  收敛于 `ensure_media_item` / `ensure_assets` / `scrape_media_item` 三段分工。
- **前端 client 层纪律**：`lib/http.ts` 唯一 fetch 出口，`lib/api/*` 与后端路由一一对应，
  页面层零裸 fetch。

问题集中在四类：**安全欠账**（站点凭据明文）、**口径重复**（同一决策多处拷贝、靠注释对齐）、
**路由层失守**（libraries/network 把业务写进路由）、**模块成环**（订阅域/媒体库域靠函数内
延迟导入撑着）。以下按 P0/P1/P2 三档展开。

---

## P0：安全与正确性（先做，互相独立，可并行）

### P0-1 站点凭据加密落库

**现状**：同类敏感数据存在三套加密行为——
- `SettingStore._encrypt_fields`（`movieclaw_api/settings/store.py:152`）：按 `secret_fields` 声明式加密；
- `llm_provider_repo.py:31` / `downloader_repo.py:50`：各自命令式调 `get_secret_box()`；
- `credential_repo.py`：**完全不加密**，站点 cookie / api_key / password 明文落库
  （`movieclaw_db/models/site_credential.py:56-57` 自己标注了 ⚠️）。

**目标**：`credential_repo` 对齐 downloader/llm 的做法，敏感列写入前 `SecretBox.encrypt`、
读出后 decrypt。存量明文数据用 `SecretBox.is_encrypted()` 判别做**读时兼容**（明文读到即
返回原值），另加一次性 alembic data migration 把存量行整体加密，迁移后删除读时兼容分支的
必要性说明保留在注释里。

**验证**：
1. 新增测试：写入凭据后直接查表，断言列值 `SecretBox.is_encrypted()` 为真；
2. 迁移测试：造明文行 → 跑迁移 → service 层读出仍是正确明文；
3. 现有站点验证（`services/verification.py`）测试全绿。

### P0-2 「投递目录三级兜底」收口为单一实现

**现状**：同一决策写了四遍，且 `subscription_health.py:9-11` 自己立了"口径同源"原则却没做到——
- `services/download_dispatch.py:79-91`（真实投递，用 `resolve_dispatch_dir`）；
- `services/download_dispatch.py:225-239`（订阅弹窗预检）；
- `services/subscription_health.py:231-233`（体检，用的却是 `resolve_dispatch_rule`）；
- `api/routes/downloaders.py:51-67`（手动下载，整段写在路由里，靠 :44-46 的注释声明
  "与订阅同一决策"）。

**目标**：在 `services/library_routing.py`（决策类逻辑的自然归属）落一个唯一入口，形如
`resolve_save_path(session, media_kind, library_id?) -> SavePathDecision`，返回值携带
`mode / base / 决策理由`。四个调用点全部改调它；`downloaders.py` 路由瘦身为"解析参数 →
调 service → ok()"。

**验证**：
1. 为 `resolve_save_path` 补三级兜底（指定库 / 默认库 / 下载器默认目录）的单元测试；
2. 体检与真实投递对同一配置给出相同结论（新增一条对拍测试，直接消灭"体检说好、投递却挂"）；
3. `grep` 确认 `mode`/`base` 推导逻辑全仓只剩一份。

### P0-3 消灭绕过 SettingStore 的直写

**现状**：`services/torrent_matcher.py:30, 57, 87-99` 用 `SettingRepository` 直读直写
namespace `subscription_match_watermark`，手写 `json.loads/dumps`——未经 `@register_setting`
注册、无 Pydantic 校验、无缓存、不在 `export_all()` 导出范围，直接违反
`movieclaw_db/models/app_setting.py:20` 的规定。

**目标**：声明 `MatchWatermarkSetting(SettingSchema)` 并注册（namespace 建议
`subscription.match_watermark`，遵守点号分层），读写改走 `get_setting_store()`。
旧 namespace 数据加一次性迁移或读时兼容。

**验证**：`grep -rn "SettingRepository" src/movieclaw_api/services/` 无命中（services 层
只允许经 SettingStore 触达 app_setting）；被动匹配测试全绿。

---

## P1：分层收口（P0 之后做，按模块拆 PR）

### P1-1 network 路由补 service 层

**现状**：`api/routes/network.py`（320 行）是唯一没有对应 service 的设置路由——
- 请求/响应模型内联在路由文件（:60-99），未进 `schemas/`；
- 业务校验写在路由（:155-174 `_validate_payload`）；
- 路由直接摸 Repository 并**在路由层解密 api_key**（:106-121, :221-260）；
- 对 LLM 连通性的探测（:241-253）与 `services/llm_config.py:195` 的验证是**两套判据**。

**目标**：新增 `services/network_config.py` 承接校验、目录拼装、保存编排（镜像 diff →
`close_media_service()` → `reset_all_breakers()`）与探测目标解析；模型迁至
`schemas/network.py`；LLM/站点的连通性探测复用 `llm_config` / `verification` 已有实现，
删除路由里的第二套判据。路由文件目标 ≤120 行。

**验证**：网络设置保存/探测的现有行为不变（现有测试 + 手动过一遍设置页）；
`routes/network.py` 中不再出现 Repository 与 SecretBox 的 import。

### P1-2 libraries 路由瘦身（约 400 行业务下沉）

**现状**：`api/routes/libraries.py` 1777 行、36 端点、25 处函数内延迟导入。四块业务逻辑
长在路由里：

| 块 | 位置 | 问题 |
|---|---|---|
| 认领 | :1647-1679 与 :1687-1732 | 同一段四步编排（ensure_media_item → claim_identity → close_fulfilled_wanted → ensure_assets）**写了两遍** |
| 复核拍板 | :463-496 | 路由直接改 ORM 字段并 commit，还跨域调订阅 |
| 海报墙聚合 | :935-1088（单端点 154 行） | 4 段裸 SQL + Python 聚合 + 海报三级优先级 + 缺集口径 |
| 缺失/待识别清单 | :1485-1597 | select → delete → commit 直接写在端点 |

**目标**：
1. `services/library_claim.py`：落 `claim_units(session, file_ids, identity, units) -> ClaimResult`，
   单个/批量两个端点共用；复核拍板一并收进来（它是"人工认领"的孪生动作，
   `library_scan._review_identity` 是它的机器侧另一半）；
2. 海报墙聚合下沉到 `services/library_items.py`（该文件已示范了正确形态：
   `build_item_detail` 让路由只剩 DTO 映射），顺带让"库存已有集"的 SQL 复用
   `LibraryFileRepository.owned_units`，删除 :1017-1020 那段"必须与 subscription 口径
   对齐"的注释——用共用代码代替注释约定；
3. 缺失/待识别清单的增删查下沉到 service 或 repository 语义方法。

**验证**：`libraries.py` 目标 ≤1000 行、函数内延迟导入 ≤8 处；认领/复核/海报墙现有测试
全绿；新增"单个认领与批量认领走同一函数"的测试。

### P1-3 共享口径函数化

**现状**：两处统计靠注释维持一致——
- "已播集数"在 `routes/subscriptions.py:89-95` 与 `routes/libraries.py:1001-1016` 各一份；
- `routes/subscriptions.py:82-95` 路由直接 import `MediaItemRepository` 自己遍历。

**目标**：在 `movieclaw_db/repositories/media_repo.py` 落 `aired_unit_counts()` 语义方法
（或 services 层共享函数），两个调用点改调它。

**验证**：两个页面（订阅创建预检、海报墙）对同一条目给出一致的"已播/缺集"数字（对拍测试）。

### P1-4 跨模块私有符号转公开契约

**现状**：下划线函数被别的模块 import，私有边界名存实亡——
- `library_ingest.py:76` → `library_scan._guess_evidence`；
- `torrent_sync.py:30` → `verification._friendly_error, _is_transient_error`；
- `download_dispatch.py:48-51` 与 `wanted_fulfillment.py:67` → `subscription_matching._units_text`。

**目标**：被跨模块消费的符号去掉下划线、补 docstring，或移入其归属的共享模块
（`_guess_evidence` 应随 P2-2 移入识别链共享底座；`_units_text` 随 P2-1 移入订阅子包）。

**验证**：`grep -rn "import.*\._\|from.*import _" src/movieclaw_api/services/` 无跨模块命中。

---

## P2：结构优化（规模问题，P1 之后做，收益是长期可维护性）

### P2-1 订阅域子包化，消解五模块环

**现状**：`subscription / subscription_matching / download_dispatch / wanted_search /
wanted_fulfillment` 互相成环（大环：`subscription:619 → wanted_search:21 → 
subscription_matching:242 → download_dispatch:47 → subscription`），全靠函数内延迟导入
撑着——services 层共 36 处延迟导入，`download_dispatch` 8 处最多。这五个模块实际是
**一个概念包**，只是没有目录边界。

**目标**：
```
services/subscription/
    __init__.py      # 对外公共接口：create/list/…、evaluate_and_dispatch、kick_search_soon
    core.py          # 原 subscription.py（CRUD 与工单生长）
    matching.py      # 原 subscription_matching.py
    dispatch.py      # 原 download_dispatch.py
    wanted.py        # 原 wanted_search.py + wanted_fulfillment.py
    health.py        # 原 subscription_health.py
    _shared.py       # units_text 等包内共享原语
```
包内允许互相 import（顶层、非延迟）；包外（路由、scheduler 注册、其他 service）只准
import `services.subscription` 的公共接口。定时任务注册点随文件迁移，`lifespan.py:104-118`
的 import 清单同步更新。

**验证**：
1. 订阅全链路测试（被动匹配、主动搜索、投递、体检）全绿；
2. 包内函数内延迟导入清零；包外对子包内部模块（`subscription.matching` 等）零直接 import
   （可加一条简单的 import-linter 约定或守护测试）；
3. 9 个定时任务在启动日志中全部注册成功。

### P2-2 媒体库域子包化 + 识别链共享底座

**现状**：library_* 系列 11 个文件交叉引用密集，三组双向环
（scan↔items、scan↔organize、config↔ingest、import_watch_config↔ingest）；
`library_scan.py` 单文件 1608 行；识别链在 scan（`library_scan.py:1180`
`resolve_with_candidates`）与 ingest（`library_ingest.py:581` `verify_resolve`）共用底座
但编排各写一遍。

**目标**：与 P2-1 同构，落 `services/library/` 子包；把"证据收集 → 解析 → 收敛/复核"的
识别编排抽成 scan 与 ingest 共用的一个函数（吸收 `_guess_evidence`）；`library_scan.py`
按阶段拆分（发现/识别/探测/资产），单文件 ≤800 行。

**验证**：扫描、导入、对账、整理的现有测试全绿；scan 与 ingest 对同一文件给出相同识别
结论（新增对拍测试）。

### P2-3 抽取进程内任务状态 `TaskState`

**现状**：「单飞集合 / 实时进度 / 停止请求」三件套在 `library_scan.py:209-264`、
`library_organize.py:75-84`、`media_scrape.py:223-240` 三处各实现一遍
（`media_scrape.py:217` 注释自认"与扫描同模式"）。

**目标**：抽一个 ≤60 行的 `services/task_state.py`（或并入 P2-2 子包），三处改用。
这是纯机械替换，适合作为子包化 PR 的前置小 PR。

**验证**：并发触发同一库的扫描/整理/刷新，第二次请求得到"已在进行中"响应（现有行为不变）。

### P2-4 外观与头像纳入配置导出（可选）

**现状**：外观走文件系统（`services/appearance.py:24-32`，`.active` 标记文件），
不在 `SettingStore.export_all()` 范围内，配置备份漏掉"当前生效背景图"。

**目标**：不强行搬进 SettingStore（图片资产进 JSON 不合理），但在导出/备份路径里补上
外观目录的清单信息。优先级最低，接受"暂不做、记录在案"。

---

## 执行顺序与拆分建议

```
第一批（并行，互不冲突）：P0-1 凭据加密 ｜ P0-2 投递目录收口 ｜ P0-3 水位线入内核
第二批（并行）：          P1-1 network service 化 ｜ P1-3 口径函数化 ｜ P1-4 私有符号转公开
第三批：                  P1-2 libraries 瘦身（依赖 P1-3 的共享口径）
第四批：                  P2-3 TaskState → P2-1 订阅子包 → P2-2 媒体库子包（依赖 P0-2、P1-4）
```

每一项独立成 PR，PR 内遵循「先补/确认测试 → 重构 → 测试仍绿」的顺序；子包化两项
（P2-1/P2-2）是纯移动 + import 调整，**不与任何行为变更混在同一个 PR**。

## 不做清单（明确排除，防止范围蔓延）

- 不改包分层结构（src/ 下 12 个包的边界与依赖方向维持现状）；
- 不改响应信封、异常处理、鉴权三区挂载；
- 不把业务定时任务迁出 services（"任务住在领域包"的设想暂不推进——当前单进程部署下
  无独立部署诉求，迁移收益不足）；
- 不动前端 `lib/api/*` 结构（后端路由路径不变，前端零改动）。
