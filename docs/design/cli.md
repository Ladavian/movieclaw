# movieclaw CLI 化架构设计

> 目标：把 Web 控制台能做的所有事情——搜索、订阅、媒体库、下载器、站点、监听导入、
> 规则组、AI 助手、网络、设置、日志——全部通过一个类 `gcloud` / `gh` / `kubectl`
> 风格的命令行工具完成。本文是架构设计与基础能力定义，不含具体实现代码。

---

## 0. 前提假设与总体定位

在动手之前，先把关键假设摆在明面上（如与预期不符，需先纠正再实施）：

1. **CLI 是「远程客户端」，不是「第二个业务实现」。**
   CLI 只调用现有 `/api/v1` HTTP 接口，所有业务逻辑仍在服务端。这保证
   Web、CLI、未来任何客户端行为一致，也让 CLI 代码量维持在「薄壳」量级。
2. **CLI 与服务可以不在同一台机器。** 用户可能在自己的笔记本上管理 NAS 上的
   movieclaw，因此必须支持配置服务器地址与远程认证（见 §3）。
3. **命令行工具名 `movieclaw`，短别名 `mc`。** 语言选 Python（与后端同栈，
   详见 §7 权衡），以 `movieclaw-cli` 包形式放在本仓库 `src/movieclaw_cli/`。
4. **覆盖范围 = 页面能看到的一切**，包括外观、头像这类「装饰性」功能——
   全量映射见 §2 命令树，129 个后端端点逐一有归宿。

现状关键事实（决定了设计中的几个硬约束）：

- 认证只有 **Cookie 会话**（`movieclaw_session`，itsdangerous 签名，7/30 天过期，
  改密即全端下线）+ 浏览器插件专用 Bearer 同步令牌。**没有通用 API Token**。
- 有 **2 个 SSE 流式端点**（`/search/stream`、`/agent/runs/{id}/stream`），无 WebSocket。
- 长任务（扫描 / 整理 / 元数据刷新 / Agent 运行）全部是「POST 启动 + GET 轮询」模式。
- 所有响应统一 `ApiResponse{success, code, message, data}` 信封，错误统一
  `ErrorResponse{success, code, message, details}`。
- 敏感字段（站点凭证、下载器密码、LLM Key）**保存后永不回读**。
- 删除类操作的「确认」语义全在前端 `window.confirm`，后端不做二次确认。

---

## 1. 分层架构

```
┌────────────────────────────────────────────────────────┐
│  命令层  src/movieclaw_cli/commands/                    │
│  按资源域一个模块：auth / search / sub / lib / site /   │
│  dl / watch / rules / llm / agent / net / logs / ...    │
│  只做：参数解析 → 调 client → 交给输出层渲染            │
├────────────────────────────────────────────────────────┤
│  客户端层  src/movieclaw_cli/client/                    │
│  按域封装的薄 API client（拆信封、类型化返回、错误映射）│
├────────────────────────────────────────────────────────┤
│  基础能力层  src/movieclaw_cli/core/                    │
│  config   多上下文配置（服务器/凭证/默认项）            │
│  http     httpx 封装：认证注入、超时、重试、统一错误    │
│  sse      SSE 解帧 / Last-Event-ID 续传 / 退避重连      │
│  task     长任务 --wait 轮询 + 进度条                   │
│  output   table/json/yaml 渲染、TTY 检测、退出码        │
│  prompt   确认(--yes)、密文输入、候选选择器、目录浏览器 │
└────────────────────────────────────────────────────────┘
```

设计准则（对应流行 CLI 的共识）：

- **名词-动词结构**：`mc <资源> <动作> [对象] [标志]`，同 `gcloud`/`gh`。
- **stdout 只放数据，stderr 放进度和提示**；`mc sub list -o json | jq` 必须干净可管道。
- **人机双态**：TTY 下默认表格 + 彩色 + 进度条；非 TTY（管道/脚本）自动降级为
  纯文本、无色、无交互——需要确认而未给 `--yes` 时直接报错退出，绝不挂起等输入。
