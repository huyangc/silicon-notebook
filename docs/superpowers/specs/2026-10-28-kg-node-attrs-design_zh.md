# 知识图谱节点属性（attrs）形态 — 设计

2026-10-28。**状态：第一轮评审（6 条 P1）与复审（4 条 P2）都已修订（处置见 §十八、§十九），交用户拍板（§十六）。** 拍板前不实现，也不往节点加字段。**用户已批准（2026-10-28）：D1–D16 全部按推荐，已立项，先做 Gate 0。**

来源：`fangan_todo.md`「### 知识图谱」下的「节点属性 attrs 形态未定」，以及用户 2026-10-19 裁决 16（长期方向四项都先写规格，评审后给用户看）。用户 2026-10-28 同时裁决：推理分层（extends / Level 0–4 / Hypothesis）暂不做；schema 归纳对既有类型提议新字段、refine 抽样都不做。本规格不涉及这三项，只在 §十一 写清 attrs 与它们的接口边界，保证将来要做时不必推翻 attrs。

**数据来源。** 凡标「本机实测」的数，都在本机 PostgreSQL 开发库 `silicon_notebook_local_pg16_live` 上只读测得：全部 10 个笔记本、107 个来源、四类核心对象共 102,099 个。§6.2 的写放大是在一次性库 `silicon_notebook_kgattrs_hot_test` 上测的：只复制了 `knowledge_objects` 一张表（不含同步触发器），测完已删库。探针 SQL 见附录 A。生产规模沿用 [问题清单设计](./2026-10-19-kg-analysis-findings-design_zh.md) 引的基数（878 万对象、4.9 万来源）。**本机没有生产规模的库。** 凡是生产上的代价，都是按本机比例推算的，标「推算」，不得当实测引用。

**修订记录。**
- v1（e263d3ba1）推荐「独立列 + 入库后补全 + 存量回填」。
- v2（本版）按评审改为**零存储**：别名在读的时候派生，公式符号说明由用户在详情里按需触发。存储方案降为备选（D13），并补上实测的写放大数据。逐条处置见 §十八。

## 一、结论先行

1. **只做两个字段**：Concept 的 `aliases`（别名）和 Formula 的 `variables`（符号说明）。其余五个候选全部拒绝，理由见 §四。
2. **推荐零存储方案**：
   - **别名在读的时候派生**（D11）：R1–R3 是「名字 + 证据元素原文」的纯函数。两个详情出口本来就读出了经查看者范围过滤的元素原文，在这一步顺带算出别名即可。不存储、不迁移、不回填、不改复制和同步；改名和治理合并之后结果自动一致；权限口径在构造上就与出处一致。
   - **公式符号说明按需补全**（D12）：用户在公式详情里点「识别符号说明」，系统对这一个公式调一次模型，每一条都在原文里核验，核验不过的丢弃。结果不入库，再次打开时尽力命中模型响应缓存（§7.3 缓存语义）。
3. **问答热路径零改动**：派生和补全只挂在两个界面详情出口上（`/objects/{id}/context` 与 `/concepts/{id}/detail`），而且必须显式开启。`evidence_context`、reasoning、`node_context_reader` 这些回答路径既不调用也拿不到 attrs，由静态守卫加运行期探针双重钉住（§7.5）。
4. **主抽取 prompt、`_KG_SCHEMA_HINT`、入库产物、向量、检索、合并种子、簇：全部不变。** 对这些路径来说，本方案的改动是零。
5. **备选（D12、D13 选存储时才用）**：公式符号说明批量补全并存进旁表 `knowledge_object_attrs`（S4），不加在 `knowledge_objects` 上。实测在主表上 UPDATE 4.9% 的行，HOT 只有 0.2%，三个 trigram GIN 一共涨了 9 MB（§6.2）。
6. **先测量，后实现**（Gate 0，§13.1）：每类规则至少产出 100 条，按 Wilson 下界判线，必须有中文切片，由用户在本地标注。字段之间可以部分通过，各自独立上线。

## 二、现状（代码事实）

### 2.1 节点模型与 attrs 的历史

- `backend/app/services/kg/models.py:20-28` `Node`：`id / type / name / section_path / evidence / mentions / steps / validity_scope`。`validity_scope` 只对 Claim/Formula 有意义（行 28 注释）。
- attrs 原本存在：`git show 866d3d930`（2026-06-01，"refactor(kg): remove attrs"）删掉了 `Node.attrs: Dict[str, Any]`，节点文本全部归入 `name`，同时把形态问题登记进 `fangan_todo.md`。
- `scripts/kg_strip_attrs.py` 是当时的一次性脚本，用来剥掉 gold 草稿里的 `attrs`，目标目录是 `fangan/testcases_kg/*/ch*/gold_kg.yaml`。该目录在 `.gitignore:22`，主 checkout 里也不存在（本机 `ls` 实测）。`scripts/README.md:578` 登记了这个脚本。
- 候选字段的出处：`fangan/qiefen.md:723-737` 教材型 Formula 示例带 `variables:{符号: 含义}`；`fangan/article_research_gold_spec.md:161,170` 公式 metadata 可选 `role`；同文件 `:224` canonicalization gold 带 `aliases`。

### 2.2 存储

- PG `knowledge_objects`（`backend/app/repositories/postgres/migrations/0001_initial.sql:359-375`）：`payload jsonb`、`evidence jsonb`、`source_id`、`status`、`ordinal` 等。没有类型化的属性列。
- 本机实测这张表有 11 个索引，其中 3 个是 trigram GIN：`payload->>'name'` 全局一个、按笔记本一个，整段 `payload::text` 按笔记本一个（附录 A）。
- 入库的 payload 由 `kg_ingest.build_records`（`backend/app/services/kg_ingest.py:561`）组装：`{"name", "section_path"}`，有内容时加 `validity_scope`（行 590-592）和 `steps`（行 593-605）。证据条目由 `_ev`（行 291-296）构造，其中 `quoted_span` 截断到 400 字符。
- 本机实测，四类核心对象 payload 的顶层键只有 `name`、`section_path`（102,099 个）、`validity_scope`（3,309 个）和 `steps`（3,245 个）。

### 2.3 payload 的读者：新增键会悄悄改变什么

| 读者 | 位置 | 怎么读 payload | 加入 payload 顶层的新字符串/列表的后果 |
|---|---|---|---|
| 对象向量文本 | `retrieval.py:709-722` `_payload_text`；`source_embedding.py:21-23` | 除 `section_path` 与 `_` 前缀外，**所有字符串值和列表元素**拼接；dict 值跳过 | 向量文本变化，全库要重嵌，ANN 与跨文档合并的向量层随之变化 |
| 关键词集合 | `retrieval_candidates.py:1968`、`:6125`；`retrieval.py:485` | 同一个 `_payload_text` | 关键词命中变化 |
| 关系向量的端点名 | `knowledge_lifecycle.py:1393`、`:1556`；`retrieval_candidates.py:2031-2032` | `_payload_text(...)[:80]` | 端点名截断窗口里混进新文本 |
| 搜索框知识腿 | `postgres/search.py:1185-1204` | `(payload::text) COLLATE "C" ILIKE` | **键名本身**也参与匹配：`aliases`、`variables` 这类词会命中所有带属性的对象 |
| 支撑上一行的索引 | `migrations/0042_hotpath_batch2_search_indexes.sql:302-306` | GIN trigram 建在 `payload::text` 上 | 索引变大 |
| 重建种子流 | `postgres/unified_kg_store.py:381-400` | 整列 `payload::text` 流出 | 每次重建的 IO 变大 |
| 插件问答引擎 | `plugin_ask_engine.py:405-406` | 读 `payload["definition"]` 作摘要 | 顶层 `definition` 会直接改变插件引擎的输出 |
| 知识记录展示 | `ask_service.py:591` `knowledge_record` | 按 schema 字段排序后，其余键用 `str(value)` 展示 | dict 被渲染成 Python repr |
| 图谱详情面板 | `frontend/app/kg-graph-view.tsx:53-58`、`:769-775` | 除 `name`/`section_path` 外的键通用渲染 | 原样露出 JSON |
| 对象编辑 API | `models/knowledge.py:8-11`；store 整列替换 | 客户端提交整个 payload | 旧客户端一次 PATCH 就抹掉属性 |

这张表排除了 S1（`payload.attrs`）。

### 2.4 `knowledge_objects` 的整行读者（新增列会被带上）

检索和图侧热路径有多处 `SELECT * FROM knowledge_objects`：PG 的 `postgres/knowledge_store.py:2094`（`usable_object_rows_on`）、`:2318`（`follow_object_rows`）、`:2359`（`retrieval_objects`），SQLite 在对应位置的 `:1863`、`:2014`、`:2067`。如果给主表加列，这些读都会开始搬运并解析 attrs。复制写入器 `notebook_copy_writer.py:1303-1311` 只对 `payload` 和 `evidence` 做 `refs.json` 的 id 重映射，新列里的 element_id 和 source_id 在复制和导入之后会指向旧 id。

### 2.5 详情出口与回答路径共用的读

