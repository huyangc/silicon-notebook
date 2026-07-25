# Knowhow 表批量规整并发、逐项审阅、审计 actor 与内容感知列宽设计

- 日期：2026-07-25
- 状态：**已批准（2026-07-25）**
- 性质：已获用户批准的产品与工程设计规格；实施与验收以本文推荐方案和推荐默认值为准
- 关联设计：
  - `docs/superpowers/specs/2026-07-18-knowhow-cell-markdown-normalization-design.md`
  - `docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md`
  - `docs/superpowers/specs/2026-07-15-knowhow-tables-design.md`
  - `AGENTS.md` 的 Knowhow、模型调度、全栈一致性和测试约束

> 批准记录：用户于 2026-07-25 明确批准本文全部推荐方案与推荐默认值，并授权开始完整实施。任何偏离本文已批准产品语义的变更仍需重新评审。

---

## 1. 背景

当前 Knowhow 表已经具备格子 Markdown 规整、整行/整表批量规整、合并共享格保存守卫、版本流水、单格历史、代码附件和智能补全空列。此次需求不是重新设计这些能力，而是补齐四处体验和治理短板：

1. 批量“一键规整”的候选生成在 `frontend/app/knowhow-panel.tsx` 的 `KnowhowReformatBatchModal.runBatch()` 中按物理格子串行 `await`。现有 `reformatDedupeKey(columnId, originalMd.trim())` 与批次内成功缓存已经避免相同输入的重复调用，但不同输入仍无法并行。
2. 批量队列目前只显示格子 label 与状态；所有原文/候选 Markdown 预览集中堆在弹窗底部。它不是行级增删 + 行内 token 高亮的原始 Markdown diff，保存成功后也不能从结果项直接打开格子。
3. session 用户的 Knowhow 写路径大量把 `user.id` 同时当稳定身份和可读审计 actor。Agent 路径已经使用 `profile_name`。历史时间线、单格历史和代码附件来源因此常显示用户 id，而不是 username。
4. 主网格 `.knowhow-grid-table` 使用 `table-layout: fixed`，所有 `th/td` 约 `200px`。短列浪费空间，步骤/长文本列又过窄。

“根据其他格子补齐当前格子”已经存在：行详情与矩阵分支都提供“智能补全空列”。本设计只钉住它的回归契约，**零新增功能**。

## 2. 现状核查映射

### 2.1 批量规整

- `frontend/app/knowhow-panel.tsx`
  - `KnowhowReformatBatchModal`
  - `runBatch()`：静态 `batch.items` 上串行 `for...of + await`
  - `runSave()`：按 `planReformatSaves()` 规划保存单元，串行保存
  - `snapshotRef`：弹窗挂载时冻结完整 `allRows + anchorColumnId`
  - `pendingReloadRef`：运行/保存 stale 只记标志，弹窗卸载后再刷新父表
  - `abortedRef`、`mountedRef`：中止后停止发新请求，并丢弃迟到结果
- `frontend/app/knowhow-optimize-logic.ts`
  - `ReformatBatchState` / `ReformatBatchItem` / `ReformatBatchCellStatus`
  - `reformatDedupeKey()`：`JSON.stringify([columnId, originalMd.trim()])`
  - `applyReformatCachedResult()`：候选复用，但按目标物理格子的原文重算 `changed`
  - `planReformatSaves()`：共享格保存等价类、代表项、完整写目标和 anchor group 覆盖判定
  - `reformatResultIsStale()` / `isReformatUnitStale()`：逐字而非 trim 的 stale 判定
  - `beginReformatBatchSave()`：人工整体确认是进入保存阶段的唯一入口
- `frontend/app/knowhow-model.ts`
  - `reformatKnowhowCell()` 调用既有 `POST .../reformat`，返回 `candidateMd/source/changed/sourceMd`
- `backend/app/services/knowhow/api.py`
  - `reformat_cell()` 解析 `knowhow_reformat` workload；LLM 成功且过内容不变式才采用，否则走规则兜底；只产建议、不写库
- `backend/app/services/model_registry.py` / `model_provider.py` / `model_work.py`
  - `knowhow_reformat` 是 `interactive` workload
  - 物理服务 `max_concurrency`、总队列、per-actor 队列、公平调度、deadline 和 breaker 仍由 `ServiceScheduler` 权威控制
  - `GET /api/model-services/status` 已能返回 workload 到物理服务的绑定及 `maximum/active/queued`

### 2.2 保存完整性

以下约束已经存在，任何并发改造都不得削弱：

- 批次从挂载到结束使用同一份 frozen table snapshot。
- 候选生成响应的 `source_md` 必须与该物理格子的 `originalMd` 逐字相等；否则运行阶段标 stale，不进入成功缓存。
- 保存前可做一次整表预检，但权威校验仍在服务端事务内。
- 每个保存单元下发完整 `expected_before`；共享/合并格下发冻结的 `target_row_ids`。
- 完整 anchor group 保存还必须验证冻结成员、每行 anchor 内容及当前精确成员集合；漂移返回 409。
- 409 不是普通保存失败：候选保留、状态为 stale、不得覆盖新内容，并允许基于刷新后的新快照重跑。
- stale 期间不立即 reload；先安全结束并关闭批量弹窗，卸载后再 reload。
- 保存仍按既有保存单元串行执行；本设计只并发候选生成。
- `knowhow_store` 的 15 条用户内容写路径必须在同一事务最后写 `knowhow_changes`，其 fingerprint 必须匹配事务后的表状态。

### 2.3 审计与展示

- `backend/app/api/knowhow_routes.py` 的 session 写路径普遍传 `actor=user.id`。
- `backend/app/api/deps.py` 的 `RequestActor.actor_label` 对 Agent 使用 `profile_name`，对 session 用户仍使用 `user.id`。
- `backend/app/services/knowhow/api.py::put_cell_code()` 当前把 `updated_by` 同时传给 `knowhow_cell_code.updated_by` 与 `knowhow_changes.actor`。
- `frontend/app/knowhow-history-drawer.tsx` 直接显示 `change.actor`。
- `frontend/app/knowhow-cell-history.tsx` 直接显示 `entry.actor`。
- `frontend/app/knowhow-code.tsx` 经 `codeProvenanceSuffix(code.updatedBy)` 显示代码最近更新者。
- `frontend/app/knowhow-model.ts` 已把 wire 的 `actor`、`created_by`、`updated_by` 映射为字符串；无需为了可读 label 改字段类型。
- `knowhow_tables.created_by` 是稳定身份 id；`knowhow_changes.actor`、`knowhow_milestones.created_by`、`knowhow_cell_code.updated_by` 是审计快照文本，语义不能再混用。

