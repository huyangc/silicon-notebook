# 论文卡片、文档级检索与逐篇抽取设计

状态：草案，用户已拍板 §1；§9 的默认项待确认
日期：2026-10-28
基线：`master` `e5d5bd619`（行号以此为准，实现时以符号名为准）

## 1. 背景与已拍板事项

用户常问：「请列出这个笔记本中所有与 X 相关的论文，包括主要技术指标和关键创新点」。现状答得差，
根因是工具缺口而不是 prompt（调研见 §2）。本设计补四件事，**一轮做完**：

| # | 能力 | 内部检索 Agent | MCP |
|---|---|---|---|
| A | 论文卡片（缓存的逐篇结构化要点） | `get_paper_cards` 动作 | `get_paper_cards` 工具 |
| B | 文档级检索 `find_sources` | `find_sources` 动作 | `find_sources` 工具；`list_sources` 补论文元数据字段 |
| C | 临时字段逐篇抽取 | `extract_from_sources` 动作 | `extract_from_sources` 工具 |
| D | 表格确定性渲染 + LLM 只写综述 | 合成阶段 `answer_layout="paper_table"` | 不涉及（外部模型自行组织） |

用户裁决（2026-10-28）：

1. **范围**：A–D 四件一次做完，不留延后项。
2. **卡片只在用到时懒生成**：不在入库流水线里生成，不做存量回填任务。
3. **模型绑定复用 `paper_metadata`**：不新增 workload，生产 `[bindings]` 不用改。
4. **指标数值必须原文出现**：每条指标的数值归一化后必须能在它引用的原文元素里找到，否则丢弃并记审计；
   文字字段（问题、方法、创新点）只要求挂上合法的原文元素引用。
5. **MCP `read` 档也可以触发卡片生成**（会消耗模型配额，接受）。
6. **卡片只覆盖论文**：`source_paper_meta.is_paper=1` 的来源才生成；非论文在 `find_sources` 里照样能
   被找到，要点走 `extract_from_sources`。

## 2. 现状（设计的出发点）

**内部检索 Agent**（reasoning 模式，JSON reflect 循环，不是原生 function calling）：

- 文档名录 `enumerate_*` + `collection="sources"`（`collection_enumeration.py:1236`）**不能过滤**；
  reflect 每轮只看到 ≤20 个标题；合成内联 ≤100 行、每篇摘要 ≤200 字。
- `search_chunks` 每次召回 16 个片段，每次运行最多调用 3 次；`read_document` 每次运行最多读 4 篇，读的是
  按预算采样的原文，不抽取结构化字段，也没有并行。
- `source_paper_meta` 已有 `venue/pub_year/doi/keywords` 与 `source_authors`，但 Agent 只读到
  `is_paper/paper_title`（`app/domain/source_display.py`）；元数据里**没有摘要字段**。
- 规模只影响检索车道（ANN、FTS 降级），不影响动作集合与名录上限。

**MCP**（21 个工具，响应 12KB 封顶）：

- `list_sources` 能分页拿全量，但不返回作者/年份/会议，也不暴露仓库已有的 `q` 过滤。
- `search` 是子串 `LIKE`、上限 20、不能限定来源；`ask(output="evidence")` 不能限定来源，每次还要调一次
  模型做问题理解。
- 没有按 `source_id` 读原文或章节的工具，也没有任何批量工具。

**可复用的现成零件**：

- 单篇检索：`section_retrieval.section_chunk_similarities` / `section_chunks`（`section_retrieval.py:643/698`），
  按单篇加载向量、带 LRU、成本与库大小无关。
- 单篇采样：`document_source_overview.prepare_source_overview`（`:120`），`spread/opening` 两种，带生成版本见证。
- 受限来源的片段检索：`RetrievalCandidates._retrieve_chunks(..., allowed_source_ids=)`（`retrieval_candidates.py:3546`），
  大库走 ANN（带来源过滤回调）或 FTS 降级。
- 「模型填槽位、代码渲染行」的先例：`document_guide.render_document_guide`（`document_guide.py:94`）。
- 论文元数据的接地校验：`paper_meta.verify_paper_meta`。

## 3. 总体流程

```
用户：列出所有与 X 相关的论文 + 指标 + 创新点
  │
  ▼ reflect 第 1 轮
find_sources{query:"X 的语义描述", terms:["X","x 的别名"], filters…}
  │  → 排序后的论文列表（p1…pN 句柄）、强相关/相关计数、召回是否触顶
  ▼ reflect 第 2 轮
get_paper_cards{sources:["p1"…"p10"]}
  │  → 已缓存的直接读；缺失/过期的懒生成（有界并发、单次上限）
  ▼ （可选）reflect 第 3 轮
extract_from_sources{sources:[…], fields:["在 COCO 上的 mAP"]}
  │  → 卡片没有的字段：单篇内检索 + 小模型抽取 + 数值接地
  ▼
answer{answer_layout:"paper_table"}
  │
  ▼ 合成
代码渲染：覆盖说明 + 论文表（每格带 [kN]）+ 未展开的相关论文清单
LLM 只写：总体综述与跨论文对比（引用同一套 kN）
```

