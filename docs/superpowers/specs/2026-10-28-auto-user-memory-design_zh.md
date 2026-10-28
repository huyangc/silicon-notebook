# 自动用户记忆：设计规格（讨论稿）

日期：2026-10-28　状态：**用户已批准（2026-10-28）：D1–D21 全部按推荐，已立项**（§14 决策点清单）。已过三轮 opus 评审并按意见修订，第三轮结论为无阻塞（修订记录见 §16）。不含实现代码。

> 来源：`fangan_todo.md`「三、长期方向」第 4 条「自动用户记忆：`memory_mode` 固定 manual；Agentic Memory
> 已提供候选 / 巡固机制，是否开放自动写入待决」；用户 2026-10-19 裁决 16「长期方向四项都启动设计：先写规格、
> 评审后给用户看」。基线 `824615155`。PostgreSQL 为规范定义，SQLite 为镜像；下文行号对本基线。
> 上游设计：`docs/superpowers/specs/2026-07-13-agent-memory-system-design.md`（Memory 层 C）、
> `docs/superpowers/specs/2026-08-18-agentic-memory-design.md`（Agentic Memory A/B 层，P1–P4 已合入）、
> `docs/superpowers/specs/2026-09-22-retrieval-experience-per-notebook-design_zh.md`（经验库按笔记本分区）、
> `docs/incremental-sync-design.md`（跨环境增量同步）。
> 本文落地后，数值只登记在 `docs/product-and-api*.md`；本文给的是**建议默认值**。

---

## 0. 结论先行

1. **「自动」= 系统从用户自己的提问活动里自动「提议」记忆，落成待确认（`candidate`），由用户逐条采纳或忽略；
   不自动写入正式记忆。** 这是对现有人审门（Memory 层 C「进答案、可被 `[k]` 引用，才需要人审」）的直接复用：
   待确认记忆结构上进不了正式问答、报告与搜索（§1.3），一条错误的建议在用户采纳前**对检索零影响**。
2. **按人开关**：`user_profiles.memory_mode` 第一次获得语义——`manual`（默认，与今天逐字一致）/`suggest`（开启记忆建议）。
   默认关；用户在记忆页就地开启。
3. **只做「内容性知识」**：术语含义、领域事实、对回答的纠正。「怎么查」归笔记本理解覆盖层与检索经验库、「怎么答」
   归回答偏好——这三样已经有自动通道（§1.4），本特性**不碰也不复制**。
4. **v1 信号两类、确定性取样 + 每轮至多一次有界模型调用**：①问题理解时用户自己给出的澄清答复；②同一人在同一库里
   反复问相近的问题。「追问里纠正上一轮回答」是第三类，建议放 PR-3（§3.3）。热路径零模型调用、零新增等待。
5. **隐私边界**：取数 SQL 自带 `created_by` / 会话属主谓词，只读本人在本库的网页提问；产物是本人私有的 `memory_items` 行。
   **「本人的记忆」有十几个读者各有口径**（导出、退出披露、卡片与分析页计数、删除提醒、管理员总览、跨环境同步……），每个都对
   「未采纳的系统建议」显式表态（§6.3）。事件与日志只带 id、计数、类别与耗时。

---

## 1. 现状（PG 为规范；文件:行号或函数名）

### 1.1 用户记忆怎么产生——今天只有两条通道

| 通道 | 落库状态 | 入口 |
| --- | --- | --- |
| 用户在回答上「保存到 Memory」：先预览（`memory_preview` 工作负载生成标题/正文/标签，可编辑）再确认 | 直接 `confirmed`，`origin='ask_answer'`，`source_answer_id` 绑定原回答，provenance 带问题/回答/引用 | `MemoryService.create_from_answer`（`backend/app/services/memory_service.py:992`）；预览 `POST /answers/{id}/memory-preview`（`backend/app/api/memory_routes.py:549`） |
| 外部 Agent 经 MCP `propose_memory` | `candidate`，`origin='external_agent'`，按 `client_request_id` 幂等 | `MemoryService.create_candidate`（`memory_service.py:934`）；`backend/app/api/mcp_tools/memory_context.py:267` |

- **「每人每回答至多一条记忆」是唯一索引**：PG `0002_integrity_indexes.sql:23` `idx_memory_answer_once (created_by, source_answer_id)
  WHERE source_answer_id IS NOT NULL`（SQLite `sqlite/migrations.py:1603`）；写入是 `ON CONFLICT ... DO NOTHING`
  （`postgres/memory_store.py:600`），之前先查 `memory_by_answer`，命中任意状态都原样返回（`memory_service.py:1015–1019`）。
- 生命周期 `candidate | confirmed | rejected | deprecated`（PG `0001_initial.sql:420` CHECK）；`confirm`（`memory_service.py:1102`）
  只接受 `candidate → confirmed`，`reject`（:1129）只接受 `candidate`，`deprecate`（:1142）只接受 `confirmed`。
  路由层只有单条 confirm / reject / deprecate 与 **`POST /memories/bulk-delete`**（`memory_routes.py:462`）；**没有批量确认、也没有批量拒绝**。
- `origin` 封闭枚举 `('ask_answer','external_agent')`：PG `0001_initial.sql:419`、:432 两处 CHECK；SQLite `sqlite/migrations.py:1588`、:1636。
  按 `origin` 分支的读者：存储层只判 `external_agent` 的幂等（`postgres/memory_store.py:564`、:574）；前端 `ask_answer` 分支渲染
  「原问题 / 来源回答 / 提问方式 / 依据 / 引用」溯源行（`frontend/app/memory-model.ts:362`、:403）；导出的「类型」标签
  `_ORIGIN_LABELS`（`memory_service.py:84`，用于 :260）；记忆页的来源筛选与来源徽标（`frontend/app/memory-panel.tsx:712`、:923）。
- **`memory_mode` 是空壳**：PG `0001_initial.sql:680` 列默认 `'manual'`，无 CHECK；建档只写字面量 `'manual'`（`auth_store.py:385`、
  两后端 `identity_store.py`）；`UserProfile.memory_mode` 默认 `"manual"`（`models/identity.py:32`），`GET /me` 返回它，但没有代码读它做判断。
  `user_profiles` 主键是 `id`，按 `user_id` 查没有索引（PG `0001_initial.sql:676–686`）。
- `create_candidate` 的读权检查 `_require_notebook`（:954）**在写事务之外**；它也不排向量（`_schedule_embed` 只在 `confirm` 时，:1127）。
  需要在写事务内复核成员关系的写入，用 `access_sql` 的带锁探测：先 `FOR SHARE` 锁 notebooks 行、再 `MEMBER_PROBE_FOR_SHARE_SQL`（:120）
  锁成员行、`GRANT_PROBE_FOR_SHARE_SQL`（:317）锁授权边（`postgres/access_sql.py:39–50` 的说明；先例 `postgres/memory_store.py:1258`
  `_lock_memory_aggregate_on`）。

### 1.2 存在哪、谁能读

- 表 `memory_items`（PG `0001_initial.sql:400`）+ `memory_provenance`（:424）+ `memory_revisions`（:435）+ 向量表；归创建者私有，绑定且只绑定一个笔记本。
- 浏览器：笔记本内「记忆」页只显示本人在本库的记录，待确认有独立筛选与「审核」按钮（`memory-panel.tsx:704`、:929）；总记忆页只聚合本人。
- MCP：`search(include="memory")` 返回 token 主人在该库的 `confirmed` 与（持 `memory:read_candidates` 时）`candidate`，候选带
  `unconfirmed: true`（`memory_context.py:84`）；`include="formal"` 永不返回候选。
- 他人（含笔记本主人）对 Memory 与其隐藏合成源一律 404（产品文档第 1306 行起）。

### 1.3 如何进入检索与知识图谱

- **问答**：`AskService._memory_hits`（`ask_service.py:2873`）→ `MemoryRetriever.notebook_memory_hits`（`memory_retrieval.py:195`），
  **只取 `("confirmed",)`**；语义准入 0.80（:38），上下文每条 1,200 / 总 6,000 字符（:64–65），`k3000+` 独立编号。来源范围收窄或全局问答时
  整条通道关闭（`ask_service.py:2882`）；无 `read` 档的 MCP token 运行期关闭（`source_scope.py:2176`，ContextVar）。
- **权威序**：`candidate(1) < personal 原始证据(2) < confirmed Memory(3) < base(4)`（`memory_retrieval.py:32–37`），只在相关性相同时生效——
  **一条错误的已确认记忆在平分时能压过用户自己的原始资料**（§7）。
- **深度报告**：`report_engine.py:1552`、:2549 同样只读 confirmed。
- **知识图谱**：确认时可勾「同时抽取到知识图谱」（默认勾选）；0067/v87 起记忆派生对象结构性隔离于一切共享产物（`0067_memory_kg_isolation.sql` 文件头；
  产品文档「知识图谱里的记忆」第 2124 行起）。

### 1.4 已经存在的「自动」——本特性的边界

| 已有自动通道 | 记什么 | 信任级别 | 位置 |
| --- | --- | --- | --- |
| 笔记本理解底座 | 库的形状 | 不进答案、不可引用 | `agent_profile_job.py` 底座链路 |
| 理解覆盖层「我的检索心得」 | 本人在本库怎么查、查不到什么 | 同上 | `AgentProfileConsolidationService.note_ask_completed`（`agent_profile_job.py:1859`） |
| 检索策略经验库（按笔记本分区） | 这个库里哪种检索动作好用（封闭词表） | 同上 | `retrieval_experience_job.py` |
| 回答偏好 `answer_language` 归纳 | 用户的回答语言 | 推断值须用户确认才进 prompt | `search_profile_job.py`；`search_profile.py:153`/`:186` |