### 2.4 智能补全空列

- `frontend/app/knowhow-complete-logic.ts::completableKnowhowColumns()` 只选择存储值逐字等于 `""` 的非 anchor 列。
- `completionSavePlan()` 冻结写目标、每行空串基线和完整组 anchor 守卫；共享格只有组内 raw 值全部精确为空时才按整组保存。
- `KnowhowRowCompletionModal` 只生成建议，逐项接受；保存 origin 为 `llm_complete`。
- 后端补全使用至多 8 条同表参考、有界 reasoning 检索，返回最多 20 项；已有内容和纯空白内容都不得被覆盖。

### 2.5 主表布局

- `KnowhowGridView` 通过 `orderedColumns`、`filteredRows` 和可选 `gridDisplayRows` 渲染记录型表或 anchor 合并矩阵。
- `.knowhow-grid-scroll` 已提供横向/纵向滚动。
- `.knowhow-grid-table` 已固定布局；首列和表头 sticky。
- 当前没有 `<colgroup>`，所有普通列和同步状态列共享约 200px 的单元格宽度规则。

---

## 3. 目标

1. 只把批量规整的候选生成改为有界并发，明显缩短不同输入较多时的等待时间，同时不越过模型调度器的容量、公平性和 breaker。
2. 让每个有候选的队列项可在同一批量弹窗内查看真正的原始 Markdown diff，并从已保存项安全打开现有格子详情。
3. 把稳定身份 id 与可读审计 label 明确分层；新 session 写入显示 username，历史 id 在读时有界解析，且不破坏 fingerprint 链。
4. 用内容感知但稳定、有界的列宽替代统一 200px；保持固定布局、横向滚动和首列 sticky。
5. 为现有智能补全空列增加明确的回归保护，不新增第二套补全入口或逻辑。

## 4. 非目标

- 不并发保存，不把整体确认改成逐项 accept/reject。
- 不新增批量规整后端 job、批量 reformat endpoint 或模型取消 endpoint。
- 不改变 `reformat_cell()` 的 prompt、内容不变式、规则兜底和 source 枚举。
- 不修改 `ServiceScheduler` 的容量、公平性、deadline 或 breaker。
- 不把历史 actor 改造成外键，不批量重写历史表，不做 schema migration。
- 不重命名现有 wire 字段。
- 不给列宽增加拖拽、用户持久化、服务端存储或跨设备同步。
- 不新增“智能补全空列”能力，不把纯空白当空格子。
- 不修改 README、README_zh、AGENTS.md、CLAUDE.md 或 `fangan_done.md`；本文只是 proposal。

---

## 5. 用户流程

### 5.1 批量规整

1. 用户点击“一键规整整行/整表”。弹窗仍先显示物理格子总数和可能使用 AI 的提示。
2. 用户点击“开始规整”。前端按 dedupe key 建立工作组，用有界 worker pool 并发生成候选。
3. 进度始终按物理格子计数。一个成功结果扇出给 N 个重复格时，进度可一次增加 N；不得改成“唯一请求数”。
4. 用户可点“中止”。已完成候选保留；未开始和在途项进入中止终态；迟到响应不得复活它们。
5. 进入 reviewing 后，队列项显示状态；有候选的项显示“查看改动”。点击后，批量弹窗正文切换为单项详情，顶部提供“返回批量结果”。不再叠第二个全局模态框。
6. 用户仍只进行一次“确认保存”。保存按现有 `ReformatSaveUnit` 串行执行。
7. 已保存项提供“打开格子”。系统先卸载批量弹窗，再调用现有格子详情入口。
8. stale 项保留候选供本次查看，但不得保存。用户选择“基于最新内容重新规整”时，先关闭弹窗、消费 pending reload、取得新 detail，再创建全新批次；旧候选不自动 rebase。

### 5.2 审计展示

1. 新 session 用户写入后，时间线、单格历史、里程碑创建者和代码来源优先显示 username。
2. 旧流水若存的是当前仍存在的 user id，读 API 将其解析成 username；删除/未知用户原样显示旧值。
3. Agent 继续显示 profile_name，不转换成用户 username。
4. 权限、owner、目标笔记本访问和 `KnowhowTable.created_by` 仍只使用稳定 user id。

### 5.3 内容感知列宽

1. 打开表或改变过滤条件后，系统只采样有界数量的可见物理行，估算每列最长可见行。
2. 列宽通过 `<colgroup>` 一次性应用；表仍为 fixed layout，不因单个 cell 异步撑开。
3. 总宽度超过容器时继续横向滚动；首列和表头 sticky 语义不变。

---

## 6. A：批量规整候选生成的有界并发

### 6.1 并发上限

推荐定义两个值：

```ts
const REFORMAT_GENERATION_PRODUCT_CEILING = 3;
const REFORMAT_GENERATION_FALLBACK = 2;
```

批次开始时读取一次已有 `GET /model-services/status` 快照，找到 `workloads[].id === "knowhow_reformat"` 的唯一服务：

```ts
effective = clamp(service.maximum, 1, REFORMAT_GENERATION_PRODUCT_CEILING)
```

以下任一情况使用 fallback=2：状态请求失败、响应无效、workload 未绑定、重复绑定、`maximum <= 0`。fallback 仍受产品 ceiling 限制。

这个数只是单个浏览器批次的发压上限，不是容量预留，也不替代后端调度器。多个浏览器/用户同时运行时，物理服务的 `max_concurrency`、总队列 `10×`、per-actor 队列 `2×`、优先级公平性、30 秒 interactive deadline 与 breaker 仍由 `ServiceScheduler` 决定。前端不得根据 `active` 猜测“剩余槽位”，因为那会在快照到请求之间产生竞态。

无界 `Promise.all(items.map(...))` 明确禁止。

### 6.2 工作队列和 single-flight

候选生成按物理项建状态，按 dedupe key 建工作组：

```ts
type ReformatWorkGroup = {
  key: string;
  pendingMembers: ReformatBatchItem[];
  inFlight: boolean;
  cached?: SuccessfulFreshResult;
};
```