MCP 侧把同样的三个服务原样暴露成工具，由外部模型自行编排（§7）。

## 4. 论文卡片（A）

### 4.1 卡片内容

```json
{
  "tldr":        {"text": "…", "anchors": ["<element_id>", …]},
  "problem":     {"text": "…", "anchors": […]},
  "method":      {"text": "…", "anchors": […]},
  "contributions": [{"text": "…", "anchors": […]}],          // ≤5
  "metrics": [{                                              // ≤12
      "name": "top-1 accuracy", "value": "85.2", "unit": "%",
      "setting": "ImageNet-1k val", "compared_to": "ViT-B 81.8",
      "anchor": "<element_id>"
  }],
  "datasets": [{"name": "ImageNet-1k", "anchors": […]}]      // ≤8
}
```

- 长度上限：`tldr` ≤300 字，`problem/method` 各 ≤400 字，每条贡献 ≤200 字；超出由校验截断，不丢字段。
- 每张卡片去重后最多 24 个 anchor，超出按字段顺序保留：`tldr` → `metrics` → `contributions` → `method` →
  `problem` → `datasets`；被裁掉 anchor 后变空的条目按 §4.2 规则丢弃。
- 字段一律可空；模型对原文没有的内容写 `null` 或空列表（prompt 与 `paper_meta_prompt` 同一口径：不许凭记忆补）。
- schema hint 写成 `PAPER_CARD_SCHEMA_HINT` 常量，示例里**不写 `null`**、空列表只用于字符串列表字段
  （`json-hint-strict-alignment-merged` 的教训），并纳入 `test_model_schema_hint_snapshots`。

### 4.2 接地校验（零 LLM，纯函数 `verify_paper_card`）

- **引用合法**：每个 `anchors`/`anchor` 必须是本次输入采样里出现过的元素 id；非法 id 删掉。
- **文字字段**：清掉非法 anchor 后一个都不剩的字段（或贡献条目）整条丢弃。
- **指标**：`anchor` 合法 **且** `value` 归一化后出现在该元素**送给模型的那段文本**里（表格截断后的文本，
  不是全文元素）。归一化规则：
  全角转半角；去千分位逗号；`85.20` 与 `85.2` 视为相同（去小数尾零）；`%`、`×`、`x` 单位与数字分开比对；
  数值必须以完整数字 token 匹配（`85.2` 不能命中 `185.23`）。`name/setting/compared_to` 不做接地，只截断长度。
- 被丢弃的条目和原因写进 `raw_json.dropped`（与 `paper_meta` 同一审计信封形状）。
- 校验后整张卡片字段全空 → 视为生成失败（§4.5）。

### 4.3 输入采样

单次调用的输入不超过 `PAPER_CARD_INPUT_CHARS`（默认 16000 字）。采样确定性、可复现：

1. 前 40% 预算给开头（`coverage="opening"`：标题、摘要、引言通常在这里）。
2. 余下 60% 里，先放表格元素（`element_type=table`，按文档顺序，单表截断到 1500 字，最多 6 张）——
   技术指标多数在表里。
3. 剩余预算按 `spread` 均匀采样全文其余元素（含最后一个元素，结论常在末尾）。

实现复用 `prepare_source_overview` 的分页读取与生成版本见证；需要新增一个按元素类型挑选的读取函数时，
放在 `document_source_overview.py` 同一模块，不另起读取路径。输入块里每个元素带元素 id 前缀，
供模型回填 `anchors`。

### 4.4 存储：`source_paper_cards`

| 列 | 类型 | 说明 |
|---|---|---|
| `source_id` | text PK | FK → sources，`ON DELETE CASCADE` |
| `notebook_id` | text | FK → notebooks，`ON DELETE CASCADE` |
| `basis` | text | 内容版本：来源元素生成见证（`source_element_generation` 同一聚合 `MAX(source_elements.created_at)`），**统一归一成 UTC ISO 字符串**再存再比——现有 PG 实现按会话时区渲染，直接存会在时区变化时让全部卡片失效 |
| `card_version` | integer | prompt/schema/校验规则版本号；代码常量 `PAPER_CARD_VERSION` |
| `status` | text | `ready` / `failed` |
| `card` | jsonb | §4.1 的校验后内容（`failed` 时为 `{}`） |
| `raw_json` | jsonb | 审计信封：`dropped`、输入元素数与字数、失败原因码 |
| `model` | text | 生成所用模型 |
| `attempts` | integer | 连续失败次数（成功清零） |
| `created_at` / `updated_at` | timestamptz | |

