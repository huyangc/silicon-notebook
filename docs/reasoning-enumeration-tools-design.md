# 逐步推理集合枚举工具与综述大纲协同设计

状态：spec 定稿，分三个交付物推进（PR-1 止血 / PR-2 工具化枚举 / PR-3 综述大纲协同方向）。
本文是实现期的计划真源；数值契约合入时同步进 `docs/product-and-api*.md` 契约表（数值不上屏）。
PR-1/PR-2/PR-2.5 已合入 master。PR-3：O1（大纲便签动作+校验+账目回喂+门控+trace）与
O2（按节合成+集成）已实现（分支 `claude/outline-coevolution`）；O3（前端标签+文档四份+
契约表+守卫收尾）已完成，待 O4（`check.sh` + 提 PR + codex 闭环）收尾。

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
- 文档：`docs/product-and-api*.md` 契约表加 `answer_element_items` 行（中英），并在同一
  权威文档中同步检索档位合同；入口 README / `AGENTS.md` / `CLAUDE.md` 不承载该细节。

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
  计数的源集合逐字一致；若要排除某类源，两侧必须同步排除，否则会出现「地图报 12、
  枚举只给 8」的假部分。
- **【codex 第 4 轮 P1 订正】私有 Memory 两侧同谓词排除**：确认 Memory 是 owner 私有的
  （Ask 只经按 owner 隔离的记忆检索通道读它，Knowhow 补全按合同排除它），而集合清单按
  participant 作用域取数、自身没有 owner 过滤——共享笔记本里任何成员都能把别人的确认
  记忆按公式/表格/图片/代码块、以及从该合成源抽出的知识对象逐条读出来。修法是
  **无条件排除**（单人库同口径，一份清单只有一个含义），且**计数与行两侧同一谓词**：
  * 元素侧在 `source_change_signal_rows` 里排除 `source_type='memory'`。那条查询同时
    是计数、计划与收尾指纹的唯一来源，排掉即三处一致；三个信号列都不在任何索引上、
    行本来就要访问，所以谓词是纯残余过滤、不改访问路径。
  * KG 侧不能进 SQL——`knowledge_objects` 不带来源类型，`NOT EXISTS` 又是第二条无索引
    残余过滤（与 status 同形态）。改为每个参与库一次有界 `memory_source_ids` 读取，行
    过滤与 `USABLE_STATUSES` 并列放在同一段有界过扫描里；分母同步减去这些源名下的
    可用对象数（`knowledge_type_count_rows_for_sources`，按 id 传入，避免第二处
    「谁是 Memory」的拼写）。只做行过滤不减分母是最坏形态：returned 与 total 对不上，
    一张本该完整的清单被判成永久 `concurrent_change`。
  * 显式 `source_id` 那条刻意绕开计划的路径必须自己再挡一次（一次有界查询，仅该路径付）。
  * 看板计数（`notebook_catalog` 的 `knowledge_type_count_rows`）**不改**：它回答的是
    「这个库里有多少知识」，把 Memory 派生对象算进去是对的。两处口径分叉是刻意的。
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
- **【codex 第 6 轮 P2 订正】载荷闸必须落在 wire 形状上**：执行器的 run 级池量的是
  紧凑 dataclass，而真正下发/持久化的是 `TypedCollectionItem` —— 那是个两臂联合体，
  元素行仍带 `name`/`section_path`/`evidence_element_ids`、知识对象行仍带
  `source_title`/`location_label`/`text`/`asset_id`，全在默认值上，外加每份 result
  自己的元数据与 coverage。所以 exhaustive 档贴着执行器上限跑完的一轮，下发的
  JSON 能明显越轨。`typed_collection_results` 因此接收 `payload_chars`，逐 item 按
  `model_dump_json()` 长度累计（与响应序列化逐字符等长，有测试钉住），并**先预留**
  每份 result 的信封（元数据 + coverage）：信封正是「这份被裁过」的披露载体，为省
  几百字符把它丢掉等于把披露一起丢掉。裁到的那一份诚实降级为 `complete=False` /
  `truncated_reason="payload"` / `returned_total=实际送达条数`——coverage 描述的是
  用户手里那份清单，执行器那个更大的数留在 trace 里当成本账。分工：执行器闸拦的是
  「读得比该请求允许产出的还多」，wire 闸拦的是「传/存得比声明的还多」。
  合成预览必须按**送达的那份**渲染（`delivered_outcomes` 派生视图，不重算），否则
  prompt 里会出现结果卡没有的行、块头还写着 complete——prompt 与卡片对同一份清单
  说两套话，正是 coverage 合同要防的东西。
- **【codex 第 6 轮 P2 订正】预览额度跨清单分配**：`inline_rows` 是 run 级共享额度，
  先到先得会让第一份清单（真实枚举通常一上来就够 100 条）吃光，后面每个集合
  `previewed 0`，混合/多清单问题于是只按第一张卡作答——恰好违反这个共享额度当初
  要防的饿死。改两遍分配：第一遍每份保底 `max(1, inline_rows // n)`（按自身条数与
  剩余额度夹住，集合比额度还多时按序发完为止），第二遍把余量按原顺序贪心分完。
  单清单口径逐字不变。字符预算装不下的行不再回捐——那时块已经是字符受限，下一份
  同样花不掉。
- **【codex 第 4 轮 P2 订正】地图计数必须进合成上下文**：reflect prompt 明确教模型
  「集合远大于本轮清单额度时别翻页、直接用地图计数作答」，而答案合成是另一次调用，
  只拿到检索证据与清单预览，从来看不到地图——那是在要求它报一个它拿不到的数；且
  「大集合 + 零其它证据」这一路连合成触发条件都不满足，用户拿到空答案。修法：
  `ReasoningResult` 带出本 run 已建好的 `collection_map_text`（不重建，地图有 memo），
  `collection_enumeration_answer.collection_map_block()` 包一层固定表头（服务端确定性
  输出、可无 `[k]` 引用、数的是「存在多少」而非「检索到多少」），整块硬上界 = 表头
  长度 + 地图上限 600，装配在 source 分区**最前**（预算吃紧时第一个被牺牲的不该是
  它），并把它加进 `_answer_reasoning` 的合成触发条件。地图为空（工具关闭 / 建图失败）
  时注入零字节，行为逐字回到接入前。
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
  单行化且不得出现可与引用正则冲突的 `[数字]` 形状。进入预览的条目使用隔离的
  `k5001+` id 和反向证据映射；模型用到哪一行就引用哪一个 `[k]`，只有实际绑定的清单
  锚点可为答案归因。只有带存活 `source_id`/`element_id` 的绑定键才能通过单独的
  exact-evidence 合同判 grounded，且按实际引用 key 而非 object id 匹配（同一对象的普通
  `k1` 命中不能借 `k5001` 绕过相关度阈值）；枚举答案无锚点时清空无关
  ranked-citation 回退，避免伪归因。
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
- 每个送达的来源元素/KG 对象条目至多携带一条仍存活的有界原文 `Citation`；KG 对象从
  已封顶的 evidence element id 中取首个有效元素。进入答案合成预览的条目使用隔离的
  `k5001+` 引用键与反向映射；只有模型实际绑定且带存活 `source_id`/`element_id` 的清单
  锚点可确定性判 grounded。一个锚点都没绑的枚举答案不展示无关 ranked-citation 兜底，
  历史 KG 出处全部失效时明确显示「暂无可用原文出处」。
- 跨库条目（v1 收口 → 已由后续任务补齐）：挂载参考库的条目保留「来自参考库《名》」标注；
  「查看来源」跳转与图片**已恢复**，走 participant 集内的后端代理读取
  （`GET /notebooks/{active}/sources/{id}[/elements]` 与既有资产端点的同口径放宽），与图谱
  引用定位红线同构——浏览器始终只用当前 active notebook 过权限，后端在其有效参与集内解析
  并内部代理；挂载仍不等于该库直接成员权限，裸 `/sources/{id}` 保持 owner∪member 口径，
  写入（重新解析/删除）刻意不代理，来源详情弹窗对参考库来源按只读渲染。
- 图片条目可见性触发加载（IntersectionObserver，测试环境回退立即加载）——防止大清单展开
  产生数百并发鉴权资产请求。
- enumerate trace 步 detail 展示适配（scanned_rows 分支已在 reasoning-trace.ts）。

### 2.7 守卫、文档与评测

- 架构守卫默认模式重生成（AskResponse 变化）+ facade allowlist。
- 文档：docs/product-and-api*.md 契约表新增 enum_* 与工具说明；`.env.example` 与
  docs/deployment-and-configuration*.md 登记 `REASONING_ENUM_TOOLS_ENABLED`（对照
  KNOWHOW_KG_NODE_RETRIEVAL_ENABLED 先例三处齐改）；在产品/API 权威文档中修订
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

**2026-07-30 立项（用户拍板）**：PR-1/2/2.5 已合入且真机验收通过（公式题根治；文章题机制
全对但深度受采样限制）。用户决定不做深度补丁（全文直读/摘要升级等暂缓），直接做大纲协同——
「根据 notebook 的内容逐渐完善结果」，服务综述/深度分析类问题；**v1 门控钉在 `exhaustive`
（穷尽）档**（用户原话「把档位选到穷尽的时候」；thorough 及以下完全不出现，回到现状；是否
下放 thorough 由真机评估决定）。

