# 运行时向量维度截断(EMBED_RUNTIME_DIM)切换 runbook

> 把检索的相似度空间从原生维(如 Qwen3-Embedding-8B 的 4096)截到 1024/2048(MRL 前缀截断 + re-normalize),
> 使进程内矩阵 / ANN 索引 / fold-build 内存峰值按截断维计(4096→1024 约 ÷4),不改写库内 4096 原向量(真相源,可逆)。
> 设计与任务分解见 [docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md](superpowers/specs/2026-07-03-runtime-dim-1024-plan.md)。

## 前提

- 使用部署规定的进程拓扑；SQLite 保持单进程，PostgreSQL 的在线/离线构建共用每库跨进程构建锁。切换期间避免并行发起同一库的构建。
- `EMBED_DIM` = 库内向量的**存储/原生维**(生产 4096),**切换期一个字都不动**。
  改小 `EMBED_DIM` 会让存量向量被当异维残留丢光(全库静默失忆)—— 启动校验会拦 `EMBED_RUNTIME_DIM > EMBED_DIM`,但不拦「误改小 EMBED_DIM」,故靠此禁令。
- 已用代表性数据完成 MRL 截断质量评估，确认目标维可接受。下列 `app.eval.mrl_truncation` 命令只支持 SQLite；PostgreSQL 部署需先通过适用的离线评估确认质量，不能直接把 PostgreSQL URL 传给该工具。

## 切换序列

1. **切换前基线**(留档,便于事后对照；以下命令均从仓库根目录执行):
   ```bash
   PYTHONPATH=backend python -m app.eval.mrl_truncation --tables knowledge,chunk,relation --sample-rows 50000
   # embed 端点在线时再记 gold 基线:
   PYTHONPATH=backend python -m app.eval.mrl_truncation --gold backend/app/eval/recall_gold.yaml --notebook <大库id>
   ```
   记录切换前 `EMBED_RUNTIME_DIM` 与 `SCALE_AUTO_FOLD_ON_ADD` 的有效值，供完成或回滚时恢复。
2. **备份现有索引并检查磁盘余量**：在维护窗口等待在途构建结束，再停止服务及离线构建者，保持停止到第 4 步，避免备份期间索引换代；备份目录应为本次切换新建。
   ```bash
   cp -r <storage_dir>/kg_index <storage_dir>/kg_index.bak-native
   ```
   full rebuild 与 fold 都先写入每次构建独有的 `.tmp-<claim_token>`，再经 `live → .old`、`temporary → live` 两次 rename 发布；成功后 `.old` 会被清理，不能当作长期回滚备份。
   容量预算应包含现有索引、独立备份、正在构建的新索引暂存空间及增长余量；逐库构建按最大单库预留暂存空间，并发构建则累加。发布前失败保留旧索引；中断后的 `.old` / `.tmp-*` 按[运维恢复说明](./operations_zh.md#old--tmp-claim_token-残留与人工恢复)检查处理。
3. **关闭 auto-fold**：`.env` 设置 `SCALE_AUTO_FOLD_ON_ADD=false`，在下一步启动时生效。
4. **开截断**：`.env` 加 `EMBED_RUNTIME_DIM=1024`（**EMBED_DIM=4096 不动**），按部署方式启动服务（使用仓库后端脚本时执行 `scripts/backend.sh start`），确认 `/api/ready` 就绪后继续。
   - 重启后即时:暴力面(小库矩阵、element、delta 补召回)经统一 helper 立即在 1024 空间自洽;
   - 三条持久 ANN(kg/chunk/relation)manifest 仍是旧维 → `scale_index_status` 报 `state=stale, stale_reason=dim_mismatch`,查询侧守卫降级(等同重建前,不更糟)并发 `dim_mismatch` 事件。
5. **逐库全量重建**(把 ANN 建到新空间):
   ```bash
   # 对每个有 manifest 的库(全部 base + 已索引大个人库):
   curl --config '<私有 curl 认证配置文件>' --fail-with-body \
     -X POST '<部署地址>/api/notebooks/<nb>/scale-index/rebuild' \
     -H 'Content-Type: application/json' \
     -d '{"when":"now","mode":"full"}'
   ```
   认证文件仅本人可读，内含 `header = "Authorization: Bearer <登录会话 token>"`，会话须有该笔记本 `kg:write` 权限；每库检查 `/api/notebooks/<nb>/scale-index/status`，等待构建成功并确认新 manifest 的维度后再继续，提交请求成功不等于构建完成。
   - **务必 mode=full**(dim 失配下 auto 也会解析为 full,但显式写死,与「大库绝不能 auto/fold」教训同源);
   - 覆盖清单:`ls <storage_dir>/kg_index/` 下**所有** manifest.json 的库,漏任一 base → federated 对它永久 `ann_sources_skipped`;
   - 「刷新图谱」(`/unified-kg/rebuild`)只重聚类不产 ANN,**不是**切换动作;
   - 预估(16C,未实测,先对最大库用 `when=idle` 试跑读 `scale_index_build` 9 段校准):87万 KG + 80万 relation + 21万 chunk @1024,单大库约 20–60min,RSS 峰值 6–9GB(逐行截断进预分配矩阵,峰值才 ÷4);建议低峰/idle 窗。
6. **重建后**：服务通过 manifest 磁盘签名识别新一代索引，后续请求自动加载。完成下方验收后，把 `SCALE_AUTO_FOLD_ON_ADD` 恢复为切换前的值，再按部署方式重启使配置生效，并确认 `/api/ready` 就绪。
7. 切换窗口若在持续摄取:新向量照旧 4096 落库(安全);未 fold 的 delta 对查询只 FTS 可见(`SCALE_SEARCH_INCLUDE_DELTA` 默认 false),下次 full rebuild 一并收进。

## 回滚

- **有备份**：先停止所有服务进程及离线构建者，保留当前索引用于排查，再还原独立备份；将 `EMBED_RUNTIME_DIM` 和 `SCALE_AUTO_FOLD_ON_ADD` 恢复为切换前的值，随后启动并检查 `/api/ready`、各库状态与检索。备份之后的新增内容仍需 fold / full rebuild 才能进入持久 ANN。
- **无备份**：恢复原 `EMBED_RUNTIME_DIM`，保持 auto-fold 关闭，重启后逐库 `mode=full` 重建并验收，最后恢复原 auto-fold 配置。原生向量未被本次截断改写，可据此重建；构建时长取决于库规模。

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