- 索引：`(notebook_id)`。
- **有效性在读取时判定**：`status='ready' ∧ basis=当前见证 ∧ card_version=PAPER_CARD_VERSION ∧ 来源仍是论文`
  才算命中；否则当作缺失，懒生成会覆盖写。所以重新解析、重新分类、升级 prompt 都不用写清理代码。
- **显式清理**只有两处：`clear_paper_meta`（重新分类成非论文时）同一事务里删卡片；来源/笔记本删除走 FK 级联。
- **失败冷却**：`status='failed'` 且 `updated_at` 在 `PAPER_CARD_RETRY_SECONDS`（默认 3600）内的不重试，
  直接返回 `failed`；`attempts` ≥3 后冷却加长到 24 小时。避免每次提问都对同一篇坏文档烧模型。
- **写入竞争**：upsert 是条件写——同 `basis` 同 `card_version` 已是 `ready` 时，`failed` 不得覆盖它；
  `attempts` 在 SQL 里自增（`attempts = source_paper_cards.attempts + 1`），不做读改写。
  加锁时来源恰好被删（锁路径抛 `KeyError`）→ 该篇返回 `not_found`。
- **重建索引**：懒生成路径不与 staged rebuild 交互；正确性只靠 `basis` 见证——重建发布后元素见证变化，旧卡片自然失效。
- 登记口径（缓存表，不跨环境、不进归档）：
  - 同步清单 `LOCAL`（各环境自己懒生成，免去变更捕获触发器）；
  - shadow 清单 `LOCAL_EPHEMERAL`（迁移后元素时间戳渲染不同，复制过去也全部失配，不复制）；
  - 不进复制/导出/导入闭包（`ARCHIVE_TABLES`、`_COPY_SNAPSHOT_QUERIES`、`notebook_copy_writer`），
    相关守卫里的「不复制」名单显式列入；
  - 笔记本删除：A 类（随 notebooks 行级联，进第 3 阶段计划，`EXPECTED_PHASE_3_TABLE_COUNT` 69→70）；
    **不是** `source_paper_meta` 那种归档输入表；
  - `merge_dbs.py` 归入笔记本作用域表；
  - PG 时间戳列登记 `_store_utils._TIMESTAMP_COLUMNS_BY_TABLE`。

### 4.5 生成服务 `PaperCardService`

纯函数部分放 `app/services/paper_cards.py`（prompt、schema hint、`verify_paper_card`、采样计划），
编排放 `PaperCardService`（与 `SourceIngestionService.ensure_paper_metadata` 同层）：

```
get_cards(items: list[(owning_notebook_id, source_id)], *, generate_limit, cancel_event, actor_id) -> list[CardResult]
CardResult.status ∈ ready | generated | not_paper | meta_pending | failed | deferred | not_found
```

- **调用方先过天花板**：传进来的每个 `source_id` 必须属于本次运行的来源天花板（问答）或凭证白名单内主人可读的
  笔记本（MCP）；Agent 直接写的 source_id 不在天花板内 → 拒绝该篇（账本说明），与 `read_document` 只接受名录内来源同一口径。
- 按**所属库**分组后批量读缓存（每库一条 SQL，按 `notebook_id` 收窄，同 `e5d5bd619` 口径）；挂载库与引用库按各自所属库读写。
- 对缺失或过期的：
  1. 没有 `source_paper_meta` 行且 `doc_type` 合格 → 用一个**可取消的元数据抽取变体**（接收 `cancel_event`，
     取消、队列超时/满原样上抛，不吞成 `failed`）先抽元数据；这一步**计入 `generate_limit`**，超出的篇返回 `deferred`。
     镜像笔记本（只读同步副本，写入会抛 `MirroredNotebookError`）**不写元数据**，直接返回 `meta_pending`。
     元数据抽取的每种返回值都显式映射：`stored`→继续判论文、`not_paper`→`not_paper`、
     `skipped/disabled/no_llm/no_text`→`meta_pending`、`failed`→`failed`（不写卡片行）。
  2. 论文 → 进入生成队列，**元数据与卡片合计本次最多 `generate_limit` 次模型调用**，超出的返回 `deferred`（调用方可再调一次）。
  3. 卡片与元数据都是由原文确定性派生的缓存，不含提问者的任何信息，所以**读者也可以为他有读权的挂载库/引用库
     写入缓存**（§9 第 6 条，待确认）；只有镜像笔记本不写，卡片缺失时返回 `meta_pending`。
