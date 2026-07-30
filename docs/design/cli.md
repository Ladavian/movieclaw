# movieclaw CLI 化架构设计（Agent-first · OpenAPI 驱动）

> 目标与三条硬要求（来自产品）：
>
> 1. **CLI 的目标用户是 Agent**——给模型提供模型友好的工具，人类可用是副产品。
> 2. **每个新 API 自动获得 CLI 能力**——不允许「加一个接口就要手写一个命令」，
>    可用工具必须能动态发现。
> 3. **每个命令自带 help（描述 + 示例）供探索发现**——尽可能复用 OpenAPI 文档，
>    不另外维护第二份说明。
>
> 结论先行：**以 OpenAPI spec 为唯一事实源，CLI 在运行时从服务器拉取 spec、
> 动态生成命令树与 help；再叠一层很薄的「精选命令」处理跨接口工作流。**
> 后端已具备关键条件：129 个端点全部带中文 `summary`、参数带 `description`、
> 请求/响应全部是 Pydantic schema——原材料已经在代码里，缺的只是暴露与约定。

---

## 0. 前提假设

1. **CLI 是远程薄客户端**：只调 `/api/v1`，业务逻辑全在服务端。
2. **首要运行形态**：movieclaw 自带 AI 助手（movieclaw_agent）在隔离工作区执行
   bash——CLI 会被放进这个工作区，成为 Agent 操作产品本身的工具集；其次才是
   用户在自己终端远程使用。因此**非交互（non-TTY）是主形态，不是降级形态**。
3. 工具名 `movieclaw`，短别名 `mc`，Python 实现（与后端同栈同仓，见 §9）。
4. 现状硬约束（盘点结论）：认证只有 Cookie 会话（无通用 API Token）；
   2 个 SSE 端点（搜索流、Agent 运行流）；长任务全部「POST 启动 + 轮询」；
   响应统一 `ApiResponse{success,code,message,data}` 信封；敏感字段保存后不回读；
   生产环境 openapi.json 当前关闭（`app.py` 中 `docs_enabled = app_env == "local"`）。

---

## 1. 总体架构：三层命令面，覆盖率逐层收窄、体验逐层增强

```
L2 精选命令（手写，个位数）    mc sub create / mc download / mc search ...
   跨接口工作流、长任务 --wait、SSE 聚合 —— 覆盖「多步才能完成一件事」的场景
─────────────────────────────────────────────────────────────
L1 生成命令（自动，=接口数）   mc sub list / mc lib scan / mc site add ...
   运行时由 OpenAPI spec 生成：命令名、参数、校验、help、示例全部来自 spec
   —— 新 API 合入后端即自动出现，CLI 零改动
─────────────────────────────────────────────────────────────
L0 原始逃生舱（恒定 3 条）     mc api list / describe / call
   直接按 method+path 调任意端点 —— 即使 L1 映射规则不认识的新形态接口，
   Agent 也永远有一条 100% 覆盖的路可走（对标 gh api）
```

```
┌ 后端（唯一事实源）────────────────────────────────┐
│ FastAPI 路由: summary/description/operation_id     │
│ Pydantic schemas · x-cli-* 扩展元数据              │
│ → GET /api/v1/spec（鉴权后可访问的 openapi.json）  │
└──────────────┬────────────────────────────────────┘
               ↓ 拉取 + 按 hash 本地缓存
┌ CLI ──────────────────────────────────────────────┐
│ core/   config·auth·http·sse·task·output·errors   │
│ gen/    spec 加载 → 命令树构建 → 参数映射 → 调用   │
│ overlay/ 精选命令注册（同名覆盖生成命令）          │
└───────────────────────────────────────────────────┘
```

**为什么 L0 必须存在**：「新 API 自动支持」的最后保障不是生成器多聪明，而是
存在一条不依赖任何映射规则的通路。`mc api call POST /subscriptions --input body.json`
在生成器还不认识某个新端点形态时依然可用，Agent 拿着 `mc api describe` 的输出
就能自助完成调用。

---

## 2. OpenAPI 作为唯一事实源：后端要补的四件事

help、参数、校验、示例全部从 spec 来，等价于「API 文档写好，CLI 文档就写好了」。
后端需要一次性补齐并用 CI 守护（模式对标现有的 `test_auth.py` 全路由匿名扫描守护测试）：