调度器同时最多激活 `effective` 个**不同 key**。同一 key 任意时刻最多一个请求在途。

每组算法：

1. 取第一个 pending member 作为本次 leader，标 running，发其真实 row/column 请求。
2. 成功后先做 leader 的 `sourceMd === originalMd` 逐字校验。
3. 只有 HTTP/解析成功且未 stale，才把结果写入该批次缓存，并通过 `applyReformatCachedResult()` 扇出给该组其余仍 pending 的物理项；每个目标仍按自己的原文重算 `changed`。
4. leader 请求失败：只把 leader 标为 `reformat_error`；不得把失败写进缓存，也不得把同组 followers 一起判失败。该组重新入队，下一 member 可成为新 leader 重试。
5. leader stale：只把 leader标为 `stale_skipped`；不得缓存或扇出。下一 member 可独立尝试。
6. 所有 member 都失败/stale 才结束该组。

这同时满足 single-flight 和“不让代表请求污染整个等价类”。成功且 fresh 才有批次内复用；失败、stale、中止结果一律不缓存。

### 6.3 伪代码

```ts
async function runGenerationPool(state, groups, limit, runEpoch) {
  const ready = groups.slice();
  let active = 0;

  return new Promise<void>((resolve) => {
    const pump = () => {
      if (!isCurrent(runEpoch) || abortedRef.current) return settleAbort(resolve);
      while (active < limit && ready.length > 0) {
        const group = ready.shift()!;
        if (group.cached) {
          fanOutFreshResult(group);
          continue;
        }
        const leader = group.pendingMembers.shift();
        if (!leader) continue;
        active += 1;
        runLeader(group, leader, runEpoch)
          .then((outcome) => {
            if (!isCurrent(runEpoch)) return;
            if (outcome.kind === "fresh_success") {
              group.cached = outcome.result;
              fanOutFreshResult(group);
            } else {
              settleLeaderOnly(leader, outcome);
              if (group.pendingMembers.length > 0) ready.push(group);
            }
          })
          .finally(() => {
            active -= 1;
            if (active === 0 && ready.length === 0) resolve();
            else pump();
          });
      }
    };
    pump();
  });
}
```

实现可用显式 worker 循环或上述 pump；必须满足最大在途数可静态/行为测试，不能通过一次性创建 N 个 promise 再用 semaphore 包住来制造大量待决闭包。

### 6.4 取消、关闭与迟到响应

- 每次 generation run 持有单调 `runEpoch` 和每个在途请求的 `AbortController`。
- `reformatKnowhowCell()` 增加可选 `signal?: AbortSignal`，透传到现有 `requestJson()`。
- “中止”立即：递增 epoch、停止出队、abort 所有在途 fetch、把 running/pending 物理项结算为 `aborted`，保留已完成的 changed/unchanged/error/stale。
- fetch abort 只保证客户端不再等待/接收。现有同步 reformat HTTP 端点没有 durable job/cancel API；服务端模型调用可能继续到自然结束并暂占调度器槽位。UI 不得宣称“模型已在服务端终止”。
- 任何响应在 reducer 前同时检查 `mountedRef`、runEpoch、顶层 phase 和 item 当前状态。关闭、中止或新一轮运行之后的迟到响应一律 no-op。
- running/saving 时 X、背景和 Esc 保持不可直接关闭；用户须先“中止”生成。saving 不可中止，避免用户误以为已提交的前半批被回滚。
- modal 卸载时 abort 尚存 controller，并按既有 `pendingReloadRef` 恰一次 reload。

### 6.5 部分失败与重跑

- 单项失败不阻断其他 key 或其他 member。
- reviewing 阶段提供“重试未完成项”，只重试 `reformat_error/aborted/pending`，复用同一 frozen snapshot；已成功缓存可继续复用。
- `stale_skipped` 不能在旧 snapshot 内重试。其动作是“关闭、刷新并重新规整”：先卸载 modal，后 reload，再新建 batch。
- save 409 后候选仍留在 item 中供查看 diff；状态为 stale，不进入再次保存。
- 保存失败 `save_error` 可保留现有候选。若提供“重试保存”，仍必须重新做 fresh table preflight，并重新发送原 `expected_before/target_row_ids/anchor_guard`；推荐首版不加此按钮，保持关闭后重跑的单一路径。

### 6.6 状态机调整

顶层 phase 保持：

```text
idle → running → reviewing → saving → done
                  ↑
              retry generation
```

物理 item 可继续使用现有状态集合。建议把“本轮运行 id”和“当前单项详情 id”放在组件局部状态，不持久化到 domain state；不要改变人工整体确认是进入 saving 的唯一边。

---

## 7. B：逐项 Markdown diff 与“打开格子”

### 7.1 同一模态框内导航

`KnowhowReformatBatchModal` 增加局部 view：

```ts
type BatchView =
  | { kind: "queue" }
  | { kind: "item"; rowId: string; columnId: string; tab: "diff" | "preview" };
```

- `changed/saving/saved/save_error/stale_skipped` 且有 `candidateMd` 的项显示“查看改动”。
- `unchanged/reformat_error/aborted/pending` 不显示该按钮。
- item view 复用同一 card、header、focus trap、拖动和 resize；breadcrumb 为“批量结果 › 行名 · 列名”。
- “返回批量结果”恢复 queue 的滚动位置和焦点。
- 不创建第二个 portal/global modal，不让 Esc 同时关闭两层。

### 7.2 原始 Markdown diff 为主

默认 tab 是“Markdown 改动”，以 `white-space: pre-wrap` 显示原始文本：

- 未改行：中性上下文。
- 删除行：`-`、红色背景；新增行：`+`、绿色背景。
- 相邻删除/新增行在安全配对后进行行内 token diff：删除 token 红色强调，新增 token 绿色强调。
- 空白差异必须可见：保留实际空白布局，并用底色/下划线标识空白 token；tab 可显示轻量 `⇥` 辅助标记，但复制内容仍是原文。
- “渲染预览”作为次级 tab，沿用现有两列 `KnowhowMarkdown` before/after。它不能替代原始 diff。

### 7.3 token 规则

tokenizer 必须确定性覆盖混合中英文：

