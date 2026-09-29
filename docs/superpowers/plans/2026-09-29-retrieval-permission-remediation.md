# 检索层权限整改计划（PR-E 系列，2026-09-29）

来源：`2026-09-29-retrieval-permission-audit.md`（七份审计台账）+ 八份只读核实（opus，master @ 9bacb8d6，
逐条对代码复核）。与 `2026-09-29-scope-ceiling-remediation.md`（PR-A～PR-D）并行交付；本计划只规划
那份计划没有覆盖的部分，已在执行的发现逐条标出分支。

路径约定：`$R` = `/Users/huzhifeng/workspace/silicon-notebook`（实现方换算成自己的 worktree 根）。
行号以 master @ 9bacb8d6 为准，PostgreSQL 在前、SQLite 在括号里；实现前按函数名重新定位。

## 0. 约束与状态

**交付规则**（用户 2026-09-29，沿用既有计划）：计划内范围本轮全部交付；拆多个 PR 连续交付，不砍范围、
不登记 `fangan_todo.md`、不写「后续 PR」。子代理发现范围内缺口，当场报告并补进同一 PR。

**架构裁决**：权限与来源范围只在检索/读取层审核；合成与终态核对假定到手的信息均可读，不设按权限
隐藏的分支。本计划所有修复都落在读取层（store SQL、检索腿、读取端点、建图输入），不碰合成与终态核对。

**已裁决、不再讨论**：Q2、Q4、M1–M4，以及主 agent 定的七条（入口永远冻结默认天花板；挂载库在单库运行
只开放可见来源；硬删除 Memory / 成员退出 / 账号删除清掉全部派生行；按回答 id 的接口校验会话属主；
收窄时本人隐藏来源不参与、只改文档；部署管理员看回答全文是审计权限，写进文档；E-2～E-5 并入 PR-B）。

**在途分支（文件被占用，本计划的任务不得在它们合入前改这些文件）**

| 分支 / worktree | 计划 | 占用文件（与本计划相关的） |
| --- | --- | --- |
| `claude/scope-ceiling-remediation`（scope-fix） | PR-A（A5 已提交 fd0303f1，store 修复轮在途） | 两个 `knowledge_store.py`、`knowledge_lifecycle.py`、`knowledge_query.py`、`repository_runtime.py`、`evidence_context.py`、`kg_routes.py`、`domain/knowledge_contracts.py`、`docs/product-and-api*.md`、`docs/development*.md`、KG 面板前端 |
| `claude/enumeration-source-ceiling`（enum-ceiling） | PR-B | `ports.py`、两个 `knowledge_store.py`、`collection_catalog.py`、`collection_enumeration.py`、`prompts.py`、`reasoning_retrieval.py`、`scripts/architecture_boundary_baseline.json`、`test_source_scope.py` |
| `claude/active-reserve-seats`（reserve-seats） | PR-C | `ports.py`、`ask_service.py`、`chunk_federation.py`、`reasoning_retrieval.py`、`report_engine.py`、`retrieval.py`、`retrieval_service.py`、`outline_synthesis.py`、`spreadsheet_analysis.py`、baseline |
| `claude/citation-partial-check`（citation-check） | PR-D 消费方 | `global_ask.py`、`global_ask_routes.py`、`mcp_tools/global_ask.py`、`models/ask.py`、`models/global_ask.py`、`global_ask_ports.py`、`sqlite/source_store.py`、`conversation_public_view.py`、`chunk_federation.py`、`repository_runtime.py`、`frontend/app/c/[token]/page.tsx`、`public-conversation.ts`、`answer-panel.tsx`、`citation-card.tsx`、`workspace-model.ts`、`globals.css` |

**迁移号**：origin/master 最新 PG `0065_sync_export_snapshot.sql`、SQLite `SCHEMA_VERSION = 85`；
`claude/system-update-notification-4b18f6` 已占 `0066_user_seen_release_ordinal.sql` / v86。本计划从
**PG 0067 / SQLite v87** 起号（PR-E4 用 0067/v87，PR-E8 用 0068/v88）。**每个 PR 开出前一刻**必须重新执行
`git -C $R fetch && git -C $R ls-tree --name-only origin/master backend/app/repositories/postgres/migrations/ | tail -3`
与 `git -C $R show origin/master:backend/app/repositories/sqlite/migrations.py | grep -n "SCHEMA_VERSION ="`
核对；被占则顺延并同步 `schema_manifest.py`、迁移测试与本段。PG 同号文件并存不会产生 git 冲突，会静默重号，
所以这一步不可省。

**棘轮**（`scripts/architecture_boundary_baseline.json`，零余量双向：变长违规，变短也必须同 diff 下调）：
`ports_protocol_method_count` 966 只减不增（给既有协议方法加关键字参数允许，新增方法不允许）；
本计划可能触及的有上限函数及当前长度（全部等于上限）：`memory_context.py::register_memory_context_tools.ask_notebook`
142、`AskService.ask_chunk` 332、`_run_reasoning_stage` 515、`_draft_reasoning_response` 698、
`ReasoningRetriever.run` 1294、`ReportEngine._draft_section` 266（reserve-seats 分支改为 310）、
`NotebookCopyService.copy_notebook` 640、`MemoryService.transfer` 389、`rebuild_unified_kg` 482、
`rebuild_communities` 413、`complete_relations_for_source` 425、`_process_source_scoped` 352、
`RepositoryRuntime.__init__` 145。各任务写明如何在其内设计。

## 1. 台账勘误

| 条目 | 台账原话 | 核实结论与证据 | 处理 |
| --- | --- | --- | --- |
| R1 行号 | 插件引擎默认范围在 `ask_service.py:2130-2211`；`source_scope.py:878` 是 exclude 返回 None | 插件合成实际在 `ask_service.py:2166-2213`（2211 行进入 context）；878 是「无 scope → 原样返回 allowed」，legacy exclude 无显式清单返回 None 在 921-923 | 行号修正，结论不变 |
| A-1 细化 | 无范围时元素臂读到所有成员 Memory 元素 | 只在小库（copyable）路径成立：`retrieval_candidates.py:2664-2673` → `postgres/source_store.py:1043-1066` 无 source_type/owner 谓词；大库元素走 chunk 召回（2624-2663），Memory 源无 chunk，不漏。KG 两臂无论大小都漏 | 修复不变（E1-2） |
| A-2 | 旧 `exclude` 持久化报告范围返回 None | 生产不可达：每个阶段都经 `_validate_source_scope` 重新冻结为 include——confirm `report_routes.py:274`、generate `:524`、auto-confirm `:138`；`report_execution.py:178-179 / 237-238` 的 raw 回退只在路由传 None（持久化值本身为 None）时触发 | 不修；E1-3 加一条回归钉住「三个阶段都重冻结为 include」 |
| A-3 | 收窄时本人隐藏来源不参与，与文档措辞不一致 | 文档与此行为**一致**（`product-and-api.md:558` / `_zh.md:458`）。真正不符的是同段「Memory 投影只进创建者本人的上限……绝不暴露」（对无范围运行为假）与 `:560` / `_zh:460`「省略字段保持历史全范围行为」（正是 A-1 根源） | 改写这两句（E1-Z） |
| A-4 位置 | `chunk_store.py:380` | 380 是读侧 `retrieval_rows`；写入函数是 `replace_source_chunks`（`chunk_store.py:209`）与 `insert_rows`（`:323`），外加 `source_chunking.build_chunks_for_source`（246-285，可被 `maintenance.py:1327` 与 facade 2251/2259 按 source_id 直调） | 修复落在真实写入函数（E4-1） |
| B-2 范围 | 挂载库 Knowhow 行经 PPR 进答案（疑似） | 成立且更宽：`graph_retrieval.py:472/480` 读全部参与库全部 chunk（含 Knowhow 行与冻结后新上传的来源），`:1152` 水合无谓词，`:1145` 的 `ranked[:top_chunks]` 截断先于 `retrieval_service.py:98-101` 的过滤 | E2-3 |
| B-4 | 挂载库弱支撑关系提示无天花板 | 对挂载库**不成立**：`weak_support_relations` 只以当前库 id 调用（`reasoning_retrieval.py:6754`），SQL `WHERE notebook_id=<active>`。在**当前库内**成立：闸只看 `source_scope_restricted()`（`retrieval_service.py:519-525`），全选冻结或无范围运行会把他人 Memory 派生对象名写进 reflect 提示词 | 按当前库修（E2-1） |
| B-8 | 73fb2747 未修 | 属实（WT 查询只加了 `ORDER BY ... LIMIT`，无状态谓词）；PR-A store 修复轮已接手 | 已在执行 |
| C-4 社区摘要 | 共享派生产物带 Memory | 摘要这一项代码上成立但生产不触发：`summarize_communities`（`knowledge_lifecycle.py:7036`）唯一调用方是 facade（`repository_facade.py:3029`），无路由/作业/脚本 | 仍修（facade 可调），E4-2 |
| C-9 账号删除 | 账号删除同样残留 | 仓库**不存在**账号删除：无 `DELETE FROM users`、无删除用户路由；管理端只改角色/密码/上传上限 | 该子项不适用；硬删与退出照修（E5-2） |
| D-1 严重度 | P1 | 回答 id 为 `ans-` + 128 位 uuid（`repository_facade.py:200`），会话列表按属主过滤，公开页不投影 answer_id；需泄漏 id 才可利用，实际 P2。`test_memory_api.py:158` 用的是无会话的回答（creatorless），不是他人回答 | 照修（E7-4） |
| D-4 | 公开分享含作者 Memory 引用，未提示 | 会话分享（笔记本内与全局，共用 `conversation-share-modal.tsx`）**已在分享前显示条数**，但只在前端数 `citations[].memory_id`；报告分享是一键切换、无任何提示 | 补三个缺口，不另起机制（E7-2/E7-5/E7-6/E7-3） |
| D-5 插件回落 | 回落到当前库 | 纵深缺口而非直接泄漏：命中先受 `allowed_source_keys` 约束（只含本人隐藏来源，`plugin_ask_engine.py:545-548`） | 仍改为失败即丢（E2-2） |
| E-3 | 枚举/目录/概览读活的来源清单 | 花名册、来源计数、文档概览、`read_document` 已经认冻结天花板：`_visible_signal_rows` 按 `scope.allows` 过滤（`collection_catalog.py:529-532`）。只有要素计划、KG 计数与指纹未认——即 PR-B 的 B1/B2 | 已在执行（补测试见「并入 PR-B」） |
| 测试清单 | `test_reasoning_document_read.py:738`；`test_global_ask_engine_parity.py` 两条以 `out_of_ceiling` 作废 | 前者实为 **740**；后者文件里没有 `out_of_ceiling` 字面量，相关用例在 923（`_VOID_OUT_OF_CEILING` 分支）、984、1034，属 PR-D 的「整份作废 → 部分失败」改写 | 归 PR-B / PR-D |
| 测试清单 | `test_knowledge_context_source_ceiling.py:221`、`test_peer_ceiling_subgraph.py:301` 需改写 | 二者钉的是**原语**在无 scope 时不动；修复落在安装点（入口永远装 scope），原语语义不变，这两条仍成立 | 保留，docstring 改为「生产不可达，由 `test_default_ceiling_guard.py` 保证」（E1-2） |

**核实中新发现（台账未列，全部纳入本轮）**

| 编号 | 发现 | 证据 | 归属 |
| --- | --- | --- | --- |
| N-1 | 手工 `merge_knowledge` 把 Memory 对象证据并入共享对象；之后删除该 Memory 时，`source_index_backfilled` 分支按证据引用删对象，**整个共享对象被删**（数据丢失），且合并后的共享对象携带 Memory 引文 | `knowledge_governance.py:2177` → `merge_objects_in_transaction`（PG `governance_store.py` ~1769-1810）；`_stale_object_ids_for_source_batch`（PG `knowledge_store.py:2922-2951`） | E4-3 拒绝跨类合并 + E4-5 迁移剥离存量 |
| N-2 | 冲突裁决 "modify" 把模型的 `resolved_payload` 合进目标对象 payload，共享对象可被 Memory 内容改写 | `knowledge_governance.py` ~776-795 | E4-3 |
| N-3 | `list_knowledge` 的 `Evidence` 带 `source_id`，泄漏 Memory 来源 id，可直喂 `/sources/{id}/elements` | `knowledge_query.list_knowledge`（:238） | E4-4 + E3-1 |
| N-4 | `follow_relation_evidence_rows` 按 id join `sources` 无库谓词 | PG `knowledge_store.py:1692-1702` | E2-4 |
| N-5 | 既有拷贝产生的 Memory 来源 `memory_id` 被清空、`source_type` 仍是 memory，任何生命周期路径都找不到它们 | `notebook_sharing.py:388` | E5-3 孤儿清扫 |
| N-6 | 笔记本摘要 `base_notebooks` / `base_kg_notebook_ids` 向成员暴露主人私有挂载库的名字与图谱标志 | `query_store.mounted_bases_row`（PG :441 / SQLite :559）→ `notebook_catalog.py:237-260` | E6-2 |
| N-7 | 有写权限的非作者成员可一键公开他人报告，而报告引用的是作者本人的 Memory（`notebook_memory_hits(self.user_id)`） | `report_routes.py:595-642`；`report_engine.py:2421-2443` | E7-2 |

## 2. 共享定义（每个概念一处定义）

### D1 默认天花板构造器

- **位置**：`$R/backend/app/services/source_scope.py`，新增 `default_ceiling_context(notebook_id, owner_id, readers, *, local_scope=None, base_scope=None)`
  （context manager）与 `CeilingReaders`（冻结数据类，三个可调用：`visible(nb)`、`hidden(nb, owner, include_memory)`、
  `mounted(nb)`）。扩展既有 `source_scope_context`，不另起安装路径；`source_scope.py` 不 import repo，读取器由调用方注入
  （`ask_service` 用 `repository_runtime.py:2573-2577` 已有的 `ask_engine_*` 端口，路由用 repo）。
