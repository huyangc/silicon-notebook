# Agentic Memory 设计方案（讨论稿）

日期：2026-08-18　状态：**已定稿**——Q1–Q6 全部拍板（见 §12），按 §10 分期实施，P1 开工。

---

## 1. 背景与目标

每个 notebook 有一个 agent bot 负责该库（及挂载参考库）的检索、问答与深度报告。当前每次 agentic search 都从零开始：agent 对库的理解、对"这个库怎么查有效"的经验、对用户习惯的了解，全部不跨轮保留。

三个目标：

1. **Notebook 级 agent 记忆**：agent 对本库形成累积理解，每个 notebook 的 agent 都随使用变得更懂这个库。
2. **用户级检索习惯个性化**：每个用户的提问/检索习惯能被记住并改进后续体验。
3. **MCP 开放**：外部 code agent 接入本系统时，也能读写相关记忆。

非目标（本方案明确不做）：

- 不改变现有 Memory（`memory_items`）的产品语义——它仍是"用户可读写的显式知识条目"，人审门不动。
- 不做跨 notebook 的 agent 通用经验迁移（`agent_profiles` 仍只是 token 身份）。
- 不做自动遗忘算法——用预算上限与作用域清空代替（见 §2 调研结论 5）。

---

## 2. 调研摘要：通用做法的关键结论

对 MemGPT/Letta、Mem0、Zep/Graphiti、A-MEM、LangMem、Generative Agents、Claude memory tool / Claude Code auto-memory、Reflexion/Voyager/ExpeL/AWM/R²-Mem/MERIT 及 MCP memory 生态做了机制级调研。与本方案直接相关的结论：

### 2.1 跨系统共识

1. **RAG ≠ 记忆**。记忆的定义性特征是会被 agent 主动改写；检索只是记忆系统里的一个工具（Letta 原话）。
2. **"常驻上下文的一小块"与"外部可检索的一大堆"必须分开设计**。两个代表形态：
   - Letta core memory block：`label / description / value / limit` 四元组——有名字、有职责说明、有硬字符预算的**少数几个槽位**，而非无界条目池。`description` 让"该往哪写"成为确定性路由；`limit` 保证永不吃光上下文。
   - Claude Code auto-memory：`MEMORY.md` 索引常驻（硬上限 200 行/25KB），主题文件按需读。**索引硬上限是整套设计的支点**——它强制后台整理做真正的合并/删除/下沉，比重要性打分更有效。
3. **写入侧重、读取侧轻**。LLM 调用花在写入侧（抽取、去重、链接），读取尽量一次 cosine top-k（A-MEM 极致如此）。Mem0 的更新决策范式：新事实 + top-10 相似旧记忆 → 一次 function-call 选 `ADD/UPDATE/DELETE/NOOP`——冲突消解被限定为局部、可测试的判断。
4. **后台异步巩固是必需品，不是优化**。hot path 只做最小写入，深度整理放后台（Letta sleep-time/dreaming、Claude Code 会话后 `extract-memories`、OpenAI dreaming）。**触发用确定性阈值而非定时**（Generative Agents：累积 importance > 150 才反思，每天实际 2–3 次，零模型调用的闸）。
5. **自动遗忘没人做对**。实用做法只有三种：预算上限逼出取舍（Claude Code / Letta block limit）、作用域到期（Mem0 `run_id`）、永不删只标失效（Zep 双时间轴 `valid_at/invalid_at`）。
6. **作用域必须是存储层取数 SQL 里的谓词**，不是结果侧过滤（Zep 对 `group_id` 的明确表述）——与本仓库 Memory `created_by` 谓词红线同源。
7. **高层结论必须保留到底层证据的可追溯链**（Generative Agents 洞见格式 `insight (because of 1, 5, 3)`）。

### 2.2 检索策略记忆（与目标②直接相关）

- **R²-Mem**：把搜索经验蒸成 `IF <situation> THEN <strategy>` 条目，分「规划经验」「反思经验」两库、好/坏分区；检索时先把当前处境抽象成 situation 表示再 embedding 匹配（**不能拿原始问题查经验库**——"相似的问题"≠"相似的处境"）。反直觉发现：**从失败步骤学比从成功步骤学更有效**。LoCoMo F1 +22.6%、迭代轮数 −20.2%。
- **AWM**：归纳经验时把具体值参数化成占位符（`"dry cat food"` → `{product-name}`），一条经验才能覆盖一族任务；跨未见域提升 8.9–14.0pp 是这一条的直接收益。
- **MERIT**：任务级经验（交互开始时注入）与步级经验（交互中按 state signature 注入）双层。
- ⚠️ **"per-corpus 检索策略记忆"在公开文献里是空白**：R²-Mem 经验库是全局的，MERIT 明确放弃 per-database 组织。机制形状可抄，作用域切法无前人验证——稳妥路线是**全局库 + notebook 作为检索时的过滤/加权维度**，避免每个新库冷启动全空。

