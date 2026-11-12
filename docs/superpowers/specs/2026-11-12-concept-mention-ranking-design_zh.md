# 概念提及排行 `rank_concepts` 设计

状态：定稿候选 r6（r2/r5 按两轮独立评审修订；r3–r6 依次写入用户裁决，§13 已全部拍板），待用户确认后进入实现
日期：2026-11-12
基线：`master` `b4917b1db`（行号以此为准，实现时以符号名为准）

## 1. 背景与目标

用户常问：「在当前笔记本中，被提及最多的 topic / 优化点 / xxx 是什么，列出 top3」。现状答不好，根因是**工具缺口**：
检索 Agent 只能看到按相关度挑出的前 K 段内容，「最多」只能从样本里目测，回答不可复核。

本设计给检索 Agent（reasoning 模式）和 MCP 补一个**零 LLM 调用**、确定性的排行工具：按「提及它的来源篇数」给合并后的概念
排序，返回带计数、带出处的排行。带主题词时要调一次 embedding（与普通检索相同），此外不调模型。语义筛选（「哪些算优化点」）
交给 reflect 循环里**本来就在的模型**：它拿到带名字的排行后自己挑，必要时换主题词再排一次。

**非目标**：

- 不开代码沙箱（理由见 §15）。
- 不做论断（claim）排行：论断只按精确种子合并，几乎每条都只出现在 1 篇来源里，排出来没有意义。
- 不改「问题理解」步：不加「最多 / 排名」正则，工具只靠 reflect prompt 被选中（`agentic-tools-not-regex-routing`）。

## 2. 现状（设计的出发点）

**Agent 侧**：

- 枚举动作 `enumerate_kg_objects` 按 `created_at` keyset 翻页（`collection_enumeration.py` `_walk_kg_objects`），
  不排序、不计数；每行证据截断到 3 个（`MAX_EVIDENCE_REFS`），不能拿来数来源。
- 集合地图（`collection_catalog.py`）只给每类对象的总数。
- 「最多 / top3」这类问题在问题理解步多半被判为 `ranked`（`query_intent.py:250-283`），这时 `completeness_required=False`，
  回答**没有任何完整性披露**。只有被判为 `aggregate` 时，`suppress_completeness_warning` 才恒为假
  （`response_drafting.py:3234-3238`），一定追加「本次回答未验证完整性」。问题理解步的字面兜底 `_AGGREGATE_REQUEST`
  （`query_intent.py:72-80`）不认「最多 / 排名」；reasoning 循环本身不读 `result_scope`。
- prompt 里没有任何关于「最多 / 排名 / 被多少来源提到」的指引；答案规则 11 还要求「不合并相似项」（`prompts.py:503-521`）。

**数据侧（可复用）**：

- `concept_clusters`：对象到合并后概念（canonical）的映射，统一口径是 `COALESCE(已发布代 c.canonical_id, o.id)`，
  读方必须带已发布代谓词 `_PUBLISHED_CLUSTER_GEN`（`postgres/unified_kg_store.py:122-125`）。canonical id 随合并漂移，
  跨重建不稳定。
- 按 canonical 读成员的正确 SQL 形态：`postgres/unified_kg_store.py:3354-3420` `_display_name_rows_in`，即「簇表按 canonical
  走 `idx_clusters_nb_canonical_member_gen`」UNION ALL「单例对象按 id 查 + `NOT EXISTS` 簇行」两条腿。
- 图谱分析的预计算（`kg_findings._Corpus`，`kg_findings.py:379-398`）在每次 `rebuild_communities` 里**已经算出**
  每个 concept canonical 的 `df`（出现在几个可见来源）与 `can_objects`（可见可用对象数），只是没有落库。口径是
  `source_canonical_rows` 的「可用状态 + 可见 owner 来源（排除 memory / knowhow 投影）」，但它**有意保留孤儿引用**
  （owner 指向已删除来源，`postgres/unified_kg_store.py:2978-2983`），所以落库前要先屏蔽孤儿（§10）。取前若干名只需把这些下标
  并进 `compute_findings` 的 `needed`，交给 `_resolve_canonical_ids`；另需两次有界读（§10）。
- 同名判据：`concept_squash_key`（`kg_merge_seed.py:210`，F4 的主键）。
- 主题检索：`RetrievalService._retrieve_scored(notebook_id, query, types=…)`（`retrieval_candidates.py:2635`）。
  **注意**：显式传 `allowed_source_ids` 会令 `explicit_source_filter=True`、跳过全库 ANN，只剩 FTS
  （`retrieval_candidates.py:2667-2716`、`:3074-3077`）。

**不能用的**：

- `mention_edges`：DF 门会丢掉命中论断数超过 `max(20, 2%×论断总数)` 的别名，**被提到最多的概念恰恰最容易被丢掉**；
  而且只覆盖跨源概念、两次重建之间不更新。不作为计数依据。
- 整库在线 `GROUP BY canonical ORDER BY count DESC LIMIT n`：必须扫完整库同类对象，`LIMIT` 管不住；
  `ports.py:3360-3400` 明确禁止把这种形状放在请求路径上（本机 30 万簇行 PG 152 ms 且已溢盘，生产约 200 万簇行）。

## 3. 计数口径

一个概念（canonical）在一个库里的计数：

| 字段 | 定义 |
|---|---|
| `source_count`（主排序键） | 统计范围内、拥有该 canonical 至少一个可用成员对象的**不同 owner 来源**数 |
| `object_count`（次排序键） | 统计范围内该 canonical 的可用成员对象数（同一来源多次抽到同一概念会多计，界面称「提及条数」） |

- **统计范围**：owner 来源属于该库可见导入来源，且（天花板生效时）属于本次运行冻结的天花板。
- **成员谓词**三条路径与预计算完全一致，统一复用 `source_canonical_rows` 那条谓词：`USABLE_STATUSES`
  （`app/domain/knowledge_contracts.py:16`）、owner 非空串、owner 不是 memory / knowhow 投影来源。孤儿引用（owner 指向已删除
  来源）不计：在线路径要求 owner 在可见来源集合内；预计算在 bincount 之前按同一趟里读到的可见来源 id 屏蔽孤儿来源的 incidence（§10）。
  测试钉住「同一库上 S 路径与预计算结果相同」。
- **按 owner 而不是按证据支撑计**：与图谱分析的 `df` 同一口径，省掉 `knowledge_object_sources` join 与 `source_index_backfilled`
  双分支。这**刻意偏离**全仓天花板「按证据支撑判」的定义（`postgres/source_ceiling.py:165-185`），后果见 §13.1。
- **排序完全确定**：`source_count` 降序 → `object_count` 降序 → `canonical_id`（C 序）。展示名不参与排序（预计算没有名字）。
- **只排 concept**，工具参数里不提供类型选择。

**已知会偏离真实值的两种情况，在结果里显式提示，不静默修正**：

- **同名未合并**（F4 / S1）：本次调用算出的行里 `concept_squash_key` 相同的，标同一个 `same_name_group`，**不相加**
  （两行的来源集合可能重叠，相加会重复计数）。
- **疑似重复导入**（F2）：读图谱分析账本里本库 F2 的条数（一次主键点读）。不为 0 时，覆盖说明写
  「全库有 N 对疑似重复导入的来源，计数可能偏高」（F2 是全库口径，勾选收窄后也照写「全库」）。

## 4. 执行路径（单个库）

### 4.0 天花板与「收窄」判定（先于一切读取）

「收窄」只表示**用户自己缩小了范围**，与天花板是否生效是两回事：

- **单库运行的活动库**：收窄 = 服务端算出的 `scope.narrowed is True`（`source_scope.py:188,557`）。来源漂移（`ceiling_binds_for_run`
  为真但不是用户收窄，比如提问过程中有新来源入库）**不算收窄**：按未收窄统计，读到的行与引用按冻结集逐个核验（沿用
  `fast_path_members`），覆盖说明写「统计期间来源有变化」，`exact=false`。
