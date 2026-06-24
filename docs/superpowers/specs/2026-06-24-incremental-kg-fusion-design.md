# Spec:增量 KG 融合(分层)+ KG 抽取 LLM 独立配置

- 日期:2026-06-24
- 状态:设计已批(用户「同意你的说法」+「KG 抽取 LLM 独立也写进来,一把做掉」),待实现计划
- 分支:`claude/incremental-kg-fusion`(off origin/master)
- 关联记忆:`hipporag-ppr-plan`、`cross-doc-entity-resolution-research`、`kg-conflict-resolution-state`、`kg-extract-llm-timeout`、`model-service-url-only`

## 背景 / 动机

排查「上传 4 篇新论文后 PPR/概念漫游对比检索不到它们」发现两个问题:

1. **跨文档融合不自动**:上传新源走 `process_source → _run_extraction`(parse→embed→抽 KG,逐源**增量**存 `knowledge_objects`),但**跨文档融合**(`concept_clusters` 归一名簇 = PPR/概念漫游赖以跨文档的桥)只由 `rebuild_unified_kg`(notebook 级**全量**、含 LLM)触发,上传不跑。**实测**:nb-b37185f4ae 的 4 篇新论文 3121 个 KG 对象 **0 个进簇**(全库 41713 中已入簇 38592 = 老的)。→ 新论文「入库但未融合」,对比检索够不到。
2. **KG 抽取无独立模型**:抽取用的是主 `self.llm_client`(`openai_compat_model`,见 `_run_extraction:1614` `extract_graph(self.llm_client,…)`),没有独立配置组——而 `REASONING_LLM_*`/`REWRITE_LLM_*`/`EMBED_*`/`RERANK_*` 都有。抽取是批量离线任务,应能用与在线问答解耦的模型。

参考 `../ref-kg` 4 个实现的增量做法(code-grounded):LightRAG(名键增量、描述合并 ≥8 片段才 LLM)、HippoRAG(内容 hash 不并点 + synonym 边 + PPR,但 synonym 边全量 kNN)、MemGraphRAG(精确三元组 + 实体重叠/向量相似检测 → LLM 裁决、schema-local)、graphrag(每次全量重跑 Leiden,贵)。**关键洞察**:「新实体影响簇分布」只发生在**向量近重层**;**归一名种子层天然 shift-free**(canonical_id 只由实体自身名决定)。4 个参考要么永远 local 要么永远全量,**用户的三层升级是更细的中间地带**。

**已有机件正好支撑**:`cluster_concepts`([kg_merge.py:261](backend/app/services/kg_merge.py:261))本身就分层——精确同名 + 已确认对 **force-union**(=Tier1),向量候选 **≥hi(0.94)→ auto_candidates、[lo,hi)(0.82~0.94)→ pending,均不自动 union**(=Tier2 入队)。增量 = 把这套限定在「新实体 vs 已有簇」+ 追加写。

## 目标

- **A**:上传新源后**自动增量融合**进 `concept_clusters`(分层:确定性 append → 桥接入队 → 全量逃生口),新论文立刻可被 PPR/概念漫游跨文档检索到;不每次全量、不每次 LLM。
- **B**:KG 构建的 LLM 走**独立配置组** `KG_LLM_*`(缺省回退主 `openai_compat_*`),与在线问答模型解耦。
- 守 `[0,1]`/tau;`rebuild_unified_kg` 全量路径保留为逃生口、行为不变;模型仅经 URL 端点([[model-service-url-only]])。

## 非目标

- 不硬并点(沿用 HippoRAG/现状:软簇 = `concept_clusters` 成员,可回退)。
- 不做向量层「自动合并」——桥接候选**只入 `concept_merge_candidates` 队列**,由现成复审/全量消费(用户拍板:保守、可观测)。
- 不改 `rebuild_unified_kg` 的全量算法(只新增增量路径)。
- Tier3 触发**保持手动**(不做自动漂移检测)。

## A. 增量 KG 融合(分层)

### A0. 前提
「合并」= 往 `concept_clusters` 追加成员(软桥),不删 `knowledge_objects`。concept 的 `canonical_id = "K-" + _norm(name)`(逐实体确定性)。