- 并发：`ThreadPoolExecutor(max_workers=min(n, model_clients.parallelism("paper_metadata"), PAPER_CARD_MAX_WORKERS=4))`
  + `contextvars.copy_context().run`；任务前与每个结果后 `raise_if_cancelled`；
  `chat_json(..., cancel_event=)`；数据库读写包在 `retrieval_fanout_slot()` 里。
- 优先级：继承调用方的 `model_work_scope`。问答里是 INTERACTIVE；MCP 工具显式进入
  `model_work_scope(priority=INTERACTIVE, actor_id=凭证主人, notebook_id=…)`。
  队列等待超时（INTERACTIVE 30 秒）或 `ModelQueueFull` → 本篇返回 `deferred`，**不记失败**。
- 单飞：进程内按 `source_id` 加锁，并发请求同一篇时后来者等待前者结果；等待期间轮询取消、不占
  `retrieval_fanout_slot` 也不占线程池工位（在提交前判定、等待放在池外）。跨进程重复生成可接受（条件 upsert 兜底）。
- 事件：每篇写一条 `paper_card` 事件（`source_id`、`status`、丢弃计数、耗时），只记 id 与类名，不记正文。

## 5. 文档级检索 `find_sources`（B）

### 5.1 参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `query` | string | 主题的自然语言描述，用于语义腿；可空（纯过滤） |
| `terms` | list[string] ≤6，每个 ≤64 字 | 字面词/别名/缩写，用于元数据腿；可空 |
| `year_from` / `year_to` | int \| null | 按 `pub_year` 过滤 |
| `venue` | string | 会议/期刊子串 |
| `author` | string | 作者名子串 |
| `kind` | `"papers"` \| `"all"` | 默认 `"papers"`：`is_paper=1`，**加上**还没有元数据行但 `doc_type` 合格的来源（懒生成、不回填，元数据可能还没抽；这类结果 `card_status=meta_pending`）；`"all"` 含非论文 |
| `scope` | `"all"` \| `"current_notebook"` | 与 `enumerate_*` 同义（含勾选的引用库） |
| `cursor` | string | 翻页 |

`query` 与 `terms` 至少一个非空，或至少一个过滤条件非空。

### 5.2 来源集合与两条腿

**来源集合**（两条腿共用，先于一切匹配确定）：

- 问答里与 `enumerate_sources` 同一来源：`_participants` + `scope_source_plan` + `scope_ceiling_digest`
  （`collection_enumeration.py:1287-1300`），即本次运行冻结的勾选天花板与参与库（挂载/引用库按所属库分组）；
  天花板下推（`current_source_scope()` 存在、列表为空）时按 `verify_unbound_read` 口径事后校验读到的来源。
- 可见性谓词与 `list_sources_page` 一致：用户可见类型、提升来源规则、他人 Memory/Knowhow 投影不可见
  （`postgres/source_store.py:470-482`）。
- **MCP 没有运行作用域**（`_chunk_source_ceiling` 与 `filter_retrieval_items` 在无作用域时不收窄），所以 MCP 路径
  必须显式传 `allowed_source_ids = all_visible_source_ids(nb) ∪ hidden_source_ids(nb, 凭证主人)`，
  否则别人的 Memory 投影片段会出现在 `best_passage` 里。`extract_from_sources` 同样适用。

**元数据腿**（每个库一条 SQL，PG 为规范定义）：在 `sources.title/file_name/summary`、
`source_paper_meta.paper_title/venue/keywords`、`source_authors.name` 上对 `terms` 做 `lower(col) LIKE` 子串匹配；
结构沿用 0048 的「每腿各自以 `notebook_id=` 起头、UNION 后再取 id」写法，并重复部分索引的谓词以便命中。
- 0048 的三个 trigram 索引是**可选的热路径索引**（`--apply` 才建），生产不一定有；`summary`、`venue`、`keywords`
  本来就无索引。**先在生产规模样本上差分测量**（49k 来源、单库 10k），有索引与无索引两种情况都测；超过 50ms 再加
  `HOTPATH_INDEX_SPECS` 里的索引，不预先加。
- `%`/`_` 必须转义（现有 `list_sources_page` 没转义，新 SQL 不沿用）。
- C collation 下 `lower()` 只折叠 ASCII：非 ASCII 词按原样匹配，规格接受这一点（中文无大小写）。
- 记录每篇命中了哪些字段，以及命中是否是**词边界匹配**（短 ASCII 词如 "GAN" 子串命中 "organ" 不算词边界）。

