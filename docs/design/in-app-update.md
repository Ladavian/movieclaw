# 应用内更新（In-App Update）设计与实施计划

## 背景与目标

Docker 镜像的构建和分发都很重，而实际频繁变动的只有前端代码、后端代码，
偶尔变动的是 NER 模型；运行时（Python/Node/pip 依赖/系统库如 ffmpeg）很少变。
国内用户拉取 Docker 镜像慢且不稳定，而从 GitHub Release（可走加速镜像）下载
几 MB 的 tar 包体验好得多——模型分发已经验证了这条通道（`torrent-ner-v1`）。

**目标：**

1. 应用内一键更新前端、后端代码与 NER 模型到主干最新发布版，无需重建/重拉镜像。
2. 只要运行时依赖没变，用户永远不需要升级容器；依赖变了，UI 明确提示需要升级镜像。
3. 坏更新永远不能把用户的部署搞挂——最差情况回落到镜像内置版本。
4. Agent 执行环境始终感知**当前生效**的源码目录（见「Agent 源码感知」一节）。

**非目标：**

- 不在容器内 git pull / 构建（容器内没有 pnpm、完整 node_modules，也不该有）。
- 不做运行时依赖的应用内更新（pip/npm 依赖变化 = 必须升级镜像，这是刻意的边界）。
- 不做多版本并行运行 / 灰度，单实例部署不需要。

## 总体架构：镜像基线 + data 卷 overlay

```
镜像层（不可变，永远完整可运行）          data 卷（持久，承载更新）
/app/src          ← 后端基线            /app/data/updates/
/app/web          ← 前端基线              versions/
/app/models/…     ← 模型基线                v0.2.0/
/entrypoint.sh                               backend/   (src/ alembic/ alembic.ini)
                                             web/       (standalone 产物)
                                             manifest.json
                                           current -> versions/v0.2.0   （原子切换）
                                         models/torrent-ner-v2/         （模型独立更新）
```

核心原则：**更新不覆盖镜像内文件，只改变进程的启动指向。**

- 后端：`PYTHONPATH=<current>/backend/src python -m movieclaw_api.main`
- 前端：`node <current>/web/apps/web/server.js`
- 无 overlay 或 overlay 不兼容/损坏时，指向回镜像内基线路径。

为什么不覆盖 `/app/src`：

1. 容器层写入是易失的——用户重建容器（改 env、机器重启后 recreate）就回退旧版；
   而 data 卷上的 overlay 在容器重建后依然生效，这正是"不换镜像跑新代码"的关键。
2. 基线不动才有兜底可退；覆盖了就退无可退。
3. 改一个符号链接是原子的，覆盖几百个文件中途断电会留下半新半旧的残局。

## 版本与兼容契约

两条独立的版本线：

- **应用版本**（频繁发）：`pyproject.toml` 的 `version`，Release tag 形如 `v0.2.0`。
- **运行时版本**（依赖变了才 bump）：镜像内烧入整数
  `ENV MOVIECLAW_RUNTIME_VERSION=1`。凡是改动 pyproject dependencies、Node 大版本、
  系统包（ffmpeg 等）、entrypoint 契约，就 +1 并同步发布新镜像。

Release 的 `manifest.json` 声明 `requires_runtime: N`。更新前后端比对：

- 匹配 → 允许应用内更新；
- 不匹配 → UI 提示「本次更新包含运行时依赖变化，请升级 Docker 镜像」，拒绝下载。

用显式整数而不是依赖清单哈希：哈希无法表达「这次依赖变动向后兼容」的人工判断，
且整数对用户的报错信息更友好。配套 CI 守卫：PR 改了 `pyproject.toml` 的
dependencies 段或 Dockerfile 关键行而没 bump `MOVIECLAW_RUNTIME_VERSION`，CI 失败。

## Release 产物与 CI

新增 GitHub Actions workflow（`.github/workflows/release.yml`），打 `v*` tag 时：

1. `pnpm ext:zip` + `pnpm web:build`，打包 `app-web.tar.gz`：
   standalone 产物 + `.next/static` + `public`（含扩展 zip）。纯 JS 跨架构通用
   （sharp 已随 `images.unoptimized` 移除，产物内不得包含任何原生二进制——CI 校验）。
2. 打包 `app-backend.tar.gz`：`src/` + `alembic/` + `alembic.ini`，
   **并预置 CI 现场导出的 `movieclaw_cli/data/spec.json`**（运行期硬依赖，
   预置进产物同时保证「存在」与「与代码同版」，与镜像内现场导出同一思路）。