- **对象详情路由** `GET /notebooks/{nb}/objects/{id}/context`（`api/kg_routes.py:463-475`）→ facade → `KnowledgeQueryService.node_context`（`knowledge_query.py:926-964`）。它按查看者范围算出 `allowed_source_ids`，再调 `node_context_reader`，即 store 的 `node_context`。store 返回的 `occurrences` 已经是 `_enrich_evidence` 读出的元素原文（`element_text`），并且在天花板下按 `_scoped_occurrences`（`postgres/knowledge_store.py:443-447`）过滤：元素实际所属的来源和条目声明的来源都必须在天花板内（codex #806 r1）。
- **store 的 `node_context` 也服务回答路径**：`evidence_context.py:452-461`、`reasoning_retrieval.py:4667`（`name_only=True`）、`repository_runtime.py:2107`（`node_context_reader`）。这些调用都直接打到 store 或 retrieval 层，**不经过** `KnowledgeQueryService.node_context`。
- **簇成员探测** `_node_context_cluster_sql`（`postgres/knowledge_store.py:386-432`）只在两个条件同时成立时才执行：有天花板，并且 `canonical_description!=''`（行 2665-2680）。所以它**不是**一条在每次详情都会跑、可以顺带复用的成员读。
- **统一图谱的 Concept 详情**走 `GET /concepts/{id}/detail`（`kg_routes.py:440-460`）→ `KnowledgeQueryService.concept_detail`（`knowledge_query.py:525`）。它用 keyset 分页，每页最多 `CONCEPT_DETAIL_PAGE_MAX = 200` 个成员（`domain/knowledge_contracts.py:108`）。「按成员作答」的分支（#1020，`_member_answered_detail`）同样落到 `_concept_detail`（行 592）。`_concept_detail` 在本页成员上调一次 `_viewer_resolved_evidence`（行 689-741），一次 `_enrich_evidence` 读回本页全部证据的元素原文，并已经按查看者范围过滤（Memory 查看者、#1020 的挂载非成员、B-11 的外库元素定位符）。它返回的是**打平的**富化列表，没有成员 id；不过输入列表是按 `member_ids` 顺序逐成员拼出来的（`knowledge_query.py:906-910`），所以可以无 SQL 地对齐回成员（§7.2）。
- 图谱面板选中 Concept 时，前端**同时**请求概念详情和对象详情（`frontend/app/use-kg-graph.ts:582`、`:601`），都带 `owns(owner)`、requestId、选中节点三重守卫。概念详情的「加载更多」（`use-kg-graph.ts:626-640`）把新页的顶层字段整个覆盖，只拼接 `members`、按 id 去重 `attached`。
- **本机实测**：存储的 `quoted_span` 只有 400 字符。「名字 (缩写)」形态在完整元素原文里命中 2,017 个 Concept，在 `quoted_span` 里只命中 1,263 个，占 63%。所以别名派生必须读完整元素原文，而上面两个出口恰好已经读到了。

### 2.6 抽取 prompt、JSON hint 与严格投影

- 主抽取：`kg/extract.py:255` `_prompt`（固定部分 5,404 字符，本机实测）+ `_KG_SCHEMA_HINT`（行 70-76，351 字符）。gleaning 复用同一 prompt 与 hint（行 435-456）；refine 只看 `[i] type: name`（行 401）。
- 严格投影：`core/model_schema.py:1-14` 说明，「占位键 map」与开放对象无法闭合，**整个 hint 会退回 JSON mode**。`_STRING_LIST_FIELDS`（行 27-30）之外的空列表示例同样无法投影。会话记忆 `json-hint-strict-alignment` 的规则是：示例里不写 `null`，非字符串列表的空列表要给一个示例项。
- 形状校验：`core/model_json.py:579-584` 只对 `frame` / `validity_scope` 放行「嵌套对象可以是 `{}`」。
- `backend/tests/test_model_schema_hint_snapshots.py` 用 AST 发现所有字面 hint 并钉住投影摘要。
- 模型响应缓存：`LLM_CACHE_ENABLED` 默认开，默认 TTL 90 天、上限 2 GiB（`core/config.py:686-691`）。
  - **只有带 `response_validator` 的调用才读写缓存**（`core/llm.py:907-920` 读、`:1128-1139` 写）。
  - 键是 `llm_key(model, 完整 messages, hint, base_url, temperature, top_p, max_tokens, thinking_mode, response_format)`（行 900-906）。
  - 命中前要用本次的校验器重新判一次；`bypass_cache=True`（行 814、888）既不读也不写。
  - 缓存是本机文件，按大小淘汰，主要被 KG 抽取写入占用。

### 2.7 canonicalize 与跨文档合并

- 同文档：`kg/canonicalize.py:15-48` 只合并 Concept（按本文件的 `_norm`，行 8-13：NFKC、小写、去非字母数字，**不经过任何别名表**）和 Procedure。Claim/Formula 原样保留。入库只绑定 `node.evidence`（`kg_ingest.py:582-589`），`mentions` 不落库。
- 跨文档：`kg_merge.py` 按种子聚类，再加向量层。种子用的是 `domain/kg_merge_seed.py` 的 `_norm`（行 70-80）：会经 `_ALIASES`（行 26-35，硬编码 8 条，如 `vco → voltage controlled oscillator`）把缩写折成全称。批内还会从「Full (ACR)」名字推出首字母缩写映射（`kg_merge.py:179-228`，`_is_initialism_of` 在 `kg_merge_seed.py:44`）。凡改变种子语义都必须 bump 聚类代码版本（`kg_merge.py:58-60` 注释、`:97`）。

### 2.8 「定义」类信息已有三条来源

- **簇融合描述** `concept_clusters.canonical_description`（`0001_initial.sql:151`）：重建的描述阶段（`knowledge_lifecycle.py:6229`）**只对多成员簇**生成（行 6263-6266），prompt 在 `prompts.py:119`；`kg rewrite-descriptions`（#1030）可以整库重写。
- **`defines` 边**（Claim → Concept）：`node_context` 的定义回落读它（`postgres/knowledge_store.py:476-507`）。
- **证据片段**：前两条都没取到时，回落到第一条出处原文（`evidence_context.py:1414-1423`）。
- 本机实测：17,245 个 Concept 簇里 2,665 个是多成员簇，其中 2,593 个有融合描述；2,231 个 Concept 有 `defines` 边。

### 2.9 公式与公式元素

- KG 解析的元素类型含 `formula`（`domain/kg/parsing.py:28`）。OMML（#1035）与 MinerU 公式块带 `metadata.text_format = "latex"`（`services/parsers.py:1123`、`:1487`、`:1887`）。
- 本机实测：13,289 个 Formula 对象的首条证据里，294 个落在 `formula` 元素上（都是 latex），12,722 个落在 `paragraph` 上（行内公式）。13,292 个公式只落在 10,769 个不同的证据元素上，约 19% 的上下文是重复的。
- 符号说明（「where …」「其中」「式中」）只出现在**公式所在元素本身**的有 482 个（3.6%）；算上**紧随其后的那个元素**是 2,000 个（15.0%）。
- **「同一来源的下一个元素」怎么取**（v2 此处有误，复审更正）：
  - 两端都有 `idx_source_elements_source_created (source_id, created_at, id)`（PG `migrations/0003_core_indexes.sql:29`，SQLite `repositories/sqlite/migrations.py:1013`），PG 另有 `(source_id, element_type, created_at, id)`。
  - 但不能按 `(created_at, id)` 取下一个：同一次解析的元素 `created_at` 相同，元素 id 是 `el-{source_id}-{index:04d}`（`source_ingestion.py:1467`、`:1808`），超过 9,999 个元素时按字符串排序会排错（`-10000` 排在 `-1001` 之前）。本机 10,767 个公式里有 365 个（3.4%）按两种排序取到的「下一个」不同（复审实测）。
  - 所以 PG 用全局自增的 `ordinal`（唯一索引 `uq_source_elements_ordinal`）。
  - SQLite 的 `source_elements` **没有 `ordinal` 列**（`sqlite/migrations.py:655-663`），等价写法用隐式 `rowid`：`replace_elements`（`sqlite/source_store.py:1989-2040`）先删后用 `executemany` 按元素顺序插入，同一次插入内 `rowid` 单调递增。另外三处插入路径（`sqlite/maintenance.py:538`、`:1987`，`sqlite/migrations.py:5569`）实现时逐一核对是否按顺序插入。SQLite 文档不保证 `VACUUM` 之后没有显式 INTEGER PRIMARY KEY 的表的 `rowid` 不变（实践中保持相对顺序）。SQLite 是镜像端，接受这一点，并加用例钉住「解析后 rowid 序 = 元素序」。
  - 顺带记录一个**既有**排序问题（不在本规格范围，协调方另行登记）：`source_elements()` 与 `source_elements_after` 按 `(created_at, id)` 排序，元素超过 9,999 个的来源会排错。

### 2.10 展示、导出、同步、Knowhow

- 内置四类的 `ObjectSchema.fields` 都只有 `["name", "section_path"]`（`domain/extraction_profiles.py:53-86`）。`LIST_FIELDS`（行 21-28）里有一个历史上的 `aliases`，供自定义类型使用。
- `.snbk` 导入时 `archive_reader.check_columns`（行 375-392）会拒收「本部署没有的列」；表集合由 `ARCHIVE_TABLES` 钉住（`domain/notebook_archive.py`），`FORMAT_VERSION = 1`（行 47）。
- Knowhow 投影（`services/knowhow/projection.py:1-40`）把表格的每个格子投影成一个知识对象，`object_type` 是**列名**，按 `(table_id, column, value_key)` 先删后插。列名恰好叫 `concept` 或 `formula` 时，产出的对象会被当成核心类型。
- MCP 工具里**没有**对象详情或概念详情的出口（`api/mcp_tools/` 里 grep `node_context`、`concept_detail` 都是零命中）。

### 2.11 模型绑定

`model_registry.binding_gap`（`services/model_registry.py:530-551`）明确写着：功能开关**不**豁免绑定。因为开关在运行时翻转，绑定表只在启动时读一次，所以功能关闭的工作负载也必须绑定，这是用户裁决「有没配置的就失败」的字面执行。严格模式下有未绑定的工作负载就拒绝启动（行 492-494）。

### 2.12 评测与 gold

- `fangan/testcases_kg/` 在 `.gitignore`，没有策展后的 KG gold。
- 仓库里**有**解析层 gold：`fangan/testcases/{cmos,engram,harness}`，都是英文。cmos 是公式密集的教材。
- `app/eval/sa_calibration.py` 是抽取 prompt 新旧对照工具，有 `validity_scope_fill_rate`（行 96-100），并在 `evaluate_gate` 的说明里写明「不要对它迭代 prompt」（行 71-82）。
- `app/eval/recall_gold.yaml` 与会话记忆 `scoped-chunk-retrieval-fts-only` 的 24 题对照集都只能在本机手动跑，不进 CI。