### 3.1 v1 实现合同 —— 大纲便签（outline scratchpad）

- **新 reflect 动作 `update_outline`**（第 11 个动作 id；仅 exhaustive 档且
  `REASONING_OUTLINE_ENABLED`（默认 true，config 注意 validation_alias）开启时进
  prompt/schema/allowed_actions——条件化机制镜像 enumeration 的单闸模式，关闭态/低档位
  与现状逐字一致，冻结基线复用既有守卫形态）。
- **大纲状态（run 局部）**：有界——≤12 节、两层（节可带 parent）、标题 ≤60 字符、每节绑定
  证据 key ≤8 个；`update_outline` 动作携带整份章节结构（结构全量替换，避免增量补丁的对账
  复杂度），但同一稳定节 id 的证据与旧绑定取并集，遗漏不删除，`remove_evidence` 才显式撤销。
  8 键满额时旧键优先，未接纳的新键进入下一轮账目供模型腾位后重试。每 run 该动作 ≤6 次，
  超出 skip(outline_budget)。
- **绑定键校验（服务端事实）**：合法键=本轮反思上下文已展示的候选标识（与 `_summarize`
  的展示形式同一套；含枚举清单条目 id）；非法键静默丢弃；无任何合法绑定的节保留为**空节**
  进账目。口径同 knowhow 补全的证据 key 校验先例。
- **账目回喂（OG→检索的缺口信号）**：每轮 reflect 上下文追加大纲便签行——各节标题+绑定数，
  空节单独点名（「无绑定证据的节: …」）；与 attempted/enumeration 账目同法。模型据此对
  空节发定向子查询。KG→检索的缺口信号**复用**现有 expand_community / follow_chain / PPR /
  exact_lookup；**显式不做** SBM/结构洞分析（成本高、增量存疑）。
- **按节合成**：循环结束时若大纲 ≥2 个非空节**且该 run 未产出集合清单/结构化整表枚举**
  （codex r7：清单块与结构化行不进节切片，「按来源列出全部公式」被节化会拿 ranked 样本写
  散文、丢掉手上已有的完整清单；清单 run 保持单次合成，清单预览/覆盖披露完整进入合成
  上下文，清单进节切片留给 v2 设计）：逐节合成——每节只装配**该节绑定证据**的
  上下文切片（复用既有 evidence_context 装配器，各节 key 基址偏移 `section_index × 10000`
  保证命名空间不相交），合并 id_map 与锚点，答案=各节 `## 标题` 拼接（Markdown 标题前端
  原生渲染）；空节跳过并记 trace。任一节合成失败→整体回退单次合成（fail-open，trace 记
  outline_synthesis_fallback）。k 次合成调用是真实成本——这正是只在 exhaustive 档开放的
  理由（用户显式选穷尽=接受成本）。
- **trace**：新 step_type `outline`（前端补「大纲」标签；NEXT_ACTION 补 `update_outline`
  →「整理大纲」）；每次动作 summary 形如「更新大纲: N 节(M 节待补证据)」；按节合成在
  synthesis 步 detail 记 `outline_sections`/`outline_fallback`。
- **响应契约 v1 刻意不加字段**：大纲经 trace 与答案结构（标题层级）可见，不进 AskResponse
  （避免 api_contract churn；真机验证后如需结果卡再立项）。
- **何时建大纲：模型自决**（哲学一致）——reflect 动作说明写明适用面（综述/盘点/多主体
  对比/「逐渐完善」类长问题）与不适用面（单点事实题别建）；集合地图与来源清单是天然的
  大纲种子材料（先枚举目录再按主题建节）。
- 深度报告模式**不在 v1**：报告的确认合同门槛（大纲增补式细化不得替换/收窄确认主题）留作
  独立评估项。

**落地决策（O1 实现期定案，与上面的意图一致，补足未定的部分）**

- **档位闸读的是 limits 自己的身份**：`AskRetrievalLimits` 增加首字段 `effort`（就是选中它的
  那个档位 id），`outline_wiring_active(settings, limits)` 判 `limits.effort == "exhaustive"`。
  不从预算数字反推——那样任何一次 `dataclasses.replace(limits, 某个预算=…)`（测试与未来的调用方
  都会这么干）都会静默改变「这是哪一档」的答案。没有 limits 的调用方（深度报告逐节深挖、knowhow
  补全）因此天然是关闭态。
- **账目回喂必须携带整份大纲，而不只是「标题 + 绑定数 + 空节点名」**：reflect 的 prompt **没有
  对话历史**（每轮都是一条全新的 user message），而章节结构是全量替换——模型手上唯一一份「我
  上次交了什么」就是这段回喂。证据不再依赖逐字转录：服务端按稳定节 id union，模型漏抄一个 key
  不会删除旧绑定；但结构仍须完整回喂，否则模型无法保留/调整章节。空节仍单独点名（缺口信号）。
  8 键 union 溢出时，账目另按节列出未接纳的新 key，并教学式提示用 `remove_evidence` 腾位。pending
  只是反馈状态，不会把普通额度尚存的下一份全量更新冻结成 repair-only；那份载荷仍可改结构，合法绑定
  全部照常合并。`sufficient` / stale 终态只有在 `max_steps` 仍有正常步骤余额时才最多补一轮专用纠错，
  第 6 次常规更新耗尽自身额度后也可提供一次资格；这些专用提交都只能保持 id/标题/parent 不变地换
  evidence，第 6 次后的首次提交即消费资格，终态纠错不能执行检索。`max_steps` 是绝对上限，末步溢出
  直接在收尾披露而不追加第 51/50 轮；stale 熔断先落 trace，再进入仍在预算内的纠错。整份账目仍由
  12 节、每节 8 个已绑定 key 加一次有界溢出列表夹住，与语料规模无关，且只在 exhaustive 档出现。
  未接纳 key 是持久 pending：成功绑定、在 `remove_evidence` 点名放弃或整节删除之前持续回喂；只腾位却
  漏抄新 key 不会把它静默清掉。结构违规只拒绝真正的纠错提交；普通额度轮不因 pending 继承同结构
  限制。完整状态每节硬顶 56 key（6 次常规 + 1 次纠错），prompt 每轮只展示前 8 个与剩余计数，处理后滚动露出下一批。
- **绑定键的合法集合按「本 run 曾实际展示」单调累积，而不是整个候选池**：`_summarize` 对候选池开
  头尾窗口，run 内维护 `ever_shown`。第 3 轮实际展示并绑定的证据即使第 5 轮滑入中段仍合法；从第一轮
  起就位于省略中段、从未渲染的 id 则不能靠猜中通过。合法集 = 当前候选池 ∩（ever_shown ∪ 当前大纲
  已合法持有的 key），既守住 persistence，也守住「合法集 ⊆ 模型可见或服务端已持有」。
  - **v1 不含枚举清单条目的 id**（评审订正，恢复「合法集 ⊆ 模型可见」这条不变式）：枚举账目按
    合同只回覆盖计数、不回条目 id（元素/知识对象清单一个字的正文都不带），模型根本拿不到那些 id
    ——放进合法集只是一片没人能踩到的死面，同时放宽了「只能引用服务端给过你的东西」这条校验。
    **O2 或后续改动让清单条目 id 对模型可见时，连同可见性一起把它们加回来**，并在这里记一笔。
  - **来源 id 同样刻意不在内**：一份文档不是一条证据，绑上它给 O2 的按节合成产不出任何上下文
    切片，而目录给模型的句柄本来就是标题而不是 id。
- **原文候选在大纲在场时才标 `(id=…)`**：KG 候选的 id 一直都在（`expand_graph`/`follow_chain` 拿它
  当参数），chunk/element 此前没有任何动作需要指名道姓。大纲把「这一节靠哪几条证据」变成了模型要
  表达的东西，而纯散文库（有文档、没图谱）里能绑的恰恰只有 chunk 与 element——不给 id，综述类问题
  的大纲只能全是空节。关闭态与低档位的候选摘要逐字节不变。
- **空提交是 skip，不是清空**：`update_outline` 带不出任何合法节（畸形、全无标题）时记
  `skip(outline_empty)` 并**保留**上一份大纲。一次畸形响应把已经建好的大纲抹掉是最坏的一种
  fail-open；代价是模型无法显式清空大纲（它可以用一节替换掉整份），这是刻意的取舍。
- **大纲修订对 stale 熔断中性**（评审订正；第一版把「大纲变了」算成进展，实测有洞）：大纲不带来
  任何新证据，而 stale 数的正是「还在不在往前推进」。算成进展的话，两份大纲 A、B 交替提交每轮都
  「有变化」、每轮都把 stale 清零，空转上限从 3 轮被抬到 19 轮。所以 `outline_*` 一律不进
  `no_progress` 账目：纯整理会照常推进 stale 并在 `reasoning_stale_limit` 轮内熔断，而正当流程里
  绑定轮本来就伴随检索动作（那些动作自己会重置 stale），中性对真实用法零影响。`changed` 仍留在
  trace detail 里当观测量。
