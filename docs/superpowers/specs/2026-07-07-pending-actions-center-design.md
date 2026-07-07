# 待确认中心(头像旁铃铛)设计 Spec

日期:2026-07-07 · 状态:待用户评审 · 前置:两阶段深度报告(#203)引入"用户确认大纲"环节,需全局入口聚合待办

## 0. 目标与决策

用户可能在多个 notebook 里同时有"待处理/待确认"的事,需要头像旁一个**全局待确认中心**聚合展示、点击直达、**实时更新**。

**用户已拍板**:
1. **v1 范围三类**:①深度报告待确认(outline_ready)②治理队列(合并/边审/晋升)③索引完成情况(building/完成/建议重建)。
2. **只显示我创建的**(严格 `created_by=当前用户`)。
3. **UI = 铃铛 + 徽章 + 下拉**。
4. **事件推送(非轮询)**:job 完成 → 主动推送 → 显示。
5. **完成提示覆盖跨会话**:用户离线(关页)期间完成的事件,重开也要提示。

**约束(效率一等,见 [[efficiency-first-mandate]])**:事件驱动优于轮询——**空闲零查询**,只在真有 job 完成时算一次(多用户下净省)。零 LLM/embed。SSE 复用现有带认证的流式范式;进程内事件总线(部署单进程,`_scale_building` 内存集即证);跨会话补发用**进程内内存缓冲(不新增表)**。

## 1. 后端

部署实证(本会话 Explore 坐实):**单进程** uvicorn(启动无 `--workers`);已有 `StreamingResponse`(`ask_stream`)+ 前端 `getReader()+TextDecoder` 带 Bearer 读流的范式;`background_jobs.submit` = `threading.Thread`+`copy_context`(同进程线程池);auth = `Authorization: Bearer`。

### 1.1 计算核心 `pending_actions(user_id) -> dict`(REST 与 SSE 共用)
一次连接内跑,返回 `{count, items:[...]}`。数据源全走现成表/原语,均已坐实:

- **我的 notebooks(严格 created_by)**:**不复用 `list_notebooks()`**(它含"分享给我只读"的库);单查
  `SELECT id, name FROM notebooks WHERE created_by = ? AND status != 'copying'` → `(id→name)` 映射 `MY`。
- **① report_outline**:`SELECT id,question,notebook_id,updated_at FROM reports WHERE status='outline_ready' AND created_by=? ORDER BY updated_at DESC`(`outline_ready` 由 report_engine.py:149 写)。每条 → `{type:'report_outline', notebook_id, notebook_name, report_id, title(question截断), created_at}`。
- **② governance**(3 条聚合查,非 N+1;谓词已对齐三队列端点):
  - `merge`:`concept_merge_candidates WHERE notebook_id IN (MY) AND status='pending' GROUP BY notebook_id`
  - `edge`:`knowledge_relations WHERE notebook_id IN (MY) AND review_status='pending' GROUP BY notebook_id`
  - `promotion`:`promotion_candidates WHERE notebook_id IN (MY) AND status IN ('proposed','under_review') GROUP BY notebook_id`
  - 每(notebook×子类,count>0)→ `{type:'governance', subtype:'merge|edge|promotion', notebook_id, notebook_name, count}`。
- **③ index**:遍历 `MY`,`scale_index_status(nb).state` 单字段分类:
  - `state in ('stale','suggested')` → `{type:'index', state, notebook_id, notebook_name}`(actionable:建议重建/建议建立)
  - `state in ('building','queued')` → `{type:'index', state:'building', notebook_id, notebook_name, progress?}`(信息态;`delta_chunks/total_chunks` 算粗略 %)
  - `state in ('indexed','unindexed')` → 不出项
  - 效率:`scale_index_status` 已按 manifest 版本缓存;可跳过 `eligible=False`。

**count(徽章数)** = actionable 项条数(report_outline + governance + index 的 stale/suggested)。**不含** building 与瞬时完成事件。governance 的 `count` 是队列深度(可上千),只在项内显示(如「待合并 1000」),不进 badge。

### 1.2 REST 快照端点 `GET /api/me/pending-actions`
`Depends(get_current_user)` → `pending_actions(user.id)`。用途:**首屏秒开** + **SSE 断线兜底** + 不支持流的降级。与 SSE 同源计算,无重复逻辑。

### 1.3 SSE 流端点 `GET /api/me/pending-actions/stream`
`StreamingResponse(media_type="text/event-stream")`,`Depends(get_current_user)`。一条流承载两类消息(JSON 行):

| 消息 | 时机 | 前端反应 |
|---|---|---|
| `{"kind":"snapshot","data":{count,items}}` | 连上时立即 + 每次待办变化后 | 整体替换铃铛内容(幂等,无前端 diff) |
| `{"kind":"event","event":"index_done","notebook_id","notebook_name"}` | 索引/报告等 job 完成瞬间 | 弹 toast + 客户端"已完成"项 |

**连接生命周期**(在 asyncio loop 内):
1. 建立 → 注册一个 `per-connection asyncio.Queue` 到该 user 的连接集。
2. **先 flush 该 user 的 pending 缓冲**(§1.5 跨会话补发)→ 逐条作为 `event` 发出。
3. 发一次 `snapshot`(初始全量)。
4. 进等待循环:`await queue.get()` → 收到 publish 的消息就 `yield` 给客户端;周期性发 `: keepalive` 注释帧(如 15s)防中间层掐连接。
5. 断开(客户端关闭/网络断)→ 从连接集移除该 queue。

### 1.4 进程内事件总线 + 触发点
模块级单例 `PendingBus`(`asyncio` 世界操作,启动时 `asyncio.get_running_loop()` 存主 loop 引用)。job 在**线程**里,故跨线程投递用 `loop.call_soon_threadsafe(...)`。

- `PendingBus.mark_dirty(user_id)`:重算该 user 的 `pending_actions` → 向其所有连接 queue fan-out `snapshot`;无连接则忽略(存量待办本就持久,重开会拉到)。
- `PendingBus.emit(user_id, event)`:向其所有连接 queue fan-out `event`;**无连接则存入 pending 缓冲**(§1.5)。

**触发接线**(在会改变待办的 job 完成处;user_id 从 `copy_context` 带的 `_REQUEST_USER` 解析):
- `background_jobs.submit(fn, name=, notify_pending=False)` 新增 `notify_pending` 旗标:相关提交点(报告 plan/generate、索引 build/fold、KG rebuild)设 `True`;submit 的 `finally` 里若 `notify_pending` 则 `bus.mark_dirty(uid)`(经 `call_soon_threadsafe`)。**单接入点**覆盖 snapshot 刷新。
- **索引完成 toast**:索引 build/fold job 成功收尾时显式 `bus.emit(uid, {event:'index_done', notebook_id, notebook_name})`(mark_dirty 之外的瞬时提示)。
- (可扩:报告 generate 完成 → `event:'report_done'`,v1 聚焦 index_done。)

### 1.5 跨会话补发(进程内内存,不新增表)
`PendingBus` 维护 `pending_buffer: dict[user_id, list[(ts, event)]]`。`emit` 时若该 user **无活跃连接** → append 到缓冲。新连接建立(§1.3 步骤 2)→ flush 并清空该 user 缓冲。**TTL**:append/flush 时丢弃超 30min 的旧事件(防用户永不回来堆积)。
- **只有瞬时完成事件(index_done 等)进缓冲**;存量待办(报告/治理/stale)是持久状态,重开即在 snapshot,无需缓冲。
- **取舍(已与用户确认取内存)**:唯一丢失窗口 = **后端进程重启**;而索引构建是随进程存活的线程 job,进程重启则构建本身中断、不存在"已完成待送达"的事件——故对索引完成提示几乎零实质损失。若未来要"重启也不丢"的强持久,再引入 `notifications` 表(v2)。

## 2. 前端:铃铛 + 徽章 + 下拉(topbar 头像旁)

**铃铛位置**:顶栏 `topbar-right` flex 容器内、`user-menu` 之前(现有 `status`+`user-menu`,page.tsx ~2847-2889)。Bell 图标 + 未读数徽章(count>0 显红点)。照搬 `accountMenuOpen`/`accountMenuRef` 模式做 `pendingOpen`/`pendingRef`(开合 + 外部点击关)。

**实时数据流**(复用 `ask_stream` 的带认证流式范式):
- 登录后建立 SSE:`fetch('/api/me/pending-actions/stream', {headers: authHeaders()})` → `response.body.getReader()` + `TextDecoder` 按行解析(同 page.tsx:564 现范式)。
- 消息处理:`kind:'snapshot'` → 整体替换 `pending` state(count+items);`kind:'event'/index_done` → 弹 toast +注入客户端"已完成"项(灰色分组,点击/关闭即消除)。
- **断线重连**:读循环结束(网络断/服务端关)→ 指数退避重连(如 1s→2s→…→30s 上限);重连成功后服务端会重发 snapshot + flush 缓冲(补发离线期间的完成事件)。
- **REST 兜底**:首屏或 SSE 建立前,先 `GET /api/me/pending-actions` 拉一次快照秒开;SSE 持续失败时退回定时 REST(低频,如 60s)保底。
- 登出 → 关闭 SSE + 清空 state。页面隐藏可选保持连接(SSE 空闲近零成本)。

**下拉面板**(按 type 分组;每项点击 `openPendingItem(item)`):
- **待确认大纲**:「深度报告《问题…》· {nb名}」→ `openNotebook(nb)` + `switchChatMode('reports')` + 设 `pendingReportFocusId=report_id`。
- **治理·合并**:「{nb名} · 待合并 N」→ `openNotebook(nb)` + `setKgViewOpen(true)`(合并审查在 KG 图谱视图 `kg-view` 的 `kg-rail` 内联段「待确认合并」,~4318;进视图即加载 `pendingMerges`)。
- **治理·边审**:「{nb名} · 边审 N」→ `openNotebook(nb)` + `openEdgeReviewQueue()`(模态 `edgeReviewOpen`,~2715)。
- **治理·晋升**:「{nb名} · 晋升 N」→ `openNotebook(nb)` + `openPromoQueue()`(模态 `promoOpen`,~2682;队列端点 `/promotion-queue` 全局、属 admin,plan 视 admin 可见性 gate)。
- **索引 building**:「{nb名} · 索引构建中(45%)」→ `openNotebook(nb)` + `setKgViewOpen(true)`(scale 索引状态/重建入口;plan 坐实落点)。
- **索引 stale/suggested**:「{nb名} · 建议重建索引 / 建议建立索引」→ 同上。
- **索引 done(客户端瞬时)**:「{nb名} · 索引构建完成」(灰色"已完成"分组)→ `openNotebook(nb)`,点击/关闭即消除。
- 空态:「暂无待确认」。

**deep-link 机制**:page.tsx `async function openPendingItem(item)` → 先 `await openNotebook(item.notebook_id)`(现有函数:拉详情+来源+会话+重置 chatMode='ask'+更新 hash),再按 `item.type/subtype` 追加 setter。
- **报告聚焦**:`active`(当前报告)是 ReportsPanel 内部 state → 新增父层 `pendingReportFocusId` 传入 ReportsPanel;组件在其变化且列表就绪时 `getReport(id)→setActive`,消费后回调清空(仿 `pendingKgFocusId` 聚焦 KG 节点范式,~1708)。

UI 达 [[ui-polish-bar]]:徽章对齐、下拉圆角阴影、分组标题、hover、加载/空态、toast 一致风格。

## 3. 数据模型 & 兼容
- **无新表/新列**:待办全读现有表(reports/concept_merge_candidates/knowledge_relations/promotion_candidates)+ `scale_index_status`;事件总线与跨会话缓冲均**进程内内存**。
- 新增 Pydantic:`PendingActionsResponse{count:int, items:List[dict]}`(v1 用宽松 dict + type 标签,免为每类建模)。SSE 消息为 JSON 行(snapshot/event)。
- `background_jobs.submit` 新增可选 `notify_pending: bool=False`(向后兼容,默认不触发)。

## 4. 测试 & 验收
- 单测 `pending_actions(user_id)`:三源聚合(造 outline_ready 报告 / pending 合并候选 / building 集含某 nb / stale 索引 → 各出项 + count 只计 actionable);**跨用户隔离**(别人及"分享给我只读"的库不出现);空 → count=0。
- `PendingBus`:`mark_dirty` 有连接→fan-out snapshot;`emit` 无连接→入缓冲、新连接→flush 补发;TTL 丢弃过期;多连接 fan-out。
- API:REST 端点权限(未登录/匿名 user-local)+ 结构;SSE 端点 headers/首帧 snapshot(可用 TestClient 读首块)。
- 前端:tsc/test;snapshot 替换、event toast、断线重连退避、REST 兜底;deep-link 各类切 nb+落点(能测的做,视觉人工)。
- 真机:多 notebook 各造一类待办 → 铃铛显合计;发起索引重建→**关页**→完成后重开→**补发"构建完成"toast**;逐项点击精确直达。

## 5. 分期
- **v1(本 spec)**:REST 快照 + SSE 实时推送 + 精确 deep-link(报告→大纲编辑器 / 治理三面 / 索引区)+ 索引完成 toast + **跨会话补发(内存缓冲)**。
- **v2**:完成提示的**持久化**(`notifications` 表,后端重启不丢 + 通知历史)、报告生成完成等更多 `event` 类型、治理项点击落到**具体某条候选**、WebSocket(若需双向)。

## 6. 效率账
- **事件驱动 vs 轮询**:轮询 = 每用户每 ~45s 全量查询(不管有无变化);本方案 = **空闲零查询**,仅在有 job 完成时 `mark_dirty` 触发一次重算 + 推送。多用户生产环境净省。
- SSE 空闲连接 = 一个挂起协程 + 一个 `asyncio.Queue`,近零 CPU/内存;keepalive 15s 一帧。
- 每次重算:report_outline 1 查 + governance 3 聚合查 + index(building 走内存集免费、stale 仅 eligible 库、`scale_index_status` 有缓存)。全走索引、无 LLM/embed。
- 跨会话缓冲:内存 append + 重连一次 flush,零 DB。

## 7. 风险/已核实 → 结论
- ✅ **单进程**:uvicorn 无 `--workers` → 进程内事件总线成立。
- ✅ **SSE 认证**:auth=Bearer,`EventSource` 不能带 header,但复用 `fetch+getReader+Bearer`(ask_stream 现范式)绕开。
- ✅ **治理 pending 判据**:merge=`status='pending'`、edge=`review_status='pending'`、promotion=`status IN('proposed','under_review')`;三表均有 `notebook_id`。中心计数与队列页一致。
- ✅ **治理精确 deep-link**:三面异构——合并=KG 视图 `kgViewOpen`、边审=`openEdgeReviewQueue()`、晋升=`openPromoQueue()`;直接调各自 opener 即精确,无需给弹窗加 tab。
- ✅ **index 分类**:用 `scale_index_status().state` 单字段。
- ✅ **我的库口径**:严格 `created_by=?`(不含分享只读)。
- ⚠ **跨线程投递正确性**:job 线程 → asyncio loop 用 `call_soon_threadsafe`;连接注册/注销与 fan-out 在 loop 内串行,避免竞态。plan 明确。
- ⚠ **晋升全局/admin**:count 按我的 notebook 过滤,deep-link 到全局 promo 模态;plan 视非 admin 可见性决定是否隐藏晋升项。
- ⚠ **索引精确落点**:scale 索引状态/重建入口在 KG 视图内具体位置,plan 定位并滚动;兜底=进 KG 视图即可见。
- ⚠ **后端重启丢完成事件**:内存缓冲的唯一窗口;论证对索引场景近零损失(构建随进程死);强持久留 v2。