三者挂在同一个提问完成钩子上：`RepositoryRuntime._note_ask_completed`（`repository_runtime.py:1245`）构造
`AskCompletedObserverCallContext`（`domain/extensions.py:271`），交给 `AskCompletedObserverHost.observe_application`
（`app/extensions/ask.py:116`）。宿主**串行**执行各观察者、共用一个截止时刻（`ask_post_completion_extension_timeout_seconds`，
`core/config.py:268`），预算耗尽即 `break`；每个观察者经一项公开能力常量拿到核心端口（`domain/extensions.py:231–237`，经
`app/extension_sdk/ask.py:27` 与 `extension_sdk/__init__.py:80` 对仓库外插件公开），宿主只认 `_OBSERVER_ACCESS_CAPABILITIES`
里的三项（`extensions/ask.py:41`）；内置实现在 `app/extensions/builtin/ask_completion.py`。覆盖层链在计数之前先过 `_member_can_read`
（`agent_profile_job.py:1812`，codex #520 R5 的残余在 P2-T3 关闭）。成员被移出（`remove_member`）或取消链接分享（存储原语 `kick_all_members`，
`postgres/sharing_store.py:1193`）时，覆盖层作业行与理解块按 owner 集合清空，**两条路径都不删个人记忆**（产品文档第 3196 行）。

**空白恰好在层 C**：上述三条只学「怎么查/怎么答」，没有通道会自动沉淀「内容」。

### 1.5 「本人的记忆」今天的读者与口径

| 读者 | 位置 | 今天的状态口径 |
| --- | --- | --- |
| 记忆页列表 / 待确认数 | `list_memories`；`memory-panel.tsx` | 全状态，可筛选 |
| 分析页「记忆」卡片（计数 + 最近 3 条标题） | `notebook_content_overview`（`postgres/memory_store.py:525–547`） | 计数全状态；标题取 confirmed + candidate |
| 记忆导出（Markdown） | `export_markdown`（`memory_service.py:1406`） | 全状态，候选带标记 |
| 自行退出笔记本：披露 | `exit_disclosure`（:1273）→ `member_exit_snapshot`（`postgres/memory_store.py:1976`） | 全状态（退出**删除**全部状态） |
| 自行退出笔记本：收尾核对 | `finish_member_exit` 的 remaining（`postgres/memory_store.py:2074–2110`） | 全状态（核对「一条不剩」才结束成员关系） |
| 退出群组预览 | `group_exit_preview`（:1281）→ `group_exit_memory_preview`（`postgres/memory_store.py:2020`）；`group-leave-dialog.tsx:54`、:198、:213 | 全状态；**退出群组不删除记忆，只是看不到**（「Leaving deletes nothing」） |
| 笔记本卡片计数 / 本地证据预过滤 | `memory_counts_by_owner_notebook`（`postgres/query_store.py:861`）；`notebook_catalog.py:680` | 全状态（预过滤用它作 confirmed 的超集） |
| 删除窗口提醒（「在那里有记忆的读者」） | `postgres/query_store.py:2167`–:2181 | 全状态 |
| 删库通知的受众 | `delete_notice_audience`（`postgres/notebook_delete_job_store.py:285–294`） | 任一状态有行即入选 |
| 删库分批删除 | `postgres/notebook_delete_job_store.py:980` 一带 | 全状态 |
| 管理员用户总览 `memory_count` | `postgres/query_store.py:1069` | 排除 `rejected`，计入 candidate |
| 跨环境增量同步 | `app/migration/sync/manifest.py:518`（`memory_items` 为 `SYNCED_WITH_MAPPING`）；`docs/incremental-sync-design.md:95–116` | 全状态，按笔记本 scope 随行；增量只走变更日志（`export.py:1400` `_incremental_rows`） |
| 深拷贝 / 笔记本导出 | 产品文档第 1160 行；`notebook_transfer/export.py:6`（ruling M2） | 一律不带 |
| 会话 / 报告公开分享披露 | 产品文档第 3629 行起 | 只数被引用的（候选从不被引用） |

反馈：`POST /answers/{id}/feedback` 先过 `user_owns_answer`（`ask_routes.py:1692`；判据 `notebook_sharing.py:539`），所以 `feedback` 表
虽无用户列（`0001_initial.sql:224`）每行都属于会话属主；同一回答可有多行（`postgres/ask_state_store.py:1947` 只 INSERT）。

---

## 2. 目标与非目标

**目标**
1. 开启了「记忆建议」的用户，不必记得手动保存，也能把提问中暴露出来的**稳定内容性知识**沉淀成本库私有记忆。
2. 建议质量可观测：按信号类看采纳率、忽略率、过期率、采纳后被弃用的比例；不达标的信号类能单独关掉。
3. 对没开启的用户、对检索与回答质量、对热路径延迟：**逐字零变化**（含界面）。

**非目标**
- 不自动写正式记忆；不由机器修改或删除用户已有的任何记忆（同 `agent_profile_job.user_authoritative` `:486`、`search_profile` 的「job 不覆盖 user」）。
- 不学「怎么查 / 怎么答」；回答风格偏好若出现在信号里一律丢弃。
- 不跨用户、不跨笔记本；不读全局问答、MCP 发起的提问、深度报告正文、其他成员的任何数据。
- 不做正则路由识别「请记住…」（用户裁决「agentic 能力做成工具不做正则路由」）。

---

## 3. 「自动」的精确定义：信号

### 3.1 信号一：澄清答复（`clarified`）

- **是什么**：问题理解阶段模型提出歧义问题、用户亲自作答。答案在 `QueryIntentContract.clarification_answers`（`models/ask.py:299`），
  随 `AskResponse.intent`（`ask.py:1152`）持久化在 `answers.payload`；检索已把它当「用户确认：…」补充项使用（`query_intent.py:987`）。
  每条答复对应一行歧义（`QueryIntentAmbiguity`：`id`、`question`、`options`，`ask.py:245`）。
- **噪声**：大量澄清是一次性参数（「2023 还是 2024？」→「2024」）。是否可持久由每轮那一次模型调用判断（§4.2）；判否进入负缓存（§5.1），不落库。
- **不绑回答**：澄清类建议的 `source_answer_id` 为空，回答 id 只写进 provenance——否则它会占住该回答的「每人每回答一条」唯一索引，
  用户之后在同一回答上手动保存时会拿回这条澄清建议、甚至用回答正文覆盖澄清内容（§5.3）。
- **只取网页提问**（`submitted_via='web'`，取值见 `models/ask.py:455–464`）：MCP 提问的澄清是外部 Agent 答的。

### 3.2 信号二：反复提问（`repeated`）

- **是什么**：同一人在同一库、采样窗口内（§4.2 第 3 步）、30 天内问了 ≥3 次相近的问题，且这些回答都还没有本人的任何记忆。
- **聚类（确定性、零模型调用）**：
  - 每个问题取检索层关键词基（`retrieval._tokens`，`retrieval.py:241`；`keyword_basis`，:359）去停用词后的集合；**集合少于 2 个词的问题
    （「继续」「详细说说」这类）不参与聚类、不算指纹**。
  - **聚类窗口**：下界 = max(job 行起点, 当下 − 30 天)，上界 = 当下，窗口内按时间取最近的至多 40 条（同 §4.2 第 3 步的谓词）。
    窗口与游标是两回事：游标之前的问题仍可作为簇成员，但**只有至少包含一条游标之后的新问题的簇才算信号**——否则同一个旧簇每轮都会被重新评估。
  - 按提问时间顺序做「领头者聚类」：一个问题与某簇**领头问题**（簇内最早一条）的 Jaccard ≥ 0.6 才入该簇，不做单链传递，避免把不同话题串成一个大簇；
    两个集合都非空才计算 Jaccard（空集已在上一条排除）。**平局规则**：一个问题同时与多个领头问题达到阈值时，入 Jaccard 最高的那一簇；
    Jaccard 也相同时入领头问题最早的那一簇。一个问题只属于一个簇；与所有领头都不达阈值时自成新簇的领头。结果因此只取决于窗口内问题的时间顺序，可复现。
  - 每次取样重新聚类，不保存簇。
- **建议内容**：簇内最近一次有据回答；簇内若有回答被本人评为「有用」，优先它（同一回答多条反馈只看属主最新一条，只作并列时的选优依据）。
  草稿形状同「保存到 Memory」预览；`source_answer_id` 绑定被选中的回答（这样用户手动保存时能接上，§5.3）。

### 3.3 信号三（PR-3）：追问里的纠正（`corrected`）

同一会话第 i+1 轮的问题在纠正第 i 轮回答。判定需要模型，且是**污染风险最高**的一类：用户的纠正可能是错的，采纳后权威（3）高于本人原始资料（2）。
PR-3 要求每条建议附「与本轮引用资料是否矛盾」标记并显示在卡片上（§7）。输入成对（上一轮回答节选 + 本轮问题），只取本人会话。

### 3.4 明确不用的信号

| 信号 | 理由 |
| --- | --- |
| 单独一次「有用」评价 | 每个赞都提议会刷屏；只作信号二的选优依据。就地提示单列 §14 D19 |
| 「没用」评价与评论 | 评论是自由文本，往往是抱怨而非知识 |
| 深度报告与「就此追问」 | 就此追问（`ask_jobs.report_id` 非空，PG `0080`）的回答会转述报告正文，报告可能是别人写的——v1 排除，§14 D20 |
| 全局问答 | 记忆绑定且只绑定一个库；全局问答对等运行、本就关闭记忆通道（`ask_service.py:2882`） |
| 仅检索不作答的提问 | `ask_jobs.output='evidence'`（PG `0070`）没有回答 |
| 编辑过的「问题理解」措辞 | 改的是查询，属于「检索心得」原料 |
| 问题里「请记住…」 | 需要正则路由或改理解 prompt（热路径） |

---

## 4. 管线

### 4.1 触发：提问完成观察者第四个端口

**SDK 契约改动（仓库外插件可见，全部是追加、带默认值）**
- `domain/extensions.py`：新能力常量 `ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY = "ask:memory_suggest_completed_access"`；
  新端口协议 `MemorySuggestAskCompletedPort`；`AskCompletedObserverCallContext` 末尾追加 `memory_suggest`（默认 `None`）；
  `CompletedAskNotification` 末尾追加 `submitted_via: str = ""`、`had_clarification: bool = False`、`ask_job_id: str = ""`（不透明 id，用于初始化游标，§4.2）。
- `extension_sdk/ask.py`、`extension_sdk/__init__.py` 导出新常量。
- `extensions/ask.py:41` `_OBSERVER_ACCESS_CAPABILITIES` 加入新常量。
- `extensions/builtin/ask_completion.py` 加一个内置观察者 bundle，**在内置观察者中注册在最后**：宿主串行、共用截止时刻，预算紧时最后一个最先被 `break`——
  记忆建议是四条里最不要紧的。注意「最后」只对内置观察者成立：部署装的仓库外插件观察者按注册顺序可能排在它后面，它们的预算不受本特性保护，也不会因本特性变差（本观察者自身只做一次主键读与一次 upsert）。