- **`sufficient` 短路之前先应用当轮的 `update_outline`**（评审 R2 P2）：`update_outline` 与
  `sufficient=true` 出现在同一轮是**最常见的收尾形态**——reflect prompt 教模型「每个方面都有证据了
  才置 sufficient」，那正是它把最后一批绑定补上、交出定稿大纲的时刻。短路排在动作分发之前时这份
  载荷被静默丢弃：循环里不报任何错，按节合成却拿到上一轮那份还带空节的旧大纲，用户看到的是一篇
  没有结构的答案。修法取**「短路前先应用再 break」**而不是「只对检索类动作短路」：后者要么让循环
  在模型已说够了之后再多跑一轮反思（纯成本），要么还是得引入一个「做完这一步就停」的标志位，等于
  同一件事绕一圈。应用与分发链**共用 `apply_outline_update` 这一个函数**（预算/校验/trace 语义因此
  不可能分叉——额度耗尽时同样只记 `outline_budget` 并不改大纲），两处各写一份就是本仓库反复吃过亏
  的「两个各自正确的一半拼出一个错误的整体」。`next_action == "answer"` 那条路刻意不碰大纲：模型
  没提交载荷，也没说要改结构。
- **便签感知剩余额度**：`_outline_note(sections, updates_left)` 尾部带「还可以再整理 N 次」（照集合
  地图那条 allowance 行的先例）；额度耗尽后整段措辞换成「已定稿，不要再提交 update_outline，请改用
  检索动作补齐空节或直接作答」。账目必须描述**现在**能做什么——它若还在教「再次提交时要带上……」，
  模型就会照做、撞上 `outline_budget`、白烧一轮反思。
- **去重 id 的有界性是结构性的**（评审 P0）：`_unique_outline_id` 的后缀段是 `for range` 且拼后缀前
  先把 base 裁到 `_OUTLINE_ID_CHARS - len(后缀) - 1`，穷尽后落回位置 id（鸽笼原理保证有空位）。
  原来的 `while True` + `f"{base}-{n}"[:32]` 在两种真实输入下 100% CPU 死循环（33 字符且前 32 位
  相同的两个 slug；三个 31 字符的同名 id——后者 `[:32]` 恰好把数字截掉），而这个函数跑在 Ask 的
  worker 线程里：循环体内没有取消检查、`except Exception` 接不到、`/ask/cancel` 无人轮询，只能重启
  后端。
- **reflect 的 outline 参数按「有才传」**：`self.reflect(question, summary, **({"outline": True} if
  … else {}))`，与 `plan()` 的 `max_subqueries`/`collection_map`、`_construct_reasoning_retriever`
  对 `fail_closed` 的处理同一惯例。关闭态与低档位下调用形状与接入前逐字一致，既有的 reflect 测试
  替身不必为一个它们永远收不到的参数改签名。
- **代价登记（留给 O3）**：`update_outline` 是模型面的第 11 个动作 id，所以前端
  `reasoning-trace.ts` 的 `NEXT_ACTION` 与 `TRACE_STEP_LABELS` 各缺一个键（反思步会显示回退文案、
  `outline` 步没有短标签）。`reasoning-trace.test.mjs` 里那条「覆盖后端全部 10 个真实取值」的用例
  自带 case 表，**不会**因为后端多一个动作而变红——O3 要主动补，不能等它报警。

**按节合成的落地决策（O2 实现期定案）**

- **切片只装配「可绑定」的证据，其余块留在回退路径**：集合地图块、枚举清单预览、私有
  Memory、查询期推导链**不进**任何一节的上下文。它们都不在 `outline_binding_keys` 的合法集里
  ——模型根本没法把它们绑给某一节，硬塞进去只会让每节都重复看到同一份、还挤占该节真正
  绑定的证据。v1 的分节模式因此是严格的「绑定证据驱动写作」；这些块在单次合成（含回退）
  路径上一字未动。**已知残余（codex r8 登记，v1 如实说明不改）**：复合问题的某一面若由
  集合地图计数/私有 Memory/查询期推导链回答（「库里有几篇文档，并对比两大主题」），
  分节路径会丢那一面的可用证据。r7 已把权威完整清单（类型化枚举/结构化整表）划出分节；
  剩余三类没有确定性判据判「问题是否需要它」（语义匹配是本轨道刻意不做的），而地图块
  恒存在——按「存在即绕过」等于永远不分节。出路是让这三类可绑定（typed chain 绑定已在
  v2 登记项里），与穷尽档真机评估一起定。
- **触发条件是「解析后仍有 ≥2 节拿得出证据」**，不是「大纲里有 ≥2 个非空节」：绑定键在
  收尾解析时查不到东西的节与空节等价，都进 `outline_skipped`。解析后不足 2 节时**不算回退**
  ——那一轮压根没进过分节路径。但（codex r6 修订）大纲 detail 键集挂在「规划跑过」而不是
  「分节真的尝试过」上：绕过分节时 `outline_sections=0`、`outline_fallback=false`，
  `outline_skipped` 的「问到了没找到」披露**保留**——否则单节答案会显得完整。没有大纲/
  低档位/关闭态下规划不会跑，detail 仍与关闭态逐键一致。
- **每节的锚点按本节自己的 id_map 解析，再合并**（对 §3.1「锚点解析用合并后的 map」的收敛）：
  号段偏移已经保证任意两节不相交，所以**合法**标记在两种口径下结果逐位相同；差别只在一种
  情况——某节写出别节号段的 `[k]`。那只可能是幻觉（它从没见过那个号），按节解析直接丢弃，
  合并解析却会一本正经地把它绑到**另一节的证据**上。号段偏移买到的正是这个，不能在最后
  一步还回去。
  - **正文也必须在拼接前按本节号段清洗**（codex PR#407 R3 P1）：只让按节解析不发锚点是不够的
    ——前端 `buildAnswerReferences` 是拿**正文里的标记**去查**合并后**的引用表，第二节那个
    `[k1]` 照样会绑到第一节的无关证据上，误绑从渲染这道后门原样回来。隔离必须在送到读者眼前
    的那份文本上也成立。清洗**在解析之前**跑：`parse_anchors` 对混合组是整组失效的，先解析会
    让 `[k10001, k1]` 里合法的 k10001 也拿不到锚点，而正文却已写着 `[k10001]`，变成永远绑不上
    的标记。
  - 与 graph 模式 `_strip_unbound_markers` 的两处刻意分歧：①**混合组保留合法子集**而不是整组
    丢弃——那边未知键是凭空造的 id、判断不了它想引什么，这边号段互不相交是服务端造的事实，
    同组里的合法键是服务端确确实实发给这一节的证据，连着好的一半一起丢是拿一次幻觉惩罚一条
    真引用；②空白收敛带 `(?<=\S)` 前置断言——标记按规则 1 挂在句末，删掉只会留下跟在非空白
    字符后的空格串；不加断言会把长文里的嵌套列表与四空格代码块一起压平。
- **被 `top_n` 截断的绑定对象由检索侧带出，不回库 hydrate**：`top_hits` 是 `collected` 按相关度
  截顶的选集（穷尽档 cap 96），而绑定键的合法集是整个 `collected`，所以模型完全可以绑上一个
  最终被挤出选集的对象。`ReasoningResult.outline_evidence` 在 run 收尾时从 `collected` 里取出这
  一批（零查询）。**按 id 回库 hydrate 是一条走不到的分支**：三个候选池在 run 内只增不减，任何
  曾经合法的键在收尾时仍在池子里；为一条不可达路径在 Ask 合成上加一次数据库往返，与
  「效率是一等约束」相悖。这批对象在按节合成真的产出答案时并入 `classify_evidence` 的证据池
  ——否则模型引用服务端指名喂给它的证据，反而会被判成「引了空气」、整篇降级成 `inferred`。
  - **相关度必须与选集同口径**（评审 P2）：`collected[oid]` 存的是**首次收下它的那个产出方**
    给的分，而 `top_hits` 每条都过了对整个问题的重排。一个首收 0.95、重排 0.12 的对象带着 0.95
    进证据池，它一条就能把整篇抬到 grounded——而它恰恰是被截断掉的那种「其实不相关」。非 quota
    路径用同作用域现成的 `scored_map`（与 `top_hits` 同一个 map、同一句 `.get(oid, rk)`，零新
    查询）；quota 路径没有全局重排 map，把带出值夹到**选集的最低分**（选集为空 ⇒ 天花板 0.0）。
- **逐节 grounding + 既有三档确定性降级**（用户改判，推翻此前 `any(各节)` 裁定）：每一节只用
  自己的证据切片与锚点通过 `classify_evidence`。收尾 synthesis detail 的 `section_grounded` 是有界
  的逐节记录列表（每项含该节 `grounded` 布尔与 `evidence_level`），不是整篇布尔值；旁边另列无据节
  标题，给 v2 留下「哪一节从无据变有据」的测量信号，不新增 AskResponse 字段。整体继续复用
  `grounded`/`overview`/`inferred`：全部已合成节 grounded 时照旧走全局 `classify_evidence`；否则只
  **封顶** `overview`（全局若已是 inferred 不反向抬高）。零节达到精确 grounded 档但各节仍有高分
  引用、仅模型自报保守时，因此可保留 overview，不再被强制误写成 inferred。这避免裸 `all` 因一节
  正当常识衔接过度降级，也不再让一节有据把其余章节全部遮成 grounded。
