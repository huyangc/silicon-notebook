# 运行时向量截断 4096→1024 实现计划

> 基线:worktree `claude/mrl-spike`(≈master)。所有 file:line 出自 5 路盘点并已交叉对表。
> 核心不变量:**DB 四张 embeddings 表永远存 4096 原生向量(真相源);截断只发生在读侧/相似度空间;查询侧与语料侧共用同一 helper,逐位同语义。**

---

## 1. 设计定案

### 1.1 新配置

```python
# backend/app/core/config.py(embed_dim=Field(1024,...) 在 :106 旁新增)
embed_runtime_dim: int = Field(0, validation_alias="EMBED_RUNTIME_DIM")
```

- **命名统一为 `EMBED_RUNTIME_DIM`**(区域 4 方案;区域 5 的 `RUNTIME_SIM_DIM` 弃用,与 `EMBED_DIM` 配对语义更清晰)。
- **默认 0 = 关闭**(不截断,行为零变化;所有代码任务可先合、生产不受影响,部署显式开)。
- **必须 `validation_alias`**:本仓库 pydantic-settings v2,`Field(env=...)` 是死的([pydantic env别名坑]);加 alias 后测试构造须用 `Settings(EMBED_RUNTIME_DIM=...)` 形式。
- **启动校验收口在 config.py `model_validator`**(Settings 构造即校验,后端 create_app、`batch_ingest.py:937` 的 `Settings()`、eval 的 `get_settings()` 三类入口一处全覆盖):
  - `embed_runtime_dim < 0` 或 `0 < embed_runtime_dim` 且 `> embed_dim` → 硬 RuntimeError(出声,呼应 PR#175 预检哲学)。
  - `embed_dim` 语义**固定为存储/原生维**(生产恒 4096,embedder 不带 dimensions 参数照旧,`embedding_dashscope.py:48` 不动)。**绝对禁止用改 EMBED_DIM=1024 实现切换**——`arr.size == settings.embed_dim` 过滤器(`sqlite_repository.py:4615/4619`、`:4723`、`:6093`)会把全部 4096 存量向量当异维残留丢光,全库静默失忆。

### 1.2 截断 helper:三个收口 + 四个显式旁路

**Helper 放 `backend/app/services/vector_index.py`**(decode/encode/build_matrix 同文件,读侧天然路过):

```python
def resolve_runtime_dim(settings=None) -> int:
    """返回生效截断维;0 = 不截断。"""

def truncate_vec(arr: np.ndarray, runtime_dim: int) -> np.ndarray:
    """arr[:runtime_dim] + L2 renormalize。全仓唯一截断语义(截后归一),
    与 mrl_truncation 的 truncated_sims 口径用测试锁死等价。"""
```

统一拍板:**截断+renorm 是原子操作**,不允许「有的截了归一有的没归一」。理由:build_matrix 的 matmul 假设行单位范数;hnswlib cosine 内部归一虽免费,但口径唯一才可与 eval 交叉验证。

**收口 A — 语料侧:`vector_index.py:42-107 build_matrix()`**
截断插在 `decode_vector` 返回后、第 93-95 行 L2 归一**之前**;且必须在第 89-91 行「首行定维」判定**之前**执行(区域 3 陷阱:否则混存期异维行被静默跳过整批丢)。归一在截断后跑,renorm 自动完成——代码证据 `vector_index.py:93-95`。`runtime_dim` 参数默认 `None`=读 settings,显式 `0`=不截(供测试与例外)。一处覆盖:`_vector_matrix` 全四表(7481-7501)、`_delta_vector_matrix`(9155)、`build_scale_index` 三矩阵(8388/8490/8515)、fold `_delta_vecs`(8664-8677)、三处 delta 暴力(9820/9991/10362)、`_retrieve_scored` erows(10085)、`_hydrate_chunk_candidates`(10412)、xbridge(9259-9264)、PPR 图 emb_synonym(7722)、`_gather_kg_graph`(8138)。

**收口 B — 查询侧:`sqlite_repository.py:7400-7407 _embed_query()`**
对返回值 `truncate_vec`。一处覆盖全部 7 个查询消费点(2304/9438/9903/9924/10007/10246/10257)。

**收口 C — 维度契约:manifest**
- `sqlite_repository.py:8541`:`manifest["dim"] = int(ann_vectors.shape[1])`(取**工件实际维**,不信配置)——除 helper 外最关键的单行。
- `:7923 _open_scale_ann` 与 `:8662 fold` 的 `manifest.get("dim", ...)` fallback 改为运行时生效维(`resolve_runtime_dim() or settings.embed_dim`)。
- `:8402` 空矩阵占位同步。
- 9 处 manifest-dim 守卫(2308/4726/7923/8662/9275/9452/9788/9960/10329)**比较逻辑不改**——两侧截断后自动通过;唯 `:4726-4728` 现比 `settings.embed_dim` 须改比运行时生效维。

**四个显式旁路(不走 build_matrix,逐条 decode,各接 `truncate_vec`)**:
1. `_gather_elements`(7428-7429,element 兜底逐向量 cosine——`retrieval.py:250-252` 对 len 不等静默返 0.0);
2. `incremental_fuse_source` Tier2 暴力(4613-4635):**先按存储维 `embed_dim` 过滤(4615/4619 语义不变),后截断**——两步分离;
3. `_tier2_bridge_candidates_ann` query 侧(4720-4728):new_vecs 截断 + 守卫改比运行时维;
4. `_stream_seed_reps` Pass B **两个分支**(6088-6093,legacy-JSON 分支 6091 绕过 decode_vector 是已知漏点):过滤(6093,存储维)通过后、累加(6102)前截断。

**聚类/融合空间拍板:统一到运行时空间(1024)**。理由:Tier2 ANN 分支查的是持久 kg ANN(切换后 1024),暴力分支若留 4096 则同功能两分支 lo=0.82 语义分裂;seed_reps 统一后 rep 累加内存亦 ÷4。代价=阈值漂移,进第 4 节清单。`resolve_notebook_conflicts`(5243-5247)同步接 helper 并**补上缺失的存储维门**(现连 dim 过滤都没有,`conflict_detect._cosine_sim` :314-321 的 zip 混维会算出错误值)。

### 1.3 明确不截断(负面清单,写进代码注释+守卫测试)

| 位点 | 理由 |
|---|---|
| 写路径 6 个 INSERT:`sqlite_repository.py:3292/3317(3321)/3370/3413/3515` + `_knowledge_vectors` 懒回填 `:3561-3572` | embedder→encode_vector 直连零变换,4096 真相源 |
| `_backfill_knowledge/relation_embeddings`(3575/3599)、`reembed_kg.py`、`backfill_relation_embeddings.py`、`backfill_kg_embeddings.py`、`batch_ingest.py run_embed`(560) | 薄包装自动跟随写路径 |
| `batch_ingest.py vectors-to-blob`(598/649/728) | 改写真相源列本身,必须逐字节保真 |
| `copy_notebook`(1941-1974) | 维度无关整列拷贝 |
| **`decode_vector` 本身**(vector_index.py:26) | ①batch_ingest BLOB 回填走 decode→encode 会物理截断真相源;②mrl_truncation 需原维基线;③seed_reps JSON 分支本就绕过它——不能当全局截断点 |
| `app/eval/mrl_truncation.py` 全文件 | 原生基线标定工具;:322 SystemExit 护栏别删;docstring 加「EMBED_DIM 恒为存储维」 |
| 存储维过滤器 4615/4619、4723、6093 | 继续对照 `embed_dim`(过滤旧 embedder 异维残留) |
| `retrieval_metrics.py`(55/61) | 走 repo 生产栈自动跟随运行时维——正是想要的 |
| FakeEmbedder(embedding.py:47) | 保持按 `embed_dim` 出向量,模拟端点原生输出;否则测试全在 runtime==storage 下跑,掩盖混维 bug |

### 1.4 embed_dim 12 个消费点判定表(区域 4 结论,照单执行)

- **保留存储维语义**:FakeEmbedder 构造(embedding.py:47)、写路径、过滤器 4615/4619/4723/6093、`_cluster_input_version` 既有项(5690)。
- **改为运行时生效维**:manifest 写入(8541,取 shape[1])、tier2 ANN 守卫(4726-4728)、load/fold fallback(7923/8662)、空矩阵占位(8402)。
- **版本键追加 runtime_dim**(非替换):5690、7744-7749、7685-7687、`_vector_matrix` 缓存键。

---

## 2. 任务序列(subagent-per-task;每任务独立可合,默认关闭保证零行为变化)

依赖图:`T0 → T1 → {T2, T3, T4}(并行)→ T5 → T6 → {T7, T8, T9}(并行)→ T10 → T11`

**T0(S)配置+启动校验** — 独立可合
- 改:`backend/app/core/config.py`(新字段+model_validator)。
- 验收测试:alias 环境变量读入生效;`runtime>embed_dim`/负值构造即硬错;`runtime=0` 等价关闭;`Settings(EMBED_RUNTIME_DIM=...)` alias 构造可用(防 kwarg 坑)。

**T1(M)截断 helper + build_matrix 接线** — 独立可合
- 改:`backend/app/services/vector_index.py`(`truncate_vec`/`resolve_runtime_dim`;build_matrix 截断插 82-92 区间、dim 判定前)。
- 验收测试:①截断+renorm 后输出维==runtime_dim 且每行范数=1;②4096 存量行与假想 1024 行混存不互丢;③runtime=0 时输出与现行为逐位相同(回归);④**与 `mrl_truncation.truncated_sims` 数值等价交叉断言**(锁口径);⑤扩 `tests/test_ask_vector_matrix.py:159/195` 混格式 oracle 成混维版。

**T2(S)_embed_query 截断** — 依赖 T1
- 改:`sqlite_repository.py:7400-7407`。
- 验收测试:输出维==runtime_dim;**端到端不变量测试:`_embed_query` 输出维 == `build_matrix` 输出列数**(同空间断言,全计划的核心回归)。

**T3(M)缓存/版本键纳入 runtime_dim** — 依赖 T0,可与 T2 并行
- 改:`_vector_matrix` 缓存键(7481-7501)、PPR 图缓存 version(7685-7687)、`_probe_scale_version_signal` settings_tail(7744-7749)、`_cluster_input_version`(5690)。
- 验收测试:runtime_dim 变更→矩阵缓存 miss、scale 索引判 stale、rebuild 版本闸不跳过;扩 `test_scale_index_version_probe.py`、`test_rebuild_cache.py`、`test_query_hotpath_cache.py`。

**T4(M)四旁路接 helper(KG 融合/聚类/element/conflict)** — 依赖 T1
- 改:`sqlite_repository.py` 7428-7429(_gather_elements)、4613-4635(Tier2 暴力,两步分离)、4720-4728(Tier2 ANN + 守卫改运行时维)、6088-6093(seed_reps 双分支)、5243-5247(conflict 补存储维门+截断)。
- 验收测试(**全部用「DB 存 4096(或 fake 32)+ runtime 1024(或 16)」双维 fixture**):Tier2 暴力/ANN 仍产候选而非空 [];`_stream_seed_reps` reps 非空且 rep 维==runtime;element cosine 非零;`conflict_detect._cosine_sim` 混维显式拒绝(改零容忍)而非 zip 静默。新增 FakeEmbedder 双维 conftest fixture。

**T5(M)ANN 构建/manifest 真相化** — 依赖 T1
- 改:`sqlite_repository.py:8541`(shape[1])、8402、7923;`kg/scale_index.py:270-287` 回退重建路径确认消费的是已截断矩阵。
- 验收测试:**manifest.dim == runtime_dim == ann.bin 实际维 == chunk/relation ANN 维** 三方一致断言(扩 `test_scale_index_repo.py`);双维 fixture 下 build→open→knn_query 端到端命中。

**T6(M)fold/mode 闸 + 缓存失效修复** — 依赖 T5
- 改:`_resolve_scale_mode`(8863-8878)加「manifest-dim ≠ 运行时生效维 → 强制 full」;`fold_scale_index_delta`(8662-8722)add_items 前断言 delta 矩阵列数==manifest dim,失配**拒 fold + 发事件 + 建议 full**(而非 hnswlib 硬错被 `_run_scale_op` 吞成一行日志);`scale_index_status`(8814-8861)dim 失配 → state=stale(带 reason,前端徽章自动可见);**build_scale_index 收尾补 `_scale_idx_cache.pop`**(对齐 fold 的 8755——修复「full rebuild 后热进程看不见新索引」已核实缺陷)。
- 验收测试:旧 4096 索引 + runtime 1024 → status=stale、mode=auto 解析为 full、fold 拒绝且有事件;rebuild 完成后同进程立即拿到新实例(新增「rebuild 后无 DB 变更须换新实例」用例,`test_auto_scale_index.py`/`test_scale_idx_cache_lru.py` 扩)。

**T7(S)dim_mismatch 可观测化** — 依赖 T1,可与 T6 并行
- 改:9 处静默守卫加一次性 `dim_mismatch` 事件(复用 model_error/事件机制):`_semantic_search` 2310、tier2 ANN 4727、xbridge 9277、scale_ppr 9453、relation ANN 9789、kg 对象 9961、chunk ANN 10330、`query_sims`/`top_k_sims`(vector_index.py:115/140)加 logger.warning。
- 验收测试:每守卫「失配→预期降级路径+事件断言」矩阵。

**T8(S)diag_slow.py 修正** — 依赖 T0
- 改:`scripts/diag_slow.py:420-427` 判据改为「manifest.dim == 运行时生效维 且 运行时维 <= live_dim」,分别打印存储维/运行维;:529 report_env 加 EMBED_RUNTIME_DIM。失配判据抽纯函数配单测(该脚本现零测试)。
- 验收:双维下健康库不报警、真失配(manifest≠runtime)报警。**必须与主改造同批上线**,否则切换次日运维被误导全量重建。

**T9(S)死代码清理/标注** — 独立可合
- `_knowledge_vectors`(3522-3573,全仓无调用者)、`_knowledge_similarity` 向量分支(7214-7224,唯一调用方传 {})、`score_knowledge` 原始向量分支+`cosine_sims`(retrieval.py:377-399/261-279,仅测试引用)——删或加「已死,复活须接截断 helper」注释;同步改 `tests/test_retrieval.py`/`test_retrieval_numpy.py` 锁行为的用例。

**T10(M)结构性守卫测试族** — 依赖 T1-T7
- 新增 `tests/test_dim_invariants.py`:
  a. **grep 断言测试**:全仓 `decode_vector`/`np.frombuffer` 直读白名单(build_matrix、batch_ingest、mrl_truncation、四旁路),白名单外新增直读即测试失败;
  b. 写路径维度断言:runtime<embed_dim 配置下,四表新写向量长度仍==embed_dim(各一);
  c. vectors-to-blob 转换前后维度不变;
  d. 旧维索引+新维 delta:fold 拒绝、auto 升 full;
  e. `_embed_query`==build_matrix 同空间端到端(与 T2 合并亦可)。

**T11(S)runbook/文档** — 最后
- `docs/runtime-dim-truncation-runbook.md`(第 3 节内容)+ README/README_zh 配置项说明([CLI要进README] 口径)+ mrl_truncation docstring。

---

## 3. 切换 runbook(采纳区域 5,修正:配置名统一 EMBED_RUNTIME_DIM;T6 合并后 stale/强制 full/缓存失效自动化,人工步骤相应简化但保留双保险)

**前提:T0-T8 全部合入并部署。** workers=1(`scripts/prod.sh:85`),进程内去重有效。

### 3.1 切换序列

1. **切换前 gate(可先做)**:`cd backend && python -m app.eval.mrl_truncation`(overlap 模式,knowledge/chunk/relation 三表,期望 overlap@10≈0.79-0.84)+ `--gold app/eval/recall_gold.yaml` 记录基线 recall@12/MRR。
2. **备份(决定回滚成本)**:`cp -r {storage_dir}/kg_index {storage_dir}/kg_index.bak4096`(估 15-30GB 磁盘;full rebuild 经 `save_scale_index` **原地覆盖** :8534/8558,非 tmp+rename,不备份则无即刻回退)。
3. **当天临时 `SCALE_AUTO_FOLD_ON_ADD=false`**(config.py:182;双保险——T6 已让失配 fold 拒绝+升 full,但避免夜间 idle 窗抢在人工 full 前触发)。
4. `.env` 加 `EMBED_RUNTIME_DIM=1024`(**EMBED_DIM=4096 一个字都不动**)→ `scripts/backend.sh restart`(清空 `_scale_idx_cache`/`_vector_cache` 冷起)。
5. 重启后即时状态:暴力面(小库矩阵、element、delta 补召回)经 helper 立即自洽 1024;三条持久 ANN 面 manifest 仍 4096 → T6 使其显示 **stale(dim 原因)**,查询侧守卫降级(同重建前守卫态,不更糟)。
6. **逐库触发全量重建**:`POST /notebooks/{nb}/scale-index/rebuild` body `{"when":"now","mode":"full"}`(routes.py:895-908),或前端「重建索引」钮(page.tsx:618-619;T6 后 mode=auto 对 dim 失配也解析为 full,但 runbook 仍写死 **mode=full**,与部署机「绝不能 auto/fold」教训同源)。
   - **覆盖清单**:`ls {storage_dir}/kg_index/` 下**所有**有 manifest.json 的库(全部 base + 已索引大个人库);漏任一 base → federated scale_ppr 对它永久 `ann_sources_skipped`。
   - **「刷新图谱」(page.tsx:2346→/unified-kg/rebuild)只重聚类不产 ANN,不是切换动作。**
   - 三 ANN(kg/chunk/relation)共用 manifest 单一 dim,一次 build 同批产出,**不可分面渐进**。
   - 预估(16C,未实测,先对最大库 `when=idle` 试跑一次读 `scale_index_build` 9 段事件校准):87万 kg + 80万 relation + 21万 chunk @1024,单大库 20-60min,RSS 峰值 6-9GB(helper 逐行截断进预分配矩阵,峰值 ÷4 才成立);build 在 daemon 线程与查询同进程,建议低峰或 idle 窗(2-6 点,config.py:174-175)。
7. **重建后**:T6 已修 build 收尾 pop 缓存,理论上立即可见;保守起见验收前再 `backend.sh restart` 一次(双保险)。恢复 `SCALE_AUTO_FOLD_ON_ADD=true`。
8. 切换窗口持续摄取:新摄取照旧 4096 落库(安全);未 fold 的 delta 对查询只有 FTS 可见(`SCALE_SEARCH_INCLUDE_DELTA` 默认 false),full rebuild 会一并收进。

### 3.2 回滚

- **有备份**:`.env` 删 `EMBED_RUNTIME_DIM` → 还原 `kg_index.bak4096` → restart。分钟级。
- **无备份**:删配置 + restart + 全库再 mode=full 重建(每大库再等 20-60min,期间守卫降级态)。**4096 原向量永在 DB,任何方向重建无损**——这是整个方案的安全底座。
- build 中途崩溃会留混合工件(manifest.json 最后写是唯一保护)→ 对该库直接再触发 full。

### 3.3 验收清单

- **manifest**:全部 `kg_index/*/manifest.json` 的 `dim==1024`,n_ann/n_chunk_ann/n_relation_ann 与四表行数吻合,watermark 无 delta。
- **事件归零**(`scripts/diag_slow.py --since 24` 事件段):`kg_bruteforce_refused`、`chunk_bruteforce_skipped(large_library_no_ann)`、`relation_scoring_skipped`、`scale_ppr_bailout(zero_reset & ann_sources_skipped>0)`、`dim_mismatch`(T7 新增,应为 0)、`model_error stage∈{scale_ann_open_*}`。每大库有完整 `scale_index_build` 9 段。
- **diag_slow(T8 已修)**:无「维度失配」告警,env 段显示存储 4096/运行 1024。
- **资源**:稳态 RSS 12-21GB → 约 4-8GB;夜间 fold 峰值 ÷4。
- **质量**:mrl_truncation --gold 复测 recall@12/MRR 对第 1 步基线的相对衰减在预算内(spike 口径 overlap@10≈0.79-0.84);T1 已锁 eval 与线上 helper 数值等价,口径同源。
- **前端**:大库问答恢复语义引用、图谱搜索语义命中、scale 索引徽章无 stale、「N源待索引」随夜间 fold 消退。

---

## 4. 阈值重校准清单(切换后按序观察/重调)

**方法先行**:切换前在真机采样两个分布——已知同义对(merge 审阅队列里 approved 对)与随机无关对——在 4096 与 1024 两空间各算 cosine,**按分位数平移阈值**,不拍脑袋乘系数。预期方向(区域 3 推断,非实测):MRL 前缀维承载粗语义,中高相似区间 cosine 整体上移 → 固定阈值下候选集单向膨胀。

| 优先级 | 阈值 | 位置 | 症状/风险 | 量化工具 |
|---|---|---|---|---|
| P0 | `PPR_EMB_SYNONYM_THRESHOLD=0.83` | config.py:226(消费 7729/8145/8429/9244) | 同义边变密→PPR 传播稀释、图变稠 | `retrieval_metrics run_recall`(graph/reasoning 模式 recall@12/MRR) |
| P0 | RELEVANCE_FLOOR / tau(grounded 判档) | retrieval 常量 | grounded 判档漂移→引用/兜底行为变 | recall_gold + 真机对照 NotebookLM;守 [0,1]/tau 不变量 |
| P1 | 聚类 `hi=0.94 / lo=0.82` | kg_merge.py:307-308(**硬编码,rebuild 未覆写——需先提 settings 化小 PR 才可调**) | hi 越线 auto-candidates 变多(误并,幸有 LLM confirm 0.90 双闸);lo 越线 pending 膨胀撞 max_pending=1000 旧伤 | rebuild 后看 auto/pending 计数 + merge 审阅抽检 |
| P1 | Tier2 `lo=0.82` 三处 | kg_merge.py:180/439、sqlite_repository.py:4706(硬编码) | 桥接候选膨胀→审阅队列压力 | 同上 |
| P2 | `KG_CONFLICT_SIM_THRESHOLD=0.8` | config.py:318 | 特性默认关;开启部署 semantic 候选变多→LLM 成本↑ | test_conflict_e2e 真机抽检 |
| — | **勿动**:`KG_MERGE_CONFIRM/SEPARATE=0.90/0.80`(config.py:244-245,LLM 置信度非余弦)、find_duplicates ≥0.6(纯关键词) | | 误调即引入无关回归 | |

**关于 sa_calibration**:否——`sa_calibration.py` 只测抽取 prompt,零向量消费(区域 4 已核实),与本清单无关。可量化工具就两件:`app/eval/mrl_truncation.py`(overlap + --gold recall/MRR,切换前后同口径)与 `app/eval/retrieval_metrics.py`(走生产检索栈,自动在运行时空间量)。阈值调整会经 T3 的版本键自动失效 PPR 图缓存/scale 版本探针(7685-7687/7746-7748 已含阈值项)。

---

## 5. 风险登记(系统性防御)

| # | 风险 | 防御(已排入任务) |
|---|---|---|
| R1 | **静默零召回是本项目的默认失败模式**:9 处 dim 守卫返回空/continue 全部无事件(vector_index.py:115/140、retrieval.py:251、2310/4727/9277/9453/9789/9961/10330),漏接任何一侧症状只是「检索变差」 | T7 全守卫加 `dim_mismatch` 事件;T2 端到端同空间不变量测试;验收清单以事件归零为准 |
| R2 | 漏点的结构性防御:未来新代码绕过 helper 直读向量做相似度 | T10 grep 断言测试——`decode_vector`/`np.frombuffer` 直读白名单外即红;helper 集中在 vector_index.py 一个文件,review 面小 |
| R3 | EMBED_DIM 被误改成 1024(过滤器丢光存量→全库静默失忆,比零召回更难察觉) | T0 启动校验 runtime>embed_dim 硬错;启动预检(PR#175 同族)加「runtime>0 时采样 DB 首行向量维,< runtime 即硬错」;runbook 加粗禁令 |
| R4 | 缓存/版本键盲区:切 dim 不重启命中旧维矩阵/旧索引恒空(`_vector_matrix` 键无 dim、settings_tail 7744 无 dim、`_cluster_input_version` 5690 无 dim)——[聚类缓存 PR#132] 同族旧伤 | T3 全部版本键纳入 runtime_dim;T6 build 收尾 pop `_scale_idx_cache` |
| R5 | fold×切换窗口 = delta 积山假死事故复刻(4096 delta add 进 1024 ANN 硬错被 `_run_scale_op` 吞) | T6 fold 前维度断言拒绝+事件+`_resolve_scale_mode` 强制 full;runbook 切换日临时关 auto-fold |
| R6 | manifest 撒谎(声明 1024 实为 4096 或反之;hnswlib 错维 load 行为未验证,fail-open 依赖它抛错) | T5 manifest 写 `shape[1]` 实际值 + 三方一致断言测试;T6 status 以 manifest 为准判 stale;不依赖 hnswlib 报错 |
| R7 | 测试盲区:存量测试全在 runtime==storage 单维下跑,对「过滤维/守卫维用错」零检出力 | T4 双维 FakeEmbedder fixture 成为混维测试标配;T10 不变量族;FakeEmbedder 本身保持存储维语义(1.3 负面清单) |
| R8 | pydantic-settings v2 双坑(alias 失效/kwarg 失效) | T0 专项测试锁 alias 读入+alias 构造 |
| R9 | 跨库拼接硬错:`_ppr_graph` 7728 np.vstack、seed_reps 6096 rep_sum——联邦参与库截断不一致直接 ValueError | 收口 A 全局生效即天然同维;联邦部署要求所有实例同一 EMBED_RUNTIME_DIM(写进 runbook);vstack 前可加维度断言(T4 顺手) |
| R10 | diag_slow 误报/失明双向坏 | T8 与主改造同批;判据抽纯函数+单测 |
| R11 | 无即刻回退(kg_index 原地覆盖非版本化) | runbook 强制备份步骤;长期可选改 save_scale_index 为 tmp+rename(不阻塞本计划,可 spawn 后续任务) |
| R12 | 峰值内存不降(若「先整载 4096 再切列」) | T1 实现约束:截断发生在 build_matrix 逐行 decode 处、写入 n_hint 预分配矩阵;runbook 以 RSS ÷4 为验收项反向兜底 |

**遗留待办(不阻塞,合并后 spawn)**:kg_merge.py 阈值 settings 化(P1 重校准前置);`save_scale_index` 原子化(R11);`kg/ppr.py`/`kg/scale_index.py` 内部确认无独立向量读取(区域 1 不确定项——区域 2/3 盘点已覆盖其入口均由 build_matrix 喂入,风险低)。