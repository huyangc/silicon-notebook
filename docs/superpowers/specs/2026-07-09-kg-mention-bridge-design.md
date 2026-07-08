# P2: 共提桥接层（mention bridge）— Design

日期：2026-07-09。前置：P0（PR#226）/P1（PR#227）已合 master。

## P2-A 验证结论（沙箱 = 真实库拷贝 + 合并后代码 rebuild，全部实测）

**症状回顾**：对比/横向题三模式坍缩（只引用被点名那篇）——根因是图里没有跨文档「兄弟」结构。

1. **社区层不提供兄弟结构**：4 组教科书兄弟集（MHA/MQA/GQA/SWA、RoPE/ALiBi、MoE/Dense、SFT/RLHF/DPO）全部落在**不同** Louvain 社区，两两直连 canonical 边 1/18。社区尺寸两极：top10 全为 700-1118 的巨型 blob（单社区 ≈ 全部簇的 16%），中位数 5——Louvain 在以文档内边为主的稀疏图上聚出的是文档邻域，不是语义同类。
2. **emb_synonym 边也不桥兄弟**：兄弟对余弦 0.27-0.68 << 0.83 同义阈值（阈值语义就是同义词，不该降）。
3. **消费面审计**（A1）：`expand_community`（reasoning reflect 动作）与 `ask_chunk` 对比题检测**都已接线**且机制正确（peer 名→子查询扩展），但它们喂的是 `community_peers`（Louvain 社区成员）→ 按 1 实测返回的是噪声。另：`ask_graph` 的社区摘要注入是**死代码**（`summarize_communities` 无生产调用方 + `summary!=''` 过滤 → 恒空）；报告引擎对 communities 表零直接引用（仅经 ReasoningRetriever 继承 expand_community）。
4. **共提桥信号充足**（带缩写别名的词边界匹配）：8,784 条桥 claim（同一条 claim 提及 ≥2 个跨源概念）→ 5,796 个桥接对；定点验证 GQA↔MQA=16、MQA↔MHA=23、RLHF↔DPO=2、DeepSeek 家族 165-302；已知盲区 RoPE↔ALiBi=0（本语料无共提 claim）。噪声模式明确：泛词别名（"model" 2,308 对）→ 需 DF 上限门。
5. stitch（共现簇对 LLM 裁决）候选 589 对（≥5 源），但头部被「热门×热门」支配（GQA×RoPE 15 源），预期产出率低 → **推迟**。

**选型**：不修 Louvain（图本身缺兄弟边，调参无解）、不上 LLM stitch（低产出）；做**确定性共提桥接层**——一次 rebuild 内从 claim 文本提取 mention 边与共提对，直接替换 `expand_community` 的数据源，并给 PPR 图补 claim→concept 跨文档导通。零 LLM、零重抽。

## 设计

### 1. 派生表（`_migration_9` + baseline 双写 + SCHEMA_VERSION=9）

```sql
CREATE TABLE IF NOT EXISTS mention_edges (
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    claim_object_id TEXT NOT NULL,          -- 原始 claim 对象 id(PPR 节点身份)
    concept_canonical_id TEXT NOT NULL,     -- 命中的跨源概念 canonical
    matched_alias TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (notebook_id, claim_object_id, concept_canonical_id)
);
CREATE TABLE IF NOT EXISTS concept_comentions (
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    canonical_a TEXT NOT NULL,              -- a < b 定序
    canonical_b TEXT NOT NULL,
    bridge_claims INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (notebook_id, canonical_a, canonical_b)
);
```
`unified_kg_state` 加 `mention_seq INTEGER NOT NULL DEFAULT -1`（canonical_rel_seq 同款）。

### 2. 构建 `rebuild_mention_bridge(notebook_id, force=False)`（seq 闸 + fail-open，挂 rebuild 尾部与跳过分支）

