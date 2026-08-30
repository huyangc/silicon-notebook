# R3：KG 审核队列 / 查重 / 概念详情热路径缩窄（设计与实施规格）

热路径修复计划批 2·R3（审计 KG-2/KG-3/KG-4 + 生产诊断 §6 review_count 建议）。
三条红线不变：不降检索性能、不降 KG 抽取性能、不改问答结果质量。本文是实现
契约与评审锚点；实现拆两个 PR（PR-A 审核队列、PR-B 查重+概念详情）。

## 现状病灶（审计 + 本轮摸底核实）

- **KG-2（P0）** `review_queue`（knowledge_governance.py:207）每请求把全 notebook
  非 rejected 关系全量 fetchall（8.35M 行 + 分批端点对象 payload），Python 算
  corroboration/trust/priority 后 `heapq.nlargest(limit)`；`limit=100` 只裁输出。
  每次审核决定 bump `kg_mutation_seq`，下个请求整队重来。
- **centrality**：`_edge_centrality_map` 是 rustworkx betweenness（top-20k 度数
  收窄），version-cache 键 `_scale_index_version`；审核动作 bump seq 恰好把它
  打失效——**而 verified/pending 翻转根本不改变任何排序输入**（trust/corr/
  centrality 都不读 `review_status`；只有 reject 改拓扑与 corr 分组）。
- **review_count（§6）**：`COUNT(*) WHERE notebook_id AND review_status!='rejected'`
  作为业务查询目前不存在（诊断探针是预铺路）；索引
  `idx_knowledge_relations_nb_review` 已备好；前端拿不到真实队列总量。
- **KG-3（P0）** `find_duplicates`（knowledge_governance.py:1730）把该类型全部
  对象连 payload+evidence 全列取回（`_knowledge_objects`），分块/打分全在
  Python；读侧无界。
- **KG-4 应用侧** `concept_cluster_detail_rows`（knowledge_store.py:2996）无
  LIMIT 无分页——hub 簇成千上万成员整批返回（含全份 payload/evidence），
  邻接查询 `IN (member_ids)` 无上限。索引半已由批 1 修复。

## 关键设计判断（v2，经 opus 设计评审修订）

1. **归一化不下推 SQL**。`edge_trust._norm` / `kg_merge_seed._norm`（NFKC+\w）
   与 PG `lower()`/regexp 在非 ASCII 上不保证逐字一致（İ/K 类折叠分歧）。
   计划里「SQL 分块与 Python 归一化逐字一致」的要求改由**两趟取数**达成：
   归一化留在 Python，候选集合等价按构造成立，无对账残差风险。
2. **「ec>0 缩窄候选」不成立（设计评审 B1，已实测）**：rustworkx
   `digraph_edge_betweenness_centrality` 对图内每条边恒正（边在自身两端点的
   最短路上）。因此 v1 设想的候选缩窄/补位查询**整体放弃**——20k 节点以下的
   库候选=全部边，`id=ANY` 分批严格劣于现单次扫描。KG-2 的读侧主修复改由
   **T-A2 的排名 memo 承担（降频：每拓扑版本算一次，不再每请求一次）**；
   T-A1 只做严格等价的取数瘦身。诚实口径：**冷路径量级不变（降频不降幅）**，
   `_edge_centrality_map` 的加载路径本批不动，reject 后的一次重算不可避，
   登记为后续观察项。
3. **verified/pending 翻转不改变任何排序输入**（trust/corr/centrality 均不读
   `review_status`，逐链核实）；只有 reject 改拓扑与 corr 分组。memo 对
   verified/pending 做 carry-forward，reject 才失效。跨进程安全性靠 seq
   严格比对（期望 old_seq+1，不符则整条丢弃），carry 只是同进程审核循环的
   快路径，正确性不依赖它。
4. **并列序**：排序 SQL 无 ORDER BY，但既有测试（
   test_governance_read_narrowing.py:184/308/389/415、
   test_query_hotpath_cache.py:661）以「同输入序」为前提逐位断言新旧一致。
   修订后设计不改变取数顺序与排序路径，这些测试**原样保留**继续生效，
   不迁移、不放宽。

## PR-A：审核队列（KG-2 + review_count）

### T-A1 取数瘦身（严格等价，先行）