- **挂载参考库与全局问答的参与库**：恒为未收窄。它们的冻结集按构造就是该库全部可见来源（`global_ask.py:1003-1004`
  `visible_source_ids_by_notebook`；挂载库 `_mounted_library_ceilings` 冻结 `all_visible_source_ids`，`source_scope.py:2195-2197`），
  全局问答也不提供逐库收窄。这是运行上记下的构造性事实，不再读一次来源 id 去比对。
- **天花板为空**（库被排除、挂载库读超时冻结为空集、本库一篇都没勾、`covers_notebook` 为假）：该库**不参与**排行，按
  `library_skipped` 告知（§6.4），绝不报 `scoped_large_notebook`。
- 收窄时 `scope_sources = 冻结集 ∩ 可见来源 − memory/knowhow`；未收窄时用成员谓词（§3）而不物化来源列表。
- 时间与取消统一走 `read_budget(deadline, cancel_event)`（§4.2），天花板参数走 `ceiling_param`。

### 4.1 路径判定（按顺序）

先判**大库**：整库可用 concept 数 > `KG_RANK_ONLINE_MAX_OBJECTS`（50 000）。计数走新增的公开方法
`CollectionCatalogService.unscoped_usable_counts(nb)`，复用集合地图 `(nb, "")` 那份 seq memo，通常命中缓存；未命中时代价是
`knowledge_type_count_rows`（store 级 seq memo）加 Memory 扣减。**不能**用 `_notebook_kg_counts(ceiling_binds=True)`：
它在收窄的运行里返回天花板口径的计数，未命中时会触发 4 次带全量天花板的 `count_knowledge`，正是大库上要避免的形态。

| 顺序 | 条件 | 结果 | 是否可判 `exact` |
|---|---|---|---|
| 0 | **大库且用户收窄**（§4.0 的定义，不论有无 `query`） | **不支持**，返回 `unsupported: scoped_large_notebook`，不做任何回退统计 | — |
| 1 **Q 主题** | 参数给了 `query` | `_retrieve_scored(types=["concept"])` 取前 `KG_RANK_QUERY_CANDIDATES`（100）个对象，映射成 canonical 去重，再按 §4.2 重算 | 否：语义是「与主题最相关的 100 个概念里的排行」 |
| 2 **S 小库全量** | 无 `query`，小库 | 在线全量聚合 | 是（未截断、无漂移时） |
| 3 **P 预计算** | 无 `query`，大库（此时必然未收窄） | 读 `kg_concept_ranks`（§10） | 仅预计算新鲜、无漂移时 |

- **大库只勾选部分来源时不支持（用户裁决 2026-11-12）**：这种组合要么得在天花板内重算头部泛概念（成员行多，慢且常被截断），
  要么只能拿全库排名当候选（会漏掉只在所选来源里突出的概念），两种回退都给不出可信的排行。所以不回退，明确告诉用户失败原因和
  怎么做才能问（§6.4）。小库勾选部分来源照常支持（S 路径的扫描量只取决于整库大小）。
- Q 路径：未收窄时**不传** `allowed_source_ids`，保留向量召回（检索层自己与运行天花板求交）。小库收窄时必须传，这时只剩字面检索，
  覆盖标记 `candidates_lexical_only=true`，覆盖说明写「所选来源范围内只按字面匹配找候选，同义说法可能漏掉」。
- S 路径门放行后不论是否收窄都用同一条 SQL，扫描上限就是 5 万对象。
- P 路径的两种情形：
  1. **新鲜**（`built_at_kg_seq`、`built_at_cluster_seq` 都等于当前）：直接用存储的计数，`exact`。
  2. **陈旧**：照用存储的计数，不重算；覆盖标记 `stale_counts=true`，覆盖说明只陈述事实「统计建于较早的图谱状态
     （之后有 N 次变更未计入）」，不在说明里叫用户去做什么；能不能「更新分析」由前端按当前用户权限决定是否给出路（§6.4）。
  预计算缺失时按 §10「排行数据缺失的三种情况」返回 `unavailable`，同样按失败明确告知（§6.4）。

### 4.2 在线计数 SQL（PG 为规范定义，SQLite 镜像）

**重算（只用于 Q 路径）**：候选 canonical 按候选顺序分批，每批一条两腿查询（仿 `_display_name_rows_in`）：

```sql
-- 腿 1：已进簇的成员，按 canonical 走 idx_clusters_nb_canonical_member_gen
SELECT c.canonical_id AS can, o.source_id, o.id
FROM concept_clusters c JOIN knowledge_objects o ON o.id = c.member_object_id
WHERE c.notebook_id = :nb AND c.canonical_id = ANY(:batch) AND <已发布代谓词>
  AND o.object_type = 'concept' AND <成员谓词> AND <范围谓词>
UNION ALL
-- 腿 2：单例对象，按主键查，且没有已发布代簇行
SELECT o.id, o.source_id, o.id
FROM knowledge_objects o
WHERE o.notebook_id = :nb AND o.id = ANY(:batch) AND o.object_type = 'concept'
  AND <成员谓词> AND <范围谓词>
  AND NOT EXISTS (SELECT 1 FROM concept_clusters c WHERE c.notebook_id = :nb
                  AND c.member_object_id = o.id AND <已发布代谓词>)
```

外层按 `can` 聚合 `COUNT(DISTINCT source_id)` 与 `COUNT(*)`。`:batch` 一律经 `id_binding` 的 `member_of` / `drive_by` 绑定
（`tests/test_id_list_binding_guard.py`）。成本控制：

- **先计数、再切批**：对全部候选做一次 `concept_clusters` 的 index-only 计数（`GROUP BY canonical_id`），按候选顺序切批，
  每批成员行 ≤ 10 000；单个 canonical 超过 10 000 行时自成一批（§16 的头部概念就有 1.7 万成员）。
- **行数上限** `KG_RANK_RECOUNT_MAX_ROWS`（200 000，§16 实测：上限 5 万时最坏情况 100 个候选只能重算 6 个）：下一批会让累计超过上限
  就停下，后面的候选（包括超过剩余额度的单个大概念）不重算、不出现在结果里，`recount_capped=true`，账本点名跳过了几个。
- **时间上限与取消**：整个调用在 `read_budget(deadline=now+KG_RANK_RECOUNT_BUDGET_MS, cancel_event)` 里执行（1 500 ms；
  PG 侧 `_BudgetedReadConnection` 按剩余时间设语句超时，SQLite 侧 progress handler 中断，`postgres/database.py:633-641`、
  `sqlite/database.py:472`），不另在 `execute_with_ceiling` 上设 `statement_timeout`。捕获 `ReadBudgetExceeded` 后**先**
  `raise_if_cancelled`（同 `source_scope.py:2503-2513`）：用户点了停止就按取消结束整个运行，不吞成截断；真超时才按截断处理。
  每批用新连接，被中断的连接不再用于取名与取引用。一个候选都没来得及重算时，这次调用按失败处理（`recount_budget_exhausted`，
  §6.4），不返回空排行。
- 多库时每库各自 1 500 ms，单次调用另有总上限 `KG_RANK_CALL_BUDGET_MS`（3 000），并计入 peer lane 的时限；超出总上限的库按
  `library_skipped`（原因：超时）告知。
- PG 与 SQLite 都钉计划：腿 1 走 `idx_clusters_nb_canonical_member_gen`，腿 2 走主键。

**全量（S 路径）**：

