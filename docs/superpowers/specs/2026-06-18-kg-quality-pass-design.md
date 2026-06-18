# KG 质量提升 Pass(检索文本去噪 + canonical 折叠 + about 降权)

- 日期:2026-06-18
- 状态:设计已确认,待写实现计划
- 分支/PR:直接叠加在 `claude/wonderful-bell-3b27db` / **PR #59**(与关系检索增强一并合入,用户拍板)
- 关联:`2026-06-17-kg-relation-retrieval-design.md`(本 pass 的复测用其 gold 集 + 关系检索)

## 1. 背景与诊断(真机 nb-b37185f4ae,3.86 万对象实测)

graph 答案 A/B 显示"关系种子更好 ≠ 答案更好",根因指向 **KG 内容/检索文本质量**。在真实 KG 上量化:

| 问题 | 量级 | 根因 |
|---|---|---|
| **① section_path 污染检索文本** | **100% 对象**带 `section_path` 字段;`_payload_text`(retrieval.py:499)无脑拼接所有字符串字段 → 64.7% 检索文本可见 " > "/挂章节号 | payload **干净**(`{"name":"Mixtral","section_path":"3 > 3.1"}`),bug 在检索文本构造 |
| ② 同名概念被章节切碎 | concept **17% 碎片**(KV cache ×23、RoPE ×20、Transformer ×17…) | `concept_clusters` 已填充(30253 成员/27755 canonical)但 **`_retrieve_scored` 不消费**;且聚类 seed 疑似含 section_path 致欠合并 |
| ③ about 边占 56.8% | 半数关系是弱结构边 | 检索/上下文噪声源 |
| ④ 空洞 claim | 仅 33 条 | 忽略 |

payload 顶层字段仅 4 个:`name`(干净)、`section_path`(唯一污染源,被误纳)、`validity_scope`(dict,已跳过)、`steps`(合法)。

**关键洞察:KG 数据本身干净,问题在检索消费层;① 是根因(修它,② 聚类也跟着变准)。全部可离线修、非销毁。**

## 2. 目标与成功判据

**目标:** 离线提升已建 KG 的检索质量,不重抽、不改对象 id。

**成功判据:** ①②③ 落地后,在 `recall_gold.yaml`(45 题)上**重跑 recall + graph A/B**:
- 主:relation/node recall@k ↑(尤其去噪后桥接边);
- 次:graph 答案 A/B 的 grounding 回升 / 伪引用回落 / correctness 不降(检验"去噪能否把 A/B 抬起来");
- 护栏:对象 id 不变 → gold 集仍有效;`[0,1]`/tau 不变。

## 3. 范围

**In:** ① 检索文本去 section_path + re-embed;② 检索消费 canonical 簇(非销毁);③ about 边降权;④ 复测。

**Out(YAGNI/明确不做):** 销毁式节点合并(改 id)、KG 重抽取、抽取 prompt 改动、模糊同义自动合并(只进人审队列)、"其它"未定项。

## 4. 组件设计

### ① 检索文本去 section_path(根因)

- 引入"非语义元数据字段"概念:`_payload_text` 排除 `section_path`(连同已有的 `_` 前缀跳过逻辑)。`section_path` 仍留 payload,供引用/显示单独取用。
- 同步:`cluster_objects` 的 seed 归一化(`seed_concept`/`seed_claim`)确保**只基于 `name`**(不含 section_path),否则同名跨章节欠合并。
- **re-embed**:`section_path` 进过 `knowledge_embeddings` / `relation_embeddings` 的向量 → 在干净文本上重嵌。现有 backfill 只补缺失(幂等),故需 **force**:先 `DELETE` 该 notebook 的 `knowledge_embeddings`/`relation_embeddings` 再调 backfill(或给 backfill 加 `force=True` 分支)。离线 CLI。
- 不变量:非销毁、不改 id。

### ② 检索消费 canonical 簇(非销毁合并)

