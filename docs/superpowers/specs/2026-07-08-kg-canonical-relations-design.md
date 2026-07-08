# P1: Canonical 关系层 + answer-context 联邦盲区修复 — Design

日期：2026-07-08。前置：KG 实体合并基线评审（同日）确认——`knowledge_relations` 跨源边严格 0 条；折叠到 canonical 端点后 585/50,519 条边有 ≥2 源支持、921 条有重复实例，但该聚合只在 `rebuild_communities` 内存中算完即弃（sqlite_repository.py `ew` dict）；`_answer_context` 的簇折叠与关系查询只看 active notebook，base 命中既不去重也不出关系。方向已获用户确认（评审报告「建议 P1」+「继续」）。

## 目标

1. 把「这条关系被多少来源支持」变成持久化、可消费的一等数据（图谱边权/悬停、ask relations 行排序与标注）。
2. 修 `_answer_context` 两个联邦盲区：跨 tier canonical 折叠、base 库关系可见。

**如实预期**：这是可信度展示与联邦正确性修复，不是跨文档边密度革命（基准库仅 ~1.2% 边有跨源支持）。

## 非目标（YAGNI）

- 不改 `rebuild_communities` 的内存聚合（将来可改读本表，v1 不动）。
- 不动关系检索（PR#59 RELATION_RETRIEVAL_ENABLED）的嵌入文本。
- 不做增量维护：表随 `rebuild_unified_kg` 重算（communities 同款 seq 闸），新源产生的关系在下一次 rebuild 后进入聚合；期间原始关系照常可见，只是计数滞后。
- 不加边点击交互/边评审入口（边审查队列已有独立入口）。

## 设计

### 1. 表 `canonical_relations`（派生，rebuild 全量重写）

```sql
CREATE TABLE IF NOT EXISTS canonical_relations (
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    canonical_src TEXT NOT NULL,        -- 折叠后端点(COALESCE(cluster.canonical_id, 原object_id))
    edge_type TEXT NOT NULL,
    canonical_tgt TEXT NOT NULL,
    support_count INTEGER NOT NULL DEFAULT 1,   -- 原始关系行数
    source_count INTEGER NOT NULL DEFAULT 1,    -- 非NULL source_id 去重数(≥1时至少1;全NULL记1)
    sample_relation_ids TEXT NOT NULL DEFAULT '[]',  -- 原始 knowledge_relations.id, cap 5, JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (notebook_id, canonical_src, edge_type, canonical_tgt)
);
```

- **方向保留**（区别于 communities 的无序对）：viz 边有箭头、edge_type 有方向语义。
- **折叠规则** = communities 同款 SQL LEFT JOIN `concept_clusters`（全类型 cluster_map，K-/KL-/KF-/KP- 前缀天然隔离）；未聚类端点用原 object_id。
- **排除**：`review_status='rejected'` 的边（对齐 `_federated_rx_graph`；communities 现状不滤属既有差异，不在本 PR 收敛）；折叠后自环（src==tgt）丢弃（对齐 `derive_unified_graph`）。
- **source_count**：`COUNT(DISTINCT source_id)` 忽略 NULL（relink 边 source_id 可为 NULL），但下限 1。
- **迁移**（守 schema-migration-convention）：CREATE 进 `_migration_1` baseline **且** 新增 `_migration_8`（已部署库补建）；`unified_kg_state` 加 `canonical_rel_seq INTEGER NOT NULL DEFAULT -1`（`_migration_1` 内 `_add_column_if_missing` **且** `_migration_8` 守卫 ALTER）；`SCHEMA_VERSION = 8`。

### 2. `rebuild_canonical_relations(notebook_id, force=False) -> int`

- **seq 闸**：镜像 `rebuild_communities`——`canonical_rel_seq == kg_mutation_seq` 且表非空则跳过（force 绕过）；重写完成后把 `canonical_rel_seq` 置为函数入口捕获的 seq。
- **实现**：流式 cursor 跑折叠 JOIN（communities :7012-7036 同款 + `review_status!='rejected'`），python dict 聚合 `(src,type,tgt) -> {support, sources(set of non-null), samples[:5]}`（内存以 #canonical 对为界，基准库 5 万级、部署库估 <100 万，均可承受）；单写事务 `DELETE WHERE notebook_id` + 分批 `executemany INSERT`。
- **调用点**：`rebuild_unified_kg` 尾部（`build_viz_index` **之前**，viz 要消费计数）+ 跳过分支（与 `rebuild_communities(level=0)` 并排，同样 try/except fail-open + 事件 `canonical_relations_rebuild_failed`）。

### 3. 读侧消费 A：图谱边计数

