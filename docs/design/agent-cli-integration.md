# 产品内 Agent 集成 mclaw：独立工具设计（tool 描述 + 提示词思路）

> 背景：movieclaw_agent 已有 bash/read/write/edit 四个基础工具，P1 已完成
> 工作区自动授权。**集成方式定为：新增一个独立的 `mclaw` 工具**——不在 bash
> 描述上捎带，而是让模型在「选工具」这一层就看见一个明确的产品操作入口，
> 工具描述本身携带一级服务目录（有哪些 router/命令族可用）。
>
> 这与现有提示词架构完全同构：`prompts.py` 的既有原则是「正文只写通用行为
> 准则，**领域语义由各工具的 description 承载**」——mclaw 的一切知识都放进
> 它自己的工具描述，系统提示词正文**零改动**。

---

## 0. 为什么独立工具优于 bash 捎带（本设计的三个硬收益）

1. **选择面清晰**：模型决定「用什么做这件事」时看的是工具列表。独立的
   `mclaw` 工具让「操作产品 → 选 mclaw」一步成立，不需要模型先想到 bash
   再想起里面装了个 CLI；bash 回归纯粹的通用 shell 定位。
2. **令牌隔离（安全升级）**：MOVIECLAW_TOKEN 只注入 mclaw 工具的子进程，
   **不再进 bash 的环境**——bash 里 `env`/`echo $MOVIECLAW_TOKEN` 从此拿不到
   凭证，泄漏面从「整个 shell」收窄到「一个不透传环境的专用工具」。
3. **硬闸位点**：递归禁令（不允许在工具里再调 `mclaw agent ...`）从提示词
   软约束升级为工具 handler 里的代码硬闸，模型绕不过去。

---

## 1. 工具定义（评审点 ①：参数面与执行语义）

```python
# movieclaw_agent/tools/mclaw.py
def make_mclaw_tool(
    workdir: Path,
    extra_env: dict[str, str],      # MOVIECLAW_SERVER / MOVIECLAW_TOKEN（只给本工具）
    service_map: str,               # 一级服务目录文本，由 API 层从 spec 渲染后传入
) -> AgentTool: ...
```

参数 schema（刻意最小——mclaw 自身就是完整的参数体系，工具不再重复建模）：

```json
{
  "type": "object",
  "properties": {
    "args": {
      "type": "string",
      "description": "mclaw 后面的完整参数串（不含 mclaw 本身），如 'sub list' 或 'search \"沙丘2\" --resolution 2160p'"
    },
    "timeout": {
      "type": "number",
      "description": "超时秒数（可选，默认 300；长任务等待时适当调大）"
    }
  },
  "required": ["args"]
}
```

执行语义：

- **`shlex.split(args)` 后以 argv 直接执行 mclaw 可执行文件，不经 shell**
  ——没有管道/重定向/变量展开，注入面为零；需要组合处理输出时，模型把
  JSON 结果交给 bash/read 等其他工具，职责分明。
- 子进程环境 = 进程环境 + extra_env（令牌仅此处注入）；cwd = 工作区。
- **硬闸**：`args` 的首个 token 为 `agent` 时直接返回错误文本
  「不能在工具里调用 mclaw agent——那是你自己的运行入口（禁止递归）。
  请直接在当前会话完成任务」，不执行子进程。`login`/`logout` 同样拦截
  （授权已配置，执行只会破坏凭证状态）。
- 输出组装（复用 bash 工具的截断实现）：

```
<stdout 截断后内容>
[stderr]
<stderr 截断后内容>
[退出码 7：结果有歧义——stdout 是候选清单，选定后按提示重跑]
```

  **退出码语义标注是本工具独有的运行时教学**：handler 按退出码契约附一行
  中文解读（0 不标注；1 业务错误看 stderr；2 用法错误先 --help；3 授权失效
  应停止并报告用户；5 需要 --yes；6 等待超时任务仍在后台；7 歧义待选）。
  模型即使没读过任何文档，也能从工具结果本身学会正确的下一步。

## 2. 工具 description 全文草案（评审点 ②：一级服务目录 + 使用协议）

description = 静态协议文本 + 动态服务目录（`service_map` 拼接）。全文如下：

```text
movieclaw 的官方命令行工具。对本产品的一切操作——搜索资源、订阅、媒体库、
下载、站点/下载器/规则等全部设置——都用本工具完成，不要用 bash 直接调 API。
授权已自动配置，永远不需要 login。

可用服务（一级目录，参数细节用 --help 现查，如 args="sub --help"）：
- search   站点资源搜索：search "关键词" 流式聚合出带行号的结果
- download 下载：download <行号> 提交上次搜索的某行（或 --site-id + --url）
- sub      订阅：sub create 一步完成消歧/预检/创建；list/show/update/pause/delete
- lib      媒体库：库管理、scan 扫描、organize 整理、items 条目、
           unidentified 待识别认领、missing 缺失重下、review 身份复核
- site     PT 站点接入与验证 ｜ dl 下载器接入与默认设置
- watch    监听导入规则 ｜ rules 订阅过滤规则组
- discover 影视元数据与榜单 ｜ people 影人档案（本地库）
- llm      AI 模型供应商 ｜ net 网络与代理 ｜ logs 系统日志
- auth     账号/API 令牌 ｜ status 一眼看部署与登录状态

使用协议：
- 输出即数据：stdout 是 JSON（默认），stderr 是过程提示与错误原因。
- 参数拿不准就先 --help（域级和命令级都有，含示例），不要凭记忆猜。
- 列表默认有条数上限、长字段有截断；下结论前确认没有被截断（--limit 可调）。
- 带 ⚠ 的命令需要 --yes 确认。其中 lib items delete 会删除磁盘上的媒体文件：
  必须先用只读命令查清将删除的具体条目、向用户复述并取得本轮明确同意后才能
  执行；用户泛泛说「清理/整理」不构成删除文件的同意。其余 ⚠ 命令（删配置、
  清记录）在用户任务明确要求时可直接 --yes。
- 扫描/整理/元数据刷新默认阻塞等待完成；预计超过 4 分钟的任务用 --no-wait
  启动，再用对应查询命令轮询进度，或调大本工具的 timeout 参数。
```