**语义腿**：新端口 `retrieve_chunks_in_sources(nb, query, recall=FIND_SOURCES_CHUNK_RECALL=400, allowed_source_ids=…)`，
内部走 `_retrieve_chunks`，大库自动走 ANN（来源过滤回调）或 FTS 降级。按 `source_id` 分组，每篇取
`best = max(relevance)`（`_retrieve_chunks` 返回的是关键词+语义融合后的 `relevance`，不是余弦）、`hits = 命中片段数`，
并记录这次走的是哪条车道（`baseline/ann/fts_degraded`）。有过滤条件时先用元数据确定 `allowed_source_ids` 再跑语义腿。

### 5.3 合并与分档

- 每篇得到：`lexical_fields`（命中字段集合及是否词边界）、`best`、`hits`、车道、最佳片段。
- **分档**（不暴露浮点分数，避免逐字快照跨平台漂移）：
  - `strong`：标题/论文标题/关键词**词边界**命中，或 `best ≥ FIND_SOURCES_STRONG_RELEVANCE`；
  - `related`：其他字段命中、非词边界命中，或 `best ≥ FIND_SOURCES_RELATED_RELEVANCE`；
  - 其余丢弃。
- 阈值按车道分开：`fts_degraded` 车道只有词面融合，语义阈值不适用，只按元数据命中 + `hits ≥ 2` 进 `related`。
- 阈值是配置项，初值由 §8 的校准脚本在**当前嵌入模型**下算出；文档写明「更换嵌入模型后需重跑校准脚本」。
- 排序：档位 → `lexical_fields` 数 → `best` → `hits` → `pub_year` 降序 → `source_id`（完全确定，可翻页重算）。
- 纯过滤（无 `query`、无 `terms`）：全部入 `strong`，按 `pub_year` 降序、标题、id 排。

### 5.4 返回

```
items[]: handle(p1…), source_id, title, pub_year, venue, authors(≤3), tier(strong|related),
         reasons[title|paper_title|keywords|summary|author|venue|passages:N],
         best_passage{chunk_id, text≤240}, card_status(ready|missing|not_paper|failed)
counts: strong, related, filtered_scope_total
coverage: lexical_complete(bool), semantic_recall_capped(bool), next_cursor
```

- `lexical_complete=true`：过滤后范围内每篇都被元数据腿检查过。
- `semantic_recall_capped=true`：第 400 个召回片段仍高于 `related` 阈值，可能还有漏网的论文。
  这时 Agent 必须在回答里说「可能还有更多」，不得声称已列全（与 `prompts.py:1533` 的完整性规则同一口径）。
- 每页 20 篇（Agent 与 MCP 相同）；每次调用重算排序，翻页靠游标里的偏移量 + 参数指纹，参数变了游标作废。
  翻页会重跑 400 个片段的语义腿，所以 Agent 侧**每翻一页都计入本次运行的 `find_sources` 次数**。
- MCP 的 `best_passage` 需要 `ref`：现有 ref 只有元素类型，没有片段类型，按 `evidence_page.py:55-60` 的做法
  把片段映射到它的首个元素再出 ref。

### 5.5 权限

只在检索层收窄（`retrieval-owns-permissions`）：来源集合按 §5.2 确定，`get_paper_cards` 与 `extract_from_sources`
接收的每个来源也在服务入口按同一集合校验。呈现层不做二次权限判断。
MCP 侧的集合 = 凭证白名单内、主人当下可读的单个笔记本的「可见 ∪ 主人自己的隐藏来源」，每次调用重读凭证。

## 6. 临时字段抽取 `extract_from_sources`（C）

- 参数：`sources`（句柄或 source_id，≤8）、`fields`（≤4 条，每条 ≤80 字，例如「在 COCO 上的 mAP」）。
- 每篇：
  1. 全篇 chunk id 取自 `section_outline`（上限 4000 行，超出的长文档只在前 4000 行里选，账本注明）。
     `section_chunk_similarities` 对多条查询取的是最大值，所以**每个字段单独调用一次**，各取前 4 个片段，
     去重后 ≤12 个；该篇没有向量（返回 `{}`）时回落到 `chunk_fts_search(..., allowed_source_ids=[该篇])`。
     再加上开头 2 个元素（标题/摘要）作上下文。
  2. 一次 `paper_metadata` 模型调用，schema：`{"values":[{"field_index":0,"value":"…","anchor":"<chunk_or_element_id>"}]}`。
  3. 校验：`anchor` 必须是输入里的 id；值里含数字的按 §4.2 的数值接地规则校验，不通过就丢弃并记审计。