`review_queue_rows` 的关系扫描与整体流程**不动**（同一 SQL、同一顺序、同一
Python 打分与 `heapq.nlargest`）。唯一改动：端点对象批取的投影从整列
`payload` 改为 SQL 侧提取 name——

- PG：`SELECT id, object_type, payload->>'name' AS name FROM knowledge_objects
  WHERE notebook_id=%s AND id=ANY(%s)`（保留 notebook 过滤与去重批取——
  **不得**把 name 挪进关系 JOIN：那会 8.35M 边 × 2 次 payload 解引用替代
  ~50 万次去重取值（读放大），且外库端点会拿到真实 name 改变 corr 三元组
  （跨库语义回归，test_governance_read_narrowing.py:291-308 钉着）。
- SQLite：照既有 ⚠ 契约——裸 `id IN (...)` **不带** notebook_id 谓词 +
  投影 notebook_id + Python 过滤（sqlite/governance_store.py:332-352 记录的
  0.138s→14.155s 规划器回归；SQL 文本测试已钉），投影加
  `json_extract(payload,'$.name')`。
- 服务层 `node_names[id] = str(name) if name is not None else ""`：两侧统一
  coerce。**登记的健壮性变化**（与 anchor 下推同类）：payload.name 为非字符串
  （如数字）时，旧路径 `_norm(int)` 抛 500，新路径按其文本参与打分；两侧
  （PG `->>`/SQLite json_extract+str()）归一到同一文本。
- `node_types` 继续由**过滤后**的端点批取构建；展示用 `_src_type/_tgt_type`
  继续来自关系 JOIN（现状的刻意不对称，禁止「顺手统一」）。
- corroboration_counts / compute_trust_score / nlargest 一概不动。

验收：既有五处逐位等值断言原样通过；新增 name 非字符串用例（旧路径 raise、
新路径打分正常，两侧一致）。

### T-A2 排名 memo + carry-forward（P0 主修复，依赖 T-A1）

- 新 `ReviewQueueMemo`（app/services 下新模块，照 CopyStatsMemo 先例
  runtime-owned：由 runtime 组装挂到明确 seat——挂
  `RetrievalSnapshotCache` 旁的治理位或新属性，
  test_repository_runtime_composition.py:275/303 冻结的属性集与
  ownership_manifest.py **同 diff** 更新）。single-flight +
  `threading.Lock` + per-notebook epoch + 有界 LRU（≤512 本，淘汰
  fail-closed 推进 epoch，照 counts_cache 的 pending memo——**指名对齐
  epoch 保护最严的那两个**（pending/visible_pending），不照无 epoch 的
  type_status_counts）。
- 键 = `kg_mutation_seq`。**失效完备性论证（须写进模块 docstring）**：
  review_queue 的全部输入 =（非 rejected 关系行、端点对象 payload.name 与
  object_type、centrality map）。前两者的每条生产写路径都经
  `mark_unified_kg_dirty`（模块唯一前进点）；centrality 是这两者的纯函数
  （其 vector_cache 键另有 settings 维度，但 settings 变化伴随进程重启，
  process-local memo 天然清空）。与 notebook_scale.py 拒绝
  kg_mutation_seq 单独作键的先例不冲突：那里的产物还依赖 embedding/
  settings 输入，这里不依赖。已知豁口：facade `add_relations`
  （repository_facade.py:2288）是不 bump 的纯测试路径——照
  knowledge_query.py:518-530（insert_test_object）先例在该方法内显式
  invalidate 本 memo。
- **读序契约（硬）**：冷算必须**先点读 seq、再取数据**（内容永远 ≥ 标签；
  反序会让 carry 把陈旧内容续成新版本）。配变异锚点测试：把实现改成
  「先取数后读 seq」必须有用例变红（用注入的 bump 交错模拟）。
- 值 = top-M items（M=1000 模块常量）+ 标签 seq。`review_queue(nb, limit)`：
  0 ≤ limit ≤ M 时命中返回**切片的浅拷贝（逐 item dict 拷贝）**——返回值
  不与 memo 共享可变对象；miss 则 single-flight 冷算（T-A1 后的路径）写回
  （epoch 校验防失效期写回）。`limit < 0` 或 `limit > M` 直通冷路径不经
  memo（语义=现状）。切片等价性：`nlargest(M)` 前缀 = `nlargest(limit)`
  （堆的稳定并列随前缀转移）。