1. 连续 ASCII 字母/数字/下划线为一个 token。
2. 连续空格或 tab 为一个 whitespace token；换行只属于行级 diff。
3. CJK、全角字符和 emoji 按 Unicode grapheme 切分；优先 `Intl.Segmenter(granularity="grapheme")`，不可用时回退 `Array.from()`。
4. 标点和 Markdown 控制符分别作为 token；连续相同围栏符可合并为一个 token，例如 ````` ``、`***`。
5. 不解析/改写 Markdown；diff 输入始终是存储原文和候选原文。

### 7.4 有界算法与降级

新增纯逻辑模块建议为 `frontend/app/knowhow-markdown-diff.ts`，禁止在 JSX render 内写临时二次复杂度算法。

推荐预算：

| 层级 | 正常路径上限 | 超限行为 |
|---|---:|---|
| 单侧字符 | 20,000 UTF-16 code units | 线性 prefix/suffix 降级 |
| 单侧行数 | 400 | 线性 prefix/suffix 降级 |
| 行级 Myers 步数 | 200,000 | 中止精细 diff，整个变化中段按删除/新增块显示 |
| 单行 token | 256 | 该行只做整行高亮 |
| 单行 token 矩阵 | 65,536 cells | 该行只做整行高亮 |
| 单项所有行内预算 | 250,000 cells | 剩余行只做整行高亮 |

流程：

1. 仅在用户打开某项时计算，不为列表中所有候选预计算。
2. 先剥离共同前缀/后缀行。
3. 在预算内运行有步数上限的 Myers 行 diff。
4. 对相邻 delete/add block 做有界配对，再对配对行做 token LCS/Myers。
5. 任一预算耗尽立即降级为“共同前后文 + 整段删除 + 整段新增”，复杂度保持线性。
6. 结果按 `(rowId,columnId,originalMd,candidateMd)` 在 modal 生命周期内 memo；关闭即释放。

不得引入没有硬上限的 `O(lines_before × lines_after)` 或 `O(tokens_before × tokens_after)` 路径。

### 7.5 “打开格子”

- 只对 `saved` 项显示“打开格子”；`save_error/stale` 不显示。
- 非共享保存单元：打开该 item 自己的 `(rowId,columnId)`。
- 共享/合并保存单元：打开 canonical representative，规则固定为 `writeTargets` 中 `row.position` 最小者，若 position 相同再按 `rowId` 字典序；列仍为当前 `columnId`。这与主网格 rowSpan 的首个可见物理格一致，避免打开被合并隐藏的兄弟格。
- `planReformatSaves()` 应显式产出 `representativeRowId`，不要依赖数组碰巧有序。
- 点击后流程：记录 pending target → 关闭并卸载批量 modal → 如有 stale pending reload 则先 await reload → 清除不匹配的表内过滤条件（如会遮住目标）→ 调用现有 `onOpenCell(rowId,columnId)`。不得在两个 modal 同时存在时直接打开。
- 若目标行/列在关闭与打开之间已删除，使用现有“格子定位不合法/重新加载”错误通道，不创建幽灵详情。

### 7.6 不改变确认范围

“查看改动”是阅读能力，不是逐项审核状态。首版明确不增加：

- 单项接受/拒绝 checkbox；
- 只保存选中项；
- 修改候选正文；
- 从 diff 直接保存。

人工闸仍是“对所有 changed 项一次性确认保存”。

---

## 8. C：username 审计与 actor 治理

### 8.1 两类字段的强语义

| 类别 | 示例 | 存储值 | 用途 |
|---|---|---|---|
| 稳定身份 id | `notebooks.owner_id`、`knowhow_tables.created_by`、权限检查参数、复制目标 owner | `user.id` | 权限、归属、关联、稳定比较 |
| 可读审计快照 label | `knowhow_changes.actor`、`knowhow_milestones.created_by`、`knowhow_cell_code.updated_by` | username/display/profile 快照文本 | 向用户解释“谁做的” |

任何实现不得通过全局替换 `user.id → username` 完成。特别是复制、导入、转移、整本 notebook 拷贝等当前复用 `actor/creator` 参数的路径，必须拆成 identity id 和 audit label 两个命名明确的参数。

### 8.2 session label 规则

新增一个后端单一真源 helper，建议放在中性模块 `backend/app/core/audit_actor.py`：

```python
@dataclass(frozen=True)
class AuditPrincipal:
    identity_id: str
    audit_label: str

def session_audit_principal(user: UserProfile) -> AuditPrincipal:
    label = user.username.strip() or user.display_name.strip() or user.id
    return AuditPrincipal(identity_id=user.id, audit_label=label)