### 2.13 本机实测汇总（候选字段的信号）

| 信号 | 计数 | 占比 |
|---|---|---|
| Concept 名字本身是「Full (ACR)」形 | 1,119 / 23,826 | 4.7% |
| Concept 首条证据 `quoted_span` 含「(ACR)」括注 | 2,965 / 23,826 | 12.4% |
| Concept 名字后紧跟括号（完整元素原文 / `quoted_span`） | 2,017 / 1,263（共 23,720） | 8.5% / 5.3% |
| Concept 首条证据含「also called / 简称 / 又称 / i.e.」等 | 558 / 23,826 | 2.3% |
| Concept 名字归一后恰好是 `_ALIASES` 的键 | 21 | — |
| Formula 自身或下一元素含「where / 其中 / 式中」 | 2,000 / 13,289 | 15.0% |
| Claim 名字含「数字 + 单位」 | 1,317 / 61,658 | 2.1% |
| Claim 名字含否定词 | 2,945 / 61,658 | 4.8% |

这些是用正则做的**上界粗估**，只用来判断哪个字段值得做，不是落地率。落地率由 Gate 0 测。

## 三、目标与非目标

**目标**

1. 定下 attrs 的字段集合、产出方式、权限口径和评测方式，使 `fangan_todo.md` 这一条可以关闭。
2. 在不改变任何检索、合并、回答和搜索结果的前提下，让界面详情能显示「别名」和「符号说明」。
3. 每一条属性都能追溯到查看者能读到的具体元素原文。

**非目标**

- 让 attrs 影响检索、种子合并、回答上下文或搜索框（D8，另行立项）。
- 改主抽取 prompt 或重抽存量。
- 推理分层、schema 归纳的字段提议、refine 抽样（2026-10-28 裁决不做；边界见 §十一）。
- 允许用户编辑 attrs。
- MCP 出口（今天没有对象详情类工具；要加属于新范围，并受 MCP 输出预算约束）。

## 四、候选字段逐一评估

| 字段 | 价值证据 | 可靠性 | 成本 | 对合并的影响 | 读者 | 结论 |
|---|---|---|---|---|---|---|
| Concept `aliases[]` | 8.5% 的 Concept 在完整元素原文里写成「名字 (缩写)」（§2.13）；今天缩写只在聚类批内临时推出（§2.7），详情里看不到；`fangan_todo` 的「焦点只按存储的簇名解析」也是同一缺口 | **高**：确定性规则，复用 `_is_initialism_of` | 读时派生：零存储、零模型调用 | 不进种子 | 详情面板；将来的焦点解析与搜索（D8） | **做** |
| Concept `kind` | 没有读者；`kind_of` 边已表达分类 | 中：边界词表难定 | 要占用主抽取输出 | 跨文档成员不一致时要表决 | 无 | **拒绝** |
| Concept `definition` | 已有三条来源（§2.8）；多成员簇 97% 已有融合描述 | 中：逐窗口抽取会与融合描述冲突 | 每个 Concept 多 100–300 字 | 多份定义要融合，等于把描述阶段再做一遍 | `node_context` 已有定义回落 | **拒绝**。单成员簇没有融合描述是真缺口，应该在描述阶段解决 |
| Claim `quantitative_values{}` | 只有 2.1% 带数值与单位；数值本来就在 `name` 文本里；没有按数值比较的消费者 | 中：单位归一与条件绑定容易错 | 低 | 无 | 无 | **拒绝**。等有了数值冲突检测或数值筛选的设计再考虑 |
| Claim `polarity` | 否定不等于极性（「降低」「不变」）；`contrasts_with` 与冲突裁决已覆盖对立关系 | 低 | 低 | 无 | 无 | **拒绝** |
| Formula `variables{}` | 公式名是 LaTeX，不说明符号就看不懂；15% 的公式在相邻元素里写了含义（§2.9） | **中高**：模型配对加原文核验（§7.3） | 按需：只花在用户点开的公式上 | 不参与 canonicalize；跨文档种子按表达式 | 详情面板；将来的公式检索（D8） | **做**，改成列表形态，因为占位键 map 无法严格投影（§2.6） |
| Formula `role` | 与 `validity_scope.approximation`、`derived_from` 边重叠 | 低：定义式、推导式、近似式、经验式的边界模糊 | 低 | 无 | 无 | **拒绝** |

## 五、数据形态（出口上的形状）

零存储方案下，attrs 只是两个详情响应里的一个可选字段，在服务层按请求现算：

```json
// concept（对象详情 / 概念详情）
"attrs": {"aliases": [{"text": "VCO", "element_id": "el-…"}]}

// formula（按需补全接口的响应）
"attrs": {"variables": [{"symbol": "g_m", "meaning": "跨导", "element_id": "el-…"}]}
```

- 每条只带 `element_id`，指向本次响应里查看者可读的一个出处元素，**不带 `source_id`**（出处元素本身已有来源信息，评审 P2-6）。
- **上界**（具名常量，放进 `app/domain/kg_attrs_contracts.py`）：`aliases` ≤ 8 条，每条 ≤ 80 字符；`variables` ≤ 24 条，`symbol` ≤ 32 字符，`meaning` ≤ 200 字符。超出部分按出现顺序截断。按 v1 的写法，单行最坏约 7 KB（24 × (32 + 200×3 字节 UTF-8 + 键与 id 约 80)）。零存储方案下这只是响应大小；只有 D13 选存储时才影响行宽。
- 空集合时**不返回 `attrs` 键**。
- **硬约束**：attrs 里不放任何指向其他对象的 id。节点之间的关系只走 `knowledge_relations`（§十一）。
- **类型注册表** `ATTR_SPECS: Mapping[object_type, AttrSpec]`，只登记 `concept` 和 `formula`。
- **范围**：Knowhow 投影出来的对象不生效（D15）。判别不发 SQL：看对象 id 前缀 `ko-kh-`（`services/knowhow/ids.py:11` `_cell_ko_id`）。Knowhow 投影的 `object_type` 是列名，「列名恰好叫 formula」不该被当成公式。Memory 派生的对象照常生效，可见性跟随既有的查看者范围。
- `models.Node` **不加字段**，`KnowledgeRecord` **不加字段**（评审 P3：否则 `related_knowledge` 的每一条都会多一个 null）。

## 六、存储形态

### 6.1 推荐：不存储（D11 + D12 的推荐组合）

别名读时派生，公式符号说明按需补全并只进模型响应缓存。于是：

- 不迁移，不触碰 `knowledge_objects`。§2.4 的 `SELECT *` 读者、复制写入器的 id 重映射、`.snbk` 的表与列集合、同步触发器、`schema_manifest`：全部不变。
- 不需要回填，也没有「入库后再 UPDATE」的写放大。
- **重算语义天然成立**：attrs 是（名字，查看者可读的证据元素原文）的纯函数。改名、治理合并、冲突「修改」、`update_knowledge` PATCH 之后，下一次读自动得到新结果，没有失效路径要维护（评审 P2-3）。
- 代价：每次打开详情多一段纯 Python 计算（§十二）；公式第一次补全要等一次模型调用。

### 6.2 只有选存储时才适用：实测写放大

在一次性库里复制 `knowledge_objects`（102,099 行，用 COPY 装载，fillfactor 默认 100，相当于批量入库后的状态），然后：

- `ALTER TABLE … ADD COLUMN attrs jsonb NOT NULL DEFAULT '{}'`：在 `lock_timeout = 3s` 下瞬时完成（PG 11+ 的常量默认值只改元数据）。
- 两条 UPDATE 写入 4,965 行（4.9%）：2,965 个带括注的 Concept，加 2,000 个随机公式。共 0.48 s。

| 结果 | 数值 |
|---|---|
| HOT 更新 | **9 / 4,965（0.2%）**：整页装满，几乎每次更新都要重插全部 11 个索引 |
| `idx_knowledge_objects_nb_payload_trgm` | 24.2 MB → 28.9 MB（+19%） |
| `idx_knowledge_objects_nb_name_trgm` | 18.0 MB → 20.2 MB（+12%） |
| `idx_knowledge_objects_name_trgm` | 17.8 MB → 19.9 MB（+12%） |
| 堆 | 94.4 MB → 99.0 MB（+5%） |
| 死元组 | 4,965 |

三个 trigram GIN 的增长主要是 fastupdate 待处理链表。搜索框的知识腿在热路径上要扫这条链表，直到 VACUUM 合并它。生产上还要加同步触发器的每行变更日志（`0064_sync_change_capture.sql:625-655`，测量时去掉了）。**结论：不在 `knowledge_objects` 上做入库后的 UPDATE。** 这排除了 v1 推荐的 S2。

### 6.3 存储方案对比（D13）

| 方案 | 做法 | 结论 |
|---|---|---|
| S1 `payload.attrs` | payload 里加 dict 键 | 排除：§2.3 的读者全会受影响 |
| S2 主表加列 `attrs jsonb` | v1 的推荐 | 排除：§6.2 的写放大；§2.4 的 `SELECT *` 读者要先全部改成显式列；复制和导入的 id 重映射要扩到新列 |
| S3 类型化列 | 每个字段一列 | 排除：同 S2，并且每加一个字段就要迁移 |
| **S4 旁表 `knowledge_object_attrs`**（只在 D12 选「批量补全并存储」时用） | `(object_id PK, notebook_id, attrs jsonb, input_sig, origin)`，外键 `ON DELETE CASCADE` 到对象 | 主表零写入、零索引放大；主表的 `SELECT *` 不会带上它；条目只存文本、不存 id（读时按元素原文重新定位，见 §7.3），所以复制和导入不需要重映射。代价：新表要进 `ARCHIVE_TABLES` 并 bump `FORMAT_VERSION`（旧版本拒收新归档）；进同步清单、复制写入器、删除表清单（`notebook_delete_tables.py`）、`schema_manifest`；两端迁移；`lock_timeout` 写进迁移 |

