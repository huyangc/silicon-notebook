# 索引与构建统一整合(正名 + 聚合状态 + 取消)设计

**日期**: 2026-07-09
**分支**: feat/index-build-consolidation(新 worktree,off origin/master)
**状态**: 大方向 + 命名 + 面板位置已确认(用户「同意」完整整合重构)

## 背景

一个知识库有多个「构建/索引」系统,当前散落、命名冲突、无法取消,用户看不懂:
- **三个用户可感知的构建系统**命名冲突:概念合并(unified KG)与检索索引(scale index)在同一行**都渲染字符串「已同步」**,指两个毫不相干的东西;「上次重建·N前」只属于概念合并,却让人以为是检索索引。
- **入口散落**:检索索引重建可从 5 处触发(3 种确认行为);KG 抽取 5 处;概念合并同面板 2 处(确认不一致);4 条独立轮询循环,无统一「有东西在建」信号。
- **无取消**:「检索索引:已排队(空闲时建)」= 空闲队列(凌晨 2–6 点),全代码库无任何取消/出队路径;且「已排队」状态不触发轮询刷新(可能早建完仍显示排队)。

现状证据见本会话两份代码地图(后端 6 系统 × 触发/状态/可取消;前端 16 控件 × 7 面)。

## 三个系统(正名 + 统一状态词表)

| 系统 | 正名 | 状态词(各系统独立一套,同屏绝不重名) | 时间戳 | 动作 |
|---|---|---|---|---|
| KG 抽取(knowledge_objects) | **知识图谱** | 未建 / 就绪 / 抽取中 / N 篇待抽 | —(用 kg_building) | 构建·完整重抽·补连孤立 |
| 跨文档概念合并(concept_clusters + 派生层) | **概念合并** | 最新 / 待重建 / 重建中 | 上次 N 前(last_rebuild_at,已有) | 重新合并 |
| 检索 ANN 索引(scale index) | **检索索引** | 未建 / 最新 / 待更新 / 排队中 / 构建中 | 上次 N 前(**本次新增 last_built_at**) | 构建·全量重建·**取消** |

**不变量**:「已同步」字符串退役;每系统用自己的状态词;同一屏永不出现两个相同状态词指不同系统。viz 索引、FTS、embedding 仍为不可见内部件,不进面板(除非未来需要)。

## 架构

### 后端(先做,可独立测)

**A. 聚合状态端点** `GET /notebooks/{id}/index-status` → 一次返回三系统状态:
```
{
  "kg": {"ready": bool, "building": bool, "pending_sources": int},
  "unified_kg": {"dirty": bool, "building": bool, "last_rebuild_at": str},
  "scale_index": {"state": "unindexed|suggested|queued|building|indexed|stale",
                  "exists": bool, "stale": bool, "unindexed_sources": int,
                  "last_built_at": str, "eligible": bool}
}
```
- 复用既有 `scale_index_status` / `unified_kg_status` / NotebookSummary 的 kg 字段,聚合成一个 payload;**旧端点保留兼容**(不删)。
- 单一职责:纯读、无副作用(不触发 viz build——沿用 `_viz_index_probe` 只读语义)。
- 效率:三系统状态本已各自廉价(scale 版本探针 O(1)、unified COUNT/dirty、kg summary),聚合只是合并成一次 HTTP,减少前端 4 条轮询为 1 条。

**B. 取消/出队** `POST /notebooks/{id}/scale-index/cancel`:
- state=queued → 从 `_scale_idle_queue` 移除该 notebook(新增 `_dequeue_scale_idle(notebook_id)`,加锁,幂等)。返回新状态。
- state=building → **本轮不做真打断**(fire-and-forget 守护线程无句柄);返回明确信号 `{"cancelled": false, "reason": "building_not_interruptible"}`,前端提示「正在构建,无法取消;完成后会自动更新」。真打断(协作式,镜像 ask-job cancel)列为**非目标/后续**。
- 其它 state → no-op 幂等返回。

**C. 检索索引 last_built_at**:`build_scale_index` / `fold_scale_index_delta` 写 manifest 时加 `built_at`(_now());`scale_index_status` 与聚合端点透出。旧 manifest 无该键 → 显示「—」不报错。

**D. 修「已排队不刷新」**:前端轮询恢复条件含 queued(见前端 E);后端无需改(状态已对)。

### 前端(与后端同 PR co-design)