- **语义**：
  1. 外层已有 scope（全局运行、嵌套调用）→ 原样透传（保住 `test_peer_mode_ask_steps.py:656` 与 subjectless 的单写者）。
  2. 本地维：调用方已提交（路由已冻结的 include）→ 原样用；未提交 → 合成 `include: visible(nb)`、
     `hidden_source_ids = hidden(nb, owner, include_memory=memory_channel_allowed())`、`narrowed=False`、`owner_id`，
     且 `source_provided=False`——`current_source_scope_payload()` 仍返回 None，报告持久化合同不变
     （`ceiling_active` 按值判定为真，`restricted` 为假，全选冻结同形）。隐藏半边用 RAW 读（含 Memory），与
     drift probe 的要求一致；`include_memory=False` 时复用 `plugin_hidden_sources`（`ask_service.py:2107-2132`）
     剔除 Memory 的写法，并把该写法搬进构造器，插件引擎改调构造器。
  3. 库维：已提交 → 原样；未提交 → `base_provided=False`，不按库过滤。
  4. **逐库天花板**：对每个挂载参与库 `p ≠ nb` 装 `notebook_source_ceilings[p] = visible(p)`，memo 键沿用
     `("federated_chunk_visible", p)`（与 `chunk_federation._peer_visible_sources` 同一份读）。`__post_init__`
     只禁止「本地天花板与逐库天花板同时覆盖当前库」，挂载库条目合法。
  5. 新字段 `ActiveSourceScope.ceilings_total: bool = False`：置位时，**非当前库且无条目**的库在 `covers_notebook` /
     `allows` / `scoped_allowed_source_ids` / `filter_retrieval_items` 一律拒绝。关闭两个失败即放行的口子：运行中途
     新挂载的库（`communities.mounted_base_ids`、`follow_start_row`、`_ppr_graph`、`_federated_rx_graph` 不走 memo），
     以及 E-1 的对等模式非参与库。构造器与 `global_run.py:164-174` 都置位；`subjectless` 的单写者守卫不变。
- **安装点（全部换成构造器）**：`AskService.ask`（`ask_service.py:1066`，一处覆盖 HTTP 同步/流式/作业与 MCP
  `ask_notebook`）、`ask_plugin_engine`（2166-2213 删去自合成）、`report_execution.py` 的 `start_plan`（:183）与
  `start_generate`（:263）worker、`ask_routes.py` 的两个意图预检（:504、:581，现为路由内进 context）。
  `report_engine.py:3359` 的 `run` 嵌在 worker 内，走透传。
- **静态守卫**：新增 `$R/backend/tests/test_default_ceiling_guard.py`——用 `tests/architecture/semantic_source.PythonSourceIndex`
  断言 `backend/app` 中 `source_scope_context(` 只被 `source_scope.py`（构造器）与 `global_run.py` 调用（相等断言的白名单）。
- **成本**：无范围运行多一次可见全集读与一次隐藏读（与浏览器默认路径今天付的相同；A5 实测 4.9 万来源库 17 ms）。
  检索腿沿用物化数组下推——浏览器冻结与全局运行已在生产这样做。浏览/列表读者不走这条，见 D2。

### D2 「Memory 来源本人可读」谓词

- **SQL 孪生**：新文件 `$R/backend/app/repositories/postgres/memory_sql.py` 与 `$R/backend/app/repositories/sqlite/memory_sql.py`
  （与 `mount_sql.py` 同款镜像对，模块 docstring 写全理由，另一份指向它）。一律用**排除形式**、不绑可读全集数组
  （PG16 实测：绑可读全集数组每条语句约 20 ms 规划，同连接约 10 次后切通用计划慢 5–20 倍）：
  - `memory_source_readable(source_alias)`：`({a}.source_type <> 'memory' OR EXISTS (SELECT 1 FROM memory_items rm WHERE rm.id = {a}.memory_id AND rm.created_by = %s))`，
    恰好一个参数（查看者）。空查看者 `''` → 任何 Memory 都不可读。`memory_id` 为空或孤儿的 Memory 来源对所有人失败即关。
  - `foreign_memory_object_excluded(object_alias)` 与 `foreign_memory_relation_excluded(relation_alias)`：
    `NOT EXISTS (SELECT 1 FROM sources fs WHERE fs.id = {o}.source_id AND fs.source_type = 'memory' AND NOT EXISTS (SELECT 1 FROM memory_items fm WHERE fm.id = fs.memory_id AND fm.created_by = %s))`，一个参数。
  - 用法边界：store 读者加关键字 `viewer_id: str | None = None`；`None` → 不拼谓词，SQL 文本与今天逐字相同。
    服务层只在 `KgViewerScopeReader.for_notebook(nb)` 返回非 None（库里有别人的 Memory）时传 `viewer_id`——短路沿用 A5，
    无 Memory 的库零成本、字节不变。
- **Python 孪生**：`$R/backend/app/services/kg_viewer_scope.py`（A5，fd0303f1）。E4-7 做两处对齐：`KgViewerScope` 暴露
  `viewer_id` 供 store 关键字使用；对象隐藏判据从「证据触及外人 Memory 且无可读来源」改为「主 `source_id` ∈ 外人 Memory 来源」
  （D4 的分类器；E4-5 迁移剥离存量混合证据后两者等价）。新增孪生测试：同一组夹具（本人 Memory、他人 Memory、
  孤儿 Memory、Knowhow、普通来源、混合证据）在 SQL 片段与 Python 规则下判定一致，双后端。
- **`hidden_source_ids`**（`postgres/source_store.py:203` / SQLite `:167`）改为消费 `memory_source_readable`（E3-1），
  使「谁的 Memory 进天花板」与读取端点同源。
- **读者（全部接入，按任务分配）**：来源/元素端点、`user_can_read_source`、`source_readable_in_participant_scope`、
  MCP `get_cited_element`、`catalog_routes._owned_source`（E3-1）；KG 列表/图/统一图/搜索/计数/治理队列/分析与
  笔记本搜索 KG 腿（E4-4、E4-7）；检索腿经 D1 天花板（E1、E2）。
- **静态守卫**：新增 `$R/backend/tests/test_memory_reader_guard.py`：扫描两个后端 `repositories/*/` 与 `postgres/search.py`
  里含 `FROM knowledge_objects`、`FROM knowledge_relations`、`source_elements`、`concept_clusters` 的 SQL 字符串常量，
  其所在函数必须登记在相等断言的白名单里并写明类别（读侧带 `viewer_id` / 建图侧带 `memory_derived_*` 排除 /
  写路径 / 按 source_id 自限）。新增读者不登记即红。配一组行为矩阵（两名成员、一方有已确认 Memory，逐端点断言不出现），
  不单靠正则。

### D3 挂载有效性谓词（带查看者，M3）

- **契约变更（两个后端同修）**：`mount_sql.py` 模块 docstring 里「owner 取挂载方笔记本的 created_by 而非请求用户，
  使参与集与谁在提问无关」**作废**。新契约：参与集随提问人变化；有效边 = 既有 `MOUNT_VALID_EXPR` **且**
  `(b.tier = 'base' OR everyone_grant_expr('b.id') OR v.uid = a.created_by OR <v.uid 对 b 有读权>)`。
  其中读权用 `access_sql` 既有的列引用形式拼装：`b.created_by = v.uid OR member_exists_expr('b.id', 'v.uid', 'vm') OR grant_access_expr('b.id', 'v.uid', ...)`，
  不引入新参数。
- **新片段**：`MOUNT_VIEWER_JOIN`（`... CROSS JOIN (SELECT %s AS uid) v WHERE e.notebook_id = %s ...`，恰好两个参数，顺序
  `(viewer_id, notebook_id)`）、`MOUNT_EFFECTIVE_FOR_VIEWER`、`MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY`。新名字而不是改旧
  片段，使漏改的调用点在绑定参数时直接报错，而不是静默错绑。旧的单参数片段保留给不随查看者变化的四处。
- **挂载人 = `a.created_by`**：挂载是 owner 专属（`notebook:mount` → owner，`api/deps.py:387`，`notebook_routes.py:242-275`），
  仓库没有笔记本转让。`notebook_bases.created_by` 不采用：两后端可空（PG `0001_initial.sql:503`，SQLite
  `migrations.py:344,1700`）；`replace_mounts` 每次整批删除重插并盖上当前编辑者（PG `notebook_store.py:208-229`）；
  深拷贝改写为新主人（`notebook_sharing.py:130-160`）；同步导入把未映射用户映射为导入者
  （`migration/sync/manifest.py:384-386`）。**NULL 的处理**：`notebooks.created_by` 可空（`0001_initial.sql:522`），
  `v.uid = NULL` 不为真，于是「挂载人未知 → 只对自己能读被挂库的用户生效」，正是保守读法；空查看者（无 actor 的后台路径）
  同样只剩 base / everyone 两支，失败即关。
- **随查看者变化的消费点**（两个后端）：`notebook_store.resolve_participants` / `participant_ids` / `participant_tiers` /
  `participant_notebook_ids`（PG :76-131，SQLite :68-116）、`participant_rows`（PG :110 / :96）、
  `unified_kg_store.mounted_base_ids`（PG :1417 / :1333）、`knowledge_store.any_mounted_has_kg_on`（PG :1173 / :1017）、
  `knowledge_store.follow_start_row`（PG :1635、子查询 :1653 / :1399、:1423）、`query_store.notebook_has_usable_base_kg`
  （PG :312 / :367）、`query_store.mounted_bases_row`（PG :441 / :559）。**不变**：`list_mount_edges` 与 `mountable_notebooks`
  （路由 owner 专属，查看者恒为 `a.created_by`）、`sharing_store.valid_copied_mount_base_ids`（按新主人判）、
  `governance_store.mounted_public_base_ids`（只看 `tier='base'`）。
- **查看者来源**：新增 `$R/backend/app/services/retrieval_run.py::current_viewer_id()`（检索运行内取
  `current_retrieval_run().actor_id`，否则取请求用户，都没有返回 `''`）。`repository_runtime.py` 里注入参与者谓词的三处
  wiring（:2004、:2176、:2574）统一传 `viewer_id=current_viewer_id()`，服务层调用点绝大多数不用改。store 方法的
  `viewer_id` 为**必填关键字**（无默认值），漏传当场 TypeError。
- **缓存键**：`graph_retrieval._participant_graph_cache_key`（:42-75）在「查看者有效参与集 ≠ 全部有效边」时改为
  `{nb}:{fp}:{family}`，`fp` 复用 `retrieval_participants.override_fingerprint` 的算法作用于有效集合（fingerprint 必须在 family
  之前，驱逐按后缀匹配）；两者相等时键逐字不变，于是主人与集合相同的成员共享缓存。`retrieval_candidates.py:396-404` 的
  run 内 memo `("retrieval_participants", active_id)` 加 `actor_id`。`("federated_chunk_visible", nb)`、`cluster_map`、
  `matrix`、`kwtok` 按单库键，不受影响。笔记本摘要没有跨请求缓存（`notebook_catalog.py:248-255` 每次查询），无需改键。
- **守卫**：`$R/backend/tests/test_mount_viewer_guard.py`——`participant_*` / `mounted_base_ids` / `any_mounted_has_kg_on` /
  `follow_start_row` / `notebook_has_usable_base_kg` / `mounted_bases_row` 的每个生产调用都显式传 `viewer_id=`；
  `test_participant_override_guard.py` 的白名单同步更新。

### D4 「派生自 Memory」分类器（M1、M2、删除清理共用）

- **规则**：KG 对象/关系派生自 Memory ⇔ 其**主** `source_id` 指向 `source_type = 'memory'` 的来源。依据：`store_kg` 每个对象都
  新铸 `ko-` id（`knowledge_lifecycle.py:1174`），带 `source_generation` 时拒收其他来源的证据（1210-1212）；关系补全与重链只在
  单一来源内（`completion_candidate_rows` 等钉 `source_id`，`relink_notebook_kg` docstring 1831）。跨来源混合只有三条路：
  簇成员、手工合并（N-1）、冲突 "modify"（N-2）——E4 把三条都堵上，E4-5 迁移剥离存量混合。
- **位置**：同在 `memory_sql.py`：`memory_derived_object(alias)`、`memory_derived_relation(alias)`（零参数：
  `EXISTS (SELECT 1 FROM sources ds WHERE ds.id = {a}.source_id AND ds.source_type = 'memory')`），以及从
  `query_store` 搬来的 `no_memory_member_cluster(cluster_alias)`（原 `_NO_MEMORY_MEMBER_CLUSTER_SQL`，PG :113）；原
  `_NOT_MEMORY_OWNED_SQL`（PG :104 / SQLite :107）改为引用 `memory_derived_object('o')` 的否定。
- **消费方**：建图输入（E4-2、E4-3、E4-6）、检索缓存构建（E2-2）、拷贝快照（E5-1）、删除清理（`remove_memory_source` →
  `delete_source` → `clear_source_graph_state` 按证据引用删对象；不变量成立后只会删到 Memory 对象本身）。
- **Knowhow 不在内**：Knowhow 投影是笔记本级共享的，M1 只针对 Memory。

### D5 Memory 通道开关（`memory:read`）

- **位置**：`source_scope.py` 新增 `memory_access_context(allowed: bool)` 与 `memory_channel_allowed()`（独立 ContextVar，
  默认 True；不进 `ActiveSourceScope`，因为 MCP 的 `search_notebook_context` 不装 scope 也要用）。
- **单一接缝**：`$R/backend/app/services/memory_retrieval.py` 的 `MemoryRetriever.notebook_memory_hits` 与 `.context` 在开关关闭时
  返回空。覆盖全部七个调用点（`retrieval_candidates.py:813`、`ask_service.py:1541/1739/3017`、`knowledge_query.py:196`、
  `notebook_catalog.py:801`、`report_engine.py:1477/2427`），调用点不改。D1 构造器读同一开关决定隐藏半边是否含 Memory。
- **安装方**：MCP `ask_notebook` 与 `search_notebook_context` 按 token 的 `memory:read` 装开关（E1-3）。

## 3. 存量数据的 M1 清理

**要清掉什么**：上线前构建的共享产物里，Memory 派生对象可能是簇成员或簇种子（`K-<规范化种子名>` 的 canonical id 本身
可能就是 Memory 名字）、`canonical_name` / `canonical_description`（整簇复制到每个成员行）、`canonical_relations` 计数、
`mention_edges` / `concept_comentions`、`communities` / `community_members.canonical_name` 与社区摘要、`kg_analysis_artifacts`、
`concept_merge_candidates` / `kg_conflict_candidates`、磁盘上的可视化工件与 scale ANN，以及手工合并留下的混合证据。

