# 删除整本笔记本：一天导出窗口（规格，2026-10-22，随实现定稿）

> 现状引用以实现落地时的代码为准；架构文档在根目录 `architecture.md`，产品/API 合同在
> `docs/product-and-api_zh.md`「删除整本笔记本：一天窗口」。拍板项 B1–B12 与 B9 落地细节的用户 / 主会话裁决记在文末。
> 本文最初由 Plan 代理起草（基于 origin/master `19c511daf`），实现时与它不符的地方已按实现改写，并在 §11 列出。

## 0. 问题
整改计划 `docs/superpowers/plans/2026-09-29-retrieval-permission-remediation.md` §13.2 第 7 条：主人删整本笔记本时成员记忆随库
级联删除，成员事先不知情、无法导出。用户 2026-10-21 裁决：告知 + 成员导出窗口一天；延迟删除 1 天；这一天里库对所有人不可用，只剩
成员带走自己记忆的入口。

## 1. 落地前的现状（文件:符号）
- 入口 `api/notebook_routes.py::delete_notebook`（202 `{"status":"deleting"}`）；守卫 `api/deps.py::require_notebook_delete`（只认
  主人、不看生命周期、手工镜像围栏 `_raise_if_mirrored`）；重复 DELETE：非主人 404、主人 409（`NotebookAlreadyDeletingError`）。
- 立墓碑 `repositories/{postgres,sqlite}/notebook_delete_job_store.py::NotebookDeleteJobStore.request`：同事务 CAS
  `status='deleting' WHERE … {NOTEBOOK_LIVE_SQL}` + 插 `notebook_delete_jobs('queued','mark')`。
- 六相位 `mark → paths → quiesce → rows → files → finalize`；`sweep_once` 启动一次 + 每 `NOTEBOOK_DELETE_SWEEP_SECONDS`（300）：
  驱动 A `list_stale`、驱动 B `list_notebooks_missing_job`（退避上限 5）。
- 收尾 `notebook_store.py::delete_row_and_orphan_embeddings`：留存投影 → `GlobalAskStore.stamp_retained_turns`（#931）→ 删根行。
- 「不可用」单一谓词 `NOTEBOOK_LIVE_SQL`（两后端约 40 个读侧站点）；`scripts/diag_common.py` 逐字副本；行为面守卫
  `test_notebook_lifecycle_visibility.py`（+PG 抽查）。

## 2. 设计总览
`notebooks.status` 新增 `'pending_delete'` 并并入 `NOTEBOOK_LIVE_SQL`（一处改动让全部读写站点对所有人隐藏）。只开**五个**专用例外：
①导出本人记忆 ②主人撤销 ③「即将删除」告知投影（铃铛 + 列表区块）④`GET /notebooks/{id}` 404 附说明码（仅读者）⑤把本人已确认的
记忆**移动**到自己的另一本笔记本（裁决 B9）。到期由删除扫尾新增的**驱动 C** 在一个事务里 `pending_delete → deleting` + 插作业行，
之后完全复用六相位、留存、#931。

## 3. 状态机
```
live --T1 主人 DELETE--> pending_delete --T3 到期(扫尾驱动 C)--> deleting --六相位--> (消失)
  ^                            |
  +------T2 主人撤销(到期前)-----+
copying / importing 不能进入 pending_delete（T1 的 CAS 要求存活）
```
窗口判据（两后端同一条 SQL 文本，只差占位符；`:now` 取注入时钟）：开放 `status='pending_delete' AND delete_after > :now`；到期
`… AND delete_after <= :now`。PostgreSQL 的 `delete_after` 是 `timestamptz`；SQLite 是 TEXT，窗口三条写路径与全部绑定参数都经
`app.domain.notebook_delete_grace.stored_instant_text` 写成定宽 UTC 串（微秒恒写、偏移恒 `+00:00`，字节序即时间序），所以裸文本比较
与 PostgreSQL 的比较同义，部分索引也能直接服务这个范围（`isoformat()` 省略零微秒的写法与之混比同样正确：`'+'` 排在 `'.'` 之前）。