```sql
SELECT COALESCE(c.canonical_id, o.id) AS can, COUNT(DISTINCT o.source_id), COUNT(*)
FROM knowledge_objects o LEFT JOIN concept_clusters c
  ON c.notebook_id = o.notebook_id AND c.member_object_id = o.id AND <已发布代谓词>
WHERE o.notebook_id = :nb AND o.object_type = 'concept' AND <成员谓词> AND <范围谓词>
GROUP BY 1
```

整库 ≤ 5 万对象，结果在 Python 里按 §3 排序，保留前 80 行（4 页）。

### 4.3 展示名与同名分组

- 只为本页要展示的 canonical 取名（每页 ≤ 20 个）：按 `member_object_id` 顺序，取第一个属于统计范围、可见、可用的成员的
  `payload->>'name'`（`LATERAL … LIMIT 1`，走同一个簇索引；与引用共用 `KG_RANK_CITATION_SCAN` 的成员扫描上限）。规则确定、有界，名字只取范围内可见来源自己的对象，
  不会带出天花板外或 knowhow 对象的名字。
- 取不到名字的行丢弃，计入 `unnamed_dropped`。
- 同名分组用 `concept_squash_key`，在本次调用算出的全部行（≤ 80）内判。
- 取名查询与计数不在同一个快照，两次读之间来源变化时可能名字取不到、该行被丢弃，接受这一点。

### 4.4 引用

- 每行最多 3 个证据元素，取自**不同 owner 来源**的成员：按 `member_object_id` 顺序扫描范围内成员，最多扫
  `KG_RANK_CITATION_SCAN`（50）个，按来源去重取前 3 个（`LATERAL` 有界查询，钉计划）。元素取自成员证据，沿用
  `collection_item_citations` 的存活与天花板判据；未收窄时按 `fast_path_members` 核验。
- 合成证据块里每行一个 `[kN]`，绑定第一个元素；结果卡显示全部 ≤3 个。引用证明「这个概念在这里被提到」，**不证明计数本身**。
  计数由系统给出（§6.4 首行与结果卡），prompt 要求照抄（§5.4）。

## 5. Agent 动作（reflect）

### 5.1 动作与参数

新核心动作 id `rank_concepts`，嵌套参数键同名，参数一律用字符串或字符串枚举：

| 参数 | 类型 | 说明 |
|---|---|---|
| `query` | string ≤200 字，可空 | 限定主题的自然语言描述（「推理性能优化方法」）；空表示统计范围内全部概念 |
| `scope` | `"all"` \| `"current_notebook"` | 与 `enumerate_*` 同义；全局运行里 prompt、schema 与解析一起去掉（`local_only_scope_offered()`） |
| `cursor` | string | 翻页，见 §5.5 |

不提供 `top` 参数：固定每页 20 行，留出余量，让模型从里面挑出符合语义的项（比如去掉「模型」「方法」这类泛概念）。

### 5.2 门控

`rank_concepts_active()`：以下条件全部成立时动作才出现，prompt 行、schema 分支、白名单、解析、执行五处读同一判据：

- `enumeration_wiring_active`（catalog 与 enumeration 服务都在场）、`allow_enumeration`、新增的 `allow_rank_concepts`；
- 部署开关 `REASONING_RANK_CONCEPTS_ENABLED`（默认开，§13.7）；
- 本次运行 `kg_in_scope_for` 为真；
- 档位额度 > 0，且不在 `spent` 集合里。

报告引擎不传 enumeration 服务，动作在报告里自动关闭；knowhow 补全把 `allow_rank_concepts` 显式设为 False。
schema 门是 bool，满足 `test_reflect_action_contract.py` 按签名反射开闸的要求。

### 5.3 回喂账本

每次调用回喂给 reflect：

- 路径（`query` / `scope_full` / `precomputed`）、各库统计范围的来源数；
- 本页 ≤20 行：句柄 `c1…`、展示名、`source_count`、`object_count`、`same_name_group`、所属库（多库时）；
- 覆盖标记（§6.2）。

与 `enumerate_kg_objects` 不同，这里回喂名字（同 `find_sources` 回喂标题）：不给名字，模型就挑不出哪些算「优化点」。

### 5.4 prompt（L0，只在门开时出现）

在 `enumerate_actions` 之后、`completeness_rule` 之前加一段：

- 问「被提到最多 / 最常出现 / 最主要的 X 是什么」→ 用 `rank_concepts`。X 是一个主题范围时写进 `query`；问整个库时留空。
- 拿到排行后**按问题的语义自己筛选**：跳过不属于 X 的泛概念，从剩下的行里取前 N 名。本页不够 N 个时，翻页或换一个 `query`
  再排一次。
- 回答里的计数**照抄**排行里的数字，说明口径是「提及它的来源篇数」；`same_name_group` 相同的行要说明「可能是同一个概念」，
  不自己相加；覆盖标记不是 `exact` 时不得声称「最多」，只能说「在统计到的范围内」。
- 多库时排行按库分开，不自己把不同库的计数相加（§7）。
- 排行返回失败（`unsupported` / `unavailable`）时，不要换个方式「估」出最多的是什么：正文直接说明没能统计、原因和用户该怎么做，
  再给出检索到的相关内容并说明那不是统计结果。

### 5.5 游标与翻页

- 每次调用在本次运行内按参数指纹（`query`、`scope`、路径）缓存完整结果（≤80 行），翻页只切片，每页 20 行，最多 4 页；
  **每翻一页计入本次运行的调用次数**。
- 游标编码：参数指纹、偏移、每个参与库的 `(kg_mutation_seq, cluster_mutation_seq)` 与天花板摘要（`scope_ceiling_digest`）。
  任一项与当前不符就拒绝（`RankCursorStaleError`，「图谱已变化、从第一页重新排行」），账本记 `rank_cursor_stale`，已发的句柄不变。
  MCP 两次调用之间没有运行内缓存，按同一规则重算并校验游标。

## 6. 证据、合成与结果卡

### 6.1 合成证据块

- 与枚举预览**共用 5000 段**：把 `enumeration_prompt_block` 现在自己从 `COLLECTION_KEY_BASE+1` 起号的逻辑抽成共用的
  连续 `next_key` 分配器，两个块按拼装顺序取号，避免撞号。`kind_for_key` 仍归为 `collection`，`AskEvidenceKindCount`
  不新增 kind；`response_drafting` 的 `included_collections` 计数把排行块按一个清单计。
- 块内每行：`[kN] <展示名> — 在 <source_count> 篇来源中出现，共 <object_count> 条`，加同名组与所属库标记。块头是覆盖说明。
  用 `context_spans.entry_lines / joined` 拼，保证 MCP `output="evidence"` 能按行切分。
- 内联行：每条调用链（参数指纹）内联它**最后一次取到的那一页**（≤20 行，与模型在 reflect 里看到的一致），全部调用链合计
  ≤40 行；在「结构化侧」预算内拼装，顺序：knowhow → 枚举预览 → **排行** → 卡片 → 文档读取。装不下的整条调用链不内联，
  只出现在结果卡里。
- **有排行结果时保持单次合成**：`response_drafting.py:2781` 的分节条件 `not enumerations` 扩成「无枚举且无排行」，
  理由与枚举相同（codex r7：节切片不装清单块，分节会让模型手上没有计数）。

### 6.2 覆盖标记（wire、账本、首行共用）

```
coverage: status(ok|unsupported|unavailable), reason, path(query|scope_full|precomputed),
          scoped(bool), drifted(bool), scope_sources, candidates_lexical_only(bool),
          stale_counts(bool), seq_behind(int), recount_capped(bool), recount_skipped(int), unnamed_dropped(int),
          duplicate_import_pairs(int), exact(bool), next_cursor,
          skipped_libraries[{notebook_id, reason(read_failed|timeout)}]
```

`status` 不是 `ok` 时没有任何行，`reason` 取以下之一：

