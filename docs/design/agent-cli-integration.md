# 产品内 Agent 集成 mclaw：知识分层与提示词设计

> 背景：movieclaw_agent 已有 bash/read/write/edit 四个基础工具，P1 已完成
> 工作区自动授权（每次运行注入 MOVIECLAW_SERVER / MOVIECLAW_TOKEN，bash 里
> `mclaw ...` 直接可用）。**缺的最后一环是让模型「知道并正确使用」mclaw**
> ——这是纯提示词与工具描述工程，本文细化到可逐段评审的文案。
>
> 集成形态沿用 docs/design/cli.md §6.1 的决策：形态 A（bash + 提示词）先行，
> 形态 B（命令注册为一等工具）预留，文末附简要展望。

---

## 0. 设计约束（来自现有提示词架构）

`movieclaw_agent/prompts.py` 的既有原则必须遵守：

1. **正文只写通用行为准则**，不含领域词汇；
2. **领域语义由工具 description 与运行时环境段承载**；
3. **会变的事实绝不写死**，由 `build_system_prompt(extra_environment)` 运行时拼接。

由此推出本设计的核心问题：mclaw 的知识（它存在、怎么发现用法、怎么判读结果、
什么能做什么不能做）应该**拆开放在哪几层**，每层怎么防漂移。

---

## 1. 知识分层：四层各放什么

```
L1 bash 工具 description     「mclaw 存在且已授权」——一句话（模型选工具时必看的位置）
L2 系统提示词「mclaw 规程」段  稳定的使用协议：发现/判读/危险/长任务/禁区（纯准则，零命令清单）
L3 运行时环境段               动态事实：命令族清单 + 精选捷径（渲染自 spec，永不漂移）
L4 按需发现（不占提示词）      参数细节靠 mclaw <域> <命令> --help 现查
```

分层的判断标准，与「什么进提示词、什么靠发现」的取舍：

| 知识 | 放哪 | 理由 |
|---|---|---|
| mclaw 存在、已授权 | L1 + L2 开头 | 不告知则模型根本不会想到用；授权已配好必须显式说，否则模型会先跑 login |
| 使用协议（退出码/stdout-stderr 分工/--yes 规约） | L2 | 稳定不变、每次任务都用到，值得常驻；写成准则而非清单 |
| 有哪些命令族、精选捷径 | L3 | 会随版本变 → 必须从 spec 生成；只给「地图」不给「说明书」，控制 token |
| 每条命令的参数细节 | L4 | 126 条命令全量进提示词 ≈ 上万 token，而 --help 一次现查 ≈ 数百 token 且永远准确 |
| 部署状态（站点/下载器/库接入情况） | 不进（一期） | 模型一条 `mclaw status` / `mclaw site list` 就能查到，预注入是重复投资；二期可选 |

**防漂移机制**：L3 由代码从内置 spec 渲染（同仓构建 → 与服务器严格同版）；
L2/L1 里**禁止出现任何具体命令清单**（守护测试断言 L2 文案不含「mclaw <域> <动词>」
形态的枚举），改版本时无需同步维护提示词。

Token 预算：L1 一句（~30 token）+ L2（~450 token）+ L3（~220 token）≈ 700 token，
一次性成本，换掉的是模型每次任务开头的多轮盲目探索。

---

## 2. L1：bash 工具 description 修改稿（评审点 ①）

bash 工具属于通用的 movieclaw_agent 包，不能写死领域内容——给工厂加一个可选
`extra_note` 参数，由 API 层（知道 mclaw 存在的那一层）传入：

```python
# movieclaw_agent/tools/bash.py
def make_bash_tool(workdir, extra_env=None, extra_note: str | None = None): ...
# description = _DESCRIPTION + ("\n" + extra_note if extra_note else "")

# movieclaw_api/api/routes/agent.py 传入：
_MCLAW_NOTE = (
    "工作区已安装本产品的命令行工具 mclaw（授权已自动配置）："
    "对 movieclaw 本身的一切操作——搜索、订阅、媒体库、下载、设置——都通过执行 mclaw 完成。"
)
```

> 为什么放工具描述而不只放提示词：模型决定「用哪个工具做这件事」时，
> 权重最高的上下文就是各工具的 description；这一句让「操作产品 → 选 bash → 跑
> mclaw」的链路在选择工具那一刻就成立。

## 3. L2：系统提示词「mclaw 规程」段全文草案（评审点 ②）