| 转移 | 触发 | 事务内 CAS | 失败分流 |
|---|---|---|---|
| T1 | 主人 `DELETE`（`request_pending`） | `SET status_before_delete=status, status='pending_delete', delete_requested_at=:now, delete_requested_by=:actor, delete_after=:now+GRACE WHERE id=? AND {LIVE}`（不动 `updated_at`：撤销后一切原样） | 不存在 404；已在窗口里 409 `{"code":"notebook_pending_delete","delete_after","message"}`；deleting / copying / importing 沿用 409 |
| T2 | 仅主人 `POST /notebooks/{id}/cancel-delete`（`cancel_pending`） | `SET status=COALESCE(NULLIF(status_before_delete,''),'draft'), status_before_delete='', delete_after=NULL, delete_requested_at=NULL, delete_requested_by=NULL WHERE id=? AND {窗口开放}` | 0 行再读：已存活 → 200 当前摘要（幂等）；窗口里但已到期、或 deleting → 409 `{"code":"notebook_delete_due","message"}`；不存在 404 |
| T3 | 扫尾驱动 C（`list_due_pending` → `promote_due_pending`） | `SET status='deleting', updated_at=:now WHERE id=? AND {到期}` + 同事务 INSERT 作业行（与 `request()` 共用 `_insert_job_on`） | 0 行跳过 |

无竞态：T2/T3 对同一行 CAS，PG 行锁 + READ COMMITTED 重求值、SQLite 全局写锁；两边时钟对截止判断不一致时也只有一个落地（两个
方向都有双连接用例）；`idx_notebooks_pending_delete_due` 之外，`idx_notebook_delete_jobs_one_active` 兜重复建作业；驱动 B 不变。
T3 后不清新列（随行删除）。

刻意不变：在途维护检查点 `notebook_deleting` 与两个「库还活着」探针仍只认 `("deleting", None)`——窗口期内在途维护照常完成、撤销后数据
完整；旧同步删除路径（`job_id is None`）与同步导入 3d 仍立即删除（B7）；`NotebookDeleteJobRunner.request` 仍是立即 tombstone，网页
路由不再走它。

时钟：`request_pending` / `cancel_pending` / `list_due_pending` / `promote_due_pending`、告知投影、导出与转移的窗口查询都收显式 `now`
（`None` = 读一次 store 注入的时钟）；驱动 C 每 tick 只读一次时钟（`sweep_once(now=…)` 可注入）；驱动 B 的退避同一次读数（行为不变）。
宽限常量 `NOTEBOOK_DELETE_GRACE_SECONDS = 86_400`（`app/domain/notebook_delete_grace.py`），登记进产品/API 围栏表。

## 4. 读写面（pending_delete 期间）
原则：一律按「不存在」处理，只有 §5 五个例外。
| 面 | 主人 | 成员 | 实现 |
|---|---|---|---|
| 列表 / 列表搜索 | 卡片消失；顶部「即将永久删除的笔记本」区块（撤销删除；本人记忆 N>0 时导出与转移） | 卡片消失；N>0 时区块一行 + 导出 / 转移 | 自动 + §5.3 |
| `GET /notebooks/{id}` | 404 + 说明码 → 说明框（撤销、导出、转移、返回列表） | 有读权：404 + 说明码；无读权：普通 404 | §5.4 |
| 子资源、写与管理端点、单库问答 / 报告 | 404（在途任务跑完，撤销后可见） | 404 | 自动 |
| 再次 DELETE | 409 `notebook_pending_delete` | 404 | T1 |
| 全局问答 | 勾选范围无它；用到它的轮次 404，撤销恢复，收尾后按 #931 可读 | 同 | 自动（B5） |
| 挂载 | 挂载方当场失去参考库，撤销恢复 | 同 | 自动 |
| 记忆（列表 / 检索 / 候选 / 新建 / 编辑 / 记忆页转移 / 退出共享） | 看不到写不进 | 同；退出共享 404 | 自动 |
| 导出本人记忆 | 可 | 可 | §5.1 |
| 转移本人记忆到自己的笔记本 | 可 | 可 | §5.5 |
| MCP / Agent token | 404，token 不动 | 同 | 自动；MCP 不提供导出（B11） |
| 分享链接（`/shared`、`/c`、报告分享、全局会话分享） | 404 | 同 | 自动，撤销恢复 |
| 铃铛 | 该库原条目消失；新增「即将永久删除」一条（计入未读） | 有记忆的成员新增一条 | 自动 + §5.3 |
| 群组页 | 共享清单无它；审批 409「这本笔记本已安排删除，不能再共享。」 | 退群预览**列出**依赖该组可读的窗口期库，标「即将删除」并提示先导出（B12） | `group_routes.py` + `group_exit_memory_preview` |
| 管理员统计 | 窗口期内不计入，收尾后以留存摘要计入 | — | 自动（B6） |
| 同步导出 | 不受影响；新列登记 target-owned，导入新建镜像行时取默认值 | — | `sync/manifest.py` + `import_._insert_overrides` |
| 后台维护 / 调度 | 在途照常；新入队被存活守卫挡 | — | 生命周期可见性矩阵对 `pending_delete` 全量再跑一遍 |
| 诊断脚本 | 计入隐藏状态 | — | `diag_common.py` 同改 |