### 2.1 生产环境暴露 spec（带鉴权）

现在 openapi.json 只在 `APP_ENV=local` 开放，原因是防匿名暴露接口面。方案不是
重开匿名 openapi.json，而是新增一个**受 `require_login` 保护**的等价端点：

```
GET /api/v1/spec        # 返回 app.openapi()，响应头带 ETag（spec 内容 hash）
```

CLI 登录后拉取，按 ETag 协商缓存到 `~/.cache/movieclaw/spec-<server>.json`；
命中缓存时启动零网络开销。**版本漂移自动解决**：CLI 永远和它面对的那台服务器
的 spec 一致，服务器升级加了接口，CLI 下次拉取即见。

### 2.2 operation_id 命名约定 → 命令名

FastAPI 默认 operation_id 冗长（函数名+路径+方法）。统一改为
`<域>.<动作>` 两段式，直接决定命令树：

```
operation_id = "sub.list"        →  mc sub list
operation_id = "sub.pause"       →  mc sub pause <id>
operation_id = "lib.scan"        →  mc lib scan <id>
operation_id = "lib.items.claim" →  mc lib items claim <file_id>
```

实现上用一个自定义 `generate_unique_id_function` 兜底 + 路由处显式声明，
CI 测试强制：全部路由 operation_id 匹配 `^[a-z][a-z0-9]*(\.[a-z0-9-]+)+$` 且唯一。

### 2.3 x-cli-* 扩展词表（写在路由装饰器 `openapi_extra` 里）

spec 标准字段表达不了的 CLI 语义，用少量扩展字段声明（声明式，不写代码）：

| 扩展字段 | 含义 | 示例 |
|---|---|---|
| `x-cli-examples` | help 里的示范用法（命令行形态，≥1 条） | `mc sub create --tmdb 693134 --seasons 1,2` |
| `x-cli-dangerous` | 破坏性等级：`confirm`（需 --yes）/ `destructive`（删磁盘，需 --yes 且回显影响面） | 删条目、删库 |
| `x-cli-long-task` | 声明这是长任务启动端点 + 进度从哪读（端点或字段路径），驱动统一 `--wait` | `{"progress": "lib.show#scan_progress"}` |
| `x-cli-stream` | SSE 端点标记 + 终态事件名 | 搜索流 / Agent 流 |
| `x-cli-hidden` | 不生成 L1 命令（纯 Web 基础设施，如图片代理），L0 仍可达 | `/images/proxy` |
| `x-cli-paged` | 分页参数名，驱动统一 `--limit/--all` | `/agent/sessions` |

**CI 守护测试**（新 API 自动支持 CLI 的强制机制）：遍历 OpenAPI 全部路由，
校验 ① summary 非空（已满足）② operation_id 合规 ③ 写操作有 description
④ `x-cli-dangerous`/`x-cli-long-task` 该标的都标了（DELETE 方法默认要求
dangerous 声明，除非显式豁免）。**漏标即 CI 红**——这一条把「自动支持」从
善意约定变成机械保证。

### 2.4 描述与示例的质量基线

- summary（已有，中文）→ 命令一行简介；docstring/description → `--help` 长说明；
  Pydantic `Field(description=...)` / `Query(description=...)` → 每个标志的说明。
- `x-cli-examples` 是新增工作量的大头，但它同时会出现在 Swagger 文档里，
  等于一份钱买两样：API 文档示例 + CLI help 示例。

---

## 3. 动态生成机制：spec → 命令树

### 3.1 运行时生成 vs 构建期生成（权衡）

| | 运行时（推荐） | 构建期代码生成 |
|---|---|---|
| 新 API 生效 | 服务器升级即生效，CLI 零发布 | 必须重发 CLI |
| 版本漂移 | 不存在（spec 来自对端服务器） | CLI 与服务器版本要配对 |
| 启动开销 | 首次拉 spec（~100KB），此后 ETag 缓存命中 | 无 |
| 静态检查 | 弱（映射逻辑要靠测试保证） | 强 |
| 离线 --help | 需有缓存（首次必须连过一次服务器） | 天然支持 |