- **别名表**：跨 ≥2 源的 concept 簇 → {canonical_name 全名, 去括号头名, 括号缩写(3-8 位字母数字,绕过 Latin 长度门——GQA/MQA 类 3 位缩写是最有价值别名)}，Latin 别名 len≥4、CJK 别名 len≥3（trigram FTS 最短查询长度=3；2 字中文名多为高频泛词，放弃可接受）；来源即 P0 的 `_strip_paren_acronym` 语义。缩写 len<3 者（如 "V2"）不入 FTS 词表。
- **匹配**：rebuild 作用域的**临时 contentless trigram FTS**（对齐仓库 kg_objects_fts 的 trigram 选型；避免 Python 大词表 regex 在部署规模的性能墙，也不引新依赖）建于 claim 名文本；每别名一条 phrase MATCH → 候选 claim；**Latin 别名对候选文本再做 `\b` 词边界后校验**（防 trigram 子串误命中如 rope⊂europe），CJK 别名子串即命中。FTS 表用完即 DROP。
- **DF 上限门**：命中 claim 数 > `mention_alias_df_cap`（默认 2%×claims）的别名整体丢弃（泛词如 "model"），并计数入事件（不静默）。
- **写出**：mention_edges（claim→concept canonical）；同一 claim 命中的 canonical 组合两两计入 concept_comentions（a<b）。单写事务 DELETE+批量 INSERT+seq 写回。
- **开关**：`mention_bridge_enabled` 默认 **on**（确定性、有界；与 emb_synonym 默认开一致）。

### 3. 消费 A：`sibling_peers` 替换 expand_community 的数据源

- 新函数 `sibling_peers(repo, base_nb, focal_name, top_k)`（communities.py 旁）：focal 名 → canonical（复用 community_peers 的解析），`SELECT` concept_comentions 两侧 `ORDER BY bridge_claims DESC LIMIT top_k`，过滤 `bridge_claims >= sibling_min_bridge`（默认 2），返回 [(canonical_name, bridge_claims)]。
- **接线**：`reasoning_retrieval.expand_community` 分支与 `ask_chunk` 对比题路径改为**先 sibling_peers，空则回退 community_peers**（表缺/滞后/flag 关时行为与今日一致）。trace step_type 不变（前端「对比」标签零改动），summary 文案标注来源（同类共提 vs 社区）。

### 4. 消费 B：PPR 图补 claim→concept 跨文档导通

- mention_edges 以 `(claim_object_id, f"cluster:{concept_canonical_id}", mention_edge_weight)`（默认 0.5，同 variant 边量级）注入三个图构建点：`_ppr_graph`、`_federated_rx_graph`、scale index 图构建（emb_synonym 注入点旁）。cluster router 节点已存在（P1 前即有）；claim 节点即实体节点。
- 效果：GQA↔桥claim↔MQA 的 2-hop typed 导通进入 PPR 质量流（graph/reasoning 检索）。

### 5. 非目标（YAGNI/推迟）

- 不改 Louvain 输入、不删 ask_graph 死注入（单独小 PR 或留给 summarize 功能救活时处理——本 spec 只记录事实）。
- 不做 LLM stitch（数据显示低产出；若 mention 桥上线后对比题仍不达标再议）。
- mention 边不进 knowledge_relations / 不进边审查队列 / 不进 viz 图（避免 1.6 万派生边污染原始层与审阅面）。
- 前端零改动（expand_community 表面不变）。

### 6. 效率账

- rebuild 新增：临时 FTS 建表 O(claims) + 别名 MATCH（本机 890 别名×29k claims 实测秒级；部署 5 万别名×40 万 claims 估分钟级，一次性、seq 闸防重复、flag 可关）；无 LLM/embed 调用。
- ask 侧：sibling_peers 一条索引查询；PPR 图构建多一次 mention_edges 全读（挂现有版本缓存的图构建内，量级 ≤ 数万行）。

### 7. 测试

- 迁移 v8→v9（含已部署库回拨补建用例）。
- 匹配器：全名/头名/缩写命中、`\b` 后校验（rope 不命中 europe）、CJK 子串、DF 门丢弃泛词、短别名不入词表。
- 构建：桥对计数正确（GQA/MQA 式 fixture）、seq 闸跳过/force、fail-open。
- sibling_peers：排序/下限过滤/回退 community_peers。
- 接线：reasoning expand_community 用共提 peer（fixture 断言子查询包含兄弟名）；ask_chunk 对比路径同；PPR 图含 mention 边（build_ppr_graph 输入断言）。
- 回归：全量 pytest。

## 生效方式

合并部署后下一次「刷新图谱」自动构建（mention_seq=-1 首建）；与 P0/P1 同一次 rebuild 完成。中文语料同样受益（CJK 别名子串匹配）。