## 七、产出路径

### 7.1 为什么不进主抽取 prompt

1. **不回退**：主 prompt 和 hint 不变，抽出哪些节点就不变，90 天抽取缓存继续命中。往 5.4k 字符的 prompt 里加规则，会改变节点选择，没有 gold 时无法证明没有回退。
2. **严格模式**：主 hint 保持可闭合（§2.6）。
3. **可独立开关、可独立计价**。

### 7.2 别名：读时派生（确定性规则，零模型调用）

**模块与函数**：

- 派生与核验全部是**模块级函数**，放在一个具名模块 `app/domain/kg_attrs.py` 里（不做成任何类的方法）：`derive_concept_aliases(name, items, *, exclude_names)`、`normalize_latex(text)`、`verify_formula_variables(formula, context, reply)`，以及上界常量与 `ATTR_SPECS`。这个模块不依赖 services 或 repositories。
- 调用方一律写成 `from app.domain import kg_attrs` 再调 `kg_attrs.derive_concept_aliases(...)`，**禁止** `from app.domain.kg_attrs import derive_concept_aliases`。这条由 §7.5 的守卫强制，目的是让运行期间谍 patch 模块属性时，恰好就是调用点实际查找的那个名字。

`derive_concept_aliases` 只在下面两处、并且只在显式开启时被调用（§7.5）：

- **对象详情**：`KnowledgeQueryService.node_context` 增加 keyword 参数 `with_attrs: bool = False`，只有 `/objects/{id}/context` 路由传 `True`。为 `True` 且对象是 concept 时，取 store 返回的 `occurrences`（已经按天花板过滤，`element_text` 是完整元素原文），派生结果放进响应的 `attrs`。
- **概念详情**：`_concept_detail` 在拼 `evidence` 时（`knowledge_query.py:906-910`）同步拼一个等长的 `owners` 列表（每条证据属于哪个成员 id）。`_viewer_resolved_evidence` 增加一个返回值：与它返回的富化幸存条目一一对应的 owner 列表。做法是对 `raws[:own]` 和 `owners` 用同一个 `unreadable` 判定过滤，**不新增 SQL**。然后对**本页**每个成员，用它自己的富化幸存条目派生别名，挂在 `members[i].attrs.aliases` 上。
  - **不返回顶层 `attrs`**，因为「加载更多」会整个覆盖顶层字段（§2.5）。
  - 前端从已加载的全部 `members[*].attrs.aliases` 现算并集：按 casefold 计数，去重，按次数降序、文本升序排列，截断到 12 条。
  - 标题写「别名（已加载成员）」；第一页已经覆盖全部成员（`next_cursor` 为空）时去掉括注。
  - 合并规则写进 `use-kg-graph.ts` 同一处的注释，并加 vitest。
- **两份同时到达时显示哪一份**：图谱面板选中 Concept 时两份都会请求（§2.5）。界面显示概念详情的并集（覆盖已加载的全部成员）；概念详情 404 或不适用时（如从知识浏览器打开的单个对象），才显示对象详情的 `attrs.aliases`。

**排除**：与成员自己的名字相同的别名，以及与 `canonical_name` 相同的别名，都要排除（后端在两处都做，比较用 `canonicalize._norm`）。

**规则**（按顺序，去重；比较与去重用 `kg/canonicalize.py:8-13` 的 `_norm`。**不能**用 `kg_merge_seed._norm`：它会经 `_ALIASES` 把 `VCO` 折成全称，导致「别名等于名字」的判断把真别名丢掉。本机名字归一后恰好是 `_ALIASES` 键的 Concept 有 21 个；评审报的 231 个可能是按「包含」计，口径不同，但无论哪个口径都必须用无别名表的 `_norm`）：

- **R1**：名字本身匹配 `_PAREN_ACRONYM_RE`，且括号内是括号前的首字母缩写（`_is_initialism_of`）→ 别名是括号内的缩写。来源是名字。`element_id` 取**本次响应里第一条可读、且 `element_id` 非空的出处**。没有这样的出处时，比如出处都被 B-11 清掉了定位符（`element_id` 为 ""），别名照常返回，但不带 `element_id` 键，因为名字本身已经可见。R2–R4 只在 `element_id` 非空、并且原文取自本库元素的条目上运行；被清掉定位符的条目只剩存储时的 400 字摘录，所以跳过。
- **R2**：元素原文里出现「`<名字>` (ACR)」或全角「`<名字>`（ACR）」，且 ACR 是名字的首字母缩写 → 别名 ACR。
- **R3**：名字是一个 ASCII 缩写，元素原文里出现「Full (名字)」，且名字是 Full 的首字母缩写 → 别名 Full。
- **R4（CJK，D4 可选）**：名字含 CJK，元素原文里名字后面紧跟「(ACR)」或「（ACR）」，ACR 匹配 `^[A-Z][A-Za-z0-9-]{1,7}$`，并且不是实例标号（`^[A-Z]{1,2}\d+$`，如 M1、Q5）→ 别名 ACR。

**不变量**（评审 P3 已改正 v1 的冲突写法）：R1 的别名是**名字**的子串；R2–R4 的别名是**某个出处元素原文**的精确子串，且该元素就在本次响应、查看者可读的出处里。所有别名都不等于名字本身。

「又称 / also known as」写法只占 2.3%，确定性规则难做精，本规格不覆盖（D4 选项 c 可以覆盖，但它会把别名变成模型产物，丢掉读时派生的全部好处）。

### 7.3 公式符号说明：按需补全（推荐）与批量补全（备选）

**按需（推荐，D12-a）**：

- **触发**：公式详情里的「识别符号说明」按钮。
  - 按下后按钮立刻进入「识别中…」状态并禁用，结果显示在按钮正下方（CLAUDE.md 的按钮规则）。
  - 请求在飞时用户切换了节点：沿用 `use-kg-graph.ts` 的 `owns(owner)`、requestId、`selectedNodeIdRef` 三重守卫，过期的响应丢弃，并写进前端测试。
- **接口**：`POST /notebooks/{nb}/objects/{id}/formula-variables?source_notebook_id=…`，与对象详情同签名，支持挂载库。
  - 依赖 `require_notebook_read`，只接会话鉴权，**不**挂 `deps.py:791` 起的「会话或 Agent token」依赖。这与 Ask 的 REST 路由一致：Agent token 只走 MCP。
  - 因为会触发模型调用，所以用 POST。
- **上下文**：
  - 先调 `KnowledgeQueryService.node_context` 取查看者范围内的出处（与详情同一套范围），取第一条 `element_id` 非空的出处的元素原文。
  - 再取同一来源里的下一个元素，按 §2.9 的理由用元素序，不用 `(created_at, id)`：
    - PG：`SELECT n.id, n.text FROM source_elements n WHERE n.source_id=%s AND n.ordinal > (SELECT ordinal FROM source_elements WHERE id=%s) AND n.ordinal <= (SELECT ordinal FROM source_elements WHERE id=%s) + 64 ORDER BY n.ordinal LIMIT 1`。因为 `_enrich_evidence` 不返回 ordinal，所以用主键子查询取。EXPLAIN 钉住：子查询走 `pk_source_elements`，外层走 `uq_source_elements_ordinal` 的有界范围扫描，再按 `source_id` 过滤。
    - SQLite：同形写法，把 `ordinal` 换成 `rowid`。
    - 64 的窗口：`ordinal` 是全局自增的，并发入库交错写入时，下一个元素可能落在窗口之外，这时取不到，退化为只用第一条出处。
  - 下一个元素的来源也必须在查看者范围内，否则只用第一条。
  - 两段合计截断到 `KG_ATTRS_CONTEXT_CHARS`（默认 1,200）。
- **调用**：单个公式一次调用，工作负载见 D6。并发去重：同一 `(notebook, object, 上下文散列)` 在飞时，后来的请求等待同一结果。每个用户同时最多一个在飞的识别请求（D12）。
- **缓存语义**（复审 P2-2）：
  - 调用带 `response_validator`，所以会读写模型响应缓存（§2.6）。校验器规则：根是 dict，`variables` 是列表（**接受 `{"variables": []}`**），每项是含字符串 `symbol` 和 `meaning` 的 dict。校验器**只判形状，不判核验**，所以「原文没写」和「全部被核验拒绝」的回复都会被缓存。同一 prompt 重问只会得到同一份回复，缓存它可以避免重复花钱。
  - 状态映射固定为四种：
    - `ok`：至少一条通过核验；
    - `none_stated`：模型返回空列表；
    - `none_grounded`：模型返回了条目，但全部被核验拒绝；
    - `failed`：调用异常、回复不合法，或模型未绑定。这种回复不会被缓存。
  - **只有 `failed` 提供「再试一次」**：失败的回复从未入缓存，重试自然会真调一次，不需要 `bypass_cache`。`none_stated` 与 `none_grounded` 不提供重试入口，否则重试只会命中同一份缓存。
  - 再次打开时**尽力**命中缓存：缓存有 2 GiB 上限且是本机文件，被淘汰或换机器后会重新调用一次模型。
- **不入库。**

**prompt 要点**：只抽原文**明确写出**的符号含义；`meaning` 逐字照抄原文，不翻译、不改写；原文没写就返回空列表。

**回复 hint**（新的字面 hint，进快照测试）：

```
{"variables":[{"symbol":"","meaning":""}]}
```

示例给了一项，可以严格投影，示例里没有 `null`。批量备选的 hint 是 `{"items":[{"i":0,"variables":[{"symbol":"","meaning":""}]}]}`，`i` 沿用 KG 边端点的整数规则。实现以快照测试的投影结果为准；如果退回 JSON mode，就是设计错误，要改 hint，不能接受。

**核验**（评审 P2-1 修订；全部通过才保留一条，拒绝原因计数）：