### 2.3 MCP memory 工具面惯例

（官方 memory server / Mem0 MCP / Graphiti MCP 的一致规律）

1. 工具数 6–9 个；**读拆两个粒度**：宽查（语义搜索）+ 精确取（按 id）——只给语义搜索，外部 agent 拿不到确定的东西。
2. **作用域是显式参数且可在服务器侧钉死**，不让模型自由填。
3. 批量删除必须带已确认作用域，不提供无作用域 `delete_all`。
4. 写入异步排队、工具立即返回，不阻塞在 LLM 抽取上。
5. **外部写入面窄于内部**：官方 memory server 的 observation 模式——外部只能追加原子观察，高层结论由内部巩固进程生成，投毒爆炸半径被限制在可追溯的原子条目上。

---

## 3. 现状盘点：地基与缺口

### 3.1 可复用的地基

| 拼图 | 现状 | 关键位置 |
| --- | --- | --- |
| 条目存储 | `memory_items` 家族：内容 + 版本链（revision CAS）+ embedding + FTS | `backend/app/repositories/*/memory_store.py`、`migrations.py` |
| 检索器 | `MemoryRetriever`：词法+向量双路融合、语义准入阈值、上下文硬预算、`k3000+` 独立编号空间；已被 Ask 与深度报告共同复用 | `backend/app/services/memory_retrieval.py`；`report_engine.py` 两处调用 |
| 人审门 | `candidate → confirmed` 状态机；外部 agent 只能 `create_candidate` | `memory_service.py` |
| MCP 鉴权 | `agent_access_tokens` + scope 枚举 + notebook 白名单，每次调用实时复核 | `backend/app/api/mcp_server.py`、`memory_service.py` |
| MCP 工具面 | 约 12 个 tool（`propose_memory`/`search_agent_memory`/`ask_notebook` 等），带响应脱敏/截断 | `mcp_server.py` |
| 运行内注入先例 | **集合地图**：每 run 建一次、≤600 字符、注入 plan/reflect 上下文、随 `ReasoningResult` 进合成上下文 | `reasoning_retrieval.py` |
| reflect action 先例 | `exact_lookup`/`ppr_retrieve` 的 `allow_*` 策略位机制；`update_outline` 便签（运行内可写、稳定 id、8 键滚动） | `reasoning_retrieval.py` |
| 后台 job 先例 | `kg_build_jobs` durable 单飞/探活/熔断/排空；检索索引 idle scheduler | `catalog_job.py`、`kg` 服务 |
| 变更历史先例 | knowhow `record_change`（写事务最后一步、before/after + 指纹） | knowhow store |
| per-notebook 状态先例 | `unified_kg_state`（单行状态位）、`notebook_object_schemas`（notebook 覆盖行，v47/v25 最新迁移） | `migrations.py` |
| per-user 偏好先例 | `user_profiles.ui_mode` 的 PATCH 范式；**`memory_mode`/`domain_focus` 两列建表即有但从未被任何代码写过**（空壳占位） | `identity_store.py`、`system_routes.py` |
| trace 透明先例 | `memory`/`skip` 步带耗时入轨迹；事件脱敏红线（不带正文） | `ask_service.py` |

### 3.2 缺口（本方案要新建的）

1. **Memory 不在 reflect 循环内**：现在是循环前一次性预取、只在最终合成汇合；agent 无法在推理中主动查记忆。
2. **没有检索策略记忆**：Memory 存内容性知识，"这个库怎么查有效"无处安放；`outline` 便签只在单轮内生效。
3. **没有 consolidation**：多条记忆自动合并/去重/摘要机制为零（唯一近似是人工触发的 Memory→KG 晋升）。
4. **用户习惯画像是空壳**：`memory_mode`/`domain_focus` 有列无逻辑。
5. **产品哲学张力**：现有设计刻意要求外部 agent 写入必须人审——"agent 自动固化经验"需要一条有边界的新通道（§4 的信任分级就是解法）。