**canonical id 的铸造规则（上线后）**：Memory 对象既不做种子、也不做成员，永远是单例，canonical 即自身对象 id（沿用
`COALESCE` 路径）；共享簇的 `K-<种子名>` 只可能来自共享种子。存量 id 由下面的迁移整簇删除 + 重建重铸。

### 3.1 迁移（PR-E4：PG `0067_memory_kg_isolation.sql`，SQLite `_migration_87`，`SCHEMA_VERSION = 87`）

同一顺序、同一语义，SQLite 用 Python 实现 JSON 改写；只作用于「含 Memory 来源」的库（受影响库集合 F）：

1. `unified_kg_state` 加列 `memory_isolation_version integer NOT NULL DEFAULT 1`；`UPDATE ... SET memory_isolation_version = 0 WHERE EXISTS (SELECT 1 FROM sources s WHERE s.notebook_id = unified_kg_state.notebook_id AND s.source_type = 'memory')`。
   新行默认 1（上线后建的图天然隔离）。PG 同步 `schema_manifest.py`。
2. M = F 内 `memory_derived_object` 的对象；A = 任一代（published / building，见 `0051_derived_generation.sql`）里含 M 成员的 canonical id。
3. 删除 `concept_clusters` 中 `canonical_id ∈ A` 的**整簇**行（带走 Memory 名的 id、名字与描述；其余共享成员暂成单例，重建后重新成簇）。
4. 删除 `concept_merge_candidates`、`kg_conflict_candidates`、`mention_edges`、`concept_comentions`、`canonical_relations`、
   `community_members` 中引用 A ∪ M 的行；清空 F 内含这些成员的 `communities` 的摘要列；删除 F 的 `kg_analysis_artifacts`。
   列名由实现方按 `0001_initial.sql` 核对。
5. 剥离混合证据：F 内**非** Memory 对象的 `evidence` 去掉 `source_id` 为 Memory 来源的条目（PG 用 `jsonb_agg` 子查询），删除对应
   `knowledge_object_sources` 行。主来源是 Memory、证据里夹着共享条目的对象（有人把共享对象手工合进了 Memory 对象）**保留为本人私有**：
   不外泄，代价是那段被手工合并走的共享证据只剩 Memory 主人可见。迁移日志输出这类对象的计数（不含内容），运维文档写明。
6. F 内 `community_seq`、`canonical_rel_seq`、`mention_seq` 置 -1，`dirty = 1`，`kg_mutation_seq` 与 `cluster_mutation_seq` 加 1
   （各层按序号闸重算，进程缓存按版本失效）。

幂等：删除已删的行是空操作；改写只挑仍含 Memory 条目的对象；版本列保证不重复入队。成本：所有语句按 `notebook_id` 限定并走
`memory_derived_object` 的 EXISTS；JSON 改写只碰有混合证据的对象（只来自手工合并，量小）。公共库不会有 Memory
（`memory_kg_eligible` 排除 `tier='base'`，`source_ingestion.py:2154`），超大公共库不在 F 里。

### 3.2 上线后重建（PR-E4，`$R/backend/app/services/memory_isolation_rebuild.py`）

- 同时把 `kg_merge.CLUSTER_ALGO_VERSION` 从 3 升到 4（`kg_merge.py:52`），使非强制重建必然重算。
- `startup_warmup.py` 在 `mark_ready()` **之后**新增一步 `_rebuild_memory_isolated_notebooks(repo)`（仿 `_reproject_legacy_knowhow_tables`，
  :869；吞异常，不影响就绪）：查 `memory_isolation_version = 0` 的库，交给**一个**后台工作线程逐库执行，走与
  `POST /unified-kg/rebuild` 相同的 `KgMaintenanceJobs` 路径（共享租约/claim 与进度），以系统 actor 调
  `rebuild_unified_kg(force=False)`。它会连带重算规范关系、提及桥、社区、可视化与自动索引（5919-5941）。
- 成功时置 `memory_isolation_version = 1`：写在重建成功落库调用的那个 store 方法里，不在 `rebuild_unified_kg` 函数体里
  （482 行零余量）。任何一次重建（包括手动）都会顺带清掉标记，这是对的，因为上线后的重建天然隔离。
- 失败：发内容无关事件 `memory_isolation_rebuild_failed`（库 id、阶段、异常类名），下次启动重试；也可以手动点重建或跑
  `backend/app/scripts/recluster_kg.py`。
- 过渡期读侧：F 内未完成重建的库，`KnowledgeLifecycleService.unified_graph` **不读**迁移前的可视化工件——对象数 ≤
  `viz_sync_build_max_objects` 时走全量路径（带 D2 谓词），否则返回既有的「可视化未构建」形态（E4-7）。其余读者本来就走 D2 谓词。
- 成本：聚类是流式的；模型花费只落在新出现的歧义种子对的 `kg_merge_review`（有检查点）与描述签名变了的簇的
  `kg_concept_description`（签名复用）；社区是纯图计算，不调模型；可视化超过同步阈值时重新挂到自动索引上。
- 运维怎么确认完成：`SELECT count(*) FROM unified_kg_state WHERE memory_isolation_version = 0` 为 0；checkup 新增只读项
  「Memory 隔离重建待完成：N 个库」；事件 `memory_isolation_rebuild_started/completed/failed`。步骤写进 `docs/operations*.md`。

### 3.3 防止复发的写侧守卫

E4-2/E4-3/E4-6 的建图输入排除、E2-2 的缓存构建排除、E4-1 的 chunk 写入拒绝、E4-3 的跨类合并拒绝，加上
`test_memory_reader_guard.py` 的建图侧登记。

## 4. PR 划分与顺序

| PR | 目标 | 分支 / worktree | 依赖与合入顺序 |
| --- | --- | --- | --- |
| **PR-E0** 共享判据 | D2/D4 的 `memory_sql.py` 双后端 + 契约测试；`query_store` 旧常量改为引用它 | 从 master 新建 `claude/memory-sql-predicates` | 无依赖；**第 1 个合入** |
| **PR-E5** 拷贝与删除清理 | M2、C-9、孤儿清扫 | 从 master 新建 `claude/memory-copy-delete`，E0 合入后 rebase | E0；第 2 个 |
| **PR-E1** 默认天花板 | D1、D5；A-1、B-1、B-7、E-1、C-11 | 从 master 新建 `claude/default-source-ceiling` | PR-A/B/C 合入后才做 E1-2；第 3 个 |
| **PR-E2** 检索各腿 | B-2～B-6、B-9～B-11、C-5、D-5 插件半 | PR-A/B/C 合入后从 master 新建 `claude/retrieval-leg-ceilings` | 第 4 个（与 E1 无代码依赖，合入顺序在 E1 后，测试用显式装的 scope） |
| **PR-E3** 来源/元素读取 | C-3、D-5 catalog 半、`hidden_source_ids` 同源 | PR-D 合入后从 master 新建 `claude/memory-source-reads` | E0；第 5 个 |
| **PR-E4** KG 的 Memory 结构隔离 | M1 写侧 + KG 读者 + 存量迁移；C-1/C-2/C-4/C-6/C-7/C-10、A-4 | 从 master 新建 `claude/kg-memory-isolation`，E0 合入后 rebase | E0、PR-A（A5）、PR-B；第 6 个 |
| **PR-E7** 回答级与分享 | D-1～D-4、M4 | 从 master 新建 `claude/answer-share-guards` | PR-D、E1（`ask_routes.py`）；第 7 个 |
| **PR-E8** 晋升出处 | B-12（B-11 的读侧在 E2） | E4 合入后从 master 新建 `claude/promotion-provenance` | E2、E4；第 8 个 |
| **PR-E6** 挂载仅对挂载人生效 | M3、N-6 | 从 master 新建 `claude/mount-viewer-scope` | 调用点散在 E2/E3/E4 的文件里，**最后合入** |

同一文件跨 PR 在不同波次修改的，按上表合入顺序 rebase（改动落在不同函数）。每个 PR 最后一个任务是「Z：文档、基线与端口整合」。

## 5. 任务

每个任务通用红线：先 PG 后 SQLite，双后端同批；新增 SQL 配 EXPLAIN pin（`backend/tests/postgres/*_explain_pins.py` 同款）；
单库无挂载、无外人 Memory 的库字节不变；热函数零余量；`ports.py` 只加关键字参数；不在合成或终态核对里加权限分支；
测试按可观测行为断言，不钉行号。关键用例写明它必须抓住的变异。

### PR-E0 共享判据

**E0-1 `memory_sql.py` 与旧常量归一**（sonnet）
- 目标：落地 D2/D4 的全部片段；`query_store` 的 `_NOT_MEMORY_OWNED_SQL` / `_NO_MEMORY_MEMBER_CLUSTER_SQL` 改为引用它，SQL 文本逐字等价。
- 文件：新建 `$R/backend/app/repositories/postgres/memory_sql.py`、`$R/backend/app/repositories/sqlite/memory_sql.py`；
  改 `$R/backend/app/repositories/postgres/query_store.py`、`$R/backend/app/repositories/sqlite/query_store.py`；
  新测试 `$R/backend/tests/test_memory_sql_contract.py`、`$R/backend/tests/postgres/test_memory_sql_contract_pg.py`。
- 验收：片段参数个数固定（readable / foreign 各 1 个，derived / cluster 各 0 个）；行为矩阵在真实表上成立——本人 Memory
  可读、他人 Memory 与孤儿 Memory 不可读、Knowhow 与普通来源可读、空查看者不可读；嵌进更大查询结果一致（仿 `test_access_sql_contract.py`）；
  `query_store` 旧查询结果不变（取一条既有用例前后对比）。
- 变异：把 `rm.created_by = %s` 换成 `true`，矩阵里「他人 Memory 不可读」必须红；去掉 `source_type = 'memory'` 条件，「普通来源可读」必须红。
- 文档：`docs/development*.md`「依赖、授权与状态所有权」段加一句：`memory_sql.py` 与 `access_sql.py` / `mount_sql.py` 同样双后端同步维护（归 E0-Z）。

**E0-Z**（sonnet）：上述文档；baseline 不涉及。

### PR-E1 默认天花板

**E1-1 构造器与开关**（opus）
- 目标：D1（构造器、`ceilings_total`）、D5 开关、E-1。
- 文件：`$R/backend/app/services/source_scope.py`；新测试 `$R/backend/tests/test_default_source_ceiling.py`。
- 验收：无外层 scope 时装出 include + 本人隐藏 + 挂载库逐库可见天花板，`current_source_scope_payload()` 为 None，
  `source_scope_restricted()` 为假；有外层 scope 时透传；`ceilings_total` 置位时无条目的非当前库在四个接口都拒绝；
  subjectless 运行里未选中的公共库被 `covers_notebook` 拒绝（E-1）；`memory_channel_allowed()` 默认真，
  `memory_access_context(False)` 内为假，并使构造器的隐藏半边不含 Memory。
- 变异：去掉 `ceilings_total` 分支，「运行中途新挂载的库被拒」必须红；把合成的 `source_provided` 改成 True，
  「报告持久化 payload 仍为 None」必须红。
- 红线：不动 `subjectless` 的单写者；`__post_init__` 的歧义拒绝不放松。

**E1-1 落地记录（提交 00b0d6a2）与规格评审后补入的事项**
- 读取器形状与计划不同：`CeilingReaders` 四个可调用（`participants`、批量 `visible_by_notebook`、原始 `hidden`、`source_metadata`），
  Memory 剥离在构造器里做（`partition_memory_sources`，缺元数据的 id 两边都不放）；新字段 `withheld_hidden_source_ids` 只给漂移探针读。
  构造一次 3 次读（Memory 通道关闭且隐藏集非空时 4 次），与来源数、挂载库数无关。
- **E1-1 修复轮要做**：① 报告重装入口——`report_engine.py:3359` 在 worker 内用刷新后的冻结**替换**外层 scope，构造器的无条件透传
  表达不了；在 `source_scope.py` 加受限的重装入口：外层是非 subjectless 的默认天花板时替换本地维与库维，同时继承外层的
  `notebook_source_ceilings` 与 `ceilings_total`，外层 subjectless 时仍透传（本计划 D1「走透传」一句据此更正）；② 提交的
  `local_scope` 在 Memory 通道关闭时同样剥掉 Memory（把「MCP 不提交范围」这条隐含前提变成失败即关）；③ 两处写了尚未发生之事的
  注释（`source_scope.py:220`、`:1336`）改成如实描述。
- **E1-2 必须做（接缝没替它做）**：删掉插件引擎的自合成 `ask_service.py:2159-2212`（它按 `not scope.source_provided` 判断，而合成的
  本地维 `source_provided=False`，留着会在构造器里面再装一层没有逐库天花板的 scope）；`plugin_hidden_sources` 改为无条件
  `partition_memory_sources(...)[0]`（插件引用带不上 memory 身份，必须始终剔除），仓库只剩一种写法；`AskService` 注入批量
  `visible_by_notebook` 与 `source_metadata`（构造参数，不加 Protocol 方法）；`global_run.py:164-174` 置 `ceilings_total=True`；
  安装点不得吞掉读取器异常再退回无 scope；`follow_chain` 早退（`retrieval_service.py:133-138`）与 `any_base_has_kg`（`:476`）
  补看 `ceilings_total`（前者若 E2-1 先删则免）；更新 `notebook_source_ceilings`、`scoped_subgraph_nodes` 里「缺省即逐字不变 / 生产不可达」
  的过时 docstring；成本对照要计入：漂移探针每次多两次读（今天 scope 为 None 时零读）、有挂载时 `_chunk_kg_overlay` 多一次批量证据读。
- **E1-3 必须做**：`_run_ask_notebook` 与 `search_notebook_context` 在 worker 线程内进入 `memory_access_context(allow_memory)`；
  `MemoryRetriever` 两个方法在通道关闭时返回空；MCP 入口不得开始提交 `local_scope`；报告 worker 用重装入口。
- **E1-Z 文档要点名的用户可见变化**：挂载库的 Knowhow / 个人记忆投影不再参与单库问答，在该库内提问仍可用。