- **回退成功不留假报警**（评审 P2）：分节阶段记下的 `model_error` 在回退单次合成**成功**后从响应
  里摘掉（`_err_sink` 的 `[分节起点, 回退起点)` 区间）。那次故障已被同一轮吸收、用户拿到了完整
  答案，再挂一条红色横幅是在报一个没有影响结果的错误。**事件日志不受影响**——`note_model_error`
  的两个副作用里只有响应那一份是给用户看的，`events.jsonl` 仍然记全。回退也失败时两个阶段的报警
  都保留。
- **每节写完发一条进度轨迹步**：复用 `synthesis` 这个 `step_type`，detail 带
  `section_index`/`section_total`/`section_title`（截 60）。分节合成是整轮最长的一段（k 次模型
  调用），不发进度实时轨迹会静止几分钟；前端现有 `synthesis` 分支只在 `detail.anchors` 是数字时
  给细节文案，进度步不带 `anchors`，所以不必等 O3 补渲染就是可读的。进度步**实时发出、不因后续
  回退而回收**（那一节确实写完了、那笔钱确实付了），但只在该节**真的完成后**才发——失败的那节
  没有「已写完」。
  - **进度步不带 `duration_ms`**（codex PR#407 R1 P2）：前端的轨迹总耗时是**所有**步 `duration_ms`
    的求和，而收尾那条总 `synthesis` 步已经独家记了从 `synthesis_started` 起的整段合成时间。进度步
    再各记一份，分节答案的合成耗时就报成约两倍（回退 run 还会把已完成节的那几次尝试也算进去）。
    进度步是**进度标记**，不是独立的耗时区间；「合成耗时必须进轨迹总耗时」那条红线由总步承担，
    恰好一次。
- **切片按文档序排列，孤儿子节晋升为 `##`**（codex PR#407 R1 P2）：`parse_outline_sections` 明确
  允许模型先写子节再写父节（两层裁剪刻意等收齐所有节之后才做，正是为了不把合法的前向引用误判成
  非法），所以提交序里 `### 子节` 完全可能排在它的 `## 父节` 前面——那是个层级倒挂的 Markdown
  文档结构。`plan_outline_sections` 因此把幸存的节排成文档序：顶层节按提交序，每个顶层节后紧跟它
  的全部子节（子节间保提交序）。父节是空节、被跳过时，子节的 `section.parent` 仍指着一个不会出现
  在答案里的 id，照它渲染就是一个没有父标题的孤儿 `###`；切片因此携带**解析后**的 `parent_id`
  （空 = 顶层），孤儿原地晋升。
  - **重排放在 plan 而不是只在渲染**（超出评审要求的那一点）：序号与号段偏移都按这里的顺序分配，
    于是节级 prompt 的「(2 of 5)」、进度步的「第 2/共 5 节」和答案里那一节的实际位置说的是同一件
    事。只在渲染时重排的话，这三处会各说各的——模型被告知在写第 2 节，读者却在第 4 个标题下读到它。
- **模式判定是显式布尔 `sectioned`，不看标题真值**：标题是内容，不是开关。靠真值判的话，一个
  绕过 `parse_outline_sections`（它保证标题非空）的构造会落进「号段偏移生效了、节级指令却没发」
  的混合态——prompt 看着正常，模型却按整篇答案的口径写每一节。`answer_prompt` 的四个新形参一并
  改成 keyword-only。
- **分节模式不做证据精炼**：`evidence_refine` 是**每次装配一次**模型调用，节模式下会把 k 次合成
  变成 2k 次；而精炼的用处是「上下文太大先挑重点」，一节至多 8 条绑定证据本来就没有中段可丢。
- **逐节判定不借跨节证据**：每节在自己的 slice/anchors 上运行 `classify_evidence`，再按上面的三档
  规则聚合；不能拿合并后的高分锚点替另一节通过 grounding。`counts` 仍是各节装配计数之和——同一条
  证据被两节绑上就计两次，那是诚实的：它确实进了两次 prompt。
- **每节的字符预算沿用该档位的完整 `kg/chunk_context_chars`**，不再切分：一节至多 8 条证据，
  预算根本不会成为约束，切分只会引入一个永远不生效、却要解释的旋钮。
- **代价登记（留给 O3）**：收尾那条 `synthesis` 步 detail 新增 `outline_sections`（实际合成节数）/
  `outline_fallback`（bool）/ `outline_skipped`（被跳过的节标题，有界 ≤12×60 字符）/
  `ungrounded_sections`（未过依据门的节标题，同界），另有每节一条
  带 `section_index`/`section_total`/`section_title` 的进度步（同为 `synthesis` 类型），前端
  `reasoning-trace.ts` 已为它们补渲染（进度步显示第几节；收尾步在「N 处引用」后披露
  略过节与依据不足节，各只点名前 3 个标题、余下计数收口——codex r5：不渲染的话用户
  拿到的是一份「看起来完整」的多节答案）；答案文本新增 `## / ###` 两级标题（`globals.css` 的
  `.chat-answer .answer-markdown h2/h3` 已在 O2 内定标，react-markdown 原生渲染）。
  ⚠ 那组字阶**必须**带 `.chat-answer` 前缀：`.answer-markdown` 是共用渲染管线类，深度报告容器
  写的是 `report-markdown answer-markdown`（同一元素两个类），裸选择器与 `.report-markdown h2`
  同特异性、按源码顺序会把报告的 18px/15px 标题顶掉。回归门 `answer-heading-scope-guard.test.mjs`
  （静态特异性/作用域）+ `answer-heading-scope.component.test.tsx`（真实 DOM 祖先链）。

#### 3.1.1 大纲采用引导（adoption nudge）

**真机动机。** 上线后在穷尽档跑了三次真机 run（2 个库、3 个问题），`update_outline`
的采用率是 **0/3** —— 动作在场、schema 在场、prompt 里的适用面也写清楚了，模型一次
都没用。最有说服力的一次：84 篇来源的库 + 「综述这个 notebook 里的文章：每篇的核心
贡献是什么，它们之间有什么联系与分歧？」。模型先用 `enumerate`（`collection=sources`）
**一次性完整列出 84 篇**（`complete=true`），随后连开 4 次 PPR 找「核心贡献」、撞上
PPR 次数上限，最后自述「虽然枚举了所有 84 篇文章标题，但多次子查询未返回具体每篇
文章的核心贡献，现有候选无法覆盖全部文章」并作答。它手里有完整清单，缺的恰恰是「把
清单变成结构、再逐节补证」——也就是大纲。动作说明讲清了大纲**是什么**，却从不在模型
真的拿到清单的那一刻说一句「现在正是时候」。

**合同。** 在 reflect 上下文里补一行**确定性、有界**的引导（`_outline_nudge_note`），
只在「该开大纲却还没开」时出现。零新查询、零新模型调用、零新动作 id：

- **出现条件（全部成立）**：① `outline_wiring_active(settings, limits)` 为真（穷尽档 +
  `REASONING_OUTLINE_ENABLED`）；② 当前大纲**为空**（一旦模型建了大纲，`_outline_note`
  接管同一区位，引导就成了噪音）；③ 本 run 引导次数未达上界；④ 存在结构性理由之一——
  (a) 本 run 已把**来源清单**完整列完（`state == "complete"`）且条数达到下限，
  (b) 已确认意图给出的必答检索方向数达到同一下限。两条都成立时优先用 (a)：它带着一个
  真实条数，「84 篇」比「若干个方面」更能让模型看出把清单摊成章节的性价比。
- **`state == "complete"` 是硬判据**，不放宽成「列过就算」：半份清单变成的大纲天然缺节，
  而模型此刻并不知道自己缺了哪几节；引导它按一份没列完的目录定稿，比不引导更糟。条数取
  各条来源清单链的**最大值**（限定单源与全作用域是两条独立的链），不相加。
- **方向数不重新解析契约**：取 run() 手上现成的已确认方向清单长度减一（`confirmed_intent_queries`
  的第一条恒为完整的已确认问题本身，其余才是必答主题派生的方向）。planner 路径下它恒为 0。
- **措辞三要素**：现状（未建大纲）+ 具体依据（N 篇 / M 方向）+ **可忽略的出口**。出口不是
  客套：服务端并不知道这一题到底需不需要大纲，没有出口的措辞会让模型对单点事实题也硬建
  一份，把省下来的轮次又赔回去。一行、≤160 字符。
- **区位**：与大纲便签同一处（几份账目之后、集合地图之前）。互斥由 `_outline_nudge_note`
  自己的「sections 非空即返回空串」判据保证，**不**写成调用点的 `else`——写成 `else` 的话
  「删掉那道判据」这类改动不会报红。
- **trace**：不新增 step 类型；只在**真的发出**引导的那一轮给既有 reflect 步 detail 加
  `outline_nudged: true`（评估用）。无条件写 `false` 会让关闭态/低档位的 detail 多出一个键，
  与冻结基线「逐键不变」的口径冲突。
