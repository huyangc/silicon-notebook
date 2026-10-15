# MCP 回答「摘要 + 限时链接」交付方式设计

状态：草案，待拍板（§8）
日期：2026-10-15

## 1. 背景与问题

生产上 MCP `ask` 被接进即时聊天工具当助手用。`ask` 返回的 `answer` 是 Markdown
（表格、标题、`[kN]` 引用标记、公式、图片），聊天软件不渲染，读起来是一团符号。

目标：调用方传一个参数，`ask` 就只返回一段**纯文本摘要**，外加一个**点开即可在 Web
系统里看完整渲染结果**的链接；链接有失效时间，不让公开记录无限累积。

## 2. 现状（设计的出发点）

- **会话分享已有一整套公开页**：`/c/{token}`，匿名接口
  `GET /api/public/conversations/{token}` 与图片 `.../assets/{alias}`；笔记本会话
  token 前缀 `cshr-`、全局会话 `gshr-`，同一个投影模块
  `services/conversation_public_view.py`、同一套上限与渲染（引用/表格/代码/公式/图片）。
  每次打开都按分享者身份复核读权（笔记本挂载 / 全局会话涉及的库集合），失权即 404。
- **分享 token 存在会话行上**（`conversations.share_token` 一列 + 水位线），不是独立表。
  所以「复用分享会让记录越来越臃肿」的真实含义不是行数，而是：**每个 MCP 会话都变成永久
  公开链接**，暴露面只增不减，且无法按条到期。
- **会话分享的粒度是整条会话 + 水位线**。机器人一个会话里可能连续回答很多人，按会话分享
  会把同会话里别人的问题一并公开。
- **分享前要问作者确认个人记忆披露数**（`share_disclosure_required` 409 握手）。MCP 调用
  没有作者在场，无法逐次确认。
- MCP `ask` 现有参数 `output="answer"|"evidence"`；回答已经落库为作业
  （单库 `askjob-…`，全局 `gask-…`），`submitted_via="mcp"`。
- 后端没有「Web 前端对外地址」的通用配置；`AUTH_FRONTEND_BASE_URL` 只为 SSO 回调服务，
  文档明确不得从请求头推导 origin。

## 3. 方案总览

新增一种**按单条回答、带过期时间的公开链接**（下称「回答链接」），复用会话分享的公开页、
投影与复核逻辑，只换掉三样东西：**粒度**（一个作业而非整条会话）、**生命周期**（到期失效）、
**披露确认**（由 Agent token 的事先授权代替逐次握手）。

```
IM 用户 ──提问──> IM 机器人 ──MCP ask(delivery="link")──> 后端
                                                  │ 正常跑完问答（作业照常落库）
                                                  │ 生成纯文本摘要（确定性，不调模型）
                                                  │ 签发 alnk- token，写 answer_links 一行
IM 用户 <──摘要 + URL + 过期时间── IM 机器人 <─────┘
   │ 点击
   └──> /c/alnk-… ──> GET /api/public/conversations/alnk-…
                       查 answer_links：未过期 → 校验 Agent token 仍有效 → 复核读权
                       → 用会话分享同一投影渲染这一条问答
```

## 4. 接口设计

### 4.1 MCP `ask` 新参数

`delivery: "inline" | "link"`，默认 `"inline"`（现行行为，一字不变）。用字符串枚举不用布尔
（与仓库工具参数惯例一致，也给将来留第三种值）。

`delivery="link"` 时：

| 状态 | 返回 |
| --- | --- |
| `answered` | `job_id`、`conversation_id`、`status`，以及 `preview`（纯文本摘要）、`preview_truncated`、`link.url`、`link.expires_at`（ISO8601 带时区）。**不返回** `answer` 全文、`citations`、`coverage`、`trace` 分页体——它们在链接后面；需要时仍可 `get_ask(job_id)` 读。 |
| `needs_clarification` | 与现在相同（澄清问题本来就是短文本），不签链接。 |
| `failed` / `cancelled` | 与现在相同，不签链接（没有可看的回答）。 |

组合规则（在任何仓库工作前校验，与 `_validate_output` 同位置）：