**E1-3 MCP 与报告安装**（opus，波次 2）
- 目标：C-11；报告 worker 装默认天花板；D5 接缝；A-2 回归钉。
- 文件：`$R/backend/app/api/mcp_tools/memory_context.py`、`$R/backend/app/services/report_execution.py`、`$R/backend/app/services/memory_retrieval.py`；
  测试 `$R/backend/tests/test_memory_mcp.py`、`$R/backend/tests/test_report_api.py`。
- 做法：`_run_ask_notebook` 与 `search_notebook_context` 包 `memory_access_context(allow_memory)`；把 `ask_notebook` 里的引用过滤
  （963-966）与锚点循环（924）抽成 `_strip_memory_items(answer, allow_memory)`，锚点按 `object_type == 'memory'` 同样过滤，
  `ask_notebook` 因此变短，baseline 同 diff 下调；`MemoryRetriever` 两个方法在开关关闭时返回空；`start_plan` / `start_generate`
  改用构造器。
- 验收：无 `memory:read` 的 token 调 `ask_notebook`，答案引用、锚点里都没有 Memory 条目，也没有 Memory 投影的元素/KG 命中
  （天花板不含本人 Memory 来源），Memory 通道零调用；有 `memory:read` 时照旧；`search_notebook_context` 同样。
  无范围创建的报告在 plan / generate 两阶段都装了默认天花板，且 `understanding.source_scope` 仍持久化为 None
  （改写 `test_report_api.py:1016` 为这条新断言，并同步 :1313/:1349）。报告三阶段都重冻结为 include（A-2 回归钉）。
- 端到端：经真实 MCP 工具入口（进程内客户端）跑两位成员、一方有已确认 Memory 的共享库。
- 变异：锚点过滤删掉，「锚点无 Memory」必须红；`MemoryRetriever` 忽略开关，「Memory 通道零调用」必须红。

**E1-2 安装到问答入口**（opus，波次 3：PR-B/C 合入后）
- 目标：A-1、B-1、B-7 的安装落地；插件引擎改调构造器；`global_run` 置 `ceilings_total`；静态守卫。
- 文件：`$R/backend/app/services/ask_service.py`（`AskService.ask` 57 行、`ask_plugin_engine` 477 行，均无上限）、
  `$R/backend/app/api/ask_routes.py`（两个意图预检；`_validate_source_scope` 的 docstring）、`$R/backend/app/services/global_run.py`；
  新守卫 `$R/backend/tests/test_default_ceiling_guard.py`；改写 `$R/backend/tests/test_ask_engine_plugin.py`（:1897 过时 docstring、:1977）、
  `$R/backend/tests/test_source_scope.py`（:61、:742、:803——经服务入口的改写，直调原语的保留并注明生产不可达）、
  `$R/backend/tests/test_source_graph_activation.py:244`、`$R/backend/tests/test_chunk_retrieval_characterization.py`（:901、:1768）、
  `$R/backend/tests/test_base_scope.py`（:126、:587、:1142）、`$R/backend/tests/test_peer_mode_ask_steps.py`（:676/:680）、
  `$R/backend/tests/test_global_run.py`（:513/:531）；docstring 更新 `$R/backend/tests/test_knowledge_context_source_ceiling.py:221`、
  `$R/backend/tests/test_peer_ceiling_subgraph.py:301`。
- 验收（每个入口至少一条经真实路由的端到端，两个后端）：共享库里成员 B 有已确认 Memory（带元素与 KG），成员 A 以
  ① HTTP `/ask` 不带 `source_scope`、② `/ask/stream`、③ 意图预检、④ MCP `ask_notebook`、⑤ 无范围报告提问——元素臂、两条 KG 臂、
  关系、社区邻居、推导链都拿不到 B 的 Memory 派生内容；A 自己的 Memory 照常参与。单库运行挂一个非公共库、库里有他人
  Memory 与 Knowhow 投影：KG/关系/社区/推导链只出该库可见来源的内容（B-1、B-7）。无挂载、无外人 Memory 的库，答案与轨迹
  和改前字节一致（黄金 fixture 不变）。
- 变异：`AskService.ask` 退回 `source_scope_context(nb, None, None)`，①④ 必须红；逐库天花板不装，B-1 用例必须红；
  删掉 `global_run` 的 `ceilings_total`，E-1 端到端（锚点挂了未选中的公共库，各通道都不出其内容）必须红。
- 红线：`ask_chunk` 等有上限函数不加行；插件引擎删自合成后不留第二份实现。

**E1-Z 文档、基线与端口**（sonnet，E1 最后）
- `docs/product-and-api.md` / `_zh.md`「按来源选择检索范围」（EN :558/:560，ZH :458/:460）：省略字段不再等于历史全范围，改为冻结
  默认天花板（可见 ∪ 本人隐藏），单库运行里挂载库只贡献可见来源；「收窄时本人隐藏来源不参与」保留并写准；删掉那句对无范围
  运行不成立的「绝不暴露」，改成对所有入口成立的表述。
- `docs/agent-mcp-memory-sop*.md`：`memory:read` 现在同时约束答案正文、引用、锚点与 Memory 投影命中。
- `docs/development*.md`：默认天花板守卫与 `ceilings_total` 一句。`architecture.md` 数据流里的 scope 安装点。
- baseline：`ask_notebook` 下调到实测值。

### PR-E2 检索各腿按天花板（波次 3，PR-A/B/C 合入后）

**E2-1 服务层腿**（sonnet）
- 目标：B-3、B-4、B-5 服务侧（`kg_block` / `kg_id_map`）。
- 文件：`$R/backend/app/services/retrieval_service.py`、`$R/backend/app/services/reasoning_retrieval.py`（只动 `_filter_candidates`
  的 chain 分支，不碰 `run`）、`$R/backend/app/repositories/postgres/unified_kg_store.py`、`$R/backend/app/repositories/sqlite/unified_kg_store.py`
  （`weak_support_relation_rows` PG :1105 / :1023、`relation_endpoint_name_rows` 加 `allowed_source_ids` 关键字，支持判定同
  `community_member_peers`）；测试新建 `$R/backend/tests/test_retrieval_leg_ceilings.py`。
- 验收：`follow_chain` 每一跳的证据无条件过 `filter_evidence`，无幸存证据即丢该跳（删掉 :133-138 的「无天花板就原样返回」）；
  弱支撑关系在全选冻结下不出现只被外人 Memory 支撑的目标名；`mixed_chunk_candidates` 的 `kg_block` / `kg_id_map` 只保留幸存节点。
- 变异：恢复 :133-138 的早退，推导链用例（他人 Memory 引文当锚点）必须红；弱支撑 SQL 忽略参数必须红。

**E2-2 候选层腿**（sonnet）
- 目标：B-5 候选侧、B-9 服务侧、B-10、C-5、D-5 插件半。
- 文件：`$R/backend/app/services/retrieval_candidates.py`、`$R/backend/app/services/exact_lookup.py`、`$R/backend/app/services/plugin_ask_engine.py`；
  改写 `$R/backend/tests/test_source_scope.py:994`（受限精确查找零 I/O 的替身）、`$R/backend/tests/test_peer_mode_exact_arm.py`、
  `$R/backend/tests/test_chunk_retrieval_characterization.py:1074-1082`、`$R/backend/tests/test_federated_global_budget.py:240`。
- 做法：`_chunk_kg_overlay`（:4738）在 `scope.ceiling_active or scope.peer_ceiling_active` 时剪枝，`_ceiling_scoped_subgraph`（:4588）
  对当前库按 `scope.allows` 判；删除 `_exact_lookup_chunks_one` 的收窄关闭闸（:4329-4330），单库路径传
  `scoped_allowed_source_ids(nb, visible)`，`_ceiling_bound_exact_deps`（:164-185）从先取后滤改为下推关键字；
  `_retrieve_chunks_ann` 在 `if allowed is not None` 内 `ids, mat = self._mask_vector_matrix(ids, mat, kept_ids)`；
  `kwtok` 缓存（:1306-1333）改为只由非 Memory 对象构建，不再取决于第一个调用方的过滤，Memory 对象恒走现场打分；
  关系向量矩阵（:1256-1277）只装非 Memory 关系，本人 Memory 关系按本人隐藏来源现场打分；插件命中来源不在
  `source_origin` 时丢弃（:641-643，同 :342 失败即关）。
- 验收：收窄运行里精确标识符检索可用，且天花板外的来源不占探测窗口（天花板外 50 条同名命中 + 天花板内 1 条，仍返回界内那条）；
  kwtok 在「先无范围调用、后范围调用」顺序下排名不受外人 Memory 片段影响；ANN 返回的矩阵只含保留行。
- 变异：恢复关闭闸，收窄精确用例必须红；恢复先取后滤，窗口用例必须红；kwtok 构建带回 Memory，排名用例必须红。

**E2-3 图与 PPR**（opus）
- 目标：B-2。
- 文件：`$R/backend/app/services/graph_retrieval.py`、`$R/backend/app/repositories/postgres/chunk_store.py`、`$R/backend/app/repositories/sqlite/chunk_store.py`
  （`graph_hydrate_rows`，PG :409）。
- 做法：图结构缓存仍按参与集（不把逐次天花板放进缓存键，避免按勾选组合重建多百万节点图）；在 `ranked[:top_chunks]` 截断**之前**
  按逐库天花板过滤候选（水合带 `allowed_source_ids`），使截断后的名额只给界内段落；Knowhow 行随「挂载库只开放可见来源」出局。
- 验收：挂载库有高分 Knowhow 行与冻结后新上传的来源时，PPR 输出只含界内段落且条数达到 `top_chunks`（界内候选足够时）。
- 变异：过滤挪回截断之后，「条数达到 top_chunks」必须红。

**E2-4 store 与证据**（opus）
- 目标：B-6、B-9 store 侧、B-11 读侧、N-4；E2 全部端口关键字。
- 文件：`$R/backend/app/repositories/postgres/knowledge_store.py`、`$R/backend/app/repositories/sqlite/knowledge_store.py`
  （`in_network_relation_rows` PG :1720 / :1479；`chunk_exact_search` :3197 / :3045；`_enrich_evidence` :1867 / :1642；
  `follow_relation_evidence_rows` :1692）、`$R/backend/app/repositories/postgres/search.py`（`chunk_exact_candidate_rows` :725）、
  `$R/backend/app/services/evidence_context.py`、`$R/backend/app/repositories/ports.py`；改写与新增
  `$R/backend/tests/test_evidence_context_service.py`（:703 保留无天花板值，加天花板用例）、`$R/backend/tests/test_canonical_relations.py:393`（同）、
  PG conformance 与 EXPLAIN pin。
- 做法：`in_network_relation_rows(..., allowed_source_ids=None)`：有天花板时 `AND r.source_id = ANY(%s)`（SQLite `json_each`），
  `DISTINCT` 改为 `GROUP BY` 并投影 `COUNT(DISTINCT r.source_id)` 作为界内支持数；`evidence_context` 在天花板生效时用界内数渲染
  「×N源」，没有界内行的边直接消失。`chunk_exact_candidate_rows` / `chunk_exact_search` 加 `allowed_source_ids` 关键字（PG 同
  `search.py:517-521` 的 `= ANY(%s)`，SQLite 同 `chunk_fts_search` 的 `json_each`），空列表返回空。`_enrich_evidence(..., owner_notebook_id=)`：
  只用属于对象所在库、且库存活的来源的元素覆盖文本与来源；外库条目保留存储时的 `quoted_span` / `source_title`，不按全局 id 现读；
  `citation_source_info` 不用外库来源的现名覆盖标题。`follow_relation_evidence_rows` 加库谓词。
- 端口（关键字）：`chunk_exact_search`（`ports.py:2472`）、`in_network_relation_rows`（:2512）、`in_network_relations`（:2448）、
  `_enrich_evidence`（:2363）、`citation_source_info`（:2977）、`follow_relation_evidence_rows`（:2510）、`weak_support_relation_rows`、
  `relation_endpoint_name_rows`、`graph_hydrate_rows`。
- 验收：晋升到公共库的对象，引用卡与提示词里不再出现推广者私有库元素的现文与现标题（B-11）；关系行只计界内来源。
- 变异：`_enrich_evidence` 去掉库谓词，B-11 用例必须红；`in_network_relation_rows` 忽略天花板，「×N源」用例必须红。

**E2-Z**（sonnet）：`docs/product-and-api*.md` 检索语义（收窄时精确查找可用；关系支持数按界内来源计；PPR 名额只给界内段落；
晋升对象外库证据按快照呈现）；`fangan_todo.md` 删去「逐库精确臂的已知残余……下推进 `chunk_exact_search`」那一句，
`fangan_done.md` 记完成；baseline 不涉及（E2 碰到的函数均无上限）。

### PR-E3 来源/元素读取按 Memory 属主（波次 3，PR-D 合入后）

**E3-1**（sonnet）
- 目标：C-3、D-5 catalog 半、`hidden_source_ids` 同源、改正错误注释。
- 文件：`$R/backend/app/api/source_routes.py`（:491、:791、:801、`source_readable_in_participant_scope` :862 及其三个调用 :916/:928/:942；
  改正 ~880 行「Memory 投影根本不写 source_elements」——事实是 `source_ingestion.py:2214-2250` 写元素与元素向量）、
  `$R/backend/app/services/notebook_sharing.py`（`user_can_read_source` :1147）、`$R/backend/app/repositories/postgres/sharing_store.py`、
  `$R/backend/app/repositories/sqlite/sharing_store.py`（`source_notebook_id` 加 `viewer_id` 关键字，拼 `memory_source_readable`）、
  `$R/backend/app/repositories/postgres/source_store.py`、`$R/backend/app/repositories/sqlite/source_store.py`（`hidden_source_ids` 消费片段）、
  `$R/backend/app/api/mcp_tools/citations.py`、`$R/backend/app/api/catalog_routes.py`（`_owned_source` :139 拒绝 `memory` / `knowhow`，同
  `source_routes._HIDDEN_SOURCE_TYPES` 先例）；改写 `$R/backend/tests/test_memory_mcp.py:3321-3324`、`$R/backend/tests/test_multi_domain_bases.py:2066-2100`
  （无主 `mine-memory` → 404，`mine-knowhow` 仍 200）；新建 `$R/backend/tests/test_memory_source_endpoints.py`。
