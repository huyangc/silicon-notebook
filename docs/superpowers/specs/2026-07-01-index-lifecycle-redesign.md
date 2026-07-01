# 检索索引生命周期重设计:index 与 tier 解耦、大小库统一、增量 fold + 全量重建

> 日期：2026-07-01 · 状态：设计已确认(Q1–Q6),待写实现计划(Phase 1)。
> 关联 review：[docs/kg-scale-retrieval-review.md](../../kg-scale-retrieval-review.md)。
> 已落地前置：PR#129(scale_ppr combined 图缓存 + splice 向量化)、PR#130(base chunk ANN,`chunk_ann_enabled`)、PR#134(在线重建 scale 索引入口,base-tier 后台)。

---

## 1. 背景与问题

当前一切索引/检索决策都挂在 **`tier=='base'`** 一个标志上,它同时承担了两件**正交**的事:

- **轴 1 · 联邦角色**:base = 全局唯一权威参考层(跨库引用、冲突仲裁);personal = 个人笔记。
- **轴 2 · 规模 / 是否建索引**:大库需预建 ANN+CSR 才快;小库暴力即可、且永远最新。

把两轴绑死导致三类问题(后两条为外部 review 揪出的 P0-00 / P0-0):

1. **大的个人库(非 base)永远得不到索引** → 一直全量暴力、慢,哪怕百万 chunk。
2. **直接查 base 库本身**:`scale_ppr` 用 `id != active` 排除 self → 返回空 → 回退 rustworkx 全内存图(大库验收最易踩)。
3. **小的 base 库**被迫认为该建索引。

## 2. 核心模型(已确认 Q1 / Q2)

> **「是否建索引」是每个 notebook 自己的、由规模驱动的属性,与 tier 彻底解耦。检索分派只看「indexed 与否」,不看 tier。tier 退回只管联邦。**

统一检索公式:

> **任意 notebook 的检索 = (已索引主体 → ANN 有界) ⊕ (未索引近期 delta → 暴力)**

- 小库 = 全是 delta(纯暴力,快、永远最新)。
- 大库刚建完 = 全是索引核。
- 大库 + 新上传 = 索引核 ⊕ 小 delta。

**正确性不变量**:delta 始终被暴力检索兜住 —— 新传内容永远查得到(满足「随传随查」);`suggested`/`stale` 只是「建议重建」的性能信号,绝不影响召回正确性。

## 3. notebook 索引状态机(与 tier 无关)

| 状态 | 含义 | 检索行为 | 进入条件 |
|---|---|---|---|
| `unindexed` | 小库(默认) | 全暴力 | 初始 |
| `suggested` | 越过阈值①(总量够大) | 仍全暴力 | 摄取后自动检测 |
| `queued` | 用户选「空闲时(重)建」 | 仍现状暴力 / 索引⊕delta | 用户点选 |
| `building` | 后台(重)建中 | 不阻塞(仍走旧索引⊕delta 或全暴力) | 立即 / 调度器 |
| `indexed` | 有新鲜索引,delta 空/小 | 索引核 ⊕ 小 delta | build/fold 完成 |
| `stale:fold` | 已索引,delta 攒过 fold 阈值② | 索引核 ⊕ 较大 delta(正确但 delta 段变慢) | 摄取后自动检测 |
| `stale:rebuild` | 质量漂移(见 §4)需全量重建 | 索引核 ⊕ delta | 质量阈值③ / 周期 |

转移触发:摄取(重算规模→可能置 suggested/stale)、用户选时机(→building / queued)、调度器(queued→building)、build/fold 完成(→indexed)。

## 4. delta 水位与两档合并(已确认 Q5=B)

**delta 界定(复用 splice 思路,从「跨库」推广到「同库」)**:索引 manifest 记一个**水位线**(build 时纳入的 source 集合 / 单调 ingest revision);`delta` = 水位之后新增的 source/chunk/KG/边。`_index_delta(nb)` 按水位取 delta(O(delta))。

**逐组件可增量性**(回答「快速合并 vs 全量重建」):