- **全局标志**在所有子命令生效：
  `--server` `--context` `-o/--output (table|json|yaml)` `--yes` `--quiet`
  `--no-color` `--timeout` `--debug`（打印请求/响应，脱敏凭证）。
- **错误信息用中文**、面向非开发者（与 CLAUDE.md 项目约定一致），并带可行动建议，
  例如：`错误：没有默认下载器，请先执行 mc dl add 或 mc dl set-default <id>`。

---

## 2. 命令树全景（129 个端点 → 命令的完整映射）

命名约定：资源用常用缩写二级命令（`sub`=subscriptions、`lib`=libraries、
`dl`=downloaders、`watch`=import-watch），动词用 `list/show/create/update/delete/
enable/disable/verify` 等统一词表。`<id>` 位置参数，其余走标志。

### 2.1 认证与账号（auth）

```
mc login [--server URL]            # 交互式：探测 bootstrap → 用户名/密码 → 换取令牌
mc logout                          # POST /auth/logout + 清本地凭证
mc auth status                     # GET /auth/me（含当前 context、服务器、过期时间）
mc auth bootstrap                  # GET/POST /auth/bootstrap 首次建号（headless 部署场景）
mc auth token create|list|revoke   # 【需后端新增，见 §3】管理长期 API Token
mc account profile set --nickname  # PUT /auth/profile
mc account password change         # PUT /auth/password（交互密文输入；提醒全端下线）
mc account avatar set <file> | get # POST/GET /auth/avatar
```

### 2.2 搜索（search）

```
mc search "沙丘2"                        # SSE 流式：逐站上屏进度(stderr) + 结果表(stdout)
   --category movie|tv|... --site a,b   # 站点子集 / 分类
   --resolution 2160p --group ... --hdr # 客户端侧筛选（对应前端筛选弹层）
   --sort seeders|size|date --limit N
   --no-stream                          # 走阻塞版 GET /search（脚本场景）
   --incognito                          # 无痕：不写搜索历史
mc search history list|show <id>|delete <id>|clear
                                        # GET/DELETE /search/history*，show 读快照
mc search prefs get|set                 # GET/PUT /search/preferences（自定义分类标签）
```

### 2.3 发现与元数据（discover / media / people）

```
mc discover movie|tv [--source tmdb|douban]   # GET /discover/{kind}
mc discover top250                            # 豆瓣 Top250
mc media search "关键词" [--source]           # GET /discover/search（TMDB/豆瓣条目搜索）
mc media show movie|tv <tmdb_id>              # GET /discover/{kind}/{id}
mc media show douban <douban_id>              # GET /discover/douban/{id}
mc people show <tmdb_person_id>               # GET /people/{id}（纯本地）
```

### 2.4 订阅（sub）

```
mc sub list [--kind movie|tv]                 # GET /subscriptions（含工单进度列）
mc sub show <id>                              # GET /subscriptions/{id}
mc sub create --tmdb <id> [--seasons 1,2] [--follow] [--rule-set <id>] [--library <id>]
      # 组合流：POST /subscriptions/prepare（歧义时进交互候选选择器，海报墙 → 终端列表）
      #        → GET /subscriptions/dispatch-preview（投递预检，问题用中文当场讲清）
      #        → POST /subscriptions
mc sub update <id> [--seasons ...] [--follow/--no-follow] [--rule-set] [--library]
      # PATCH /subscriptions/{id} —— 前端没做入口，CLI 直接补齐这块能力
mc sub pause <id> / resume <id>               # PATCH /subscriptions/{id}/pause
mc sub delete <id> [--yes]                    # DELETE /subscriptions/{id}
mc sub activities <id> [--limit N]            # GET /subscriptions/{id}/activities（时间线）
mc sub preview --tmdb <id> [--library <id>]   # GET /subscriptions/dispatch-preview（模拟一单）
mc sub health                                 # GET /subscriptions/pipeline-health（链路体检）
```

### 2.5 媒体库（lib）—— 最大域，36 端点