- 插件 SDK 兼容说明、`docs/deployment-extensions-sop*.md` 的能力清单同步。

**端口内的判定（答案交付之后，fail-open）**
1. `scope != "notebook"` 或 `submitted_via != "web"` → 返回。MCP 与全局提问**根本不计数**，不存在 MCP 引起的空跑。
2. 主键读 `memory_suggest_users(owner_id)`；行不存在或 `enabled = false` → 返回。**默认用户到此为止，零写入**；不读 `user_profiles`、不需要为它加索引。
3. `_member_can_read` 同款复核（复用 `SharingStorePort.user_can_read_notebook`，同样 fail-open）——防止退出后才到达的完成通知重建 job 行。
4. upsert `memory_suggest_jobs(notebook_id, owner_id)`：**行不存在时**以本次的 `ask_job_id` 对应提问作游标起点（含它本身，§4.2 第 3 步）；`pending_signal += 1`；
   达阈值（建议 5 次）或 `had_clarification`（§14 D3(x)）才交给轻活池（队列 put，不等待）。

### 4.2 后台 job（每 (笔记本, 用户) 单飞）

照抄 `agent_profile_jobs` 协议（PG `0029_agent_profile.sql:94`：单行状态 + `claim_token` 代际 + 每条退出路径落终态 + 启动兜底收 queued）：

1. **认领时复核**：`memory_suggest_users.enabled`、笔记本存活（`NOTEBOOK_LIVE_SQL`，不在删除窗口）、本人仍可读；任一不满足 → 终态 `skipped:<原因码>`。
   退出群组、被移出后读不到的库都在这里落 `skipped`；已有的草稿留在原处（看不到），照常按 30 天过期。
2. **零调用闸**：本人在本库「未采纳的系统建议」待确认数 ≥ 积压上限（建议 5）；部署开关关闭。
3. **取样**（§6.1 的谓词）：`ask_jobs` 中 `created_by=本人 ∧ notebook_id=本库 ∧ status='done' ∧ submitted_via='web' ∧ output='answer' ∧ report_id=''
   ∧ (created_at, id) ≥ 游标`；回答只投影 `payload->'intent'->'clarification_answers'`、歧义行的 `id/options`，以及选优需要的 `evidence_level`。
   **游标与采样窗口的语义**：job 行的生命周期就是采样窗口。行在一次通知里被创建时，游标起点 = 触发这次创建的那条提问（含）；
   关闭开关、自行退出、被移出、取消链接分享都会删除 job 行（§5.4），所以重新开启或重新加入之后，第一次网页提问重新建行，起点自然落在那之后——
   **不回溯开启前、关闭期间、或上次成员身份期间的提问**（§14 D14）。`ask_job_id` 为空（极少数没有 job 行的入口）时起点取建行时刻。
   澄清类信号只取游标之后的提问；反复类的聚类窗口是 [max(job 行起点, 当下 − 30 天), 当下] 内最近的至多 40 条，且簇里必须有游标之后的新问题（§3.2）。
   读计划：可用索引有 `idx_ask_jobs_creator_activity`（`created_by, created_at DESC, id DESC`，PG `0040`）与 `idx_ask_jobs_nb_status`
   （`notebook_id, status`，PG `0003`）；大库上规划器可能选后者扫全库已完成提问。实现时须在 PG 上实测并钉 EXPLAIN，必要时加
   `ask_jobs(created_by, notebook_id, created_at DESC, id DESC)`。
4. **确定性预筛**：去掉本人已有任何记忆的回答；去掉命中指纹表的信号（§5.1，含负缓存）；每轮至多 8 个信号项。没有信号项 → 终态 `no_signal`，
   推进游标，不调用模型、**不占每日额度**。
5. **每日额度预占**：`memory_suggest_users` 行上一条条件 UPDATE
   `SET quota_used = CASE WHEN quota_day = 今天 THEN quota_used + 1 ELSE 1 END, quota_day = 今天 WHERE owner_id = ? AND (quota_day <> 今天 OR quota_used < 上限)`，
   影响 0 行即终态 `daily_cap`。单条语句的行锁保证跨笔记本的并发 job 不超额（两后端同语义，不需要 advisory lock）。「今天」按 **UTC 日期**算
   （与库内时间戳同一时区；不同部署时区下日界线不同是已知取舍，额度只是成本闸）。模型调用失败不退还额度。
6. **去重近邻**（取舍：**只在采纳时向量化**，与今天 `confirm` 的行为一致）：
   - 信号项文本一次批量 embed（1 次调用、≤8 条文本）；
   - 新增存储读取 `memory_suggest_neighbors(owner_id, notebook_id, query_vectors, statuses, k=3)`：一次读出本人在本库**已有向量**的记忆（已采纳或手动保存的），
     进程内算原始余弦取前 3；再并上本人在本库**全部状态**（`candidate/confirmed/rejected/deprecated`，由调用方显式传入）的 FTS 词法前 3。
     向量侧照搬 `memory_retrieval_rows` 的上限（`postgres/memory_store.py:2510`，`vector_limit` 钳到 500，:2534），按 `updated_at` 取最近的 500 条；
     现有 `memory_retrieval_rows` 只放行 `candidate/confirmed`（:2526–2529），不能直接复用，新读取自己做状态白名单。
   - 不复用 `MemoryRetriever._hits`（它返回融合分、每个查询各 embed 一次、受 `memory_channel_allowed()` ContextVar 控制）。
   - 不在创建候选时排向量：省掉每轮第二次 embedding，代价是待确认/已忽略的建议只能靠词法当近邻；它们与信号项往往同源同词，词法够用，
     语义近似再由模型兜底。
7. **一次有界模型调用**（工作负载见 §14 D4/D18）：输入 = 信号项清单（服务端编号、类别、问题、澄清问答或回答节选、近邻标题）；输出 = 封闭 JSON，每项
   `{item_id, keep, kind: term|fact|correction, title, content, duplicate_of: <近邻 id>|null}`。回答节选与近邻包在「不可信资料」指令里（knowhow
   `llm_complete` 先例）；输出只能引用服务端发出的 id（同 `agent_profile_job.parse_base_reply` 的 `served_ids` 校验，`:545`）。畸形输出整批丢弃，终态
   `model_rejected`（不写负缓存，下轮可重试）。`keep=false` 或 `duplicate_of` 非空的项写**负缓存**（§5.1），同一件事不会每轮都被送去模型。
8. **写入**（`MemoryService.create_suggestion`，新入口）：每个 `keep ∧ duplicate_of=null` 的项，在**同一写事务**内：用 `access_sql` 带锁探测复核读权
   （notebooks 行与成员行/授权边 `FOR SHARE`，§1.1 末条）、复核 `memory_suggest_users.enabled`、以 `claim_token` CAS 确认 job 行仍属本次认领
   （关闭开关会删 job 行，CAS 失败即放弃整批）、登记指纹、插 `candidate`（`origin='ask_answer'`，`suggested_signal` 为信号类，§10）。单轮至多 3 条。
9. **结算**：按终态处理游标（下表）、`runs += 1`、记事件（§6.4）。

**游标去向（按终态）**。游标只越过「本轮已被完整处理」的提问：每轮至多送 8 个信号项，排在第 8 项之后、被截断的那些提问**不能被游标越过**，下轮继续。

| 终态 | 游标 | 说明 |
| --- | --- | --- |
| 成功（含部分项被判否/判重） | 前移到本轮已送出的最后一个信号项所在提问 | 判否/判重的项已进负缓存 |
| `no_signal` | 前移到本轮扫描到的最新提问 | 不占额度 |
| `skipped:*`、零调用闸（积压、部署关闭）、`daily_cap` | 不动 | 条件恢复后从原处继续 |
| `model_rejected`、`model_unavailable`、`failed:*` | 不动，`consecutive_failures += 1` | 下次达阈值时重试同一批 |
| 同一批连续失败满 3 次 | 前移越过这一批，`consecutive_failures` 归零，记 `gave_up` 事件 | 防止畸形输出每天耗掉额度、把这个库永远卡在同一批上 |

每次失败都消耗一次每日额度，所以最坏情况是同一批 3 次调用、跨 1–3 天后放弃；成功一轮即把 `consecutive_failures` 归零。

**每轮外部调用**：至多 1 次 chat + 1 次批量 embedding（≤8 条文本）。

### 4.3 成本上限（建议默认）

| 项 | 建议值 | 说明 |
| --- | --- | --- |
| 热路径模型调用 / 新增等待 | 0 / 0 | 端口在交付之后；默认用户多一次 `memory_suggest_users` 主键读（行不存在） |
| 触发 | 本人在本库每 5 次网页提问，或带澄清答复的提问（D3(x)） | |
| 每轮 | ≤1 chat + ≤1 次批量 embedding | |
| 每轮输入 | ≤8 项；每项问题 ≤500、澄清 ≤500、回答节选 ≤1,500 字符；合计 ≤8,000 字符 | 截断走同一个 `Settings` 字段 |
| 每轮输出 | ≤1,024 tokens；≤3 条新候选 | |
| 每人每日模型调用 | ≤3 次（UTC 日，跨库合计，条件 UPDATE 预占） | `no_signal` 与零调用闸不占额度 |
| 积压上限 | 本人在本库未采纳建议待确认 ≥5 条即停 | 零调用闸 |
| 负缓存有效期 | 30 天 | 模型判否 / 判重的信号 30 天内不再送模 |

### 4.4 失败与降级

- 新工作负载未绑定：严格模式下启动即拒（#779 规则，§12 升级说明）；放行档下 job 终态 `model_unavailable`，开关旁就地提示「记忆建议暂不可用：管理员尚未配置模型」。
- 任意异常：job 落 `failed:<异常类名>`，不重试本批。端口层异常不影响已交付答案（`ask_execution.py:464` 同理）。

---

## 5. 去重、合并、过期

### 5.1 指纹表：存在即不提

新表 `memory_suggest_fingerprints(owner_id, notebook_id, fingerprint, kind, expires_at NULL, created_at)`，主键 `(owner_id, notebook_id, fingerprint)`，
只存哈希、不存正文，**不指向任何记忆行**，因此不需要在任何记忆删除路径上挂钩子。

