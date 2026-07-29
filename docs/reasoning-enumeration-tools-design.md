# 逐步推理集合枚举工具与综述大纲协同设计

状态：spec 定稿，分三个交付物推进（PR-1 止血 / PR-2 工具化枚举 / PR-3 综述大纲协同方向）。
本文是实现期的计划真源；数值契约合入时同步进 `docs/product-and-api*.md` 契约表（数值不上屏）。

## 0. 背景与设计决策

### 0.1 问题

「当前库里有哪些公式」这类**类型化集合枚举**问题在逐步推理模式下效果差。已定位的四层结构性根因：

1. **意图层**：`query_intent.py` 的 `_result_scope` 只认显式词（全部/所有/穷举…），模型自判的
   scope 被无条件覆盖；「哪些」不命中 → scope=ranked，完整枚举合同与披露文案全程不激活。
2. **检索层**：全仓库没有任何按 `element_type` / `object_type` 取数的检索能力；公式的 LaTeX 与
   「公式/equation」词法语义都天然失配，模型只能用关键词检索逼近（如 "equation OR 公式"，其中
   OR 只是噪声 token）。
3. **反思层**：模型对「是否已检索到全部」的断言无任何校验，原样进 trace 与终止判定。
4. **合成层**：`_answer_reasoning` 的 `elements[:6]` 硬切（不分档、按插入序）；trace 的
   「采用 N 段原文」统计于截断之前，系统性高估模型实际看到的证据。

### 0.2 决策哲学（用户拍板）

**模型负责决策，执行器负责事实。** 逐步推理是 agent 循环，枚举等重型检索能力做成模型可调用的
工具，由模型决定何时用、用多少；不再新增词法路由。确定性只保留三类**工具合同**：

- 工具输出有界（页大小、字符、每 run 预算）——成本物理；
- complete/partial 覆盖率由执行器按游标是否耗尽计算，结构化返回、前端徽章直读，不依赖模型转述
  ——覆盖率是事实不是观点；
- 权限与挂载作用域。

现有 Knowhow 显式词枚举快路径保持不动（保守、无歧义、已上线）。

## 1. PR-1 止血（独立小 PR，零新模型调用）

### 1.1 元素装配保真

- `backend/app/services/ask_service.py` `_answer_reasoning`：装配前把 `elements` 按检索相关度
  降序排序（`RetrievedElement` 现有分数字段为准，tie-break `element_id`），硬顶从裸 `[:6]` 改为
  新档位字段 `answer_element_items`。
- `backend/app/core/ask_retrieval_policy.py` `AskRetrievalLimits` 追加字段
  `answer_element_items: int`，加在 `chunk_context_chars` 之后（`overflow_semantics` 之前）；
  五档值 **4 / 6 / 8 / 12 / 16**；五个字面构造与阈值表注释同步。
- 语义不变式：元素仍在 chunk/source 分区内消费预算（`len(source_context) < chunk_budget`
  守卫保留），不突破 `chunk_context_chars`。

### 1.2 trace 诚实

- `backend/app/services/reasoning_retrieval.py` run() 末尾 answer 步 summary 由
  「合成: 采用 X 个KG候选 + Y 段原文」改为「合成候选: X 个KG / Y 段原文」（它统计的是候选池）。
- `_answer_reasoning` 回传实际进入 prompt 的计数（chunk/element/kg 各 id_map 大小）；
  synthesis 步（ask_service.py 构建处）detail 增加 `included_kg` / `included_chunks` /
  `included_elements`；summary 文案不变。

### 1.3 prompt 指令

- `answer_prompt` 追加规则 11：枚举/列举类问题必须把知识条目中每个不同的匹配条目逐条列出、
  各自挂 [k]，不得抽样合并；证据可能不覆盖全集时明确说明。
- `reflect_prompt` 在 aspect check 段后追加：reason 中不得声称「所有/全部 X 已检索到」——
  相关性检索无法证明完整性；改为陈述实际找到了什么。（PR-2 会把这句改写为「完整性陈述以
  enumerate 工具返回的 coverage 为准」。）

### 1.4 测试与文档

- 行为测试：构造 >cap 的 elements（相关度乱序），断言进入 element_context 的子集按相关度
  取 top-cap；synthesis detail 计数正确。prompt 内容测试各加一条断言，并做删除+移动两种
  变异验证。
- 文档：`docs/product-and-api*.md` 契约表加 `answer_element_items` 行（中英）；AGENTS.md 与
  CLAUDE.md 档位契约句同步。README 不动。

