# 冻结来源范围下恢复 chunk 向量通道

## 缺陷

`backend/app/services/retrieval_candidates.py::_retrieve_chunks_baseline` 在 chunk ANN
（scale 索引）不可用时，曾在「大库暴力守卫」**之前**有一条早返回：

```python
if allowed_source_ids is not None:
    # A selected scope is always a bounded candidate query. Do not materialize
    # every chunk/vector merely because the enclosing notebook is below the
    # broad copy/share threshold. ...
    return self._retrieve_chunks_fts_degraded(
        notebook_id, query, query_vector, recall, -1,
        allowed_source_ids=allowed_source_ids,
        source_restricted=source_restricted,
    )
```

HTTP 侧冻结的来源范围**恒**给出允许清单——全选冻结也带清单（见
`app/services/source_scope.py::scoped_allowed_source_ids` 的 docstring，codex #640 R1
裁决）。因此这条早返回的判据 `allowed_source_ids is not None` 对**界面发出的每一次
问答**都成立：只要该库没建 scale 索引，chunk 通道就只剩 FTS 词法候选，**完全不做
向量检索**。

## 复现数据（离线对照集）

同一个库、同一道题：

| 查询 | 无范围 | 冻结单来源范围 |
| --- | --- | --- |
| 中文「什么是递归深度语言模型」（英文语料） | 134 命中 | **0 命中** |
| 英文同义查询（词法可命中） | 134 | 119（FTS） |

事件指纹：`chunk_bruteforce_skipped reason=large_library_no_ann n_chunks=-1`，在一个
`copyable=true` 的小库上出现。

单元级复现（`backend/tests/test_chunk_retrieval_characterization.py` 的 fixture，
FakeEmbedder + 两段英文正文 + 中文问句）：无范围 1 命中 / 冻结范围 0 命中，事件
`{'reason': 'large_library_no_ann', 'n_chunks': -1, 'threshold': 20000, 'fts_hits': 0,
'embed_ok': True}`。

## 根因

两处各自正确的改动叠成了缺陷：

1. **#422（2026-08-01）** 引入这条早返回，理由是「选定范围本来就是有界查询，别为
   小库全表物化 chunk/向量」。在 #422 的世界里，`allowed_source_ids` 非 `None` 被当
   成「用户真的收窄了」的同义词。
2. **#640 R1** 裁定：冻结必须绑定候选生成，**全选冻结也要带允许清单**（否则并发新增
   来源会扩大已在飞的请求，且不带清单的 producer 会读到每一位成员的隐藏 Memory）。

于是 (2) 让 (1) 的判据对所有请求恒真。#640 自己在**路由**维度上已经把这条纪律写清楚
了——`_lexical_gate_source_scoped` 明确禁止用 `allowed_source_ids is not None` 推断
「这次运行被收窄了」——但 #422 那条早返回是**通道选择**维度的同一个错误，没有被同一
次修正覆盖。

## 修法（通用规则：scoped 与 unscoped 用同一把守卫）

1. 删掉那条早返回。scoped 请求继续往下走到大库暴力守卫，与 unscoped 共用同一判据：
   只有 `large`（`not notebook_copy_stats(nb)["copyable"]`）或整库
   `n_chunks > chunk_bruteforce_max_chunks` 才走 `_retrieve_chunks_fts_degraded`。