| `status` | `reason` | 含义 |
|---|---|---|
| `unsupported` | `scoped_large_notebook` | 大库且用户收窄（§4.1 第 0 行） |
| `unavailable` | `not_computed` | 大库还没有排行数据，「更新分析」能产出（§10） |
| `unavailable` | `analysis_unavailable` | 社区层关闭或本库建不了板块，「更新分析」也产不出（§10） |
| `unavailable` | `refresh_produced_nothing` | 「更新分析」成功结束后仍没有排行数据（§10 第 3 种） |
| `unavailable` | `recount_budget_exhausted` | Q 路径一个候选都没来得及重算（§4.2） |

多库时读不到的库不让整次调用失败，记进 `skipped_libraries`（只有这一种叫法），由 §6.4 的失败行告知。

单库 `exact` 成立的两种情况（都要求 `drifted=false`、`unnamed_dropped=0`）：

- `path=scope_full`，且 `recount_capped=false`；
- `path=precomputed`，且 `stale_counts=false`。

多库时每个库各有一份覆盖；整体不判 `exact`（§7）。

### 6.3 结果卡

- 新增结果类型 `ConceptRankingResult`（`kind="concept_ranking"`），加入 `AskResponse.result_sets` 的判别联合。不复用
  `TypedCollectionResult`：它没有计数字段，前端遇到不认识的 `collection` 值会落进普通清单分支，计数会被静默丢掉
  （`answer-panel.tsx:735`）。
- **每条调用链一张卡**（同枚举），卡头是这条调用链自己的覆盖；多库时卡内按库分节，每节一份覆盖。
- 行：展示名、来源数、提及条数、同名组、≤3 个证据元素。卡里显示这条调用链取到过的全部页（≤80 行）。
- `status` 不是 `ok` 的调用也出一张卡（或卡内一节），只写失败原因的文字，**不带出路**：出路只放在回答开头的失败行里，
  一处可点，避免两个按钮状态要同步。
- 前端新卡片 `ConceptRankingCard`：表头「概念 / 来源数 / 提及条数」，文案遵守 `docs/ui-vocabulary.md`（用「概念」，
  不用「TopN」「抽取」）。`global-deleted-sources.ts` 的 `markResultSet` 认新 kind。

### 6.4 首行披露、失败告知与完整性提示

**失败告知（用户裁决 2026-11-12：失败要明确告诉用户；给出的出路必须能点、点了立即生效）**：本次运行里只要有一次排行调用
`status` 不是 `ok`，或有参与库被跳过，回答正文前就有一行确定性的失败说明，**不论模型正文写了什么、引没引用排行**，也不受
`result_scope` 影响。每种原因一句，写清没做成什么、为什么；出路做成句子末尾可点的字（`.link-button.inline-action`，同
`report-scope-hint.tsx` 的「全选」），点一下就执行，不是叫用户去别处找按钮：

| 原因 | 失败句（后端产出，纯文字） | 出路（前端挂在句尾，`[…]` 可点） | 点击后立即做什么 |
|---|---|---|---|
| `scoped_large_notebook` | 没能按提及次数排行：本笔记本内容较多，只勾选部分来源时不支持这类统计。 | [全选本库来源并重新提问] | 把本库来源恢复为全选（`selectAllSources`，参考库的勾选不动，§13.11），并以**显式的全选范围**提交同一问题（不依赖状态刷新，避免带着旧范围提交） |
| `not_computed` | 没能按提及次数排行：本笔记本还没有排行数据。 | [更新分析]（后台进行，大库可能较久） | 走图谱面板同一个入口 `kgWorkspace.startRebuild()`（同一套忙碌位、409 后领养在跑任务、共享轮询，带 `ensure_concept_ranks`）。**这一步会调用模型时先弹一次确认框，否则不弹、直接开始**（§13.13，判据见表后）。句尾原地变成「正在更新分析…」，刷新页面后由现有领养机制恢复进度；终态后读一次 `concept_ranks_built_at_seq`：产出了就变成 [重新提问]，没产出就改写成 `refresh_produced_nothing` 那一句 |
| `analysis_unavailable` | 没能按提及次数排行：本笔记本暂时不支持这类统计。 | 无（点了也产不出） | — |
| `refresh_produced_nothing` | 没能按提及次数排行：这次更新没有产出排行数据。 | 无 | — |
| `recount_budget_exhausted` | 没能按提及次数排行：统计超时。 | [重新提问] | 立即用同一问题重新提问（§16：第二次读时缓存已热，最坏情况从 1.2 s 降到 0.3 s 以内，重试通常能完成） |
| 有库被跳过 | 有 K 个笔记本读取失败，没有计入排行。 | [重新提问] | 同上 |

除「有库被跳过」外，每句之后接同一句后缀：「下面的回答只基于检索到的部分内容，不是统计结果。」

**「更新分析」何时确认**（用户裁决 2026-11-12：会调模型时确认）：只有真的重新聚类时才会调模型——部署配置了 `kg_merge_review` 和 / 或
`kg_concept_description` 绑定时，`rebuild_unified_kg` 会在重新聚类里调用它们（`kg_routes.py:322-339`）；聚类输入没变、只补排行数据时
（`ensure_concept_ranks` 走「只补账本」路径）不调模型。所以 `GET /notebooks/{id}/unified-kg/rebuild/status` 新增只读字段
`model_calls_possible: bool` = 「上述任一绑定已配置」且「聚类输入版本与已发布的不同（这次会重新聚类）」，与任务内部的版本闸同一判据。
前端点击时读这个字段（失败行渲染时已随状态轮询拿到）：为真就弹一次确认框，复用图谱面板 `confirmGenerateKgAnalysis` 的文案，并加一句
「这一步会调用已配置的模型」；为假就直接开始。用户在确认框里取消时，句尾恢复成可点的 [更新分析]。

- **文案只有一个主人：后端**。新字段 `AskResponse.ranking_notice: str`（完整纯文字，多句用换行分开）与结构化的
  `ranking_failures[{reason, notebook_ids}]`。正文开头写入同一段 `> {ranking_notice}`，`conclusion` 同步，供 MCP、导出、复制、
  分享页与导入分享（`public_share_import.py`）使用。前端照 `completeness_notice` 的做法（`answer-completeness.ts`），新增
  `answerBodyWithoutRankingNotice` 按**精确前缀**剥离这段，再逐字用后端文案重画，只按原因码在句尾挂出路；一条测试钉住后端产出与
  前端剥离一致。
- **正文开头的固定顺序**：失败句 → 披露行 → knowhow 覆盖行 → 模型正文 → 通用完整性提示。三者在 `response_drafting.py`
  同一处（现通用提示与 knowhow 覆盖行的拼装点，`:3225-3258`）按这个顺序拼装。
- **只给当前用户做得到的出路**（`ui-partition-and-inline-actions`）：
  - 「更新分析」只给**单库运行的活动库**，且当前用户是可编辑成员、库不是镜像；挂载库、全局问答参与库、只读成员与镜像库只有文字。
    已有图谱任务在跑（维护槽被占）时置灰并就地写「图谱正在处理其他任务」，任务结束后自动放开。
  - 「全选本库来源并重新提问」只看**实时状态**：当前打开的就是这个笔记本、来源栏处于自选模式、且本库来源现在不是全选。
    不依赖回答里存的数据判断「范围来自哪里」；MCP 发起的提问（会话 `submitted_via="mcp"`）不显示。
  - 各宿主页面：笔记本页给上表全部出路；全局问答页（`ask/global-ask-workspace.tsx`）只可能出现「有库被跳过 / 统计超时 / 未生成」，
    只给 [重新提问]，点了**回填输入框**（与该页现有「重新提问」一致，由用户再点一次发送；用户指定的例外，§13.12）；分享页、导出、复制、MCP 只有纯文字。