- `kind='proposed'`：提议时写入，`expires_at` 为空，永久有效——此后这条建议被采纳、忽略、过期还是删除（单条删除、`bulk_delete`、
  「未变才删」`postgres/memory_store.py:1851–1885`、手动保存替换），指纹都还在，**同一指纹不再提**。
- `kind='declined'`（负缓存）：模型判 `keep=false` 或判重时写入，`expires_at = 当下 + 30 天`；过期前不送模，过期后可再试一次（语料或认识可能变了）。
- 「存在」= `kind='proposed'` 或 `expires_at > 当下`。
- **写入优先级**（一条 `INSERT ... ON CONFLICT (owner_id, notebook_id, fingerprint)` 完成，两端同语义）：
  - 写 `proposed`：已有 `declined` 时**升级**为 `proposed`、`expires_at` 置空；已有 `proposed` 时什么都不做。
  - 写 `declined`：已有 `proposed` 时什么都不做（`proposed` **永不降级**）；已有 `declined` 时只把 `expires_at` 延长到新值（取两者较晚）。
  - 用例：declined → proposed 升级；proposed 上再写 declined 不变；declined 上再写 declined 只延期。
- **指纹的算法**：
  - 澄清类：`sha256("clarified" | notebook_id | 答复关键词基集合排序)`；若答复关键词基少于 2 个（「前者」「都要」「2024」），把该歧义行的
    `options` 关键词基并入集合再哈希——短答复离开选项就没有意义，单看答复会跨主题撞指纹。仍不足 2 个词的不登记 `proposed`（只可能被判否进负缓存）。
  - 反复类：簇内**每个成员问题**各算 `sha256("repeated" | notebook_id | 该问题关键词基集合排序)`，全部登记（每簇 ≤10 个）。新簇**过半成员**的指纹已存在才算命中；
    原样重问一次只贡献一票，不会把一个新话题簇整个压掉；窗口滑动后的同一话题簇通常过半成员仍在，或由近邻与模型兜底。
- **措辞**：本设计只保证「**同一指纹不重提**」；换了说法的同一件事由近邻 + 模型 `duplicate_of` 兜底，不作保证。
- **清理**：随笔记本删除级联；自行退出笔记本（记忆被删）时删除该 (用户, 库) 的全部指纹；被移出、退出群组时保留（记忆还在，只是看不到）。
- 指纹表是 LOCAL（不同步，§6.6）；另一环境可能再提一次同一件事，由那边的近邻与模型兜底，登记为已知行为。

### 5.2 合并：机器不改用户的记忆

- 新信息细化已有记忆时，照常落新候选，provenance 带 `refines: <旧 memory_id>`；采纳弹窗多一个默认**不勾**的「同时弃用《旧标题》」，勾了才走现有 `deprecate`。
- 不做 Mem0 式 UPDATE/DELETE（与理解块 `user_authoritative`、回答偏好、人审门同向）。

### 5.3 与「保存到 Memory」的衔接（只涉及绑了回答的反复类建议）

`create_from_answer` 在 `memory_by_answer` 命中时的分支改为：

| 命中的已有行 | 行为 |
| --- | --- |
| 系统建议、`candidate` | 同一写事务内 CAS `candidate → confirmed`，写入用户这次提交的标题/正文/标签；返回 confirmed |
| 系统建议、`rejected`（已忽略或过期） | 见 §14 D16：推荐**替换**——同一写事务内删除旧行、插入一条普通手动记忆（`suggested_signal` 为空）；指纹不动（`proposed` 永久） |
| 其他（手动保存过的、Agent 候选、已确认的建议） | 保持现行为：原样返回 |
| 并发：插入撞 `idx_memory_answer_once` 拿回已有行 | 视为 no-op，返回已有行，不报错 |

**采纳有两个入口，副作用必须完全相同**：记忆页的 `confirm`（`memory_service.py:1102`），以及上表第一行 `create_from_answer` 里的 CAS
`candidate → confirmed`。两者都走同一个内部收尾（实现时抽成一个 helper，两处只调它）：写一条修订、排向量（`_schedule_embed`）、按 KG 复选框调度抽取
（`_maybe_schedule_kg`）、`memory_lifecycle` 事件、以及 D15(a) 的同步补发（§6.6）。§11 对这两条路径各写一例同步用例与副作用用例。

`POST /notebooks/{id}/answer-memory-links`（`memory_routes.py:446`）今天返回 `{links: {answer_id: memory_id}}`（前端类型
`Record<string, string>`，`frontend/app/ask-api.ts:303–307`）。改为**追加字段**，不改原有字段的形状：
- `links` 保持 `Record<string, string>`，但**不再包含未采纳的系统建议**（套 §6.3 的片段）——旧前端因此不会把只有草稿的回答显示成「已保存」，
  「保存到 Memory」按钮照常可点，点了走上表第一、二行；
- 新增 `states: Record<answer_id, {memory_id, status, suggested}>`，PR-2 的前端据此显示「有建议」。
两个字段都在 PR-1 落地；旧前端忽略 `states`，行为正确。

### 5.4 过期与清理

| 对象 | 规则 |
| --- | --- |
| 未采纳的系统建议（`candidate`） | 30 天无人处理 → 系统迁移为 `rejected`，revision 原因 `expired`（部分索引支撑，§10） |
| 已忽略 / 过期的行 | 与任何被拒绝的候选一样留在记忆页「已忽略」筛选里，用户可删；**系统永不清理**，登记为已知行为。数量有界：每人每天至多 3 次模型调用 × 每轮至多 3 条 = 9 条建议（跨库合计），即使全部被忽略或过期，增长也不超过这个速率 |
| 已采纳的 | 与手动记忆完全相同，永不自动过期 |
| `memory_suggest_jobs` 行 | 随笔记本删除级联；关闭开关、自行退出（`leave_notebook`）、被移出（`remove_member`）、取消链接分享（`kick_all_members`）、**退出群组**（对 `group_exit_memory_preview` 算出的那些「退出后读不到」的库）都删除该人在该库的行——被移出与取消分享与覆盖层作业行走同一组按 owner 集合删除的原语（产品文档第 3196 行），退出群组在同一个退出事务里删。删除后重新建行时游标从新的触发提问开始（§4.2 第 3 步），所以「不回溯上次成员身份期间」对所有失去读权的方式都成立 |

**自行退出与 job 的竞态**。`leave_notebook`（`memory_service.py:1297`）先清空记忆、再由 `finish_member_exit` 在成员行锁下核对「一条不剩」（:1356 一带）；
若一个 job 恰好在这两步之间写入一条建议，核对会得到 remaining>0，退出卡成「未完成」。两道闸：
1. 退出的认领步（`member_exit_snapshot(..., claim=True)`）在同一事务里删除该人在该库的 job 行——在飞 job 的写事务 `claim_token` CAS 随之失败，不会再写；
2. 兜底：`create_suggestion` 的写事务以 `FOR SHARE` 锁成员行（§4.2 第 8 步），`finish_member_exit` 持成员行锁，两者串行；`finish_member_exit` 在自己的事务里、
   计 remaining 之前，先删除该人在该库的「未采纳的系统建议」（只可能是竞态窗口里新出现的草稿，退出本来就会删除它们），然后照旧数全部状态。
   这样竞态永远不会让退出卡住，`finish_member_exit` 的「全状态核对」语义也不变。
| 指纹 | §5.1 |

过期清扫挂在现有启动/周期清扫上，每轮有上限（建议 500 行），不调用模型。

---

## 6. 隐私与可撤销

### 6.1 取数谓词写在 SQL 里

- `ask_jobs`：§4.2 第 3 步的谓词全集。
- 回答：`answers` join `conversations` 且 `conversations.created_by = 本人`（与 `user_owns_answer` 同判据）；只投影所需 JSON 路径。
- 反馈：只经上一条已归属的回答 id 读取，取属主最新一条。
- 近邻：`memory_items.created_by = 本人 AND notebook_id = 本库`。
- 新守卫 `backend/tests/test_memory_suggest_privacy_guard.py`（仿 `test_agent_profile_isolation_guard.py`）：模块内每个读端口方法登记归属谓词；读取 SQL 文本
  必须含对应谓词；不得调用不带 owner 参数的 ask/answer/memory 读取；不得 import 全局问答、报告、他人记忆读取路径。

### 6.2 可见、可删、可关

- **可见**：每条建议在记忆页带「系统建议」标记与理由，可点开来源回答。
- **可删**：逐条「忽略」（= `reject`）或删除；批量删除沿用 `bulk_delete`。今天没有批量确认/批量拒绝，本特性**不新增批量操作**（§14 D8）。
- **可关**：`PATCH /me/memory-mode` 改回 `manual` 在同一事务里写 `user_profiles.memory_mode`、置 `memory_suggest_users.enabled=false`、删除该用户全部
  `memory_suggest_jobs` 行；在飞 job 的写事务复核 `enabled` 并以 `claim_token` CAS，因此关闭之后**一条也不会再写进来**。关闭弹窗提供「同时删除 N 条未处理的建议」
  （默认不勾，§14 D12）。`user_profiles.memory_mode` 只供 `GET /me` 展示；端口与 job 只读 `memory_suggest_users`。两者只有这一个写入口、同一事务，守卫测试钉住「没有第二个写者」。
- **部署级关闭**：`AUTO_MEMORY_SUGGEST_ENABLED`（建议默认 true，只决定「能不能开」）；关闭时开关隐藏、端口与 job 全停，已有建议留在记忆页由用户处理。

### 6.3 各读者对「未采纳的系统建议」的口径

定义：**未采纳的系统建议** = `suggested_signal IS NOT NULL AND status IN ('candidate','rejected')`；**已采纳** = 它的取反里带 `suggested_signal` 的行，
即 `status IN ('confirmed','deprecated')`（采纳后被弃用的仍是用户的记忆）。新增共享 SQL 片段（`memory_sql`，两后端各一份）供下列读者统一引用；
守卫测试**逐个枚举**引用它的读者与**豁免它的读者**。

**统一理由（§14 D17）**：未采纳的建议是系统草稿，不可导出、不可转移，提醒用户也无事可做——所以它不进任何「你有几条记忆」的计数与提醒；
**唯一例外是用户自己发起、且会删除它们的动作**（自行退出笔记本），在那里如实多说一句。

