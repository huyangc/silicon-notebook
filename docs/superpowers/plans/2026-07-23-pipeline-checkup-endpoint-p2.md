# P2 体检 endpoint + 看板改造 — 实现计划

承 `docs/superpowers/specs/2026-07-22-pipeline-damage-recovery-design.md`「二·体检层 / 三·修复层 / 四·看板改造」。
P0(#323)、P1(#324)、P1.5 完成标记(#330)已合入 master(SCHEMA=27)。本计划把体检层落地。

**用户已定(2026-07-23)**:① 前后端 + 修复 CTA **一个 PR 全做**;② H8 索引损坏走 **version_signal 缓存**。

## 一、范围与非目标

**做**:一个 per-notebook 只读体检 endpoint 聚合 H2–H8 + 看板「来源状态」「索引与构建」两块升级 +
铃铛聚合提醒 + 两个新修复动作(重新解析 N 篇 / 补齐向量)接线其余已有 CTA。

**不做**(承设计文档「明确不做」):H1 已出局(P0 启动清算把 queued/parsing 翻 failed,H6 家族覆盖);
G7 增量融合半程崩溃不做精确检测(体检直接建议「重新合并知识图谱」);不改 `parse_status` 语义;
**凡调 LLM/embedding 的修复一律不自动触发**(承 efficiency-first),体检只读、修复由用户点。

## 二、体检项的代价模型(已在 master 核实,memory 分类的落地)

⚠ 设计文档乐观假设「H1–H8 都能挂 kg_mutation_seq、O(1)」**已被推翻**。真实分层:

| # | 体检项 | 判据 / 数据源(已核实函数) | 代价模型 | 修复动作 |
|---|---|---|---|---|
| H2 | 空源 | `QueryStore.sources_without_elements`:用户源 + `parse_status IN ('parsed','extracting','extracted')`(**白名单**:排 queued/parsing 在途、metadata-only 待上传、failed 已呈现)+ `NOT EXISTS elements`,再减租约 | 直查(索引覆盖),per-nb 便宜 | 重新解析 |
| H3 | 缺分块 | **新增查询**:`elements>0 AND chunked_at IS NULL` **且 source_id 不在活跃租约快照**(P1.5 的 chunked_at) | 直查 + 内存租约减法 | 重新解析 |
| H4 | 缺 chunk 向量 | `maintenance.count_missing_chunk_vectors`(:537) | **直连 COUNT,不可 memo**(embed 成功路径不 bump seq,已核实) | 补齐向量 |
| H5 | 缺 element 向量 | `maintenance.count_missing_element_vectors`(:456,A5/PR#324 已落) | 直连 COUNT,同 H4 | 补齐向量 |
| H6 | KG 未完成 | `query_store.pending_kg_source_count`(:66) | **已 memo 在 kg_mutation_seq,O(1)** | 分析新增 N 篇(已有) |
| H7 | 索引过期/维度失配 | `index_status`(:382)/`index_projection_store.version_signal`(:66) | 走**自己的** version_signal memo(含维度),不折进 kg_mutation_seq | 更新索引 fold(已有) |
| H8 | 索引产物损坏 | A2 的加载端交叉校验(`scale_artifact_store` 读 manifest 计数 vs 数组长度) | **需真做一次磁盘 load** → **按 version_signal 缓存**(见下) | 重建索引 full(已有) |

**H3 关键(P1.5 交接)**:读活跃租约前必须 `with self._active_sources_lock: active = set(self._active_sources)`
取快照,否则并发 process_source 的 stamp/pop 触发 `dict changed size during iteration`。租约现在是
`dict[str,int]` 引用计数,取 `set(keys)` 即活跃源集。H3 = `SQL 候选集 - active` 的 Python 后置减法。

**H4/H5 关键**:向量 embed 成功**不 bump kg_mutation_seq**(`source_embedding.py` 无 mark_unified/invalidate),
所以**不能**折进 seq-memo(会一直报旧值),必须每次直连 COUNT。这两个 COUNT 是 per-nb 索引查询,可接受。

## 三、H8 缓存:磁盘 manifest 身份 + 只缓存健康(评审 B1 订正)

⚠ **本节初稿的前提「用 version_signal 当缓存键、重建必 bump 它」是错的**,已被 T2 双评审(spec-review B1)
推翻并证实:`version_signal` 只由 `unified_kg_state` 的三个 seq 组成(`index_projection_store.py:82-90`),
**scale 索引 rebuild/fold 不 bump 任何 seq**(`scale_artifact_store.py:61` 白纸黑字「磁盘索引只在 rebuild/fold
时换新 version、与 kg_mutation_seq 无关」)。用 version_signal 当键会:① fold 后损坏但 seq 不变 → 缓存返健康 →
**静默漏报**(而 H8 存在的唯一意义就是抓静默损坏);② 损坏被缓存后用户点重建(原子换成健康产物、seq 不变)
→ 仍报损坏、**清不掉**。根因在计划前提,不在实现。

订正后的设计:

```
def h8_index_integrity(nb_id):
    exists, manifest_version = scale_artifacts.scale_manifest_identity(nb_id)  # sub-ms 磁盘读
    if not exists:
        return 0                                    # 未建索引 → 廉价短路,不 load、不缓存
    cached = self._h8_cache.get(nb_id)              # 缓存只存「健康(0)」
    if cached and cached.manifest_version == manifest_version:
        return 0                                    # 本代产物已探过健康
    result = self._probe_index_integrity(nb_id)     # 真 load + 计数vs长度交叉校验(A2 判据)
    if result == 0:
        self._h8_cache[nb_id] = (manifest_version, 0)   # 只缓存健康,键=磁盘产物身份
    else:
        self._h8_cache.pop(nb_id, None)             # 损坏从不缓存 → 每次现探 → 修好即自愈
    return result
```

- **缓存键 = 磁盘 manifest 身份**(`read_manifest_version`——就是为「磁盘索引变没变」而生、allow_stale 检索
  路径已在用):rebuild/fold(`.tmp`+swap 原子)换新 `manifest.version` 即让缓存失效。
- **只缓存健康(0)、损坏(1)从不缓存**:full/fold 都原子换目录,故「健康→损坏」只可能来自外部篡改(越界);
  「损坏→健康」是用户重建的正常闭环——损坏罕见,每次现探即可,修好立刻现探为健康(即便重建写回同一
  version 也能自愈,免了「按身份缓存损坏」的粘滞误报)。
- `probe_scale_index_integrity`(模块级,`checkup.py`)复用 `load_scale_index` 的加载端校验:manifest 缺失→0
  (未建)、manifest 在但 load 返回 None→1(损坏)、异常→0 且不缓存(**never-raise**,承 P0 不 raise 进热路径)。
- 进程内 `OrderedDict` LRU,`_H8_CACHE_MAX=256`,重启即空(重启后首个 checkup 重算一次,可接受)。

## 四、endpoint 契约

`GET /notebooks/{notebook_id}/checkup`(新增,`require_notebook_read`)。**只读**、无写、无模型调用。
与系统级 `/health`(system_routes,API/LLM 状态)语义不同,勿混。

```jsonc
{
  "notebook_id": "nb-...",
  "checked_at": "2026-07-23T...",
  "healthy": false,                    // 任一 check.count>0 即 false
  "checks": [
    {
      "code": "H2",                    // 内部代号
      "count": 3,                      // 命中源数(H7/H8 是 0/1 布尔映射成 count)
      "sample": ["src-a","src-b"],     // 有界样本(≤N,给前端展示;不返回全量避免大 payload)
      "fix": "reparse"                 // 修复动作枚举:reparse|backfill_vectors|extract_kg|fold_index|rebuild_index
    }
    // H3..H8 同形
  ]
}
```

⚠ **界面词汇红线**:响应体是内部契约(code=H2/fix=reparse 等内部枚举);面向用户的文案在**前端** `errors.ts`
风格的映射层做(H2→「空源」、reparse→「重新解析」),后端不出黑话给用户。`sample` 只给 source_id,
前端自行取标题。

## 五、修复层(两新 + 三已有接线)

| 修复动作枚举 | 后端 | 幂等 | 触发 |
|---|---|---|---|
| `reparse`（H2/H3） | **新增批量端点**:`POST /notebooks/{id}/sources/reparse` body `{source_ids:[...]}`,逐个走 process_source(已有管线) | 是(整源重做) | 用户点，非自动 |
| `backfill_vectors`（H4/H5） | **新增 UI 入口**接已有 backfill：`batch_ingest._count_missing_*` 的补齐路径 / maintenance 补向量 | 是(只补缺失) | 用户点，非自动 |
| `extract_kg`（H6） | 已有「分析新增 N 篇」 | 是 | 用户点 |
| `fold_index`（H7） | 已有 fold（`/index-status` 侧的更新入口） | 是 | 已有 auto/idle 保留 |
| `rebuild_index`（H8） | 已有 full 重建（scale-index rebuild） | 是 | 用户点 |

⚠ reparse 批量端点必须复用 process_source（含 P1.5 的租约 + 分块串行锁），**不要**另造摄取路径。
所有权/scope 守卫沿用现有 source 端点（owner 校验）。

## 六、前端触点(全栈对等,同 PR)

1. **「来源状态」块**(`page.tsx:4869`)升级为体检块:
   - 无异常 → 保持现有中性 tag 行(常态不打扰,健康库不能看起来像坏了)。
   - 有异常 → 列出 H2–H6 源级问题(数量 + 样本标题) + 对应修复 CTA(重新解析 / 补齐向量 / 分析新增)。
2. **「索引与构建」块**(`page.tsx:4901`)加可信度维度:
   - H7 过期 → 「更新索引」CTA(已有 fold)。
   - H8 损坏 → 「重建索引」CTA(已有 full)+ 明确的损坏提示(这是唯一会静默错的一格)。
3. **铃铛**(`content-overview-cards.tsx` / 待确认中心):体检发现异常时冒**一条聚合**提醒,点击直达看板。
   **不复制体检详情、不新增待办类型**(铃铛语义是「待你确认」,体检多是「系统能自修」)。
4. 轮询:看板弹窗打开时经既有聚合轮询 effect（`page.tsx:929` 一带）拉 checkup,让位机制沿用。

## 七、任务分解(子代理逐任务)

- **T1**〔store〕H3 缺分块查询:`chunk_store` 或 `query_store` 加 `sources_missing_chunks(nb)`
  = `elements>0 AND chunked_at IS NULL`(SQL 候选集,租约减法在 service 层做)。配组件测试。
- **T2**〔service〕体检聚合 `CheckupService.run(nb)`:装配 H2–H8;H3 取租约快照做减法;H8 version_signal 缓存 +
  `_probe_index_integrity`(复用 A2 加载端校验、只读、不 raise)。纯读、无模型。配单测(每个 H 造命中/不命中)。
- **T3**〔api〕`GET /notebooks/{id}/checkup` 端点 + 响应 pydantic 模型(models 层)+ 契约刷新(新端点跑默认模式刷 api_contract)。
- **T4**〔api〕修复端点:`POST /sources/reparse` 批量(复用 process_source)+ backfill_vectors 入口接已有补齐。owner 守卫。
- **T5**〔fe〕「来源状态」块升级 + checkup fetch + 内部代号→界面词映射层。
- **T6**〔fe〕「索引与构建」块加 H7/H8 可信度维度 + 已有 fold/full CTA 接线。
- **T7**〔fe〕铃铛聚合提醒(一条、跳转、不新增待办类型)。
- **T8**〔test〕后端 checkup service/endpoint 集成 + 前端组件测试;词汇守卫(`check_ui_vocabulary`)过。

每个任务完成后跑任务级规格评审(spec-review)+ 代码质量评审(code-quality-review),再推进下一个。

## 八、验证与红线

- 全栈对等:后端每个体检项/修复动作都有前端触点(同 PR)。
- 界面词汇:响应体内部代号,用户文案只在前端映射;`scripts/check_ui_vocabulary.py` 硬门过。
- 效率:checkup 只读、无模型;H6/H7 memo、H8 version 缓存、H4/H5 直连 COUNT(已论证不可 memo)。
- 架构守卫:新端点跑默认模式刷 `api_contract`;新 facade 成员走 allowlist + 一跳委托。
- 全量门 `PYTHONPATH=backend pytest backend/tests`(**从 worktree 根跑**,否则 #329 的 `from scripts import` 假 ImportError)。
- 文档同步:影响产品行为 → README/README_zh/AGENTS/CLAUDE 四份评估是否需改(体检是新用户可见能力,大概率要在 README 提一句)。

## 九、开放实现细节(实现时定,非阻塞)

- H8 缓存的有界化策略(LRU N 个 nb vs 与 notebook 生命周期对齐)。
- `sample` 的上界 N(给前端展示够用即可,如 20)。
- checkup 是否要一个「上次体检时刻」的轻记录(纯前端展示,不持久化亦可)。