选**运行时**：它是「每个新 API 自动支持 CLI」的唯一彻底解，启动开销靠缓存
消化；映射逻辑的正确性用「对着真实 app 导出的 spec 做快照测试」保证——后端
CI 里生成一次命令树快照，路由变更时快照 diff 一目了然。

### 3.2 参数映射规则（生成器的全部约定，刻意保持少）

| OpenAPI 元素 | CLI 形态 |
|---|---|
| path 参数 | 位置参数，按路径顺序（`/subscriptions/{id}` → `mc sub show <id>`） |
| query 参数 | `--kebab-case` 标志，类型/枚举/默认值/必填照搬 schema |
| requestBody（对象） | 顶层字段拍平成 `--标志`；嵌套对象/数组字段收折为 `--<字段>-json '<json>'`；整体替代形态 `--input body.json`（`-` 表示 stdin）三选一 |
| multipart 上传 | `--file <path>` |
| FileResponse 下载 | `--output-file <path>`（缺省打印保存到的临时路径） |
| 枚举 | 校验 + help 里列出候选值 |
| `x-cli-paged` | `--limit N` / `--all`（自动翻页聚合） |

规则之外的形态一律不猜——生成器跳过并在 `mc api list` 里标注「仅 L0 可用」，
CI 快照会暴露这类端点，届时要么扩规则要么进 L2 精选层。**不追求生成器全知全能，
追求失败显式可见。**

---

## 4. Help 体系：一份 spec，三种消费形态

### 4.1 人类形态：`--help` 逐级探索

```
mc --help                # 域列表（来自 tags + 中文描述）
mc sub --help            # 该域全部命令 + 一行简介（来自 summary）
mc sub create --help     # 长说明(description) + 全部标志(参数 description)
                         # + 示例(x-cli-examples) + 关联命令(同域推荐)
```

### 4.2 机器形态：help 是主协议，结构化目录只为「注册进工具列表」服务

**对模型而言，`--help` 本身就是发现协议**——Agent 在工作区跑 bash 时，
`mc --help → mc sub --help → mc sub create --help` 的逐级探索对模型和对人
同样自然，模型读帮助文本毫无障碍。因此**不为「模型发现能力」单设机制**，
help 就是唯一入口；4.1 的三级 help 同时服务人类与模型。

结构化自描述只保留两个明确用途，都不是给模型「读」的：

```
mc api list / describe / call    # L0 逃生舱本身的组成部分：list 用于找到
                                 # method+path，describe 给出原始参数 schema，
                                 # 没有它们 call 无从下手（对标 gh api）
mc capabilities                  # 【为 §6.1 集成形态 B 预留】输出全部命令的
                                 # name/description/input_schema JSON——当 CLI
                                 # 命令要注册成 Agent tool 列表里的一等工具
                                 # （function calling）时，消费方是「程序」
                                 # 而非「模型」，help 文本满足不了 schema 需求
```

若集成形态 B（见 §6.1）最终不做，`mc capabilities` 随之取消——它不是
独立价值点，只是形态 B 的前置件。

### 4.3 错误即帮助

Agent 最常见的「学习方式」是试错。因此错误输出必须携带修正路径：

```json
{"success": false, "code": "VALIDATION_ERROR",
 "message": "缺少必填参数 --tmdb",
 "hint": "用法示例：mc sub create --tmdb 693134 --seasons 1,2；详见 mc sub create --help"}
```

参数校验错、404、业务错（服务端中文 message 直接透传）都附 `hint`；
未知命令时基于编辑距离给「你是不是想用 …」。

---

## 5. Agent 友好设计准则（本方案的核心约束，逐条可测试）

1. **默认零交互。** 任何路径下都不会挂起等待输入。需要确认 → 没给 `--yes`
   就以退出码 5 失败并说明；有歧义 → 把候选作为结构化数据返回（见第 4 条）。
   交互式提示只在「显式 TTY + 人类模式」下才可能出现。
2. **非 TTY 默认输出 JSON。** Agent 场景自动命中；`-o json` 输出的是服务端
   `data` 字段原样（拆信封），字段名与 API schema 一致——这是稳定契约，
   任何「表格列怎么排」的调整都不影响它。TTY 下默认 table（人类副产品）。