---

## 4. 总体设计：三层记忆，按消费面分信任级别

核心思路：**按记忆被谁消费来分信任级别，而不是按记忆来自谁**。

| 层 | 内容 | 作用域 | 消费面 | 写入信任 |
| --- | --- | --- | --- | --- |
| **A. Notebook 画像块** | agent 对本库的累积理解（库形状、核心概念、检索要领、已知缺口） | notebook 级（待拍板，§12-Q2） | 注入规划 prompt；**不进答案正文** | 后台自动写；界面可见、可编辑、可清空 |
| **B. 策略经验 + 用户 Profile** | IF-THEN 检索经验；用户习惯单文档 | 经验：全局 + notebook 加权（待拍板，§12-Q3）；Profile：per-user | 只影响 agent 怎么查、怎么组织答案 | 自动写，免人审 |
| **C. 内容性知识** | 现有 Memory | user × notebook 私有 | 进答案、可被 `[k]` 引用 | **人审门维持不变** |

分级论证：A/B 层的内容不进答案正文、不可被引用为证据，错了的代价是检索效率下降而非事实错误上屏，且随时可清空重建；C 层进答案，才需要人审。因此这不是推翻"外部写入必须人审"，而是新开两条**消费面更窄**的通道。

三层与现有红线的关系见 §9。

---

## 5. 层 A：Notebook 画像块

### 5.1 数据模型：共享底座 + 私有覆盖层（方案丙，已拍板）

仿 Letta memory block：固定若干**具名块**，每块 `label / description / value / limit / revision / updated_at / evidence_json`。

**块按其巡固输入的数据归属分成两组**（这是 Q2 展开后的核心结论，论证见 §12-Q2）：

**共享底座（notebook 级，一库一份）**——原料全部是 notebook 级数据，共享库里成员本来就可见：

| label | description（决定写入路由） | 内容示例 |
| --- | --- | --- |
| `corpus_shape` | 这是什么类型的库、资料结构、语言分布 | "以芯片设计工具手册为主，命令参考格式，中英混排" |
| `key_entities` | 反复出现的核心概念/命名/缩写 | "PPA、`set_db` 命令族、DAC 在本库指数模转换器" |
| `corpus_gaps` | **语料侧**缺口与解析质量问题 | "X 手册只有目录无正文；三份 PDF 是降级解析、表格可能残缺" |

**私有覆盖层（user × notebook 级，每个提问过的成员一份）**——原料是该成员自己的提问轨迹：

| label | description | 内容示例 |
| --- | --- | --- |
| `retrieval_notes` | 我在本库有效/无效的查法 | "命令名走精确通道命中率高；概念性问题 PPR 扩展噪音大" |
| `usage_gaps` | 我查过但本库查不到的 | "薪酬相关内容零命中" |

要点：

- **不做无界条目池**。块数固定、每块有硬字符预算，超预算由巡固过程自己取舍——这是调研结论 2 的直接落地，也天然解决遗忘问题。
- **证据可追溯**：`evidence_json` 存每条结论支撑的 `source_id` 列表（Generative Agents `because of` 格式）。来源被删/重解析时，巡固下一轮据此复核；界面上结论可点开来源。**没有存活证据支撑的断言在巡固时降权或删除**（覆盖层的 `usage_gaps` 例外——"查不到"本就没有 source 证据，它的证据是零命中账目）。
- 一张表 `agent_notebook_profile(notebook_id, owner_id, label, ...)`：`owner_id` NULL = 共享底座行，非 NULL = 覆盖层行；唯一约束 `(notebook_id, owner_id, label)`（nullable 列正是 shadow 停车方案最省事的形状）。变更历史仿 knowhow `record_change` 存 before/after（供界面回看"agent 的理解怎么变的"与回滚）。
- **归属语义的边角**（定稿即契约）：只读成员也能提问，故也有自己的覆盖层，但不能编辑共享底座（底座编辑权同 knowhow 口径：owner/可写成员；覆盖层本人永远可编辑可清空）。成员被移出/降级后其覆盖层随参与集实时判定失效，物理清理挂成员移除路径或惰性清扫。notebook 深拷贝**不复制**任何画像行（副本从零巡固，登记为刻意行为）。画像只属于 active notebook，不跨库读挂载参考库的画像（同 exact_lookup"挂载 base 联邦刻意未做"口径）。单人库下两组退化为同一人所有，可在同一次 job 内巡固，用户感知为一份完整画像。