- 验收：同库成员 A 对 B 的 Memory 来源调 `GET /sources/{id}`、`/elements`、`/elements-page`、in-scope 三端点、MCP `get_cited_element`
  一律 404（与不存在不可区分），对自己的 Memory 200；可预测元素 id 同样 404；catalog 七个端点对 Memory / Knowhow 来源 404。
- 变异：`source_notebook_id` 忽略 `viewer_id`，端点矩阵必须红。
- **E3-Z**：`docs/product-and-api*.md` Memory 一节（Memory 来源与元素只对创建者可读；catalog 不接受隐藏来源）。

### PR-E4 KG 的 Memory 结构隔离

**E4-1 chunk 写入拒绝**（sonnet，波次 1）
- 文件：`$R/backend/app/repositories/postgres/chunk_store.py`、`$R/backend/app/repositories/sqlite/chunk_store.py`（`replace_source_chunks` :209、
  `insert_rows` :323：按涉及的 source_id 查一次 `source_type`，含 `memory` 即抛 `ValueError("memory sources are not chunked")`）、
  `$R/backend/app/services/source_chunking.py`（`build_chunks_for_source` 遇 Memory 来源直接返回 0 并发内容无关事件）；
  新建 `$R/backend/tests/test_memory_chunk_guard.py`。
- 验收：经 facade `_build_chunks_for_source` / `_chunk_and_embed_source` 与 `maintenance.chunk_and_embed_source` 对 Memory 来源建 chunk，
  零行写入；store 直写被拒。变异：去掉 store 检查，直写用例必须红。

**E4-1 落地记录（提交 5d02306a）与由此补入的范围**
- `build_chunks_for_source` 返回类型是 `str`（回落告警码），遇 Memory 来源返回 `""`，不是计划原文的 0。
- `replace_source_chunks(memory_source, [])` 同样被拒（同一条写路径）；没有调用方对 Memory 来源这样用。
- 三条既有测试原来经写路径给 Memory 来源种 chunk，已改为先建普通来源再改类型、或直接 INSERT；断言不变。
- **E4-1b 其余 chunk 写路径的兜底与静态守卫**（sonnet，波次 2）：`kg_build_job_store.py`（双后端，暂存 chunk 的原子发布）、
  `knowhow_transfer_store.py`（双后端）、`migration/sync/import_.py`（批量导入）各加同一条拒绝；`sharing_store.py` 的拷贝归 E5-1。
  新守卫 `$R/backend/tests/test_memory_chunk_write_guard.py`：扫描两个后端所有写 `chunks` 表的语句，每一处要么经过
  `_refuse_memory_source`，要么登记为「上游只取可见来源」并给出取数函数名；新增未登记的写入点即红。
- **存量清理并入 E4-5 迁移**：删除 `source_type='memory'` 来源名下已有的 chunk 行及其向量/FTS 行（正常情况下为零行）；
  迁移测试夹具里种一条这样的行，迁移后不存在。派生表逐张点名：`chunk_embeddings`、`chunk_questions`（含 `question_indexed_at`）、
  `chunk_elements`、SQLite 的 `chunks_fts`（**没有外键级联**，必须显式删）。派生表的写入点（`embed_chunks_for_source`、
  问题索引 `question_index_chunk_page`、`backfill_fts`、`maintenance` 回填 `element_ids` / `chunk_elements`）不另加守卫：
  它们只挂在已存在的 chunk 行上，安全性依赖「库里不存在 Memory chunk」这条不变量，由本迁移与写侧守卫共同保证；
  迁移测试要断言这四张表在迁移后对 Memory 来源都是零行。
- **E4-1b 守卫的白名单**：整库镜像类路径（`migration/sqlite_to_postgres.py` 的 COPY、`migration/shadow/*` 的复制、
  `maintenance.py` 对已有行的 `UPDATE chunks SET element_ids`）登记为「只搬运/只改已有行」。
- **E4-1 规格评审（通过）留下的两处小修，随 E4-1 的质量评审意见一起改**：`test_memory_chunk_guard.py:196` 的 docstring 改为指向
  E4-5 迁移；PG 文件补一条与 `test_memory_confirmation_still_ingests_without_chunks` 对应的用例。

**E4-2 建图输入排除**（sonnet，波次 2）
- 文件：`$R/backend/app/repositories/postgres/unified_kg_store.py`、`$R/backend/app/repositories/sqlite/unified_kg_store.py`
  （`seed_payload_rows` :144 / :139、`stream_seed_rows` :154 / :149、`canonical_relation_seed_rows` :260 / :247、`mention_seed_rows` :280 / :265、
  `community_graph_rows` :333 / :327、`cluster_size_histogram` :1647 / :1603、`largest_clusters` :1710 / :1690、
  `relation_provenance_counts` :1861 / :1839、`community_rows_for_summary`、`incremental_cluster_rows` 若在此文件）、
  `$R/backend/app/repositories/postgres/search.py`（`mention_claim_rows` :991）、`$R/backend/app/repositories/postgres/knowledge_store.py`
  **不在本任务**（`community_context_rows` / `duplicate_seed_rows` 归 E4-4）；测试扩展 `$R/backend/tests/test_kg_analysis_precompute.py`
  （仿 982-1030 的模式）、`$R/backend/tests/test_canonical_relations.py`、`$R/backend/tests/test_mention_bridge.py`、
  `$R/backend/tests/test_rebuild_communities.py`、`$R/backend/tests/test_rebuild_streaming.py`。
- 做法：一律加 `NOT memory_derived_object(...)` / `NOT memory_derived_relation(...)`，先例是 `source_canonical_rows`
  （PG :2100-2170）。三个 `knowledge_lifecycle` 热函数（482/413/425 零余量）不动。
- 验收：含他人 Memory 的库重建后，簇名、簇描述、canonical id、规范关系计数、提及桥、社区成员名、最大簇分析与直方图都不含
  Memory 派生对象，且 Memory 对象不在任何簇里。变异：去掉种子读者的排除，「Memory 对象不在任何簇」必须红。

**E4-3 融合与治理写侧**（opus，波次 2）
- 文件：`$R/backend/app/services/knowledge_lifecycle.py`（`incremental_fuse_source` :2341，250 行无上限：来源为 Memory 时入口处
  直接返回，两个调用方——`run_extraction` :2739 与 `scale_index_builder.py:802`——同时覆盖；Tier-2 候选集排除 Memory）、
  `$R/backend/app/services/knowledge_governance.py`（`merge_knowledge` :2177 拒绝 Memory 对象与非 Memory 对象合并、拒绝不同主人的
  Memory 对象互并，`user_error(409, "个人记忆派生的知识对象不能与共享对象合并")`；冲突检测排除 Memory）、
  `$R/backend/app/repositories/postgres/governance_store.py`、`$R/backend/app/repositories/sqlite/governance_store.py`
  （`conflict_resolution_rows` :1127、`conflict_relation_rows` :1187 排除 Memory；`merge_objects_in_transaction` 在事务内复核同一条件，
  防止服务层判定与写入之间的竞态）；新建 `$R/backend/tests/test_memory_kg_isolation_write.py`，扩展
  `$R/backend/tests/test_incremental_fusion.py`、`$R/backend/tests/test_resolve_notebook_conflicts.py`。
- 验收：确认一条 Memory 后增量融合不把它放进任何共享簇、不产生合并候选；手工合并跨类返回 409，store 层直调同样拒绝；
  冲突候选不含 Memory 对象或关系；删除该 Memory 后共享对象一个不少（N-1 回归）。
- 变异：去掉入口早退，「不进共享簇」必须红；去掉 store 内复核，直调合并用例必须红。

**E4-5 迁移与重建**（opus，波次 2）
- 文件：新建 `$R/backend/app/repositories/postgres/migrations/0067_memory_kg_isolation.sql`；改 `$R/backend/app/repositories/postgres/schema_manifest.py`、
  `$R/backend/app/repositories/sqlite/migrations.py`（`_migration_87`、`SCHEMA_VERSION = 87`）、`$R/backend/app/services/kg_merge.py`（版本 4）、
  `$R/backend/app/services/startup_warmup.py`、`$R/backend/app/services/checkup.py`（只读项）；新建 `$R/backend/app/services/memory_isolation_rebuild.py`；
  重建成功置标记的 store 方法在 `unified_kg_store.py`——该文件本波归 E4-2，本任务在报告里给出一行改动，由 E4-2 落地；
  新测试 `$R/backend/tests/test_memory_isolation_migration.py`、`$R/backend/tests/postgres/test_memory_isolation_migration_pg.py`、
  `$R/backend/tests/test_memory_isolation_rebuild.py`；同步迁移 manifest / 快照夹具。
- 验收：按 §3.1/§3.2。升级库与新库都跑通；迁移重复执行无变化；夹具库里共享簇含 Memory 成员、canonical id 由 Memory 种子铸造、
  共享对象含 Memory 证据——迁移后三者都不存在，受影响库标记为 0；启动后重建完成标记为 1，事件齐全；重建失败下次启动重试。
- 变异：跳过整簇删除只删成员行，「canonical id 不再含 Memory 名」必须红；成功不置标记，「重建不重复入队」必须红。
- 红线：迁移只做确定性数据操作，不调模型；就绪前不跑重建。

**E4-6 可视化与 scale 索引**（sonnet，波次 2）
- 文件：`$R/backend/app/services/scale_index_builder.py`（`_derive_object_graph_lite` :1083 的关系输入、ANN 标签排除 Memory 对象）、
  `$R/backend/app/repositories/postgres/index_projection_store.py`、`$R/backend/app/repositories/sqlite/index_projection_store.py`
  （`active_object_graph_rows` :334 / :385）；扩展 `$R/backend/tests/test_viz_index_build.py`、`$R/backend/tests/test_kg_viz_index.py`。
- 验收：可视化工件与 ANN 标签里没有 Memory 对象、没有只由 Memory 关系支撑的边（邻居视图里「两个可见节点之间只由他人 Memory
  关系支撑的边」因此消失）。变异：去掉投影排除，工件用例必须红。

**E4-4 KG 读者（store）**（sonnet，波次 4，PR-E2 合入之后，避免与 E2-4 同改 `knowledge_store.py`）
- 文件：`$R/backend/app/repositories/postgres/knowledge_store.py`、`$R/backend/app/repositories/sqlite/knowledge_store.py`（`list_knowledge_page`
  :2055 / :1843、`type_counts` :2011 / :1786、`graph_node_rows` :2104 / :1891、`relations_for_notebook` :2112 / :1898、`unified_graph_rows`
  :1109 / :954、`fts_search` :3106 / :2947 及语义检索的水合/折叠读、`neighbor_relation_rows` :1117、`community_context_rows` :1143 / :987、
  `duplicate_seed_rows` :1783 / :1545 与 `duplicate_member_rows`）、`$R/backend/app/repositories/postgres/knowledge_counts_cache.py`、
  `$R/backend/app/repositories/sqlite/knowledge_counts_cache.py`、`$R/backend/app/repositories/postgres/search.py`（`notebook_knowledge_rows` :979）+
  `$R/backend/app/repositories/postgres/hotpath_indexes.py`（:271-283 的部分索引依赖该 SQL 文本，同步更新并保住 EXPLAIN pin）、
  `$R/backend/app/repositories/sqlite/query_store.py`（笔记本搜索内联 SQL :2204-2207）、`$R/backend/app/repositories/ports.py`（关键字）；
  改写 `$R/backend/tests/test_collection_enumeration.py:1120`（看板计数不再含他人 Memory）。
- 做法：读侧拼 `foreign_memory_*_excluded`（带 `viewer_id`）；治理与重复、社区摘要上下文这类共享工具拼 `memory_derived_*` 全排除；
  计数缓存只存共享口径（`type_status_counts` 排除 Memory 派生），本人 Memory 计数按本人 Memory 来源现场加一次有界查询；
  `notebook_copy_stats` 口径的分享预览大小同 E5-1。
- 验收：见 E4-8 的端到端矩阵；无外人 Memory 的库 SQL 文本与结果逐字不变（`viewer_id=None`）。
- 变异：`list_knowledge_page` 忽略 `viewer_id`，列表与总数用例必须红。

**E4-7 KG 读者（服务）**（sonnet，波次 4）
- 文件：`$R/backend/app/services/knowledge_query.py`（`list_knowledge`、`knowledge_types`、`graph`、`search` :179）、`$R/backend/app/services/knowledge_lifecycle.py`
  （`unified_graph` :4308：全量路径带谓词，过渡期标记为 0 的库不读旧工件）、`$R/backend/app/services/kg_viewer_scope.py`（§2 D2 的两处对齐 +
  孪生测试）、`$R/backend/app/services/knowledge_governance.py`（`pending_merges`、`pending_conflicts`、`find_duplicates`、`review_queue_page` 读侧）、
  `$R/backend/app/services/kg_analysis.py`、`$R/backend/app/services/notebook_catalog.py`（看板计数 :193、`shared_preview` :1021 的计数口径）；
  扩展 `$R/backend/tests/test_kg_viewer_scope.py`、`$R/backend/tests/postgres/test_kg_viewer_scope_pg.py`。
- 验收：服务层只在 `for_notebook` 非 None 时传 `viewer_id`；孪生测试双后端一致。变异：`evidence_hidden` 保留旧证据规则，混合证据夹具必须红。

**E4-8 路由与端到端**（sonnet，波次 4）
- 文件：`$R/backend/app/api/kg_routes.py`、`$R/backend/app/api/knowledge_routes.py`（只在服务签名变化处改），新建
  `$R/backend/tests/test_memory_kg_readers_e2e.py`（双后端）。
- 验收（成员 A 看成员 B 已确认 Memory 的共享库，经真实路由）：`GET /knowledge`（行、总数、证据里的 source_id）、`/knowledge-types`、
  `/graph`、`/unified-kg`（全量与工件两条路径）、`/kg/search`（FTS、ANN、折叠名）、`/unified-kg/pending-merges`、`/kg/conflicts/pending`
  （rationale、resolved_payload）、`/duplicates`、`/edge-review-queue`、`/kg-analysis`（看板成员名、最大簇、计数）、状态路由计数、
  `/objects/{id}/neighbors` 的边、HTTP `/search` 与 MCP `search_notebook_context` 的 KG 腿——都不出现 B 的 Memory 派生名、文字、
  id 或计数；B 自己浏览列表/图/搜索时看得到自己的 Memory 对象；无 Memory 的库响应字节不变。

