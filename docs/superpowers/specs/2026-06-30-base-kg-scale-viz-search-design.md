# 设计：领域基础 KG 的可视化/搜索规模化(SP1)

- 日期：2026-06-30
- 范围：SP1 —— 让 KG **可视化**与节点**搜索**在 10^5–10^6 节点的基础 KG 上服务端有界、不物化/不传输全量。
- 依赖：复用 SP2(已合并)的持久化 scale 索引(hnswlib ANN);可独立于 SP3。
- 不在本 spec：SP2(检索基底)、SP3(摄取/聚类)。

## 背景与两堵墙

1. **可视化(墙 1)**：`unified_graph(notebook_id, level, limit)` 对 `limit=80` 也先 `_unified_graph_full` **物化整张图再切**(`derive-full-then-slice` + 缓存进 `_unified_cache`)。10^6 时开图谱=在 RAM 建 10^6 折叠图。`sqlite_repository.py:_unified_graph_full`。
2. **搜索(墙 2)**：`search_notebook` 用 `LIKE` 扫 `knowledge_objects.payload` JSON(**无 FTS5、无 name 列、无文本索引**),每查询 O(n) 扫 + 反序列化;前端搜索更触发 `fetchUnifiedGraph(nb,0)` 拉全量 → 逼后端物化整张图 + 传几百 MB JSON。

**已扛得住**:`concept_detail`/`node_context` 走 `knowledge_relations(notebook_id, source/target_object_id)` 邻接索引(有界);但无通用「从节点展开 1-hop」端点。

**关键架构细节**:viz 图是**折叠后**的(成员 concept → canonical,经 `derive_unified_graph` + cluster_map),所以「核心 top-N」是折叠后的度数——不能简单对原始 relations 聚合,需要折叠维度的度数/边。

**用户确认**:搜索 = FTS5 词法 + SP2 ANN 语义双路;index-backed 大库去掉「全部」档位,改「核心 N + 搜索 + 邻域展开」;尽可能并行实现。

## 架构总览

延续「离线预计算、查询有界」:把**折叠后的 viz 图**在(离线)构建期持久化,查询只取有界子集,绝不 RAM 物化全量。

## Part A — 可视化有界化

**构建期(由 SP2 `build_scale_index` 一并产出,持久化到 `{storage_dir}/kg_index/{nb}/viz.npz` + `viz_meta`)**：
- 持久化**紧凑折叠 viz 图**:canonical 节点 id 列表 + 每节点折叠**度数**(int32)+ 折叠边(canonical→canonical,去重,CSR/压缩 npz)+ 每节点 object_type。名字**不进索引**(按需回 DB)。
- 与 scale 索引**同 manifest version**:`_scale_index(nb)` 判有效时 viz 图一并有效;KG 变动→version 失配→需重建(`build_scale_index` 重跑,SP3 的 run_kg kg→index 链已覆盖 base 层)。

**查询期 `unified_graph(notebook_id, level, limit)`**:
- 有有效 viz 索引 → 取折叠度数 **top-N** 节点 + 其**诱导折叠边**,按这 N 个 canonical 的成员回 `knowledge_objects` hydrate 展示名(O(N≤320),无 `_unified_graph_full`)。返回与现 `unified_graph` 同形(nodes/edges/total_nodes/total_edges/truncated)。
- 无索引/小库 → 维持现 `_unified_graph_full`(不回归)。

**邻域展开端点 `GET /notebooks/{id}/objects/{oid}/neighbors?cap=N`**:从**持久化折叠 viz 图**取该 canonical 的 1-hop 折叠邻域(节点+边,与 viz 同折叠维度,避免 object 级与 canonical 级混淆),`cap` 有界;返回 {nodes, edges} 同 viz 形。无索引小库 → 用 `knowledge_relations` 邻接索引兜底。供前端逐跳展开。

## Part B — 搜索有界化(FTS5 词法 + ANN 语义)