### 5.2 注入点（读路径）

- **规划前注入**：走集合地图同款形状——`ReasoningRetriever` run 开始时读取**共享底座 + 当前提问者自己的覆盖层**（别人的覆盖层行根本不进本进程，同"别人的 Memory 源 id 不进本进程"口径），按固定顺序拼成一个带表头的小块（硬预算截断），注入 plan 与 reflect 上下文。深度报告引擎逐字复用 `ReasoningRetriever`，**天然贯通**，无需第二条接线；报告的意图理解/大纲规划阶段是否也注入见 §12-Q5。
- **合成阶段**：画像块**不进**答案合成上下文（它不是证据，防止模型把画像内容当事实写进答案）。`retrieval_notes` 只影响查法，`corpus_shape` 只影响规划措辞。
- **trace 透明**：注入发生时在轨迹记一步（仿 `memory` 步，带块数与字符数，不带正文），关闭态零痕迹。
- **开关**：总闸 `AGENT_PROFILE_ENABLED`（默认 true，参照 `REASONING_ENUM_TOOLS_ENABLED` 的单点判定模式——注入/巡固/界面/MCP 四处共用一个判定）。

### 5.3 巩固 job（写路径）：两条独立链路

隔离靠**取数 SQL 的谓词**，不靠 prompt 约束（论证见 §12-Q2）：

**底座链路**（per-notebook 单飞）：
- **触发**：来源增删/重解析累计 ≥ N₁（确定性阈值，零模型调用的闸；阈值为部署可调 Settings）。
- **输入**（全部有界）：当前底座块 + 语料统计（来源数/类型分布/解析警告，现成 SQL 聚合）+ KG 对象聚合。**取数 SQL 不 join `ask_jobs`/trace/Memory——结构上拿不到任何成员的使用数据**，无需任何"别写进去"的软约束。
- 共享库全员受益，一库巡固一次。

**覆盖层链路**（per (notebook, user) 单飞）：
- **触发**：该用户在该库已完成 Ask 数 ≥ N₂，或其深度报告完成 1 次（报告完成是天然的高信息量结束点）。
- **输入**（全部有界）：该用户当前覆盖层块 + **仅该用户自己的** trace 摘要（谓词 `WHERE user_id = ?` 写在取数 SQL 里，同 `memory_items.created_by` 红线形状）+ 该用户 token 下外部 agent 的观察（§7）。因产物只有本人可见，查询词/失败话题可以放心进巡固输入——这正是拆两条链路换来的自由度。
- 私有 Memory 内容仍不进任何巡固输入。

两条链路共同的执行契约：
- **执行**：复用后台队列先例（轻活队列），durable 行 + 条件唯一索引单飞 + 每条退出路径落终态 + 启动兜底收 queued（同 `kg_build_jobs` 协议）。
- **调用**：各一次有界 LLM 调用，输出对应组块的新值 + 每条结论的证据 source_id。畸形输出整体丢弃保留旧块（fail-open），不重试烧钱。
- **写入**：单写事务更新块 + record_change 历史；乐观并发用 revision（照抄 memory_revisions 的教训：不用时间戳，SQLite 截秒会假阴性）。
- **模型通道**：复用现有 workload 机制新增 `agent_profile_consolidate` workload，不新增配置面之外的东西。
- 单人库两条链路可合并为一次 job 执行（输入谓词仍分开写）。

### 5.4 界面

- 知识图谱视图或笔记本设置页新增「AI 对这个库的理解」面板：四块可读、可编辑（编辑走人写路径，同样记历史）、可单块清空、可整体重建（手动触发一次巡固）。
- 用户编辑过的块，巡固时作为"用户权威输入"合并而非覆盖（用户改动优先保留——这同时是冷启动通道：用户可以直接告诉 agent 库的形状）。
- 界面词不得出现内部黑话（词汇守卫红线）；"画像/巡固"等词的界面表达待定稿时过一遍 `AGENTS.md` 界面词汇表。

---

## 6. 层 B：检索策略经验 + 用户 Profile

### 6.1 检索策略经验（IF-THEN 库）