3. **一次调用 = 一个完整结果（阻塞语义优先）。** 与人类 CLI 相反：
   - 长任务默认 `--wait`（轮询到终态才返回，超时可控，`--no-wait` 才立即返回）；
   - `mc search` 内部走 SSE，但默认输出**聚合完成后的稳定结果**，站点进度
     打到 stderr；`--stream-events` 才逐帧输出 NDJSON（给需要增量的调用方）。
   Agent 的心智是「调用工具 → 拿到结果」，不是「盯着进度条」。
4. **歧义是数据，不是对话。** `mc sub create --title "沙丘"` 命中多个 TMDB
   候选时，返回退出码 7 + 候选清单 JSON（id/标题/年份/简介摘要）+
   hint「重跑并指定 --tmdb <id>」。多轮消歧靠 Agent 的多次工具调用完成，
   每次调用自身保持无状态。
5. **输出有预算。** 列表默认 `--limit`（各域给合理默认，如 50），截断时在
   stderr 明示「共 312 条，已截断，--all 取全量」；长文本字段（简介、日志）
   默认截断带标记。上下文窗口是 Agent 的稀缺资源，多余输出就是伤害。
6. **破坏性操作显式化。** `x-cli-dangerous` 驱动：`confirm` 级需 `--yes`；
   `destructive` 级（删磁盘文件）需 `--yes` 且执行前回显影响面（条目名、
   文件数、路径）到 stderr。help 与 `mc api list` 里都标注 ⚠，让模型在
   选工具阶段就看见风险等级。
7. **幂等与可重试友好。** 服务端已有的幂等语义（重复订阅幂等返回）在 help 中
   写明；网络错误自动重试仅限只读请求（GET），写请求失败原样报错绝不自动重发。
8. **退出码契约**：0 成功 / 1 业务错误 / 2 用法错误 / 3 认证失败 / 4 连不上
   服务器 / 5 缺 `--yes` / 6 长任务失败或超时 / 7 歧义待消解。

---

## 6. 内置到产品 Agent：两种集成形态与自动授权

CLI 的第一消费者是产品自带的 AI 助手（movieclaw_agent，隔离工作区跑 bash）。
「CLI 进入 Agent 的 tool 列表」有两种形态，授权机制两者共用：

### 6.1 集成形态

| | 形态 A：bash + 工作区（推荐先做） | 形态 B：命令注册为一等工具 |
|---|---|---|
| 做法 | CLI 装进 Agent 工作区，模型通过已有 bash 工具调用，靠 `--help` 探索 | 启动时用 `mc capabilities` 拉命令目录，把每条命令注册成独立 tool（function calling，带 input_schema） |
| 改动量 | 几乎为零（工作区镜像加一个包 + 注入两个环境变量） | Agent 模块要做目录拉取、工具注册、参数拼装到 argv 的桥接层 |
| 模型体验 | 通用 bash 心智，组合能力强（管道、jq）；需要多轮 help 探索 | 工具即目录，schema 强约束参数，选择更稳、幂次更少 |
| 风险面 | bash 是全能工具，边界靠工作区隔离 | 工具面收窄到白名单命令，可按危险等级过滤注册 |

**结论：P1 落地形态 A（成本趋近于零，立刻可用）；形态 B 作为后续演进**——
它的全部前置件只有 `mc capabilities`，且注册时可以只挑非 destructive 命令，
把「Agent 能碰什么」变成注册期的白名单决策。两形态不互斥，可共存。

### 6.2 自动授权：按 run 签发的短时效内部令牌（Agent 全程零登录）

```
用户发起 Agent 任务
  → agent 模块创建 run，向认证服务申请内部令牌
      令牌 = itsdangerous 签名（复用现有会话签名密钥，新 salt
             "movieclaw.agent-token.v1"），负载 {aud:"agent", run_id, exp}
      —— 无状态、不落库、无需新增存储
  → 拉起隔离工作区时注入环境变量：
      MOVIECLAW_SERVER=http://127.0.0.1:8000   （同容器回环直连）
      MOVIECLAW_TOKEN=<内部令牌>
  → CLI 环境变量优先级最高 → 每个请求自动带 Bearer → 零配置、零交互
  → 服务端 require_login 扩展：Cookie 或 Bearer（PAT / agent 令牌同一入口验签）
```

关键性质（选无状态签名令牌而非落库 PAT 的理由）：