3. 生成 `manifest.json`：`{ version, requires_runtime, min_model, sha256: {文件: 哈希}, changelog }`。
4. 三个文件作为 Release assets 上传。模型更新维持现有独立 Release 通道。

产物布局的硬性约束（见下节「Agent 源码感知」的依赖）：`backend/` 内部必须保持
仓库的相对布局——`src/`、`alembic/`、`alembic.ini` 的相对位置不变，因为
`movieclaw_db/migrations.py` 靠 `Path(__file__).resolve().parents[2]` 定位 alembic。

## entrypoint 改造

entrypoint 烧在镜像里、无法应用内更新，因此它的逻辑必须**最小且稳定**，
版本相关的复杂逻辑全部放在可更新的后端代码里。它只做三件事：

1. **解析启动指向**：`current` 链接存在、manifest 完整、`requires_runtime` 与镜像
   匹配、且未被标记为 bad → 从 overlay 拉起前后端；否则从镜像基线拉起。
   把生效来源导出为环境变量（如 `MOVIECLAW_CODE_ROOT`、`MOVIECLAW_OVERLAY_VERSION`），
   供后端与 Agent 感知。
2. **失败兜底**：在 data 卷记启动尝试标记；overlay 版本连续 2 次未通过健康存活
   （后端进程短时间内退出）→ 标记 bad、回落上一版本或镜像基线重新拉起，
   状态外显到 UI（后端启动时读取标记文件上报）。
3. **重启约定码**：现有 exit 42 = 只重启后端（保持不变）；新增 exit 43 =
   前后端全量重启（重新走一遍解析逻辑，用于代码更新后切换）。约定码常量
   与 `services/app_config.py` 的 `RESTART_EXIT_CODE` 同处维护。

## 后端更新服务与 UI

新增 `movieclaw_api/services/app_update.py`：

- **检查**：GitHub API 查最新 Release（直连失败降级到配置的加速镜像），
  比对当前版本与 `requires_runtime`，返回可更新性 + changelog。
- **执行**：下载到临时目录 → 逐文件 sha256 校验 → 解包到 `versions/<ver>/` →
  备份 SQLite（见下）→ 原子替换 `current` 链接 → 以 exit 43 触发全量重启。
  任何一步失败都不影响正在运行的版本；下载带进度，通过现有事件通道推给前端。
- **回退**：保留上一个版本目录，UI 提供「回退到上一版本」；更旧的自动清理。
- **状态**：当前运行版本、生效来源（镜像基线 / overlay vX）、镜像运行时版本、
  bad 标记与兜底事件，全部在「设置 → 关于/更新」展示。

前端：设置页新增「检查更新」「立即更新」「回退」，下载进度条，
依赖不匹配时展示「需升级镜像」的引导文案。

**数据库安全**：alembic 迁移是单向的，回退到旧代码后新 schema 可能不兼容。
应用更新切换前自动把 SQLite 文件复制到 `updates/backup/<ver>-<时间戳>.db`
（SQLite 单文件，成本≈0），回退时提示用户可恢复。备份保留最近 N 份。

**下载安全**：用户会走第三方加速镜像，sha256 校验为强制项——manifest 从
GitHub 官方 HTTPS 获取（体积小、直连通常可行），大文件可从镜像下载、按
manifest 校验。二期可加 minisign 签名（公钥烧进镜像）防 Release 被篡改。

## Agent 源码感知

Agent 的系统提示词会告知模型「后端源码在哪里」，供 bash/read 工具查阅分析。
overlay 生效后，**必须保证 Agent 看到的是当前实际运行的代码，而不是镜像基线**。

现状盘点（关键机制已经就位）：

- `movieclaw_agent/prompts.py:62`：`_SOURCE_ROOT = Path(__file__).resolve().parents[1]`
  ——从模块自身位置反推。后端进程以 overlay 的 `src` 为 PYTHONPATH 启动时，
  `__file__` 就在 overlay 内，`_SOURCE_ROOT` **自动**指向 overlay，无需改动。
  这正是当初「从本文件位置反推而不是写死路径」注释所预期的部署形态。
- `movieclaw_db/migrations.py:13`：同理自动跟随，前提是产物保持相对布局（已列为
  CI 硬性约束）。
- mclaw 工具的 spec 从运行中代码同版加载（`services/mclaw_tool`），自动跟随。

需要显式补充的三点：