- **形态**（R²-Mem + AWM）：条目 = `IF <situation> THEN <strategy>` + 好/坏标记 + 出处（哪次 run 蒸出的）+ 适用特征（语料类型/规模/语言等 notebook 特征，用于加权）。具体值参数化成占位符（`set_db` → `{identifier}`）。
- **数据源是现成的**：Ask trace 已持久化每步动作/耗时/结果计数。outcome 信号：
  - 成功：某检索步产出的证据最终被 `[k]` 锚点绑定（答案实际引用）。
  - 失败（**更值得记**）：零命中步、被 skip 的动作、白跑的方向（attempted 但零绑定）、exact_lookup 四类 skip 账目。
- **蒸馏**：离线后台，与层 A 巡固同 job 或独立低频 job；每次读一批已完成 run 的 trace，蒸出/合并条目（Mem0 式局部决策：新经验 + top-k 相似旧经验 → ADD/UPDATE/NOOP）。
- **作用域**：按 §2.2，默认**全局库 + notebook 特征加权**（待拍板 §12-Q3）。全局库上限固定条数，满了按"最近被采用次数"淘汰。
- **消费**：v1 只做**任务级注入**——run 开始时按当前处境（intent 契约的结构化字段，而非原始问题）检索 top-k 条目，与画像块同处注入规划上下文。步级注入（reflect 循环中途按 state signature）留 P4。
- **红线边界**：策略经验只影响"怎么查"（动作选择、查询措辞、通道偏好），**绝不影响检索范围**——"用户勾选是检索范围的唯一来源"红线不动；也不得建议收窄/扩大 source_scope/base_scope。

### 6.2 用户习惯 Profile

- **形态**（LangMem Profile 语义）：per-user **单文档、只更新不追加**——语言偏好、常用检索档位、偏好的答案组织形态（表格/散文/详略）、常用领域词。与策略经验分开存：Profile 答"这个用户什么样"，经验库答"这类处境怎么办"。
- **存储**：复用 `user_profiles` 现有空列位（`domain_focus` 改造或新列 `search_profile_json`，迁移时机与层 A 新表合并进同一个 `_migration_48`）。
- **写入**：优先级 = 用户显式指示（设置页直接编辑）> outcome 可判的归纳（后台低频，从该用户已完成 run 统计）> LLM 自主判断（不做 v1）。
- **消费**：注入 Ask/报告的规划与合成风格提示（合成侧只影响组织形态措辞，不影响证据与事实）。自动模式（`ui_mode=auto`）下隐藏控件的强制默认值红线不动——Profile 不得代替用户改档位，只能在**用户可见控件的默认选中值**上做个性化预填（待拍板 §12-Q4）。

---

## 7. 层 C：MCP 开放给外部 code agent

完全复用现有 token/scope/notebook 白名单/审计，只增 scope 与 tool：

### 7.1 新增 scope

- `agent_profile:read` —— 读层 A 画像块。
- `agent_observation:write` —— 追加原子观察（见下）。
- 策略经验库 v1 **不开放**外部读写（全局库跨租户，隔离论证复杂，缓一期）。

### 7.2 新增 tool（维持"宽查 + 精确取"惯例）

| tool | scope | 说明 |
| --- | --- | --- |
| `get_notebook_profile` | `agent_profile:read` | 返回共享底座 + **该 token 持有者自己的**覆盖层（脱敏截断走既有 `_budget_response`）。code agent 一进来就能拿到"这个库长什么样、怎么查有效"——对 code agent 场景价值最直接 |
| `add_observation` | `agent_observation:write` | 追加一条原子观察（"我用 X 查到了 Y"/"Z 手册里没有 W"），落观察队列表，**立即返回**（写入异步惯例）；观察**只进 token 持有者自己的覆盖层巡固**——v1 外部 agent 对共享底座零写入路径，投毒爆炸半径被限制在投毒者自己的覆盖层。幂等靠 `client_request_id`（照抄 `propose_memory`） |
| `search_agent_memory` / `get_memory` | 既有 | 不变；内容性知识仍走 `propose_memory` → 人审 |

### 7.3 安全边界

- **作用域由 token 绑定**，模型不填 notebook_id 之外的自由作用域（现状已如此，维持）。
- 观察条目带 `agent_profile_id` 出处，界面可按来源清空某个 agent 的全部观察（带已确认作用域的批量删除惯例）。
- 观察是**不可信输入**：巡固 prompt 对观察段沿用 system 级不可信证据指令（knowhow `llm_complete` 先例）。且观察只进持有者自己的覆盖层巡固（见 §7.2）——外部投毒即使成功，污染面也只是投毒者自己的私有块，共享底座结构上不可达。
- 首次接入 SOP：`docs/agent-mcp-memory-sop.md` / `_zh.md` 成对更新（文档同步红线）。