## 2. PR-2 工具化枚举（stacked 于 PR-1 分支）

### 2.1 存储与迁移

- SQLite：`_migration_37` 追加索引
  `CREATE INDEX IF NOT EXISTS idx_source_elements_source_type ON
  source_elements(source_id, element_type, created_at, id)`；`SCHEMA_VERSION` 36→37。
- PostgreSQL：新增打包迁移 `0015_source_element_type_index.sql`（同索引，风格对齐现有
  COLLATE "C" 约定）；`schema_manifest.py` bump；全仓核对 `migrate()`==N 类断言同步。
- 双后端 schema 测试同步。

### 2.2 集合地图（catalog）

- 可枚举元素 kind 白名单：`formula` / `table` / `image` / `code_block`（paragraph/heading/
  page_text 等不进枚举：语义无意义且体量大）。
- per-source 元素类型计数：`GROUP BY element_type`（限白名单），走新索引；进程内有界 LRU，
  key=(source_id, 该源变更信号)。变更信号优先 `sources` 表现有更新时间字段，否则用
  `idx_source_elements_source_created` 索引 seek 的 per-source MAX(created_at)。
- 作用域 = active notebook + 有效挂载 base（`mount_sql.py` 谓词，与 Ask 联邦同口径）；
  作用域计数 = Σ 各源缓存计数，源列表本身有界。
- KG 对象类型计数：复用 `notebook_catalog.py` 对同一个 `knowledge_type_count_rows` port 的调用，
  不新增查询路径；`collection_catalog.py` 另按 `kg_mutation_seq` 记一份自己的 L3 记忆化（而非
  直接复用 `notebook_catalog.py` 的缓存对象），因为该 port 调用只在 SQLite 侧由 store 记忆化、
  PostgreSQL 侧不记忆化——两侧都要在热路径上只付一次读取，就必须各自持有这层 seq-keyed memo。
  【落地后订正，T7】
- 地图文本 ≤600 字符，形如
  `[Collections in scope] elements: formula 12 (3 sources), table 5 … | KG objects:
  concept 1234, claim 567, formula 89, procedure 45 | knowhow tables: 2`；
  每 run 构建一次，注入 plan 上下文与每轮 reflect 的 candidates_summary 尾部（与
  NO_NEW_EVIDENCE_NOTE 同法）。构建失败 fail-open（无地图仍可答，记 skip 步）。

### 2.3 枚举执行器（零 LLM）

新模块 `backend/app/services/collection_enumeration.py`：

- `enumerate_elements(scope, kind, source_id=None)`：内部 keyset 游标
  (source 顺位, created_at, id)，页大小 `enum_page_size`；item 含 element_id / source_id /
  source_title / element_type / location_label / text（截 `cell_excerpt_chars`=1000）。
- `enumerate_kg_objects(scope, object_type)`：走现成 `(notebook_id, object_type,
  created_at, id)` 索引；item 含 object_id / name / section_path / 有界来源引用。
  **【codex 第 1 轮 P2-5 订正】** 页查询不带 status 谓词——该索引不含 `status`，
  写进 SQL 就是无界残余过滤，停用对象占比高的老库上「一页」不再是 O(limit)。
  改为读回原始行（含 `status`）后在服务层用同一份 `USABLE_STATUSES` 过滤，内部
  循环补页至凑满一页或触及 `max_rows × 4` 的原始行过扫描上限；`scanned` 计原始
  行，触顶发 `truncated_reason="budget"` 的诚实 partial，且游标越过已扫的不可用
  区段以保证续跑推进。刻意**不加**状态索引：那要在本 PR 里再叠一次 schema bump
  （已因 master 抢号顺延过一次），且 partial index 会把 status 集合冻进 schema。
- **一次动作在预算内自动翻页**：直到游标耗尽或触及 `enum_rows_per_run` / `enum_pages_per_run`
  / `structured_payload_chars`(256k 复用) 任一上限。**页预算只计同一源的第 2 页及之后**（真正
  的额外往返；源访问次数由行预算天然约束）。模型不感知 cursor；同一集合重复请求时，
  若上次因预算截断且本 run 预算有余则从内部续游标，否则 skip(already_enumerated)。
- **游标携带作用域身份**：cursor 必须带开场指纹（元素=作用域 (source,变更信号) 指纹；
  KG=参与 notebook 的 kg_mutation_seq 向量）与累计 returned；续跑先比对，作用域已变→
  `concurrent_change`，绝不静默从头重跑；分母校验在链末端按累计 returned 生效。合同不变式：
  `complete=false ⟹ cursor 非空`，唯一例外 `truncated_reason=concurrent_change`。显式
  `source_id` 请求直接对该源发索引分页查询，不得做「不在计划⇒零」推断。