### A1. 三层(映射到 `cluster_concepts` 已有逻辑)
- **Tier 1 — 确定性 append(无 LLM,~95%)**:新源每个 concept 算 `K-<_norm(name)>`。该 canonical_id 已存在 → **追加成员**(瞬间跨文档桥);新名 → 建新簇(单成员)。名种子层**不动任何已有实体的归属** → 无簇分布漂移。
- **Tier 2 — 桥接检测 → 入队(检测无 LLM)**:新 concept 的 embedding 对**已有 concept** 做 ANN(`top_k`),命中分 ≥`lo`(0.82)且落在与其名种子簇**不同的** canonical 簇、且该对不在 `decided_pairs` 的 rejected 里 → 视为「桥接两簇」候选 → **写入 `concept_merge_candidates`**(`canonical_a/b` + score + status='proposed'),**不自动 union**。≥hi(0.94)的标 auto(供 LLM 兜底复审),[lo,hi) 标 pending(人工)。检测本身只算向量,不调 LLM。
- **Tier 3 — 全量重建(逃生口)**:现成 `rebuild_unified_kg`(手动/周期),消费队列、全局重聚、生成簇描述。行为不变。

### A2. 新增组件
- `kg_merge.py`:`place_new_concepts(new_concepts, new_vectors, existing_cluster_map, existing_canon_names, existing_vectors, confirmed, rejected, hi, lo, top_k) -> {appends:[{canonical_id,member_object_id,canonical_name}], new_candidates:[{canonical_a,canonical_b,score,tier}]}`。纯函数,复用 `_norm` + ANN 护栏;**只放置新实体、不重排已有**。
- `sqlite_repository.py`:
  - `append_clusters(notebook_id, rows, object_type='concept')`:**追加**写 `concept_clusters`(不 DELETE),`member_object_id` 幂等(已存在则跳过)。区别于现 `write_clusters`(DELETE+全插)。
  - `incremental_fuse_source(notebook_id, source_id)`:取该源新 concept(name+payload)+ 其 `knowledge_embeddings` + 已有 `cluster_map`/canonical 名/已有 concept 向量 + `decided_pairs` → 调 `place_new_concepts` → `append_clusters` 落 Tier1 + 入队 Tier2 候选 → 驱逐相关缓存(`cluster_map`/`ppr_graph` 等,复用 `_invalidate_unified_cache`)。
  - 接线:`_run_extraction` 存完 KG(`store_kg` 后)调 `incremental_fuse_source`(flag 门控,见 A4)。
- claim/formula/procedure:精确名种子(`cluster_objects` seed_fn),**同法 Tier1 append**(无向量层),可同 PR 跟上或留后续(见非目标取舍)。

### A3. 数据流
```
process_source → _run_extraction → store_kg(新 KG 对象/边)
  → incremental_fuse_source:
       Tier1 名种子 → append_clusters(瞬间跨文档桥,无 LLM)
       Tier2 向量 ANN → 桥接候选入 concept_merge_candidates(无 LLM)
       驱逐 cluster_map/ppr_graph 缓存
  → 新论文即刻进 PPR/概念漫游的跨文档桥
（手动)rebuild_unified_kg → 消费队列 + 全局重聚 + 簇描述(逃生口,行为不变)
```

### A4. 开关 / 不变量
- 新增 `KG_INCREMENTAL_FUSION_ENABLED`(默认 **True**——这是修 Q2 缺口的主路径;关→回到今天「上传不融合、等手动 rebuild」)。
- Tier2 阈值复用 `cluster_concepts` 的 `hi=0.94`/`lo=0.82`(不新增旋钮)。
- 幂等:`incremental_fuse_source` 可重跑(append 幂等 + 候选去重);`_run_extraction` 重抽同源时,A 的成员随该源 KG 被清而失效,重融即可。
- 守不变量:Tier1 追加不改已有 canonical_id;Tier2 不自动改簇;只有 Tier3 全量可重排——与今天一致。

## B. KG 抽取 / 构建 LLM 独立配置

### B1. 配置(`config.py`,镜像 `reasoning_llm_*`)
```python
kg_llm_base_url: str = Field("", env="KG_LLM_BASE_URL")
kg_llm_api_key: str = Field("", env="KG_LLM_API_KEY")
kg_llm_model: str = Field("", env="KG_LLM_MODEL")
# + @property kg_llm_configured:三者齐 → True
```