- `output="evidence"` + `delivery="link"` → `invalid_argument`（证据模式不产生回答）。
- `client_request_id` 重试命中同一作业：**复用**该作业已签的未过期链接，不重复签发；已过期则
  重新签一条（同一作业至多一条有效链接，见 §5 唯一约束）。
- 单库、全局（2–8 库、`"*"`）、插件引擎模式都支持——链接指向作业，与路由方式无关。

`get_ask` 增加同名可选参数 `delivery`，语义相同：对已完成作业补签/取回链接（机器人先拿全文、
事后才决定发链接，或连接中断后取结果，都用得上）。

### 4.2 纯文本摘要 `preview`

确定性生成，不额外调模型（不增加延迟、不引入新的失败面）：

1. 去掉 `[kN]` 引用标记、图片语法、HTML 标签。
2. Markdown 降级为纯文本：标题去 `#`；粗斜体/行内代码去标记保留文字；列表项改成 `• `；
   链接保留文字；代码块保留内容；**表格与公式块整块跳过**，在该处放一行「（表格，见链接）」/
   「（公式，见链接）」。
3. 从头截取，上限 `MCP_ANSWER_PREVIEW_MAX_CHARS`（默认 300，按 Unicode 码点计），优先在段落
   或句末（。！？；.!?）处截断；截断时末尾加「…」并置 `preview_truncated=true`。

实现放在后端一个独立的纯函数模块，单测覆盖每种 Markdown 结构；不复用前端渲染器。

### 4.3 公开页

- 复用 `/c/{token}` 页面与 `GET /api/public/conversations/{token}`、`.../assets/{alias}`。
  新 token 前缀 `alnk`，在 `capability_tokens.py` 与 `cshr`/`gshr` 并列，按前缀分派第三个分支。
- 投影：调用会话分享的同一投影，快照**只含这一个作业对应的一轮**（问题 + 回答 + 引用 + 图片），
  白名单、上限、图片 HMAC 别名、`Cache-Control: no-store` 全部继承。
- 页面头部多一行「此链接将于 YYYY-MM-DD HH:mm 失效」。
- 过期后：返回 **410** + 「链接已过期，请重新提问」，前端显示同句。只在行仍存在（见 §5 宽限期）
  时可区分 410；行被清扫后与未知 token 一样 404。撤销/失权仍是 404，不暴露原因。

### 4.4 每次打开的复核（任一不满足 → 404）

1. `expires_at > now()`（否则 410）。
2. 签发它的 **Agent token 仍存在、未撤销、未过期，且仍有 `ask` 权限**——吊销机器人 token 即
   让它签过的所有链接立刻失效，这是运营上唯一需要的「批量撤销」手段。
3. 作业仍存在且为完成态（作业/会话被删 → 链接死）。
4. 按 token 所属用户身份复核读权：单库作业复核该库及其引用到的挂载库；全局作业复核
   `resolved_notebook_ids ∪ cited_notebook_ids ∪ 各锚点/引用的 notebook_id`——与会话分享
   现有两个分支的谓词**同一份实现**，不另写。
5. token 的笔记本白名单（allowlist）仍覆盖上面这些库：token 被收窄后，旧链接不得越权。

## 5. 数据模型与失效

新表 `answer_links`（SQLite 新版本号 / PG 新迁移，加之前先看 `origin/master` 最新号）：

| 列 | 说明 |
| --- | --- |
| `token` text PK (COLLATE "C") | `alnk-<256 bit>`，`new_capability_token("alnk")` 生成 |
| `job_kind` | `notebook` / `global` |
| `job_id` | `askjob-…` / `gask-…` |
| `owner_id` | 回答所属用户 |
| `agent_token_id` | 签发它的 Agent token |
| `created_at`, `expires_at` | 时间戳 |

- 部分唯一索引：`(job_kind, job_id)` 上同时至多一条（签发时过期行先删后插，同一事务）。
- `expires_at` 上建索引供清扫。
- `owner_id`、`agent_token_id` 登记进 `user_references.py`（账号合并/删除路径必须处理新引用列）；
  删除 Agent token 时级联删其链接；删除作业/会话时级联删。