- **关闭态/低档位零字节变化**，与本引导接入前逐字一致。
- **已知取舍：引导轮最多吃掉 2 个 stale 槽位**（评审登记）。`update_outline` 本身不新增候选，
  所以那一轮 `no_progress` 恒真、stale 计数 +1（这是大纲机制既有的「修订对熔断中性」设计——
  纯整理不算进展）。于是最坏情况下，模型连续提交两份**被拒**的大纲（结构违规/无合法证据键，
  大纲仍为空、引导因此继续出现）会比不引导时早两轮撞上 `REASONING_STALE_LIMIT`。这是引导
  上界定成 2 的原因之一：它同时也是这条代价的上界。触发要求「模型连续两轮提交都被服务端
  拒掉」，概率低；真机评估若观察到，第一顺位是收紧引导措辞而不是给大纲轮豁免熔断——
  豁免会让「反复整理目录」重新变成躲开熔断的合法姿势。

数值上限（每 run 引导轮数、触发所需的最小篇数/方向数）只在
`docs/product-and-api.md` / `_zh.md` 的「大纲便签与按节合成」契约段维护，这里不重复。

**同批修的一条假报警。** 同一次真机 run 里，答案合成的第一次调用耗时 4 分 25 秒返回空
content（上游 `malformed_response`），有界重试第二次成功、答案完整（78 锚点 / 5829 字），
用户面却仍挂着红色横幅「本次回答可能不完整」。规则改成：`_answer_with_retry` 进入时记下
响应内报警槽的长度，**重试成功**时删掉本次尝试期间新增的那几条（与按节合成回退那处
同源——故障已被同一轮吸收，用户拿到的是完整答案，再挂横幅是在报一个没有影响结果的
错误）。边界：两次都失败时**全部保留**，包括最后那条 empty-content 的 `RuntimeError`
（「检索到却答不出」必须可见）；摘除**按身份**而非按位置——只丢 mark 之后 `workload_id`
为 `ask_answer` 的那几条，同一 run 里更早的、以及 `synth()` **内部**别的 workload 记下的
报错（`evidence_refine`、embedding/rerank 等）一条不动。位置式截断今天恰好等价（证据精炼
静默吞异常、不记 model_error），但那是个会被下一次改动打破的巧合：给它补一条
`note_model_error` 是很自然的一步，之后「首次即成功 + 精炼失败」就会把别人的报警一起删掉。
`events.jsonl` 始终
记全（`note_model_error` 的两个副作用里只有响应那一份是给用户看的）；取消路径不受影响。

### 3.2 任务拆分

| 任务 | 内容 | 角色/模型 |
| --- | --- | --- |
| O1 | 大纲状态+动作+校验+账目回喂+门控+trace | general-purpose (opus) |
| O2 | 按节合成（切片/偏移/合并/回退）+集成 | general-purpose (opus) |
| O3 | 前端标签+文档四份+契约表+守卫收尾 | impl-task (sonnet) |
| O4 | check.sh+提PR+codex 闭环 | 主代理 |

每任务后双评审（spec-review + code-quality-review，opus），变异验证规矩同全篇。

### 3.3 v2 合同 —— KG 弱支撑边回喂（kg-gap，PR-4，stacked 于 PR-3）

DualGraph 共演化里 v1 只做了 OG→检索的一半（空节点名回喂）；KG→检索的定向缺口信号
（Enrich：已绑定证据周边**支撑薄弱**的关系是综述最该补证的方向）是这条 PR 的全部内容。
服务端算出有界候选、以便签行喂给模型，由模型用**既有动作**（add_subquery / follow_chain /
expand_graph）决定要不要补——零新动作、零新模型调用、零 schema 变更。Explore（跨社区
结构洞）仍刻意不做（v1 已否决 SBM；`kg_community_edges` 有独立新鲜度契约，接进来要多背
一整套 stale 语义，ROI 未证）。

**门控**：`REASONING_OUTLINE_KG_GAP_ENABLED`（默认 true，config.py 用 `validation_alias`）
且 `outline_wiring_active`（即穷尽档+大纲总闸）。关闭态零查询、prompt 逐字不变——沿用
「flag 关=执行处 skip、不改动作面」惯例。

**触发与成本**（效率红线）：仅在**被接受的** `update_outline` apply 之后计算一次（被拒/
空提交/纯结构轮不算白不算——没有新绑定就没有新邻域），每 run 天然 ≤6+1 次。run 级
`kg_gap_probed_seeds` 账目记住本 run 已探测过的 seed，绑定集合不变的纯换键/改标题 apply
因此零查询。每次三步、每步一条有界查询，全部走既有索引：
1. 本次大纲绑定的 KG 对象 id 全集（≤12 节×8 键；element/chunk 键不参与——弱支撑边定义在
   KG 图上）经既有 `cluster_fold_rows` 折叠成 canonical 集（有界、本轮 id，红线原文照守）。
2. `canonical_relations` 按 `canonical_src IN (折叠集)` 的 **PK 前缀**探测，
   `source_count <= _KG_GAP_SOURCE_MAX`（2），`ORDER BY source_count ASC, canonical_tgt ASC,
   canonical_src ASC, edge_type ASC LIMIT _KG_GAP_PROBE_LIMIT`（24）。后两个尾键只在合同
   留白处定序——计数与 `canonical_tgt` 相等的行完全可能来自不同源端/不同边类型，没有尾键
   的话「取前 24 行」两次跑出哪 24 行由存储顺序决定。**tgt→src 反向刻意不做**：`canonical_tgt`
   无索引，加覆盖索引要动双后端 schema；登记残余，等真机评估证明 src 侧有用再花这笔。
   表空/从未 rebuild → 零行，整段静默缺席（不是错误——canonical 层本来就是可选产物）。这一
   步的存储读原语是 `weak_support_relation_rows`。
3. 目标端显示名解析走 `canonical_relations.sample_relation_ids` → `knowledge_relations` 主键
   → 两端点对象（主键）→ `idx_clusters_member` 簇行这条链（存储读原语
   `relation_endpoint_name_rows`），**不走** `concept_clusters.canonical_id IN (...)`：那一列
   两侧后端都没有索引，会退化成按 notebook 整表扫描（实测 4 万行簇 62ms/次，百万级线性放大
   到秒级，而这条查询每次被接受的大纲更新都要跑一次）。等价性由 `canonical_relation_seed_rows`
   的同构 join 保证——样本关系折叠后的两端就是这条 canonical 边的两端，是构造事实，不是需要
   人复核的断言。数据来源优先级不变：`canonical_name` 优先、非 concept 的 canonical 回退
   `knowledge_objects` payload name、两端任一解析不到就丢弃整行。
   联邦刻意不做（`canonical_relations` 是 per-notebook 产物；挂载库绑定贡献零候选，登记残余，
   与 exact_lookup「联邦刻意未做」同先例；真机评估还须盯挂载库命中率）。

探测 fail-open：异常记一条 `kg_gap_unavailable` skip 步并返回空，绝不打掉整个 run——这是
一段可有可无的提示。成本按本轮 ≤`_KG_GAP_MAX_SEEDS`（96）个 seed 的**出度**有界（`LIMIT`
只封住返回行数，hub 源端单次可以扫到数万行），不是常数级；总量靠「每 run ≤6+1 次、只探本轮
新出现的 seed」压住。

**端口形状**：`RetrievalPort` 新增一个方法 `weak_support_relations(notebook_id, object_ids)
-> list[GapRelationRow]`，fold+probe+名字解析三步全部在 `retrieval_candidates` 服务端完成
（reasoning_retrieval 零 SQL），双后端 parity。返回行含 src/tgt canonical id、双端显示名、
edge_type、`source_count`（撑着这条边的**不同文档**数，不是聚合掉的原始关系行数）。

**回喂形状**：大纲便签追加一段（仅当有**新**候选）：头行「库内支撑薄弱的相关关系(每条
仅 1-2 源支撑,与某节相关时可用 add_subquery/follow_chain 定向补证,不相关可忽略):」+
每行「A —edge→ B(支撑N)」（N 是 `source_count`）。每行 ≤80 字符、每轮 ≤`_KG_GAP_NOTE_LINES`
（6）行、整段 ≤520 字符。**run 级 (src,edge,tgt) dedup 账目**（仅内存）：每条边只展示一次
——它是提示不是状态，反复展示只烧 prompt 预算；展示顺序 = `source_count` 升序、tie 按
(src,tgt) 字典序，稳定可测。**终态溢出纠错轮既不渲染也不消费该段**：那一轮便签的开头写着
「不得改结构或执行检索」，这一段教的却是「用 add_subquery/follow_chain 定向补证」——两句话
打架，模型照做会把仅有的一次纠错资格烧在 `outline_overflow_repair_declined` 上；渲染与消费
是同一个判据、在 `kg_gap_note_segment` 一处返回，不拆成两处各判一次。除此之外，apply 在
终态轮发生时算出的候选无处展示，如实丢弃（提示服务于继续检索的轮次）。

**trace**：outline 步 detail 增 `kg_gap_candidates`（本次 apply **新入队**的候选行数，供
下一轮便签展示；终态轮那批只入账、无处展示，如实丢弃，不代表本次实际上屏行数）。无候选/
关闭态不加键。不加 step 类型，前端不动。

