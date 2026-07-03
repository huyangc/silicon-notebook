# 运行时向量维度截断(EMBED_RUNTIME_DIM)切换 runbook

> 把检索的相似度空间从原生维(如 Qwen3-Embedding-8B 的 4096)截到 1024/2048(MRL 前缀截断 + re-normalize),
> 使进程内矩阵 / ANN 索引 / fold-build 内存峰值按截断维计(4096→1024 约 ÷4),不改写库内 4096 原向量(真相源,可逆)。
> 设计与任务分解见 [docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md](superpowers/specs/2026-07-03-runtime-dim-1024-plan.md)。

## 前提

- `workers=1`(`scripts/prod.sh`),进程内 `_scale_building` 去重有效。
- `EMBED_DIM` = 库内向量的**存储/原生维**(生产 4096),**切换期一个字都不动**。
  改小 `EMBED_DIM` 会让存量向量被当异维残留丢光(全库静默失忆)—— 启动校验会拦 `EMBED_RUNTIME_DIM > EMBED_DIM`,但不拦「误改小 EMBED_DIM」,故靠此禁令。
- 已跑过 MRL 截断 spike(`python -m app.eval.mrl_truncation`)确认目标维质量可接受。

## 切换序列

1. **切换前基线**(留档,便于事后对照):
   ```bash
   cd backend && python -m app.eval.mrl_truncation --tables knowledge,chunk,relation --sample-rows 50000
   # embed 端点在线时再记 gold 基线:
   cd backend && python -m app.eval.mrl_truncation --gold app/eval/recall_gold.yaml --notebook <大库id>
   ```
2. **备份现有索引**(决定回滚成本 —— full rebuild 原地覆盖工件,非 tmp+rename):
   ```bash
   cp -r <storage_dir>/kg_index <storage_dir>/kg_index.bak-native
   ```
3. **临时关 auto-fold**(双保险;代码已让 dim 失配的 fold 拒绝+转 full,此步只是避免夜间 idle 窗抢跑):
   `.env` 加 `SCALE_AUTO_FOLD_ON_ADD=false`。
4. **开截断**:`.env` 加 `EMBED_RUNTIME_DIM=1024`(**EMBED_DIM=4096 不动**)→ `scripts/backend.sh restart`。
   - 重启后即时:暴力面(小库矩阵、element、delta 补召回)经统一 helper 立即在 1024 空间自洽;
   - 三条持久 ANN(kg/chunk/relation)manifest 仍是旧维 → `scale_index_status` 报 `state=stale, stale_reason=dim_mismatch`,查询侧守卫降级(等同重建前,不更糟)并发 `dim_mismatch` 事件。
5. **逐库全量重建**(把 ANN 建到新空间):
   ```bash
   # 对每个有 manifest 的库(全部 base + 已索引大个人库):
   curl -XPOST .../api/notebooks/<nb>/scale-index/rebuild -d '{"when":"now","mode":"full"}'
   ```
   - **务必 mode=full**(dim 失配下 auto 也会解析为 full,但显式写死,与「大库绝不能 auto/fold」教训同源);
   - 覆盖清单:`ls <storage_dir>/kg_index/` 下**所有** manifest.json 的库,漏任一 base → federated 对它永久 `ann_sources_skipped`;
   - 「刷新图谱」(`/unified-kg/rebuild`)只重聚类不产 ANN,**不是**切换动作;
   - 预估(16C,未实测,先对最大库用 `when=idle` 试跑读 `scale_index_build` 9 段校准):87万 KG + 80万 relation + 21万 chunk @1024,单大库约 20–60min,RSS 峰值 6–9GB(逐行截断进预分配矩阵,峰值才 ÷4);建议低峰/idle 窗。
6. **重建后**:build 收尾已 pop 进程缓存(理论上立即可见);保守起见验收前再 `backend.sh restart` 一次。恢复 `SCALE_AUTO_FOLD_ON_ADD=true`。
7. 切换窗口若在持续摄取:新向量照旧 4096 落库(安全);未 fold 的 delta 对查询只 FTS 可见(`SCALE_SEARCH_INCLUDE_DELTA` 默认 false),下次 full rebuild 一并收进。

## 回滚

- **有备份**:`.env` 删 `EMBED_RUNTIME_DIM` → 还原 `kg_index.bak-native` → restart。分钟级。
- **无备份**:删配置 + restart + 每大库再 `mode=full` 重建。**4096 原向量永在 DB,任何方向重建无损** —— 这是本方案的安全底座。

## 验收清单

- **manifest**:全部 `kg_index/*/manifest.json` 的 `dim == 1024`;`n_ann/n_chunk_ann/n_relation_ann` 与四表行数吻合。
- **事件归零**(`python scripts/diag_slow.py --since 24` 的事件段):`dim_mismatch`、`scale_fold_refused`、`kg_bruteforce_refused`、`chunk_bruteforce_skipped(large_library_no_ann)`、`relation_scoring_skipped`、`element_scoring_skipped`、`scale_ppr_bailout(ann_sources_skipped>0)` 均应为 0;每大库有完整 `scale_index_build` 9 段。
- **diag_slow 维度段**:`report_env` 显示 `EMBED_DIM=4096` / `EMBED_RUNTIME_DIM=1024`;规模画像段**不**报「维度失配」(判据已改为 manifest 应 == 运行时维,库内向量恒为存储维属正常)。
- **资源**:稳态 RSS 与夜间 fold 峰值较切换前显著下降(目标 ÷4 量级)。
- **质量**:`mrl_truncation --gold` 复测,recall@12 / MRR 对第 1 步基线的相对衰减在预算内。
- **前端**:大库问答恢复语义引用、图谱语义搜索命中、scale 徽章无 stale。

## 阈值重校准(切换后按优先级观察)

存量向量↔存量向量的相似度阈值在 1024 空间会整体偏移(MRL 前缀维承载粗语义,中高相似区间 cosine 上移 → 候选集单向膨胀)。方法:切换前采样已知同义对(merge 审阅 approved)与随机对,在两空间各算 cosine 按分位数平移阈值。优先级:

| 优先级 | 阈值 | 位置 | 量化工具 |
|---|---|---|---|
| P0 | `PPR_EMB_SYNONYM_THRESHOLD` | config.py | `app/eval/retrieval_metrics.py`(graph/reasoning recall@12/MRR) |
| P0 | `RELEVANCE_FLOOR` / `tau`(grounded 判档) | retrieval 常量 | recall_gold + 真机对照;守 [0,1]/tau 不变量 |
| P1 | 聚类 `hi=0.94/lo=0.82`、Tier2 `lo=0.82` | kg_merge.py(硬编码,需先 settings 化) | rebuild 后 auto/pending 计数 + 抽检 |
| P2 | `KG_CONFLICT_SIM_THRESHOLD` | config.py(默认关) | test_conflict_e2e 抽检 |

> 阈值调整经缓存版本键(已含 runtime_dim + 阈值项)自动失效 PPR 图缓存/scale 探针。