2. 小库的 scoped 请求因此落到守卫下方的有界暴力向量路径。这条路径的向量一半改成
   **缓存整库矩阵 + 打分前掩码**：
   - chunk 文本行仍由 `_gather_chunks(db, nb, allowed_source_ids=...)` 按允许来源取；
   - 向量取 `self._vector_matrix(db, nb, "chunk_embeddings", "chunk_id")`——与 unscoped
     **同一份**版本键控进程缓存；
   - 随后 `_mask_vector_matrix(ids, mat, 允许 chunk id 集合)` 用 numpy 花式索引取行
     子集，返回 `(sub_ids, sub_mat)`，再进 `score_chunks` / MMR。

   为什么不是「每次按允许来源现拉向量行」（`vector_rows_for_ids` + `build_matrix`）：
   那条路**零缓存复用**，每个子查询各建一次矩阵；一次问答被 `_retrieve_chunks_multi`
   放大到最多 8 个并发子查询，各自解码同一批向量行（legacy JSON 行 5000×1024 实测
   ~264ms/次）并各持一份。走缓存则整库只解码一次，由这 N 个子查询共享。

   为什么不能把整库 `(ids, mat)` 原样返回、只靠 `chunks` 收窄（掩码是**必需**的）：
   - `ids/mat` 是返回值的一部分，上层 MMR 拿它当**多样性矩阵**——留着不允许的行就等于
     让被排除的内容以隐藏前提的形式参与选择；
   - 全选冻结 ≠ 生产者读到的宇宙：冻结清单的隐藏半按**请求人**解析，不带清单的生产者
     读到的是**每一位成员**的隐藏 Memory（见
     `app/services/source_scope.py::scoped_allowed_source_ids` 的 docstring，codex #640
     R1），所以「全选时清单恒真、可以省掉」这条捷径不成立；
   - 过滤必须发生在 `score_chunks` **之前**：被排除的候选不得占用 Top-K
     （`docs/product-and-api.md` 的原话）。

   掩码同时满足这三条：候选与矩阵行都严格 ⊆ 允许 chunk，且过滤发生在打分之前。
   `runtime_dim`（MRL 截断维）随 `_vector_matrix` 的版本键走，不再由这条路径自己重解。
   判据只留 `large` 与 chunk 计数两条，两条都是**库**的属性，与本次请求勾了几个来源
   无关。这个论证写进了代码注释。
3. 大库守卫触发时，把 `allowed_source_ids` 与 `source_restricted` 一并下推给
   `_retrieve_chunks_fts_degraded`（过滤不放松）。unscoped 一支这两个值恒为
   `(None, False)`，与两个默认值逐字相同，行为不变。
4. `n_chunks` 计数：改走 seq-gated memo `notebook_chunk_count`
   → `IndexProjectionStore.total_chunk_count` → 方言各自的
   `knowledge_counts_cache.chunk_count`（与 `/scale-index/status` 开库路径同一份 memo，
   warm 是一次主键 seq 读）。裸 `chunks.count_row` 在这里是每子查询一次整库 COUNT，会
   被 `_retrieve_chunks_multi` 放大到最多 8 次。`large` 分支的计数只作事件诊断，同样走
   memo。不再传 `-1` 字面量——事件里的 `-1` 会让排查者以为计数探针坏了。
   `_retrieve_chunks_fts_degraded` 的 docstring 写明 `n_chunks` 是**整库**计数（经
   memo 取真值），不是允许来源内的计数，也不是候选数。
4b. `_gather_chunks` 的 scoped 分支按 `_in_batches`（`_IN_CHUNK` = 900）分批调用
   `hydrate_rows`。分批只需要发生在**第二跳**：来源 id 那一跳两个方言都把清单整体绑成
   **一个**参数（SQLite `json_each(?)`、PostgreSQL `source_id=ANY(%s)`），占位符数与
   来源数无关；而它返回的 **chunk** id 是 `hydrate_rows` 的 `IN (?,?,…)` 直接展开，
   行数 = 允许来源里的 chunk 总数，一个来源就能有上千条。有界性的来源是「允许来源 →
   chunk 子集」，不再依赖 `notebook_copy_max_rows`。
5. `source_restricted` 的**路由**语义（词法语料语言闸）保持原样：由
   `_lexical_gate_source_scoped` 现探，不由 `allowed_source_ids is not None` 推断。
   这条纪律不动。
6. 不变的三件事：大库（not copyable）的 scoped 请求仍 FTS 降级；有 ANN 索引的路径
   不变；unscoped 路径行为不变。

## 已接受的成本

- **全选冻结 = unscoped 的代价 + 一次子集拷贝**。掩码用 numpy 花式索引，返回的是拷贝
  （共享的缓存矩阵绝不能被就地改写或交出引用）；全选时这份拷贝与整库矩阵同样大。换来
  的是同一次问答的 N 个子查询共享一次解码，净额远优于「每子查询各建一份」。
- **COUNT 走 memo**，因此在 seq 未变时读到的是缓存值而不是现算值。这与
  `/scale-index/status` 开库路径共用同一份 memo 与同一套失效钩子；chunk 的唯一写入者
  （`build_chunks_for_source`）与 `delete_source` 都会 bump `kg_mutation_seq`，见
  `knowledge_counts_cache` 的模块 docstring。

## 验收

- 对照集在**冻结范围**下，中文问句对英文语料的零命中归零（回到与无范围一致的命中集）。
- 小库的 scoped 请求不再发 `chunk_bruteforce_skipped`；该事件只在大库/超阈值库出现，
  且 `n_chunks` 是真实整库计数。