- `set_edge_review`：**carry 必须以前值为条件（质量评审 P1-2 引出的规格
  修订）**——该方法允许任意迁移，包括 rejected→pending（撤销拒绝），那会把
  边加回集合并改变拓扑/corr/计数。`update_edge_review` 改为返回 prev_status
  （PG 用 UPDATE...FROM(SELECT...FOR UPDATE) old RETURNING old.prev；SQLite
  同事务 SELECT 后 UPDATE）。UPDATE + bump 后读回 new_seq：
  - prev ∈ {pending,verified} **且** new ∈ {pending,verified} →
    `memo.carry(nb, expected_seq=new_seq-1, rel_id, status)`：锁内
    entry.seq==expected 时 **copy-on-write**（新 list + 该 rel 的新 dict）
    更新 `review_status` 并 retag new_seq；**rel 不在 top-M 内时同样
    retag**；seq 不符整条丢弃。同时对 review_queue_total memo 做同款
    retag（值不变、标签 +1）。
  - 迁移任一侧涉及 'rejected'（含 rejected→rejected 幂等写）→ 排名 memo
    与 total memo 一并 invalidate（集合/拓扑可能已变，宁可重算）。
- centrality 的既有失效路径不动。

验收：carry 命中/不在 top-M 仍 retag/seq 不符丢弃/single-flight/LRU
fail-closed/读序变异锚点/「verified 翻转后同进程下一次取队列不冷算」计数器
断言/跨进程模拟（连 bump 两次）必须重算/返回值突变不影响 memo。

### T-A3 review_queue_total（独立，可并行）

- `knowledge_counts_cache.py`（PG+SQLite 镜像）加第 5 个 seq-gated memo
  `review_queue_total`：`COUNT(*) WHERE notebook_id=%s AND
  review_status!='rejected'`。**epoch 保护对齐 pending/visible_pending
  两个 memo 的形态**（不是 type_status_counts 的无 epoch 形）。**不进**
  `warm_all`（大库冷 COUNT ~1.1s，懒算）。seq 闸完备性：knowledge_relations
  的生产写路径均 bump（关系补全/store_kg/job publish 已抽查）；豁口
  facade `add_relations` 与 T-A2 同一处置（显式 invalidate）。
- **total memo 的 carry（质量评审 P1-2）**：审核循环里每次 verified/
  pending 判定都 bump seq，若无处置，每次点击都付一次 ~1.1s 冷 COUNT 且
  结果逐值相同。counts_cache 增加 `carry_review_queue_total(nb,
  expected_seq, new_seq)`（锁内 seq 严格比对后仅改标签），由
  `set_edge_review` 按上面 T-A2 的同一前值条件调用（T-A3 先落此钩子，
  T-A2 复用同一处置点）。docstring 不得声称「下次必命中」——要写明
  审核动作本身就是 KG mutation，命中依赖 carry。
- API 形状：`GET /notebooks/{id}/edge-review-queue` 响应从
  `List[EdgeReviewItem]` 改为 `{"items": [...], "total": n}`。前端标题在
  `total > items.length` 时必须同时给出截断提示（「共 N 条 · 显示前 M
  条」），不得只声称总量（质量评审 P1-1）；完整分页归 R4/后续。**契约面同
  diff**：`scripts/generate_repository_contract_fixtures.py` 重生成
  `backend/tests/fixtures/repository_contract/api_contract.json`；
  `backend/tests/test_edge_review_queue.py:301` 的 list 断言更新；前端
  `edge-review-queue.ts` + `page.tsx`；`docs/product-and-api.md` 与
  `docs/product-and-api_zh.md` **成对**更新（AGENTS.md routing 的双语对）。

## PR-B：查重 + 概念详情

### T-B1 查重两趟取数（KG-3）

- pass 1（窄）：新 store 查询取 `id, status, object_type, payload->>'name'`
  （procedure 类型额外整 payload——`seed_procedure` 要 steps 签名；其余三型
  seed 只读 name）。`status != 'deprecated'` 下推 SQL（现 Python 过滤删除）。