## 5. 例外入口
**5.1 导出本人记忆**：端点不变 `GET /notebooks/{id}/memories/export`；路由依赖改为专用 `require_memory_export_access`（读权 ∧
(存活 ∨ 窗口开放)，只挂导出与 §5.5 两条）。`memory_export_snapshot` 第一条语句在同一快照里读状态并分类（标题也从此取）：
```sql
SELECT nb.name, nb.status, nb.delete_after,
       (<read_access_clause>) AS readable,
       (nb.<NOTEBOOK_LIVE_SQL>) AS live,
       (<notebook_delete_window_open_sql('nb')>) AS window_open
FROM notebooks nb WHERE nb.id=:id
```
`classify_delete_window`：不存在、不可读或 copying/importing → 404；live 或 window_open → 继续（`_export_where` 改为
`export_access_exists_clause`，两后端各一份）；已到期 / deleting → 404 `{"code":"delete_window_closed"}`（阶段 3 删成员行后自然退化为
普通 404）。只导本人记忆（各状态），主人同（B4）。窗口内导出时文件头追加：「这本笔记本已被所有者删除，{时刻}起其中的全部内容（包括
这些记忆）将被永久删除。」（时刻按导出时钟的偏移写到分钟）。截止含义=「导出必须在截止前开始」（快照在 T3 前建立则完整，且不阻塞收尾）。

**5.2 撤销**：`POST /notebooks/{id}/cancel-delete` → 200 `NotebookSummary`；新依赖 `require_notebook_cancel_delete`（复用
`user_owns_notebook_regardless_of_lifecycle` / `NOTEBOOK_DELETE_OWNER_SQL` + 镜像围栏）；撤销后成员、授权、挂载、分享 token、会话、
在途结果原样回来，**§5.5 转走的记忆除外**；审计事件 `notebook_delete_cancelled`（另有 `notebook_delete_requested` /
`notebook_delete_promoted`，都不含内容）。

**5.3 告知投影**：`QueryStore.pending_delete_notices(user_id, *, now, notebook_id=None)`：窗口开放 ∧ 调用者满足 `read_access_clause`
（不带存活）∧（是主人 ∨ 本库有 ≥1 条本人记忆）（B3；带 `notebook_id` 时只要读者即可，供 §5.4）；返回
`{notebook_id, name, delete_after, is_owner, memory_count, transferable_count}` 按 `delete_after` 升序，由部分索引驱动（没有 pending 库时
读一个空索引，两后端计划钉住）。消费：①`GET /api/me/notebook-deletions` → `{"items":[…]}`，读不到时前端 fail-closed（「暂时读不到即将
删除的笔记本，请稍后重试」）②`PendingActionsService.list_for_user` 把它们作为 `type:"notebook_pending_delete"` 排在最前，**计入未读**
（投影单独一条语句，既有 `pending_actions_projection_rows` 一个字节都不动）③T1/T2/T3 提交后对主人与本库全部记忆创建者
`publish_snapshot`（fail-open；读不到受众就放过，等下一次快照）。