- **【codex 第 3 轮 P1 订正】首解析窗口根治，而非披露**：`replace_elements` 在**同一
  写事务**里把 `sources.updated_at` 推到新元素所带的时刻（双后端同修），变更信号因此
  与元素换代原子翻转。此前登记的「首解析 under-count 自愈窗口」（元素已落库、
  `chunked_at` 本就是 NULL 所以清空是空操作、信号要等下一次 `set_status`）不复存在，
  文档与注释里所有把它写成「已登记的一致低报」的段落一并改写。副作用核实：
  `sources.updated_at` 只被变更信号查询读取一次，不参与排序/展示/其他缓存键，而且解析
  收尾本来就会写它——这只是提前到数据真正改变的那一刻。
- **【codex 第 3 轮 P2 订正】往返上界显式化**：每次动作维护页查询计数器，元素侧上界
  `max_rows + max_pages`（零计数源不进计划⇒不访问；进计划的源访问即产行⇒受行预算
  约束），KG 侧上界 `参与库数 + max_pages + 原始行过扫描上限`（该侧没有 per-分片计数
  可跳过，且状态过滤会产生补页）。越界抛 `EnumerationInvariantError`，调用方按普通
  执行器失败 fail-open 成一次 skip。**驳回**「首页也计费」：那会重新打破宽而薄语料
  （一百个源各一条公式）的 complete 可达性——本特性第 1 轮已经修过这个形态。
- **【codex 第 2 轮 P1 订正】收尾复检必须重解析参与库集合**：只对**开场那份参与
  notebook id 列表**重算指纹/seq，看不见「跨页期间挂载/卸载/失效了参考库」——空的
  新库不贡献来源信号，被卸载的库的信号也仍在，两种都会被判成稳定并报 complete。
  收尾改为经 `participant_ids`（与开场 `participant_tiers` 同一个
  `resolve_participants` / `mount_sql.py` 谓词入口）重新解析，集合不等即
  `scope_stable=False`；指纹/seq 复检也用**收尾解析出的集合**算。元素与 KG 两条
  路径同修。
- complete 判定：作用域游标耗尽 && 作用域指纹（源集合+变更信号 / kg_mutation_seq）首尾一致；
  否则 complete=false + `explicit_partial`（复用 `EXPLICIT_PARTIAL_OVERFLOW`）+
  truncated_reason（budget/payload/concurrent_change）。total 来自 2.2 缓存，取不到则省略。
- repository 端口：ports.py 新增有界查询方法 + 双后端 adapter + facade 一跳委托（架构守卫
  allowlist / 默认模式 rebaseline）。禁全表扫描：查询必须命中上述索引形状，测试仿
  `test_indexed_only_principle.py` 风格钉住。
- **地图/枚举覆盖一致性（T2 评审移交的硬约束）**：执行器枚举的物理源集合必须与集合地图
  计数的源集合逐字一致（含 Memory 派生合成源）；若未来要排除某类源，两侧必须同步排除并在
  coverage 行显式披露，否则会出现「地图报 12、枚举只给 8」的假部分。
- **kind 白名单单一真源**：执行器与 reflect 校验必须 import `collection_catalog` 的
  `ENUMERABLE_ELEMENT_KINDS` / `ENUMERABLE_KG_OBJECT_TYPES`，禁止再写字面量副本；T3 补一条
  源码级唯一性守卫（防「再抄一份」变异）。

### 2.4 reflect 动作接入

- `REFLECT_SCHEMA_HINT` / `allowed_actions` 增加 `enumerate_elements` / `enumerate_kg_objects`；
  schema 增分支对象 `enumerate: {"kind":"formula|table|image|code_block",
  "object_type":"concept|claim|formula|procedure","source_id":"","source_title":""}`。
  **【codex 第 1 轮 P2-4 订正】** 内部 source id 从不上屏也从不进候选摘要，模型
  只能按标题表达「列出《某某》里的公式」。`source_title` 由服务端在
  `scope_element_plan` 的源清单里做确定性解析（trim + casefold 精确匹配，按
  `source_display_rows` 窗口批量读标题，上限 1024 个源），唯一命中才用其 id；
  零命中或多命中记 `skip(enumeration_source_unresolved)`，trace 只报匹配个数与
  模型给的名字、不报内部 id。两个都给时 id 优先。
  **【codex 第 2 轮 P2 订正】** 计划长度超过 `_MAX_TITLE_RESOLVE_SOURCES` 时不得
  再从前缀断言唯一（同名的第二个源可能就在上限之后），直接返回
  `("", 0, truncated=True)`、不扫描；调用方沿用同一条
  `enumeration_source_unresolved` skip，detail 带 `truncated=true` 供排查。