---

## 8. 数据库与 shadow 成本

- 新表（暂定）：`agent_notebook_profile`（块）、`agent_profile_changes`（历史，或并入块表 JSON 历史——定稿时决）、`agent_observations`（观察队列）、`retrieval_experiences`（策略经验，P2 才建）。
- 迁移：SQLite `_migration_48` 起 + bump `SCHEMA_VERSION`；PG `0026_*.sql` 起。**每张新表都要过正向 shadow 的 UNIQUE 停车方案设计**（v25 catalog 100 个 unique surface 均有静态停车方案的契约延续）——设计要点：新表尽量保持**叶表**（学 `catalog_candidates` 刻意不给 job_id 加外键的先例），唯一约束优先用 nullable 列或可停车的确定性候选形状。这是容易低估的工作量，P1 排期要计入。
- 深拷贝/transfer：notebook 深拷贝是否复制画像块须显式决定并写进契约（参照 `unified_kg_state` 不复制、`chunk_elements` 联动的登记先例）；v1 建议**不复制**（副本从零巡固），登记为刻意行为。

---

## 9. 与既有红线的关系

| 红线 | 本方案的处理 |
| --- | --- |
| 外部 agent 写入必须人审 | 内容性知识（层 C 通道里的 `propose_memory`）不变；新通道（观察/画像/经验）**不进答案正文、不可被引用**，属消费面更窄的新类别——需用户确认此扩展（§12-Q1） |
| 用户勾选是检索范围唯一来源 | 层 B 明确禁止影响 scope；画像/经验只影响查法与措辞 |
| 数值上限只登记在 `docs/product-and-api*.md` | 块数/块预算/注入字符/经验条数上限/巡固阈值默认值全部只登记在那里 |
| 效率一等约束 | 注入零新增模型调用（纯读+拼接）；巡固是阈值触发的一次有界调用；观察写入零模型调用 |
| 观测脱敏 | 巡固/注入事件只带计数/耗时/不透明 id，不带正文 |
| 全栈对等 | 画像面板、Profile 设置、观察管理界面与后端同批交付 |
| 文档同步 | README×2 / AGENTS.md / CLAUDE.md / product-and-api×2 / agent-mcp-memory-sop×2 同批 |
| 界面词汇 | 全部界面文案过词汇守卫，不出现 profile/consolidation 等黑话 |

---

## 10. 分期

| 期 | 内容 | 交付判据 |
| --- | --- | --- |
| **P1** | 层 A 最小闭环：`agent_notebook_profile` 表 + 阈值巡固 job + 规划注入（Ask+报告共用）+ 界面面板（看/改/清空/手动重建）+ 开关 | 同一 notebook 连续多轮 Ask，第二轮起规划上下文带画像；关闸后与现状逐字一致 |
| **P2** | 层 B-经验：trace 蒸馏 IF-THEN（全局+加权）+ 任务级注入 | 失败查法可见地不再重复（有 A/B 判据再细化） |
| **P3** | 层 C：MCP 两个新 tool + 观察队列 + SOP 文档；层 B-Profile：用户设置页 + 归纳 | code agent 经 MCP 拿到画像并能追加观察 |
| **P4** | reflect 循环内 `consult_memory` action（`allow_*` 策略位 + 档位预算账目）；步级经验注入 | 仅 deep 以上档启用，trace 有独立步 |

P1 独立成立且收益/成本比最高；P2–P4 各自可独立砍掉不影响前序。

---

## 11. 风险登记

1. **画像内容漂移/污染**：巡固输出错误理解 → 后续每轮被误导。缓解：证据 source_id 强制、用户可见可编辑可清空、fail-open 保旧块、变更历史可回滚。
2. **共享库隐私**：已由方案丙结构性消除（§12-Q2）——共享底座的取数 SQL 不触任何使用数据，覆盖层按 `user_id` 谓词私有。残余风险只剩实现走样（如覆盖层查询漏写谓词），须由守卫测试钉住"底座巡固输入零 ask 表访问"与"覆盖层谓词在 SQL 内"。
2b. **覆盖层收益隔离**：A 踩过的坑帮不到 B（隐私换效果的直接代价）。回收通道是 P2：覆盖层中已参数化、不含话题的纯策略条目晋升进全局经验库。
3. **策略经验全局库跨租户**：不同部署/不同用户群的经验混在全局库。v1 单机部署问题不大，SaaS 化前须重审。
4. **per-corpus 策略记忆无文献验证**：P2 效果可能不显著。缓解：P2 独立开关，蒸馏与注入解耦，效果不好可只留蒸馏观测不注入。
5. **shadow 停车方案工作量**：每张新表的 unique surface 都要设计，P1 估期计入。