**5.4 说明码**：`GET /notebooks/{id}` 改挂专用依赖 `require_notebook_read_or_delete_notice`：读权照旧；拒绝时调
`PendingActionsService.deletion_notice(user_id, notebook_id)`，读者得 404 `{"code":"notebook_pending_delete", "notebook_id", "name",
"delete_after","is_owner","memory_count","transferable_count"}`，其他人普通 404。

**5.5 转移本人记忆到自己的笔记本**（裁决 B9 + 2026-10-22 落地裁决）：新端点 `POST /notebooks/{id}/memories/transfer`，请求体
`{"target_notebook_id"}`（`extra="forbid"`，模式不由调用方选）。`MemoryService.transfer_out_of_delete_window`：取一次时刻；窗口状态
`None` → 404、`closed` → 404 `delete_window_closed`；由服务端按窗口谓词取出本人**全部已确认、且不是 `promotion_state='proposed'`**
的记忆（`delete_window_transferable_ids`），逐条走既有 `MemoryService.transfer(…, "move", extract_kg=True)` 的校验与结果形状
（`{"results":[…]}`）。每条的源读取改走 `_transfer_source`：同一个时刻下按窗口谓词读、只认这本库；读不到且窗口已关 → 这一条
`error_code="delete_window_closed"`。也就是「截止前开始、逐条判断」：推进一旦提交，剩下的逐条失败，已转走的保留。目标照常要求是
调用者拥有的存活笔记本（不可写 / 非存活 → 404）。**转走的记忆不会因撤销回来**。候选与晋升审批中的条目不动，界面说「N 条记忆无法转移，
请先导出」，N = `memory_count - transferable_count`。不开「列出记忆 id」的读面。`MemoryService.transfer` 的函数长度天花板不升：新参数
与源读取各占原来的一行。

## 6. 文案（遵守 ui-vocabulary；时刻取服务端 `delete_after`、本地时区绝对时间；`frontend/app/notebook-deletion-copy.ts` 唯一出处）
- **主人确认弹窗**：标题「删除笔记本」；正文「确定删除“{名称}”吗？确认后，这本笔记本立即对所有人不可用：你和其他能读它的人都打不开、
  搜不到，问答、Agent 和分享链接也用不到它。它会再保留一天，之后永久删除其中的一切：来源、深度报告、会话，以及每个人各自保存在这里
  的记忆。这一天里，在这里有记忆的人会收到通知，可以导出自己的记忆，或把它们转移到自己的笔记本；你也可以随时撤销删除。撤销后其余一切
  恢复原样，但已经被转移走的记忆不会回到这本笔记本。」挂载提醒（N>0）：「{N} 个笔记本正在把它作为参考库。确认后它们立即用不到这条参考库；
  撤销删除可以恢复，一天后永久失去。」按钮「取消 / 确认删除」（在途「正在提交…」）；仍不显示成员人数与记忆条数。成功：「已安排删除
  “{名称}”。它现在对所有人都不可用，{时刻}起永久删除；在此之前可以在笔记本列表顶部撤销。」
- **列表区块**（标题「即将永久删除的笔记本」）：主人「“{名称}”已安排删除，现在对所有人都不可用。{时刻}起永久删除，在此之前可以撤销。」；
  成员「所有者删除了“{名称}”，它现在打不开了。{时刻}起会永久删除其中的一切，包括你在这里的 {N} 条记忆；在此之前可以导出或转移到你的
  笔记本。」（N=0 时只说到「其中的一切。」）；本地已过截止「删除时间已到，正在永久删除。」（无控件，重新拉取）。控件就地反馈：撤销中… /
  已撤销 +「已撤销删除，笔记本已恢复；已经被转移走的记忆不会回来。」/ 409「删除时间已到，已无法撤销。」/ 404「这本笔记本已不存在。」；导出中…
  / 已导出 / 导出失败 + 原因（超限沿用 `LEAVE_EXPORT_TOO_LARGE_TEXT`、`delete_window_closed`「删除时间已到，记忆已无法导出。」、已无可导出的）；
  「转移到我的笔记本」先显示「转移后这些记忆会离开这本笔记本，即使所有者撤销删除也不会回来。」，再挑自己的笔记本「确认转移」（转移中… /
  「已把 N 条记忆转移到“…”。M 条没有转移成功，请导出保存。」/「删除时间已到，记忆已无法转移。」）；没有自己的笔记本时「你还没有自己的其他
  笔记本可以接收这些记忆，请先导出。」。导出在途时撤销仍可点。