**FTS5 词法索引**:
- 新建 FTS5 虚拟表 `kg_objects_fts`(`tokenize='trigram'` 保子串语义,对齐现 LIKE),列:`object_id`(unindexed)、`notebook_id`(unindexed)、`name`。
- 维护:`store_kg` 写对象时同步 upsert FTS 行;KG 删除/重抽时清理;对**存量** notebook 提供 backfill(随 build_scale_index 或一次性迁移)。base 离线构建时自然填充。
- 查询:`SELECT object_id FROM kg_objects_fts WHERE notebook_id=? AND kg_objects_fts MATCH ? LIMIT k` → 有界 id。

**语义(复用 SP2 ANN)**:embed query → 持久化 hnswlib `knn_query(k)` → 语义相近节点 id(仅 scale 索引存在时)。

**新端点 `GET /notebooks/{id}/kg/search?q=&k=`**:FTS5(词法)∪ ANN(语义)合并去重,各带 `match`(lexical/semantic)+score,hydrate 名/类型,**有界**返回 `[{object_id, name, object_type, score, match}]`。小库/无索引 → 回退现 `search_notebook`(已 LIMIT)。

## Part C — 前端(KG 视图改造)

- **核心视图**:fetch 不变(`/unified-kg?limit=N`),后端对大库改有界路径,前端无感。
- **搜索**:删除 `uGraphFull` 懒加载(`fetchUnifiedGraph(nb,0)`)+ 客户端过滤,改调 `/kg/search` → 渲染命中节点;点节点 → 调 `/objects/{oid}/neighbors` 逐跳展开并并入当前视图。
- **「全部」档位**:index-backed 大库去掉(`KG_RANGE_STEPS` 据 `base_kg_available`/索引标志动态);小库保留。
- 按 [[ui-polish-bar]]:命中列表/展开交互对齐精致,改完给视觉验证(show_widget 或 preview)。

## 测试

- **viz 等价**:有界路径(top-N 折叠节点 + 诱导边)== 现 `unified_graph(limit=N)` 在小库上的结果(无回归);hydrate 名正确;neighbors 端点 1-hop 有界正确;viz 索引持久化 + version 失效。
- **搜索**:FTS5 trigram 子串命中;ANN 语义命中;两路合并去重 + match 标注;backfill 存量;小库回退;空查询/无结果。
- **前端**:tsc + 现有测试;搜索改服务端后不再拉全量(断言不发 `limit=0`);「全部」档位按索引存在与否显隐。
- **gated 规模慢测**:合成大库,viz top-N + `/kg/search` 在 10^5+ 下有界/快(记录耗时)。

## 风险与预算

- **折叠 viz 图持久化**:与 SP2 scale 索引同目录/同 version;rebuild/重抽后需重建(随 build_scale_index 或 rebuild 钩子)。10^6 折叠节点 + 折叠边的 CSR ~百 MB(可接受)。
- **FTS5 维护一致性**:`store_kg` 同步写 + 删除清理 + backfill;若漏维护则搜索缺漏 —— backfill 兜底 + 测试守护。trigram FTS5 体积约为原文 3x(子串代价),对 name(短)可接受。
- **等价**:有界 viz 的 top-N + 诱导边必须与折叠全图的同 N 子集一致(等价测试守护)。
- **语义搜索仅在 scale 索引存在时**:无索引库只走 FTS5/回退,文档化。

## 实施分期(可并行)

- **P1 后端搜索**(`fts.py`/repo + routes):FTS5 表 + 维护 + backfill + ANN 语义 + `/kg/search`。**与 P2 可并行**(不同关注点)。
- **P2 后端 viz**(viz 图持久化 + 有界 `unified_graph` + neighbors 端点)。**与 P1 可并行**。
- **P3 前端**(搜索改服务端 + 去「全部」+ 点击展开)。依赖 P1/P2 端点。
- 测试/规模慢测随各 Part。