```

规则固定为：`username.trim()` 优先，其次 `display_name.trim()`，最后 `user.id`。不得在不同 route 各写一份 fallback。

Agent 继续使用 `principal.profile_name` 作为 audit label，owner/权限仍使用 `principal.owner_id`。`RequestActor` 可直接持有 `identity_id + actor_label + is_agent`，session 分支也调用同一 helper。

### 8.3 写路径治理

`backend/app/api/knowhow_routes.py` 的所有普通 Knowhow 写路径都先解析一次 principal：

- import table / create table
- table meta / anchor set
- column add / rename / kind / delete
- row add / delete
- single cell / batch cell / guarded group cell
- append import
- revert
- milestone create
- single-table copy/move

传参规则：

```python
principal.identity_id  # 权限、owner、created_by
principal.audit_label  # actor、milestone created_by、updated_by
```

需要拆参的真实路径：

- `knowhow_api.import_table()` / `create_table()`：从一个 `actor` 拆为 `created_by_id` 与 `actor_label`；store 的 `created_by` 收前者，创世 change 的 `actor` 收后者。
- `services/knowhow/transfer.py::_remap/copy_table/move_table/transfer_table`：目标 `knowhow_tables.created_by` 使用 `creator_id`；目标创世流水使用 `actor_label`。
- `services/notebook_sharing.py` 与 SQLite/PostgreSQL `seed_copied_knowhow_genesis()`：新 notebook/表 owner id 与创世流水 actor label 分开传递。
- `knowhow_api.put_cell_code()`：保留 `updated_by=actor_label`，change.actor 同 label；不得再用 identity id。
- `create_knowhow_milestone()`：`created_by` 存 label；它不是权限外键。

`backend/app/repositories/sqlite/knowhow_store.py` 与 PostgreSQL mirror 的 15 条写路径继续只接显式 `actor` 文本，store 不读 ContextVar。actor 的正确解析属于 API/service 边界。

### 8.4 历史读时解析

不做破坏性迁移。新增一个有界批量 resolver：

```python
resolve_audit_labels(candidates: Collection[str], *, limit=512) -> dict[str, str]
```

语义：

1. 去空、去重，最多取 512 个候选；超出部分原样回退。
2. SQLite 以固定最多 200 个 id 一组 `WHERE id IN (...)`，最多 3 次查询；PostgreSQL 用等价 bounded `ANY/IN`。严禁逐 actor 查询。
3. 命中用户：返回该用户当前 `username.trim() or display_name.trim() or id`。
4. 未命中、已删除用户、未知文本：不改。
5. `origin == "agent"` 的 change.actor 明确跳过用户解析，原样保留 profile_name。
6. 对缺少 origin 的历史 `updated_by`，只在精确命中 live `users.id` 时解析；普通 Agent 自由文本通常不命中并原样回退。极端情况下 Agent 名与 live user id 完全相同，旧 schema 无法无歧义区分；见评审项。

读 API 以一次响应为批量单位解析：

- `GET .../history`：本页 `changes.actor` + 全量 `milestones.created_by` 一次收集解析。
- `GET .../history/{seq}`：top-level actor；payload 中当前会展示的 code `updated_by` 可同批解析。
- `GET .../cells/.../history`：所有 entry.actor 一次解析。
- 行级 code API：所有 `updated_by` 一次解析。
- history diff：若前端展示 code updated_by，则同一响应批量解析；未展示字段仍保持兼容。

建议在 service/query 层组装展示响应，不让 SQLite/PostgreSQL product store 互相导入 identity store。若新增 facade 查询成员，必须保持一跳委托和 backend-neutral mirror。

### 8.5 `knowhow_cell_code.updated_by` 与 fingerprint

`updated_by` 是 `_FINGERPRINT_SQL` 的组成部分。因此：

- 禁止为了显示 username 批量 UPDATE 存量 `updated_by`。
- 禁止在读取时把解析后的 label 写回数据库。
- 新 code 写入直接存新规则得到的 audit label；这是一次真实用户写操作，fingerprint 变化和新的 `cell_code_put` change 同事务记录，合法且可回退。
- 旧附件若未再次编辑，库中 id 永久保持原样，fingerprint 链不变；API 只在响应投影里显示解析后的 username。
- 历史 payload 中的旧 `updated_by` 同样不改，只做响应期展示解析。

### 8.6 wire 兼容策略

优先保持现有字段名和字符串类型：

- `actor` 仍叫 `actor`
- `created_by` 仍叫 `created_by`
- `updated_by` 仍叫 `updated_by`

它们在审计型响应中的语义明确为“可显示 label”，不再承诺是稳定 id。现有前端 TypeScript 类型无需破坏性改名，只补充注释和回归测试。

不推荐首版同时增加 `actor_id/actor_label` 双字段：历史数据未必能恢复 actor id，双字段会制造大量 nullable/unknown 分支。若未来要做强审计主体关联，应独立设计 schema migration 和 actor kind，而不是在本改动中半实现。

### 8.7 需要复核的 actor 展示面

- 表历史时间线：`frontend/app/knowhow-history-drawer.tsx`
- 单格历史：`frontend/app/knowhow-cell-history.tsx`
- 代码附件“最近更新 · 来自”：`frontend/app/knowhow-code.tsx` / `knowhow-code-logic.ts`
- 里程碑标记或详情若新增创建者展示：`KnowhowMilestone.createdBy`
- 单条 change、两版 diff、行/列删除快照中的 code `updated_by`
- Agent HTTP 与 MCP 的 row detail code `updated_by`
- 复制/移动/整本复制产生的目标表创世流水 actor
- 回退产生的 change.actor

---

## 9. D：内容感知列宽

### 9.1 结构

在 `KnowhowGridView` 中新增纯函数模块，建议 `frontend/app/knowhow-column-widths.ts`：

```ts
type ColumnWidth = { columnId: string; widthPx: number };

computeKnowhowColumnWidths({
  columns,
  visibleRowsSample,
  anchorColumnId,
  compact,
}): { columns: ColumnWidth[]; statusWidthPx: number; tableWidthPx: number };
```

渲染：

```tsx
<table className="knowhow-grid-table" style={{ width: tableWidthPx, minWidth: "100%" }}>
  <colgroup>
    {widths.columns.map((item) => <col key={item.columnId} style={{ width: item.widthPx }} />)}
    <col style={{ width: widths.statusWidthPx }} />
  </colgroup>
  ...
</table>
```

保留 `table-layout: fixed`。移除 `th/td { width: 200px; }`，单元格只遵从 colgroup。`.knowhow-grid-scroll`、表头 sticky、首列 sticky 和 rowSpan 逻辑保持。

### 9.2 有界采样

- 宽度只看当前 `filteredRows` 的有界样本，不扫描无限 R×C。
- 最多 64 个物理行：前 48 个 + 后 16 个；不足时不重复。
- 每格最多检查前 120 个 grapheme，且最多 8 个可见行；超出截断。
- 复杂度上限约为 `O(columns × 64 × 120)`，与全表行数无关。
- anchor 合并矩阵仍从物理 `filteredRows` 采样，不从 rowSpan 展示产物反推，避免共享格重复/遗漏造成宽度抖动。

### 9.3 可见文本摘要

纯函数按以下规则估算，不执行 Markdown 渲染器：

1. 统一 CRLF 为 LF；按换行切分，取估算单位最大的可见行，而不是累计全文长度。
2. 去掉识别出的行首 Markdown block marker（heading、blockquote、list marker）和成对强调/反引号控制符。
3. link/image 只保留 label/alt；`asset://` URL 不参与宽度。
4. Markdown 表格只估算当前行中最长 cell，而不是整行所有列之和。
5. 识别出的 Markdown 控制符权重 0；未形成合法结构的普通标点仍按标点计。
6. tab 按 2 个空格估算。

grapheme 权重：

| 字符类别 | 单位 |
|---|---:|
| CJK、全角字符、emoji | 2.0 |
| ASCII 字母、数字、下划线 | 1.0 |
| ASCII/半角标点 | 0.75 |
| 空格 | 0.5 |