| 读者 | 决定 |
| --- | --- |
| 记忆页列表、待确认数 | 计入（有「系统建议」子筛选）——这就是审核队列 |
| 分析页记忆卡片 `notebook_content_overview` | 套片段：计数与「最近 3 条」都排除未采纳建议 |
| 记忆导出 `export_markdown` | 套片段排除；导出头部注明「另有 N 条未处理的系统建议未导出」 |
| 自行退出：披露 `exit_disclosure` / `member_exit_snapshot` | 计数套片段；N>0 时披露文案加一句「以及 N 条未处理的系统建议」（退出会删除它们） |
| 自行退出：收尾核对 `finish_member_exit` remaining | **豁免**：必须数全部状态，否则草稿会让「一条不剩」核对误判；守卫里显式列为豁免 |
| 退出群组预览 `group_exit_memory_preview`；`group-leave-dialog.tsx` | 套片段：每库条数与总述都只数「你的记忆」。退出群组**不删除**任何记忆，草稿只是看不到、照常 30 天过期，不另加说明 |
| 卡片计数 `memory_counts_by_owner_notebook` | 套片段 |
| 本地证据预过滤 `notebook_catalog.py:680` | 不受影响：被排除的都不是 confirmed，计数仍是 confirmed 的超集 |
| 删除窗口提醒（`query_store.py:2167`–:2181） | 套片段：只有草稿的读者不收「计入未读」的删除提醒（草稿不可导出，提醒无事可做） |
| 删库通知受众 `delete_notice_audience` | 登记为「超集，不受影响」：多通知一个只有草稿的人无害；收紧它要改删除作业，不值 |
| 删库分批删除（`notebook_delete_job_store.py:980` 一带） | 不变：全部删除 |
| 管理员用户总览 `memory_count`（`query_store.py:1069`） | 排除未采纳的建议（与该处注释「被拒绝的候选不代表用户采纳了 Memory 机制」同理）；它本就排除 `rejected`，所以只需再减未采纳的 `candidate` |
| 跨环境增量同步 | §6.6 / §14 D15 |
| 深拷贝 / 笔记本导出 / 分享披露 | 不变 |
| MCP `search(include="memory")` | §14 D9 |

**执行计划**：计数类读者今天走 `idx_memory_owner_notebook_status`（`created_by, notebook_id, status, updated_at`，PG `0005`）可仅索引扫描；
直接加 `suggested_signal` 谓词会回表。实现方式改为「**原计数 − 未采纳建议计数**」，后者走新部分索引（§10）：
- **在同一条 SQL 里算完**：两个标量子查询相减，或两组按同一键分组的聚合 `LEFT JOIN` 后相减，外面兜一层 `GREATEST(0, …)`——不分两次读，
  否则两次读之间并发采纳/忽略会让差值短暂为负或漏减（同产品文档对理解块「先读清单再相减」的教训）。原计数那一半的子计划与今天逐字相同。
- **管理员总览**（`query_store.py:1069`）原本就排除 `rejected`，所以只减「未采纳且 `status='candidate'`」的那部分。
- **删除窗口提醒**（`query_store.py:2167`）今天用 `EXISTS (SELECT 1 FROM memory_items ...)` 判「在那里有记忆」；改为同一语句里比较两个计数
  （全部行数 > 未采纳建议行数），两个计数都限定在该 (用户, 笔记本) 上、都走索引；它的计划钉（`test_notebook_delete_grace_plans_pg.py`）按新形状重写。
- 需要逐行过滤的读者（分析页最近 3 条、导出）行数本就有界，直接加谓词。
- 以下计划钉须重新核对：`tests/postgres/test_memory_sql_explain_pins.py`、`tests/postgres/test_notebook_delete_grace_plans_pg.py`、
  `tests/postgres/test_memory_live_predicate_explain_pins.py`（§11）。

### 6.4 日志与事件不带内容

- 新事件 `memory_suggest_run`：`{notebook_id, owner_id, outcome, signals_by_kind, deduped, declined_cached, candidates_written, model_status, latency_ms}`；异常只记类名。
- 生命周期事件沿用 `memory_lifecycle`，新增 `action="suggestion_created"` / `"suggestion_expired"`，并带 `suggested_signal`。
- 守卫：事件键白名单测试，禁 `question/answer/title/content/statement` 等键。

### 6.5 MCP

- `search(include="memory")` 对持 `memory:read_candidates` 的 token 返回未采纳的系统建议时，行上加 `suggested_by_system: true`（§14 D9）。待确认建议没有向量，只经词法通道召回。
- Agent 不能采纳、忽略系统建议；不新增工具。

### 6.6 跨环境同步

**版本错配**：导入的 `_preflight`（`app/migration/sync/import_.py:2505–2522`，拒绝点 :2512–2521）要求包与目标端的 schema pair 相等，
错配的包在写任何行之前整包被拒，不存在「旧环境读到新取值、中途硬失败」。本设计也不在 `memory_items` 上加唯一约束（指纹在 LOCAL 表），
不存在两环境各自提议同一件事、同步时撞约束。`memory_items.suggested_signal` 是普通列：同步捕获只记行 id、导出读当前行，按
`docs/incremental-sync-design.md:263–266` 的先例不需要同步侧登记。

**D15 各选项的真实代价**（§14 D15 的依据）：

- **(a) 只同步已采纳的**。需要两件新机制：
  1. **按行过滤**：今天的 `TableSyncSpec`（`manifest.py:154`）只有表级与 scope 级圈定。新增对 `memory_items` 的行谓词「不是未采纳的建议」（§6.3 片段取反，
     含 `deprecated`），并让 PARENT scope 的 `memory_revisions` / `memory_provenance` / `memory_embeddings` 跟随父行是否入选——子表若不跟随，
     导入的 `_verify_row_scopes`（`import_.py:2138`）找不到父行会拒掉整个包。**过滤只作用于 upsert，deletes 不过滤**：目标端对从未收到过的行执行按主键
     重放 delete 时，`_replay_delete_table`（`import_.py:5943`）先查目标端：没有这一行的条目计为 `absent` 并跳过（docstring 约 :5950–5966，
     「a source that deleted a row this target never received」正是这种情形），实现时以用例钉住。
  2. **父行后来才入选时补发子行**：建议创建时 provenance、第 1 条修订已进入变更日志，那一轮导出时父行还是 candidate，子行被过滤掉；之后用户采纳，增量窗口里只有
     父行 UPDATE 和新修订——增量导出只走变更日志（`export.py:1400` `_incremental_rows`），没有任何机制会补发早先的 provenance 与修订。结果是目标端的已采纳记忆缺
     provenance（`_stamp_memory_sync_origin`，`import_.py:1608`，无处可写，「查看原回答」404）、修订历史缺第 1 条。补法：**采纳事务里对该记忆的 provenance 行与全部
     修订行各做一次无变更 UPDATE**——两端的捕获触发器在任何 UPDATE 上都记一条 upsert（PG `0064_sync_change_capture.sql` 的 memory 子表触发器没有
     `WHEN` 条件，`AFTER INSERT OR DELETE OR UPDATE`，`memory_embeddings`/`memory_provenance`/`memory_revisions` 三个触发器在 :689–783；SQLite 每表三条触发器同理），下一个增量包就会带上它们。向量在采纳时才生成（§4.2 第 6 步），
     天然在父行入选之后，不需要补。手动保存替换（§5.3）插的是新行，同样不需要。这一步只在 `suggested_signal` 非空的采纳路径上执行（`confirm` 与 `create_from_answer` 的 CAS
     两个入口共用同一个收尾，§5.3），有界（1 + 修订数条 UPDATE）；先读 `sync_capture_control`（单行），捕获关闭时跳过这些 UPDATE——
     那时触发器本来也不记日志（PG `0064` 各捕获函数开头的 `IF NOT EXISTS (... enabled)`），UPDATE 只是白写。
  
  代价：manifest 新增「行谓词 + 子表跟随」能力（全量与增量两条导出路径）、采纳路径的补发、对应的同步用例（采纳前后各导一次、目标端 provenance 与修订齐全）。
- **(b) 全部同步**（零新机制）。代价：①目标端的过期清扫会改写镜像行（下一包又被源端改回）；②目标端的忽略、删除会被下一包改回或重新插入
  （`docs/incremental-sync-design.md:194–196`「记忆是两端都写的唯一同步表」）；③两端各自产生修订，`(memory_id, revision)` 反复冲突产生 warning；
  ④镜像草稿占目标端的积压上限，目标端的建议因此停摆。保留 (b) 必须同时做到：过期清扫与积压计数排除镜像行（provenance 带 `sync_origin`）、
  目标端对镜像草稿不提供采纳/忽略按钮（只读展示），否则用户在目标端的操作会被静默撤销。
- **(c) 系统建议行一律不同步（含已采纳的）**：同样需要按行过滤（但不需要补发）；代价是已采纳的建议——已经是用户的记忆——在目标端缺席，与手动记忆不一致。

**重新评估后的推荐仍是 (a)**：它是唯一让「用户的记忆」在两端一致、且不把源端草稿的工作流带到目标端的选项；两项新机制都有界、可测。(b) 看似零成本，
但为了不让目标端用户的操作被静默撤销，需要的排除与只读处理并不比 (a) 少。

---

## 7. 质量风险：错误记忆污染检索

| 风险 | 防线 | 观测 |
| --- | --- | --- |
| 错误建议进入回答 | **结构性**：候选不在 `("confirmed",)` 之内，正式问答/报告/搜索读不到；零容忍守卫（fangan §19.5「candidate→正式平面」）新增系统建议用例 | 守卫测试 |
| 用户随手采纳了错的 | 只能逐条采纳；卡片显示来源回答与原话；纠正类（PR-3）显示「与资料不一致」 | 采纳后 30 天内被弃用/删除的比例 |
| 采纳后权威压过原始资料 | 与手动记忆同等；纠正类必须带冲突标记才允许提议 | 引用了已采纳建议的回答的「没用」率 vs 手动记忆 |
| 建议刷屏 | 积压上限 + 日额度 + 指纹 + 负缓存 + 30 天过期 | 忽略率、过期率 |
| 同一主题反复耗额度 | 负缓存（30 天） | `declined_cached` 计数 |
| 资料中的提示注入借回答节选进建议 | 不可信资料指令 + 封闭输出 + 服务端 id 校验 + 人审 | `model_rejected` 计数 |
| 与 KG 抽取叠加 | 采纳时沿用现有复选框（§14 D7）；派生对象受 0067 隔离，只影响本人 | — |