- **重新提问**都是新的一轮，问题文字、回答方式、档位与原来相同，提交后焦点交给新的一轮；点击前不自动发生任何事。失败行本身不随点击消失，
  原回答保留。
- 同一原因只写一次。
- 账本把失败原因回喂给 reflect；prompt 要求模型此时**不得从检索样本里推断「最多」**，正文里也要说清楚没能统计
  （系统失败行是兜底，保证用户一定看到）。
- 通用完整性提示不因排行失败而强制追加：它只在 `completeness_required` 为真时出现（`ask_service.py:5690-5745`），而「最多」类问题
  多半是 `ranked`；失败句后缀已经写明「不是统计结果」。

**披露行**：回答正文引用了至少一个排行行的 `[kN]` 时，在失败句之后加一行确定性的披露（同样并入 `ranking_notice`），**与 `result_scope` 无关**：

- `exact`：`按提及它的来源篇数统计，范围：<N> 篇来源。`
- 不是 `exact`：`按提及它的来源篇数统计，<逐项写出缺口>，结果可能不全。`（缺口取自 §6.2 的覆盖说明，只陈述事实，不含指令）
- 多库：每个被引用的库一句。

新判据 `ranking_line_rendered(answer, ranking)`（与论文表的 `coverage_line_rendered(answer, paper)` 并列，不是标志位）：
只有披露行确实渲染了、被引用的排行全部 `exact`、且本次运行没有失败的排行调用时为真，并压掉通用的「本次回答未验证完整性」，
`aggregate` 范围也生效。这是 `response_drafting.py:3234-3238` 唯一新增的例外，已裁决（§13.6）。回答没引用排行、
也没有失败的排行调用时不加任何行，原有完整性逻辑不变。

## 7. 多库（全局运行与挂载参考库）

- 每个参与库**分别**走 §4，各用自己冻结的天花板；挂载库与全局参与库恒为未收窄（§4.0），所以多库里不会出现 `scoped_large_notebook`。
- 每库读取有时限与失败隔离（同 `find_sources` 的 peer lane 做法，单次调用总上限见 §4.2），读不到的库记进 `skipped_libraries`，
  由失败行告知。
- 带主题词时每个参与库各跑一次 `_retrieve_scored`；查询向量能否跨库复用以实现时核实 `_embed_query` 的缓存为准，不能复用时
  「一次 embedding」改为「每库一次」写进 §8 与文档。
- **默认不跨库相加**：canonical id 只在单库内有效；各库各取前 N 再相加也得不到真正的全局前 N（一个概念可能在每个库都排第 25 名、
  相加后进前 20）；笔记本复制会生成新来源 id，同一份文档在两个库里会被重复计数。结果按库分节，每节一份排行与覆盖；
  模型在回答里分库说明，或在明说「各库分别统计」的前提下自行比较。跨库相加列为 §13.5 的备选。
- 参与库解析放在 `collection_enumeration.py` 里（它已在 `test_participant_override_guard.py` 的读者白名单中），不新开读者模块。

## 8. MCP

- 新工具 `rank_concepts`，`read` 档，放进 `mcp_tools/sources.py`，工具总数 +1。
  参数：`notebook_id`、`query`、`cursor`、`limit`（1–20）。返回：§5.3 的行、每行 `refs` 序号、顶层去重的 `refs` 表、§6.2 的覆盖。
- MCP 没有运行作用域：只读单库，按「凭证白名单内、主人当下可读」的单库**未收窄**处理（memory / knowhow 投影按成员谓词天然不计），
  每次调用重读凭证。
- `ask(output="evidence")` 不带 `result_sets`，所以排行要靠 §6.1 的证据块文本带出计数。
- 服务端 `instructions` 加一句：问「最多 / 最常」先用 `rank_concepts`，再用 `read_reference` 复核代表性出处。
- 文档写明：带 `query` 时会调 embedding（单库一次；多库按 §7 核实结果写「一次」或「每库一次」）。

## 9. 预算与配置

`AskRetrievalLimits` 新增 1 个字段 `rank_calls_per_run`（含翻页；按位置构造，5 行一起改，`test_query_intent.py` 的整表钉值与
文档档位表同步）：overview 1 / standard 2 / deep 3 / thorough 3 / exhaustive 4。

配置项（`app/core/config.py`，部署文档中英同步）：

| 名称 | 初值 | 说明 |
|---|---|---|
| `REASONING_RANK_CONCEPTS_ENABLED` | true | 部署开关 |
| `KG_RANK_ONLINE_MAX_OBJECTS` | 50 000 | 大库门槛：S 路径整库上限，也是「大库只勾选部分来源不支持」的判据 |
| `KG_RANK_QUERY_CANDIDATES` | 100 | Q 路径候选对象数 |
| `KG_RANK_RECOUNT_MAX_ROWS` | 200 000 | 单次重算扫描的成员行上限 |
| `KG_RANK_RECOUNT_BUDGET_MS` | 1 500 | 每个库一次重算的时间上限（经 `read_budget`） |
| `KG_RANK_CALL_BUDGET_MS` | 3 000 | 多库时单次调用的总时间上限 |
| `KG_RANK_CITATION_SCAN` | 50 | 每行取名与取引用时最多扫的成员数 |

预计算行数固定 80（= 4 页 × 20 行），不做成配置。

**阈值先测量再定稿**（热路径 SQL 的惯例）：在生产规模样本上（PG 主环境，SQLite 镜像）测以下形态的冷/热耗时：

- S 路径全量聚合，整库 5 万对象，未收窄与「勾选大部分来源」两种；
- §4.2 重算：Q 路径候选含头部泛概念（成员上万）；
- 取名与引用的 `LATERAL` 查询；
- P 路径预计算读取、预计算里屏蔽孤儿用的可见来源 id 读取；
- Q 路径检索（未收窄走 ANN、收窄走 FTS）。

测量结果见 §16。S 路径在 5 万对象上实测 PG 17–32 ms、SQLite 34–39 ms，`KG_RANK_ONLINE_MAX_OBJECTS` 保持 5 万
（冷磁盘没测到，留余量）。

**覆盖索引**（§16 实测收益，进 `HOTPATH_INDEX_SPECS`，按惯例 `--apply` 才建，已裁决见 §13.10）：

- `knowledge_objects (id) INCLUDE (notebook_id, object_type, status, source_id)`：重算的成员回表变成只读索引。
  最坏情况（100 个头部候选、11.6 万成员行）从 0.27–1.23 s 降到 0.14 s；代价是索引体积约为主键的 2 倍（合成库 265 MB 对 124 MB）。
- S 路径用的 `(notebook_id, object_type, status) INCLUDE (id, source_id)`：页数从 4 391 降到 873，但耗时只从 22 ms 到 18 ms，
  **不加**。

## 10. 存储：`kg_concept_ranks`

```
kg_concept_ranks(
  notebook_id, rank, canonical_id, representative_object_id,
  source_count, object_count, built_at_kg_seq, built_at_cluster_seq,
  PRIMARY KEY (notebook_id, rank)
)
```

- **独立表，不进 `kg_analysis_artifacts` 账本**：不新增账本种类，就不会触发 `_absence` 把缺席判成 `unexpected`、
  不会让 `analysis_ledger_is_current` 对所有库返回 False 而引起一轮分钟级全量重算，也不用改 `LEDGER_CONTRACT_VERSION`、
  `CLUSTER_DEPENDENT_ARTIFACT_KINDS` 与单位表。新鲜度靠行内自带的两个世代戳。