优先使用 `Intl.Segmenter`；回退 `Array.from()`。列头也参与估算，并为 role badge/排序与编辑 affordance 预留 52px。

### 9.4 像素换算与 clamp

桌面推荐值：

| 列类 | min | max |
|---|---:|---:|
| 首列/anchor | 180px | 320px |
| procedure | 200px | 520px |
| entity | 160px | 360px |
| attribute | 160px | 420px |
| 同步状态 | 112px | 112px |

换算：`contentPx = ceil(maxUnits * 7.5) + 24px`（左右 padding），再与 header 需求取 max 并 clamp。列宽取 4px 的整数倍，减少轻微内容变化造成的像素抖动。

窄屏（现有 narrow breakpoint）不改为卡片布局，继续横向滚动；使用较紧 clamp：anchor `160..280`、procedure `176..380`、entity/attribute `144..320`、status `104`。sticky 首列宽度取最终 colgroup 值，不另写第二套 magic number。

### 9.5 重算依赖

```ts
const sampledRows = useMemo(
  () => sampleVisibleRows(filteredRows, 64),
  [filteredRows],
);

const columnWidths = useMemo(
  () => computeKnowhowColumnWidths({
    columns: orderedColumns,
    visibleRowsSample: sampledRows,
    anchorColumnId,
    compact: isNarrow,
  }),
  [orderedColumns, sampledRows, anchorColumnId, isNarrow],
);
```

不得依赖 modal 状态、hover、projection polling 或每次 render 新建的未 memo 数组。格子保存导致 `detail.rows` 真变化时重算是预期行为；只改变某个 UI 展开态不应重算。

### 9.6 首版不做

- 拖拽列宽；
- localStorage/server 持久化；
- 按用户偏好覆盖自动宽度；
- 虚拟滚动；
- 根据浏览器实际 DOM `measureText/getBoundingClientRect` 二次测量。

---

## 10. E：现有“智能补全空列”的零新增回归契约

本项状态：**功能已存在，本次零新增**。

必须保留：

- 行详情抽屉的“智能补全空列”入口。
- anchor 概念矩阵/分支场景复用现有物理行补全入口。
- 只处理 `(row.cells[column.id] ?? "") === ""`。
- 纯空白（`" "`、tab、换行）不是精确空串，不进入目标列。
- 排除 anchor/行标题列。
- 后端最多 8 条同表参考 + 有界 reasoning 检索；最多 20 项。
- suggestion-only；逐项接受，不自动保存、不整体覆盖。
- 接受时用 `expected_before=""`；共享格冻结所有 target row、空串基线、anchor 原值和精确成员守卫。
- 任何 409 都保留建议但拒绝覆盖现有内容。
- 不覆盖已有内容，不把“先 trim 再判空”引入任一前后端路径。

回归测试至少继续覆盖 `completableKnowhowColumns`、`completionSavePlan`、共享组 raw 空白退化单格计划、组件逐项接受和 origin=`llm_complete`。

---

## 11. 前后端与存储设计总结

### 11.1 后端

- 不新增批量规整端点；现有 per-cell reformat API 和响应保持。
- 复用 `/model-services/status` 作为前端并发提示来源，不改变 scheduler。
- 新增 audit principal helper；session 写路径统一使用。
- 拆分 transfer/import/copy 的 creator id 与 actor label 参数。
- 历史/代码读 API 增加有界批量 user-id → username 展示解析。
- SQLite 与 PostgreSQL 行为对等；不新增表/列/索引，不 bump schema version。

### 11.2 前端

- `knowhow-optimize-logic.ts`：并发工作组、single-flight、重试/epoch 纯状态转移。
- `knowhow-panel.tsx`：worker pool 驱动、同 modal item view、打开格子交接、colgroup。
- `knowhow-model.ts`：`reformatKnowhowCell(..., signal?)`；actor wire 字段保持 string。
- 新纯模块：bounded Markdown diff、内容宽度估算。
- 新用户可见行为与后端展示语义在同一变更交付，遵守 full-stack parity。

### 11.3 存储

- 新 audit label 直接写现有 TEXT 字段。
- identity 字段仍写 user id。
- 历史 id 和旧 `updated_by` 不迁移、不回写。
- fingerprint 算法和字段集合不变。

---

## 12. 失败与恢复

| 场景 | 用户结果 | 恢复 |
|---|---|---|
| 模型状态读取失败 | 并发 fallback=2 | 批次继续；后端 scheduler 仍限流 |
| 单 leader 请求失败 | 该物理格规整失败，同 key 下一格可重试 | reviewing 中重试未完成项 |
| leader source stale | 只标该物理格 stale，不缓存 | 关闭→reload→新批次 |
| 用户中止 | 已完成候选保留，其余 aborted | 同 snapshot 重试非 stale 项 |
| 请求迟到 | 丢弃 | 无需恢复 |
| diff 超预算 | 粗粒度整段增删 | 内容仍完整可读，可切渲染预览 |
| 保存前 preflight 失败 | 仍发权威 guarded save | 服务端 409 防覆盖；不得降级掉 expected_before |
| 保存 409 | 候选保留为 stale，不覆盖 | 关闭→reload→新批次 |
| 非 409 保存失败 | 该 unit save_error，其余继续 | 关闭后重跑；首版不增加局部保存重试 |
| audit id 无法解析 | 原样显示 | 不阻断历史读取 |
| 宽度估算异常 | 该列回退 200px，status 固定 | 表仍可滚动/编辑 |

---

## 13. 权限与安全

- 批量规整入口继续只对 `canEdit` 用户显示；后端 reformat 与 save 权限不变。
- diff 只处理当前已授权表的已返回内容；不增加跨表读取。
- “打开格子”必须复用现有 table/row/column 定位和权限检查。
- username 解析只查候选 user id，不允许按模糊 username 搜索，避免扩大身份枚举面。
- 只读成员可看已有历史 actor label；不能通过该字段取得额外权限。
- 观测日志不得记录 Markdown 原文、候选、diff token、dedupe key、username/id 映射明细或模型 prompt。
- AbortController 不是服务端取消承诺；UI 文案必须准确。

---

## 14. 性能预算

### 14.1 候选生成

- 单浏览器批次最大在途请求：3；状态失败时 2。
- 同 dedupe key 最大在途：1。
- promise/工作对象数量：`O(physical cells + unique keys)`，但同时活跃网络调用 `O(limit)`。
- 保存最大在途：1。
- 进度 reducer 更新可对一次扇出做单次批量 state 更新，避免 N 次 React commit。