- **注解 helper** `_annotate_edge_support(notebook_id, edges) -> edges`：一次 `SELECT canonical_src, edge_type, canonical_tgt, support_count, source_count FROM canonical_relations WHERE notebook_id=?` 载 dict（挂 `_vector_cache`，版本 = `canonical_rel_seq`）。**查表键须先把边端点过 `cluster_map` 折叠**：`(cmap.get(s, s), edge_type, cmap.get(t, t))`——`derive_unified_graph` 只折叠 concept 端点（claim/formula/procedure 保原始 object_id），而本表按全类型折叠；concept 端点已是 canonical id、不在 cluster_map 键中，`get(s, s)` 恒等通过。命中则附 `support_count`/`source_count` 字段，未命中不加（表空/滞后 → 前端优雅缺省）。
- **接线**：`unified_graph` 全量路径（`_unified_graph_full` 输出后）、`_unified_graph_bounded`、`/objects/{oid}/neighbors`（`kg_neighbors`）三处出口统一过 helper。
- **viz 磁盘索引不改格式**：bounded 路径从 npz 读出边三元组后同样过 helper（计数来自表而非 npz，避免 viz.npz 版本迁移；helper 的 dict 载入按 seq 缓存，成本一次 O(#pairs)）。

### 4. 读侧消费 B：`_answer_context` 联邦修复

- 新 helper `_participant_notebook_ids(notebook_id) -> List[str]`：`[active] + SELECT id FROM notebooks WHERE tier='base' AND id != ?`（与 `_ppr_graph`/`federated_retrieve` 现有 8 处内联同谓词；v1 只在新代码 + `_answer_context` 使用，不重构存量调用点）。
- **折叠**：`cmap` 改为 participants 的 `cluster_map` 合并 dict（各自版本缓存命中，合并 O(条目)；canonical_id 字符串 nb 无关 → base/personal 同名概念天然折叠去重）。
- **关系行**：对每个 participant 跑现有 in-context 端点对查询（endpoints IN id_map），union 去重 `(s,type,t)`；行生成后按 canonical 聚合 `source_count` 降序排（`canonical_relations` 按边所在 notebook 查），**≥2 源标注** `k1 -[defines]-> k3 (×3源)`；cap 30 不变。
- 受益方：reasoning 模式 + 深度报告（`_draft_section`）。graph 模式（`render_subgraph_context`）不动。

### 5. 前端（同 PR，AGENTS.md 全栈奇偶律）

`frontend/app/page.tsx`（现状：`react-force-graph-2d`，linkWidth 定值 1.35，linkColor 定值，`UnifiedEdge` 三字段）：

- 类型：`UnifiedEdge`/`FgLink` 加可选 `support_count?: number; source_count?: number`（FgLink 构建处透传）。
- **linkWidth**：`1.35 + Math.min((source_count ?? 1) - 1, 4) * 0.5`（单源保持现状 1.35，5+ 源封顶 ~3.35——克制、对齐现有视觉）。
- **linkLabel（悬停）**：`source_count ≥ 2` 时追加 ` · ${source_count} 源支持`。
- **中点标签 pill**（`drawKgLinkLabel`，非 dense 视图）：`source_count ≥ 2` 时文本追加 ` ×${source_count}`。
- **侧栏关系列表**（`.kg-relation-row`）：`source_count ≥ 2` 的行加 `<span className="tag">×N源</span>`（复用现有 tag pill 样式，保持列对齐）。
- 校验：`npm run lint`（tsc）+ `npm run test` + 视觉验证（preview 工具截图）。

### 6. 效率账（efficiency-first）

- 新增成本：rebuild 时一次 O(E) 流式折叠（基准库 5 万边 <1s；与 communities 同量级，seq 闸防重复）；ask 时 helper dict 缓存命中为 O(1) 查、未命中一次 O(#pairs) 载入（挂现有 `_vector_cache` 版本机制）；无新增 LLM/embed 调用。
- `_answer_context` 增量：≤2-3 个 participant 的 cluster_map 合并（各自已缓存）+ 每 participant 一条 IN 查询（≤30 端点）。

### 7. 测试

- 迁移：全新库建表/加列；**已部署库路径**（user_version=7 的库跑 `_migrate` 后表与列存在——schema-migration-convention 的教训用例）。
- 聚合正确性：多源支持计数、rejected 排除、自环丢弃、NULL source_id、sample cap 5、方向保留（A→B 与 B→A 不合并）。
- seq 闸：重复 rebuild 跳过 / force 重算 / kg_mutation_seq 变化后重算。
- 读侧：unified_graph 全量+bounded+neighbors 三出口注解；表空时字段缺省。
- `_answer_context`：base/personal 同名概念跨 tier 折叠成一行；base 库两命中间的关系出现在 relations 行；`×N源` 标注与排序。
- 前端：tsc 绿 + node --test 存量绿 + 视觉截图。

## 生效方式

合并部署后下一次「刷新图谱」/rebuild 自动建表内容（`canonical_rel_seq=-1` 保证首建）；无需手工步骤。与 PR#226（P0 归一化）独立可并行合并，若都合并则 rebuild 一次同时生效。