设计取舍说明（供评审）：

- **一级目录进 description，二级以下坚决不进**：目录 17 行 ≈ 300 token，让
  模型「知道去哪个域找」；126 条命令的参数细节交给 --help 现查（L4），
  否则 description 膨胀到数千 token 且必然漂移。
- **目录不是手写的**：`service_map` 段由 API 层从 CLI 内置 spec +
  `tree_builder.DOMAIN_HELP` 渲染（含每个域的一行说明与关键子能力），
  同仓构建保证与真实命令面严格同版；上面草案即渲染目标格式。
- **危险规约放 description 而非系统提示词**：这是 mclaw 的领域语义，按
  现有架构归工具承载；且模型每次调用工具时 description 都在注意力窗口里，
  比相隔很远的系统提示词更「贴现场」。
- **退出码表不进 description**：由工具结果的运行时标注承载（§1），
  省 token 且教在事发现场。

## 3. 系统提示词与其他工具：几乎零改动（评审点 ③）

- `prompts.py` 正文：**不改**。通用准则（先查证、并行调用、工作循环）
  已经覆盖 mclaw 的使用姿势；领域语义全部在工具 description。
- 环境段：**不改**（仍只有日期）。部署状态一条 `status` 就能查，不预注入。
- bash 工具：**revert P1 的 extra_env 注入**（令牌改为只进 mclaw 工具），
  description 不加任何 mclaw 内容。bash/read/write/edit 回归纯工作区定位。
- 装配（`routes/agent.py`）：

```python
def get_agent_tools(cli_env: dict[str, str]) -> list[AgentTool]:
    workdir = ...
    return [
        *builtin_tools(workdir),                      # bash 不再携带 cli_env
        make_mclaw_tool(workdir, cli_env, render_service_map()),
    ]
```

`render_service_map()` 放 `movieclaw_api/services/mclaw_tool.py`：数据源为
`movieclaw_cli.gen.spec_loader.load_baseline()` + `tree_builder`（域 →
DOMAIN_HELP 简介 + 关键子命令），进程内缓存一次。

## 4. 安全设计（双硬闸 + 令牌收窄）

| 防线 | 位置 | 拦什么 |
|---|---|---|
| 工具 handler 硬闸 | `make_mclaw_tool` | `agent` 子命令（递归）、`login/logout`（破坏凭证） |
| 服务端硬闸 | `agent.start` 路由 | 持 `agent:` 身份令牌的再发起（防绕过工具直接 curl） |
| 令牌收窄 | 只注入 mclaw 工具子进程 | bash 里 `env` 再也看不到 MOVIECLAW_TOKEN |

服务端硬闸实现（同前版设计）：

```python
async def start_agent(payload, identity: str = Depends(require_login), ...):
    if identity.startswith("agent:"):
        raise BadRequestException("Agent 工作区内不能再发起新的 Agent 运行（禁止递归）")
```

## 5. 落点与测试

| 改动 | 文件 | 性质 |
|---|---|---|
| mclaw 工具（schema/描述/handler/硬闸/退出码标注） | `movieclaw_agent/tools/mclaw.py`（新） | 核心 |
| 截断工具函数抽公用 | `tools/bash.py` → `tools/_output.py` | 小重构 |
| bash 撤销 extra_env | `tools/bash.py`、`tools/__init__.py` | revert |
| 服务目录渲染器 | `movieclaw_api/services/mclaw_tool.py`（新） | 渲染自 spec |
| 装配 + 递归服务端硬闸 | `movieclaw_api/api/routes/agent.py` | 两处小改 |

测试：
1. **description 快照测试**：完整工具描述（含渲染目录）全文快照——描述是
   模型行为的一部分，改动必须显式过评审；
2. **目录同步守护**：service_map 覆盖的域集合 == spec 非 hidden 域集合；
3. **硬闸测试**：`args="agent run xx"` / `args="login"` 返回拒绝文本且未起
   子进程；服务端 agent 令牌调 start → 400；
4. **令牌隔离测试**：bash 子进程 `echo $MOVIECLAW_TOKEN` 为空，mclaw 工具
   子进程能成功调用（e2e：真实 uvicorn + 真实 mclaw）；
5. **退出码标注测试**：构造 5/7 退出码场景，断言工具结果含对应中文解读；
6. **golden 任务（人工）**：「我的订阅有哪些」（只读）、「订阅沙丘2」
   （消歧链路）、「整理 1 号库」（危险确认链路：验证模型先报影响面再执行）。

## 6. 待你拍板的评审点汇总

① 参数面：单一 `args` 字符串（shlex 解析、无 shell）+ `timeout`，是否够用；
② description 全文（尤其一级目录的取舍粒度、危险规约措辞、「不要用 bash
直接调 API」的排他性表述）；
③ bash 撤销令牌注入——bash 里将无法调 mclaw（没有授权），这是特性而非
缺陷（一切产品操作走专用工具），确认接受；
④ 退出码语义标注放工具结果（运行时教学）而非 description，确认此取舍。