- 在 `rebuild_communities` 里由 `kg_findings` 那一趟 `_Corpus` 产出，与问题清单在同一个发布事务里整删整插。比现有那一趟多两次有界读：
  1. 读一次本库可见来源 id（≤ 约 5 万行，走 `idx_sources_visible_identity`），在 bincount 之前屏蔽孤儿来源的 incidence；
  2. 对前 80 个 canonical 做一次有界点查，取 `representative_object_id`（`FindingsObjectTally.add` 只保留来源与 canonical 下标，
     没有对象 id）：该 canonical 按 id 最小的可见可用成员。
  canonical id 漂移后，读侧用代表成员解析出当前 canonical（取名、取引用都按解析后的 canonical）。两行解析到同一个 canonical 时
  只保留排名靠前的一行，并计入 `stale_counts` 的说明。
- **「更新分析」必须真的产出排行**：`rebuild_communities` 在「图谱新鲜且账本新鲜」时直接返回（`knowledge_lifecycle.py:8120-8126`），
  而 `kg_concept_ranks` 刻意不在账本里，所以已部署、分析已新鲜的库点「更新分析」会秒级成功却什么都不产出。新增参数
  `ensure_concept_ranks`：`run_unified_kg_rebuild_job` 在用户点击触发时（图谱分析面板的「更新分析」与排行失败行的「更新分析」）带上它；
  带上时，`kg_concept_ranks` 缺行或世代戳与当前不符就视为账本不新鲜，走现有「只补账本」路径（不重新聚类）。自动整理路径
  **不带**这个参数，避免上线后对每个库触发一轮分钟级回填（§13.3）。
- **排行数据缺失的三种情况**（读侧按顺序判定，对应 §6.4 的不同原因码与出路）：
  1. 社区层关闭（`COMMUNITY_LAYER_ENABLED=false`）或本库建不了板块（无 igraph 且无 scale 索引，`community_build_refused`）：
     `analysis_unavailable`，不给「更新分析」（点了也产不出）；
  2. 其余情况下表里没有行：`not_computed`，给「更新分析」（带 `ensure_concept_ranks`）。
  3. 「更新分析」任务成功结束后读侧仍判为缺失（例如 `kg_analysis_backfill_discarded`）：如实说「这次更新没有产出排行数据」，
     不再给同一个出路，避免死循环。
- 图谱分析面板的 `analysisUpdateState`（`frontend/app/kg-analysis-model.ts:966-1004`）同样把「排行数据缺失或陈旧」算进 `needs_update`，
  否则面板会显示「已与当前图谱一致，无需更新」，与排行失败行互相矛盾。kg-analysis 报告新增 `concept_ranks_built_at_seq`，供面板与
  失败行判定。
- **上线**：已部署的库要等下一次实际重算图谱分析，或用户点「更新分析」，才有这张表的数据；之前大库的「无主题排行」返回
  `not_computed`。不强制全量重算（§13.3）。
- 不存展示名：展示名在线取（§4.3），避免已删除来源的名字留在表里。
- **同步**：`SyncClass.SYNCED`，`scope=notebook`，`since_schema` 为本次迁移版本，捕获触发器由本次迁移安装（同 `kg_analysis_findings`
  的先例，`migration/sync/manifest.py:355-358`）。目标环境的世代戳若与本地世代对不上，按陈旧处理（§4.1 情形 2），不会出现
  「有账本无明细」的空排行。不进导出 / 复制。
- **登记点**：`notebook_delete_tables.py`；同步 manifest 与 `capture_contract`；`migration/shadow/manifest.py`；
  `postgres/schema_manifest.py`；`postgres/knowledge_store.py` 的两张删图表清单（约 `:106`、`:891`）；
  `kg_build_job_store.py:515-532`；复制笔记本时缺席的那组表（`postgres/sharing_store.py:150-153`，有测试钉住）与导出白名单；
  SQLite 迁移与 PG 迁移。删除知识图谱（维护槽 `clear`）与删除笔记本时一并清理。
- 迁移号：实现时以 `origin/master` 最新号为准（当前 SQLite `SCHEMA_VERSION=108`、PG 最新 `0089`）。

## 11. 改动点清单

以 commit `b2fb29f92`（论文工具接入 reflect）为模板，逐项对照：

- **执行器**：新模块 `services/concept_ranking.py`（三条路径、重算、取名、同名分组、引用、多库）。store 端口
  `concept_rank_full_rows` / `concept_recount_rows` / `concept_rank_names` / `concept_rank_citation_rows` /
  `concept_rank_precomputed_rows` 在 SQLite 与 PG 两侧实现；登记到 `test_memory_reader_guard.py` 的 `_REGISTRY`、
  `tests/fixtures/repository_contract/facade_surface.json` 与 `test_facade_surface_fixture.py`；`repository_runtime.py` 与
  `ask_service` 接线。
- **reflect**：`CORE_REFLECT_ACTION_IDS` 与 `REFLECT_SCHEMA_TOP_LEVEL_FIELDS` 各加 `rank_concepts`（文档里的
  「19 个核心 id、18 个顶层字段」改为 20 / 19，顺带订正 `product-and-api.md` 里过时的「16 ids」「ten-word」）；
  `rank_concepts` 成为插件保留名，仓库外插件若有同名动作会启动失败，写进文档与更新说明。同时改：`prompts.py`（schema 分支与
  动作说明）、`reflect()` 解析与 fail_closed 必填校验、2000 字硬闸字段表、`_dispatch_reflect_action`、`ReflectDecision`、
  `core_reflect_actions`。
- **trace 与经验**：新 `step_type="rank_concepts"`，登记 `STEP_TYPES` 与 `KG_SHAPED_STEP_TYPES`；detail 写 `listed`
  （条数）、不写 `result_ids`（`test_trace_result_ids.py`）；在经验库的 stale 熔断里按「有结果 / 无结果」计，与枚举同口径。
  `RETRIEVAL_ACTIONS` 与 `RetrievalAction` Literal 末尾追加 `rank_concepts`，`retrieval_experience_block._ACTION_IDS` /
  `ADOPTION_ACTIONS` 同步（模块级 assert）；名字通过词表守卫（不含 source / scope 等词）。前端 `reasoning-trace.ts` 加标签
  「排行」与 `NEXT_ACTION`、`getTraceStepDetail` 分支，`profile-model.ts` 加经验词标签。
- **证据**：抽出 5000 段共用的 `next_key` 分配器；`ConceptRankingResult` 与 `AskResponse.ranking_notice` / `ranking_failures`
  的 wire 模型与 api_contract 夹具；`response_drafting` 的结构化侧拼装、分节条件、`included_collections` 计数、§6.4 的正文开头
  拼装顺序与新判据；`public_share_import.py`（导入分享时保留这段）；引用构造点登记 `test_citation_attestation_guard.py`
  的 `REGISTRY`，执行器读出的行用 `attest_read`。
- **状态守卫**：`test_reasoning_run_state_authority.py` 登记新的 `_action_*` 与预算读取点。
- **目录与图谱分析**：`CollectionCatalogService.unscoped_usable_counts`（§4.1）；`run_unified_kg_rebuild_job` /
  `rebuild_communities` 的 `ensure_concept_ranks` 参数与 `kg_routes.py` 透传（§10）；rebuild 状态接口的 `model_calls_possible`
  （§6.4，与任务内部的聚类版本闸同一判据，测试钉住两者一致）；kg-analysis 报告的 `concept_ranks_built_at_seq`。
- **前端**：`workspace-model.ts` 类型、`ConceptRankingCard`、`answer-panel.tsx` 判别分支、`global-deleted-sources.ts`；
  `answer-completeness.ts` 旁新增 `answerBodyWithoutRankingNotice`；失败行组件 `RankingFailureNotice`（逐字用后端文案，句尾按原因码挂出路）；
  `page.tsx` 给 `AnswerView` 接 `kgWorkspace.startRebuild` 与忙碌位、`sourceLibrary.selectAllSources`、带显式范围覆盖的提交入口；
  `ask/global-ask-workspace.tsx` 只接「重新提问」并复用该页现有的回填输入框行为；`c/[token]/page.tsx` 分享页剥离后只显示纯文字；
  `kg-analysis-model.ts` 的 `analysisUpdateState` 把排行缺失或陈旧算进 `needs_update`。