1. **LaTeX 归一化** `N(·)`，对公式名、上下文、`symbol`、`meaning` 都同样处理：
   - 先剥包装命令，保留内容：`\mathrm{X}`、`\text{X}`、`\mathit{X}`、`\operatorname{X}`、`\boldsymbol{X}` → `X`；
   - 希腊字母命令与 Unicode 互映（`\alpha` ↔ `α`，大小写各 24 个），统一成 Unicode；
   - 去掉 `$`、`\(`、`\)`、`{`、`}`、剩余的 `\` 和空白；`_{m}` → `_m`；
   - NFKC 加 casefold。
2. `N(symbol)` 是 `N(公式名)` 的子串，也是 `N(上下文)` 的子串。
   - 拒绝原因：`symbol_not_in_formula`、`symbol_not_in_context`。
3. `N(meaning)` 是 `N(上下文)` 的子串。
   - 拒绝原因：`meaning_not_in_context`。
4. **邻近与配对**：在 `N(上下文)` 里，这条含义的出现位置与这个符号的某次出现位置相距不超过 `KG_ATTRS_PAIR_WINDOW`（默认 120 个归一化字符）。并且本次回复的所有符号里，离这条含义最近的就是它。这一条挡住「where a is X, b is Y」里把 X 配给 b 的错配。
   - 拒绝原因：`too_far`、`nearer_other_symbol`。
5. 同一公式内按 `N(symbol)` 去重，先到者留；超过上界的截断。
   - 拒绝原因：`duplicate`、`over_cap`。
6. 记录含义落在哪个元素上，写进该条的 `element_id`。

Gate 0 输出这些拒绝原因的分布（§13.1）。

**批量（备选，D12-b，只在用户选「预先算好」时）**：与 v1 相同的入库后批量补全，加上评审要求的修订：

- 按证据元素合并后再分批（本机可省约 19% 的上下文）；
- 结果写进 S4 旁表，条目只存 `symbol`/`meaning`，读出时按 §7.3 第 6 条在查看者可读的元素里重新定位，所以不需要 id 重映射；
- `offline_batch_active` 期间不运行；
- 检查点用 `kg_rebuild_checkpoint`（stage `attrs_enrich`，version = origin）。清理策略：一次运行完成后删掉本 stage 的行；启动时删掉本 stage 下其他 origin 的行；`kg_reset_epoch` 移动时整段丢弃；
- 元素按来源批量读，不做 N+1；
- 重算语义：输入签名是（名字，全部证据元素 id，上下文散列），任何一项变化都视为过期，读出时不返回；`update_knowledge`、治理合并、冲突「修改」（它和名字写入不在同一个事务，`knowledge_governance.py:898-906`）之后都由签名比对自动失效，不靠写钩子修剪。

### 7.4 失败语义

- 按 §7.3 的四种 status 显示，日志只记原因码和异常类名，不带原文：
  - `none_stated` 与 `none_grounded` 都显示「原文没有写明符号含义」，没有重试按钮；
  - `failed` 显示「这次没有识别成功」，并提供「再试一次」。
- 别名派生是纯函数，不会失败；遇到异常就当作没有别名并记录类名，详情照常返回。

### 7.5 回答路径隔离（评审 P1-1、P3）

- **静态守卫**（AST，放在 `backend/tests/`）：
  1. `app.domain.kg_attrs` 只能以 `from app.domain import kg_attrs` 的形式导入，任何 `from app.domain.kg_attrs import …` 都会变红。
  2. `kg_attrs.<函数>` 的调用点只能出现在白名单函数里：`KnowledgeQueryService.node_context`、`KnowledgeQueryService._concept_detail`、按需补全服务 `app/services/kg_formula_variables.py` 的入口方法。匹配按**解析后的导入目标**做，不按裸名字，防止同名函数绕过。
  3. `with_attrs=True` 字面量只能出现在 `kg_routes.object_context` 里；按需补全服务只能被它自己的路由引用。
  4. `evidence_context.py`、`reasoning_retrieval.py`、`repository_runtime.py`、`graph_retrieval.py`、`retrieval_service.py`、`retrieval_candidates.py` 不得导入 `kg_attrs` 或按需补全服务。
- **防挪移变异**：守卫的测试里自带三种变异用例，每一种都必须变红：
  - 把派生调用挪进白名单模块里一个新的、不在白名单里的 helper；
  - 在回答路径模块里用别名导入；
  - 用 from-import 绕开模块属性。
- **运行期探针**：
  - 间谍 patch 在 `app.domain.kg_attrs` 的模块属性上。因为守卫 1 强制调用点都写成 `kg_attrs.<函数>(…)`，调用时按模块属性查找，所以 patch 的就是调用点实际用到的名字；from-import 导致间谍恒为 0 的情况被守卫 1 排除。
  - **正向对照**：同一夹具里先调一次对象详情和概念详情路由，断言间谍计数 ≥ 1。
  - **负向断言**：再跑一整轮问答（reasoning 加 deterministic），断言计数没有增加。
  - 另加一个用例：`KnowledgeQueryService.node_context(with_attrs=False)` 的返回与改动前逐字段相同。

## 八、合并、治理与权限

- **同文档 canonicalize 与跨文档合并**：不读 attrs，`cluster_code_version` 不 bump。
- **治理合并 / 冲突「修改」/ PATCH 改名**：零存储方案下无事可做，下一次读自动重算（§6.1）。
- **来源范围（天花板）**：别名与符号说明只用两个出口**已经**按 `_scoped_occurrences` 规则过滤过的元素（元素实际所属来源和声明来源都在天花板内，#806），以及同一套范围下的 `_viewer_resolved_evidence`。后者覆盖 Memory 查看者过滤、#1020 R2 的挂载非成员、B-11 外库元素定位符。符号说明的「下一个元素」另外要求其来源在查看者范围内。权限只在检索层（这里就是这两个读出口）判定，符合会话记忆「检索层负责权限」。
- **Knowhow**：按 D15 排除。**Memory**：随对象可见性。

## 九、读出口与前端

**后端出口**（都是新增的可选字段或新接口，不改变既有字段）：

1. `GET /objects/{id}/context`：Concept 带 `attrs.aliases`（只在路由显式开启时计算）。
2. `GET /concepts/{id}/detail`：只有 `members[i].attrs.aliases`，没有顶层 attrs；并集由前端算（§7.2）。
3. `POST /objects/{id}/formula-variables?source_notebook_id=…`：按需补全，返回 `attrs.variables` 与 `status`（`ok` / `none_stated` / `none_grounded` / `failed`）。只接会话鉴权。
4. 字段契约写进 `docs/product-and-api(_zh).md`。**不做 MCP 出口**（§三）。

**明确不进**：检索候选、`evidence_context` 证据行、向量、关键词、搜索框、问题清单、主题板块、预览、`KnowledgeRecord`、`related_knowledge`。

**前端**：新文件 `frontend/app/kg-attrs.tsx`，导出 `KgAliasesView` 和 `FormulaVariablesPanel`（不放进 `page.tsx`；page.tsx 不能具名导出）。

- 图谱详情面板（`kg-graph-view.tsx` 选中节点区，类型标签下方）：Concept 显示「别名：」加一行药丸；Formula 显示「识别符号说明」按钮，结果是两列小表，符号用既有的 `LatexText isFormula` 渲染。
- 知识浏览器：只在它已经请求对象详情的展开态里显示，不为此新增请求。
- 属性为空时不渲染标题。键名一律走 `Object.hasOwn`。新界面词登记进 `docs/ui-vocabulary.md`：「别名」「符号说明」「识别符号说明」。

## 十、存量迁移

零存储方案**没有迁移**：存量对象在下一次被打开时就有别名，公式在用户第一次点击时补全。不重抽，理由同 §7.1。

备选 S4 下才需要：两端各一号迁移（先看 origin/master 的最新号，按会话记忆「并行分支迁移号会撞」处理），加 `kg enrich-attrs` 回填命令（`--notebook` / `--all` / `--dry-run` 估价 / `--limit`，形状同 `kg rewrite-descriptions`）。

## 十一、与推理分层、schema 归纳、refine 抽样的接口边界

三项都按 2026-10-28 裁决**不做**。本节只保证将来做时不推翻 attrs：

- **Level 0–4**：如果将来做，是某个节点类型的一个标量属性。在 `ATTR_SPECS` 里加一个键即可。如果它需要存储（模型判定，不能读时派生），就走 §6.3 的 S4 旁表，主表不动。
- **Hypothesis**：新的节点类型，要改 `NODE_TYPES`、`edge_schema` 与抽取规则。attrs 只需要在 `ATTR_SPECS` 加一个条目或不加，不对节点类型集合做任何假设。
- **extends**：是边，进 `EDGE_SPECS` 并 bump `EDGE_SCHEMA_VERSION`，**不进 attrs**。§五 的硬约束（不存对象 id）保证 attrs 永远不会和边争抢同一种语义。
- **schema 归纳对既有类型提议字段**：attrs **不是** `object_schemas.fields`。内置四类的 `fields` 仍是 `name`/`section_path`，schema 编辑器与归纳既看不见也提不了 attrs。将来如果归纳提议字段，那是 `payload` 顶层的用户字段，走既有通用展示；attrs 只在响应里（或 S4 旁表里），两个命名空间不相交，历史上的 `LIST_FIELDS["aliases"]` 也不会混淆。
- **refine 抽样**：refine 在入库前丢节点（`extract.py:393`），attrs 在读时或按需产出，二者没有数据依赖。

## 十二、代价上限

**推荐方案（零存储）**：

- **别名**：每次对象详情，对 ≤ 出处条数的元素原文做几次正则加 `str.find`；概念详情每页 ≤ 200 个成员的证据。没有新增 SQL。本机最大的簇只有 59 个成员（复审实测），凑不出 200 个成员的满页，所以分两层验收：
  - **微基准，给绝对上限**：用合成夹具，200 个成员，每个成员 3 条出处，元素原文按本机 Concept 证据的 P90 长度（1,399 字符）生成，一半带「名字 (缩写)」。对 `kg_attrs.derive_concept_aliases` 在整页上跑 20 次取中位数，必须 ≤ 5 ms；单个对象详情 ≤ 0.2 ms。
  - **路由对照**：用同一合成 hub 夹具（两端各建一次）和一个普通概念，在改动前后各跑三次，p95 的差异不超过 max(5%, 3 ms)。数字贴进 PR。
- **公式按需**：每次点击一次模型调用，输入约 1,200 字符上下文加约 1.2k 字符 prompt，输出很小；重复点击命中缓存。成本与点击量成正比，不会自动花。新增一条有界的 `source_elements` 读，要有 EXPLAIN 钉。
- **模型争用**（评审 P2-7，复审 P3 更正）：
  - 并发按**物理服务**算：每个服务一个调度器、一份 `max_concurrency`（`model_provider.py:838`；部署文档「多个 workload 可以共用一个服务，它们也会共用该服务唯一的调度器和并发预算」）。工作负载只决定绑到哪个服务和调度优先级。
  - 所以 D6-a 的好处是：可以绑到另一个物理服务（真正隔离），或者在同一服务内以交互优先级排在后台抽取前面。它**不**自带独立的并行度。
  - 每用户一个在飞请求（D12）限制了最坏占用。PR 里附「批量入库期间连续点击 20 次」的入库吞吐对照（同一来源集，开与关），吞吐下降不得超过噪声。

**备选（D12-b 批量补全）的推算**（本机语料 1,674 万字符；窗口 4,000–8,000 字符，`config.py:698-699`）：

- **主抽取输入**：约 2,800 个窗口 × 35–40k 字符（首轮加 gleaning 加 refine）≈ 1 亿字符，是推算。
- **公式补全**：按证据元素去重后是 10,769 个上下文 × 约 900 字符 ≈ 970 万字符，加固定 prompt ≈ 1,070 万字符，约主抽取的 **11%**（去重前约 13%）。
- **公式密集的教材**：单来源估约 16–18%，**会超过 15% 上限**。预期的处置是：先收紧上下文截断（1,200 → 800），仍然超出的来源只送证据元素或下一元素带说明提示词的公式，并在事件里标出该来源。
- **生产全量**：按 13% 的公式占比，约 115 万个公式、去重后约 9.3 亿输入字符，这是推算。所以批量只能按笔记本手动回填。

## 十三、评测：没有权威 gold 时怎么证明有效、不回退

### 13.1 Gate 0：实现前的测量（评审 P1-6 修订）

工具：`backend/app/eval/kg_attrs_calibration.py`（手动工具，不进 CI）。它直接 import 产品里的派生与核验函数，不另写一套口径。

**样本与标注**

- **规模按产出计，不按输入计**：每类规则（R1、R2、R3、R4、公式符号说明）**至少产出 100 条**才判线；产出不足的规则判「样本不足」，不上线。脚本按需扩大抽样，直到每类满 100 条或扫完整个库。
- **英文切片**：仓库内 `fangan/testcases/cmos/*`（公式）与 `engram/*`（缩写）。标注存为 `backend/app/eval/kg_attrs_gold.yaml`；这些原文本来就在仓库里。
  - **Concept 名字和 Formula 表达式从哪来**：用产品当前的抽取链路（`kg/extract.extract_window` + `kg/canonicalize.canonicalize`，与 `scripts/kg_goldgen.py` 同一条路径），在各章 `source.md` 上跑一次，得到（名字，证据元素）对。这一步只用来产生待判的样本，模型调用走响应缓存，可以复现。
  - 别名的召回参照：`fangan/article_research_gold_spec.md:224` 那类 canonicalization gold 里已有的 `aliases`，作为参照集的一部分。
  - 待判样本连同产出它们的抽取输出一起入库，保证下次重跑时样本不漂移。
- **中文切片**（必需）：从本机库的中文来源分层抽样，覆盖 R4 和「其中 / 式中」。**由用户在本地标注**：脚本生成 `.local/kg_attrs_gate0/review.html`（只在本机，不进仓库），用户逐条勾选「对 / 错」，脚本读回 `labels.json`。中文原文和标注都不进仓库。
- **对齐键**：`(sha256(NFC(元素原文)), name)`。公式符号说明的条目键再加 `N(symbol)`。gold 不依赖对象 id，重新解析之后仍然能对齐。
- **判定人**：两个切片都由用户判定。模型只做预标注，作为草稿。

**指标与达标线**（按 95% Wilson 区间下界判）

| 指标 | 线 |
|---|---|
| R1–R3 别名精度 | 下界 ≥ 0.90 |
| R4 别名精度 | 下界 ≥ 0.85，否则 R4 不上线 |
| 符号说明配对精度 | 下界 ≥ 0.85 |
| 符号说明召回（gold 里写明了含义的公式，至少 100 个） | 点估计 ≥ 0.70 |
| 产出率（每 100 个 Concept 的别名数、每 100 个公式中有符号说明的个数） | 报告，不设线，用来对照 §2.13 的上界 |
| 拒绝原因分布（§7.3） | 报告；某一种原因占比 > 50% 时，先查规则是否误杀 |

**部分通过时的处置**：别名（R1–R3）、R4、公式符号说明三项**各自独立**判定、独立上线。例如别名过闸而公式没过：交付别名部分（PR-1），公式部分停下，把拒绝原因分布与样本交给用户，由用户决定改规则重测（换一份没见过的样本）还是放弃。`sa_calibration.py` 的纪律同样适用：不许对着同一份样本迭代 prompt。

### 13.2 不回退的证明

推荐方案不写库、不改主抽取和检索代码，所以不回退的证明主要靠**构造加守卫**：

1. **主抽取不变**：`_prompt` 与 `_KG_SCHEMA_HINT` 的投影摘要快照不变；新增的补全 hint 单独快照。
2. **回答路径不变**：§7.5 的静态守卫、变异用例、运行期间谍（0 次调用）；`node_context(with_attrs=False)` 逐字段相同。
3. **两个详情响应只多不少**：既有字段逐字节相同（新旧代码对同一夹具的 JSON 做差，只允许多出 `attrs` 键）。
4. **延迟**：§十二 的详情路由 p50/p95、入库吞吐对照。
5. **检索对照**：可选。推荐方案不碰检索代码，`recall_gold.yaml` 与 24 题对照集只能本机手动跑；跑的话结果附在 PR，不作为门。
6. **备选方案（S4 批量）才需要的逐字节入库对照**：开关开与关各入库一次，`knowledge_objects`、`knowledge_relations`、向量、簇必须逐字节相同。这要求模型输出确定：用 `FakeClient` 或回放录好的响应缓存，不能用真模型直跑。

### 13.3 上线后的观测

- 按需补全事件 `kg_formula_variables`：`status`、`variables_kept`、按原因的拒绝计数、`cache_hit`、耗时。不含原文。
- 别名派生不发事件（读路径、高频），只在异常时记录类名。

## 十四、测试与验收

新测试经 `repo._runtime.<组件>.<方法>`；禁止 skip/xfail。

- **domain 单测**：
  - R1–R4 正例；反例包括实例标号 M1/Q5、「Fig. 3 (a)」、非首字母缩写的括注、别名等于名字、`_ALIASES` 键名的概念（VCO 不得被折掉）。
  - LaTeX 归一化：`\mathrm` 先剥再去 `\`；希腊字母双向；`_{m}`；全角括号。
  - 配对核验：「where a is X, b is Y」的交换配对必须被拒；`too_far` 的边界。
- **出口**：
  - 两端 `node_context` 在天花板下：别名只来自可读出处，`_scoped_occurrences` 的「元素实际来源在外」的情况必须没有别名（PG 泳道）。
  - 概念详情的分页并集：第一页、加载更多、hub 簇；按成员作答（#1020）分支；Memory 查看者；挂载非成员。
  - Knowhow 列名叫 `formula`/`concept` 的对象不出 attrs。
- **按需补全**：
  - 有界的下一元素读：
    - PG 的 EXPLAIN 钉（主键子查询加 `uq_source_elements_ordinal` 范围扫描）；
    - 超过 9,999 个元素的来源上，取到的下一个元素必须是元素序上的下一个（`-9999` 的下一个是 `-10000`）；
    - 来源最后一个元素；下一元素属于范围外来源；SQLite「解析后 rowid 序 = 元素序」。
  - 四种 status 各一个用例；`{"variables": []}` 与全部被拒的回复会入缓存，异常和不合法的回复不会；`failed` 之后再试会真调模型。
  - 并发去重；每用户一个在飞请求；`source_notebook_id` 指向挂载库；Agent token 调用被拒（401）。
  - 前端：按钮按下、禁用、结果就近显示；请求在飞时切换节点，过期响应被丢弃（vitest 加预览台截图）。
- **守卫**：§7.5 的四条静态守卫；三种变异（挪进新 helper、别名导入、from-import）；运行期间谍的正向对照（≥ 1）与问答负向断言（不增加）。
- **概念详情的成员对齐**：`_viewer_resolved_evidence` 的 owner 列表与幸存条目逐一对应，包括有条目被判不可读、被清掉定位符的情况；不新增 SQL（用语句计数断言）。
- **前端并集**：已加载成员别名的计数、去重、排序、截断到 12；排除 `canonical_name`；「加载更多」后并集扩大、括注按 `next_cursor` 切换；两份详情同时到达时显示概念详情那份。
- **微基准**：§十二 的合成 hub 夹具，给出绝对上限。
- **hint**：新 hint 进 `test_model_schema_hint_snapshots.py`，投影为严格模式；`model_json` 形状校验接受 `variables: []`。
- **前端**：`KgAliasesView` 空 / 有 / 「已加载成员」三态；`FormulaVariablesPanel` 四态（未触发 / 识别中 / 有结果 / 无）；原型链键名；`LatexText` 渲染符号。
- **门禁**：`scripts/check.sh`、整条 PG 泳道、`check_backend_extended.sh`、`check_architecture_boundaries.py`（基线零松弛；热函数加语句放在尾注释之前或抽 helper，`_concept_detail` 与 `node_context` 要先看 `function_length_ceiling`）、`check_backend_lint.py`。

## 十五、交付切分

一个计划，按下面的顺序连续交付，范围内同一轮做完，不留延后项：

0. **Gate 0**：校准脚本、英文 gold、本地标注页先入库（独立小 PR），跑出 §13.1 的表交用户。各项独立判定。
1. **PR-1 别名（读时派生）**：`kg_attrs_contracts`、`derive_concept_aliases`、两个出口、守卫与间谍、前端别名、文档。删除 `scripts/kg_strip_attrs.py`，同步删掉 `scripts/README.md:578` 那一行，并按 `check_backend_lint.py` 规则 `--prune`，同一 diff 调低基线。
2. **PR-2 公式符号说明（按需）**：新工作负载（D6）、补全 hint、按需接口、核验、有界元素读、事件、前端按钮、发布说明（用户可见：详情面板的两项新内容）。完成后把 `fangan_todo.md` 该条移进 `fangan_done.md`。

Gate 0 中某项没过，对应的 PR 就不做，由用户另行决定。D12 选批量时，PR-2 改成 S4 迁移加批量补全加回填命令，§13.2 第 6 条变成门。

## 十六、需要拍板

**裁决（用户，2026-10-28）：D1–D16 全部按下表「推荐」列执行，已立项；先做 Gate 0，Gate 0 某项不过时对应 PR 不做、由用户另行决定。**

| # | 问题 | 选项 | 推荐与理由 |
|---|---|---|---|
| D1 | 推进还是关闭待办 | a. 按本规格推进，Gate 0 逐项判定；b. 不做，关闭待办并删除 `kg_strip_attrs.py` | **a**。推荐方案零存储、零回答路径改动，回退面很小；Gate 0 防止白做 |
| D2 | 字段集合 | a. `aliases` + `variables`；b. 再加 Claim `quantitative_values`；c. 只做 `aliases` | **a**。其余五个没有读者，或与已有结构重叠（§四） |
| D4 | 别名规则 | a. R1–R3；b. R1–R4（R4 按 Gate 0 中文切片判定）；c. b 再加一次模型调用覆盖「又称 / also known as」 | **b**。c 只覆盖 2.3%，还会把别名变成需要存储或缓存的模型产物 |
| D5 | 公式送模范围（只对 D12-b 批量有意义） | a. 全部公式（按元素去重后约 11%）；b. 只送带说明提示词的公式（约 2%） | 推荐 D12-a 时**不适用**：按需时由用户点击决定。若选批量，推荐 **a**，教材类来源超出 15% 时按 §十二 收紧并降到 b |
| D6 | 模型工作负载 | a. 新工作负载 `kg_attr_enrich`（「知识属性补全」，默认不思考，交互优先级）；b. 复用 `kg_extract` 的绑定 | **a**。可以绑到另一个物理服务或更便宜的模型，单独计价；同一服务内以交互优先级排在后台抽取前面（并发仍按服务算，§十二）。代价：严格绑定下，升级前必须给生产配置补上这一项，否则拒绝启动。发布说明与部署文档写清三件事：①配置模板 `model-services.example.toml` 的 `[bindings]`（与需要时的 `[thinking]`）新增 `kg_attr_enrich`；②生成器 `scripts/migrate_legacy_model_env.py` 产出这一项；③部署前自检：用新构建对生产的 `MODEL_SERVICES_CONFIG` 启动一次，看启动日志里有没有 `model-bindings:` 行（格式见部署文档）。实现时如果已有只读校验入口，就改用它。评审建议的「开关打开时才要求绑定」与既有裁决冲突（§2.11），不采纳 |
| D7 | 存量回填（只对存储方案有意义） | a. 按笔记本手动回填并先 dry-run；b. 后台自动；c. 只管新来源 | 推荐方案**不需要回填**。若选存储，推荐 **a** |
| D8 | 用途边界 | a. 只进界面详情；b. 同时进检索、种子合并、回答上下文 | **a**。b 的每一项都会改变结果，需要另写规格并做 A/B |
| D9 | 默认开关 | a. 不设开关，Gate 0 对应项达标就上线；b. 设开关，默认关，按部署打开：`KG_ATTRS_ALIASES_ENABLED`、`KG_FORMULA_VARIABLES_ENABLED` | **a**。别名零成本，按钮只在用户点击时花钱，回答路径不受影响。选 b 时这两个变量列进 §十七 |
| D10 | gold 标注 | a. 模型预标，用户在本地标注页复核（英文切片入库，中文切片不入库）；b. 只用 LLM 评审 | **a**。Wilson 下界要的是可信的人工判定 |
| **D11** | 别名读时派生还是存储 | a. 读时派生（两个详情出口，零存储）；b. 入库时一并写（随首次 INSERT，不做二次 UPDATE），需要 S4 或加列 | **a**。规则是纯函数，两个出口本来就读到了经范围过滤的元素原文；零迁移、零回填，权限和重算语义天然正确。代价是每次打开详情多一段纯 Python，有 p95 门 |
| **D12** | 公式符号说明按需还是批量 | a. 按需：详情里的按钮触发，结果只进模型响应缓存；b. 批量：入库后补全并存进 S4，存量用命令回填 | **a**，并按下面三条附带规则：(1) 空结果（`none_stated`）和全部被拒（`none_grounded`）都缓存，见 §7.3；(2) 不提供绕过缓存的重试，只有从未入缓存的 `failed` 可以再试；(3) 每个用户同时最多一个在飞请求，不设每日上限。理由：成本只花在被看的公式上，不迁移，没有写放大，不需要对存量花约 9 亿字符（推算）。代价是第一次点击要等一次模型调用，缓存被淘汰后要重算。三条附带规则各自也可以单独改：不缓存空结果、允许绕过缓存的重试、或加每日上限 |
| **D13** | 存储形态（只在 D11-b 或 D12-b 时） | S2 主表加列 / **S4 旁表** | **S4**。§6.2 实测主表 UPDATE 的 HOT 只有 0.2%，三个 trigram GIN 涨 12–19%；S2 还要改掉 6 处 `SELECT *` 并扩展 id 重映射。S4 的代价是新表要进归档并 bump `FORMAT_VERSION` |
| **D14** | 重算语义 | a. 纯函数：attrs = f(名字，查看者可读的证据原文)；b. 存储并由写钩子维护（`update_knowledge`、治理合并、冲突修改） | **a**，随 D11-a、D12-a 自动成立。若选存储，用输入签名比对让结果在读出时失效，不靠写钩子（冲突修改与名字写入不在同一个事务，钩子必漏） |
| **D15** | 覆盖范围 | a. 排除 Knowhow 投影对象，Memory 派生对象随可见性生效；b. 一律生效；c. Knowhow 与 Memory 都排除 | **a**，Knowhow 按 id 前缀 `ko-kh-` 零 SQL 判别。Knowhow 的 `object_type` 是列名，不是核心类型语义；Memory 已有成熟的查看者范围 |
| **D16** | Gate 0 部分通过时 | a. 各项独立上线，没过的停下交用户；b. 任一项不过就整体不上 | **a**。三项互不依赖（§13.1） |

（D3 原来是「S1/S2/S3」，已经由 D11–D13 取代，编号保留空缺，便于对照 v1。）

## 十七、实施时要更新的权威文档与清理

- `docs/product-and-api.md` / `_zh.md`：两个详情响应的 `attrs` 字段、按需接口与 `status`、范围规则。
- `docs/deployment-and-configuration.md` / `_zh.md`：
  - `KG_ATTRS_CONTEXT_CHARS`、`KG_ATTRS_PAIR_WINDOW`；
  - 新工作负载 `kg_attr_enrich` 的绑定、模板、生成器与部署前自检（D6）；
  - D9 选 b 时，再加 `KG_ATTRS_ALIASES_ENABLED`、`KG_FORMULA_VARIABLES_ENABLED`。D9-a 下不设开关。
- `model-services.example.toml` 与 `scripts/migrate_legacy_model_env.py`：新增 `kg_attr_enrich`（D6）。
- `docs/ui-vocabulary.md`：「别名」「符号说明」「识别符号说明」。
- `release-notes/`：按 `release-notes/README.md` 分级。
- 删除 `scripts/kg_strip_attrs.py` 与 `scripts/README.md:578`；`fangan_todo.md` 该条移进 `fangan_done.md`。

## 十八、第一轮评审处置

| 评审条目 | 核实 | 处置 |
|---|---|---|
| P1-1 读者挂错 | 成立：簇成员探测只在「有天花板 + 有描述」时执行（`knowledge_store.py:2665-2680`）；store 的 `node_context` 服务回答路径 | 派生改为 opt-in，挂在 `KnowledgeQueryService.node_context(with_attrs)` 与 `_concept_detail` 上，后者复用它自己的分页与查看者范围，不新增 SQL；守卫加回答路径模块与运行期间谍（§7.2、§7.5） |
| P1-2 复制、导入不重映射 | 成立（`notebook_copy_writer.py:1303-1311`） | 推荐方案不存储；S4 备选的条目不存 id（§6.3） |
| P1-3 `SELECT *` 搬运 | 成立（6 处） | 不再给主表加列；S2 排除（§2.4、§6.3） |
| P1-4 写放大 | 成立，已实测（§6.2） | 新增 S4；S2 排除；推荐零存储 |
| P1-5 更省的选项 | 成立：两个出口已经读到完整元素原文，而 `quoted_span` 只覆盖 63% | 采纳为推荐方案（D11-a、D12-a） |
| P1-6 Gate 0 撑不起结论 | 成立 | 按产出计数、Wilson 下界、中文切片本地标注、对齐键、产出率与拒绝原因、部分通过的处置（§13.1、D16） |
| P2-1 核验规则 | 成立 | LaTeX 归一化、希腊映射、邻近窗口加最近符号配对、拒绝原因（§7.3） |
| P2-2 权限口径 | 成立 | 只用已按 `_scoped_occurrences` 与 `_viewer_resolved_evidence` 过滤的元素（§八） |
| P2-3 重算语义 | 成立 | 纯函数（D14）；存储备选用签名失效 |
| P2-4 MCP 出口不存在 | 成立（grep 零命中） | 删掉，列为非目标 |
| P2-5 Knowhow | 成立 | 排除（D15） |
| P2-6 代价细节 | 成立 | 按元素去重（19%）、教材超限的处置、去掉 `source_id`、7 KB 估算（§五、§十二） |
| P2-7 模型争用 | 成立 | 独立工作负载、交互优先级、入库吞吐对照、批量备选避开 `offline_batch_active`（§十二、§7.3） |
| P3 `_norm` 口径 | 成立；本机实测 21 个，与评审的 231 口径不同 | 指定用 `canonicalize._norm`（§7.2） |
| P3 R1 不变量冲突 | 成立 | 不变量分开写（§7.2） |
| P3 `scripts/README.md:578` | 成立 | 列入清理（§十五、§十七） |
| P3 24 题集只能本机跑 | 成立 | 写明（§2.12、§13.2） |
| P3 逐字节对照需要确定性模型 | 成立 | 只在存储备选里需要，写明用 FakeClient 或回放（§13.2） |
| P3 守卫防撞名、防挪移 | 成立 | 按导入目标匹配，加变异用例（§7.5） |
| P3 `KnowledgeRecord.attrs` 加 null | 成立 | 不改 `KnowledgeRecord`（§五） |
| P3 检查点清理 | 成立 | 写进批量备选（§7.3） |
| P3 元素按来源批量读 | 成立 | 写进批量备选（§7.3） |
| P3 D6 只在开关开时要求绑定 | **不成立**：与 `binding_gap` 文档写明的用户裁决冲突（`model_registry.py:540-546`） | 不采纳，理由写进 D6 |
| P3 ADD COLUMN 写 `lock_timeout` | 成立 | 写进 S4 迁移（§6.3）；实测用了 3 s |

## 十九、复审处置（零存储方向成立，无 P0/P1）

| 复审条目 | 核实 | 处置 |
|---|---|---|
| P2-1「下一个元素」 | 成立：v2 误写「没有 (source_id, …) 复合索引」，实际有 `(source_id, created_at, id)`；SQLite 没有 `ordinal` 列（已核实 `sqlite/migrations.py:655-663`）；元素 id 的 `:04d` 已核实（`source_ingestion.py:1467`、`:1808`）；SQLite `replace_elements` 按顺序 `executemany` 已核实（`sqlite/source_store.py:1989-2040`） | §2.9 更正并写明不用 `(created_at, id)` 的理由；PG 用主键子查询取 ordinal 的有界范围查询，SQLite 用 rowid，另外三处插入路径列为实现核对项；既有排序问题提一句（§2.9） |
| P2-2 缓存语义 | 成立（`llm.py:900-920`、`:1128-1139`、`bypass_cache` 行 814） | 校验器只判形状、接受 `{"variables": []}`；四种固定 status；只有 `failed` 可重试，因此不需要 `bypass_cache`；「直接命中」改成「尽力」；D12 并入缓存、重试、每用户上限三条（§7.3、§7.4、D12） |
| P2-3 helper 与守卫 | 成立 | `app/domain/kg_attrs.py` 的模块级函数；强制模块属性调用、禁止 from-import；间谍 patch 模块属性，加正向对照（§7.2、§7.5） |
| P2-4 概念详情落点 | 成立（`_viewer_resolved_evidence` 是 689-741，只返回打平列表；`loadMoreConceptMembers` 覆盖顶层字段） | 用拼 `evidence` 时的同序 owner 列表无 SQL 地对齐；去掉顶层 attrs，由前端从成员现算并集；排除 `canonical_name`（§7.2、§九） |
| P3-1 并行度 | 成立（`model_provider.py:838`、部署文档） | §十二、D6 改写 |
| P3-2 p95 门 | 成立（本机最大簇 59 成员） | 合成 hub 夹具加微基准绝对上限（§十二） |
| P3-3 Knowhow 判别 | 成立（`knowhow/ids.py:11`） | 改用 id 前缀 `ko-kh-`（§五、D15） |
| P3-4 R1 的 element_id | 成立 | 取第一条可读且有定位符的出处，没有就不带键；B-11 清掉定位符的条目跳过 R2–R4（§7.2） |
| P3-5 两份详情同时到达 | 成立（`use-kg-graph.ts:582`、`:601`） | 显示概念详情的并集，404 时才用对象详情（§7.2） |
| P3-6 D9 开关 | 成立 | D9-a 不设开关；D9-b 列出两个变量（D9、§十七） |
| P3-7 接口签名与鉴权 | 成立（会话或 Agent token 的依赖在 `deps.py:791` 起，默认路由只接会话） | 补 `source_notebook_id`；只接会话鉴权（§7.3、§九） |
| P3-8 在飞时切换节点 | 成立 | 沿用三重守卫并写进前端测试（§7.3、§十四） |
| P3-9 英文切片的名字来源 | 成立 | 用产品抽取链路在 `source.md` 上产出，样本连同抽取输出入库（§13.1） |
| P3-10 D6 发布说明 | 成立 | 模板、生成器、部署前自检写进 D6 与 §十七 |

## 附录 A：本机探针 SQL（只读）

都在 `SET default_transaction_read_only=on;` 下运行，库是 `silicon_notebook_local_pg16_live`。

```sql
-- 2.13 括注缩写（名字形 / quoted_span 形）
SELECT count(*) FILTER (WHERE payload->>'name' ~ '^.*\S\s*\([A-Za-z0-9]+\)\s*$'), count(*)
FROM knowledge_objects WHERE object_type='concept' AND status!='deprecated';
SELECT count(*) FILTER (WHERE evidence::text ~ '\(([A-Z][A-Za-z0-9]{1,7})\)'), count(*)
FROM knowledge_objects WHERE object_type='concept' AND status!='deprecated';

-- 2.5 名字后紧跟括号：quoted_span（<=400）对完整元素原文
WITH c AS (SELECT o.payload->>'name' AS nm, o.evidence->0->>'quoted_span' AS qs,
                  o.evidence->0->>'element_id' AS eid
           FROM knowledge_objects o WHERE o.object_type='concept' AND o.status!='deprecated')
SELECT count(*) FILTER (WHERE position(lower(c.nm)||' (' IN lower(c.qs))>0
                          OR position(lower(c.nm)||'（' IN lower(c.qs))>0),
       count(*) FILTER (WHERE position(lower(c.nm)||' (' IN lower(se.text))>0
                          OR position(lower(c.nm)||'（' IN lower(se.text))>0),
       count(*)
FROM c JOIN source_elements se ON se.id=c.eid;

-- 2.9 公式说明：证据元素或下一元素；不同证据元素数
WITH f AS (SELECT o.id, (o.evidence->0->>'element_id') AS eid FROM knowledge_objects o
           WHERE o.object_type='formula' AND o.status!='deprecated'),
     fe AS (SELECT f.id, se.source_id, se.ordinal, se.text FROM f JOIN source_elements se ON se.id=f.eid)
SELECT count(*) FILTER (WHERE fe.text ~* '(where|其中|式中)'
                          OR nx.text ~* '^\s*(where|其中|式中|here)'),
       count(*) FILTER (WHERE nx.text ~* '(where|其中|式中)'), count(*)
FROM fe LEFT JOIN LATERAL (SELECT s2.text FROM source_elements s2
  WHERE s2.source_id=fe.source_id AND s2.ordinal>fe.ordinal ORDER BY s2.ordinal LIMIT 1) nx ON true;
SELECT count(DISTINCT o.evidence->0->>'element_id'), count(*)
FROM knowledge_objects o WHERE o.object_type='formula' AND o.status!='deprecated';

-- 2.8 多成员簇与融合描述
SELECT count(*) FROM (SELECT notebook_id, canonical_id FROM concept_clusters
  WHERE object_type='concept' GROUP BY 1,2 HAVING count(*)>1) t;
SELECT count(DISTINCT (notebook_id, canonical_id)) FROM concept_clusters
WHERE object_type='concept' AND canonical_description<>'';

-- 2.2 / 2.9 索引
SELECT indexname, indexdef FROM pg_indexes WHERE tablename='knowledge_objects';
\d source_elements
```

§6.2 写放大（一次性库，`pg_dump -t public.knowledge_objects` 复制、去掉同步触发器后恢复）：

```sql
VACUUM ANALYZE knowledge_objects;
SELECT pg_stat_reset_single_table_counters('knowledge_objects'::regclass);
-- 记录各索引与堆的 pg_relation_size
SET lock_timeout='3s';
ALTER TABLE knowledge_objects ADD COLUMN attrs jsonb NOT NULL DEFAULT '{}'::jsonb;
UPDATE knowledge_objects SET attrs = <约 100 字节的别名对象>
 WHERE object_type='concept' AND evidence::text ~ '\(([A-Z][A-Za-z0-9]{1,7})\)';   -- 2,965 行
UPDATE knowledge_objects SET attrs = <约 200 字节的两条符号说明>
 WHERE object_type='formula' AND id IN (SELECT id FROM knowledge_objects
   WHERE object_type='formula' ORDER BY random() LIMIT 2000);                        -- 2,000 行
SELECT n_tup_upd, n_tup_hot_upd, n_dead_tup FROM pg_stat_user_tables WHERE relname='knowledge_objects';
-- 再记录各索引与堆的 pg_relation_size 并做差
```
