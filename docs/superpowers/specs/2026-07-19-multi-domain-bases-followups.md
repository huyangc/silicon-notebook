# 多领域基准库 —— 合入后待办（2026-07-19）

PR #304 合入时**明知未做**的事项，集中记在这里，之后单独起任务收。
每条都给了：为什么当时没做、影响面、以及动手时的落点。

关联：[规格](2026-07-18-multi-domain-base-libraries-design.md) · [计划](../plans/2026-07-18-multi-domain-base-libraries.md)

## 对账（2026-09-07，按当前代码）

| 项 | 状态 | 依据 |
| --- | --- | --- |
| A1 | 半做 | 已知构造点（`evidence_context.py` chunk/knowledge/citations_from、`ask_service.py` 各处、`kg/follow_chain.py`）均已归一化并由 `test_evidence_context_service.py` 钉住；**静态守卫未建**，`repository_facade._citation()` 死代码未删 |
| A2 | 未做 | `evidence_context.py` 的 graph-BFS 节点注释仍写「暂未填」 |
| A3 | 未做 | `page.tsx` 晋升队列对 `target_base_id` 为空的候选仍可点批准/拒绝，无回填脚本提示 |
| A4 | 未做 | 知识晋升读 `currentNotebookBases`，Memory 晋升读 `base_notebooks` |
| A5 | 未做 | `page.tsx` 与 `memory-panel.tsx` 各有一份「选择贡献目标」弹窗 |
| A6 | 未做 | `sharing_store.py` 的 `_COPY_SNAPSHOT_QUERIES` 无 `notebook_bases`，也未列入「Deliberately absent」注释 |
| A7 | 未做 | `migrations.py` `migrate()` 仍只有 `current >= SCHEMA_VERSION: return []`，无「库版本高于代码」报错 |
| A8 | 未做 | `MOUNT_VALID_EXPR` 无 `test_access_sql_contract.py` 那种形状扫描守卫 |
| A9 | 部分 | 「基准库」用户可见残留已由 `docs/ui-vocabulary.md` + 守卫清掉；README promote 端点 `target_base_id`/400 未补；陈旧注释因行号漂移未核 |
| B 全部 | 已修 | `test_ui_vocabulary_guard`/`test_user_error` 绿；`smoke_memory_mcp.py` 白名单改为从 `PUBLIC_TOOLS` 派生；schema 版本守卫改为按 `SCHEMA_VERSION` 动态断言 |

未做项已登记进根目录 `fangan_todo.md`「多领域基准库合入后遗留」；本文其余部分保留为当时的问题描述与落点。

---

## A. 本特性自身的遗留

### A1（建议优先）加一条守卫：构造 Citation/AnswerAnchor 必须归一化 notebook_id

**不变量**：`Citation.notebook_id` / `AnswerAnchor.notebook_id` **仅在跨笔记本证据上非空**。
等于 active notebook 的值必须归一成空串——否则前端库名映射（含 active 笔记本）会把本库引用
显示成「来自「当前笔记本自己的名字」」。

**为什么优先**：这条不变量在本特性开发期**漏了三次**，每次都是靠人工评审抓到的：

| 轮次 | 漏的位置 |
| --- | --- |
| Task 14 实现期 | `kg/follow_chain.py`（`ChainHop.notebook_id` 早已填好却没写进 id_map） |
| codex 第 2 轮 | `evidence_context.py` 的 `chunk_context` 与 `knowledge_context` 两条路径 |
| codex 第 4 轮 | `evidence_context.py` 的 `citations_from()`，外加 `ask_service.py` 的 4 处构造点 |

第 4 轮的修复把全部产出路径列了一张清单（见下），**但清单是一次性的**：新增一个构造点没有任何
机制会提醒。这是它反复出现的根本原因。

**当前全部产出路径**（第 4 轮穷举，动手时可作为守卫的基线）：

- Citation ×7：`evidence_context.py:355`、`ask_service.py:882/891/1278/1453`（以上均已归一化）、
  `ask_service.py:382`（memory，结构性安全）、`sqlite_repository.py:3467`（模块级 `_citation()`，
  **零调用点的死代码**，可顺手清掉）
- AnswerAnchor ×1：`evidence_context.py:273` `parse_anchors`，由 5 个 id_map builder 供给
  （`chunk_context` / `knowledge_context` / `render_follow_chain_context` 已归一化；
  `render_subgraph_context` 见 A2；`MemoryRetriever.context` 结构性安全）

**落点**：静态检查（AST 扫 `Citation(`/`AnswerAnchor(` 的构造点，要求 notebook_id 参数来自
归一化过的变量），或运行期断言。放进 `backend/tests/` 的架构守卫一族。

### A2 graph BFS 路径的锚点不带来源库 id

`backend/app/services/evidence_context.py:284-287` 附近。graph 模式走 rustworkx BFS 时，
`render_subgraph_context()` 不往 `evidence_by_id` 里写节点所属的 notebook id，于是这条路径上的
引用只显示泛化的 tier 徽章，看不到具体是哪个参考库。