- **生命周期 = run 生命周期**：`exp` 取 run 最大时长（如 2 小时），run 结束
  令牌自然作废，不需要吊销存储；长会话续聊时每次新 run 重新签发。
- **全局熔断免费获得**：管理员改密会轮换签名密钥（现有机制），所有 agent
  令牌与会话一起瞬间失效——安全兜底不用另写。
- **可审计**：令牌负载带 `run_id`，服务端访问日志可把每一次 CLI 调用归因到
  具体的 Agent 运行，配合订阅/媒体库已有的活动时间线，「是谁改的」可回答。
- **与用户手动 PAT 正交**：用户在自己终端用的长期 PAT（P1 的 /auth/tokens）
  走落库 + 可命名可吊销；agent 内部令牌走无状态短时效。两者验签入口同一个，
  实现共享，语义不混。

破坏性操作的双保险：即便持有效令牌，CLI 的危险门槛（`--yes`、destructive
回显影响面）依然生效；若采用形态 B，还可在注册期直接不注册 destructive 命令。

## 7. L2 精选命令层：只收「跨接口的工作流」，个位数

生成层覆盖单接口调用，以下场景一条命令背后是多个接口的编排，值得手写
（同名注册即覆盖生成命令，其余全部放行给生成层）：

| 命令 | 编排内容 |
|---|---|
| `mc sub create` | prepare（歧义→退出码 7 候选清单）→ dispatch-preview（投递预检结论回显）→ create |
| `mc search "关键词"` | SSE 聚合 + 客户端侧筛选排序标志（--resolution/--sort…，对应前端筛选弹层）+ 结果快照落本地供 `mc download` 引用 |
| `mc download <行号|site:url>` | 读上次搜索快照 → `POST /downloaders/submit`，回显三级兜底路由结论（会/不会自动入库） |
| `mc lib organize <id>` | `--dry-run` 走 preview；正式执行强制先 preview 回显影响面再执行 |
| `mc agent run "任务"` | start → SSE 渲染（工具调用逐行）→ 终态定退出码；`--detach`/`attach`（Last-Event-ID 续传）/`cancel` |
| `mc login` | bootstrap 探测 → 密码登录 → （P1 起）自动换取长期 Token |
| `mc status` | health + auth/me + spec 版本，一眼看部署状态 |
| `mc logs -f` | 轮询模拟 follow |

预计 8 条左右。**准入标准：需要编排或本地状态才收进 L2；单接口的便利包装
一律不收**（那是生成层 + x-cli 元数据该解决的事）。

---

## 8. 基础能力层（core/，与生成无关的地基）

### 8.1 认证（唯一需要后端新增的功能点）

- **P0**：`mc login` 走 Cookie 会话（后端零改动），凭证落
  `~/.config/movieclaw/credentials`（0600）。
- **P1**：后端新增 PAT——`POST/GET/DELETE /auth/tokens`，`require_login` 扩展为
  「Cookie 或 `Authorization: Bearer <token>`」，实现直接复用插件同步令牌的
  加密落库（SecretBox）与 `hmac.compare_digest` 校验模式。
- **产品内 Agent 的自动授权**：按 run 签发的无状态短时效令牌 + 工作区环境
  变量注入，详见 §6.2；与用户手动 PAT 共用同一个 Bearer 验签入口。
- `--debug` 输出对 Authorization/Cookie/密码打码；密钥输入优先环境变量与
  `--input` 文件，标志形态在 help 里注明会留 shell 历史。

### 8.2 配置与多上下文

`~/.config/movieclaw/config.toml`，`[contexts.*]` 多服务器；优先级
**标志 > 环境变量（MOVIECLAW_SERVER/TOKEN/CONTEXT）> 配置文件 > 默认**。
环境变量形态是 Agent/CI 的主通道，可完全不落盘。

### 8.3 http / sse / task / output / errors

- **http**：httpx 封装——认证注入、超时、GET 自动重试、信封拆解、
  `ErrorResponse → 中文错误 + hint + 退出码` 映射。
- **sse**：手写分帧（`\n\n`），搜索流事件序列聚合；Agent 流 `Last-Event-ID`
  续传 + 指数退避（500ms→5s）。与前端刻意不用 EventSource 的理由相同。