### 14.2 diff

- 仅按需计算一个 item。
- 行级步数、token 数和矩阵预算见 §7.4。
- 超限必须线性降级，主线程单次目标预算 16ms；无法满足时可用 `queueMicrotask`/分片让出，但首选预算内同步纯函数。

### 14.3 列宽

- 最大扫描 `columns × 64 rows × 8 lines × 120 graphemes`。
- 不随全表行数增长。
- 不做 DOM 测量，避免 layout thrash。

### 14.4 actor 解析

- 单响应最多 512 个 distinct candidate，SQLite 最多 3 个 bounded IN 查询，PostgreSQL 等价有界。
- 禁止 N+1。

---

## 15. 可观测性

若接入现有事件/指标通道，只记录聚合元数据：

- `scope`（row/table）
- physical item count、unique key count、effective concurrency
- generation duration
- changed/unchanged/error/aborted/stale 数
- cache fan-out count、leader retry count
- save unit count、saved/save_error/stale 数
- diff degraded count 与 degradation reason 枚举
- actor resolution candidate/matched/fallback/count-limit 数
- width computation duration、sampled row count

禁止记录表标题、row/column id、dedupe key、Markdown 内容、candidate、username、user id 或异常原文。模型调用本身继续使用现有 scheduler/provider 观测链。

---

## 16. 测试矩阵

### 16.1 前端纯逻辑（node:test）

`frontend/app/knowhow-optimize.test.mjs`：

- 最大在途永不超过 1/2/3 配置值。
- 不使用无界 Promise.all 的架构守卫或行为证明。
- 不同 key 可并发；同 key single-flight。
- fresh success 才缓存；leader error/stale 后 follower 可成为新 leader。
- trim 等价输入共享 candidate，但各自重算 changed。
- 中止结算 pending/running；迟到 success/error/stale no-op。
- 进度以物理 item 为分母，fan-out 后一次跳 N。
- frozen snapshot、sourceMd、expected_before、完整 group、409 stale 既有用例全部保留。
- 保存 unit 仍严格串行。

新增 `knowhow-markdown-diff.test.mjs`：

- 中英文、emoji、全角标点、ASCII 标点、空格、tab、Markdown 控制符。
- 行增删与行内 token 高亮。
- CRLF/LF。
- 400 行/20k 字符/步数预算边界。
- 对抗性全不同长文本触发降级，不阻塞、不丢原文。
- 相同输入空 diff。

新增 `knowhow-column-widths.test.mjs`：

- CJK/全角/emoji 权重大于 ASCII。
- Markdown marker/link URL 不虚增宽度。
- 最长可见行而非全文总长。
- 64 行采样上限与首 48/末 16 确定性。
- role min/max、status 固定、4px 对齐、窄屏 clamp。

`knowhow-complete-logic.test.mjs` 保留精确空串、anchor 排除和共享组守卫用例。

### 16.2 前端组件（Vitest）

- queue → item diff → back，仍是单一 dialog。
- running/saving 关闭规则与中止行为。
- “查看改动”只在有 candidate 状态出现。
- saved 显示“打开格子”；点击先卸载 batch，再打开现有 cell modal。
- shared unit canonical representative 稳定。
- stale 不显示打开格子，重跑先关闭/reload。
- colgroup 宽度数与列数 + status 列一致；sticky 类不回退。
- 智能补全空列两个既有入口和逐项接受不回退。

### 16.3 后端 pytest

- `session_audit_principal` 的 username/display/id fallback。
- session route 的 identity id 与 actor label 拆分；created_by 仍为 id。
- Agent profile_name 保持。
- 15 条写路径 actor 与 transaction-final change/fingerprint 守卫继续全绿。
- import/create/transfer/notebook copy 的 creator/actor 不串线，SQLite/PostgreSQL parity。
- history page/cell history/row code 批量解析，无 N+1，limit/chunk 生效。
- unknown/deleted user/Agent origin 原样回退。
- code `updated_by` 读时解析不改变数据库与 fingerprint。
- 新 code 写入 username label 后 fingerprint 与 change 匹配。
- OpenAPI/wire 字段名不变。

### 16.4 回归基线

已知基线（由任务输入提供，可作为实施前对照）：

```text
node --test app/knowhow-normalize.test.mjs app/knowhow-optimize.test.mjs app/knowhow-complete-logic.test.mjs app/knowhow-history-logic.test.mjs
→ 444 pass

npx vitest run app/knowhow-row-completion.component.test.tsx app/knowhow-reformat-origin.component.test.tsx app/knowhow-cell-history.component.test.tsx
→ 12 pass；仅 React jsx/global 属性警告
```

后端 pytest 在当前已知环境中缺少 pytest，尚未执行。本文不得把它表述为已验证。

---

## 17. 验收标准

### 17.1 批量规整

- 不同 key 存在真实并发，最大在途不超过有效上限。
- 同 key 永远 single-flight；失败/stale 不污染 followers。
- 候选生成并发、保存串行。
- 物理格进度、部分失败、中止、迟到响应和重跑行为符合本文。
- frozen snapshot、sourceMd、expected_before、target rows、anchor/member guard 和 409 stale 全部不退化。

### 17.2 diff 与打开格子

- 每个有候选项可在同一 modal 查看原始 Markdown 行级 + token diff。
- 超长/对抗内容稳定降级，没有无界二次复杂度。
- 预览只是可切换次级视图。
- 保存仍是整体确认。
- saved 项可在安全关闭 modal 后打开正确物理格；共享格 representative 可重复、可测试。

### 17.3 actor

- 新 session 写入显示 username fallback 规则结果。
- 权限/owner/created_by 稳定 id 不变。
- Agent 仍显示 profile_name。
- 历史 user id 有界批量解析，无 N+1；未知文本原样。
- 不迁移存量，不重写 `updated_by`，fingerprint 链不变。
- wire 字段名和 string 类型向后兼容。

### 17.4 列宽与补全

- colgroup + fixed layout 生效；横向滚动、表头/首列 sticky 不退化。
- 宽度计算有采样上限、min/max 和窄屏行为。
- 无拖拽/持久化。
- 智能补全空列零新增，精确空串和不覆盖契约全部保留。