```
# 库管理
mc lib list [--kind]                          # GET /libraries（含扫描/整理进度列）
mc lib show <id>                              # GET /libraries/{id}
mc lib create --kind movie|tv --name X --root /path [--root ...]   # POST /libraries
mc lib update <id> [--name] [--add-root] [--remove-root]           # PUT /libraries/{id}
mc lib set-default <id>                       # POST /libraries/{id}/default
mc lib delete <id> [--yes]                    # DELETE /libraries/{id}（不动磁盘，须说明）
mc lib routing-options                        # GET /libraries/routing-options

# 长任务（统一 --wait 语义，见 §5）
mc lib scan <id> [--wait] / scan stop <id>    # POST .../scan(/stop)，进度轮询库详情
mc lib organize <id> [--dry-run] [--wait]     # preview 即 --dry-run；正式执行需 --yes
mc lib refresh <id> [--wait] / refresh stop <id> / refresh status <id>
                                              # 元数据整库刷新 + 独立进度端点

# 条目
mc lib items <id> [--filter]                  # GET /libraries/{id}/items
mc lib item show <lib> <item>                 # 条目详情（含逐文件 ffprobe 规格）
mc lib item episodes <lib> <item> --season N  # 分集清单
mc lib item refresh <lib> <item>              # 单条目重刮
mc lib item reidentify <lib> <item>           # 重新识别
mc lib item delete <lib> <item> [--yes]       # ⚠ 删磁盘文件，确认提示必须写明
mc lib item artwork list <lib> <item>         # 候选海报/背景
mc lib item artwork set <lib> <item> --file-path P | --auto   # 选定锁定 / 恢复自动

# 待识别 / 已忽略 / 身份复核 / 缺失
mc lib unidentified [--library <id>]          # GET /libraries/unidentified
mc lib claim <file_id> --tmdb <id>            # POST /libraries/files/{id}/claim
mc lib claim-batch --files a,b,c --tmdb <id>  # 整组认领
mc lib ignore <file_id> / restore <file_id>   # 忽略 / 恢复
mc lib unidentified clear [--library] [--yes] # 批量忽略
mc lib review list / resolve <group> --adopt|--keep   # 身份复核
mc lib missing [--library <id>]               # 缺失清单
mc lib missing clear [--yes] / redownload ... # 清理台账 / 交回订阅管线重下
mc lib thumb <file_id> -O out.jpg             # 分集缩略图下载
```

### 2.6 站点（site）与浏览器插件（extension）

```
mc site catalog                               # 支持的站点目录
mc site list / show <id> / stats              # 列表(含验证状态) / 详情 / 同步统计
mc site add <site_key> [--cookie|--api-key|--username/--password]
      # 凭证一律支持交互密文输入 与 --cookie-file/环境变量，避免进 shell history
mc site update <id> ...                       # 重填凭证（后端不回读，整体重填）
mc site enable|disable <id>                   # PATCH status
mc site verify <id> [--wait]                  # 触发验证 + 轮询至 ok/failed
mc site delete <id> [--yes]

mc extension token show|create|revoke         # GET/POST/DELETE /extension/token
```

### 2.7 下载器（dl）与一键下载

```
mc dl list / show <id>
mc dl add --type qbittorrent|transmission --url ... [--username/--password]
      [--save-path P] [--map /movieclaw路径:/下载器路径 ...]     # 路径映射多值标志
mc dl update <id> ... / enable|disable <id> / set-default <id>
mc dl verify <id> [--wait] / delete <id> [--yes]

mc download <torrent_ref>                     # POST /downloaders/submit
      # torrent_ref = 搜索结果里的下载引用；配 --library <id> / --save-path P
      # 无标志时按服务端三级兜底路由，并把「会/不会自动入库」的结论回显给用户
```

搜索 → 下载的衔接：`mc search` 结果表带稳定行号，`mc download` 支持
`--from-last-search <行号>`（读本地缓存的最近一次结果快照），这是把
「页面点下载按钮」翻译成 CLI 习惯的关键一步；同时支持直接给 site_id + download_url。

### 2.8 监听导入（watch）与规则组（rules）