---

## 12. 拍板记录与待定问题

**Q1（已拍板 2026-08-18）｜免人审通道**：接受"按消费面分信任级"——策略/画像类记忆 agent 自动写（不进答案正文、不可被引用、可清空），内容性知识维持人审。这是对"外部写入必须人审"决策的一次有边界扩展。

**Q2（已拍板 2026-08-18，方案丙）｜共享 notebook 的画像归属**：

展开分析后发现原来的甲/乙二选一掩盖了关键事实——四个画像块的原料归属不同：`corpus_shape`/`key_entities`/语料侧缺口只需要 notebook 级数据（成员本来就可见）；`retrieval_notes`/查询侧缺口本质是从**个人提问轨迹**蒸出来的。且"排除问题正文"这条软约束不够：**子查询是从问题派生的**（保留查询词等于没排除）；**零命中缺口天然携带话题**（"本库查不到薪酬资料"即宣告有人查过薪酬）；查法笔记聚合起来是提问风格侧写。

- 方案甲（共享 + prompt 约束"只描述语料"）：把隔离做在 prompt 层，正是"隔离必须是取数 SQL 谓词、不能是结果侧/prompt 侧补救"红线否定的形态。**弃**。
- 方案乙（整体 per-user）：把不需要隔离的语料侧块也隔离了，多人库重复付费、效果稀释。**弃**。
- **方案丙（采纳）：按数据来源拆块——共享底座 + 私有覆盖层**。底座（`corpus_shape`/`key_entities`/`corpus_gaps`）notebook 级一份，巡固输入只取 notebook 级数据、SQL 不 join ask/trace/Memory，结构上不可能泄漏；覆盖层（`retrieval_notes`/`usage_gaps`）user × notebook 级，巡固输入按 `WHERE user_id = ?` 谓词只取本人 trace，产物只有本人可见，查询词因此可以放心进巡固输入。注入 = 底座 + 提问者自己的覆盖层。仓库先例双重支撑：Memory 私有/Knowhow 共享双口径并存、`notebook_object_schemas` 的基线+覆盖行。单人库退化为一份完整画像，无感知差异。登记代价：A 的经验帮不到 B，回收通道是 P2 全局策略库（参数化条目不含话题，归属问题消解）。
- 落地细节已并入 §5.1（存储/边角语义）、§5.3（两条巡固链路）、§7（MCP 只读底座+自己的覆盖层、观察只进覆盖层）。

**Q3（已拍板 2026-08-18）｜策略经验作用域**：全局库 + notebook 特征加权（per-corpus 硬分区无文献验证且冷启动全空；属 P2，此处定方向）。

**Q4（已拍板 2026-08-18）｜Profile 与档位控件**：v1 不碰任何控件，只影响答案组织措辞——碰档位默认值与「自动模式强制默认值」红线过近，收益不值。

**Q5（已拍板 2026-08-18）｜报告注入面**：v1 只随 `ReasoningRetriever` 注入逐节检索（零额外接线）；意图理解/大纲规划阶段的注入留 P2 观察效果后再议。

**Q6（已拍板 2026-08-18）｜命名**：底座部分「AI 对这个库的理解」、覆盖层部分「我的检索心得」；实现时过 `scripts/check_ui_vocabulary.py` 词汇守卫，如被拦按守卫口径微调。

---

## 13. 参考

- Mem0 (arXiv 2504.19413)、Zep (2501.13956)、A-MEM (2502.12110)、Generative Agents (2304.03442)、Reflexion (2303.11366)、Voyager (2305.16291)、ExpeL (2308.10144)、AWM (2409.07429)、R²-Mem (2605.13486)、MERIT (2606.00547)、CoALA (2309.02427)
- Letta memory blocks / MemFS / sleep-time compute；Anthropic memory tool（`memory_20250818`）；LangMem conceptual guide；Mem0 MCP；Graphiti MCP；MCP 官方 memory server
