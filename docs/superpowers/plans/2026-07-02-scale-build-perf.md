# build_scale_index 提速与内存节食 Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景(真机事故二号)**:49万对象库跑 CLI `index` 阶段极慢且内存涨破 64GB 拖死机器。诊断(file:line 见各任务):①同一批 KG 向量的 hnsw 被建两遍(emb_synonym_edges 用完即弃 + save_scale_index 再建);②emb_synonym_edges 归一化整矩阵拷贝(hnswlib cosine 内部已归一化,纯浪费 2GB);③边表为 Python 元组×2向向 + seen_undir 元组 set(千万级边≈5GB+);④build_matrix 攒 49万小 ndarray 再 vstack(峰值 2×);⑤kg/chunk 两个 2GB 矩阵进 VectorCache 常驻;⑥中间产物活到函数尾。

**Goal**:构建时间 −40%+,峰值内存 ~40GB→≤15GB;检索语义零变化(索引文件内容等价)。

**Tech Stack**:numpy/scipy/hnswlib,pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`,测试在 worktree `backend/`。基线 **1446 passed, 1 skipped**。

## Global Constraints
- **产物等价**:ann.bin 的向量集合/labels、graph.npz 的 CSR 数值、同义边集合(id 对+权重)与现实现**语义等价**(hnsw 近似性使邻居集合可能有边际差异——同参数同 seed 同数据下 add_items 顺序不变则确定;保持插入顺序不变)。既有等价性/回归测试全绿是硬门。
- **cosine 归一化等价前提**:hnswlib space="cosine" 对存入与查询向量内部归一化,显式 `M/norms` 拷贝可去(在报告中写明此等价论证)。
- 分段计时(PR#154 的 `_timed`/on_stage)保持工作;若阶段重排,段名/语义相应更新并在报告说明。
- `_gather_kg_graph` 的 **delta/字符串路径对非 build 调用方 byte-identical**(`_active_kg_delta` 及 scoped 路径不受影响)。
- 新配置一律 pydantic-settings v2 `validation_alias`。

---

## Task 1: hnsw 建一次用两处 + 同义边瘦身 + ef_construction 可配

**Files:** `backend/app/services/kg/ppr.py`(emb_synonym_edges)、`backend/app/services/kg/scale_index.py`(save_scale_index)、`backend/app/services/sqlite_repository.py`(build_scale_index/_gather_kg_graph)、`backend/app/core/config.py`;Tests:既有 test_scale_index*/test_ppr* 全绿 + 新增。

- [ ] Step 1 测试先行:
  - `emb_synonym_edges(ids, matrix, prebuilt_index=idx)`:传入预建 hnsw 时结果与不传(自建)一致(小固定数据集,同参数)。
  - `emb_synonym_edges` 不再整矩阵归一化拷贝:monkeypatch/spy 断言无 `matrix / norms` 级分配(可断言函数内不修改输入矩阵且输出等价于旧实现 oracle——把旧实现拷进测试当 oracle 对照)。
  - 向量化去重:构造有重复邻居对的数据,输出去重且 (a,b) 有序、含 sim 权重,与旧实现输出集合相等。
  - `save_scale_index(..., prebuilt_ann=idx)`:落盘 ann.bin 可被 load 且 knn 结果与传 ann_vectors 自建一致(同 seed 下逐位一致可期,断言 labels 集合+top1 一致即可)。
  - `build_scale_index` 端到端:同义边启用时 hnswlib.Index 构造次数(spy/monkeypatch 计数)从 2 降为 1(chunk ANN 另计);manifest/检索既有断言不变。
  - config:`hnsw_ef_construction`(默认 200,validation_alias `HNSW_EF_CONSTRUCTION`)接进三处 init_index(synonym 复用点、ann.bin、chunk_ann)。
- [ ] Step 2 实现:
  - `emb_synonym_edges(..., prebuilt_index=None)`:None 时内部自建(现行为,但去掉归一化拷贝);传入时直接 `knn_query`。KNN 后处理向量化:mask=sim≥threshold & label≠row;pair 编码 `min*n+max` int64 → `np.unique` → 解码产出 [(ids[a],ids[b],sim)](sim 取该对首次出现值或最大值——与旧实现一致取遍历首见;报告写明选择)。
  - `build_scale_index` 重排:先 `_vector_matrix`(kg)→ 建 hnsw(一次,ef 可配)→ `emb_synonym_edges(..., prebuilt_index=...)` 得同义边 → `_gather_kg_graph(notebook_id, synonym_edges=...)`(新可选参数:非 None 时 gather 内跳过向量加载与 emb_synonym_edges 调用,直接并入 extra_edges;None 时现行为不变)→ … → `save_scale_index(..., prebuilt_ann=hnsw)`。
  - `save_scale_index(..., prebuilt_ann=None)`:非 None 直接 `save_index`(跳过 init+add);None 现行为。注意 prebuilt 的 max_elements/元素集合必须与 ann_labels 对齐(断言行数相等,不等则回退自建+日志)。
  - 分段计时:新增/调整段名(如 `ann_build` 独立成段,persist 只剩写盘)——报告说明,CLI 打印口径同步。
- [ ] Step 3 回归:`pytest tests/ -q -k "scale or ppr or synonym"` → 全量(基线 1446)。
- [ ] Step 4 提交 `perf(kg): 索引构建 hnsw 只建一次(同义边 KNN 复用持久化 ANN)+ 去归一化拷贝 + ef_construction 可配`。

## Task 2: 内存节食 — 边数组化 + build_matrix 预分配 + 构建不占缓存 + 及时释放

**Files:** `backend/app/services/sqlite_repository.py`、`backend/app/services/kg/scale_index.py`(build_transition 数组快路径)、`backend/app/services/vector_index.py`;Tests 同域。

- [ ] Step 1 测试先行:
  - `_gather_kg_graph(..., as_arrays=True)`:返回 (node_ids, (src_idx,tgt_idx,w) int32/float32 数组, chunk_ids, kg_node_ids, membership_counts),与 as_arrays=False 的字符串边经 index 映射后**集合相等**(小数据 oracle 对照,含 hub 边/去重/双向)。默认 False 路径 byte-identical(既有测试)。
  - `build_transition` 接受数组三元组时输出 CSR 与字符串边路径逐元素相等。
  - `build_matrix(rows, n_hint=N)`:预分配填充,输出与无 hint 逐位一致;n_hint 偏大/偏小都正确(收尾裁剪/溢出回退 append)。
  - build 路径不写 VectorCache:build_scale_index 跑完后 `_vector_cache._store` 无 `{nb}:matrix:*` 新键(直载);查询路径 `_vector_matrix` 缓存行为不变。
- [ ] Step 2 实现:
  - gather 数组路径:先扫 cluster_groups 把 hub id 预 append 进 node_ids → 建 `index={nid:i}` → 各边源(relations/memberships/extra/hub)直接产 int 对,`np.unique` 编码键去重后一次性生成双向数组;`del` relations/memberships/elem 映射等中间量。
  - build_scale_index 用 as_arrays=True;边数组喂 build_transition 快路径;CSR 建成后 `del` 边数组;矩阵直载(`build_matrix` 直调,带 COUNT n_hint,不经 `_vector_cache`);chunk 矩阵推迟到 chunk ANN 前才载、用完即 del;阶段间 `gc.collect()`。
  - `_ent_chunk_map` 输出用完(memberships/counts 生成后)即 del。
- [ ] Step 3 回归 + 一个小规模**峰值内存 sanity**(可选:tracemalloc 断言数组路径 < 字符串路径,不作硬门)。
- [ ] Step 4 提交 `perf(kg): 索引构建内存节食(边int数组化+matrix预分配+直载不占缓存+及时释放)`。

## Task 3: 向量 BLOB 化(双读兼容 + 写侧切换 + backfill CLI)

**Files:** 写点(存 embeddings 的所有 INSERT)、读点全审计、`backend/app/services/vector_index.py`、`backend/app/services/batch_ingest.py`(backfill 子命令)、README*;Tests 新建。

- [ ] Step 1 **读点审计**:grep 所有读 `vector` 列的代码(json.loads/orjson `_fast_loads`/build_matrix rows/_stream_seed_reps Pass B/增量融合/拷贝 notebook 等),列清单进报告;每处改双格式:`bytes/memoryview → np.frombuffer(buf, dtype=np.float32)`,str → 原 json 路径。
- [ ] Step 2 测试:旧 JSON 行与新 BLOB 行混存时 build_matrix/_stream_seed_reps/检索全等价;写侧新入向量为 BLOB;backfill 幂等(跑两遍结果一致)且转换后所有读点仍绿。
- [ ] Step 3 写侧:统一经一个 `encode_vector(vec)->bytes` helper(float32 tobytes);所有 embeddings INSERT 改用。
- [ ] Step 4 backfill:`batch_ingest vectors-to-blob --notebook-id nb-xxx`(分批事务,每批打印进度,支持全部 embeddings 表);README.md+README_zh.md 记用法(CLI 交付物惯例)。
- [ ] Step 5 全量回归 + 提交 `perf(storage): 向量存储 BLOB 化(frombuffer 零解析,双读兼容+backfill CLI)`。

## 收尾
- 全分支终审(opus)→ rebase → push → PR(描述带真机事故、诊断表、预期收益、部署操作顺序:先合→部署→跑 backfill→CLI 建索引看分段计时)。