- **铃铛**：「“{名称}”将于 {时刻} 永久删除」；副行成员「可以先导出你的 {N} 条记忆」、主人「可以撤销删除」；点击回到笔记本列表、区块定位
  该行。
- **说明框**（复用 root modal 的 `info` 槽）：标题「这本笔记本已被删除」，正文复用区块对应句；按钮「撤销删除」（主人）、「导出我的记忆（N 条）」、
  「转移到我的笔记本」（去区块里操作）、「返回笔记本列表」。
- **退出群组面板**：窗口期库名字后标「即将删除」，行下「所有者已删除这本笔记本，{时刻}起永久删除；退出群组后就无法再导出，请先导出。」

## 7. 迁移
- 号：起草时 origin/master PG 最新 `0079`、SQLite `SCHEMA_VERSION=98`，本 PR 先取 0080 / v99；合入前 rebase 时并行分支
  （`ask_jobs.report_id`）已先合入占了 **0080 / v99**，按下面的登记点机械改号为 **PG `0081_notebook_delete_grace.sql` ↔ SQLite v100**
  （两个迁移器都要求版本号连续）。
- DDL（PG 为准）：status 新取值不改 DDL；`delete_after timestamptz NULL`、`delete_requested_at timestamptz NULL`（SQLite TEXT，定宽
  UTC ISO）；`delete_requested_by text COLLATE "C" NULL`（无 FK，`user_references.py` 登记 `audit_kept`）；`status_before_delete text
  COLLATE "C" NOT NULL DEFAULT ''`；`CREATE INDEX idx_notebooks_pending_delete_due ON notebooks(delete_after) WHERE
  status='pending_delete'`（两边）。不回填；不建新表；不加 PG CHECK（不变量由三条写路径 + 守卫保证）。
- 改号登记点（一次性替换）：`sqlite/migrations.py` 的 `SCHEMA_VERSION` 注释与 `_migration_99`；PG 文件名；
  `postgres/schema_manifest.py`（`sqlite_version` / `postgres_version`）；`migration/shadow/manifest.py::RUNNING_SCHEMA_PAIR`；
  `scripts/verify_repository_snapshot.py` 的 v99 块（`(key[0], 99, …)` 与 `MIGRATION_MANIFEST[(98, 99)]`）；
  `tests/sqlite_migration_testkit.py::rollback_v99`；`tests/test_sqlite_migrations.py`（`test_v99_…` 与各 `migrate() == [..., 99]`）、
  `tests/test_repository_snapshot_verifier.py`（`rollback_v99`、`(91, 99)`、`(96, 99)`）、`test_ask_job_output_migration.py`、
  `test_agent_token_tiers_migration.py`、`test_promotion_provenance_migration.py`；`tests/postgres/**` 的 `migrate() == 80` /
  `current_version() == 80` / `postgres_version == 80` 等字面量、`tests/postgres/test_migrations.py`
  `test_packaged_migrations_apply_in_order` 的 ledger 列表末项 `80`，以及
  `tests/postgres/test_memory_isolation_migration_pg.py::_roll_back_to_66` 开头撤 0080 的那一段（重放要先撤掉四列与部分索引）；`fixtures/repository_v9/{expected_snapshot,manifest}.json`（重跑
  `scripts/generate_repository_contract_fixtures.py`）。
- 同批登记：`sync/manifest.py` target_owned 加四列，`import_._insert_overrides` 让新建镜像行取默认值；两份 `access_sql.py` 的
  `NOTEBOOK_LIVE_SQL` 加 `'pending_delete'` + 单点常量 `NOTEBOOK_PENDING_DELETE_STATUS` 与 `notebook_delete_window_open_sql` /
  `notebook_delete_due_sql` / `export_access_exists_clause`（`test_access_sql_contract.py` 双后端同修守卫登记；字面量守卫限定
  `'pending_delete'` 的出处）；`diag_common.py` 两处；生命周期可见性种子；`diagnostics_runtime` 两条路由归一。