- 并发、优先级、取消、队列超时处理与 §4.5 相同；单次上限按档位（§8）。
- **不缓存**：临时字段是问题相关的，每次运行重新算。
- 适用于非论文：不检查 `is_paper`。
- **引用**：每个抽到的值只登记它自己的那一个 anchor（片段先映射到元素），与卡片字段走同一个证据段（§7.2），
  不放进 `state.chunks`——片段的合成保留席位每次运行只有 6 个，还要给 `read_section` 用，装不下。

## 7. 呈现

### 7.1 Agent 动作（reflect）

- 新增三个核心动作 id：`find_sources`、`get_paper_cards`、`extract_from_sources`；
  嵌套参数键同名。每个动作有自己的门控（关闭时 prompt 行、schema 分支、白名单一起消失），
  并按 §8 的每次运行上限计数。
- 参数全部用字符串或字符串枚举（`kind`、`scope` 不用布尔）；句柄 `p1…` 由 `find_sources` 发放、
  运行内有效，Agent 也可以直接写 source_id。
- reflect 顶层新增可选字段 `answer_layout: "prose" | "paper_table"`（进 `REFLECT_SCHEMA_TOP_LEVEL_FIELDS`，
  与卡片/抽取门一起门控：两个门都关时 schema 字节不变）。**任何一轮都可以写**，取最后一次写的值——
  运行可能因步数上限或 `sufficient=true` 结束而没有显式 `answer` 动作。默认 `"prose"`；
  只有本次运行确实有卡片或抽取结果时才接受 `paper_table`，否则回落 `prose` 并在账本里写明原因。
- 三个新动作 id 与 `answer_layout` 成为保留名：仓库外插件若有同名动作会注册失败，更新说明里写明。
- 每轮反馈账本：`find_sources` 记计数、覆盖标记和前 10 个句柄与标题；`get_paper_cards` 记每篇状态；
  `extract_from_sources` 记每篇每字段是否抽到值。
- reflect prompt 增加一段用法（只在门开时出现）：
  遇到「列出所有与 X 相关的论文/文档并给出要点」→ 先 `find_sources`（`terms` 写字面词和缩写，`query` 写语义描述）→
  对 `strong` 档 `get_paper_cards` → 卡片缺的字段用 `extract_from_sources` → `answer_layout="paper_table"`；
  `semantic_recall_capped` 或还有未展开的论文时不得声称已列全。
- 试验/经验/轨迹词表：`RETRIEVAL_ACTIONS` 末尾追加三项；`STEP_TYPES` 增加对应步骤；
  前端 `reasoning-trace.ts` 与 `profile-model.ts` 补中文标签。

### 7.2 证据与引用

- 新证据段 `PAPER_CARD_KEY_BASE=9000`（9000–9999，卡片字段与抽取值引用的元素；最多 40 张卡 × 24 = 960 个 key）。
  放在 9000 而不是 8000，是为了不压缩 `read_document` 现有的 3000 个 key 余量（它的可用性判定改为「≤9000」，
  余量 2000）。
- 需要同步的地方：三处段常量（`ask_service`、`ask_evidence`、检索侧）；`ask_evidence.kind_for_key`；
  `AskEvidenceKind` Literal（`models/ask.py:1303`）与 api_contract 夹具、MCP evidence 的 `kind` 文档；
  `_assemble_reasoning_context` 里的第四段分区（`ask_service.py:4889-4900`）；报告复原证据的 `_REPORT_KEY_BANDS`
  （`ask_service.py:1332`）。
- **预算与登记分开**：
  - **登记**：表格要用到的全部 anchor 元素都登记进 `id_map` / 引用卡，**不占**结构化侧的提示词预算——
    与外部证据的处理方式相同（`ask_retrieval_policy.py:84-93`）。元素文本按 id 批量回读（新增按元素 id 跨库读取的端口，
    按所属库分组）。
  - **提示词**：给 LLM 的只是每篇一段紧凑摘要（标题、年份/会议、`tldr`、前 2 条贡献、前 3 条指标，带 key，约 600 字），
    在结构化侧预算内按「knowhow → 枚举预览 → 卡片摘要 → 文档读取」拼装；装不下的篇只在表格里出现，摘要里不出现。
  - 渲染仿照 `document_read_answer.document_read_prompt_block`：返回正文、`evidence_by_id` 与引用卡，失败整体不拼。

### 7.3 确定性论文表（D）

`answer_layout="paper_table"` 时，在 `_answer_reasoning` 里模型正文生成之后、`_parse_answer_anchors` 之前，
由代码拼出最终正文：