**数值上限**登记进 `docs/product-and-api*.md` 大纲一节的契约段；本节不重复维护。

**验收**：关闭态/低档位零查询逐字回归；canonical 层缺席时零行零噪声；dedup 不重复展示；
行/段字符界与 LIMIT 生效；阈值只放 `source_count` ≤2；排序稳定；PG parity 用例；变异
（删 dedup、删 LIMIT、阈值失效、flag 失效、便签段删除）全红。

**任务拆分**：

| 任务 | 内容 | 角色/模型 |
| --- | --- | --- |
| G1 | 端口+双后端探测实现+retriever 接入（账目/便签/trace/flag）+定向测试 | general-purpose (opus) |
| G2 | 文档同步（设计文档勾稽+product-and-api 中英+AGENTS/CLAUDE/architecture/fangan_done）+措辞钉子 | impl-task (sonnet) |
| G3 | check.sh+PG lane+stacked PR+codex 闭环 | 主代理 |

### 3.4 深度报告接入 —— 逐节深挖启用大纲共演化（report-outline，PR-5）

**动机（2026-07-31 用户拍板「先在深度报告中实现这部分的生成过程」）**：真机评估显示 Ask 穷尽档
的大纲采用率低——短综述题在小库上两三轮就答满，模型没有理由付大纲轮次；而深度报告**天生**是
大纲机制的主场（必答主题、逐节深挖、长文合成、覆盖压力）。v1 把报告排除在外的理由（k 次合成
成本、双层大纲嵌套）在「大纲只服务检索、不接管合成」的接法下不成立。

**裁定边界（沿用 2026-07-28 评估结论）**：报告侧只允许**确认合同之下的增补式细化**。用户确认
的章节/必答主题绑定是不可替换、不可收窄的合同；深挖中发现的结构只能作为该节**内部**的子结构
（`###`）增补，绝不回写报告大纲、绝不增删改确认过的节。

**接法（三件事，全部在既有缝隙上）**：

1. **五档贯通**：报告「研究深度」与 Ask「检索档位」共用 `EffortPicker` 与档名，但报告的
   `_deep_dive` 调 `ReasoningRetriever.run` 时 `limits=None`——五个档位共享 standard 预算，
   深度滑块只改每节反思轮数。PR-5 把 `DEPTHS=[1,2,4,8,16]` 映射到
   `overview/standard/deep/thorough/exhaustive` 的 `ask_retrieval_limits(effort)` 并传入
   `limits`；**每节 `max_steps` 仍用报告自己的 depth 值**（1/2/4/8/16，成本按节数放大，
   不采用 Ask 档位的 4/8/16/32/50）。行为变化显式登记：低档报告的检索预算随之变小
   （概览 1 轮本来也用不满 standard 预算）、高档变大；这是把同名档位的语义对齐，不是回归。
2. **穷尽档自动激活大纲+弱支撑边**：`limits.effort=="exhaustive"` 时 `outline_wiring_active`
   在每节深挖里为真，大纲便签、空节账目回喂、`REASONING_OUTLINE_KG_GAP_ENABLED` 的弱支撑边
   探测（§3.3）原样生效——零新机制，报告引擎不 import 任何大纲内部件。集合枚举保持不可达
   （报告构造 retriever 时不传 `collection_catalog/collection_enumeration`，wiring 判空自动
   关闭——这是现状，v1 刻意不改）。Ask 侧按节合成代码不在此路径上（报告只用 `run()` 做检索，
   撰写是 `_draft_section` 的事），无需禁用。
3. **子大纲进节撰写 prompt（增补式细化的落点）**：`_deep_dive` 把 `ReasoningResult.outline`
   （若非空）连同各子节绑定的证据 key→`[k]` 映射整理成一段有界「发现的结构」块
   （每子节一行：标题 + 该子节绑定证据对应的 `[k]` 列表；≤12 行、行 ≤80 字符、块 ≤1200 字符），
   附进 `report_section_prompt` 的上下文，prompt 措辞教撰写模型「可用 `###` 子标题按此结构组织
   本节，子结构只是建议，缺证据的子话题如实略过」。`outline_truncated_kg_evidence` 带出的
   截断绑定对象照 Ask 侧同规则并入该节证据分类池。子大纲为空/低档位时该块缺席，prompt 逐字
   回到现状。
4. **进度可见**：报告的节级 `section_status` 在深挖阶段已有 phase 文案；大纲步经 `on_step`
   流过时把该节 phase 细化为「深挖中（已整理大纲 N 节）」（沿用既有 2 秒节流持久化）；
   不新增表列、不新增 SSE。前端 `report-view.tsx` 的节进度文案随 phase 字符串自然显示，
   无需新组件。

**门控**：不加新 flag。报告侧激活条件 = 报告深度选「穷尽」+ 既有 `REASONING_OUTLINE_ENABLED`
（+ 弱支撑边另有 `REASONING_OUTLINE_KG_GAP_ENABLED`）。关掉大纲总闸即回到纯档位映射
（第 1 件事保留——它自身是独立成立的对齐修复）。

**验收**：
- 档位映射逐档钉死（1→overview…16→exhaustive）；`max_steps` 仍等于 depth 值；
  非穷尽档 `outline_wiring_active` 为假、prompt 与现状逐字一致（除预算数值）。
- 穷尽档节深挖 reflect prompt 含 `update_outline`；子大纲非空时「发现的结构」块进
  `report_section_prompt` 且有界；子大纲为空时块缺席。
- 「发现的结构」只影响节内 `###`，`reports.outline_json`（用户确认的大纲）逐字节不变；
  `missing_intents` 校验、最后绑定不可删语义不受影响。
- 弱支撑边探测在报告路径可触发（穷尽+flag 开）且 fail-open 语义保持。
- 节 phase 文案含大纲进度；报告状态机/取消语义不变。
- 变异：映射表错位、「发现的结构」块删除/越界、outline_json 被回写（负向守卫）、
  phase 细化删除。

**真机评估观察项（未定，等数据）**：报告 depth=16 时每节 `max_steps` 就是 16，而
`_MAX_OUTLINE_UPDATES = 6` 是按 Ask 侧穷尽档的 50 步定标的（12%）——同一个常数放在报告
的一节里占掉 **37.5%** 的步数预算。叠加上「连续 3 轮纯大纲整理触发 stale 熔断」（大纲
修订对 stale 中性，不清零），穷尽档报告的一节完全可能把预算花在整理结构上而检索没推进
几步。是否需要给报告侧一个独立定标（按 `max_steps` 比例、或报告专用常数），由真机评估
定；v1 刻意先共用同一个常数，不预先分叉。

**任务拆分**：

| 任务 | 内容 | 角色/模型 |
| --- | --- | --- |
| R1 | 档位映射+limits 传入+子大纲块+phase 细化+定向测试 | general-purpose (opus) |
| R2 | 文档同步（product-and-api 中英+AGENTS/CLAUDE/architecture/fangan_done+部署文档行为变化登记） | impl-task (sonnet) |
| R3 | check.sh+PG lane+提 PR+codex 闭环 | 主代理 |

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

## 6. PR-2.5：指示语接地与来源集合（enumerate_sources）

背景（用户以 NotebookLM 对照提出）：「当前notebook的文章分析」应产出逐篇分析。两个缺口：
①「当前notebook / 这个库 / 本库 / 知识图谱 / KG」等指示语未被接地为「用户打开的 active
作用域」，还会混进子查询充当噪声检索词；②「来源/文章」不是可枚举集合——模型拿不到
「库里有哪几篇」的目录，无法自发做逐篇分析或按篇定向深挖。

### 6.1 指示语接地（零新调用）

- `query_intent_prompt`、`expand_query_prompt`（`plan_prompt` 备份拼写同步）、`reflect_prompt`
  各加一段 grounding 指令：这类短语（中英同列）指用户当前打开的 notebook 及其挂载作用域，
  **不是可检索内容**；生成子查询 / `elements_query` / `exact_term` 时必须剥掉；
  「知识图谱/KG」指本库的知识结构（其规模看集合地图），不作关键词。
- 纯 prompt 层，不做确定性剥词（避免词表路由）；prompt 内容测试钉关键短语，删除+移动变异
  （移动变异的另一半是**位置**：这一段必须排在 `Question:` / `User request:` 之前，规则出现在
  被它约束的输入之后等于没出现，已有位置断言钉住）。
- **【订正，quality P1-1】「知识图谱 / KG」只在领属/指示形式下才算范围词**（`本库的知识图谱` /
  `这个库的图谱` / `the knowledge graph of this library`）。第一版把它无条件判成非话题，而这
  恰好在最可能问这句话的语料上是错的：库里就是 GraphRAG / LightRAG 论文时，
  「这些论文里知识图谱是怎么构建的」的检索词正是「知识图谱」，剥掉它不是去噪、是把查询删了。
  所以措辞里写**显式反向豁免**（文档本身讨论 knowledge graphs 时它是正当话题与检索词），
  测试两半都钉——只钉「要剥」那半的话，把豁免顺手删掉仍然全绿。这一段不受枚举 kill switch
  约束、深度报告每节每步都付，读错的代价正好落在最在意它的那批语料上。