### B2. client(`sqlite_repository.py`,镜像 `reasoning_llm_client` 的动态回退)
- 加 `kg_llm_client` 属性:`kg_llm_configured` → 用 `KG_LLM_*` 建 `OpenAICompatibleClient`;否则**回退 `self.llm_client`**(主端点)。
- 与 `reasoning_llm_client` 一样支持注入(测试可替身)。

### B3. 改点(KG 构建的 LLM 调用全切到 `kg_llm_client`)
- `_run_extraction:1614` `extract_graph(self.llm_client, …)` → `extract_graph(self.kg_llm_client, …)`(refine/glean 经 extract_graph 传 client,自动跟随)。
- `rebuild_unified_kg`:簇描述(`:3300` 区 `self.llm_client.chat_json`)+ `review_merge_candidates`(`:3287`)→ `kg_llm_client`。
- 冲突复审 `review_conflict_candidates`(`:3044` 区)→ `kg_llm_client`。
- A 的 Tier2 检测**不调 LLM**;队列被 rebuild 消费时的复审走 `kg_llm_client`。
- **范围界定**:`kg_llm_client` = 所有「KG 构建/融合」批量 LLM(抽取 + refine/glean + 簇描述 + merge/conflict 复审);**在线问答**(ask/reasoning/概念漫游答案)**不动**,仍用各自 client。
- 缺省(`KG_LLM_*` 未配)→ `kg_llm_client` == `self.llm_client` → **行为字节等价**。

## 错误 / 边界
- A:无 embedding 的新 concept → Tier1 仍生效(名种子),Tier2 跳过(无向量);ANN 无命中 → 只 Tier1。`incremental_fuse_source` 异常被 `_run_extraction` 容错吞掉(不阻断 ingest),记 event。
- A flag off → 不融合,行为同今天。
- B `KG_LLM_*` 未配 → 回退主 client,零行为变化。
- B 模型仅 URL 端点(KG_LLM_BASE_URL),不起本地模型服务([[model-service-url-only]])。

## 测试
1. **A·place_new_concepts 纯函数**:新实体名种子命中已有 canonical → appends 含它;新名 → 新簇;向量命中不同簇 → new_candidates;rejected 对 → 不入候选。
2. **A·incremental_fuse_source 端到端**:2 文档已建簇 + 第 3 文档新增同名 concept → `append_clusters` 后该 concept 进既有簇(`cluster_map` 命中),`ppr_graph` 缓存被驱逐;PPR 能跨到新文档。
3. **A·Tier2 入队**:新 concept 向量近一个**异名异簇**已有 concept(≥lo)→ `concept_merge_candidates` 多一条 proposed,**未自动并簇**。
4. **A·flag off**:`KG_INCREMENTAL_FUSION_ENABLED=false` → 抽取后该源 0 入簇(同今天)。
5. **A·幂等**:`incremental_fuse_source` 跑两次,成员不重复。
6. **B·kg_llm_client 回退**:`KG_LLM_*` 未配 → `kg_llm_client is self.llm_client`(或等价);配了 → 用 KG_LLM_MODEL。
7. **B·抽取走 kg_llm_client**:替身 `kg_llm_client` 记录调用 → `_run_extraction` 用它(主 `llm_client` 不被抽取调用)。
8. **隔离**:在线问答(ask_chunk/ask_reasoning/ask_graph)不受 A/B 影响;`rebuild_unified_kg` 全量路径快照不变。

## 数据流图(分层)
```
Tier1 名种子(确定性,无 LLM) ──命中已有簇──→ append 成员=跨文档桥
                              └─新名──────→ 新簇
Tier2 向量 ANN(无 LLM) ──≥lo 且跨簇──→ concept_merge_candidates 入队(不并)
Tier3 rebuild_unified_kg(手动) ──→ 消费队列 + 全局重聚 + 簇描述(逃生口)
```

## 未决 / 后续
- claim/formula/procedure 增量(精确名种子,无向量层)——本 PR 一并做还是留后续(建议一并,逻辑更简单)。
- Tier2 队列的「自动消费」:目前靠手动 rebuild 或现成复审入口消费;是否做「攒够 N 条候选提示复审」留后续。
- 真机:对 nb-b37185f4ae 跑一次(或等下次上传自动)验证 4 篇新论文进簇 + PPR 对比跨到它们;对照 `concept_clusters` 计数从 38592 涨到含新源。