```
> 共找到 N 篇相关论文（强相关 a 篇、相关 b 篇），其中 M 篇展开了要点。[可能还有更多：…]

<LLM 写的综述与对比>

| 论文 | 年份 / 会议 | 主要技术指标 | 关键创新点 | <临时字段…> |
|---|---|---|---|---|
| 标题 [k8001] | 2024 / CVPR | top-1 85.2%（ImageNet）[k8003]；… | …[k8005] | … |

其余相关论文（未展开）：标题1、标题2、…
```

- 每格最多 3 条指标、2 条创新点，单格 ≤240 字；单元格里的 `|` 与换行转义。
- 内联最多 `paper_table_rows` 行（§8），超出部分进「其余相关论文」清单（只列标题，最多 100 个）。
- `render_paper_table` **只渲染已登记进 `id_map` 的 key**；没登记上的 anchor 不出标记。测试断言表里每个 `[kN]`
  都能解析回元素。
- LLM 的合成 prompt 被告知「表格由系统生成，你只写综述与跨论文对比，可引用表中同一批 kN，不要重复列表」。
- sectioned 合成路径（`_answer_reasoning_sections`）：卡片证据只放在第 0 节的 `id_map`（偏移 0），表格只出现一次、
  放在第一节之前；第 1 节以后的提示词里不给卡片摘要（`_keep_only_section_markers` 会删掉跨节的 key），
  跨论文对比只由第 0 节写。
- 没有逐 token 流式，最终正文在 `AskResponse` 组装前确定，流式事件、落库与引用卡一致。

### 7.4 MCP

| 工具 | 档位 | 参数 | 说明 |
|---|---|---|---|
| `list_sources` | read | 不变 | 每项新增 `authors`（≤3 个名字）、`pub_year`、`venue`；复用 `sources_from_rows` 已经填好的字段，不加查询 |
| `find_sources` | read | `notebook_id`、§5.1 全部参数（`scope` 除外）、`offset`、`limit≤20` | 返回 §5.4；`best_passage` 带 `ref`，可传给 `read_reference` |
| `get_paper_cards` | read | `notebook_id`、`source_ids`（≤3） | 返回每篇状态与卡片；单次最多 3 次模型调用（元数据 + 卡片合计），其余 `deferred` |
| `extract_from_sources` | read | `notebook_id`、`source_ids`（≤3）、`fields`（≤4） | 返回每篇每字段的值与 ref 序号 |

- 三个新工具放进 `mcp_tools/sources.py`（不新增 registrar），工具总数 21 → 24。
- 会调模型的两个工具：在 `_run_with_progress` 里执行，客户端取消时置 stop 事件；同一凭证主人最多 2 个并发
  生成调用，超出返回 `busy`。现有 `_waiter_slot` 是按（主人，幂等键）计数的，这里要**新增按主人计数**的槽。
- **12KB 预算**（`TOTAL_TEXT_LIMIT=12000` 字符）：一个元素 ref 约 100–150 字符，按 anchor 逐个附 ref 会超。
  响应改为顶层去重的 `refs` 表 + 字段里只写序号；每次最多 3 篇；仍超预算时按 `_budget_response` 逐级收缩并报告截断。
- MCP 路径的来源集合按 §5.2 显式传入（无运行作用域）。
- 资源不存在的错误名登记 `mcp_tool_host._NOT_FOUND_RESOURCE`。
- 服务端 `instructions` 增加一段工作流说明：先 `find_sources`，再分批 `get_paper_cards`，必要时
  `extract_from_sources`，用 `read_reference` 复核关键数字。

## 8. 预算（按档位）

`AskRetrievalLimits` 新增 5 个字段（位置构造，五行一起改）：

| 档位 | find_sources 次数 | 卡片读取篇数/次运行 | 卡片生成篇数/次运行 | 抽取篇数/次运行 | 表格内联行数 |
|---|---|---|---|---|---|
| overview | 1 | 6 | 3 | 0 | 6 |
| standard | 2 | 12 | 6 | 6 | 12 |
| deep | 3 | 20 | 10 | 8 | 20 |
| thorough | 3 | 30 | 16 | 12 | 30 |
| exhaustive | 4 | 40 | 24 | 16 | 40 |

- 单次 `get_paper_cards` 最多 10 篇；单次 `extract_from_sources` 最多 8 篇。「卡片生成篇数」统计的是元数据与卡片的
  模型调用合计。`find_sources` 次数包括翻页。
- 以 `paper_metadata` 服务并发 4、单篇约 20 秒估算：standard 档最坏增加约 30 秒（6 篇生成分 2 波），
  第二次问同一主题时直接命中缓存。
- 卡片证据进合成的预算：`chunk_context_chars` 的一半，与枚举预览、`read_document` 共用现有的「结构化侧」预算，
  拼装顺序：knowhow → 枚举预览 → 卡片 → 文档读取。