- **【落地】** 四份 prompt 共用**一段**模块常量 `prompts.SCOPE_DEIXIS_GROUNDING`，不是四份
  手写变体——同一件事写四遍就有四次机会说出四个略有出入的版本。措辞加了一条防御:
  「剥掉范围词不能把问题变成另一个问题」（否则「当前 notebook 里的文章讲了什么」会被剥空）。
  `reflect_prompt` 另加一段**本地**规则逐个点名四个自由文本检索字段
  （`new_sub_query.query` / `elements_query` / `ppr_query` / `exact_term`），因为
  `exact_term` 是字面匹配——范围词进去就是一次保证零命中的探测；「问库本身的规模看计数行」
  那句只在工具开启时出现（关闭时那行计数根本不存在）。`query_intent_prompt` 另加一条:
  库级请求（「当前notebook有哪几篇」）是范围明确的完整枚举请求，不得当成「问的是哪个库」
  的歧义去阻断确认门。

### 6.2 来源集合

- 集合地图行尾加 `| sources: N`——与枚举同一计划口径；排除 Memory 合成源与 knowhow
  投影隐藏源（**以来源列表的用户可见口径为准**：先找该口径的唯一谓词处并复用，勿自造）。
- **动作形态（用户拍板）**：模型面**不新增动作**——就是既有 enumerate 动作的分支对象加
  `collection:"sources"` 参数值（与 kind/object_type 并列，动作空间维持 10 个）；仅执行器
  内部拆兄弟方法 `enumerate_sources` 保持游标/coverage 语义独立。与白名单唯一真源守卫兼容。
- 执行器（collection_enumeration）：零 LLM；item = display_title（论文标题优先，复用
  现有 helper）/ doc_type / **已存 per-source 摘要**摘录（截 excerpt_chars）/ notebook_id /
  tier；顺序=计划源序；预算轨复用（每源计 1 行）；coverage/指纹机制复用；total=作用域源数。
- 响应：`TypedCollectionResult.collection` 增 `"sources"`（union 兼容 + api_contract 默认
  模式重生成）；前端清单卡新 arm「来源清单」（标题+类型+摘要摘录；本库条目跳来源详情，
  跨库沿用围栏）；标签 parity 守卫扩展。
- 合成：走既有 enumeration_prompt_block（块头 `[Enumeration: sources, ...]`）；模型据目录
  可对每篇标题继续 add_subquery 定向深挖（既有机制，零新代码）。
- **【订正，codex R3 P1】账目回喂必须带标题**：`_enumeration_note` 原本只回「标签 + 覆盖计数」，
  于是「先枚举目录、再按标题逐篇深挖」这条 prompt 教出来的路径在下一轮就断了——模型手上一个
  标题都没有，只能拿已存摘要凑答案或反复请求同一集合。这是对「账目只回账目、不回条目正文」
  那条规则的**定向豁免**，只对 sources 成立：对这一个集合，标题不是条目正文，它是这份清单
  唯一的可操作输出。边界 = 三重硬界（≤20 条 / 每条 ≤60 字符 / 合计 ≤800 字符，超出写
  `(+N more)`），因此是**常数级**、不随清单长度增长；只回标题不回摘要；元素/知识对象清单的
  账目一个字正文都不带（它们的正文属于合成预算，而模型也不靠它们发起下一步动作）。常数依据：
  20 = 结果卡 `initialVisibleRows`（同一个「一屏能扫多少」判断），60 = 本模块
  `_INTENT_DIRECTION_LABEL_CHARS` 的先例，800 与集合地图块上限同量级。`(+N more)` 的分母是
  「有显示名的条目数」——无名文档没有可回喂的句柄，算进去会让模型去找一个不存在的标题。
  信任等级不变：标题与 `_summarize` 已在同一份 prompt 里回喂的 `el.source_title` /
  `c.source_title` / KG `name` 同类，`UNTRUSTED_EVIDENCE_SYSTEM_INSTRUCTION`（reflect 在
  `untrusted_evidence` 开启时注入）的措辞逐字点名 "every retrieved title"。
- reflect_prompt 动作说明补一句：「库里有哪几篇/逐篇分析/文章总结」类问题先枚举 sources
  拿目录（标题+摘要），再按需对各篇标题 add_subquery 深挖。
- **自适应粒度（对照 NotebookLM 实测行为，用户提供 57 源案例）**：合成侧指导——来源少时
  逐篇分析；来源多时按主题把多篇归纳成维度式综述（目录+摘要就是聚类的原料）。粒度判断
  **交给模型**（地图已给出 sources 计数），不设数值阈值、不做词法路由。
- 文档 product-and-api*/CLAUDE/AGENTS 同步；测试=prompt 内容×3 + 执行器（排除谓词/预算/
  coverage）+ e2e fake-client + 前端卡 arm + 变异矩阵。

**落地决策（实现期定案，与上面的意图一致，补足未定的部分）**

- **动作空间维持 10 个**（用户拍板，见上）：模型面新增的只是 `enumerate` 分支对象里的一个
  参数值 `collection:"sources"`，与 `kind`/`object_type` 并列（排在它们之后，让既有分支的
  前缀逐字不变）；actions 串**不变**。执行器内部仍是独立的 `enumerate_sources`（游标与
  coverage 语义各自独立），那是实现细节。理由是成本落点：动作空间是模型每一轮反思都要重读
  一遍的东西，多一个 id 的代价落在**每一次**调用上，而多一个参数值的代价只落在真的要用它
  的那一次。
  - **参数优先于动作 id**：`collection=="sources"` 一旦给出，无论模型选了
    `enumerate_elements` 还是 `enumerate_kg_objects`，这一轮列的都是文档目录，`kind` /
    `object_type` / `source_id` / `source_title` 全部忽略——模型已经明确说了要哪个集合，
    再去猜它顺手填的 kind 是否更可信，只会让同一个请求有两种解释。prompt 里只教一条路径
    （`enumerate_elements` + `collection:"sources"`），但解析不依赖模型照做。
  - **只识别 `"sources"`**：`"elements"` / `"kg_objects"` 与任何垃圾值一样在解析期清成空
    串，落回按动作 id 的既有分派（那条路径本来就给出同一个答案，所以不需要第二套「模型说
    的集合与它选的动作不一致」的仲裁）。缺省即接入前行为。
  - 共用同一把闸（`enumeration_active()`）：不新增开关。关闭态下 schema 里连 `collection`
    字段都不存在，解析期也不看它。
  - **代价**：反思步 trace 的「下一步意图」仍显示「列元素清单」（`NEXT_ACTION` 表没有第
    11 个键），真正发生的那一步由 enumerate 步自己的 summary 说清（「枚举来源清单: …」）。
    这是这个形态的已知取舍，登记在此以免被当成 bug 修掉。
- **用户可见来源口径的真源**：SQLite `source_store.VISIBLE_SOURCE_TYPES_PREDICATE`
  （`list_sources`/`list_sources_page`/`visible_document_count` 已共用它）；PostgreSQL 侧此前
  在三处内联同一段谓词，本次收成同名模块常量并让那三处引用它。新端口
  `source_change_signal_rows` 把**该谓词本身**作为投影列（`user_visible`）求值，不新造第三份「哪些类型是隐藏的」拼写，也不为它另开一条查询。
  仓库其他位置（maintenance/knowledge_store/index_projection 等）仍各自内联同形谓词——
  那是既有状况，不在本次范围内，也与本清单口径无关。
- **计划与计数同一 helper、零额外查询**：`_visible_signal_rows(signals)` 是唯一决定
  「来源清单包含哪些源」的地方，地图计数取它的长度、执行器遍历它排序后的结果，于是
  「地图说 7、清单列 8」在构造上不可能。成本 = **0**：可见性由 signal 查询自己投影出来
  （`user_visible`，各适配器在 SQL 里对可见谓词求值），`collection_map` 顺带把元素计数与
  来源计数并进**同一次** signal 读取。刻意不为它加缓存：这里没有「计数」可跳过，也没有
  剩下任何读取。
  - **订正（codex R2 P2）**：第一版为它单开一条 `hidden_source_ids` 查询取可见谓词的补集。
    那条查询**无法按 `source_type` seek**（没有任何索引带它），所以它是对该 notebook 全部
    源行的又一次扫描——每参与库一次、在请求路径上、而且就紧跟在 signal 查询刚扫过同一批
    行之后。谓词移进投影后端口整个删掉（它没有第二个调用方；显式 `source_id` 那条路径用的
    是 `memory_source_ids`）。`user_visible` 与 `created_at` 一样**不进指纹**：源类型不会变，
    把它哈希进去只会让所有已部署库的计数缓存白失效一次。守卫按 SQL 语句文本计数
    （退回一条独立查询时端口调用计数看不出来，语句计数看得出来）。
- **【订正，codex R4 P2】schema 里的 `collection` 显示为空缺省**：示例写成唯一值 `"sources"` 时，
  逐字段照抄模板的模型在列公式/知识对象时也会带上它，而参数优先于动作 id（刻意的设计）⇒ 那一轮
  被**静默改道**成文档目录。改成 `"collection":""`（与同一分支里 `source_id`/`source_title`
  同惯例），取值只出现在动作说明里——那里它读起来是一条条件指令，并显式写明「它会覆盖动作本身，
  别顺手带上」。解析行为逐字不变（空串→按动作 id 分派）；关闭态 schema 的冻结字面量不含
  `collection`，不受影响。