- **MCP**：`mcp_tools/sources.py`；资源不存在的错误名视需要登记 `mcp_tool_host._NOT_FOUND_RESOURCE`。
- **存储**：§10 的全部登记点。
- **文档**：`product-and-api` 中英（枚举段、无图移除动作段、新增排行段、经验词表、保留名个数、MCP 工具表）、部署文档配置项、
  `architecture.md` 动作清单、`fangan_done.md`、`release-notes/rank-most-mentioned-concepts.md`
  （`level: feature`、`audience: all`，标题暂定「问“被提到最多的是什么”，现在能按来源数给出排行」）。

**需要随之更新的测试**：`test_reflect_action_contract.py`、`test_peer_mode_reflect_actions.py`、
`test_model_schema_hint_snapshots.py`（手写闸列表加新闸）、`test_model_json_schema_hints.py`、`test_prompts.py`、
`test_prompt_contract_alignment.py`、`test_reasoning_retrieval.py`（`_KG_ONLY_ACTIONS` 等）、`test_retrieval_experience_job.py`、
`test_retrieval_experience_privacy_guard.py`、`test_experience_offered_actions.py`、`test_agent_profile_job_overlay.py`、
`test_reasoning_trace_stats.py`、`test_trace_result_ids.py`、`test_participant_override_guard.py`、`test_query_intent.py`、
`test_knowhow_completion.py`、特征化测试三件、`test_typed_collection_result_sets.py`、`test_ask_evidence.py`、
`test_global_citation_producers_e2e.py`、`test_backend_lint_gate.py` 基线；前端 `reasoning-trace.test.mjs`、
`agent-profile-model.test.mjs`、`raw-enum-fallback.test.mjs`。

## 12. 测试与验收

1. **CI 回归**：合成 fixture 笔记本（12 篇来源，预设概念分布：包括一个跨 9 篇的泛概念、同名未合并对、一对重复导入、
   Memory 与 knowhow 来源下的同名概念、一个孤儿引用），覆盖以下各项：
   - S 路径与预计算结果相同（成员谓词一致，含孤儿引用）；P 路径新鲜 / 陈旧两种情形；canonical 漂移后经代表成员解析；
   - 天花板收窄、来源漂移按未收窄统计并核验、Memory / knowhow 来源不计、取名不带出天花板外的名字；
   - **不得误报 `scoped_large_notebook`**：全选后提问期间有新来源入库、全局问答中途有参与库上传、挂载库读超时、只勾参考库不勾本库，
     四种都不出现这条失败；
   - 重算：头部概念自成一批、行数上限与时间上限各一例截断、`recount_capped` 与跳过个数、用户点停止时按取消结束而不是截断；
     同名分组、F2 提示、小库收窄时 Q 路径的 `candidates_lexical_only`；`id_binding` 守卫；
   - **失败告知**：大库用户收窄（有 / 无 `query` 各一例，断言没有发起任何计数查询）、`not_computed`、`analysis_unavailable`、
     多库中一个库被跳过、重算一个都没完成——每种都断言 `ranking_notice` 出现在正文开头且顺序正确（模型正文没提也出现）、
     结果卡只有文字没有出路；
   - **「更新分析」真的产出排行**：分析账本新鲜但排行缺失的库，带 `ensure_concept_ranks` 的任务会补出排行；自动整理不带时不补；
     任务成功但仍未产出时读侧给 `refresh_produced_nothing`；`analysisUpdateState` 在排行缺失时为 `needs_update`；
   - 多库按库分节、跳过库；游标失效；门控关闭时 prompt / schema 逐字节不变；
   - 结果卡与 MCP 输出、5000 段与枚举预览不撞号、有排行时不分节合成；
   - §6.4 披露行只在引用了排行时出现，通用提示只在全部 `exact` 且无失败时被压掉（含 `aggregate` 范围）。
   - **前端组件测试**（失败行的出路）：点「全选本库来源并重新提问」→ 本库来源变成全选、参考库勾选不变、以全选范围提交一轮问题文字 /
     回答方式 / 档位都相同的新提问、焦点到新的一轮；点「更新分析」→ 走 `kgWorkspace.startRebuild`、`model_calls_possible` 为假时不弹确认框直接开始、为真时弹确认框（取消后出路恢复可点）、这句变成进度、
     刷新后进度恢复、终态后按是否产出显示「重新提问」或 `refresh_produced_nothing`；维护槽被占时置灰并就地说明；只读成员、镜像库、
     挂载库与全局参与库看不到「更新分析」；不在这个笔记本、不是自选模式或本库已全选时看不到「全选本库来源并重新提问」；
     全局问答页的「重新提问」把原问题回填进输入框、不自动发送；回答开头那一段不显示两遍（后端文案与前端剥离一致）；纯文字版（复制 / 导出 / 分享页）不含可点的字。

   PG 泳道另跑同一组用例，并钉 §4.2–§4.4 各查询的计划。
2. **测量**（本机，不进 CI）：§16 已做一轮；上线前在生产只读副本上用同一组查询复测。
3. **真模型端到端**（本机 deepseek-flash，**后端用 PostgreSQL**，与测试库同一后端；跑之前先征得用户同意）：6 题，包括
   「被提及最多的优化点 top3」「最常出现的概念」「关于 X 提得最多的方法」，以及全局问答 1 题；另加两题失败场景（大库只勾选部分来源、
   大库未生成分析），在浏览器里实际点一遍出路。人工核对：回答里的计数与结果卡一致，引用能跳回原文，首行披露与覆盖一致，
   出路点了立即生效。

## 13. 裁决记录（全部已定，2026-11-12）

- 大库（整库可用概念超过门槛）只勾选部分来源时，不支持这类统计，也不做任何回退统计；回答前用确定性的失败行明确告诉用户没做成和
  原因（§4.1 第 0 行、§6.4）。其他失败（未生成分析、库读取失败、统计超时）同样明确告知。
- 失败行给出的出路必须是句子里可点的字，点了立即生效：全选本库来源并重新提问、更新分析、重新提问（§6.4）。唯一例外见第 12 项。
- 「更新分析」**会调模型时先确认，否则直接开始**（替代 r4 的「一律不弹确认框」）：判据是 rebuild 状态接口的
  `model_calls_possible`（§6.4）；不调模型时句尾注明「后台进行，大库可能较久」后直接开始。

1. **计数口径：按 owner 来源篇数**（§3），与图谱分析一致。这刻意偏离全仓天花板「按证据支撑判」的定义，已知后果：勾选范围内，
   一个由范围外来源拥有、由范围内来源支撑的对象（人工合并或晋升后会出现），枚举清单会列出它，排行却不计它；集合地图的
   concept 数与排行各行之和也对不上。
2. **语义筛选交给 reflect 模型**（§5.4），不加单独的「先归类再计数」模型步骤。问题里的类别与概念名差得很远时（比如「哪些是缺陷」），
   可能出现本页挑不出来的情况，靠翻页或换 `query` 补救。
3. **自动整理不补排行数据**：只在用户点「更新分析」（带 `ensure_concept_ranks`）或图谱分析因别的原因实际重算时产出；已部署大库在此之前
   返回 `not_computed`，失败行给「更新分析」。