**测试、校准与验收分三层**：

1. **CI 回归（入库）**：`backend/tests/fixtures/paper_topics/` 下放 40 篇**合成**论文（自写的标题、摘要、方法、
   带数字的结果表，每篇 ≤3KB，无版权问题），6 个主题、每题标注强相关/相关集合；配确定性的假嵌入器，
   测 SQL、分档、排序、游标、权限，不测阈值本身。
2. **阈值校准（本机脚本，不进 CI；全部实现完成后最后做）**：`backend/app/eval/find_sources_calibrate.py`，
   用户裁决的「模型预标 + 只标候选集」做法（2026-10-28）：
   - `candidates`：对用户指定的真实论文笔记本、6 个主题，用宽松阈值跑 `SourceFinder`，每个主题取分数前 60 篇作候选集，
     写出待标注文件（不调 chat 模型，只用嵌入检索）。
   - `prelabel`：用 chat 模型给每个候选预标「强相关 / 相关 / 不相关」。**这一步消耗模型配额，执行前必须征得用户同意**：
     脚本先打印将要发起的调用次数与所用模型，只有带 `--confirm` 才真正调用；主 agent 拿到用户在对话里的明确同意后才加这个参数。
   - 用户核对、改错预标结果（标注文件放仓库外的本机经验目录，不提交）。
   - `sweep`：扫描两条线，选出满足「候选集内主题召回率（强+相关）≥0.9、强相关精度 ≥0.8」的阈值，写回 `app/core/config.py`，
     PR 描述贴曲线与所用嵌入模型。候选集之外的漏网论文统计不到，召回率是候选集口径的近似值，文档写明。
   - 校准之前，配置里先放保守的临时值（偏低的相关线、偏高的强相关线），并依赖 `semantic_recall_capped` 与
     「可能还有更多」提示兜底。
3. **真模型端到端验收**：本机 deepseek-flash 跑「列出与 X 相关的论文 + 指标 + 创新点」6 题，人工核对表内数字
   全部能在引用原文找到。

## 9. 待确认（默认按下列执行，开工前可改）

1. **chunk 模式不接入**：chunk 模式没有 reflect 循环，这类问题在 chunk 模式下行为不变；MCP `ask` 默认 reasoning。
2. **`extract_from_sources` 的 MCP 档位跟 `get_paper_cards` 一样是 `read`**（沿用 §1 第 5 条的裁决）。
3. **失败冷却**：1 小时，连续 3 次失败后 24 小时（§4.4）。
4. **卡片不跨环境同步、不进导出/复制**：目标环境用到时重新生成。
5. **表格是否出现由模型通过 `answer_layout` 决定**，不靠关键词判断问题类型。
6. **读者可以为有读权的挂载库/引用库写缓存**（卡片与论文元数据），镜像笔记本除外（§4.5）。不允许的话，
   共享参考库里的论文只有库主人自己问过才有卡片。
7. ~~阈值校准用本机真实论文笔记本手工标注~~ **已裁决（2026-10-28）**：模型预标 + 只标候选集，预标调用前先征得用户同意，
   校准放在全部实现之后做（§8 第 2 层）。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| 懒生成让首问变慢 | 每次运行有生成上限，超出的标 `deferred`，回答里写明「其余 N 篇未展开」；同主题再问时命中缓存 |
| 交互生成与后台元数据回填抢同一服务的并发 | INTERACTIVE 权重 8:2:1；队列超时只降级为 `deferred`，不记失败 |
| MCP `read` 档可以消耗模型配额 | 每次调用生成上限 3 篇 + 每个主人 2 个并发；事件可审计 |
| 指标接地误杀（数值写法不同） | 归一化规则覆盖常见写法；丢弃明细进 `raw_json.dropped`，用验收集统计误杀率 |
| summary/keywords 子串匹配在大库慢 | 先测量再加 trigram 索引；腿内先以 `notebook_id` 收窄 |
| 语义腿召回触顶导致漏论文 | `semantic_recall_capped` 显式上报，Agent 不得声称列全 |
| 证据 key 段扩张撞上 sectioned 合成的步长 | 卡片段 9000–9999 在 10000 步长内；`read_document` 的可用性判定同步改为 9000，部署方若把 `REASONING_MAX_DOCUMENT_READS × DOCUMENT_OVERVIEW_MAX_ELEMENTS` 调到 2000 以上，启动时告警 |
| MCP 路径越权读到他人 Memory 投影片段 | MCP 显式传来源集合（§5.2），测试用他人 Memory 构造用例 |
| 嵌入模型更换后阈值失准 | 阈值按车道区分、校准脚本可重跑，部署文档写明 |