```
mc watch list / add --source /downloads --strategy hardlink|copy
        --target library:<id>|auto:movie|auto:tv
mc watch update <id> ... / delete <id> [--yes]

mc rules list / show <id>                     # GET /rule-sets
mc rules create|update|delete                 # 后端已有 CRUD、前端只读——CLI 补全入口
      # 条件字段：分辨率偏好序、编码、HDR 策略、制作组黑白名单、体积区间、
      # 做种数下限、免费种、排除 H&R —— 用标志 + --from-json 两种输入形态
```

### 2.9 AI 助手（agent / llm）

```
mc llm presets / show / set [--provider ...] [--api-key ...] / verify / delete
mc agent run "帮我把待识别清单清一清"          # POST /agent/start → SSE 实时渲染
      [--session <id>]                        # 续聊
      [--detach]                              # 只拿 run_id 就返回
mc agent attach <run_id>                      # 重连 SSE（Last-Event-ID 断点续传）
mc agent cancel <run_id>
mc agent sessions list|show|rename|delete
mc agent chat                                 # 交互式 REPL（P3，可选）
```

### 2.10 网络、系统、外观

```
mc net show                                   # GET /network/config
mc net set [--proxy-mode none|env|manual] [--proxy-url ...]
           [--tmdb-api-base] [--tmdb-image-base] [--douban-api-base]
           [--service tmdb --use-proxy/--direct]      # 逐服务开关
mc net test [--service tmdb|douban|site:<id>]  # POST /network/test 连通性测试

mc logs [--day 2026-07-29] [--tail 2000] [-f]  # GET /system/logs*；-f 用轮询模拟 follow
mc logs days                                   # 可查看的日志日期列表
mc status                                      # GET /health + /auth/me + 版本，一眼看部署状态

mc appearance list|upload <file>|use <id>|use default|delete <id>   # 背景图库
mc ui prefs get|set                            # GET/PUT /ui/preferences（玻璃质感参数）
mc fs browse [path]                            # GET /fs/browse（也是交互目录选择器的数据源）
```

### 2.11 CLI 自身

```
mc config get|set|list                        # 本地配置（非服务端设置）
mc context list|use|add|remove                # 多服务器上下文切换
mc completion bash|zsh|fish
mc version
```

> 覆盖核对：以上命令树对 129 个后端端点逐一映射；唯一不直接暴露的是
> `GET /images/proxy` / `GET /images/assets/*`（图片代理，属 Web 渲染基础设施），
> 以 `mc lib item artwork`/`mc lib thumb` 的文件下载形态间接覆盖。

---

## 3. 基础能力一：认证（唯一需要后端配合的新增点）

现状只有 Cookie 会话，CLI 有两条路：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A. 复用会话 Cookie | `mc login` 调 `/auth/login`，把 Cookie 存本地，每次请求带上 | **后端零改动**，立刻可用 | 7/30 天过期要重登；改密全端下线殃及脚本；Cookie 语义与自动化场景不匹配 |
| B. 新增 API Token（PAT） | 后端加 `POST/GET/DELETE /auth/tokens`，签发长期令牌；`require_login` 扩展为「Cookie **或** `Authorization: Bearer <token>`」 | 定时任务/脚本稳定；可单独吊销；与插件同步令牌同构，实现有先例 | 需要一次后端小改动 |

**结论：两步走。** P0 用方案 A 先跑通（`mc login` 交互登录，凭证落
`~/.config/movieclaw/credentials`，权限 0600，永不入 shell history）；P1 落地方案 B，
`mc login` 改为「密码登录一次 → 自动换取长期 Token → 后续全用 Token」，
Cookie 路径保留为兜底。Token 后端实现直接复用插件同步令牌的存储与
`hmac.compare_digest` 校验模式，加密落库走现有 SecretBox。

安全细则：

- 凭证文件与配置文件分离（配置可进 dotfiles 同步，凭证绝不）。
- `--debug` 输出对 `Authorization`/`Cookie`/密码字段一律打码。
- 所有密文输入优先交互 prompt（不回显），其次环境变量，最后才是标志
  （标志形态在帮助文本里注明「会留在 shell 历史，不推荐」）。

## 4. 基础能力二：配置与多上下文

```toml
# ~/.config/movieclaw/config.toml
current_context = "nas"

[contexts.nas]
server = "http://192.168.1.10:3000"      # 走 Next 反代或直连后端均可
[contexts.vps]
server = "https://movie.example.com"

[defaults]
output = "table"
```