4. **预计算陈旧时照用存储计数**（§4.1 情形 2），披露落后多少次变更。
5. **多库按库分节，不跨库相加**（§7）。
6. **完整性提示的新例外**（§6.4）：引用了排行就加口径说明行（`ranked` 范围也加）；全部 `exact` 且无失败时压掉通用提示（`aggregate` 范围也压）。
7. **默认开启与文案**：部署开关默认开；界面词「排行」「概念 / 来源数 / 提及条数」；更新说明 `level: feature`。
8. **带主题词时取与主题最相关的 100 个概念作候选**，不做相关度阈值校准。
9. **MCP 一起做**（§8），沿用论文工具的先例。
10. **重算覆盖索引登记为可选热路径索引**（`HOTPATH_INDEX_SPECS`），生产 `--apply` 后生效，不做正式迁移。
    没建时功能照常可用；带主题词的问题遇到头部泛概念时，冷缓存下可能触到 1.5 秒时间上限、只排出一部分（如实标截断）。
    运维文档的热路径索引清单补上这一条及其收益（§16），并写明风险：index-only 依赖 visibility map 及时更新（受 vacuum 节奏影响），
    且这张大表的每次写入多维护一个索引。
11. **「全选本库来源并重新提问」只恢复本库来源**（§6.4），参考库的勾选不动：`scoped_large_notebook` 只由本库收窄引起，挂载库从不算收窄。
12. **全局问答页的「重新提问」回填输入框**，与该页现有「重新提问」行为一致，由用户再点一次发送（§6.4）。这是「点了立即生效」的
    用户指定例外，只适用于全局问答页。
13. 「更新分析」确认规则见上。
14. **来源漂移按未收窄统计**（§4.0）：提问过程中有新来源入库时照常排行，并披露「统计期间来源有变化」，不标精确。

## 14. 风险

| 风险 | 缓解 |
|---|---|
| 泛概念（「模型」「方法」）霸榜 | 每页 20 行留余量，prompt 要求按语义筛选；带 `query` 时只在主题候选里排 |
| 合并质量差导致计数被拆散 | 同名分组提示；图谱分析的 F4 / S1 是根治入口；首行写明口径 |
| 重复导入导致计数偏高 | F2 计数提示（全库口径） |
| 大库排行陈旧 | 披露落后变更数；「更新分析」后再问 |
| 大库只勾选部分来源时问不了 | 不回退，失败行写明原因与做法（勾选全部来源后再问）；小库不受影响 |
| 收窄时 Q 路径只有字面召回（仅小库） | `candidates_lexical_only` 如实提示 |
| 在线查询在大库变慢 | 大库收窄直接拒绝；S 路径按整库上限放行；重算分批、有行数与时间上限并标截断；取名与引用有界；各查询钉计划 |
| 模型自己把计数相加或改写 | prompt 要求照抄；结果卡与首行给权威数字；端到端核对 |

## 15. 为什么不做代码沙箱

- 难点在于「什么算一个 topic、同义说法怎么归到一起」，不在算术。图谱已经做了合并，沙箱里对原文写代码多半只能做关键词计数，
  效果更差；如果逐段调模型，成本又不受控。
- 权限只在检索层审核。沙箱要直接读数据，就得绕过或重写这一层。
- 沙箱算出的数字没有出处，与「每个结论都能跳回原文」的回答方式冲突。
- 内网部署要做进程隔离、断网、资源限制与审计，成本远高于加一个工具。

如果以后确实需要更灵活的分析，只考虑受限形态：沙箱只接收检索工具已经返回的表格结果做二次计算，不能直连数据库，也不能联网。

## 16. 性能实测（2026-11-12，本机）

**环境**：M 系列 18 核 / 64 GB；PostgreSQL 16.15（`shared_buffers=128MB`，按 `execute_with_ceiling` 关并行）；SQLite 3.53.2
（CI 是 3.45，计划钉要在 CI 版本上再核）。PG 用全部真实迁移建表；SQLite 只建了相关的表与同名索引。
**以 PostgreSQL 的数字为准**：生产与测试库都是 PostgreSQL，SQLite 只是镜像后端，表里的 SQLite 列仅作参考。

**合成数据**（按生产量级）：大库 1 万篇可见来源 + 5 篇 Memory 来源，201 万 concept + 200 万 claim，195 万簇行，
56.7 万个 canonical。其中 10% 的概念落在 2 000 个头部泛概念上（Zipf 分布），第 1 名 8 228 篇来源 / 17 381 条，第 80 名 298 篇；
其余均匀分布在 60 万个长尾 canonical 上。对象按来源顺序写入，同一 canonical 的成员在堆上是分散的（与真实摄取相同）。
小库 300 篇来源、5 万 concept（正好在 S 路径门槛上）+ 5 万 claim。`knowledge_objects` 3.6 GB、`concept_clusters` 754 MB。

| 形态 | PG | SQLite | 说明 |
|---|---|---|---|
| 大库整库在线聚合（被禁止的形态） | 1.7 s，顺序扫 37 万页（2.9 GB） | 1.5 s | 印证 §2：不能放在提问路径上 |
| P 路径读预计算 80 行 | 0.1 ms | — | 主键前缀读 |
| 读 1 万个可见来源 id（预计算屏蔽孤儿用） | 1.2 ms | 1.7 ms | |
| S 路径，5 万对象，未收窄 | 22–32 ms | 34 ms | |
| S 路径，勾选 250/300 篇 | 17 ms | 39 ms | |
| S 路径，勾选 20/300 篇 | 3 ms | 3 ms | |
| Q 路径重算，常见主题（10 个中段头部 + 90 个长尾，约 1 800 成员行） | 3–12 ms | 4–6 ms | |
| Q 路径重算，最坏（100 个候选全是头部泛概念，11.6 万成员行） | 0.27–1.23 s | 0.23 s | PG 首次触达 1.23 s、再次 0.27 s |
| 同上，加重算覆盖索引 | 0.14 s | — | 上限 5 万行时只能重算 6 个候选，故上限改为 20 万 |
| 取名，本页 20 个头部概念 | 0.2 ms | — | |
| 取引用，20 个头部概念，每个扫 50 个成员 | 1–2 ms（4 千页） | 2 ms | |
| 同上，每个扫 200 个成员 | 4–8 ms（1.6 万页） | 8 ms | 冷缓存下页数决定耗时，故上限取 50 |
| 预计算取前 80（numpy，60 万个 canonical） | 4 ms | — | 在图谱分析那一趟里，可忽略 |

**结论**：

- 按 §4.1 第 0 行拒掉「大库 + 勾选部分来源」之后，提问路径上的每种形态在常见情况下都在几十毫秒以内。
- 唯一可能到秒级的是「带主题词、候选全是头部泛概念」的重算。由 20 万行上限与 1.5 秒时间上限兜住，超出就截断并告知；
  加覆盖索引后最坏情况在 0.15 秒左右。
- 模型调用：工具本身不调大模型；带主题词时一次 embedding。每调用一次占推理循环一轮（本来每轮就调一次模型），
  每次问答最多 1–4 次（按档位）；合成提示词里的排行行占用现有结构化证据预算，提示词总长不变。

**没测到的**：

- 冷磁盘：本机没有权限清空操作系统页缓存，表里的数是 PG 缓冲区冷、操作系统缓存热的情况。冷磁盘下耗时主要取决于随机读的页数：
  重算最坏情况不带覆盖索引约 11.6 万次随机读，可能超过时间上限而被截断；覆盖索引把这部分读集中到一个约为主键 2 倍大小、
  更容易常驻缓存的索引上。
- Q 路径的检索本身（`_retrieve_scored`）：与普通提问同一条检索路径，生产上已有，没有另测。
- 生产的真实分布：头部泛概念的实际规模以生产数据为准，上线前在生产只读副本上用 §9 的同一组查询复测一次。

测量脚本与数据生成 SQL 在本机 `/tmp/rankperf/`，不入库。