- **task**：统一 `--wait`——进度来源由 `x-cli-long-task` 声明（内嵌字段 /
  独立端点 / SSE 三形态），轮询节奏自适应（3s→30s），Ctrl-C 只停等待不取消
  （明确告知，取消用对应 stop 命令）。
- **output**：stdout 只放数据、stderr 放进度与提示；`-o table|json|yaml`；
  `--quiet` 只输出关键标识（如新建资源 id）；NO_COLOR 与非 TTY 自动无色。

---

## 9. 技术选型与仓库落位

**Python + click（动态构建命令树）+ httpx + rich。**

- 与后端同栈同仓；httpx 已是后端依赖；**运行时动态生成命令树**要求框架支持
  程序化注册——click 的 `Group`/`Command` 对象模型天然适合（Typer 偏静态
  装饰器风格，动态场景反而别扭，L2 精选命令仍可享受类型标注的舒适度有限，
  故整体选 click）。
- 分发：① Docker 镜像内置（产品内 Agent 与 `docker exec` 用户零安装）；
  ② `pipx install movieclaw-cli` / `uv tool install`（远程人类用户）。
- 不做的事（防过度工程）：不做插件系统、不做本地数据库（仅「上次搜索快照」
  一个 JSON 文件）、不做自动更新、不内嵌业务逻辑、不在 P0 做 MCP 壳
  （capabilities 输出已为此留好接口形态）。

```
src/movieclaw_cli/
├── __main__.py        # movieclaw / mc 入口
├── core/              # config.py auth.py http.py sse.py task.py output.py errors.py
├── gen/               # spec_loader.py（拉取+ETag缓存） tree_builder.py（映射规则）
│                      # invoker.py（参数→请求） helpgen.py（spec→help渲染）
├── overlay/           # L2 精选命令，每条一个模块
└── rawapi.py          # L0：mc api list / describe / call
tests/cli/             # respx 测 core；对真实 app 导出 spec 做命令树快照测试
```

后端改动集中且小：`GET /api/v1/spec`、operation_id 约定、x-cli-* 标注、
CI 守护测试、（P1）PAT 端点。全部是元数据与鉴权层面，不动业务逻辑。

---

## 10. 实施路线

| 阶段 | 内容 | 验证标准 |
|---|---|---|
| **P0 地基 + 逃生舱** | 后端：`/spec` 端点 + operation_id 约定 + CI 守护测试。CLI：core 全套、`mc login`(Cookie)、`mc api list/describe/call`、`mc capabilities`、`mc status` | Agent 仅凭 `mc api list → describe → call` 能完成任意接口调用；capabilities 输出通过 JSON Schema 校验 |
| **P1 生成层 + Token** | gen/ 映射规则全量落地；x-cli-* 标注铺完 129 端点；后端 PAT + Agent 工作区令牌注入；长任务 `--wait`、分页、危险确认 | 命令树快照 = 全部非 hidden 端点；产品内 Agent 工作区里 `mc sub list` 零配置跑通；漏标元数据 CI 红 |
| **P2 精选层 + 流式** | L2 八条命令（sub create 消歧流 / search+download / organize / agent run…）；SSE 两处 | 「搜索→下载→订阅→扫描入库」全流程由 Agent 通过 bash 调 CLI 完成，全程零交互 |
| **P3 打磨** | 错误 hint 全覆盖、编辑距离建议、shell 补全、`logs -f`、README/示例扩充 | 抽样端点的 --help 含示例率 100%；退出码契约回归测试全绿 |

## 11. 需要产品拍板的开放问题

1. **Agent 令牌是否需要权限降级**：§6.2 的方案默认 agent 令牌与管理员同权
   （危险门槛由 CLI 的 `--yes`/影响面回显兜底）。是否要在令牌负载加 scope、
   服务端直接拒绝 agent 令牌执行 destructive 端点？更安全但多一层实现，
   建议先不做、观察形态 A 的实际使用后再定。
2. **集成形态 B（命令注册为一等工具）的启动时机**：形态 A 成本趋近于零先上；
   形态 B 需要 Agent 模块做注册桥接层，建议等形态 A 用出真实痛点
   （help 探索轮次过多、参数出错率高）再投入。
3. **x-cli-examples 的铺设节奏**：129 端点全铺工作量可观，是否接受 P1 只给
   写操作与危险操作铺示例、读操作靠 summary + 参数说明兜底？