追加为 `prompts.py` 的独立常量 `MCLAW_PROMPT`，拼在通用正文之后。全文如下，
每一条的设计理由用引用块标注（正式文案不含引用块）：

```markdown
# movieclaw 操作规程（mclaw）
工作区已安装 mclaw，你对本产品的一切操作都通过在 bash 里执行它完成。
授权已自动配置：绝不要执行 mclaw login / logout，绝不要设置或打印任何令牌与环境变量。
```

> 「绝不打印令牌」：MOVIECLAW_TOKEN 在工作区环境变量里，必须显式禁止回显，
> 否则一句 `env` 就会把凭证吐进对话转录。

```markdown
## 用法发现
- 可用命令族见「环境」段。参数细节永远用 `mclaw <域> --help`、`mclaw <域> <命令> --help` 现查，不要凭记忆猜参数或猜取值。
- 优先用一条命令完成一个意图：搜索用 `mclaw search "关键词"`，下载搜索结果用 `mclaw download <行号>`，订阅用 `mclaw sub create`——这些命令内置了多步编排（消歧、预检、确认），比自己拼多个底层命令更不容易出错。
```

> 「现查不猜」对应实测痛点：模型最常见的失败是臆造参数名；
> 「优先精选命令」把 cli.md §7 的编排价值显式告知，否则模型可能自己串底层接口。

```markdown
## 结果判读
- stdout 是数据（此环境下默认 JSON），stderr 是过程提示与错误说明——判断成败看退出码，找原因看 stderr。
- 退出码约定：0 成功；1 业务错误（stderr 有中文原因与提示）；2 用法错误（先 --help 纠正再试）；3 授权失效（不要重试，直接向用户报告）；5 该操作需要 --yes 确认；6 等待超时但任务仍在后台执行；7 歧义——stdout 里是候选清单，向用户确认或据上下文选定后带 --tmdb 重跑。
- 列表输出默认有条数上限且长字段会截断；需要完整数据时按 help 里的 --limit / --all 调整，不要基于截断结果下结论。
```

> 退出码逐条展开是值得的：它把「试错学习」变成「查表决策」，尤其 7（歧义）
> 和 5（要确认）各自蕴含明确的下一步动作。

```markdown
## 危险操作（带 ⚠ 的命令）
- 普通确认级（删除配置、清理记录等）：当用户的任务明确要求这件事时，直接加 --yes 执行；任务只是隐含或顺带涉及时，先向用户确认。
- 破坏级（`mclaw lib items delete`，会删除磁盘上的媒体文件）：必须先执行只读命令查清将被删除的具体条目，把片名与文件数报给用户，取得**本轮对话中的明确同意**后才能加 --yes 执行。「清理一下」「整理媒体库」这类泛指授权不构成删除文件的同意。
- 拿不准影响面时，先用 list / show / preview / --dry-run 类只读命令确认。
```

> 两级规约与 x-cli-dangerous 的 confirm/destructive 一一对应；「泛指授权不构成
> 同意」这句是给模型的裁决标准，避免「用户说了整理，所以我删了」的事故。

```markdown
## 长任务
- 扫描 / 整理 / 元数据刷新默认会阻塞等待到完成。预计耗时超过 4 分钟的（大库首扫、整库刷新），改用 --no-wait 启动，随后用对应的查询命令轮询进度——bash 单条命令有执行超时，别让等待撞上它。
```

> 4 分钟阈值 = bash 工具 300s 默认超时留 20% 余量；轮询命令名不写死
> （lib show / lib refresh progress 会出现在 --help 与错误 hint 里）。

```markdown
## 禁区
- 不要执行 mclaw agent 域的任何命令——那是你自己的运行入口，会造成递归。
- 不要修改 ~/.config/movieclaw 下的任何文件。
```

> 递归禁令是提示词层的软约束，服务端还有硬闸（见 §5）；配置目录禁改防止
> 模型「帮忙修配置」把凭证与上下文改坏。

## 4. L3：运行时环境段生成器（评审点 ③）

新增 `movieclaw_api/services/agent_env.py`，从 **CLI 内置 spec** 渲染命令族
清单（进程内缓存一次；数据源与 mclaw 自身完全同源，天然不漂移）：

```python
def render_mclaw_environment() -> str:
    """渲染 mclaw 命令族清单，追加到系统提示词的「环境」段。"""
    # 数据源：movieclaw_cli.gen.spec_loader.load_baseline() +
    #        tree_builder.iter_operations()（域 → 生成命令计数）
    #        tree_builder.DOMAIN_HELP（域的一行中文简介）
```

