# 待确认中心(头像旁铃铛)设计 Spec

日期:2026-07-07 · 状态:待用户评审 · 前置:两阶段深度报告(#203)引入"用户确认大纲"环节,需全局入口聚合待办

## 0. 目标与决策

用户可能在多个 notebook 里同时有"待处理/待确认"的事,需要头像旁一个**全局待确认中心**聚合展示、点击直达。

**用户已拍板**:
1. **v1 范围三类**:①深度报告待确认(outline_ready)②治理队列(合并/边/晋升审核)③索引完成情况。
2. **只显示我创建的**(created_by=当前用户)。
3. **UI = 铃铛 + 徽章 + 下拉**。

**约束(效率一等,见 [[efficiency-first-mandate]])**:轮询端点必须廉价——几条走索引的 SELECT + 内存集判定,零 LLM/embed;登录才轮询、~45s + 窗口聚焦、`hasPending` 变化才 setState(照 [[kb-list-scale-and-poll-storm]] 的自调度模式,防 re-render 风暴)。

## 1. 后端:GET /api/me/pending-actions

单端点聚合三类,`type` 标签化(留扩展),全部限 `created_by=当前用户` 的 notebook。返回:
```json
{"count": 5, "items": [
  {"type":"report_outline","notebook_id","notebook_name","report_id","title","created_at"},
  {"type":"governance","subtype":"merge|edge|promotion","notebook_id","notebook_name","count":3},
  {"type":"index","state":"building|stale","notebook_id","notebook_name","progress?"}
]}
```
`count` = **actionable 项数**(report_outline + governance + index=stale;index=building 属信息态不计入徽章数,但在下拉里显示)。

### 数据源(全走现成原语,均已 Explore 坐实)
- **我的 notebooks(严格 created_by)**:**不复用 `list_notebooks()`**——它返回「我创建的 ∪ 分享给我只读的」;用户要"只是我创建的",故单查
  `SELECT id, name FROM notebooks WHERE created_by = ? AND status != 'copying'`,得 `(id→name)` 映射 `MY`。(分享给我只读的库,我无权处理其治理/索引,不能列。)
- **① report_outline**:1 条查询 `SELECT id,question,notebook_id,updated_at FROM reports WHERE status='outline_ready' AND created_by=? ORDER BY updated_at DESC`(reports 表有 created_by/status,`outline_ready` 由 report_engine.py:149 写入)。title=question 截断。
- **② governance**(按 notebook × 子类,count>0 才出项;谓词已核对三个队列端点):
  - `merge`:`SELECT notebook_id,COUNT(*) FROM concept_merge_candidates WHERE notebook_id IN (MY) AND status='pending' GROUP BY notebook_id`(对齐 `pending_merges`)。
  - `edge`:`... FROM knowledge_relations WHERE notebook_id IN (MY) AND review_status='pending' GROUP BY notebook_id`(对齐 `review_queue`)。
  - `promotion`:`... FROM promotion_candidates WHERE notebook_id IN (MY) AND status IN ('proposed','under_review') GROUP BY notebook_id`(对齐 `list_promotion_queue` 活跃态)。
  - **3 条聚合查询,非 N+1**。三表都有 `notebook_id` 列。
- **③ index**:遍历 `MY`,`scale_index_status(nb)` 取返回的 **`state`** 字段(单字段分类,免拼判据):
  - `state in ('stale','suggested')` → **actionable**(建议重建 / 建议建立索引),计入 badge。
  - `state in ('building','queued')` → **信息态**(构建中),进下拉不计 badge;`building` 来自内存集 `_scale_building`(零成本)。
  - `state in ('indexed','unindexed')` → 不出项(已建好 / 库太小不够格)。
  - **效率**:`scale_index_status` 已按 manifest 版本缓存;跳过 `eligible=False` 的库可再省。

**新端点**放 routes.py(用户级、非 /notebooks/{id} 子资源):`GET /api/me/pending-actions`,`Depends(get_current_user)`。repo 新增 `pending_actions(user_id) -> dict`(单次连接内跑上述查询 + 遍历 index)。

### count(徽章数)语义
badge = **下拉里 actionable 项的条数**(report_outline 项数 + governance 项数 + index stale/suggested 项数),**不是**候选原始条数(merge 队列可能上千,`m.count` 只在项内显示如「待合并 1000」)。building 与 index_done(客户端)不计入 badge。

### 索引"完成"提示(v1 要,零后端成本方案)
用户要 v1 就有"构建完成"提示。**不引入完成事件表**,用**前端轮询差分**实现:
- 后端 index 项仍只返回 `building` / `stale`(无 `done` 态)。
- 前端每次轮询记住**上一轮的 building notebook 集** `prevBuilding`。本轮算 `justDone = prevBuilding − currentBuilding`(上轮在建、本轮已不在建的 notebook)。
- 对每个 `justDone` 的 notebook:①弹 **toast**「「{nb名}」索引构建完成,点击查看」;②在下拉里注入一条**客户端本地** `index_done` 项(灰色"已完成"分组),点击导航到该 nb、点击后/手动关闭即消除,**页面刷新即清空**(它是瞬时通知,非服务端持久状态)。
- **覆盖范围**:此法覆盖"用户开着页面时构建完成"的常见场景(轮询能观察到 building→gone 的跃变)。**盲区**=用户在整段构建期间关掉标签、构建完成后才重开→客户端没有 `prevBuilding` 记忆、无 toast(重开时 index 项已不在 building 集、也不在 stale,故不出现)。这个"跨会话的完成通知"需要服务端 ack 状态,留 **v2**。
- **效率**:纯客户端 diff,零新增后端查询/表。

## 2. 前端:铃铛 + 徽章 + 下拉(topbar 头像旁)

**铃铛位置**:顶栏 `topbar-right` flex 容器内、`user-menu` 之前(现有结构:`status` + `user-menu`,page.tsx ~2847-2889)。加 **Bell 图标 + 未读数徽章**(count>0 显红点数字)。**照搬 `accountMenuOpen`/`accountMenuRef` 模式**(useState 开合 + ref 外部点击关)做 `pendingOpen`/`pendingRef`。

点击 → 下拉面板,按 type 分组;每项点击调 `openPendingItem(item)`(见下 deep-link 机制):
- **待确认大纲**:「深度报告《问题…》· {nb名}」→ `openNotebook(nb)` + `switchChatMode('reports')` + 设 `pendingReportFocusId=report_id`(打开该报告的大纲编辑器)。
- **治理·合并**:「{nb名} · 待合并 N」→ `openNotebook(nb)` + `setKgViewOpen(true)`(合并审查在 KG 图谱视图 `kg-view` 的 `kg-rail` 内联段「待确认合并」,page.tsx ~4318;进视图即加载 `pendingMerges`)。
- **治理·边审**:「{nb名} · 边审 N」→ `openNotebook(nb)` + `openEdgeReviewQueue()`(专用模态 `edgeReviewOpen`,~2715)。
- **治理·晋升**:「{nb名} · 晋升 N」→ `openNotebook(nb)` + `openPromoQueue()`(专用模态 `promoOpen`,~2682;该队列端点 `/promotion-queue` 为全局,晋升属 admin 治理——count 仍按我的 notebook 过滤,plan 视 admin 可见性 gate)。
- **索引 building**:「{nb名} · 索引构建中」(有 delta/total 则显粗略 %)→ `openNotebook(nb)` + `setKgViewOpen(true)`(scale 索引状态/重建入口在 KG 视图;plan 坐实精确落点)。
- **索引 stale/suggested**:「{nb名} · 建议重建索引 / 建议建立索引」→ 同上 `openNotebook(nb)` + 索引区。
- **索引 done(客户端瞬时)**:「{nb名} · 索引构建完成」(灰色"已完成"分组)→ `openNotebook(nb)`,点击/关闭即消除。
- 空态:「暂无待确认」。

**deep-link 机制**:page.tsx 定义 `async function openPendingItem(item)`:先 `await openNotebook(item.notebook_id)`(已有函数,拉详情+来源+会话+重置 chatMode='ask'+更新 hash),再按 `item.type/subtype` 追加上面的 setter。铃铛下拉组件通过 prop 收到 `openPendingItem` 调用之。
- **报告聚焦**:`active`(当前报告)是 ReportsPanel **内部** state → 新增父层 `pendingReportFocusId` state 传入 ReportsPanel;组件在 `pendingReportFocusId` 变化且列表就绪时 `getReport(id)→setActive`,消费后回调清空(仿现有 `pendingKgFocusId` 聚焦 KG 节点的范式,~1708)。

**轮询**:登录后 `useEffect` 定时 ~45s + `visibilitychange` 聚焦时拉 `/me/pending-actions`;比对 `count`+项签名变化才 setState(防 re-render);登出/隐藏页停轮询。**索引完成 toast**:每轮记住 `prevBuilding`(上轮 index=building 的 nb 集),算 `justDone = prevBuilding − currentBuilding` → 弹 toast + 注入客户端 `index_done` 项(见 §1.3)。

UI 达 [[ui-polish-bar]]:徽章对齐、下拉圆角阴影、分组标题、hover、加载/空态、toast 一致风格。

## 3. 数据模型 & 兼容
- **无新表/新列**:全部读现有表(reports/concept_merge_candidates/knowledge_relations/promotion_candidates)+ 内存 building 集。
- 新增 schema:`PendingActionItem`、`PendingActionsResponse{count, items}`(items 用宽松 dict 或分 type 建模,取 union 简单起见 v1 用 `List[dict]` + 顶层 count)。

## 4. 测试 & 验收
- 单测:`pending_actions(user_id)` 三源聚合(造 outline_ready 报告 / pending 合并候选 / building 集含某 nb / stale 索引 → 断言各出项 + count 只计 actionable);跨用户隔离(别人 notebook 的待办不出现);空 → count=0。
- API:端点权限(未登录 401/匿名 user-local)、返回结构。
- 前端:tsc/test;铃铛徽章数=count;点击 deep-link 切 notebook+对应面板(能测的做,视觉人工)。
- 真机:多 notebook 各造一类待办 → 铃铛显合计 → 逐项点击直达。

## 5. 分期
- **v1(本 spec)**:GET /me/pending-actions(三源)+ 铃铛下拉 + **精确 deep-link**(报告→大纲编辑器 / 治理→分析弹窗对应 tab / 索引→该 nb)+ **索引完成 toast(客户端轮询差分,零后端)**。
- **v2**:index 完成的**跨会话**通知(用户构建期间关页、重开仍提示——需服务端 ack 状态)、治理项点击落到**具体某条候选**(非仅面板)、WebSocket 实时(替轮询)。

## 6. 效率账
- 每次轮询:report_outline 1 查 + governance 3 聚合查 + index(building 内存集免费 + stale 仅对 eligible notebook 查)。全走索引、无 LLM/embed。~45s 轮询 + 变化才 setState + 页面隐藏停轮询。对多 notebook 用户,index stale 检查是唯一略重处→限 eligible 收窄。

## 7. 未决/风险(已核实 → 结论)
- ✅ **治理"pending"判据**:已核对三队列——merge=`status='pending'`、edge=`review_status='pending'`、promotion=`status IN('proposed','under_review')`,三表均有 `notebook_id`。中心计数与队列页一致。
- ✅ **治理精确 deep-link**:三面**异构**——合并=KG 图谱视图 `kgViewOpen` 内联段、边审=`openEdgeReviewQueue()` 模态、晋升=`openPromoQueue()` 模态;**无需**给弹窗加 tab,直接调各自 opener 即精确直达。
- ✅ **scale_index_status 结构**:用返回的 `state` 字段(`building/queued/suggested/unindexed/indexed/stale`)单字段分类;无进度%,粗略进度用 `delta_chunks/total_chunks`。
- ✅ **我的库口径**:严格 `created_by=?`(不含分享只读库)。
- ⚠ **晋升为全局/admin**:`/promotion-queue` 无 notebook scope、属 admin 治理。v1 按我的 notebook 过滤计数、deep-link 到全局 promo 模态;plan 视当前用户 admin 可见性决定是否对非 admin 隐藏晋升项(避免点了打不开)。
- ⚠ **索引精确落点**:scale 索引状态/重建入口在 KG 视图内的具体位置,plan 任务里定位并滚动到位;v1 兜底=进 KG 视图即可见。
- **index stale 成本**:多 notebook 累积;`scale_index_status` 已按 manifest 版本缓存,跳过 `eligible=False` 收窄。