**E. 一个「索引与构建」面板**(改造现有「知识分析看板」弹窗,不新开面):
- 三行,每行 `[正名] [状态 chip · 时间戳] [动作按钮…]`,**统一一种确认行为**(统一走一个 `confirmIndexAction(kind, mode)` → 一致的确认弹窗文案模板)。
- 消费 **聚合端点**,一条轮询(替代 4 条);轮询恢复条件覆盖任一系统 building **或 scale queued**(修 bug)。
- 「检索索引」行在 state∈{queued,building} 时显示**取消按钮** → `POST /scale-index/cancel`;building 时点取消给「无法取消」提示。
- 状态词表按上表落地;「已同步」全部替换为各系统状态词。

**F. 就地 CTA 保留但统一**:降级答案「构建索引」、严格推理提示「构建知识图谱」、来源栏「补抽 N 篇」保留(上下文有用),但:①统一到同一动作函数 + 同一确认;②可点进面板看全貌。散落的**重复管理入口**(admin 动作列表里的两条、看板卡片旧按钮)收敛进新面板。

**G. 命名/状态词落地**:`scale-index.tsx` 的 `STATE_LABELS`、KG 视图 rail 的 tag 文案、看板卡片文案统一到一处词表常量,避免再散落。

## 数据流

```
面板打开/轮询 → GET /index-status(一次)→ 三系统状态渲染三行
点「重新合并」→ 统一确认 → POST /unified-kg/rebuild → 轮询到 unified_kg.building→最新
点「全量重建」→ 统一确认 → POST /scale-index/rebuild{when:now} → 轮询 building→最新(带 last_built_at)
排队中点「取消」→ POST /scale-index/cancel → 出队 → 轮询到 unindexed/suggested
```

## 组件边界

- 后端 `index_status(nb) -> dict`:聚合三个已有 status 读取,纯函数式,独立可测。
- 后端 `_dequeue_scale_idle(nb) -> bool`:队列移除,加锁,幂等,独立可测。
- 后端 `cancel_scale_index(nb) -> dict`:按 state 分派(dequeue / building-refuse / noop),独立可测。
- manifest `built_at`:build/fold 写、status 读,一个字段。
- 前端 `IndexBuildPanel`:消费聚合端点 + 三行渲染 + 统一确认 + 取消;一个组件。
- 前端 `INDEX_STATE_LABELS` 词表常量:命名/状态词单一真相源。

## 测试

**后端**:
1. `index_status` 聚合:三系统字段齐全、值与各自旧 status 一致;纯读不触发 viz build(spy `_spawn_viz_build` 不被调)。
2. `cancel`:queued→出队(state 转 unindexed/suggested)+ 返回 cancelled=true;building→cancelled=false/reason;无队列项→幂等。
3. `_dequeue_scale_idle`:移除存在项返 True、不存在返 False、并发安全(加锁)。
4. `built_at`:build 后 manifest 有 built_at 且 status 透出;旧 manifest 缺键→status built_at="" 不报错。
5. 路由:`GET /index-status`、`POST /scale-index/cancel` 存在 + 权限(require_notebook_access 同 scale-index/rebuild)。

**前端**:tsc clean;弯引号自查=0;面板渲染三行 + 状态词正确 + 取消按钮仅 queued/building 显示 + 点取消调 cancel 端点;轮询恢复覆盖 queued。真机视觉验证(面板对齐/状态切换)。

## 非目标(YAGNI)

- 不做「打断正在构建的索引」(协作式 cancel,后续;本轮 building 明确拒绝取消)。
- 不把 viz/FTS/embedding 提到面板(仍为内部件)。
- 不删旧的单系统 status 端点(保留兼容;前端改用聚合端点)。
- 不改三个系统各自的构建算法/触发时机(只统一入口与呈现 + 加取消 + 加时间戳)。
- 不动检索索引的缓存/门控(#178/#185 已定)。
- 不重命名后端表/字段(只改用户可见的前端文案 + 新增聚合端点)。

## 交付

一个 spec + 一个 plan + 一个 PR(前后端 co-design)。plan 先排后端(A 聚合端点 / B 取消 / C 时间戳 / 路由),再排前端(E 面板 / F CTA 统一 / G 词表 / 轮询修),末尾全量验证 + PR。子代理逐任务实现 + 逐任务对抗审查 + 终审。