**E4-Z**（sonnet）：`docs/product-and-api*.md` KG 各节（Memory 对象只属于本人、不进共享簇与派生产物；计数口径；合并 409 文案）；
`docs/operations*.md` 迁移与重建的确认步骤；`docs/development*.md` `memory_sql.py` 与读者守卫；`architecture.md` KG 流水线的隔离点；
`fangan_done.md` 按 `silicon_notebook_fangan.md` 的 Memory 章节记完成；新守卫 `test_memory_reader_guard.py` 在此任务落地
（此时所有读者都已接入）。

### PR-E5 拷贝与删除清理

**E5-2 删除路径**（opus，波次 1）
- 文件：`$R/backend/app/services/memory_service.py`（`delete` :801、`bulk_delete` :806：先 `self.memory_kg.remove_memory_source(id)` 再删行，
  照 `transfer` 的做法，:1094-1095、:1173；`transfer` 本身不动）、`$R/backend/app/services/notebook_sharing.py`（`remove_member` :1159-1195 /
  `leave_notebook` :1333、`kick_all_members` :1257-1270）、`$R/backend/app/repositories/postgres/memory_store.py`、
  `$R/backend/app/repositories/sqlite/memory_store.py`（列出 `(notebook_id, created_by)` 的 Memory id）、`$R/backend/app/repositories/postgres/governance_store.py`、
  `$R/backend/app/repositories/sqlite/governance_store.py`（撤回这些 Memory 处于 `proposed` 的晋升候选，否则 `promotion_candidates` 成孤儿）；
  新建 `$R/backend/tests/test_memory_purge.py`；改写 `$R/backend/tests/test_memory_promotion.py:472-541`（:533-538「重新加入后 Memory 回来」改为不再回来）、
  扩展 `$R/backend/tests/test_memory_api.py`（:629 起的硬删用例补派生行断言）。
- 规则：成员退出、被移除、被全员踢出时，**先**清 Memory 再删成员行（中途崩溃只会留下「已无 Memory 的成员」）；只在移除后该用户对
  该库**不再有读权**时清理（仍经群组授权可读的，没有真正离开）。群组授权撤销、取消共享**不清理**：权限常是临时的，Memory 保留但因
  D2 谓词对任何人不可见，恢复权限即恢复。已批准的晋升独立于 Memory（E8 给它公共库自有出处），不动。
- 验收：硬删、批量删、退出之后，该 Memory 的来源、元素、元素向量、KG 对象/关系/出现、簇成员行、`knowledge_object_sources` 全部为零，
  并标脏；被移除但仍有群组读权的成员 Memory 保留。变异：`delete` 不调 `remove_memory_source`，派生行断言必须红。

**E5-1 拷贝**（sonnet，波次 2，E0 合入后）
- 文件：`$R/backend/app/repositories/postgres/sharing_store.py`（`_COPY_SNAPSHOT_QUERIES` :120-217、`_COPY_VALIDATED_TABLES` :219-245）、
  `$R/backend/app/repositories/sqlite/sharing_store.py`（:153-279、:280-312）、`notebook_copy_stats`（两后端）；改写
  `$R/backend/tests/test_notebook_share_copy.py:487-520`、`:523-548`；新建 `$R/backend/tests/postgres/test_copy_memory_exclusion_pg.py`。
- 做法：全部在快照 SQL 里做（`copy_notebook` 640 行零余量，服务层零改动）：`sources`、`source_elements`、`element_embeddings`、`knowledge_objects`、
  事实三表排除 Memory 来源（仿 `_KNOWHOW_SOURCE_IDS`，PG :42）；`knowledge_relations` 按 `source_id` **且**两个端点都不是 Memory 对象排除
  （否则 `object_map[...]` 在 :798-799 KeyError）；`knowledge_embeddings` / `relation_embeddings` join 回对象/关系排除；`concept_clusters` 排除 Memory
  成员行与 canonical 为 Memory 对象的行；`_COPY_VALIDATED_TABLES` 用同一谓词计数（`validate_copy` 按同谓词比对）；`sources.memory_id` 清空逻辑
  （`notebook_sharing.py:388`）因无 Memory 行可清而成为空操作，保留不删。分享预览的节点/边数（C-7）随 `notebook_copy_stats` 同口径。
- 验收：深拷贝与分享链接拷贝的副本里，任何成员的 Memory、其元素、向量、KG 对象、关系、事实、簇成员都不存在；混合端点关系不导致
  拷贝失败；副本标脏以便重建成簇。变异：去掉关系端点条件，混合端点夹具必须 KeyError 红。

**E5-3 孤儿清扫**（sonnet，波次 3）
- 文件：新建 `$R/backend/app/services/memory_orphan_sweep.py`；改 `$R/backend/app/services/startup_warmup.py`（就绪后一步，同 §3.2 的位置）、
  `$R/backend/app/repositories/postgres/memory_store.py`、`$R/backend/app/repositories/sqlite/memory_store.py`（`orphan_memory_source_ids(limit)`：
  `source_type = 'memory' AND (memory_id IS NULL OR memory_id = '' OR NOT EXISTS (SELECT 1 FROM memory_items m WHERE m.id = s.memory_id AND m.status = 'confirmed'))`）、
  `$R/backend/app/services/checkup.py`（只读计数项）；新建 `$R/backend/tests/test_memory_orphan_sweep.py`。
- 验收：既有拷贝留下的无主 Memory 来源（N-5）与硬删残留在启动后被 `delete_source` 清掉；清扫幂等；checkup 显示剩余数。
  分页常量为命名协议常量，不改变结果。变异：条件漏掉 `memory_id IS NULL`，N-5 夹具必须红。

**E5-Z**：`docs/product-and-api*.md` 深拷贝一节（Memory 一律不带）与 Memory 一节（硬删/退出清理、授权撤销保留的理由）；
`docs/operations*.md` 孤儿清扫。

### PR-E6 挂载仅对挂载人生效

**E6-1 片段与契约**（opus，波次 1）
- 文件：`$R/backend/app/repositories/postgres/mount_sql.py`、`$R/backend/app/repositories/sqlite/mount_sql.py`（D3 新片段 + docstring 改写）、
  `$R/backend/tests/test_mount_sql_contract.py`。
- `test_mount_sql_contract.py` 新期望：旧片段仍恰好一个参数；`*_FOR_VIEWER` 恰好两个参数、顺序 `(viewer, notebook)`，错序绑定的探针用例
  返回空而非他库；矩阵——主人把自己的私有库 b 挂到已共享的 a：主人有效，成员 m 无效，m 另有 b 的读权（成员/授权/owner）则有效；
  `tier='base'` 与 `everyone` 授权对所有人有效；`a.created_by` 为 NULL 时只对能读 b 的人有效；空查看者只剩 base / everyone；借入支
  （第 4 支）与未共享门行为不变；`NOTEBOOK_LIVE_SQL` 仍挡住所有支。
- 变异：去掉 `v.uid = a.created_by` 支，「主人有效」必须红；把它换成 `true`，「成员无效」必须红。

**E6-1 落地记录（提交 d9688ba6，分支 `claude/mount-viewer-scope`）**
- 公开名：`MOUNT_VIEWER_JOIN`（两个参数，顺序 `(viewer_id, notebook_id)`）、`MOUNT_EFFECTIVE_FOR_VIEWER_EXPR`（零参数，可当布尔列）、
  `MOUNT_EFFECTIVE_FOR_VIEWER`、`MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY`。读权支直接复用 `access_sql.read_access_clause`。
- **挂载人支被「MOUNT_VALID ∧ 读权支」完全蕴含**（边成立时挂载人自己必能读被挂库），保留它只为短路后面的 EXISTS；所以上面
  「去掉挂载人支，主人有效必须红」是等价变异，行为上杀不死，只由文本派生断言守住。M3 的实际语义即：挂载只对**自己能读被挂库**的
  查看者生效。
- 查看者值先 `NULLIF(…,'')`，空串与 None 同为无查看者（只剩 base / everyone）。`notebooks.created_by` 可空且无回填，NULL 挂载人时只对
  能读被挂库的人生效。
- **E6-2 要切换的 store 调用点**（PG / SQLite）：`NotebookStore.resolve_participants`（`notebook_store.py:88` / `:81`）、`participant_rows`
  （`:113` / `:100`）、`QueryStore.notebook_has_usable_base_kg`（`query_store.py:315` / `:375`）、`mounted_bases_row`（`:447` / `:565`）、
  `UnifiedKgStore.mounted_base_ids`（`unified_kg_store.py:1422` / `:1338`）、`KnowledgeStore.any_mounted_has_kg_on`（`knowledge_store.py:1177` / `:1021`）、
  `follow_start_row`（`:1653` / `:1423`）。保持不变：`list_mount_edges`、`mountable_notebooks`、`valid_copied_mount_base_ids`、`mounted_public_base_ids`。
- **E6-3 的查看者来源**：报告、全局问答、离开后接回的运行在工作线程上执行，查看者取检索运行的 `actor_id`，不取 HTTP 请求上下文。
  后台路径经 `get_notebook` → `NotebookSummaryQuery.get` 间接求值 `mounted_bases_row` / `notebook_has_usable_base_kg`，E6-3 逐一确认
  各调用方读不读这两个字段。`services/communities.py:311` 的模块级 `mounted_base_ids` 无调用方，E6-3 删除。
- `test_notebook_update_authorization_free.py` 按名字点名谓词常量，E6-2 把新名字加进名单。
- 请求路径调用点的完整清单见实现报告，E6-3 动手前用 grep 按函数名重新核对（其它 PR 合入后行号会变）。

**E6-2 store 与端口**（sonnet，波次 5）
- 文件：`$R/backend/app/repositories/postgres/notebook_store.py`、`$R/backend/app/repositories/sqlite/notebook_store.py`、
  `$R/backend/app/repositories/postgres/query_store.py`、`$R/backend/app/repositories/sqlite/query_store.py`、`$R/backend/app/repositories/postgres/unified_kg_store.py`、
  `$R/backend/app/repositories/sqlite/unified_kg_store.py`、`$R/backend/app/repositories/postgres/knowledge_store.py`、`$R/backend/app/repositories/sqlite/knowledge_store.py`
  （D3 列出的消费点换 `*_FOR_VIEWER` 片段，`viewer_id` 必填关键字）、`$R/backend/app/repositories/ports.py`；PG conformance。
- 验收：store 层矩阵同 E6-1，经真实表。

**E6-3 服务调用点与缓存**（opus，波次 5）
- 文件：`$R/backend/app/services/retrieval_run.py`（`current_viewer_id`）、`$R/backend/app/services/repository_runtime.py`（三处 wiring）、
  `$R/backend/app/services/retrieval_candidates.py`（memo 键）、`$R/backend/app/services/graph_retrieval.py`（缓存键）、`$R/backend/app/services/notebook_catalog.py`
  （摘要按查看者）、`$R/backend/app/services/communities.py`，以及仍直接调用 store 参与者方法的 `source_routes.py`、`mcp_tools/citations.py`、
  `knowledge_query.py`、`knowledge_lifecycle.py`、`plugin_ask_engine.py`、`evidence_context.py`、`collection_catalog.py`、`collection_enumeration.py`
  （全部在 `$R/backend/app/` 下）；新守卫 `$R/backend/tests/test_mount_viewer_guard.py`，同步 `$R/backend/tests/test_participant_override_guard.py` 白名单；
  新建 `$R/backend/tests/test_mount_viewer_e2e.py`。
- 验收（经真实路由，双后端）：主人把私有库挂到共享库，成员提问时该库对所有通道零贡献，成员经来源代理 `/sources/{id}` 与 MCP 取证读该库
  来源一律 404，摘要里看不到它的名字与 KG 标志（N-6）；主人一切照旧；成员另获 b 的读权后生效；两人参与集相同时共用图缓存（缓存命中计数）。
- 变异：wiring 退回无查看者，成员用例必须红；缓存键不含有效集合指纹，「成员不共用主人含私有库的图」必须红。
- 红线：后台无 actor 路径失败即关（只剩 base / everyone）；`list_mount_edges` / `mountable_notebooks` 不变。

**E6-Z**：`docs/product-and-api*.md`「读权 ⇒ 可挂载」一节加 M3 与 N-6；`docs/development*.md` 的 `mount_sql` 条目改为「随查看者」；
`architecture.md` 参与集解析。

### PR-E7 回答级与分享

**E7-3 报告分享确认（前端）**（sonnet，波次 1；按 §6 契约先写，后端 E7-2 落地后联调）
- 文件：`$R/frontend/app/report-view.tsx`（:1472-1483 分享按钮）、`$R/frontend/app/use-report-workspace.ts`（:541-566 `toggleShare`）、
  `$R/frontend/app/report-api.ts`；新建 `$R/frontend/app/report-share-confirm.tsx`；新建 `$R/frontend/tests/component/report-share-confirm.component.test.tsx`。
- 验收见 §6。

**E7-2 报告分享披露（后端）**（opus，波次 2）
- 文件：新建 `$R/backend/app/services/share_disclosure.py`；改 `$R/backend/app/api/report_routes.py`（share :595-642）、
  `$R/backend/app/repositories/postgres/memory_store.py`、`$R/backend/app/repositories/sqlite/memory_store.py`（`memory_ids_for_source_ids(source_ids, owner_id)`，
  用 `memory_source_readable`）；新建 `$R/backend/tests/test_report_share_disclosure.py`。验收见 §6。

**E7-4 回答 id 接口校验会话属主**（sonnet，波次 2）
- 文件：`$R/backend/app/api/memory_routes.py`（:396-423、:479-514、:517-545）、`$R/backend/app/services/memory_service.py`（`create_from_answer` :648-705）、
  `$R/backend/app/services/notebook_sharing.py`（新增 `user_owns_answer`：库读权 ∧ 会话属主）、`$R/backend/app/repositories/postgres/ask_state_store.py`、
  `$R/backend/app/repositories/sqlite/ask_state_store.py`（`answer_conversation_owner`：`answers ⋈ conversations`，无会话或无创建者返回 None）、
  `$R/backend/app/api/ask_routes.py`（`feedback` :1338-1347）；改写 `$R/backend/tests/test_memory_api.py:158-205`；新建 `$R/backend/tests/test_answer_ownership.py`。