- `reflect_prompt` 增加动作说明：问题要求列出/盘点某类条目时优先于 search_elements；结合地图
  计数判断是否值得全量；大集合应改为「计数+样例+建议缩小范围」。完整性陈述以工具 coverage 为准。
- run() 新增 elif 分支（镜像 search_elements 形态）：无效 kind/object_type → fail-open skip
  （fail_closed 下抛错）；预算耗尽 → skip(enumeration_budget)；结果累积到独立 `enumerations`
  列表（不混入 elements/chunks），并计入 no_progress/stale 账目；每页间 raise_if_cancelled。
- trace：`enumerate` 步（前端 chip 已存在），summary 形如「枚举公式清单: 累计 X 条/共 Y」，
  detail 含 collection/kind/returned/returned_total/scanned/total/complete/truncated_reason/
  has_more——**刻意不用 Knowhow enumerate 步的 `scanned_rows`/`known_total_rows` 键**（那是
  表行口径，复用会渲染成「12/0 行」）；前端按新键名单独适配（T6）。集合标签两张映射（元素/
  知识对象）全域不重名。
- 页预算计费：执行器在结果对象（非用户面 coverage）回传 `extra_pages`（同源第 2 页起的真实
  额外往返数），run 级按它精确扣减——不做 `scanned // page_size` 上界折算（在
  rows==size×pages 恒等式下上界折算使页池要么形同虚设、要么在载荷截断时错误提前截断）。
- **【codex 第 1 轮 P2-3 订正】载荷预算同样是 run 级**：执行器在结果对象上另回传
  `payload_chars`（本次真实消耗，与 `extra_pages` 同款「不进 coverage」的成本记账），
  run 维护 `enum_payload_used`，每次动作只发 `structured_payload_chars − 已用`；
  余量 <1 时按 `skip(enumeration_budget)` 跳过。否则一轮里的第 N 次 enumerate 会拿到
  全新满额，累计返回数倍于文档写明的 256k 请求级上限。三个池同为 run 级的推论：
  任一池触顶都会当场见底，跨动作续跑因此只在执行器停在池未耗尽的位置时才发生。
- 新档位字段（`AskRetrievalLimits`）：`enum_page_size`=50（各档相同，transport 批量而非答案
  top-N，口径同 structured_page_size）；`enum_pages_per_run` 2/4/6/8/12；`enum_rows_per_run`
  100/200/300/400/600。
- kill switch：`REASONING_ENUM_TOOLS_ENABLED`（config.py，注意 pydantic-settings v2 需
  validation_alias），默认 true；false 时不注册动作、不注入地图（回到现状）。
- **【codex 第 1 轮 P1-1 订正】无图笔记本必须够得到枚举工具**：ask_service 的
  「本笔记本尚未构建知识图谱」早退跑在 `ReasoningRetriever` 之前，会把「解析了来源但
  没建图」的库（自动 KG 抽取默认关，这是常态）整个挡在工具外面。早退条件收窄为
  「无图 **且** 集合地图上没有任何非零集合」；放行后照常进循环（图为空 ⇒ 初检索/expand
  自然返回空，地图 + enumerate + search_elements 照常工作）。`kg_required` 语义不变
  （无图且无可用 base 时为 True，前端提示继续显示），只是不再阻断执行。接线判据抽成
  `reasoning_retrieval.enumeration_wiring_active()` 供两处共用——各写一份的话，kill
  switch 一关就会放进一轮什么工具都没有的空循环。

### 2.5 合成与响应契约

- `enumeration_prompt_block`（仿 `structured_prompt_block`）：进入 source 分区最前，预览行数
  按 `inline_answer_rows`=100 口径 + 字符预算截断；块头写明 coverage（如
  `[Enumeration coverage: formula, returned 12/12, complete]`）。