### 7.1 指标与评测（范围按实写）

- **在线漏斗（按信号类）**：提议 → 采纳（原样 / 编辑后）/ 忽略 / 过期 / 待处理；采纳率 = 采纳 /(采纳 + 忽略 + 过期)。
- **采纳后**：被检索命中次数、被 `[k]` 引用次数、30 天内弃用率、引用它的回答的有用/没用比（对照手动记忆）。
- **成本**：轮数、各零调用闸命中数、负缓存命中数、模型调用数与 token、`no_signal` 比例。
- **统计出口**（PR-1）：`scripts/auto_memory_stats.py` 只读聚合（按类、按状态计数，零内容）；记忆页给本人一行「系统建议：已采纳 N、已忽略 M」（PR-2）。
- **离线评测，现有工具能做的**：`backend/app/eval/memory_retrieval.py` 只评 **Memory 平面内部的排序**（Recall@5 / MRR / nDCG + 跨用户/跨库/候选泄漏零容忍计数）。
  PR-1 在它的固定 gold 里加「故意写错的已采纳建议」作干扰项与「未采纳建议」作泄漏探针：回答「错误记忆会不会挤掉正确记忆」「草稿会不会漏进正式平面」，
  回答不了「整体答案有没有变好」。
- **离线评测，需要新建的**：端到端三臂（无记忆 / 手动记忆 / 手动 + 已采纳建议）比较答案证据召回与引用，需要新建一套评测台——固定语料笔记本、带标注的问题集、
  能在三种记忆状态下重放真实 Ask（检索 + 合成）的驱动、答案级判分；仓库今天没有问答质量评测台（`fangan_todo.md` 规则 12 条目同此）。工作量与一个独立 PR 相当，是否做见 §14 D21。
- **在线不做随机分桶**：开关是用户自选的，分桶会违背用户选择；只做采纳前后与手动记忆的对照。
- **退出判据**：某信号类累计 ≥100 条提议后采纳率 <30% 即按类关闭（每类一个 `Settings` 布尔），登记进 `fangan_todo.md`「实验开关评估出口」（§14 D11）。

---

## 8. 与现有机制的边界（不重复造轮子）

| 需要的能力 | 复用 | 新增 |
| --- | --- | --- |
| 触发 | 提问完成观察者宿主 | 第四个能力常量与端口（§4.1） |
| durable 单飞 job | `agent_profile_jobs` 协议、轻活池、启动兜底 | `memory_suggest_jobs` |
| 存储、生命周期、版本、人审 | `memory_items` / revisions / provenance / `confirm` / `reject` | `suggested_signal` 列、`create_suggestion` |
| 去重 | 记忆向量与 FTS | 原始余弦近邻读取、指纹表（含负缓存） |
| 草稿形状 | `memory_preview` 的标题/正文/标签契约与确定性兜底 | 批量判定 prompt |
| 界面 | 记忆页候选审核、前端 `ask_answer` 溯源行 | 标记、理由、开关 |
| 偏好开关 | `PATCH /me/ui-mode` 范式（`system_routes.py:92`） | `PATCH /me/memory-mode`、`memory_suggest_users` |

不另建「用户记忆」表、不另建检索器、不另起状态机、不把内容写进理解覆盖层或经验库。是否搭车理解覆盖层那次模型调用，见 §14 D18。

---

## 9. 前端交互

### 9.1 开关放在哪

- **记忆页**（总记忆页页头 + 笔记本记忆页页头）：「记忆建议：已关闭 · 开启」，其中「开启」就是控件（`.link-button.inline-action`，AGENTS.md「提示即动作」）；
  空状态说明句同理。只读成员同样可开（记忆本来就是本人的）。
- **不放进「我的回答偏好」面板**：那个账户菜单项受 `USER_SEARCH_PROFILE_ENABLED` 总闸控制，关闭时整项不渲染（`frontend/app/account-menu.tsx:30`
  `searchProfileEnabled`）；记忆建议有自己的部署开关，经 `/system/config` 下发 `auto_memory_suggest_enabled`，两个开关互不牵连。
- 按下有可见变化、松手还原，「已开启」落在控件旁。开启说明：「开启后，系统会根据你之后在各笔记本的提问建议可保存的记忆，你采纳后才会生效」。

### 9.2 建议卡片与来源展示（D5(b) 下 `origin` 仍是 `ask_answer`）

- 卡片：「系统建议」标记；理由一行——「你在问题理解时确认了这一点」/「你问过 3 次相近的问题」/（PR-3）「你在追问中纠正了回答」；「查看原回答」（复用引用打开路径）；
  （PR-3）冲突标记。溯源行沿用前端 `ask_answer` 分支（`memory-model.ts:403`）。
- 来源徽标（`memory-panel.tsx:923`）：`suggested_signal` 非空时显示「系统建议」，否则照旧；采纳后仍显示「系统建议」（来历不因采纳而改变）。
- 来源筛选（`memory-panel.tsx:712`）：新增「系统建议」一项；「Ask 回答」只含 `suggested_signal` 为空的行。列表接口的 `origin` 筛选参数增加一个虚拟取值
  `suggested`，服务端映射为 `suggested_signal IS NOT NULL`（不改存储的 `origin`）；「Ask 回答」映射为 `origin='ask_answer' AND suggested_signal IS NULL`。
  映射必须同时落在无查询词与**带查询词**两条列表路径上：带查询词时总数与候选 id 由 `memory_match_count` / `memory_page_candidate_ids` 计算
  （定义在 `postgres/search.py`，由 `postgres/memory_store.py:2357–2371` 调用；SQLite 的带查询词列表路径同样要接受），两者都要接受同一个虚拟取值，否则筛选加搜索时总数与列表对不上。
- 导出的「类型」（`memory_service.py:84`/`:260`）：已采纳的建议写「系统建议（已采纳）」，其余照旧；未采纳的建议不导出（§6.3）。
- 按钮：「采纳」（= 确认，可先编辑；沿用 KG 复选框）、「忽略」（= 拒绝）、「编辑」。结果落在按钮自身。
- 计数并入现有待确认数；**不进铃铛**（§14 D10）。

### 9.3 「有用」后的就地提示（单列决策 D19）

用户点「有用」后按钮旁出现「要把这条回答存为记忆吗？」，「存为记忆」即现有「保存到 Memory」入口；纯前端、零后台。
**若对所有用户显示，会改变未开启用户的界面，与目标 3 冲突**，所以推荐只对已开启 `suggest` 的用户显示。

### 9.4 文案

全部过 `docs/ui-vocabulary.md` 与 `scripts/check_ui_vocabulary.py`；不出现「信号」「指纹」「负缓存」「suggested_signal」等内部词。

---

## 10. 数据与迁移（PG 为规范，SQLite 镜像）

迁移号以实现时 `origin/master` 最新为准（基线：PG 最新 `0084`、SQLite `SCHEMA_VERSION = 103`，`sqlite/migrations.py:361`）。按 §14 D5 推荐方案：

1. **不动 `origin` 的 CHECK**：系统建议的 `origin` 仍是 `'ask_answer'`；另加可空列 `memory_items.suggested_signal text NULL`（取值 `clarified|repeated|corrected`，
   应用层 Pydantic `Literal` 校验，两端都不加 CHECK）。两端都是 `ALTER TABLE ADD COLUMN`，不重建表，FTS 触发器不受影响。
2. `memory_mode`：应用层 `Literal["manual","suggest"]`，**不加 CHECK**（SQLite 加 CHECK 等于重建表）。
3. 部分索引（两端同形）：
   - `idx_memory_suggest_unadopted ON memory_items(created_by, notebook_id, status) WHERE suggested_signal IS NOT NULL AND status IN ('candidate','rejected')`——
     供 §6.3「原计数 − 未采纳计数」与积压闸；
   - `idx_memory_suggest_expiry ON memory_items(created_at) WHERE suggested_signal IS NOT NULL AND status = 'candidate'`——供过期清扫，免全表扫。
4. 新表 `memory_suggest_users(owner_id PK, enabled, enabled_at, quota_day, quota_used, updated_at)`。
5. 新表 `memory_suggest_jobs(notebook_id, owner_id, status, pending_signal, cursor_created_at, cursor_id, claim_token, runs, candidates_written,
   failure_reason, started_at, finished_at, created_at, updated_at)`，主键 `(notebook_id, owner_id)`，FK notebook `ON DELETE CASCADE`。
6. 新表 `memory_suggest_fingerprints(owner_id, notebook_id, fingerprint, kind, expires_at, created_at)`（§5.1），主键 `(owner_id, notebook_id, fingerprint)`，FK notebook `ON DELETE CASCADE`。
7. 视读取实测（§4.2 第 3 步）可能追加 `ask_jobs(created_by, notebook_id, created_at DESC, id DESC)`。

不再给 `user_profiles(user_id)` 加索引（热路径改读 `memory_suggest_users` 主键）。

**登记清单**（`docs/development*.md` 的新表 / 新列流程）：
- shadow manifest：三张新表、`memory_items.suggested_signal`、两条部分索引；新表的唯一面都是主键，停车方案按主键写。
- 跨环境同步 manifest（`app/migration/sync/manifest.py`）：三张新表登记为 LOCAL；`suggested_signal` 普通列无需登记（§6.6）；D15 (a) 的行谓词与子表跟随。
- 快照校验 `scripts/verify_repository_snapshot.py` 的期望 schema；`backend/tests/fixtures/repository_contract/` 合约夹具。
- `backend/app/repositories/user_references.py`：三张新表的 owner 列分类为 `scaffolding`（账号合并只在除脚手架外无引用时删号，**不迁移数据**；这些行只有开关、计数与哈希，随账号删除即可）。
- 深拷贝与笔记本导出的表分类：三张新表不拷不导。
- `scripts/merge_dbs.py` 的表清单。

零回填、零重算。