**为什么当时没做**：需要把 notebook_id 穿进联邦图的**节点 payload 契约**（`build_rx_graph`），
比其它几处伤筋动骨。整支审查与 codex 第 5 轮都判为 P2 非阻塞。

**影响**：仅限「graph 模式 + 无源 chunk」这一条路径的徽章显示；退回的是既有的泛化文案
（「来自公共知识库」），不是错误信息。

### A3 存量待批晋升候选在队列里可点但必失败

`target_base_id=''` 的存量候选在晋升队列里不显示目标行，却仍可点「批准」，点了必 400，
且界面没有任何线索指向补救 CLI（`scripts/backfill_promotion_targets.py`）。

**落点**：`frontend/app/page.tsx` 晋升队列渲染处——目标为空时置灰 + 标注「需先用补救命令指定目标库」。

### A4 挂载数据源在两处不统一

知识条目晋升读 `currentNotebookBases`（只有进 Rules tab 才拉），Memory 晋升读
`NotebookSummary.base_notebooks`。前者在「拉取中/失败」时按钮会禁用并显示假提示「需先挂载」。

**落点**：统一到 `base_notebooks`，可顺带删掉 `listBases` 调用与其门控。

### A5 晋升目标选择器的弹窗 JSX 重复

`page.tsx` 与 `memory-panel.tsx` 各有一份（判定规则只有一份 `resolvePromotionTarget`，是对的；
但**呈现策略有两份**，且 `none` 分支一个走 toast、一个走 error）。

### A6 深拷贝静默丢挂载边

`backend/app/repositories/sqlite/sharing_store.py` 的深拷贝不携带 `notebook_bases` 行，
且不在该文件明确登记的「刻意排除项」里，也没有测试钉住这个行为。
**落点**：定性（是刻意还是遗漏）+ 加钉子测试。

### A7 回滚 20→19 不安全且无文档

`backend/app/repositories/sqlite/migrations.py` 的 `migrate()` 没有「库比代码新」的守卫。
v19 代码跑 v20 库不会被拦，而 v19 的 `set_tier` 一次「发布」就会不可逆地降级其它所有公共知识库。
**落点**：加版本上界守卫（`user_version > SCHEMA_VERSION` 时明确报错退出）+ README 写明单向性。

### A8 「挂载有效性谓词只有一个定义点」无守卫

`backend/app/repositories/sqlite/mount_sql.py` 是唯一定义点，开发期**曾因手写第二份副本被判规格不符**。
现在靠人工 grep 维持。加个 grep 测试很便宜。

### A9 零散清理

- 用户可见的「基准库」残留三处：`frontend/app/page.tsx` 的分析弹窗与晋升队列一带
  （属 UI 词汇整改轨道，不是本特性引入）
- 陈旧注释：`graph_retrieval.py:117-124/221-222/602-604`、`sqlite_repository.py:2948`、
  `ask_service.py:1115/1161`、`knowledge_governance.py:1221`（点名了已不存在的 ValueError 文案）
- `mountable_notebooks` 与全仓库通行的其它查询一致性：`status != 'copying'` 已加，
  但可复查还有没有别的通行过滤没跟上
- README 的 API 清单未提两个 promote 端点新增的 `target_base_id` 与新增的 400 态
- `fangan_done.md:353/217`（CONTRACT_DOC）与本 PR 已改对的 README/AGENTS 句子矛盾

---

## B. master 既有缺陷（**不是本特性引入**，但合入后 CI 会红）

已用 `git archive origin/master` 干净导出独立取证：以下在**不含本特性任何代码**的 master 上同样失败。
建议单独开 PR 收，别混进特性分支。

| 项 | 现象 | 备注 |
| --- | --- | --- |
| `backend/tests/test_ui_vocabulary_guard.py::test_真实前后端源码都通过守卫` | 9 条命中 | 命中的是 PR #300 传输特性的用户可见文案，**改写属产品文案决策** |
| `backend/tests/test_user_error.py::test_no_bare_chinese_4xx_http_exception` | 断言失败 | 同上批次引入 |
| `scripts/smoke_memory_mcp.py:133` | 工具白名单断言失败 | 白名单自 PR #252 后未更新，PR #272 加的 4 个 knowhow 工具未登记；`scripts/check.sh` 在 `set -e` 下会卡在这里 |
| `README.md` / `README_zh.md` / `AGENTS.md` 的「schema version is 15」 | 实际已是 20 | ⚠️ `test_architecture_documentation.py:587` 把这句**逐字硬编码**，导致这个名字里带 `not_stale` 的守卫在 16→20 五次 bump 中**全部放行** —— 守卫本身需要重新设计 |

---

## C. 合入时的口径提醒

- `scripts/backfill_promotion_targets.py` 的存量候选清点**必须在上线前跑**，否则待批候选审批会硬失败
- spec §6 关于 scale eligible 的说法在实现期被修正过：那个「挂大笔记本静默失效」的窗口**不存在**，
  保留该改动的理由是语义一致性。对外描述不要用「补洞」的说法