## 8. 用例（SQLite 进 G1，PG 孪生；共用场景 `tests/notebook_delete_grace_cases.py`）
1. T1 状态、202 响应、各角色 404 / 说明码、列表不出现、重复 DELETE 分流、镜像 409。
2. 生命周期可见性矩阵对 `pending_delete` 全量参数化（含分享三法、挂载闸、调度选取面），撤销后恢复。
3. T2 恢复全部（分享 token 与挂载回来）、非主人 404、重复撤销 200、到期后 409、deleting 409、前端墓碑清除。
4. T3：早于截止不动；到达后 deleting + 作业行；不重复建；扫尾（含冷启动）推进；推进后六相位留存与 #931 字段与直接删除逐字段相同。
5. 窗口内导出（有记忆 / 无记忆 / 外人 / 主人 / 窗口内失权）；文件头一句；存活时不带。
6. 到期未推进 → `delete_window_closed`；deleting 成员行在 → 同；阶段 3 后普通 404。
7. 并发：撤销 vs 到期（时钟偏差、两连接，双向）恰好一个生效且回报一致、无多余作业行（PG）。
8. 并发：到期 vs 导出（快照后 T3 + 删记忆行，文件完整且两者都不等待；T3 先 → closed）。
9. 两个驱动 C 同时推进只有一个成功（PG 双连接 + SQLite 双线程）。
10. 告知投影：有记忆的成员与主人各一条、无记忆读者不进列表但单本说明可读、计入未读、到期 / 撤销后消失、三次推送（失败放行）。
11. 窗口期内在途 KG 检查点为假、探针为真；推进后翻转。
12. 全局问答的批量读权窗口内排除、撤销恢复；收尾后的 `retained_until` 与直接删除相同。
13. 群组审批 409 新文案；退群预览列出窗口期库（窗口关闭的不列）。
14. 同步：源端仍导出四列；目标端新建镜像取默认值。
15. 迁移：存量四列为空；存量 deleting 不受影响；快照校验器识别 v99 → v100；`user_references` 守卫识别新列。
16. 转移（B9）：窗口内转移成功（只动已确认、非晋升审批中的）、目的不可写 / 非存活 / 就是这本库拒绝、到期后关闭、推进中途剩余逐条
    `delete_window_closed`、撤销后转走的仍在目标库不在原库。
17. 计划钉：告知投影与到期扫描（0 / 若干 pending）走部分索引，PG custom + generic 两种计划；既有 Memory 读者计划形状不变（导出那条
    多一个占位符，比较时剔除）。
18. 前端：确认弹窗文案、区块三状态两角色、撤销三结局、导出五结局、转移（提示 / 选目标 / 结果 / 关闭 / 无目标）、铃铛（分组、计入未读、
    签名、点击）、说明码错误类型、fail-closed、退群面板标签；词汇与英文控件守卫。

## 9. 文档与交付
`product-and-api.md` / `_zh.md` 成对（DELETE 合同、新小节「删除整本笔记本：一天窗口」、删除弹窗、导出、退群预览、待确认中心分组、
围栏表）；`architecture.md` 生命周期与驱动 C；`docs/operations*.md` 只读 SQL 列出 pending 与逾期未推进、手工撤销只写哪些列；
release note `feature`；计划 §13.2 第 7 条标已决指向本规格；`fangan_done.md` §61 追加。一个 PR、分阶段提交（两后端 → 前端 → 文档）。