| 工件 | 增量合并 | 方式 |
|---|---|---|
| chunk ANN / KG ANN(hnswlib) | ✅ 原生 | `load → add_items(delta 向量) → 存回`,构建 O(delta·logN)。 |
| PPR CSR 转移图 | ✅ 查询时拼 | P0-3 的 bordered-block splice,delta 当边界块,O(delta),连 fold 都不用。 |
| concept_clusters | ✅ 已有 | `incremental_fuse_source` 增量融合。 |
| viz 折叠图 | ⚠️ 懒重建 | 不在检索热路径,可延迟/降级。 |

**两档合并**:

- **增量 fold(快,O(delta) 计算 + O(N) IO)** —— `fold_scale_index_delta(nb)`:
  - `load` 现有 ANN handle → `add_items(delta 向量)` → 存 **temp 文件 → 原子 rename 替换**(规避就地改的并发:查询用旧 handle,替换后用新);
  - `incremental_fuse` 簇;re-splice CSR;推进 manifest 水位。
  - 成本地板:hnswlib 增量要 `load+save` 整个索引文件(百万向量约几百 MB IO,秒级)→ **攒批再 fold**,不每传一篇就 fold(阈值② 控制)。
- **全量重建(慢,O(N) 计算)** —— 现有 `build_scale_index`:重优化整个 HNSW + 重聚类。**质量维护**才做:
  - HNSW 多次增量插入后图连通性退化 → recall 下滑;
  - `incremental_fuse` 顺序相关 → canonical 漂移(Tier3 逃生口);
  - 删除不回收空间。
  - 放低峰窗口 / 用户 now 触发。

`fold` 与 `build` 都受既有 `_scale_building` in-flight 守卫串行化。

## 5. 检索分派统一成 ⊕(核心重构)

分派只依据「该 notebook 有无有效索引」(state ∈ {indexed, stale:*}),不看 tier:

- **`_retrieve_chunks`**:indexed → chunk ANN 打**存量核** ⊕ 暴力打 **delta chunk** → 合并;否则全暴力。(把 PR#130 的门控从 `chunk_ann_enabled` flag 改为「有有效索引」,并补 delta 暴力那半。)
- **`_retrieve_scored` / `federated_retrieve`**:同构 —— KG 对象 ANN 存量核 ⊕ 暴力 delta;孤立点集合进索引预算,delta 侧小算(消除每查询全表扫 `knowledge_relations`)。federated 按「每个参与库 indexed 与否」分派,而非按 tier。
- **`scale_ppr`**:**修 P0-00** —— 查自身且自身有有效索引时用 self index(不再 `id != active` 排除);combined = self 索引核 ⊕ self delta splice ⊕(存在 base 联邦时再叠 base 核)。base/active splice 被复用为「同库 delta splice」。
- **hnsw handle 进程缓存(P0-4)**:随 `ScaleIndex` 缓存打开的 handle(fold 后替换),顺带解掉每查询两次 `load_index`。

## 6. 触发 / 时机 / 调度(已确认 Q3 / Q4)

- **自动检测**:摄取(`process_source` / 批量)后重算规模:未索引且 `chunk 数 > 阈值①` → `suggested`;已索引且 `delta chunk 数 > 阈值②` → `stale:fold`;质量信号越阈值③ → `stale:rebuild`。
- **告知 + 挑时机**:系统主动surface「需(重)建索引」,用户二选 **立即** / **空闲时**;后者置 `queued`。**重建必然发生,只是挑时机**。
- **低峰窗口调度器(新)**:一个后台组件,在配置的低峰时段依次跑 `queued` 的 fold/build。单进程 FastAPI 里可用轻量定时线程 + `_scale_building` 串行。
- **可配**:阈值①②③(默认按 chunk 数)、低峰窗口时间。

## 7. 前端(扩展 PR#134)

治理弹窗 / 来源面板已有的「重建检索索引」动作 + 状态行,扩展为:
- 四态 badge:未索引 / 建议建索引 / 构建中 / 已同步(过期时标「建议重建」)。
- 「(重)建」动作弹二选:**立即** / **服务器空闲时**。
- 状态行显示 delta 规模(「N 篇待并入」)与核规模。
- 门控从「仅 admin+base」改为「够大或已索引的库」(与解耦一致;是否仍限 admin 可复用现有治理权限)。

## 8. 组件与接口

**后端(`sqlite_repository.py` 除非另注)**
- `scale_index_status(nb)`(PR#134 已有)→ 扩展返回 `state` 枚举 + `delta_chunks`/`delta_nodes` + `watermark`。
- `_index_delta(nb)`(新)→ 按 manifest 水位取 delta 的 chunk_ids / KG 节点 / 边。
- `fold_scale_index_delta(nb)`(新)→ 增量 fold(见 §4),原子替换工件 + 推进水位。
- `build_scale_index(nb)`(现有)→ manifest 增记水位线。
- `_retrieve_chunks` / `_retrieve_scored` / `federated_retrieve` / `scale_ppr` → 改为 ⊕ 分派(见 §5)。
- `ScaleIndex`(`kg/scale_index.py`)→ 缓存 hnsw handle;`load/save` 支持水位 + temp-swap。
- 调度器(新,`kg/scheduler.py` 或独立模块)→ 低峰窗口跑 queued。
- 路由(`routes.py`):`POST /scale-index/rebuild` 扩展 `{mode: fold|full, when: now|idle}`;`GET /scale-index/status` 返回扩展状态。
- `config.py`:阈值①②③、低峰窗口、fold/dispatch 开关(默认保守,渐进开)。

**前端(`page.tsx`)**:见 §7。

## 9. 分期(已确认 Q6)

- **Phase 1 — 解耦 + 统一 ⊕ 分派(地基)**
  - index 状态按「存在 + delta + 阈值」判,与 tier 解耦;delta 水位 + `_index_delta`。
  - 检索 ⊕:`_retrieve_chunks` / `_retrieve_scored` / `federated_retrieve` / `scale_ppr(修 P0-00)` 走「索引核 ⊕ 暴力 delta」。
  - (重)建仍用现有全量 `build_scale_index`(delta 靠暴力兜,直到下次全量)。
  - **验收**:大个人库可建并走索引;直接查 base 走 self index 不回退 rustworkx;新上传立即可查(delta 暴力);默认行为对未索引小库字节不变。
- **Phase 2 — 增量 fold(两档快档)**
  - `fold_scale_index_delta` + hnsw handle 缓存 + 原子替换;fold 阈值②。
  - **验收**:fold 后 delta 暴力集缩小、召回不劣化;fold 与并发查询无竞态;等价性对照全量重建。
- **Phase 3 — 触发 / 时机 / 调度 + UI**
  - 摄取后阈值自动检测置状态;用户 now/idle 二选;低峰窗口调度器;前端四态 + 二选钮。
  - **验收**:越阈值自动 surface;queued 在窗口内跑掉;UI 四态真机走查。

## 10. 测试要点

- **分派**:indexed 库检索只对候选打分(≤recall+delta),不全表;未索引库字节不变。
- **⊕ 正确性**:索引核 ⊕ delta 的合并结果 ⊇ 纯暴力 top-k 的关键命中(delta 内新文档必召回)。
- **P0-00**:`scale_ppr(base.id, q)` 不回退 `_ppr_graph`,用 self index 返回 base chunks。
- **fold 等价**(Phase 2):fold 后的 ANN 召回 ≈ 全量重建(recall 对照,允许小幅差);原子替换期间查询不失败。
- **fold 并发**:fold 进行中连续查询不抛错、不读到半写文件。
- **调度**(Phase 3):queued 在窗口触发、`_scale_building` 串行不重入。
- 沿用既有不变量测试:[0,1]/tau、成本分离(active/delta 不触发 base 全量)。

## 11. 不做 / YAGNI

- 不做 HNSW 在线**删除回收**(删除靠周期全量重建回收;`mark_deleted` 仅按需)。
- 不做真负载自适应调度(Q4=A:固定低峰窗口即可)。
- 不做 push-based 局部 PPR(review P0-3 连带项,仅当 base 边 >10M 实测 matvec 仍慢再议)。
- Phase 1 不引入可变磁盘索引(增量 fold 属 Phase 2);Phase 1 delta 一律暴力。