**有效期**：`MCP_ANSWER_LINK_TTL_HOURS`，默认 168（7 天），允许 1–720。调用方可传
`link_ttl_hours` 缩短，**不能**超过部署上限（超过即 `invalid_argument`，不静默夹紧）。

**清扫**：到期判断**只在读时做**（正确性不依赖清扫）；清扫只为回收空间——挂到现有后台维护
调度上，每小时删 `expires_at < now() - 宽限期` 的行，宽限期 `MCP_ANSWER_LINK_GRACE_HOURS`
默认 168，宽限期内打开返回 410「已过期」而不是 404。行数上界 ≈ 调用速率 × (TTL + 宽限期)，
每行几十字节，量级可忽略。

**关于「臃肿」要说清楚的一点**：链接表本身很小且自清；MCP 问答真正持续增长的是**作业与会话
记录**——这部分现在就在增长，与是否发链接无关。是否给 `submitted_via="mcp"` 的会话加保留期
是另一个决定，见 §8 第 3 条。

## 6. 授权与个人记忆披露

会话分享发布前要作者确认「将公开 N 条个人记忆摘录」；MCP 场景无人可确认。处理：

- Agent token 新增一个权限位 **「允许签发限时回答链接」**（在 `/agents` 的 token 权限编辑里，
  与现有五档权限并列，默认关）。打开时界面明确告知：链接对**任何拿到它的人**可见（无需登录），
  可能包含引用到的个人记忆摘录，有效期为 N 天。这一次性确认替代逐次披露握手。
- 未开此权限位的 token 调 `delivery="link"` → `permission_denied`，报错文案指向该开关。
- 个人记忆只有在 token 同时有 read 权限时才参与问答（现行规则不变），因此链接里出现记忆
  摘录的前提本来就是 token 持有者两次主动授权。

## 7. 改动清单

| 层 | 改动 |
| --- | --- |
| 迁移 | `answer_links` 表（SQLite + PG 同步）；Agent token 权限位列 |
| 仓库 | `answer_link_store`（签发/按 token 读/按作业取回/清扫），两种后端 |
| 服务 | 摘要纯函数模块；签发服务；公开读分派 `alnk` 分支，复用投影与复核谓词 |
| MCP | `ask`/`get_ask` 加 `delivery`、`link_ttl_hours`；工具描述同步 |
| 配置 | `PUBLIC_WEB_BASE_URL`（拼链接用的前端对外 origin；未配置时 `delivery="link"` 返回 `unavailable` 并说明缺配置，不从请求头推导）、TTL/宽限期/摘要长度三项 |
| 前端 | `/c/[token]` 识别过期 410、显示失效时间；`/agents` 权限位开关与告知文案 |
| 后台 | 维护调度挂清扫任务 |
| 文档 | `product-and-api`（+_zh）MCP 与公开分享两节；`deployment-and-configuration` 新配置；用户手册 Agent 接入一节 |
| 测试 | 摘要每种结构；过期 410/清扫后 404；token 吊销/收窄白名单/失权 → 404；同作业唯一链接与 `client_request_id` 复用；evidence 组合拒绝；全局作业跨库复核；图片别名随过期失效；未开权限位拒绝 |

## 8. 待拍板

1. **链接要不要登录**。
   - A（推荐）：匿名能力链接，与现有 `/c/{token}` 一致。IM 群里的人多数不是机器人 token 的
     主人，匿名才能「点开就看」；风险靠 256 bit token + 短 TTL + token 吊销即失效控制。
   - B：要求登录（内网有 SSO），并按**查看者自己**的读权复核。更安全，但看不到所引库的人会
     打不开，且要 SSO 已启用。
2. **默认有效期**：7 天是否合适（IM 消息通常几天内被点开）；部署上限 30 天是否合适。
3. **MCP 会话记录要不要保留期**：若担心的「臃肿」其实是机器人产生的大量会话/作业，可以另加
   「`submitted_via="mcp"` 的会话 N 天后自动删除」。这是删除用户数据，需单独决定；不做则
   本方案只解决链接本身的生命周期。
4. **摘要长度**：默认 300 字是否合适，或希望由调用方传 `preview_max_chars`（在部署上限内）。
