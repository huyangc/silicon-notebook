# 设计：领域基础 KG 的规模化摄取/聚类(SP3)

- 日期：2026-06-29
- 范围：SP3 —— 让 unified 聚类(`rebuild_unified_kg`)与离线摄取在 5M+ object 规模下**内存严格有界**,且 `batch_ingest` CLI 与之一致。
- 依赖：基于 SP2 分支(`build_scale_index` 等),SP2 合并后 rebase 到 master。
- 不在本 spec：SP1(可视化/搜索规模化);SP2(检索基底,已 PR#111)。

## 背景与动机

「基础 KG」可达 5M+ object。当前 `rebuild_unified_kg` 在该量级因「物化全量」会 OOM:

1. **向量加载(最大墙)**:`vectors = {oid: json.loads(vec) for ...}` 把全部向量建成 Python dict —— 5M×1536 float 约 30GB+(Python float 比 numpy 更糟)。`sqlite_repository.py:3972-3974`。
2. **reps 矩阵**:`cluster_concepts` 在 RAM 里按 name-seed 求代表向量;唯一 seed 多时 reps 又一个大矩阵。
3. **concept_clusters 写**:单 DELETE + 逐行 INSERT 循环(无分块),5M 行很慢。对比 `store_kg` 已 1000/事务分块(`sqlite_repository.py:2808`)。
4. **incremental Tier2**:再次全量 load 向量,已被 `kg_incremental_tier2_max_entities=50000` 门控(超 50K 只 Tier1)。

**结构性洞察**:unified 聚类成本随**唯一归一化名(name-seed)数**走,不是随总 object 数走 —— 精确名分组先把 5M 提及坍缩成远少的唯一名(一个垂域通常 10^4–10^6 唯一概念名),向量 ANN 只在 reps(每唯一名一条代表向量)上跑。SP3 让内存随**唯一名数**有界,与总 object 数解耦。

**用户确认**:规模目标多百万(5M+),内存必须严格有界;基础 KG 离线批量构建、基本静态。

## CLI 一致性(硬要求)

`rebuild_unified_kg` 同时被 **CLI `run_kg`、服务端「重新合并」(`unified-kg/rebuild`)、`incremental_fuse_source`** 调用。SP3 的有界实现必须是**唯一实现**,三方共用,**不分叉出 CLI 专用路径**。此外 CLI 需:kg 后自动重建 SP2 索引(防陈旧)、`--limit` 语义明确一致、大库阶段进度可观测。

## 架构:流式、随唯一名数有界的聚类管线

同一个 `rebuild_unified_kg`,行为与现实现**等价**(等价测试守护),改为三趟流式:

```
Pass A: 流式扫 knowledge_objects(id+payload.name)→ seed=_norm(name)(+acronym alias)
        → 写 SQLite 临时表 tmp_obj_seed(object_id, seed)。不在 RAM 堆 5M 字符串。
Pass B: SELECT t.seed, e.vector FROM knowledge_embeddings e JOIN tmp_obj_seed t
        流式累加 reps_sum[seed]/reps_count[seed] → reps[seed]=mean。
        内存 = #唯一seed × dim(与总数解耦)。
聚类:  kg_merge.cluster_seeds(seeds, reps, confirmed, rejected, ...) →
        {seed: canonical} + canonical_names + auto_candidates + pending。有界于 #seeds。
Pass C: 扫 tmp_obj_seed → canonical=seed→canonical,缓冲 1000 行/事务写 concept_clusters
        (替换现单 DELETE + 逐行 INSERT)。
```

LLM 复审(auto_candidates)、concept 描述生成保持现状(本就 bounded、非 O(N))。per-type(claim/formula/procedure)走同一流式 `cluster_seeds` 路径(实现前先核对其是否启用向量 tier:若启用则同样流式求 reps,若纯精确名分组则只需 Pass A+C);分块写。

## 组件与接口

- **`kg_merge.cluster_seeds(seeds, reps, confirmed, rejected, hi, lo, max_pending) -> dict`**(新,纯函数)：把现 `cluster_objects` 的逻辑入口上移到 seed 级 —— acronym alias、union-find on confirmed、rep-ANN candidates(`_ann_candidates`)、`_star_groups`、auto/pending 划分。返回 `{seed_to_canonical, canonical_names, auto_candidates, pending}`。现 `cluster_concepts`/`cluster_objects` 改为「构造 reps 后委托 cluster_seeds」,保持对外行为不变。
- **`SQLiteRepository._stream_seed_reps(db, notebook_id, object_type, seed_fn) -> (reps, seed_counts)`**(新)：Pass A+B,建临时表 + 流式求 reps。临时表用 `CREATE TEMP TABLE`(连接级,自动清理)。
- **`SQLiteRepository._write_cluster_map_streamed(db, notebook_id, object_type, seed_to_canonical, canonical_names)`**(新)：Pass C,分块写。
- **`rebuild_unified_kg`** 重写为编排上面三步 + 现有 LLM 复审/描述/pending 刷新/状态写;行为等价。
- **`write_clusters`** 改为分块 executemany(1000/事务);pending 候选写也分块。
- **config**：`kg_cluster_rep_ann_max: int`(reps ANN 上限;超则分片建索引 + WARNING)。

## rep-ANN 有界化

`cluster_seeds` 近邻候选仍用 hnswlib(不引新依赖)。#seeds 通常 ≤ ~1M → reps ~6GB 可接受。超 `kg_cluster_rep_ann_max` 时**分片建 ANN + 显式 WARNING**(绝不静默截断)。faiss IVF-PQ 磁盘索引记为「rep 数本身爆 RAM」的逃生口,本期不引入。

## CLI 编排(`batch_ingest`)

- **单一实现**:`run_kg` 仍调用同一个 `rebuild_unified_kg`(现已有界)。服务端/incremental 不变、自动受益。
- **kg→index 链**:`run_kg`(及 `all`)在 `rebuild_unified_kg` 后,若 notebook 是 base tier **或**已存在 scale 索引 → 自动 `build_scale_index`(防陈旧)。`all` = ingest→kg→index 不变。
- **`--limit` 语义 + 分批工作流**:明确「`--limit` 只限本轮抽取条数;最终聚类始终覆盖全量」写进 `--help`/README。新增:
  - `--no-rebuild`:`kg` 只抽取、跳过末尾 `rebuild_unified_kg`(+ index)。用于「分批抽取」。
  - `--rebuild-only`:`kg` 跳过抽取、只跑 `rebuild_unified_kg`(+ index)。用于分批抽取后收尾。
  - 工作流:多次 `kg --limit N --no-rebuild` → 末尾一次 `kg --rebuild-only`。
- **进度/可观测**:抽取 `i/N`、`reps built (seeds=X)`、`writing clusters X/Y`、`building scale index` —— 经 batch_ingest 现有日志 + event_log。

## 测试

- **等价测试(核心,守无回归)**：小库上流式 `rebuild_unified_kg` 的 cluster_map == 现实现(同 confirmed/rejected);`cluster_seeds` 与现 `cluster_concepts` 在小输入上 cluster_map 一致。
- **有界性测试**：streaming 路径不建全量向量 dict —— 断言 reps 数 == #唯一seed;mock/spy 验证未发生全量 `{oid: vector}` 物化。
- **分块写测试**：concept_clusters 按 ≤1000/事务(spy 写次数或断言不在单事务)。
- **临时表清理**：`tmp_obj_seed` 为连接级 TEMP,rebuild 后不残留。
- **CLI 测试**：`run_kg` 后 base notebook 的 scale 索引被重建(version 与 DB 一致,非陈旧);`--no-rebuild` 跳过 rebuild;`--rebuild-only` 跳过抽取;`--limit` 只限抽取。
- **gated 规模慢测**：合成大量唯一 seed,验证流式聚类内存有界 + 完成(记录峰值/耗时)。

## 风险与预算

- **等价性**：流式重写必须与现聚类**结果一致**(等价测试守护);acronym alias / union-find / star-groups 逻辑搬移到 `cluster_seeds` 时不得改语义。
- **临时表**：用连接级 `TEMP TABLE` 避免并发/残留;注意 `_write()`/`_connect()` 的连接生命周期(rebuild 全程同一连接)。
- **rep-ANN RAM**:#seeds 极大时 hnswlib 仍可能爆 → 分片 + WARNING;faiss 逃生口文档化。
- **CLI 共用**:服务端「重新合并」按钮与 incremental 共用同一 `rebuild_unified_kg`,重写后需跑其回归(unified_kg、incremental fusion、ask graph 模式)。
- **id→seed 临时表**:5M 行临时表占磁盘(非 RAM),可接受;建在 notebook-scoped、rebuild 结束即弃。

## 实施顺序(writing-plans 细化;标注可并行)

1. `kg_merge.cluster_seeds` 抽取 + 等价单测(纯函数,**可与 3、6 并行**)。
2. `rebuild_unified_kg` 流式化(Pass A/B/C + 临时表 + 分块写)+ 等价/有界测试(依赖 1)。
3. config `kg_cluster_rep_ann_max` + rep-ANN 分片/WARNING(**可与 1 并行**)。
4. `write_clusters`/pending 分块写(**可与 1、3 并行,但与 2 同文件需协调**)。
5. CLI:`--no-rebuild`/`--rebuild-only` + kg→index 链 + 进度(依赖 2)。
6. README 中英(**可与 5 并行,最后合**)。
7. gated 规模慢测 + 全量回归(末尾)。