- Python 侧 alias_map + seed 分组照旧（同一函数，候选集合按构造等价）。
- **顺序契约（硬，设计评审 B5b）**：`_knowledge_objects` 现走
  `ORDER BY created_at ASC, id ASC`；by_seed 插入序、组内成员序、groups
  稳定并列序全部继承它。pass 1 必须保持同一 ORDER BY；pass 2 的
  `id=ANY(...)` 回填必须按 pass-1 序重组（不得依赖 ANY 返回序）。
- pass 2：仅对成员数 ≥2 的块取行，**不取 evidence 列、显式填
  `"evidence": []`**（设计评审 B5a：`_knowledge_similarity` 字面上读
  `a["evidence"]`（knowledge_governance.py:1723-1724），但调用点硬编码
  `element_vectors={}` 使 semantic 分支恒为死代码——空列表语义等价；代码
  注释登记该死分支依据。禁止改成「取 evidence」，那会废掉本项窄化）。
- 等价 oracle：旧实现小样本对账（沿用 test_kg_empty_extraction_marker.py 的
  两侧逐用例模式）：分组成员集合、similarity、排序逐字段相等。
- `statuses=None` 语义、`_knowledge_ref` 输出形状不变。

### T-B2 概念详情 hub 成员分页（KG-4 应用侧）

- `concept_cluster_detail_rows` 加 keyset：`ORDER BY cc.member_object_id
  COLLATE "C" LIMIT %s`（+ `member_object_id > %s` 游标）；新增
  `concept_cluster_member_total`（COUNT）。默认页 200。
- **member_total 同口径（硬，设计评审 B8）**：COUNT 必须复用分页查询的
  谓词形（`JOIN knowledge_objects ... AND ko.status!='deprecated'`）——裸
  `COUNT(*) FROM concept_clusters` 会把 deprecated 成员算进去，翻页永远
  「还有更多」。
- 现查询无 ORDER BY；加 `ORDER BY member_object_id COLLATE "C"` 后
  members/evidence 返回序改变——有意变更，写进 PR 说明（既有
  test_unified_kg_repository.py:145-157 用集合断言不看序）。
- API `GET /notebooks/{id}/concepts/{canonical_id}/detail` 加
  `limit`/`after` query 参数；响应加 `member_total`、`next_cursor`。
  `attached`/`evidence` 改为**按页内成员**计算（分页语义：每页展示该页
  成员的邻接与证据；用户翻页可见全部——「分页而非截断」质量口径；前端
  仅详情面板消费（page.tsx:2407-2426 / 8115-8118），不参与图渲染）。
  产品文档（en/zh 成对）记录该展示语义变化。
- **契约面同 diff**：`ports.py:614` concept_detail 签名变更 →
  `facade_surface.json` rebaseline（tests/architecture/facade_contract.py
  流程）；`api_contract.json` 重生成（与 T-A3 同一脚本）。
- 前端 `use-kg-graph.ts` fetchConceptDetail 加游标透传 + 详情面板
  「加载更多成员」；无参调用行为 = 第一页（向后兼容）。**三个调用点
  （use-kg-graph.ts:458/640/693）中后两个是 merge/rebuild 后刷新——必须
  同步重置「加载更多」累积态，否则游标与已加载列表错位**（设计评审 B8）。
- 不带参数的既有调用方（若有服务内消费者）逐个核对：需要全量的改显式
  分页循环或传 `limit=None` 直通旧全量（保留全量分支给内部消费者）。

## 明确不做（本批范围外）

- corroboration / seed 归一化的 SQL 表达式化（判断 1）。
- centrality 换预计算产物表（v36 三张是社区级，不含 per-edge betweenness；
  version-cache + T-A2 排名 memo 已把它摘出稳态请求路径，剩余成本在
  reject/KG 写后的一次重算——真实精度要求下不可避，登记为后续观察项；
  其加载路径连 evidence 一起取的既有成本（sqlite/knowledge_store.py:2810）
  同样登记不动）。
- 大库准入闸（/graph 413 同款）——若评审认为必须，可在 T-A2 上加
  candidate 数上限保护，默认不加。
- R4（轮询面）、R1、W-CLI——按计划另行。

## 门与流程

每任务：实现（T-A1/T-A2 用 opus 实现子代理，T-A3/T-B1/T-B2 用 impl-task/
sonnet）→ spec-review + code-quality-review（opus）→ 下一任务。每 PR：
check.sh + PG lane 全绿 → codex 评审闭环 → CI 绿 → 合入。
