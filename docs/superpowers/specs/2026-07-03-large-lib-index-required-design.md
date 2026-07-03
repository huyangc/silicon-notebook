# 大库检索统一 copyable + 无索引提示建索引 设计(Branch B,并入 PR#185)

**日期**: 2026-07-03
**分支**: feat/scale-index-disk-identity(并入 PR#185,与磁盘身份缓存同分支)
**状态**: 方向 + 关键决策已确认(用户:大库定义统一 copyable;index_required 仅「完全无索引」时弹)

## 背景

「大库检索强制走索引、小库可暴力不要求索引」是产品硬不变量。当前六条检索路径里 5 条用
`notebook_copy_stats()["copyable"] is False` 判「大库」,唯 chunk 路径([_retrieve_chunks])
用独立的 `chunk_bruteforce_max_chunks` 计数阈值 —— 定义不统一。且大库若从未建索引,检索
静默降级(FTS 有界/跳过/拒绝)返回劣质结果,用户不知道该去建索引。

## 目标(两部分,一个特性)

### Part 1 — 「大库」定义全局统一到 copyable

`_retrieve_chunks` 的大库暴力守卫改为 **`not copyable` OR 现有 chunk 计数阈值** 触发:
- 大库(copyable=False):无论 chunk 多少都强制走索引/FTS 降级,绝不全表暴力 chunk。
- 小库(copyable=True)且 chunk 数 ≤ 阈值:照旧全量暴力(不要求索引)。
- 小库但 chunk 数 > 阈值:照旧降级(阈值作叠加下限,行为不变)。

六条路径自此同一把尺子。`chunk_bruteforce_max_chunks` 保留(叠加),不删。

### Part 2 — 大库无可用索引 → index_required 信号 → 前端提示建索引

**判定(用户确认)**:仅「大库 **且** 磁盘完全无 scale 索引(从未建过)」时弹。
「建过但有 delta」是刻意选的恒定成本·最终一致态,已由既有「N 源待索引」徽章覆盖,不重复
弹(否则与「最终一致」取向相抵)。

- **后端**:`AskResponse` 加 `index_required: bool = False`(镜像既有 `kg_required`)。helper
  `_needs_index(nb) = (not notebook_copy_stats(nb)["copyable"]) and (self._scale_index(nb, allow_stale=True) is None)`
  —— 大库 且 无磁盘索引。二者都廉价(copystats 版本 memo;_scale_index allow_stale 经
  Branch A 已 O(1)/廉价 manifest 读)。在 `ask_chunk`/`ask_reasoning`/`ask_graph` 三 handler
  的**主 return** 前置 `response.index_required = self._needs_index(nb)`。降级 FTS 答案照常返回,
  banner 在其上方,不替代(fail-open)。
- **前端**:AskResponse TS 类型加 `index_required?: boolean`;为 true 时在问答结果区渲染
  banner:「此知识库较大且尚未建立检索索引,当前检索能力受限。」+「构建索引」按钮 → 调既有
  `rebuildScaleIndex(nb, "now")` → 用既有 `scaleIndexStatus` 轮询反映 building 态。镜像既有
  `model_errors` 横幅位置。

## 数据流

```
ask(large, no index) → handler 检索(各守卫降级 FTS/skip/refuse)→ 主 return 前
  response.index_required = _needs_index(nb) = True
→ 前端收到 → 渲染建索引 banner + 钮
→ 用户点钮 → POST scale-index/rebuild {when:"now"} → 后台 build
→ scaleIndexStatus 轮询显示 building → indexed 后 banner 消失(下次 ask index_required=False)
```

## 决策(已定,非 TBD)

- rebuild 触发 `when:"now"`(用户点了就立即后台建,复用既有进度)。
- 降级答案仍渲染,banner 置其上(不阻断)。
- index_required 仅「完全无索引」弹;「有 delta」走既有徽章。

## 组件边界

- 后端 `_needs_index(nb) -> bool`:纯判定,单一职责,独立可测。
- 三 handler:各加一行赋值,不改检索逻辑。
- `AskResponse.index_required`:新增布尔,默认 False,向后兼容(旧前端忽略)。
- Part 1 chunk 守卫:改一个布尔条件,叠加既有阈值。
- 前端:一个 banner 组件片段 + 一个已存在的 rebuild 调用。

## 测试

1. `_needs_index`:大库无索引→True;大库有索引(含 stale)→False;小库无索引→False;小库有索引→False。
2. 三 handler:大库无索引 ask → response.index_required=True;大库有索引 → False;小库 → False。
3. Part 1:大库(not copyable)且 n_chunks ≤ 阈值 → chunk 检索走 FTS 降级(不 _gather_chunks 全表)+ chunk_bruteforce_skipped 事件;小库 chunk 少 → 全量暴力路径不变(字节等价)。
4. 前端:tsc clean;index_required=true 渲染 banner + 钮;点钮调 rebuildScaleIndex(nb,"now")。弯引号自查=0。

## 非目标(YAGNI)

- 不改「有 delta」的既有徽章逻辑/文案。
- 不把完整 scale_index_status 塞进 AskResponse(前端需要时自取 status 端点)。
- 不删 `chunk_bruteforce_max_chunks`(作叠加下限保留)。
- 不动 Branch A 的缓存改动(已合流程)。
- 不自动触发建索引(须用户显式点钮;auto-fold 是另一回事)。

## 前后端同步交付

后端(schema+helper+三 handler)与前端(类型+banner+钮接线)在同一 PR#185 同一批任务交付,
不拆成后续(遵循 frontend-backend-co-design)。