渲染输出示例（~220 token，供评审格式）：

```markdown
## mclaw 命令族（详情用 --help 现查）
- search 站点资源搜索 ｜ sub 订阅 ｜ lib 媒体库 ｜ discover 发现与元数据
- site PT 站点配置 ｜ dl 下载器 ｜ watch 监听导入 ｜ rules 订阅规则组
- llm AI 模型 ｜ net 网络与代理 ｜ logs 系统日志 ｜ people 影人档案
- auth 账号与 API 令牌 ｜ appearance 外观 ｜ extension 插件同步 ｜ fs 目录浏览
- 常用捷径：mclaw status（部署状态）｜ mclaw search "关键词" ｜ mclaw download <行号> ｜ mclaw sub create ｜ mclaw lib organize <库id>
```

接线（`routes/agent.py` 的 start_agent）：

```python
params = AgentStartParams(
    input=payload.input,
    history=history,
    model=payload.model,
    system_prompt=build_system_prompt(extra_environment=render_mclaw_environment()),
)
```

不做的事（按简洁原则）：不预注入站点/下载器/库的接入状态（`mclaw status`、
`mclaw site list` 一步可查）；不注入任何命令的参数说明（L4 职责）；不做
「常见任务示例库」（先看真实使用暴露的痛点再定）。

## 5. 安全硬闸：服务端禁止 Agent 递归（评审点 ④）

提示词禁令之外加服务端硬闸——`agent.start` 拒绝来自 Agent 工作区令牌的调用：

```python
# routes/agent.py
async def start_agent(
    payload: AgentStartPayload,
    identity: str = Depends(require_login),   # Bearer 验签后 agent 令牌返回 "agent:<sid>"
    ...
):
    if identity.startswith("agent:"):
        raise BadRequestException(
            "Agent 工作区内不能再发起新的 Agent 运行（禁止递归）；"
            "请直接在当前会话中完成任务"
        )
```

依赖缓存保证 require_login 不会重复验签。同理由（对称性）不拦其余 agent 域
读接口——工作区里查会话列表无害；只有 start 会制造递归。

## 6. 落点与测试

| 改动 | 文件 | 性质 |
|---|---|---|
| `MCLAW_PROMPT` 常量 + 拼接 | `movieclaw_agent/prompts.py` | 新增一段，正文不动 |
| bash `extra_note` 参数 | `movieclaw_agent/tools/bash.py`、`tools/__init__.py` | 可选参数，默认行为不变 |
| 环境段生成器 | `movieclaw_api/services/agent_env.py`（新） | 渲染自 CLI 内置 spec |
| start_agent 接线 + 递归硬闸 | `movieclaw_api/api/routes/agent.py` | 两处小改 |

测试：
1. **提示词快照测试**：`build_system_prompt(render_mclaw_environment())` 全文快照
   ——提示词是产品行为，改动必须显式过评审（快照 diff 即评审入口）；
2. **同步守护**：环境段里的域集合 == spec 生成命令的域集合（新增域忘了配
   DOMAIN_HELP 即红）；
3. **禁枚举守护**：断言 MCLAW_PROMPT 不含具体子命令枚举（防止后来人往 L2 塞清单）；
4. **递归硬闸测试**：持 agent 令牌调 `POST /agent/start` → 400 中文；
5. **端到端冒烟（人工）**：真实模型跑三条 golden 任务——「我的订阅有哪些」
   （只读）、「订阅沙丘2」（消歧+预检链路）、「把 1 号库整理一下」（危险确认
   链路，验证模型先报影响面再执行）。

## 7. 展望：形态 B（不在本期）

当形态 A 用出真实痛点（help 探索轮次过多、参数错误率高）时，升级路径已备好：
Agent 模块读内置 spec + `tree_builder.iter_operations()` 直接生成工具注册表
（每条命令一个 tool，input_schema 来自参数映射），执行器拼 argv 调 mclaw。
注册期可按 x-cli-dangerous 做白名单（如不注册 destructive 命令）。届时 L2/L3
大幅缩水（工具即目录），本文的分层原则依然成立。

## 8. 待你拍板的评审点汇总

① bash description 的 extra_note 措辞与注入方式；
② MCLAW_PROMPT 六个小节的逐段文案（尤其危险操作的两级裁决标准、「泛指授权
不构成同意」的表述）；
③ 环境段的信息密度（现案只有域清单 + 捷径；要不要加部署状态摘要）；
④ 递归硬闸只拦 start 不拦读接口，是否符合预期。