## 10. 拍板项（附当时的建议）
- B1 管理员能否撤销？建议不能，只有主人（与删库只认主人一致；应急由运维 SQL）。
- B2 一天可配置？建议命名常量 24h 不做部署配置（文案承诺「一天」；前端时长一律取服务端 `delete_after`）。
- B3 告知范围？建议只告知「有记忆的读者 + 主人」（everyone 库不刷屏；其余人直接打开有说明码）。
- B4 主人本人记忆？建议同成员：可导出，到期一起删。
- B5 全局问答一天空档（窗口内 404、收尾后可读）？建议接受。
- B6 管理员统计一天空档？建议接受并写明。
- B7 不经窗口的删除路径（同步导入传播、旧同步路径、离线脚本）？建议维持立即删除。
- B8 库里没有他人记忆时立即删 / 主人「立即删除」跳过？建议都不能。
- B9 窗口期允许把记忆转移到本人其它笔记本？建议第一版不允许（**用户裁决：允许**，见下）。
- B10 到期前再提醒？建议不单独推送；铃铛挂到截止、显示绝对时刻；可选剩余 2h 标红（纯前端）。
- B11 MCP 导出 / 告知？建议不需要。
- B12 窗口期内退群会失去导出机会？建议在退群预览里也列出依赖该群可读的窗口期库并标「即将删除」、可先导出。

## 用户裁决（2026-10-22）
- B1：只有主人能撤销。
- B3：只告知主人 + 在本库有记忆的读者。
- **B9：也允许转移**——窗口期内成员可把本人记忆转移到她自己的另一本（存活、她可写的）笔记本，这是第五个例外入口；到期后关闭
  （`delete_window_closed`）。落地细节（用户裁决）：
  - **固定为移动（move）**，界面上不让用户选；
  - **范围是本人在这本库里全部已确认的记忆**，一键转移；候选，以及既有移动规则会拒绝的条目（如 `promotion_state='proposed'`），一律
    不转移，界面提示「N 条记忆无法转移，请先导出」，N 的口径和现有校验一致；
  - 因为是移动，「撤销后一切恢复原样」在转移过之后不成立：主人确认弹窗与撤销成功提示如实写明已转走的记忆不会回来；成员点「转移到我的
    笔记本」之前先看到「转移后这些记忆会离开这本笔记本，即使所有者撤销删除也不会回来」；撤销后转走的记忆留在目标笔记本，写进产品合同。
  - 落地裁决（主会话）：新开窗口专用端点 `POST /notebooks/{id}/memories/transfer`，请求体 `{target_notebook_id}`，`extract_kg` 沿用记忆页
    转移的默认值、不暴露给界面；服务端按窗口谓词取出本人可转移的记忆，逐条复用 `MemoryService.transfer` 的校验，结果形状与现有接口相同；
    不开「列出记忆 id」的读面。与 T3 并发：截止前开始、逐条判断窗口，中途碰到推进之后剩下的逐条 `delete_window_closed` 失败，已转成功的保留。
- B12：退群预览列出窗口期内的库（§4 表里早先写的「退群预览不计入」与此相反，已按裁决改正）。
- 其余 B2、B4–B8、B10、B11 全部按建议（24h 常量；主人记忆同成员；全局问答 / 统计一天空档接受；镜像 / 离线路径立即删除；不提供立即删除；
  不单独推送提醒；MCP 不导出）。B10 里「剩余 2h 标红」是可选项，本 PR 未做。
- 小处（主会话裁决）：`GET /notebooks/{id}` 改用专用依赖；DELETE 的 202 响应体改为 `{"status":"pending_delete","delete_after"}`、前端读取
  它；群组审批 409 文案用「这本笔记本已安排删除，不能再共享。」；退群预览按 B12 列出窗口期库。

## 11. 与草案不同的地方（实现时修正）
- 迁移号最终是 0081 / v100：0080 / v99 已被先合入的 `ask_jobs.report_id` 占用（迁移器要求连续，见 §7）。
- 告知投影不并进 `pending_actions_projection_rows`，而是 `PendingActionsService` 另读一条 `pending_delete_notices`：既有投影语句一个
  字节都不动，计划不受影响；铃铛条目排在最前、计入未读，效果同草案。
- §5.4 的 404 在依赖层而不是路由体的 KeyError 分支（读守卫在依赖层就拒绝了），所以给 `GET /notebooks/{id}` 换了专用依赖。
- SQLite 的窗口比较是裸文本（定宽 UTC 串），不是 `julianday()`：两后端谓词逐字相同，`test_access_sql_contract.py` 才能把它们钉成同修。
- 窗口期列表区块、铃铛与说明框都多了「转移到我的笔记本」（B9），说明框复用现成的 `info` 弹窗槽，转移去区块里挑目标。
