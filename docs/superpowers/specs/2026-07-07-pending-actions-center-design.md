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

### 数据源(全走现成原语,DRY)
- **我的 notebooks**:`list_notebooks()` 已 `WHERE created_by=?`(当前用户),取其 id 集 `MY`。
- **① report_outline**:1 条查询 `SELECT id,question,notebook_id,updated_at FROM reports WHERE status='outline_ready' AND created_by=? ORDER BY updated_at DESC`(created_by 已在 reports)。title=question 截断。
- **② governance**(按 notebook × 队列,count>0 才出项):
  - merge:`concept_merge_candidates` pending 计数(复用 `get_pending_merges` 的判据,做成 `GROUP BY notebook_id` 计数查询,限 `MY`)。
  - edge:边审队列 pending 计数(复用 `edge_review_queue` 判据)。
  - promotion:`list_promotion_queue(status_filter='pending')` 过滤 notebook ∈ `MY`。
  - **实现要点**:各做一条 `... WHERE notebook_id IN (MY) [AND pending 判据] GROUP BY notebook_id`(3 条聚合查询,非 N+1)。
- **③ index**:遍历 `MY`,`scale_index_status(nb)` 取 `building`/`stale`;`building` 直接读内存集 `_scale_building`(免费)。**效率**:仅对 `eligible=True` 的 notebook 查 stale(小库天然不 eligible、跳过);building 走内存集零成本。progress 若有(重建进度)带上。

**新端点**放 routes.py(用户级、非 /notebooks/{id} 子资源):`GET /api/me/pending-actions`,`Depends(get_current_user)`。repo 新增 `pending_actions(user_id) -> dict`(聚合上述,单次连接内多查询)。

### 索引"完成"通知(范围说明)
v1 只显示 **building(进行中)+ stale(建议重建)**;index 构建**完成**=它从 building 集消失、下拉里该项自然消失(用户刷新可见"没有了"=好了)。**显式"构建完成"toast/推送属 v2**(需完成事件追踪),v1 不做——避免为一个通知引入 job 完成事件表。

## 2. 前端:铃铛 + 徽章 + 下拉(topbar 头像旁)

- 顶栏已有登录用户名/头像区(用户系统 PR#78)。在其旁加 **Bell 图标 + 未读数徽章**(count>0 显红点数字)。
- 点击 → 下拉面板,按 type 分组:
  - **待确认大纲**:「深度报告《问题…》· {notebook名}」→ 点击 = 切到该 notebook + `chatMode='reports'` + 打开该 report 的大纲编辑器(deep-link)。
  - **治理待办**:「{notebook名} · 待合并 3 / 边审 2 / 晋升 1」→ 点击 = 切到该 notebook + 打开分析弹窗对应面板。
  - **索引**:「{notebook名} · 索引构建中(45%)/ 建议重建索引」→ 点击 = 切到该 notebook(+ 索引状态区)。
  - 空态:「暂无待确认」。
- **轮询**:登录后 `useEffect` 定时 ~45s + `visibilitychange` 聚焦时拉 `/me/pending-actions`;比对 `count`/签名变化才 setState;登出/隐藏页停轮询。
- **deep-link 机制**:page.tsx 暴露 `openPendingItem(item)` — 内部 `setCurrentNotebookId(item.notebook_id)`(必要时先拉该 nb 详情)→ 按 type 设 `chatMode`/`activeReportId`/`analysisPanel`。铃铛组件通过 prop 调它。
- UI 达 [[ui-polish-bar]]:徽章对齐、下拉圆角阴影、分组标题、hover、加载/空态。

## 3. 数据模型 & 兼容
- **无新表/新列**:全部读现有表(reports/concept_merge_candidates/knowledge_relations/promotion_candidates)+ 内存 building 集。
- 新增 schema:`PendingActionItem`、`PendingActionsResponse{count, items}`(items 用宽松 dict 或分 type 建模,取 union 简单起见 v1 用 `List[dict]` + 顶层 count)。

## 4. 测试 & 验收
- 单测:`pending_actions(user_id)` 三源聚合(造 outline_ready 报告 / pending 合并候选 / building 集含某 nb / stale 索引 → 断言各出项 + count 只计 actionable);跨用户隔离(别人 notebook 的待办不出现);空 → count=0。
- API:端点权限(未登录 401/匿名 user-local)、返回结构。
- 前端:tsc/test;铃铛徽章数=count;点击 deep-link 切 notebook+对应面板(能测的做,视觉人工)。
- 真机:多 notebook 各造一类待办 → 铃铛显合计 → 逐项点击直达。

## 5. 分期
- **v1(本 spec)**:GET /me/pending-actions(三源)+ 铃铛下拉 + deep-link。
- **v2**:index 构建**完成**的显式推送/toast、治理项细分点击落到具体条目、WebSocket 实时(替轮询)。

## 6. 效率账
- 每次轮询:report_outline 1 查 + governance 3 聚合查 + index(building 内存集免费 + stale 仅对 eligible notebook 查)。全走索引、无 LLM/embed。~45s 轮询 + 变化才 setState + 页面隐藏停轮询。对多 notebook 用户,index stale 检查是唯一略重处→限 eligible 收窄。

## 7. 未决/风险
- **治理"pending"判据**:需实现时对齐三个现有队列端点的确切过滤(避免中心计数与队列页不一致)——实现者读 `get_pending_merges`/`edge_review_queue`/`list_promotion_queue` 的判据照搬。
- **deep-link 到治理面板**:分析弹窗当前是 notebook 内触发;需让 page.tsx 支持"进 nb 即打开某面板"。若接线成本高,v1 可先只 deep-link 到 notebook(面板用户手动开),报告/索引精确直达。
- **index stale 计算成本**:多 notebook 时累积;v1 限 eligible + 可加轻缓存(scale_index_status 已按 manifest 版本缓存)。