- **【订正，codex R4 P2】收尾补元数据换代复检**：作用域指纹是 `updated_at|parse_status|chunked_at`，
  证明不了「已发出文档的显示名/类型还是那一代」——`upsert_paper_meta` 只写 `source_paper_meta`
  与 `source_authors`，**从不碰 `sources.updated_at`**（已核实，非推测），`doc_type` 也是一列可以
  被单独改掉的普通列。于是走页期间的一次论文元数据回填会产出一份混代目录、却仍然报 complete。
  修法：游标携带链级 `(source_id, 元数据摘要)` 账目（`emitted_meta`，仅内存、不上 wire、不进
  用户面 coverage，条数由行池夹住），收尾对**整条链已发出的** id 做一次有界批量点查复读并逐条
  比对，不等即 `concurrent_change`。摘要键刻意只含 (显示名, doc_type)：`summary` 的唯一写者
  `set_status` 在同一条语句里就推 `updated_at`，早已被指纹覆盖，算进来只会在例行重写摘要时误报。
  语义边界：它抓的是「我已经交出去的东西变了」，不是「表变过」——尚未读过的行带着新值第一次
  被读出来，那份目录并不混代，不判 partial（有反向用例钉住）。成本 = 每条链收尾 1 条 SQL，与
  收尾的参与者/指纹读取同级，刻意不计入翻页预算。
- **计划即全集 ⇒ 无 lookahead**：计划长度已知，`position < len(plan)` 就是「还有没有更多」的
  精确答案，因此来源侧不需要元素侧那个 +1 lookahead 行（「行预算恰好等于集合大小」不会
  误报 partial），并有一条测试钉住这一点，防止有人「顺手统一」成需要 lookahead 的写法。
- **游标形态**：`(notebook_id, source_id)` 指向**尚未列出**的第一份文档（inclusive resume，
  与元素游标「最后消费的位置」相反）——一份文档就是一行，所以「下一行」与「下一份文档」
  是同一件事，指向未列出的那一份才能让「首行就撞载荷上限」也交回可用游标。
- **遍历顺序 = 来源页签顺序 `(created_at, id)`**（订正 spec-review B3：先前按 id 排，那是一个
  用户从没见过的顺序，而「前 N 篇」的截断前缀因此没有意义）。排序键随信号行一起回来
  （`source_change_signal_rows` 多投影一列 `created_at`，同一行访问、零额外查询），双后端各自
  归一化成「按字典序比较即等于本后端 `ORDER BY created_at, id`」的文本。**两侧的来源列表查询
  也必须带 `id` 次键**（codex R3 P2）：批量导入会写出并列 `created_at`，SQLite 对并列行不保证
  稳定顺序，`list_sources`/`list_sources_page` 缺次键就与目录的 `(created_at, id)` 序分叉，
  「前 N 篇」在页签与模型手上成了不同的 N 篇（并列时还会让翻页重复/漏行）；PostgreSQL 侧
  `ORDER BY created_at,id COLLATE "C"` 本来就带，这是 SQLite 单侧补齐。排序键本身——PostgreSQL 侧这条
  归一化**必须先转 UTC**（codex R2 P2）：`timestamptz` 的 offset 不是一列之内的常量，跨 DST
  转换时相隔一小时的两行会读成 `…01:30:00+02:00` 与 `…01:30:00+01:00`，按字符串比是先比墙钟
  数字再比 offset 文本，于是 `+01:00` 排在 `+02:00` 前面——而它是**更晚**的那个瞬间。不归一
  就只在 DST fold 那一小时里静默错序。naive 值不动（编一个时区是猜）。三条推论：
  ①**指纹语义不动**——摘要只吃前两个字段，创建时间对活着的源永不变，把它哈希进去只会让
  L1/L2 在这次上线时全量失效一次；②**元素侧顺序仍按 `source_id`**：它的游标是
  `(source_id, element_id)` keyset，换序会让已发出的游标对不上位置，而来源清单是按 key 重
  对齐的，没有这个约束；③地图的计数路径**不排序**（只要 `len`），排序只发生在真的要遍历
  那份清单的 `scope_source_plan` 里。游标刻意**不**携带 `created_at`：续跑是按 key 在重建的
  计划里重对齐，而任何会改变顺序的变动（增删源）都已经改变指纹并被判为
  `concurrent_change`——一个永不参与比较的字段只会腐烂。
- **登记：合成块头用 `documents` 而不是 `sources`**（措辞偏离，刻意保留）。集合 id 是
  `sources`（wire 上的 `collection`、reflect 参数值、trace detail 全用它），但注入合成 prompt
  的块头是 `[Enumeration: documents, listed 2/2, complete, previewed 2]`。理由是块头是**给写
  答案的模型读的一句英文**，而 `sources` 在那个位置有歧义（RAG 语境里 "sources" 常指「引用
  出处」，正是它下面那些 `[k]` 卡片）；`documents` 只有一种读法。代价是「同一集合在协议里叫
  `sources`、在 prompt 里叫 `documents`」，登记在此以免被当成不一致修掉；改的话两处一起改，
  并同步 `_collection_noun` 与钉住块头的测试。界面词不受影响——用户看到的一直是「来源清单」。
- **无名文档的占位**：目录预览行与结果卡共用同一句「未命名来源」（后端常量
  `collection_enumeration_answer.UNNAMED_SOURCE_LABEL`），**不**退回内部 source id：模型会把
  id 当标题引回来（目录的用途就是给它可按名深挖的标题，而 id 匹配不到任何东西），而且内部
  id 一经模型复述就进了答案。
- **wire 复用元素臂**：`TypedCollectionItem` 不加第三组近义字段——`source_title`=显示名、
  `location_label`=文档类型界面词、`text`=摘要摘录、`element_type` 留空（文档不是元素，
  前端按 `collection` 分派）。文档类型用 `extraction_profiles.PROFILES` 的界面词（上传选择
  器同一份表），未识别渲染空串，绝不上屏 `academic_paper` 这类内部 id。
- **早退闸计入来源数**（codex R5 P1 **订正**；本条第一版写的是「刻意不含」，那是错的，一并
  记在这里而不是悄悄改掉）。原理由：「来源数对任何非空库都 ≥1，算进去等于把这道闸整个拆掉、
  让那句明确提示对所有纯文本库消失」。前半段成立，结论错在三点：
  ① 被挡住的**恰好是来源清单的主力场景**——一库论文只经纯文本解析器处理（无公式/表格/图片/
  代码块），自动 KG 抽取默认关（无知识对象），而用户问的正是「库里有哪几篇 / 逐篇分析当前
  notebook」。用户问文档目录、拿回「请先构建知识图谱」，那不是一句更明确的提示，是没有回答
  被问的问题（实测确认：那句提示的唯一出口就是早退分支的确定性 `conclusion`，前端根本不消费
  `kg_required`，所以「保住提示」保住的只是一个非答案）；
  ② 来源清单**不需要图谱**：零 LLM，读的就是 `sources` 行，挡住它换不来任何正确性；
  ③ 这与 PR-2 立下的判断是同一个（自动 KG 默认关、「解析了来源但没建图」是常态，所以早退才
  收窄成「无图**且**拿不出任何集合」）——加进第三个集合后没更新判定函数，是漏跟，不是独立决定。
  保留的语义（都有用例）：**零源库仍然早退**（计数为 0，不是「有 notebook 就放行」），地图
  构建失败仍然早退，放行后 `kg_required` 仍如实为 `True`。
- **标签 parity**：后端第三张表 `_SOURCE_COLLECTION_LABELS`（按 collection 取键）+ 前端
  `SOURCE_LIST_LABELS`，接进同一条 `check_enumeration_list_labels_contract.py`（三张表都过
  「前端值 == 后端值 + 清单」与全域唯一性）。做成同形对象字面量而非裸常量，是为了让守卫
  不必为一个标签另开一条解析路径——那条新路径本身就会成为下一个「解析不了就放行」的缺口。
  渲染值是「来源清单」（与来源页签同一个界面词）。
- **自适应粒度落在合成侧、且只在目录在场时出现**：`collection_enumeration_answer.
  _SOURCE_GRANULARITY_LINE`，随枚举预览块一起注入，条件是本轮真的有 `sources` outcome。
  刻意不放进 `answer_prompt` 的枚举规则：那条规则会让产品里**每一次**合成都付这段字符，而
  只有文档目录的正确答案形状随规模变化。它排在披露说明之后、目录块之前，且与披露说明同样
  「非承重」——预算不够时它自己被丢掉而不挤掉目录本身（没有目录的粒度提示毫无用处，反过来
  则不然）。文本里不含任何数值阈值：模型手上已经有精确计数（块头 + 集合地图），写死
  「超过 N 篇就归纳」是换了名字的词法路由，而且两个方向都会错（5 篇长论文可能就该按主题答，
  30 条一页笔记可能就该逐条答）。