- 验收：他人回答、无会话回答调 `memory-preview`、`memory-preview/stream`、`from-answer`、`feedback` 一律 404；本人回答照旧。
  `answer_owner`（`sharing_store.py:585-592`，返回的是库主人）不作授权用，保持原名原义并在 docstring 写明。
- 变异：`user_owns_answer` 退化为只查库读权，他人回答用例必须红。

**E7-5 会话分享与公开页（后端）**（opus，波次 4，PR-D 与 E1 合入后）
- 文件：`$R/backend/app/services/share_disclosure.py`（会话计数）、`$R/backend/app/api/ask_routes.py`（会话分享端点 :1052-1152、公开会话 :1178-1231 的
  挂载复核）、`$R/backend/app/api/global_ask_routes.py`（:292-340）、`$R/backend/app/services/global_ask.py`（D-2：:456-502 每轮复核集合恒并入
  `resolved_notebook_ids`；分享计数入口）、`$R/backend/app/api/report_routes.py`（公开报告 :645-687 的挂载复核）；测试扩展
  `$R/backend/tests/test_conversation_share_api.py`、`$R/backend/tests/test_global_ask_share_api.py`，新建 `$R/backend/tests/test_public_page_mount_recheck.py`。
- D-3 做法：公开页每次打开，以分享创建者为查看者，对引用涉及的非本库做挂载有效性复核（E6 合入前用现有谓词，合入后自动变为按查看者）；
  报告引用只存 `source_id` / `object_id`，经一次批量 `source → notebook` 查询取库；任一库失效时的行为与全局分支一致。
- 验收：全局会话某轮引用库 A、转述了未引用但检索过的库 B，撤掉分享者对 B 的读权后公开页不可读（D-2）；笔记本会话/报告引用的挂载库
  失效后公开页同样处理（D-3）；M4 见 §6。

**E7-6 会话分享弹窗与公开页标签（前端）**（sonnet，波次 4）
- 文件：`$R/frontend/app/conversation-share-disclosure.ts`、`$R/frontend/app/conversation-share-modal.tsx`、`$R/frontend/app/conversation-share-api.ts`、
  `$R/frontend/app/global-ask-api.ts`、`$R/frontend/app/c/[token]/page.tsx`（:497-518）、`$R/frontend/app/r/[token]/page.tsx`（:192-205）；测试
  `$R/frontend/tests/unit/conversation-share-disclosure.test.mjs`、`$R/frontend/tests/component/conversation-share-modal.component.test.tsx`、
  `$R/frontend/tests/component/global-ask-share.component.test.tsx`、`$R/frontend/tests/component/public-report-page.component.test.tsx`。验收见 §6。

**E7-Z**：`docs/product-and-api*.md` 报告公开分享护栏与会话公开分享护栏（服务端披露、`share_disclosure_required`、非作者不得公开含作者 Memory
的报告）、回答 id 接口属主规则、公开页复核挂载；「管理员用户活动日志」一节写明部署管理员查看用户回答全文属审计权限。

### PR-E8 晋升对象的公共库自有出处（B-12）

**E8-1**（opus，波次 4，E2、E4 合入后）
- 目标：晋升对象携带公共库自己的出处，使它在任何天花板下都凭公共库的可见来源被召回和引用。
- 设计：晋升批准时，在公共库里为每个「来源原件」建立（幂等复用）一个合成来源 `source_type = 'promotion'`，标题「晋升自：<原标题>」
  （Memory 晋升为「晋升自个人记忆：<Memory 标题>」——批准即发布，属预期）；每条证据写一个 `source_elements` 行，文本取同一事务里读得到的
  原元素现文，否则取 `quoted_span`；证据条目改指这个来源与元素，原 `source_id` / 库 id 只作展示键保留（`origin_source_id`、`origin_notebook_id`）；
  合并进既有公共对象时只改写新进来的条目；`replace_object_sources` 按新来源写。`promotion` 加入两后端 `VISIBLE_SOURCE_TYPES_PREDICATE` 的可见集合
  （进挂载库「只开放可见来源」的天花板）；reparse 路由拒绝该类型；删除该来源按既有级联删去它支撑的晋升对象。
- 存量：迁移 PG `0068_promotion_provenance.sql` / SQLite `_migration_88`（号码按 §0 复核）——对公共库全部对象找出
  `evidence[].source_id NOT IN (本库来源)` 的外库条目（不只 `source_candidate_id` 非空的行：合并的对象保留原 id），按上面规则改写：原元素还在取现文，
  否则取 `quoted_span`，标题取 JSON 里的 `source_title`；没有 `quoted_span` 的条目丢弃，对象因此无证据时在读取时按天花板被丢弃（失败即关）。
  改写后条目不再是外库条目，天然幂等。
- 文件：`$R/backend/app/repositories/postgres/governance_store.py`、`$R/backend/app/repositories/sqlite/governance_store.py`（`approve_promotion_in_transaction`
  PG :1549-1682 / :1387，`approve_memory_promotion_in_transaction` PG :1424-1547 / :1273）、`$R/backend/app/repositories/postgres/source_store.py`、
  `$R/backend/app/repositories/sqlite/source_store.py`（可见类型）、`$R/backend/app/api/source_routes.py`（reparse 拒绝）、`$R/backend/app/services/evidence_context.py`
  （引用卡展示原件标题）、新迁移与 `schema_manifest.py`、`$R/backend/app/repositories/sqlite/migrations.py`；改写 `$R/backend/tests/test_memory_promotion.py`（:598、:614 证据内容）、
  扩展 `$R/backend/tests/test_trackF_governance_promotion.py`、`$R/backend/tests/test_multi_domain_bases.py`、PG conformance；新建
  `$R/backend/tests/test_promotion_provenance.py` 与迁移测试。
- 验收：公共库挂到个人库，单库问答与全局问答都能召回并引用晋升对象，引用卡打开的是公共库的「晋升自」来源；推广者私有库删除后晋升对象照常可用；
  迁移前后双后端一致。变异：可见类型漏加 `promotion`，「挂载库单库问答召回晋升对象」必须红。
- **E8-Z**：`docs/product-and-api*.md` 晋升一节；`docs/operations*.md` 迁移确认；`docs/ui-vocabulary.md` 来源类型中文标签。

### 并入 PR-B（`claude/enumeration-source-ceiling` 已接手 E-2～E-6；运行中的简报未含的，补以下内容）

- **E-2**：`ask_service.py:4869-4873` 的闸改成单行 `and not (source_scope_restricted() or subjectless_run_active())`（`_run_reasoning_stage` 515 行零余量，
  净增 0 行），落到既有 `elif`（:4922）置 `completeness_unavailable=True`。测试：对等模式下 Knowhow 存储替身零调用，单库对照照常枚举。
  注意 `ask_service.py` 被 PR-C 占用，按合入顺序 rebase。
- **E-3**：补一条回归钉（代码已正确）——逐库天花板冻结后上传来源，`enumerate_sources` 与集合地图的 `sources:` 都不含它（SQLite + PG twin）。
  B2 必须在 `scope_element_plan` 内部过滤：`resolve_source_title`（:1078）读这份计划，界外标题否则仍解析出 id；加「界外标题解析为 `("", 0, False)`」。
- **E-4**：`_explicit_source_plan` 对 `not source_allowed(owner_notebook_id, source_id)` 抛与非成员同一句 `ValueError`（防探测存在性）；测试覆盖 include 收窄
  与逐库天花板两种形态，以及 `fail_closed` 抛出。
- **E-5(a)**：对等模式隐藏 `current_notebook` 选项——`reflect` 在 `subjectless_run_active()` 时强制 `ENUMERATE_SCOPE_ALL`（~4026）；
  `_try_document_overview:3437` 加 `and not subjectless_run_active()`（PR-C 文件）；`reflect_prompt` / `reflect_schema_hint` 在对等模式去掉该枚举与句子，
  `render_collection_map` 去掉「(current notebook: N)」；`enumerate_sources` 在对等模式忽略 `local_only`。测试：对等模式解析、提示词字节、概览替身、单库对照。
- **E-5(b)**：`_scope_knowhow_tables`（`collection_catalog.py:881-895`）只数当前库，在 restricted 或 subjectless 时为 0（双后端，`count_rows` 共用）。
- **E-6**：`reasoning_retrieval.py:4740-4744` 在 `subjectless_run_active()` 时不注入锚点库画像；测试对照 6159 行的元素补搜守卫。

## 6. M4 分享披露设计

**原则**：在现有会话分享披露上补三个缺口——计数漏了 Memory 投影命中、只在前端计数、报告分享没有提示；不另起第二套机制。

**计数（后端唯一定义，`services/share_disclosure.py`）**
- 输入：待公开范围内每轮回答的 `AskResponse` 载荷（`answers.payload`；全局问答为 `global_ask_jobs.payload.answer`，旧行 `.response`），或报告的 `references`。
- 规则：去重后的 Memory 条目数 = 引用 `memory_id` ∪ 锚点里 `object_type == 'memory'` 的 `object_id` ∪ 引用/锚点/报告引用里 `source_id` 指向作者本人 Memory 来源的，
  经 `memory_ids_for_source_ids(source_ids, author_id)` 一次批量映射成 Memory id（补上 Memory 投影的元素/KG 命中）；报告再加 `object_type == 'memory'` 的
  `object_id`。按 Memory id 去重，数的是「几条不同的个人记忆」。
- 范围与会话公开快照同一条 keyset（`(created_at, rowid/ordinal)`，截至 `expected_through_id`）；另给 `new_memory_count`（水位之后新增的）。

**端点**
- 报告：`GET /notebooks/{nb}/reports/{rid}/share/disclosure` → `{memory_count}`；`POST .../share` body `{acknowledged_memory_count?: int}`。
- 笔记本会话：`GET /notebooks/{nb}/conversations/{cid}/share/disclosure?through_id=` → `{memory_count, new_memory_count}`；`POST .../share` body 增
  `acknowledged_memory_count`（与既有 `expected_through_id` 并列）。
- 全局会话：`/global-ask/conversations/{cid}/share/disclosure` 与 `POST .../share` 同形。
- 服务端按**即将公开的确切范围**重算：`memory_count > 0` 且 `acknowledged_memory_count != memory_count` → 409，
  `{"code": "share_disclosure_required", "memory_count": N, "new_memory_count": M}`，不发 token、不推进水位；等于 0 时不要求确认（今天的流程字节不变）。
- 报告由非作者（有写权限的成员）分享且 `memory_count > 0` → 403「报告引用了作者本人的个人记忆，只有作者可以公开分享」（N-7；M4 规定由作者决定）。
- 已公开之后：会话受水位约束，新轮次只能经带确认的 POST 推进水位；报告只有 `done` 才能分享，`done` 不能重新生成（`report_routes.py:499-512`），
  内容在分享时就固定了。所以「已公开的分享事后多出 Memory 引用」这种情形不存在，不需要回溯处理。

**前端**
- 会话弹窗：`summarizeShareDisclosure` 保留附图计数，Memory 数改用 disclosure 端点返回值（前端不再自己数 Memory，只剩一处定义）。披露文案沿用：
  「公开页会包含 {N} 条你引用到的个人记忆摘录。」/「更新后公开页共 {N} 条你引用到的个人记忆摘录（新增 {M} 条）。」；取数失败时沿用
  `SHARE_DISCLOSURE_COUNTS_ERROR` 系列文案，此时 POST 不带确认值，服务端 409 带回确数，弹窗把披露行换成确数，并把按钮文字改为「确认公开」。
  「Memory 披露绝不省略」的单测改为钉服务端数字的呈现与 409 路径。
- 报告：按「分享」时先取 disclosure。N = 0 → 直接分享（今天的行为）；N > 0 → 按钮旁就地展开确认条（不放页顶横幅）：
  「公开页会包含 {N} 条你引用到的个人记忆摘录。」+「确认公开」+「取消」。英文（文档用）："The public page will include {N} excerpts from your personal memories." /
  "Publish anyway" / "Cancel"。按钮状态：按下走全局 `:active` 基线；请求在飞时「生成链接中…」并禁用；成功时结果落在按钮上（沿用 `useCopyResult` 分格：
  「已公开，链接已复制」保持 1.6 s）；409 时确认条就地更新为服务端确数；403 时确认条就地显示那句中文原因。toast 保留，但不是唯一的反馈。
- 公开页（`/c/{token}`、`/r/{token}`）：Memory 引用照常显示标题与摘录，位置标签统一显示「作者的个人记忆」（按 `docs/ui-vocabulary.md`「Memory → 记忆」），
  让读者知道这不是文档原文。

**理由**：确认值绑在 POST 上，作者看到的数字就是服务端即将公开的数字，披露与发布之间没有竞态；零 Memory 时不加任何步骤。

**E7-3 落地后定稿的契约细节（E7-2 / E7-5 后端按此实现，前端提交 79bde950）**
- 409 的结构化内容放在 `detail` 之下：`{"detail": {"code": "share_disclosure_required", "memory_count": N, "new_memory_count": M}}`，
  与仓库其它结构化错误同形。前端解析器同时接受根级形态，但后端只发 `detail` 形态。
- 403 用 `user_error()` 抛出（带 `X-User-Message` 头），中文句子由服务端给出；没有这个头时前端只能显示通用 403 文案。
- 无确认值的 POST **不带请求体**（与今天逐字节相同）；后端不得因缺 body 返回 422。
- 发布成功但复制失败时，按钮上显示「已公开，复制失败」。
- 确认条放在报告标题行正下方、右对齐的独立一行（操作行是不换行的 flex，内联会挤压按钮）；403 时只留「取消」。

## 7. 并行执行编排

每波同一文件只归一个任务；跨波、跨 PR 同文件按 §4 的合入顺序 rebase。`docs/product-and-api*.md`、`docs/development*.md`、`docs/operations*.md`、
`architecture.md`、`fangan_*.md`、`scripts/architecture_boundary_baseline.json`、`ports.py` **只由各 PR 的 Z 任务改**，Z 任务按合入顺序串行
（同一时刻至多一个 Z 任务持有文档）。波内任务需要的端口关键字写进报告，由本 PR 的 Z 任务或本波指定的端口持有者落地。