- `AskResponse.result_sets` 泛化为 kind 判别 union：现有 `StructuredKnowhowResult
  (kind="knowhow")` + 新 `TypedCollectionResult(kind="collection")`
  {collection:"elements"|"kg_objects", element_kind/object_type, source_id, items,
  coverage: **TypedCollectionCoverage**（returned_total + Optional total（None=分母未知，
  禁止渲染 /0）+ complete + truncated_reason + overflow_semantics），并携带
  `synthesis_rows`/`synthesis_complete`（进入合成预览的行数——「枚举完整、分析部分」两轨
  分开披露，对齐 Knowhow batch 既有语义）。旧持久化数据（kind="knowhow"）解析兼容，
  未知 kind 有意 fail-loud；补反序列化测试。
- 枚举块预算：上限 `chunk_context_chars // 2`（为 chunks/elements 保底），prompt 条目行
  text 截 200 字符（卡片/transport 仍用执行器 1000 字符摘录），块头预留优先保活，条目行
  单行化且不得出现可与引用正则冲突的 `[数字]` 形状。
- `completeness_unavailable` 免责文案更新：**【codex 第 1 轮 P1-2 收紧后的最终规则】**
  四条同时成立才不前置——① `result_scope != "aggregate"`；② 意图合同的 `constraints` /
  `excluded_topics` / `assumptions` 全为空；③ 至少一条清单结果 `returned_total > 0`；
  ④ 且该条 `complete == True`。aggregate（计数/去重）、带谓词的请求、空清单与「只有
  部分清单」的场景一律保留警告。②的理由：清单卡的 coverage 只证明某个物理集合被完整
  走了一遍，证明不了它就是用户要的那个子集——模型完全可能枚举了无关 kind、或把带条件
  的请求做成不过滤的全集；这里刻意**不做**语义匹配（无确定性判据），方向定为宁可多
  警告，`assumptions`（前提）因此也算谓词。三处文案（长版+两处早退短版）统一提及
  元素/知识对象清单能力。

### 2.6 前端

- `workspace-model.ts` 增类型；`answer-panel.tsx` 增清单结果卡：按来源分组；formula →
  `FormulaView`(KaTeX)、table → **text 摘录 + 跳转来源查看完整表格**（table_html 无界且截断
  即碎，不进传输 item）、image → AuthedImage（item 携带有界 asset_id）、code_block → 代码块；
  KG 对象用 kg-type-mark 现有标签。完整/部分徽章与
  explicit_partial 提示复用 Knowhow 卡样式；初始 20 行 + 客户端翻页。
- 面向用户文案：元素侧「公式 / 表格 / 图片 / 代码块清单」，知识对象侧「概念 / 论断 /
  公式 / 过程知识对象清单」（与后端 trace 标签同口径，两张映射全域不重名）；
  「已全部列出 / 部分结果」；truncated_reason 经中文映射上屏（不吐内部 token）——过词汇守卫。
- 跨库条目收口（v1）：挂载参考库的条目显示「来自参考库《名》」标注，不渲染「查看来源」
  跳转、图片降级占位——挂载不等于该库直接成员权限（红线），来源/资产端点是 owner∪member
  口径；participant 集内的后端代理读取（与图谱引用定位红线同构）登记为独立后续任务。
- 图片条目可见性触发加载（IntersectionObserver，测试环境回退立即加载）——防止大清单展开
  产生数百并发鉴权资产请求。
- enumerate trace 步 detail 展示适配（scanned_rows 分支已在 reasoning-trace.ts）。

### 2.7 守卫、文档与评测

- 架构守卫默认模式重生成（AskResponse 变化）+ facade allowlist。
- 文档：docs/product-and-api*.md 契约表新增 enum_* 与工具说明；`.env.example` 与
  docs/deployment-and-configuration*.md 登记 `REASONING_ENUM_TOOLS_ENABLED`（对照
  KNOWHOW_KG_NODE_RETRIEVAL_ENABLED 先例三处齐改）；AGENTS.md「Architecture
  Baseline」与 CLAUDE.md 红线补「集合枚举工具」条目——并修订两处现有红线句
  「当前只有 Knowhow 支持完整枚举，其他对象集合仍是相关性结果」（PR-2 落地后不再成立，
  fangan 同款表述在 fangan_done.md 记账）；architecture.md 补 collection_catalog /
  collection_enumeration 运行时组件与全部新端口（T2 两个 + T3 三个）；README 中英一句
  能力入口。
- 测试：scripted fake chat client 驱动 reflect 返回 enumerate 动作，断言自动翻页/预算/
  complete/partial/取消/双后端 parity；离线测试钉「工具可达性与合同」，模型是否选用属真机评测
  （部署机手动对照）。
- 清单标签跨语言 parity 守卫：新增契约脚本严格消费前端两张标签表与后端两张映射，断言
  「前端值 == 后端值 + 清单」且两表值并集全域唯一，接进 contracts 泳道（改名+移动双变异
  验证）。

## 3. 综述类问题：大纲协同方向（PR-3，借鉴 DualGraph，arXiv:2602.13830）

DualGraph（Microsoft，OEDR）的核心：把**大纲图 OG（怎么写）**与**知识图 KG（知道什么）**分离
并逐轮共演化——大纲提供叙事结构与「无引用节=未探索方面」信号；KG 拓扑（弱支撑边 Enrich、
缺失关系/跨社区结构洞 Explore）生成定向查询；引用在大纲重构中持久保留；终止由大纲多维评分
判定；最终**按节合成**（每节只喂该节绑定证据）避免 lost-in-the-middle。消融显示 KG 驱动的
缺口发现主要提升 comprehensiveness/insight 与有效引用数。

对位本仓库：**KG 侧资产已存在且更强**（持久 KG、canonical fold、社区、PPR、follow_chain、
expand_community），DualGraph 是临时从网页搭图；缺的是 OG 侧与耦合：

- **逐步推理（综述形问题）v1 —— 大纲便签（outline scratchpad）**：
  - 新 reflect 动作 `update_outline`：模型维护一个有界大纲（≤12 节、两层），每节短标题 +
    绑定的已检索证据 key（服务端校验 key 合法性，非法即丢，口径同 knowhow 补全的证据 key
    校验）。
  - 循环账目回喂：「无绑定证据的节: […]」（与 attempted-queries 回喂同法）——这是 OG→查询
    的缺口信号，模型据此定向补子查询；KG→查询的缺口信号复用现有 expand_community /
    follow_chain / PPR，不实现 SBM/结构洞分析（成本高、增量存疑，显式不做）。
  - **按节合成**：存在大纲且证据量大时逐节合成再拼接（每节只喂该节绑定证据）；k 次合成调用
    是真实成本，按档位门控（deep 及以上才允许，overview/standard 保持单次合成）。
  - 何时建大纲：模型自决（哲学一致），reflect prompt 说明适用面（综述/盘点/多主体对比）。
- **深度报告模式**：报告已有大纲+逐节深挖+真实锚点 grounded 重算；可借鉴的增量 =
  轮间大纲**增补式**细化（只允许在已确认必答主题之下加子节，不得替换/收窄确认合同——与现有
  确认门约束一致）+ 每节检索引入 KG 缺口信号。作为独立评估项，不与 v1 绑定。

PR-3 在 PR-2 合入后单独立项（先真机验证 PR-1/2 对枚举类的收益，再决定 v1 范围）。

## 4. 任务拆分与模型分配

| 任务 | 内容 | 角色/模型 |
| --- | --- | --- |
| T0 | PR-1 全部（1.1–1.4） | impl-task (sonnet) |
| T1 | 2.1 迁移与索引（双后端+断言同步） | impl-task (sonnet) |
| T2 | 2.2 集合地图与计数缓存 | general-purpose (opus) |
| T3 | 2.3 枚举执行器+端口+parity | general-purpose (opus) |
| T4 | 2.4 reflect 接入+预算+prompt | general-purpose (opus) |
| T5 | 2.5 响应契约+合成集成 | impl-task (sonnet) |
| T6 | 2.6 前端清单卡 | impl-task (sonnet) |
| T7 | 2.7 文档+守卫+评测收尾 | impl-task (sonnet) |

每任务完成后依次跑 spec-review 与 code-quality-review（仓库钉 opus），再推进下一任务；
全部完成后 `bash scripts/check.sh` 全绿 → 提 PR → codex 评审闭环。

## 5. 风险与回滚

- 模型不调用工具：地图注入 + prompt 引导压低概率；kill switch 一键回退；真机措辞评测盯回归。
- 挂载大库首触计数：per-source 缓存分摊，构建 fail-open。
- 旧持久化 AskResponse 兼容：union 判别默认值 + 反序列化测试。
- 枚举覆盖缺口披露：元素层只覆盖结构化解析（MinerU）来源。**v1 刻意不做**结果卡的
  「纯文本解析来源数」黄色档披露（需要按来源解析器口径的有界探针，登记为独立后续任务）；
  v1 的诚实边界=coverage 只声明「已存储元素」的完整性，产品文档（T7）明写元素清单的
  解析器覆盖范围。KG 对象清单以「知识对象」限定词表达「已抽取」语义。