### 17.5 工程门

- `scripts/check.sh` 通过。
- `cd frontend && npm run build` 通过。
- SQLite/PostgreSQL 测试对等。
- 若最终实施改变 setup、产品行为、架构或约束，再按 AGENTS.md 同步对应中英文文档；本 proposal 本身不触发同步。

---

## 18. 文件/函数级影响清单（实施评估，不是授权）

### 前端

- `frontend/app/knowhow-panel.tsx`
  - `KnowhowReformatBatchModal.runBatch/runSave/requestClose/handleAbort`
  - queue item 渲染、item detail view、“打开格子”交接
  - `KnowhowGridView` colgroup 与 width memo
  - 相关 CSS
- `frontend/app/knowhow-optimize-logic.ts`
  - batch group/single-flight/retry helpers
  - `ReformatSaveUnit` canonical representative
- `frontend/app/knowhow-model.ts`
  - `reformatKnowhowCell(..., signal?)`
  - actor 字段语义注释保持兼容
- `frontend/app/model-services.ts`
  - 复用现有 workload/maximum 类型；可新增纯 concurrency resolver
- 新 `frontend/app/knowhow-markdown-diff.ts`
- 新 `frontend/app/knowhow-column-widths.ts`
- `frontend/app/knowhow-history-drawer.tsx`
- `frontend/app/knowhow-cell-history.tsx`
- `frontend/app/knowhow-code.tsx` / `knowhow-code-logic.ts`
- 相关 `*.test.mjs` / `*.component.test.tsx`

### 后端

- 新 `backend/app/core/audit_actor.py`（推荐位置）
- `backend/app/api/deps.py`
  - `RequestActor` session label
- `backend/app/api/knowhow_routes.py`
  - 所有 session Knowhow 写路径
  - history/cell history/milestone 响应展示解析
- `backend/app/api/knowhow_agent_routes.py`
  - 保持 Agent profile_name，补一致性测试
- `backend/app/api/mcp_server.py`
  - 保持 Agent profile_name，不新增历史工具
- `backend/app/services/knowhow/api.py`
  - `import_table/create_table/put_cell_code` 参数语义
- `backend/app/services/knowhow/transfer.py`
  - `_remap/copy_table/move_table/transfer_table` creator vs actor
- `backend/app/services/notebook_sharing.py`
  - copied Knowhow genesis actor label
- `backend/app/repositories/sqlite/identity_store.py`
- `backend/app/repositories/postgres/identity_store.py`
  - bounded audit label resolution port
- `backend/app/repositories/sqlite/sharing_store.py`
- `backend/app/repositories/postgres/sharing_store.py`
- `backend/app/repositories/sqlite/knowhow_store.py`
- `backend/app/repositories/postgres/knowhow_store.py`
  - 不改 fingerprint/schema；只核对 actor 参数贯穿
- repository facade/ports/ownership manifests（仅当新增 resolver surface 时）
- backend history/agent/transfer/copy tests

---

## 19. 实施拆分建议（批准后才可执行）

1. **纯逻辑与测试地基**：并发 resolver/worker state、bounded diff、column width 纯函数；不接 UI。
2. **批量规整 UI**：有界并发、single-flight、同 modal diff、打开格子；保持保存路径不变。
3. **actor 全栈治理**：audit principal、写路径拆参、读时有界解析、SQLite/PostgreSQL parity、所有展示面。
4. **colgroup 布局与补全回归**：接入内容感知宽度，补智能补全既有契约测试。
5. **全门验证与文档同步判断**：完整 check/build/pytest；只有产品行为最终获批并实施后才更新现行产品文档和 `fangan_done.md`。

每一拆分都必须保持可运行；不能先把后端 actor 改成 label 却让前端仍假设 id，也不能先渲染“打开格子”而没有安全 modal 交接。

---

## 20. 回滚策略

- 并发出现问题：把 product ceiling 临时降为 1，即恢复串行生成；保存路径无需回滚。
- diff 出现性能问题：保留队列与候选，临时强制走线性整段增删降级；不得回到弹窗底部无限堆全部预览。
- 列宽出现布局问题：colgroup 纯函数统一返回普通列 200px、status 112px，可快速恢复当前视觉，sticky/滚动结构不变。
- actor 代码回滚：新写入的 username 仍是合法 TEXT，旧前端会原样显示，不造成 schema 不兼容；不得试图把这些 label 反向批量改回 id。
- 读时 resolver 可独立关闭并原样返回存储文本，不影响权限或写入。
- 全部回滚均不涉及 schema downgrade，因为推荐方案无 migration。

---

## 21. 已批准的产品决策

以下决策已随本文于 2026-07-25 一并批准，实施采用括号内推荐默认值：

1. **单浏览器候选并发 ceiling / fallback**：批准为 `3 / 2`，不采用更保守的 `2 / 1`。
2. **生成中关闭行为**：批准为 running/saving 期间不可直接关闭；generation 必须显式中止，saving 不可中止，不增加“关闭即中止并关闭”的二次确认。
3. **同 snapshot 重试范围**：批准为只重试 error/aborted/pending；stale 必须关闭、reload、新批次，不自动执行“关闭→刷新→重开”。
4. **diff 预算**：批准为 20k 字符、400 行、200k 行级步数、250k 行内矩阵总预算；超限时降级为有界整段增删摘要。
5. **共享/合并格打开目标**：批准为最小 row.position、再按 rowId 的 canonical representative，而不是用户点击的隐藏物理兄弟行。
6. **actor wire**：批准维持 `actor/created_by/updated_by` 原字段名，读响应直接给可读 label，不新增 `*_label` 字段。
7. **历史 Agent 名与 live user id 碰撞**：批准无 migration 首版接受这个极端歧义；有 `origin=agent` 时严格跳过解析，缺 origin 的旧 `updated_by` 仅按 live user 精确命中解析。
8. **列宽 clamp**：批准桌面 anchor `180..320`、procedure `200..520`、普通 `160..420`、status `112`，窄屏使用本文较紧值，不把 procedure 最大宽度降到 440px。
9. **保存失败局部重试**：批准首版不增加同 modal 的 `save_error` 局部重试，避免扩大保存状态机；统一关闭后重跑。

上述决策均按推荐默认值实施；若实现阶段发现必须改变这些产品语义，需重新提交用户评审。