优先级（与 gcloud/kubectl 一致）：**命令行标志 > 环境变量
（`MOVIECLAW_SERVER` / `MOVIECLAW_TOKEN` / `MOVIECLAW_CONTEXT`）> 配置文件 > 内置默认**。
环境变量形态专为 CI/定时脚本设计，可完全不落盘。

## 5. 基础能力三：输出、退出码与长任务

**输出层（output）**

- `-o table`（默认，TTY）：rich 表格，列按域精心挑选（如 `mc sub list` 显示
  标题/类型/季/进度/状态/规则组），宽度自适应。
- `-o json`：**始终输出服务端 `data` 字段的原样 JSON**（拆掉信封），字段名与
  API schema 一致——这是脚本兼容性的契约，表格列怎么改都不影响 `-o json`。
- `-o yaml`：json 的等价 YAML 视图。
- `--quiet`：只输出关键标识（如创建后的 id），配合脚本捕获。

**退出码契约**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 通用业务错误（服务端 4xx，含 message 中文透传） |
| 2 | 用法错误（参数不合法，framework 层） |
| 3 | 认证失败/过期（提示 `mc login`） |
| 4 | 连不上服务器（网络/地址错误） |
| 5 | 需要确认但未给 `--yes`（非交互场景） |
| 6 | `--wait` 超时或长任务以失败收场 |

错误渲染统一走 `ErrorResponse`：stderr 打印
`错误[代码]：<message 中文原文>`，`details` 在 `--debug` 下展开。

**长任务（task）**

后端模式是「POST 启动 + 轮询进度」，CLI 统一为：

- 默认 `--no-wait`：启动即返回（回显「已开始，用 mc lib show <id> 查看进度」）。
- `--wait`：轮询进度端点/字段，TTY 下渲染进度条（复用前端的自适应节奏
  3s→30s），非 TTY 下改为按行打印阶段变化；`--timeout` 可控，Ctrl-C 只停止
  等待、不取消任务（明确告知），需要取消用对应 `stop` 子命令。
- 三类长任务的进度来源各不相同，封装在 task 模块内对上层透明：
  扫描/整理 → `GET /libraries/{id}` 内嵌字段；元数据刷新 → 独立 progress 端点；
  Agent → SSE。

## 6. 基础能力四：SSE 流式与交互

**SSE（sse 模块）**——一次实现，两处复用：

- 手写 `httpx` 流式读取 + `\n\n` 分帧（与前端刻意不用 EventSource 的理由相同：
  搜索流断线不应重放整次搜索）。
- 搜索流：`start → site_start×N → (site_result|site_error)×N → done`；
  TTY 下 stderr 实时刷「站点 x/y 完成」，结果攒到 done 后按排序/筛选输出 stdout
  ——保证管道输出仍是一份完整、稳定排序的数据，而不是乱序碎片。
- Agent 流：帧带递增 id，断线用 `Last-Event-ID` 续传 + 指数退避（500ms→5s），
  终态事件（done/error/cancelled）决定退出码。工具调用过程逐行渲染
  （类似 Claude Code 的转录视图的极简版）。

**交互（prompt 模块）**——页面交互到终端交互的四个对应物：

| 页面交互 | CLI 对应物 |
|---|---|
| `window.confirm` 删除确认 | `确认删除…？[y/N]`；`--yes` 跳过；非 TTY 无 `--yes` → 退出码 5 |
| TMDB 候选歧义海报墙 | 编号候选列表（标题/年份/简介截断），输入序号选定；`--tmdb <id>` 直接指定则跳过 |
| 目录选择器（fs/browse） | `--save-path` 等路径标志缺省时进入逐级目录浏览（列子目录、`..` 返回、回车选定）；非 TTY 必须显式给路径 |
| 密码/密钥输入框 | `getpass` 式不回显输入 |

破坏性分级：删除配置（站点/下载器/规则）普通确认；**删除条目（动磁盘文件）**
需输入条目名复述确认（同 GitHub 删仓库模式），`--yes` 也仅降级为普通确认不可完全静默。

