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

- SQLite：`_migration_36` 追加索引
  `CREATE INDEX IF NOT EXISTS idx_source_elements_source_type ON
  source_elements(source_id, element_type, created_at, id)`；`SCHEMA_VERSION` 35→36。
- PostgreSQL：新增打包迁移 `0014_source_element_type_index.sql`（同索引，风格对齐现有
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
- KG 对象类型计数：复用 `notebook_catalog.py` 现有 per-type 缓存（按 kg_mutation_seq 记忆化），
  不新增查询路径。
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
- **一次动作在预算内自动翻页**：直到游标耗尽或触及 `enum_rows_per_run` / `enum_pages_per_run`
  / `structured_payload_chars`(256k 复用) 任一上限。**页预算只计同一源的第 2 页及之后**（真正
  的额外往返；源访问次数由行预算天然约束）。模型不感知 cursor；同一集合重复请求时，
  若上次因预算截断且本 run 预算有余则从内部续游标，否则 skip(already_enumerated)。
- **游标携带作用域身份**：cursor 必须带开场指纹（元素=作用域 (source,变更信号) 指纹；
  KG=参与 notebook 的 kg_mutation_seq 向量）与累计 returned；续跑先比对，作用域已变→
  `concurrent_change`，绝不静默从头重跑；分母校验在链末端按累计 returned 生效。合同不变式：
  `complete=false ⟹ cursor 非空`，唯一例外 `truncated_reason=concurrent_change`。显式
  `source_id` 请求直接对该源发索引分页查询，不得做「不在计划⇒零」推断（首解析窗口保护）。
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
  "object_type":"concept|claim|formula|procedure","source_id":""}`。
- `reflect_prompt` 增加动作说明：问题要求列出/盘点某类条目时优先于 search_elements；结合地图
  计数判断是否值得全量；大集合应改为「计数+样例+建议缩小范围」。完整性陈述以工具 coverage 为准。
- run() 新增 elif 分支（镜像 search_elements 形态）：无效 kind/object_type → fail-open skip
  （fail_closed 下抛错）；预算耗尽 → skip(enumeration_budget)；结果累积到独立 `enumerations`
  列表（不混入 elements/chunks），并计入 no_progress/stale 账目；每页间 raise_if_cancelled。
- trace：`enumerate` 步（前端 chip 已存在），summary 形如「枚举公式清单: 返回 X 条(共 Y)」，
  detail 含 returned/scanned_rows/total/complete/truncated_reason。
- 新档位字段（`AskRetrievalLimits`）：`enum_page_size`=50（各档相同，transport 批量而非答案
  top-N，口径同 structured_page_size）；`enum_pages_per_run` 2/4/6/8/12；`enum_rows_per_run`
  100/200/300/400/600。
- kill switch：`REASONING_ENUM_TOOLS_ENABLED`（config.py，注意 pydantic-settings v2 需
  validation_alias），默认 true；false 时不注册动作、不注入地图（回到现状）。

### 2.5 合成与响应契约

- `enumeration_prompt_block`（仿 `structured_prompt_block`）：进入 source 分区最前，预览行数
  按 `inline_answer_rows`=100 口径 + 字符预算截断；块头写明 coverage（如
  `[Enumeration coverage: formula, returned 12/12, complete]`）。
- `AskResponse.result_sets` 泛化为 kind 判别 union：现有 `StructuredKnowhowResult
  (kind="knowhow")` + 新 `TypedCollectionResult(kind="collection")`
  {collection:"elements"|"kg_objects", element_kind/object_type, items, coverage:
  StructuredResultCoverage}。旧持久化数据（kind="knowhow"）解析兼容，补反序列化测试。
- `completeness_unavailable` 免责文案更新：本轮已产生清单结果卡时不再前置（coverage 徽章承担
  披露）；未产生时保留但措辞提及元素/对象清单能力。

### 2.6 前端

- `workspace-model.ts` 增类型；`answer-panel.tsx` 增清单结果卡：按来源分组；formula →
  `FormulaView`(KaTeX)、table → **text 摘录 + 跳转来源查看完整表格**（table_html 无界且截断
  即碎，不进传输 item）、image → AuthedImage（item 携带有界 asset_id）、code_block → 代码块；
  KG 对象用 kg-type-mark 现有标签。完整/部分徽章与
  explicit_partial 提示复用 Knowhow 卡样式；初始 20 行 + 客户端翻页。
- 面向用户文案：「公式清单 / 表格清单 / 图片清单 / 代码块清单 / 概念清单 / 论断清单 /
  过程清单」「已全部列出 / 部分结果」——过词汇守卫。
- enumerate trace 步 detail 展示适配（scanned_rows 分支已在 reasoning-trace.ts）。

### 2.7 守卫、文档与评测

- 架构守卫默认模式重生成（AskResponse 变化）+ facade allowlist。
- 文档：docs/product-and-api*.md 契约表新增 enum_* 与工具说明；AGENTS.md「Architecture
  Baseline」与 CLAUDE.md 红线补「集合枚举工具」条目——并修订两处现有红线句
  「当前只有 Knowhow 支持完整枚举，其他对象集合仍是相关性结果」（PR-2 落地后不再成立，
  fangan 同款表述在 fangan_done.md 记账）；architecture.md 补 collection_catalog /
  collection_enumeration 运行时组件与全部新端口（T2 两个 + T3 三个）；首解析 under-count
  窗口的披露口径一并写入；README 中英一句能力入口。
- 测试：scripted fake chat client 驱动 reflect 返回 enumerate 动作，断言自动翻页/预算/
  complete/partial/取消/双后端 parity；离线测试钉「工具可达性与合同」，模型是否选用属真机评测
  （部署机手动对照）。

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
- 枚举覆盖缺口披露：元素层只覆盖结构化解析（MinerU）来源；结果卡按异常分级黄色档披露纯文本
  解析来源数；KG 对象清单措辞用「已抽取的」。