1. **环境段增补运行形态**：`build_system_prompt` 的环境段除源码路径外，
   增加一行运行形态说明（读 entrypoint 导出的 `MOVIECLAW_OVERLAY_VERSION`）：
   overlay 生效时明确告知「当前运行 overlay 版本 vX，源码根目录为 <overlay 路径>；
   镜像内 `/app/src` 是旧的基线备份，查阅源码一律以前者为准」。不加这一句，
   模型用 bash 探索文件系统时可能撞见 `/app/src` 并误读旧代码。
2. **禁止任何对 `/app/src`、`/app/web` 的硬编码**：代码评审基线——所有需要
   定位源码/资源的地方一律 `__file__` 反推或读 `MOVIECLAW_CODE_ROOT`，
   本计划落地时全库巡检一遍（已知的 `tracker/registry.py`、`llm/providers/registry.py`
   均为 `__file__` 相对，合规）。
3. **bash 工具的 cwd**：Agent bash 的 workdir 与源码根无关（数据目录），
   不受 overlay 影响，无需改动；但验收用例中要覆盖「Agent 在 overlay 形态下
   read 源码文件返回的是新代码」。

## 模型独立更新

`MOVIECLAW_NER_DIR` 已是环境变量。更新流程：下载新模型到
`/app/data/models/torrent-ner-vN/` → 校验 → 更新指针（data 卷上的配置或链接）→
exit 43 全量重启（MOVIECLAW_NER_DIR 是 entrypoint 解析后导出的具体版本目录，
必须重新走一遍 resolve 才会指向新模型）。entrypoint 解析时若 data 卷有生效模型指针则导出其路径为
`MOVIECLAW_NER_DIR`，否则用镜像内置。manifest 的 `min_model` 声明代码对模型的
最低版本要求，防止偏斜。

## 已知边界（文档中向用户说明）

- `TMDB_API_KEY` 属运行时范畴，应用内更新不刷新它；失效轮换需换镜像或配环境变量。
- entrypoint 与运行时依赖的变更必须走镜像升级，`requires_runtime` 不匹配时
  应用内更新会明确拒绝。
- 回退跨越了数据库迁移时，需从自动备份恢复数据库。

## 实施分期

每期独立可合并、可验证：

**M1 — Release 产物与 CI**
- release.yml：构建三产物 + manifest + sha256，上传 Release assets。
- CI 校验：web 产物无原生二进制；backend 产物含 spec.json；相对布局正确。
- 验证：打测试 tag，下载产物在本机按目标布局解包，直接拉起前后端成功。

**M2 — entrypoint overlay 解析（地基，要最稳）**
- 解析 current / 兼容比对 / 失败兜底 / exit 43；导出 `MOVIECLAW_CODE_ROOT`、
  `MOVIECLAW_OVERLAY_VERSION`；镜像烧入 `MOVIECLAW_RUNTIME_VERSION`。
- 验证：容器测试矩阵——无 overlay（基线启动）；有效 overlay（新版启动，
  重建容器后仍是新版）；损坏 overlay / runtime 不匹配 / 连续启动失败（均回落基线）。

**M3 — 后端更新服务 + 设置页 UI**
- 检查/下载/校验/备份/切换/回退 API 与页面；Agent 环境段增补运行形态。
- 验证：端到端一键更新→重启后运行新版；篡改 sha256 → 更新拒绝且原版本无恙；
  回退→恢复上一版本；Agent 会话中 read 源码 → 返回 overlay 新代码。

**M4 — 模型独立更新 + 收尾**
- 模型更新流程；SQLite 备份保留策略；README/文档补「何时需要升级镜像」。

**M5 — 加固（已全部落地）**
- manifest 签名：采用 Ed25519（cryptography 已是运行依赖，无需引入 minisign）。
  发布侧配置 CI 机密 `RELEASE_SIGNING_KEY` 后自动上传 `manifest.json.sig`；
  部署侧配置 `UPDATE_MANIFEST_PUBKEY`（镜像构建参数或环境变量）后强制验签。
  双方都不配置时行为不变。密钥对用 `scripts/gen-release-signing-key.sh` 生成。
- 自动检查更新：定时任务 `check_app_update`（每小时，用 ETag 条件请求——
  304 不计 GitHub 未认证配额，无新版时几乎零成本）+ 后端启动后延迟数分钟
  的首查（容器重启即感知新版，不等下一个周期）。发现新版通过「待处理事项」
  提醒（绝不自动安装——更新会重启服务，必须由用户主动触发）；用户 dismiss
  的是具体版本，再有更新版本发布会重新点亮；网络不可达静默跳过，不产生告警。
- runtime bump CI 守卫：`.github/workflows/runtime-guard.yml`。