**若用户选 D5 (a)（新枚举值 `auto_suggest`）**，迁移必须额外满足：PG 用 `ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT`；SQLite 重建
`memory_items` 与 `memory_provenance` 时①关闭 `foreign_keys`（连接恒开 `PRAGMA foreign_keys=ON`，`sqlite/database.py:452`，否则 DROP 会级联删掉
revisions/provenance/embeddings）②保持 rowid（`memory_items_fts` 是 `content_rowid='rowid'` 的 external-content FTS5，`sqlite/migrations.py:1652–1657`）
③逐字节复原 3 个 FTS 触发器与 3 个同步捕获生成器产出的触发器 ④接受快照校验的 `schema-sql-changed` 并更新期望 ⑤重建后 `rebuild` FTS；
另外 §9.2 的来源展示与筛选改为按 `origin` 判断。

---

## 11. 测试与验收

**后端（两后端各一份，PG 泳道必跑）**
- 端口：MCP / 全局提问不计数；默认用户零写入（只有一次主键读）；失去读权后到达的通知不 upsert；在内置观察者中排最后，预算耗尽时被 `break` 不影响前三条。
- 游标：新建 job 行的起点含触发提问；关闭再开启、被移出再加入、自行退出再加入、退出群组再加入后，之前的提问都不进入样本。
  §4.2 游标去向表每种终态一例：截断项不被越过；`model_rejected` 不前移、可重试；同一批连续失败 3 次后越过并记 `gave_up`。
- 聚类窗口：下界取 job 行起点与 30 天前的较晚者、至多 40 条；只含游标之前问题的簇不算信号；平局按 Jaccard 最高、再按领头最早。
- 信号抽取：澄清答复取自本人网页回答且只投影 intent；`output='evidence'`、`report_id` 非空的提问被排除；关键词基 <2 的问题不聚类不登记；
  领头者聚类不串簇；已有记忆的回答不提议。
- 指纹：同指纹不重提——忽略后、过期后、删除后、「未变才删」后都不重提；短答复按选项区分；反复类过半规则（原样重问一次不压掉新簇）；负缓存 30 天内不送模、过期后可再送；
  写入优先级：declined → proposed 升级、proposed 上再写 declined 不变、declined 上再写 declined 只延期。
- 近邻读取：向量侧至多 500 条按 `updated_at`；词法侧包含 `rejected` 与 `deprecated`。
- 闸与额度：积压上限、部署开关、删除窗口、失去读权各落对应终态且不调用模型；两个笔记本的 job 并发预占额度不超额；`no_signal` 不占额度；UTC 日界线。
- 模型输出：越界 `item_id`、伪造近邻 id、超长字段、非 JSON → 整批丢弃且不写负缓存；`keep=false` / 判重写负缓存；单轮 >3 条只写 3 条。
- 关闭：job 在模型调用期间用户关闭开关 → 写事务 CAS 失败，零写入。写事务内的带锁成员探测：并发移出成员时不落建议。
- 衔接（§5.3 四行表各一例）；澄清类建议不占回答唯一索引；`answer-memory-links` 的 `links` 不含草稿、`states` 带状态。
- 过期：30 天迁 `rejected/expired`；已采纳的不受影响；清扫走部分索引。
- 读者口径（§6.3 表每行一例，含 `finish_member_exit` 豁免、退出群组不删记忆但删 job 行）；计数读者「原计数 − 未采纳计数」在单条 SQL 内、`GREATEST(0, …)`；
  管理员总览只减未采纳的 candidate；删除窗口提醒按两计数比较；正式平面零泄漏；他人 404；拷贝与导出不含；事件键白名单。
- 来源筛选 `suggested` / 「Ask 回答」在带查询词与不带查询词两条路径上总数与列表一致。
- 两个采纳入口（`confirm`、`create_from_answer` 的 CAS）副作用逐项相同：修订、向量、KG 调度、事件、同步补发。
- 自行退出竞态：job 在清空记忆与 `finish_member_exit` 之间写入一条建议 → 退出仍一次完成，remaining 为 0。
- 同步（若 D15 选 (a)）：建议导出前未采纳 → 包里没有它与它的子行；**分别经 `confirm` 与经 `create_from_answer` 采纳**后，下一个增量包都带上父行、
  provenance 与全部修订，目标端 `sync_origin` 正确；捕获关闭时不做补发 UPDATE；草稿被删 → 目标端 delete 计为 `absent` 不报错。
- 执行计划：重新核对 `tests/postgres/test_memory_sql_explain_pins.py`、`test_notebook_delete_grace_plans_pg.py`、`test_memory_live_predicate_explain_pins.py`；
  新增过期清扫与「未采纳计数」两条读的计划钉。
- 守卫：§6.1 新守卫；§6.3 片段的引用者与豁免者清单；`check_architecture_boundaries.py` 基线零松弛；热函数天花板只降不升。

**回退防线**：默认用户的问答轨迹、prompt 与响应字节一致；提问完成钩子耗时差在噪声内（同机 200 次 p50/p95）；记忆计数类读者在无系统建议的库上结果与计划逐字一致。

**前端**：记忆页就地开启、卡片标记与理由、来源徽标与筛选、逐条采纳、关闭弹窗、「有建议 / 已保存」区分、交互反馈；vitest + 预览截图。

**验收（本机，deepseek-flash）**：开启 → 在一个库里答一次澄清（直接达阈）→ 记忆页出现澄清建议 → 相近问题问 3 次再凑满阈值 → 出现反复类建议
→ 采纳 1 条，下一次相关提问轨迹「记忆」步命中它（`k3000+`）→ 忽略 1 条后删除，再触发不重提 → 关闭，再提问 job 不跑。

---

## 12. 文档与发布

- `docs/product-and-api*.md`：Memory 一节加「记忆建议」契约与数值表；`PATCH /me/memory-mode`；`answer-memory-links` 的 `states`；§6.3 各读者口径；MCP `suggested_by_system`；列表筛选 `origin=suggested`。
- `docs/deployment-and-configuration*.md`：新工作负载与开关；`model-services.example.toml` 同步。**升级须知**：严格模式下新工作负载未绑定会拒启——发布说明写
  「先在 `[bindings]` 补 `memory_suggest`，或临时 `MODEL_BINDINGS_STRICT=false`」。
- `docs/deployment-extensions-sop*.md`：观察者第四项能力、通知新字段与执行顺序。
- `docs/incremental-sync-design.md`：新表 LOCAL、D15 的结论与行过滤/补发机制。
- `docs/agent-mcp-memory-sop*.md`：候选里会出现系统建议。
- `architecture.md`：提问完成观察者第四条链。
- `release-notes/`：`level: feature`。`fangan_todo.md` 条目移入 `fangan_done.md`。

---

## 13. 分期

| 期 | 内容 | 交付判据 |
| --- | --- | --- |
| **PR-1 后端** | 迁移与登记清单、观察者第四端口（SDK 追加）、job、信号一 + 二、`create_suggestion`、指纹与负缓存、§5.3 衔接与 `answer-memory-links` 追加字段、`PATCH /me/memory-mode`、§6.3 读者口径与计划钉、过期清扫、D15 的同步机制、事件、守卫、`scripts/auto_memory_stats.py`、Memory 平面评测的干扰项夹具、文档 | 默认用户逐字零变化；`suggest` 下本机能产出建议 |
| **PR-2 前端** | 记忆页开关与关闭弹窗、卡片标记/理由/来源徽标与筛选、逐条采纳、「有建议/已保存」、本人统计行、D19 就地提示（若采纳）、发布说明 | 全栈对等；验收流程走通 |
| **PR-3** | 信号三「追问纠正」+ 冲突标记；D21 若选做，端到端评测台 | 纠正类卡片带冲突标记；（若做）三臂评测出数 |

PR-1 合入时部署开关可开，但没有前端入口、用户默认 `manual`，对用户不可见（`answer-memory-links` 的旧字段对没有建议的用户逐字不变）。按「特性不许留一半」，三期同属本特性计划范围，D3 / D21 拍板后范围即定。

---

## 14. 需要用户拍板的决策点

**裁决（用户，2026-10-28）：D1–D21 全部按下表「推荐」列执行，已立项。**