**波次 1**（现在即可开始；避开 §0 在途文件）

| 任务 | PR | 文件集 |
| --- | --- | --- |
| E0-1 | E0 | 两个 `memory_sql.py`（新）、两个 `query_store.py`、`test_memory_sql_contract.py`、`postgres/test_memory_sql_contract_pg.py` |
| E1-1 | E1 | `services/source_scope.py`、`test_default_source_ceiling.py` |
| E6-1 | E6 | 两个 `mount_sql.py`、`test_mount_sql_contract.py` |
| E5-2 | E5 | `memory_service.py`、`notebook_sharing.py`、两个 `memory_store.py`、两个 `governance_store.py`、`test_memory_purge.py`、`test_memory_promotion.py`、`test_memory_api.py` |
| E4-1 | E4 | 两个 `chunk_store.py`、`source_chunking.py`、`test_memory_chunk_guard.py` |
| E7-3 | E7 | `report-view.tsx`、`use-report-workspace.ts`、`report-api.ts`、`report-share-confirm.tsx`（新）、对应组件测试 |

不相交性：六组无共享文件；`test_memory_api.py` 只归 E5-2。

**波次 2**（前提：PR-E0 已合入、PR-A 已合入）

| 任务 | PR | 文件集 |
| --- | --- | --- |
| E5-1 | E5 | 两个 `sharing_store.py`、`test_notebook_share_copy.py`、`postgres/test_copy_memory_exclusion_pg.py` |
| E4-2 | E4 | 两个 `unified_kg_store.py`、`postgres/search.py`、KG 派生层测试五个（见 E4-2） |
| E4-3 | E4 | `knowledge_lifecycle.py`、`knowledge_governance.py`、两个 `governance_store.py`、`test_memory_kg_isolation_write.py`、`test_incremental_fusion.py`、`test_resolve_notebook_conflicts.py` |
| E4-5 | E4 | `0067_*.sql`（新）、`schema_manifest.py`、`sqlite/migrations.py`、`kg_merge.py`、`startup_warmup.py`、`checkup.py`、`memory_isolation_rebuild.py`（新）、三个迁移/重建测试 |
| E4-6 | E4 | `scale_index_builder.py`、两个 `index_projection_store.py`、`test_viz_index_build.py`、`test_kg_viz_index.py` |
| E1-3 | E1 | `mcp_tools/memory_context.py`、`report_execution.py`、`memory_retrieval.py`、`test_memory_mcp.py`、`test_report_api.py` |
| E7-2 | E7 | `share_disclosure.py`（新）、`report_routes.py`、两个 `memory_store.py`、`test_report_share_disclosure.py` |
| E7-4 | E7 | `memory_routes.py`、`memory_service.py`、`notebook_sharing.py`、两个 `ask_state_store.py`、`ask_routes.py`、`test_memory_api.py`、`test_answer_ownership.py` |

共享文件裁定：`unified_kg_store.py` 归 E4-2（E4-5 需要的「成功置标记」一行由 E4-2 代写）；`memory_store.py` 归 E7-2；`memory_service.py`、
`notebook_sharing.py`、`test_memory_api.py` 归 E7-4（E5-2 已在波次 1 完成）；`test_report_api.py` 归 E1-3；E7-2 的报告测试只放新文件。

**波次 3**（前提：PR-A、PR-B、PR-C、PR-D 均已合入）

| 任务 | PR | 文件集 |
| --- | --- | --- |
| E1-2 | E1 | `ask_service.py`、`ask_routes.py`、`global_run.py`、`test_default_ceiling_guard.py`（新），以及 E1-2 列出的改写测试 |
| E2-1 | E2 | `retrieval_service.py`、`reasoning_retrieval.py`、两个 `unified_kg_store.py`、`test_retrieval_leg_ceilings.py` |
| E2-2 | E2 | `retrieval_candidates.py`、`exact_lookup.py`、`plugin_ask_engine.py`、`test_source_scope.py`、`test_peer_mode_exact_arm.py`、`test_federated_global_budget.py` |
| E2-3 | E2 | `graph_retrieval.py`、两个 `chunk_store.py`、PPR 测试 |
| E2-4 | E2 | 两个 `knowledge_store.py`、`postgres/search.py`、`evidence_context.py`、`ports.py`、`test_evidence_context_service.py`、`test_canonical_relations.py`、PG conformance |
| E3-1 | E3 | `source_routes.py`、`notebook_sharing.py`、两个 `sharing_store.py`、两个 `source_store.py`、`mcp_tools/citations.py`、`catalog_routes.py`、`test_memory_mcp.py`、`test_multi_domain_bases.py`、`test_memory_source_endpoints.py` |
| E5-3 | E5 | `memory_orphan_sweep.py`（新）、`startup_warmup.py`、两个 `memory_store.py`、`checkup.py`、`test_memory_orphan_sweep.py` |

共享文件裁定：`test_source_scope.py` 同时被 E1-2（:61/:742/:803）与 E2-2（:994）需要——归 E2-2，E1-2 的三处改写写进新文件
`test_default_ceiling_entrypoints.py`，原文件里的三条由 E2-2 按 E1-2 的报告一并调整；`test_chunk_retrieval_characterization.py` 归 E1-2，
E2-2 的 :1074-1082 替身改写放到波次 4 的 E2-Z；`ports.py` 归 E2-4，E3-1 需要的 `source_notebook_id(viewer_id=)` 关键字由 E3-Z 在波次 4 落地。

**波次 4**（前提：PR-E2 已合入；E7-5/E7-6 还需 PR-E1 已合入）

| 任务 | PR | 文件集 |
| --- | --- | --- |
| E4-4 | E4 | 两个 `knowledge_store.py`、两个 `knowledge_counts_cache.py`、`postgres/search.py`、`postgres/hotpath_indexes.py`、`sqlite/query_store.py`、`ports.py`、`test_collection_enumeration.py` |
| E4-7 | E4 | `knowledge_query.py`、`knowledge_lifecycle.py`、`kg_viewer_scope.py`、`knowledge_governance.py`、`kg_analysis.py`、`notebook_catalog.py`、两个 viewer-scope 测试 |
| E4-8 | E4 | `kg_routes.py`、`knowledge_routes.py`、`test_memory_kg_readers_e2e.py` |
| E7-5 | E7 | `share_disclosure.py`、`ask_routes.py`、`global_ask_routes.py`、`global_ask.py`、`report_routes.py`、三个分享/公开页测试 |
| E7-6 | E7 | 会话分享前端六个文件与四个测试（见 E7-6） |
| E8-1 | E8 | 两个 `governance_store.py`、两个 `source_store.py`、`source_routes.py`、`evidence_context.py`、`0068_*.sql`（新）、`schema_manifest.py`、`sqlite/migrations.py`、晋升测试 |

共享文件裁定：`ports.py` 归 E4-4（E3-Z 的关键字改由 E4-4 顺带落地，写进 E4 与 E3 两个 PR 的说明里；若 E3 先合入，则由 E3-Z 在波次 3 末尾单独落地，不与 E2-4 同时进行）。
E8-1 的 `schema_manifest.py` / `sqlite/migrations.py` 与 E4-5 不同波；E8 在 E4 合入后 rebase。

**波次 5**（前提：PR-E2、E3、E4、E7、E8 已合入）

| 任务 | PR | 文件集 |
| --- | --- | --- |
| E6-2 | E6 | 两个 `notebook_store.py`、两个 `query_store.py`、两个 `unified_kg_store.py`、两个 `knowledge_store.py`、`ports.py` |
| E6-3 | E6 | `retrieval_run.py`、`repository_runtime.py`、`retrieval_candidates.py`、`graph_retrieval.py`、`notebook_catalog.py`、`communities.py`、`source_routes.py`、`mcp_tools/citations.py`、`knowledge_query.py`、`knowledge_lifecycle.py`、`plugin_ask_engine.py`、`evidence_context.py`、`collection_catalog.py`、`collection_enumeration.py`、`test_mount_viewer_guard.py`、`test_participant_override_guard.py`、`test_mount_viewer_e2e.py` |

每波并发数：6 / 8 / 7 / 6 / 2，Z 任务另计、串行。波次 5 只有两个任务，因为 E6 的调用点散在前面各 PR 的文件里，必须等它们合入。

## 8. 风险、回滚与验证门

每个 PR 的门禁：rebase 到最新 master → `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh` →
PG 泳道 `scripts/check_postgres.sh`（本机一次性测试库；动迁移的 PR 必须整条跑）→ 改了前端的 PR 在 check.sh 里跑前端单测、组件测试与构建
（新 worktree 先补 `frontend/node_modules` 软链）→ push → codex 评审闭环（成立的 finding 修到没有新问题）→ 三个 check 按 head SHA 全绿 →
`verify` → `gh pr merge --rebase`。

| PR | 主要风险 | 回滚 |
| --- | --- | --- |
| E0 | 片段与旧常量不等价，改变既有查询结果 | 纯重构，revert 即可 |
| E1 | 无范围运行多两次读；逐库天花板让挂载库 KG 腿从 `source_filter is None` 改走 `IN` 列表（`knowhow_on` 在 :1975 变为 False）；漏掉的安装点失败即放行 | 守卫防漏装；PG 上对挂载库 KG 腿做 EXPLAIN pin 与耗时对照（以全局运行同形为基线）；revert 回到今天的行为（漏洞重新打开，只作应急） |
| E2 | 召回变化（精确查找在收窄时恢复、PPR 名额重排）；新 SQL 的计划 | EXPLAIN pin；黄金 fixture 不变；按任务粒度 revert |
| E3 | 误伤同库合法读取（Knowhow） | 矩阵覆盖 Knowhow 仍 200；revert |
| E4 | 迁移删整簇后到重建完成前，共享成员暂成单例（召回暂时下降）；重建占资源；过渡期可视化降级 | 迁移不可逆（按仓库规则，恢复用升级前备份）；上线前在生产副本上演练并记录重建耗时；重建逐库、单线程、可中断重跑 |
| E5 | 退出时清理不可恢复 | 只在失去读权时清；先清后删成员行；revert 只能停止后续清理 |
| E6 | 参与集随人变化，缓存命中率下降；后台路径失败即关导致主人某些后台功能看不到私有挂载 | 缓存键只在集合不同时分叉；后台路径清单在 E6-3 报告里逐一列出；revert |
| E7 | 分享多一步确认；409 路径处理不当导致无法分享 | 零 Memory 时流程不变；组件测试覆盖 409/403；revert |
| E8 | 公共库来源列表出现「晋升自」来源（产品可见变化）；迁移改写证据 | 迁移前备份；revert 代码后已改写的数据仍可读（只是多出可见来源） |

## 9. 判定为范围外

- **E-7 运行中途撤权的时间窗**：撤权在段落回执（`global_ask.py:1189`）、引擎返回后（:1799）与发布前（:1836）三处被复核，进度流在 :1391 关闭。
  发布前的复核让整份作业失败，窗口内读到的任何内容都到不了用户，也进不了分享。所以它不影响本特性的效果，维持现状，不改代码、不登记。
- **已公开答案载荷里的 Memory 摘录，在作者事后删除该 Memory 后是否下架**：M4 规定的是分享时由作者决定；硬删除的清理范围（主 agent 定）是
  元素、向量、KG、簇成员与缓存，回答是作者自己的记录，不属派生行。作者可以撤销分享。
- **`sharing_store.answer_owner` 的命名**（返回库主人）：不作授权用途，E7-4 在 docstring 写明，不改名。

## 10. 发现 → 任务对照表

| 发现 | 去向 | 发现 | 去向 |
| --- | --- | --- | --- |
| A-1 | E1-2 | C-3 | E3-1 |
| A-2 | 台账勘误（E1-3 回归钉） | C-4 | E4-2 |
| A-3 | E1-Z（文档） | C-5 | E2-2 |
| A-4 | E4-1 | C-6 | E4-3（读侧 E4-7） |
| B-1 | E1-2 | C-7 | E4-4（分享预览 E5-1） |
| B-2 | E2-3 | C-8 | E5-1 |
| B-3 | E2-1 | C-9 | E5-2（账号删除子项：台账勘误） |
| B-4 | E2-1（挂载库部分：台账勘误） | C-10 | E4-5 |
| B-5 | E2-2 | C-11 | E1-3 |
| B-6 | E2-4 | D-1 | E7-4 |
| B-7 | E1-2 | D-2 | E7-5 |
| B-8 | 已在执行：PR-A store 修复轮（`claude/scope-ceiling-remediation`） | D-3 | E7-5 |
| B-9 | E2-4（服务侧闸在 E2-2） | D-4 | E7-2（会话侧 E7-5/E7-6，报告前端 E7-3） |
| B-10 | E2-2 | D-5 | E3-1（catalog 半）/ E2-2（插件半） |
| B-11 | E2-4 | E-1 | E1-1 |
| B-12 | E8-1 | E-2 | 已在执行：PR-B（`claude/enumeration-source-ceiling`），补充见「并入 PR-B」 |
| C-1 | E4-4（服务 E4-7，路由 E4-8） | E-3 | 已在执行：PR-B（台账部分勘误），补充见「并入 PR-B」 |
| C-2 | E4-4 | E-4 | 已在执行：PR-B，补充见「并入 PR-B」 |
| N-1 | E4-3 + E4-5 | E-5 | 已在执行：PR-B，补充见「并入 PR-B」 |
| N-2 | E4-3 | E-6 | 已在执行：PR-B，补充见「并入 PR-B」 |
| N-3 | E4-4 + E3-1 | E-7 | 判定为范围外 |
| N-4 | E2-4 | N-5 | E5-3 |
| N-6 | E6-2 | N-7 | E7-2 |

## 11. 待用户确认（给出推荐默认值，不阻塞）

- **晋升出处的呈现（E8-1）**：推荐默认值——公共库的来源列表里显示「晋升自：<原标题>」合成来源（按原件聚合，只读，不可重新解析，删除它即删除它支撑的晋升对象）。
  替代方案是把它做成隐藏类型，但「挂载库只开放可见来源」会让晋升对象在挂载运行里继续被剪掉（B-12 不解决），除非放宽那条已定裁决。
  两者的产品后果不同，所以列在这里；不回复即按推荐默认实现。