## 7. 技术选型权衡

**语言/框架：Python + Typer + httpx + rich（推荐）**

- 赞成：与后端同栈同仓，直接复用 `movieclaw_api/schemas` 的 Pydantic 模型做
  响应类型（monorepo 内以独立可安装包形式引用 schema 子集，避免把整个服务端
  拖进 CLI 依赖）；httpx 已是后端依赖，SSE/代理经验现成；开发迭代最快。
- 反对（诚实列出）：分发不如 Go 单二进制干净——用户需要 `pipx install movieclaw-cli`
  或 `uv tool install`。缓解：① Docker 镜像内置 CLI，NAS 用户
  `docker exec movieclaw mc ...` 零安装；② 若后续确有强需求，客户端层足够薄，
  用 OpenAPI 生成 Go 客户端重写的成本可控。
- 备选 Go + cobra：单二进制、启动快，但要么手写全部类型要么依赖 OpenAPI 生成
  （生产环境当前关闭 openapi.json，需在 CI 里从代码导出 spec），双栈维护成本高。
  **当前阶段不选，但架构上不封死**：§1 的客户端层就是将来可替换的边界。

**不做的事（防过度工程）**：不做插件系统、不做本地缓存数据库（仅「最近一次
搜索结果」一个 JSON 快照文件）、不做自动更新、不内嵌任何业务逻辑、
不为「未来可能的多用户」预留抽象（产品本身单管理员）。

## 8. 仓库落位与测试

```
src/movieclaw_cli/
├── __main__.py          # 入口：movieclaw / mc 两个 console_scripts
├── core/                # config.py http.py sse.py task.py output.py prompt.py errors.py
├── client/              # 按域：auth.py search.py subscriptions.py libraries.py ...
└── commands/            # 按域：与 client 一一对应，Typer 子应用注册
tests/cli/               # 与现有 tests/ 布局一致
```

- 测试策略：client 层对着 **respx**（httpx mock）测信封拆解/错误映射/SSE 分帧；
  命令层用 Typer 的 CliRunner 测参数与输出契约（`-o json` 的字段稳定性是重点
  回归项）；再加一条复用现有 API 测试基建的端到端冒烟（起真实 app，跑
  `mc login → mc lib list`）。
- 帮助文本、错误信息全中文，与产品日志约定一致。

## 9. 实施路线（每步可独立交付、可验证）

| 阶段 | 内容 | 验证标准 |
|---|---|---|
| **P0 地基** | core 五件套 + `mc login`(Cookie) / `status` / `context` / `config`；只读命令：`sub list`、`lib list`、`site list`、`dl list`、`logs`、`health` | 远程机器上 `mc login && mc sub list -o json` 全通；退出码契约测试通过 |
| **P1 核心闭环 + Token** | 后端 PAT 端点 + Bearer 鉴权；`search`(SSE) → `download`；`sub` 全家（prepare 交互流）；`lib` 库管理与长任务（`--wait`） | 「搜索→下载→订阅→扫描入库」全流程不开浏览器完成 |
| **P2 全量设置域** | site/dl/watch/rules/llm/net/extension/appearance/ui/account 的增删改；待识别/复核/缺失工作流 | 命令树 §2 逐条对照打勾；129 端点映射核对表全绿 |
| **P3 体验打磨** | `agent run` SSE 渲染与 `chat` REPL；shell 补全；`logs -f`；`--from-last-search`；文档 | 补全脚本三 shell 可用；README 增 CLI 章节 |

## 10. 需要产品拍板的开放问题

1. **API Token 的形态**：单枚全局 Token（与插件同步令牌同款极简）还是多枚可命名
   Token（可分别吊销）？建议先单枚，够用且实现小。
2. **`mc agent chat` REPL 的优先级**：`mc agent run` 单次执行已覆盖自动化价值，
   REPL 更多是体验加分项，放 P3 是否可接受？
3. **Docker 镜像是否随 P0 就内置 CLI**（`docker exec` 即用），还是等 P1 Token
   就绪后一起上？建议随 P1，避免 Cookie 形态的凭证管理进镜像又很快被替换。