| # | 问题 | 选项 | 推荐 |
| --- | --- | --- | --- |
| D1 | 「自动」到哪一步 | (a) 自动提议为待确认，用户逐条采纳；(b) 自动直接写正式记忆；(c) 两档都给 | **(a)**。(b)/(c) 让没人看过的内容获得权威 3、压过本人资料，且违背层 C 人审门 |
| D2 | 默认开还是关 | (a) 每人默认关，记忆页就地开启；(b) 默认开；(c) 部署配置决定默认 | **(a)**。零可见行为回退、零新增成本 |
| D3 | v1 信号范围 | (a) 澄清答复 + 反复提问，纠正放 PR-3；(b) 三类一次做完；(c) 只做澄清答复。附加项 (x)：带澄清答复的提问直接达阈 | **(a) + (x)**。纠正类风险最高，先拿前两类的采纳率校准 |
| D4 | 模型工作负载 | (a) 新增 `memory_suggest`（background）；(b) 复用 `memory_preview`（interactive） | **(a)**。后台批量不该与用户正在等的预览抢交互队列；代价是升级须补一条绑定。**数据外发**：两种都会把用户的问题、澄清答复与回答节选发给所绑定的模型服务，若绑的是外部服务，这些内容会离开部署——与 `memory_preview`、`agent_profile_consolidate` 同一类，由管理员的绑定决定 |
| D5 | 系统建议的来源怎么存 | (a) `origin` 新枚举 `auto_suggest`（PG 改 CHECK；SQLite 重建两表，五项迁移要求见 §10 末）；(b) `origin` 仍为 `ask_answer`，另加可空列 `suggested_signal` | **(b)**。不重建表；前端 `ask_answer` 溯源行正是建议需要的；存储层按 `origin` 的分支只判 `external_agent`；导出类型、来源徽标与筛选按 `suggested_signal` 区分（§9.2） |
| D6 | 未处理建议怎么过期 | (a) 30 天转「已忽略（过期）」；(b) 永不过期；(c) 30 天硬删 | **(a)**。不重提由指纹保证，与过期方式无关 |
| D7 | 采纳时「同时抽取到知识图谱」的默认 | (a) 与手动一致默认勾选；(b) 系统建议默认不勾 | **(a)**。采纳后它就是用户的记忆；派生对象只对本人可见 |
| D8 | 批量操作 | (a) 不新增任何批量采纳/忽略，只逐条，批量删除沿用现有；(b) 新增批量忽略；(c) 新增批量采纳 | **(a)**。今天本就没有批量确认/拒绝；批量采纳等于绕过人审 |
| D9 | MCP 是否返回未采纳的系统建议 | (a) 返回，带 `suggested_by_system: true`；(b) 不返回 | **(a)**。数据属于 token 主人，现有候选契约已标 `unconfirmed` |
| D10 | 新建议是否进铃铛 | (a) 不进，只并入记忆页待确认数；(b) 进铃铛 | **(a)** |
| D11 | 信号类退出判据 | (a) 每类 ≥100 条提议后采纳率 <30% 即按类关闭，登记评估出口；(b) 不设 | **(a)**，阈值请用户定 |
| D12 | 关闭开关时未处理的建议 | (a) 弹窗给「同时删除 N 条」默认不勾；(b) 关闭即全删；(c) 只保留 | **(a)** |
| D13 | 统计面 | (a) 事件 + 只读统计脚本（PR-1）+ 本人一行计数（PR-2）；(b) 另加管理员聚合页 | **(a)**；若要管理员页，只出计数不出内容 |
| D14 | 是否回溯开启前（及关闭期间、上次成员身份期间）的提问 | (a) 不回溯：job 行的生命周期就是采样窗口；(b) 回溯最近 30 天 | **(a)**。用户同意的是「今后」；回溯会在开启瞬间撞满积压上限 |
| D15 | 系统建议是否随跨环境增量同步 | (a) 只同步已采纳的（含已弃用）：新增行谓词 + 子表跟随 + 采纳时补发 provenance 与修订（§6.6）；(b) 全部同步：零新机制，但须让目标端过期清扫与积压计数排除镜像行、镜像草稿只读，否则目标端操作会被下一包撤销；(c) 系统建议行一律不同步（含已采纳的） | **(a)**。代价写实后仍推荐：唯一让「用户的记忆」两端一致、又不把草稿工作流带到目标端的选项；两项机制都有界可测 |
| D16 | 已忽略/过期的建议绑着某回答，用户又在这条回答上「保存到 Memory」 | (a) 复活：CAS `rejected → confirmed`（为系统建议开一条新迁移边）；(b) 替换：同一事务删旧行、插普通手动记忆 | **(b)**。结果与普通手动保存完全一样；不给状态机开新边 |
| D17 | 「你的记忆」类读者是否计入未采纳的系统建议 | (a) 都不计入，只进记忆页审核队列；唯一例外是自行退出（会删除它们）时多说一句；`finish_member_exit` 核对与删库通知受众豁免（§6.3 表）；(b) 与 Agent 候选一样按今天的全状态口径计入 | **(a)**。草稿不可导出、不可转移，提醒无事可做；否则只开了开关、一条没采纳的人会收到删除提醒、卡片显示一堆「记忆」 |
| D18 | 是否搭车「我的检索心得」巡固那次模型调用 | (a) 独立调用（本设计）；(b) 并入 `agent_profile_consolidate` 覆盖层调用，零新增模型调用、零新增绑定 | **(a)**。(b) 省一次调用和一条升级绑定；但要改一个在用特性的 prompt 与输出形状（有回退风险）、覆盖层取样是轨迹摘要不含回答与澄清答复、触发节奏不同（10 次/报告）、把「不进答案」的块与「可进答案」的内容混在同一次输出、两边失败互相牵连 |
| D19 | 「有用」后的就地「存为记忆」提示 | (a) 不做；(b) 对所有用户显示（**改变未开启用户的界面，与目标 3 冲突**）；(c) 只对已开启 `suggest` 的用户显示 | **(c)** |
| D20 | 「就此追问」报告的提问是否进入样本 | (a) 排除（`report_id` 非空）；(b) 纳入 | **(a)**。回答会转述报告正文，报告可能是别人写的，不应被洗成「我的知识」 |
| D21 | 是否新建端到端三臂评测台 | (a) 不建，只做 Memory 平面排序评测的干扰项 + 在线指标；(b) 在 PR-3 新建（固定语料、标注问题集、三种记忆状态下重放真实 Ask 的驱动、答案级判分，约一个独立 PR 的工作量） | **(a)**。建议在用户采纳之后才生效，答案层面的效果与手动记忆同质；先用在线采纳率与采纳后弃用率判断 |

---

## 15. 参考

- Mem0「新事实 + 近邻旧记忆 → ADD/UPDATE/DELETE/NOOP」（本设计只取 ADD/NOOP，§5.2）；Claude Code 会话后 `extract-memories` 的「后台抽取、阈值触发」；
  Agentic Memory 设计 §2.1 调研结论 3–5。
- 本仓库先例：`agent_profile_job`（durable 单飞、`served_ids` 校验、用户权威、`_member_can_read`）、`search_profile`（推断值须用户确认）、
  `retrieval_experience_*`（隐私守卫写法）、`docs/incremental-sync-design.md`（新列无需同步登记、schema pair 相等才导入）。

---

## 16. 修订记录

**第一轮 opus 评审后修订。** 指纹不稳定与「永不重提」做不到（改独立指纹表、反复类成员指纹、措辞降为「按指纹不重提」、去掉 200 条带正文保留与确定性余弦判重）；
回答唯一索引冲突（澄清类不绑回答、§5.3 四行表、D16）；跨环境同步（§6.6、D15）；读者口径（§1.5、§6.3、D17）；SQLite 重建风险（D5 改推荐 (b)）；
观察者宿主串行与 SDK 契约；`_member_can_read`；`user_references` 不迁移数据；评测工具的真实范围（D21）；没有批量确认/拒绝（D8）；`create_candidate` 检查在事务外；
去重不复用 `_hits`；日额度竞态；关闭即时生效；游标语义；搭车选项（D18）；就地提示单列（D19）；统计脚本移到 PR-1；登记清单；`output='answer'` 与只投影 intent；
读计划实测；反馈多行取最新；开关不放进受 `USER_SEARCH_PROFILE_ENABLED` 控制的面板；并发冲突 no-op；D4 数据外发；就此追问（D20）。
初稿 D5 推荐 (a) 的理由（「存储层按 `origin` 分支」）是错的：存储层判的是 `external_agent`。

**第二轮 opus 评审后修订。** 评审确认了第一轮的两处异议（版本错配在导入 `_preflight` 被拒、新普通列无需同步登记），措辞改为 `_preflight`（`import_.py:2505–2522`）。本轮改动：
- D15(a) 补上「父行后来才入选」的缺口：采纳事务对 provenance 与全部修订做无变更 UPDATE 触发捕获；过滤只作用于 upsert；「已采纳」含 `deprecated`；子表跟随父行，否则 `_verify_row_scopes` 拒包；
  (b) 的代价写全（过期清扫改写镜像行、目标端操作被撤销、修订冲突、积压被占）并列出保留 (b) 的必要附带；重新评估后仍推荐 (a)。
- `answer-memory-links` 改为追加字段（`links` 形状不变但不含草稿，新增 `states`），两者都在 PR-1。
- 更正：退出群组不删除记忆（只是看不到）；被移出与取消链接分享同样不删记忆；job 在认领时落 `skipped`，草稿照常过期。
- 读者表补 `notebook_content_overview`（套片段）、`delete_notice_audience`（超集，不受影响）、`finish_member_exit`（豁免并入守卫）；写事务内用 `access_sql` 带锁成员探测。
- 指纹表去掉 `state` / `memory_id`，「存在即不提」，删除路径不再需要钩子；新增 `kind` / `expires_at` 承载负缓存。
- 过度抑制：关键词基 <2 不聚类不登记；领头者聚类代替单链；反复类改「过半成员命中」；澄清类短答复并入选项。
- 负缓存：模型判否 / 判重写 30 天负缓存，不再每轮重送同一主题。
- 计数读者改为「原计数 − 未采纳计数」走新部分索引，现有计划不变；过期清扫有部分索引；三个计划钉列入 §11 重新核对。
- 游标：job 行的生命周期就是采样窗口，起点是建行的那条触发提问（通知新增 `ask_job_id`）；`remove_member` / `kick_all_members` 也清 job 行。
- D5(b) 下导出类型、来源徽标、来源筛选怎么显示系统建议（§9.2）。
- D17 理由统一：只在用户自己发起、会删除草稿的动作里告知；其余计数与提醒都不计。
- 只在采纳时向量化（每轮 embedding 从 2 次降到 1 次），去重对草稿走词法 + 模型。
- 「排最后」只对内置观察者成立；每日额度按 UTC 日；热路径闸门改读 `memory_suggest_users` 主键，不再给 `user_profiles` 加索引。

**第三轮 opus 复审后修订（无 P0/P1，补齐实现期细节）。**
- 聚类窗口写明：下界 max(job 行起点, 当下 − 30 天)、上界当下、至多 40 条；只有含游标之后新问题的簇才算信号；平局入 Jaccard 最高、再按领头最早（§3.2、§4.2 第 3 步）。
- 计数读者的减法在同一条 SQL 内完成并兜 `GREATEST(0, …)`；管理员总览只减未采纳的 candidate；删除窗口提醒由 EXISTS 改为两计数比较，计划钉随之重写（§6.3）。
- 指纹写入优先级：proposed 永不降级、declined 可升级、重复 declined 只延期，并补用例（§5.1）。
- 两个采纳入口（`confirm`、`create_from_answer` 的 CAS）共用同一收尾，副作用逐项相同，同步用例各覆盖一例（§5.3、§11）。
- 出处更正：目标端对不存在行的 delete 由 `_replay_delete_table`（`import_.py:5943`，说明约 :5950–5966）计为 `absent` 跳过；此前引的 :4508 是 `_prune_superseded` 一带。
- 按终态写明游标去向；截断项不被越过；同一批连续失败 3 次后放弃并记事件（§4.2）。
- 近邻读取：向量侧照搬 `vector_limit` 500、按 `updated_at`；状态集合由调用方传入并包含 `rejected`（§4.2 第 6 步）。
- 自行退出竞态：认领步删 job 行；`finish_member_exit` 计数前先删竞态窗口里出现的草稿（§5.4）。
- 退出群组同样删除 job 行，使 D14「不回溯上次成员身份期间」对所有失去读权的方式成立（§5.4）。
- 已忽略/过期的建议行永久保留，增长每人每天至多 9 条，登记为已知行为（§5.4）。
- 来源筛选的虚拟取值同样落在带查询词的 `memory_match_count` / `memory_page_candidate_ids` 上（§9.2）。
- 捕获关闭时跳过同步补发的 UPDATE（§6.6）。