- 全选冻结与无范围的**候选集合一致**——限定「库内没有他人隐藏来源」。这条限定是必要的：
  冻结清单的隐藏半按请求人解析，不带清单的生产者读到的是每一位成员的隐藏 Memory，
  共享库里两个宇宙相差恰好那份投影，此时全选冻结**应当**更窄，那是冻结在做它该做的事。
- 冻结范围只含部分来源时，命中只来自允许来源，**且返回的 `(ids, mat)` 的行也严格 ⊆
  允许来源的 chunk**（掩码发生在打分之前；只断言 `scored` 看不到矩阵行泄漏）。

**实测（2026-09-07，修后代码对本机 PG 部署的同一库复跑对照集，冻结单来源 include 范围）**：
25 道中文题（含带整段契约文本的原查询）零命中 **0/25**；24 道题的金标小节进前 8：中文 22/24、英文 22/24；
逐题命中数与无范围路径**完全相同**（q01 133、q03 134、q17 116……），即冻结本身不再改变候选集。
对照集脚本与题库保存在会话记忆目录 `probe-floor-2026-09-07/`（`questions.py` 24 题 + 金标；`probe.py` 地板测量；
冻结范围复跑见本 PR 描述），后续归一第三步可直接复用为对照集种子。

## 测试

`backend/tests/test_chunk_retrieval_characterization.py` 第 13 节：

| 用例 | 钉住的行为 |
| --- | --- |
| `test_scoped_small_library_keeps_the_chunk_vector_lane` | 小库 + 冻结 include 范围 + 关键词零重叠的跨语言查询 → 必须有向量命中（变异锚点：加回早返回则报红） |
| `test_scoped_small_library_emits_no_bruteforce_skipped_event` | 小库 scoped 请求不发大库降级事件 |
| `test_scoped_all_selected_freeze_matches_unscoped_candidates` | 全选冻结与无范围候选集合一致（限定库内无他人隐藏来源） |
| `test_scoped_partial_freeze_still_ceils_hits_to_allowed_sources` | 部分冻结的命中**与返回矩阵行**都只来自允许来源（变异锚点：去掉掩码 → 红） |
| `test_scoped_brute_lane_reuses_the_cached_whole_notebook_matrix` | 同一 run 的两个子查询只加载一次整库矩阵、且不再 `vector_rows_for_ids`（变异锚点：改回现建 → 红） |
| `test_scoped_large_library_still_degrades_to_fts_with_a_real_count` | 大库 scoped 仍 FTS 降级，清单与 `source_restricted` 原样下推，`n_chunks` 不是 -1 |

`backend/tests/test_retrieval.py::test_large_selected_chunk_search_uses_bounded_fts` 种了 3 条
chunk（其中一条不在允许清单里），因此 `n_chunks == 3` 同时排除「取到允许来源内的计数（2）」
与「计数探针根本没跑（空库 0 与占位 0 无法区分）」两种失败。

`backend/tests/test_chunk_retrieval.py`：
`test_report_unscoped_baseline_fallback_keeps_actor_source_ceiling` 显式钉住
`copyable=False`（FTS 降级是**大库**的 lane），新增姊妹用例
`test_report_unscoped_baseline_fallback_ceils_the_bruteforce_lane` 钉住报告 run 的 actor
天花板同样绑定小库的暴力 lane。

## 文档

`docs/product-and-api_zh.md` / `docs/product-and-api.md`「按来源选择检索范围 /
Source-selected retrieval scope」新增一段：冻结清单是**过滤**不是**通道选择**；无 ANN
索引时小库的 scoped 请求走按范围有界的向量路径（缓存整库矩阵 + 打分前掩码，候选与矩阵行
都严格 ⊆ 允许 chunk，已接受的代价是一次子集拷贝），只有大库才 FTS 降级；
`chunk_bruteforce_skipped` 只在大库出现，其 `n_chunks` 恒为经 seq-gated memo 取到的整库
真实计数。

`docs/operations_zh.md` / `docs/operations.md` 的「ANN 不可用时报告仍用有界 FTS 回退」补上
条件：只有大库/超阈值库才 FTS 降级，小库的报告 run 走有界向量路径，因此
`chunk_bruteforce_skipped` 出现在小库上是需要排查的信号而非预期回退。