- **决策:非销毁。** 不塌缩节点、不改 id;检索期把同 canonical 的碎节点**折叠到最佳代表**(同 `canonical_id` 只留打分最高的成员进候选,其余 drop)。对齐既有 `derive_unified_graph` 设计,保护 gold 集。
- 接入点:`_retrieve_scored`(及 `federated_retrieve`/关系检索)打分排序后,用 `concept_clusters` 的 `member_object_id→canonical_id` 映射(`get_cluster_map`,sqlite_repository:2530)折叠候选。无簇映射的对象按自身 id 退化(不折叠)。
- **① 修完后重跑 `cluster_objects`**(干净 seed → KV cache×23 真并成 1)。
- **合并力度:** exact 归一化名自动合(现有机制);模糊同义(MoE / Mixture-of-Experts(MoE))**进既有 `concept_merge_candidates` 人审队列**,不自动过合(防 over-merge)。
- 开关:`KG_CANONICAL_FOLD_ENABLED`(默认关,eval 验证后再开),关时检索字节等价。

### ③ about 边降权

- about 占 56.8% 弱结构边。在**关系检索排序 / 图种子打分**里给 about 一个 <1 的 **rank 乘子**(放 `rank_key`,**不进 `_fuse`** → 守 [0,1]/tau,与 `tier_weight` 同范式)。
- **保留不删**(about 仍可作上下文),只是排在推理边(derived_from/supports/depends_on)之后。
- 常量 `EDGE_TYPE_RANK_WEIGHT`(about<1.0,推理边=1.0)集中可调。

### ④ 复测(目的)

- 扩 recall 指标:**canonical 层比对** —— `run_recall` 把检索到的 id 与 gold id **先映射到 canonical_id 再算 recall@k/MRR**(否则 ② 折叠后,gold 恰是被折掉的成员会假性 miss)。无簇映射的 id 用自身。
- 在 `recall_gold.yaml` 上 baseline(本 pass 前)对照 treatment(后)跑 recall;`run_graph_ab.py`(已有,问题级并行)复跑 graph A/B。
- 数字落 PR #59 评论。

## 5. 错误处理与回退

- 无 `concept_clusters` 映射的对象/notebook → 折叠退化为按自身 id(不崩、不折)。
- re-embed 失败 best-effort 跳过(沿用现有 batch 容错);未 re-embed 的旧向量仍可用(只是带噪)。
- `KG_CANONICAL_FOLD_ENABLED` 默认关 → 检索路径字节等价,渐进开启。
- section_path 排除只影响**检索文本**;payload/显示/引用不动。

## 6. 测试与不变量

- 单元:`_payload_text` 排除 section_path(断言含 section_path 的 payload 文本不含 " > ");canonical 折叠保留最高分代表 + 去重;about rank 乘子只进 rank_key 不进 `_fuse`;recall 指标 canonical 映射。
- 等价:`KG_CANONICAL_FOLD_ENABLED` 关时 `_retrieve_scored` 字节等价。
- 不变量:对象/关系 id 不变(gold 集有效);`max(0,cosine)`∈[0,1]/tau 不被污染;降权/折叠在排序层,不入 `_fuse`。

## 7. 代码落点

`retrieval.py`(`_payload_text` 排除 section_path、`EDGE_TYPE_RANK_WEIGHT`)、`kg_merge.py`(`seed_concept`/`seed_claim` 确保只用 name)、`sqlite_repository.py`(`_retrieve_scored`/关系检索接 canonical 折叠 + about 降权、re-embed force 入口)、`config.py`(`KG_CANONICAL_FOLD_ENABLED`)、`eval/retrieval_metrics.py`(canonical 层 recall)、`scripts/`(re-embed + re-cluster 离线 CLI)、测试若干。

## 8. 关键决策记录

- 直接叠在 PR #59、一并合(用户拍板)。
- 全程**非销毁 + 不改 id**(保护已固化 gold 集 + 对齐 derive_unified_graph)。
- ① 是根因(section_path 既污染检索文本、又疑似害聚类欠合并)。
- ② 检索期折叠(非销毁),exact 自动 + 模糊进人审;`KG_CANONICAL_FOLD_ENABLED` 默认关待 eval。
- ③ 降权非删除,rank 层非 `_fuse`。
- ④ recall 必须 canonical 层比对。
