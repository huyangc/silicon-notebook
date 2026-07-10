# 可中断续跑的 KG rebuild 阶段 — 设计

- 日期:2026-07-10
- 状态:待评审
- 范围:`batch_ingest kg --rebuild-only`(以及在线「刷新图谱」共用的 `rebuild_unified_kg`)里三个小时级 LLM/embed 阶段的断点续跑
- 关联记忆:[[offline-batch-ingest-state]]、[[schema-migration-convention]]、[[kg-entity-merge-p0-state]]、[[efficiency-first-mandate]]、[[pydantic-env-alias-gotcha]]

## 1. 问题

大库跑 `batch_ingest kg --rebuild-only` 时,`rebuild_unified_kg` 在一个 Python 调用里顺序跑十几个子阶段。其中三个是小时级、且**中途被杀会丢掉整段工作**:

| 阶段 | 成本 | 中途被杀现状 |
|---|---|---|
| **merge 审查**(concept auto-candidate LLM 裁决) | 小时级(实测 47215 候选 5.8h) | 决策只在内存 `extra`,**不落库** → 重跑全部重做 |
| **概念描述生成**(per-canonical LLM) | 小时级 | 结果只在 `desc_by_cid`,写簇(stage 7)前不落库 → 中途被杀全丢 |
| **节点向量补全**(embed 每个 KG 节点) | 小时级(50万–100万节点) | 单次结尾写事务 → 中途被杀一条都没落库 |

`--rebuild-only` 走 `force=True`,直接绕过顶层跳过闸,所以重跑从头再来。三段的共性:**「跑一大批 LLM/embed 任务 → 结尾一次性写」**,没有分批 checkpoint,因此不可续跑、无实时进度、且把全部结果攒在内存(百万向量 → 数 GB)。

## 2. 目标 / 非目标

**目标**
- 三个小时级阶段做到**中断后重跑只补未完成部分**,永不重做已完成的 LLM/embed 工作。
- 附带解决:实时进度可见、内存不再攒全量结果。
- 保持现有 fail-open 语义(单块失败不崩 rebuild)、聚类结果与现在完全一致。

**非目标**
- 不给便宜的 CPU 阶段(流式聚类、写簇、pending 刷新)做 checkpoint —— 它们分钟级,重做无痛。
- 不改聚类算法语义(不 bump `CLUSTER_ALGO_VERSION`)。
- 不做「重嵌入未变节点」——节点向量按对象内容寻址,已存在即跳过;`--fresh` 只清 LLM checkpoint,不删向量。
- 不引入分布式/多进程续跑;续跑粒度 = 「杀掉 CLI 进程,重跑同一条命令」。

## 3. 背景:rebuild 的阶段链与续跑现状

`rebuild_unified_kg`(`sqlite_repository.py:6776`)顺序:

1. 顶层跳过闸(`force=False` 且 `cluster_input_version` 未变且已有簇 → 返回缓存)。`--rebuild-only` 用 `force=True` 绕过。
2. concept 流式 seed reps(`_stream_seed_reps` 写 run-scoped `kg_cluster_scratch`)—— CPU,分钟级。
3. `cluster_seeds` 首轮聚类 —— CPU。
4. **merge 审查**:`review_merge_candidates(kg_llm_client, ...)` 裁决 `auto_candidates` → `extra` → 重聚一次。 **← 小时级**
5. **概念描述生成**:PHASE1 串行取证据+算 sig+命中 `concept_clusters.canonical_desc_sig` 缓存;PHASE2 并发 `kg-desc` 线程池跑 LLM。 **← 小时级**
6. 写簇 `_write_cluster_map_streamed`(concept)—— 单事务 DELETE+INSERT,`canonical_desc_sig` 在此落库。
7. claim/formula/procedure 各自 stream+cluster+write —— CPU。
8. pending 刷新(一事务)、`unified_kg_state` UPSERT(记 `cluster_input_version`,**rebuild 的语义提交点**)、scratch 清理。
9. 派生层:canonical 关系 / 共提桥 / 社区 / viz / scale 索引(各有版本闸,fail-open)。

回到 `batch_ingest.run_kg`:

10. **节点向量补全** `backfill_node_embeddings` → `_backfill_knowledge_embeddings` → `_embed_objects_batch`。 **← 小时级**
11. scale 索引构建(base tier / 已有索引)。

**关键正确性前提(已验证)**:canonical id 对相同输入跨 rebuild **稳定**。`cluster_seeds` 里 `cid = id_prefix + min(grp)`(`kg_merge.py:367`),即簇内字典序最小归一化种子名;`auto_candidates = [(canon_id[a], canon_id[b], sim) ...]`(`kg_merge.py:371`)。同一 `input_version`(同 seeds/reps/confirmed/rejected)→ `cluster_seeds` 确定性 → 同样的簇 → 同样的 canonical id → merge 候选对与描述的 canonical id 键均可复用。描述用的是 merge 重聚后的 canonical id,而重聚也确定性(依赖首轮 confirmed + merge-review `extra`,后者续跑时全部来自 checkpoint → 一致)。

## 4. 设计总览

三段共用一张**版本键 checkpoint 表**做"崩溃恢复日志"(节点向量除外,它天然以 `knowledge_embeddings` 为 checkpoint)。核心不变式:

- **checkpoint 按 `input_version` 键**,只在 rebuild 开头 GC 掉非当前版本行 —— 数据/算法版本一变自动失效重算,永不用陈旧决策。
- checkpoint 是**恢复日志、非永久缓存**:写簇后权威存储仍是 `concept_clusters`;checkpoint 只为"在写簇/落库前被杀"提供续跑。
- **落库粒度 = 一块/一组**:merge 审查每块(`KG_MERGE_REVIEW_BATCH_SIZE`)、描述每组、节点向量每 `EMBED_COMMIT_BATCHES` 批,各自一个小写事务。被杀最多丢一块。

### 4.1 统一 checkpoint 表(mechanism 2 + 3 共用)

`_migration_10`,`SCHEMA_VERSION` 9 → 10(**新迁移号,不塞进已封版的 `_migration_1..9`**;已部署库靠版本闸执行 `_migration_10` 补建 —— 见 [[schema-migration-convention]]):

```sql
CREATE TABLE IF NOT EXISTS kg_rebuild_checkpoint (
    notebook_id   TEXT NOT NULL,
    input_version TEXT NOT NULL,   -- _cluster_input_version(nb),进入 rebuild 时捕获
    stage         TEXT NOT NULL,   -- 'merge_review' | 'concept_desc'
    item_key      TEXT NOT NULL,   -- 阶段内 item 的稳定语义键
    payload       TEXT NOT NULL,   -- json:该 item 的已完成结果
    created_at    TEXT NOT NULL,
    PRIMARY KEY (notebook_id, input_version, stage, item_key)
);
```

仓库内四个私有 helper:

- `_rebuild_ckpt_gc(db, nb, ver)` → `DELETE ... WHERE notebook_id=? AND input_version != ?`。rebuild 开头(算出 `_ver` 后、concept 阶段前)调一次,清掉上一次不同版本残留,表有界。
- `_rebuild_ckpt_clear(nb)` → `DELETE ... WHERE notebook_id=?`。`--fresh` 用,清所有版本所有阶段。
- `_rebuild_ckpt_load(db, nb, ver, stage) -> Dict[item_key, dict]`(payload 已 `json.loads`)。
- `_rebuild_ckpt_put(nb, ver, stage, rows: List[Tuple[str, dict]])` → 一个 `self._write()` 事务 `INSERT OR REPLACE`。

`--fresh` 语义:`rebuild_unified_kg(fresh=True)` 在入口先 `_rebuild_ckpt_clear(nb)`(优先于 GC),强制 merge 审查 + 描述全量重跑。用于**只换了 KG 模型/阈值、数据没变**(此时 `input_version` 不变,否则会复用旧 LLM 决策)。默认 `fresh=False`;仅 CLI `--fresh` 置真,在线「刷新图谱」不受影响。

### 4.2 Mechanism 1:节点向量增量提交

改 `_embed_objects_batch`(`sqlite_repository.py:3808`),把"一次 `pool.map` 全跑完 → 结尾一次写"改为**每 `commit_every` 批 flush 一次**:

```
pending = [(oid, text[:2000]) for it in items if text]
batches = 切成 embed_batch_size 一批
commit_every = commit_every or settings.embed_commit_batches   # 默认 50
workers = min(embed_concurrency, len(batches))
buf, done = [], 0
with ThreadPoolExecutor(workers, "emb-kg") as pool:
    for bi, part in enumerate(pool.map(_embed_only, batches), 1):
        buf.extend(part); done += len(batches[bi-1])
        if bi % commit_every == 0:
            _flush_object_vectors(notebook_id, buf); buf.clear()
            if progress: progress(done, len(pending))
if buf: _flush_object_vectors(notebook_id, buf)
if progress: progress(len(pending), len(pending))
```

`_flush_object_vectors` = 现有那段 `INSERT OR REPLACE INTO knowledge_embeddings` 抽成的小函数,一个写事务。

- **续跑**:免费。`_backfill_knowledge_embeddings` 每次进程启动重算 `have`(已存在向量)/`missing`,已 commit 的节点被排除;`INSERT OR REPLACE` 天然幂等。
- **内存**:峰值 = 一个 commit 组(≤ `commit_every × embed_batch_size` 条向量),不再攒百万。
- **进度**:新增可选 `progress(done, total)`;`_backfill_knowledge_embeddings` 增可选 `progress` 透传;`backfill_node_embeddings` 传入打印器 `节点向量: {done}/{total}`(回车覆盖,末尾换行,复用 `_rebuild_progress` 风格)。
- 惠及所有 `_embed_objects_batch` 调用方(增量提交严格更优),对不传 `progress` 的调用行为等价(仍全量嵌入,只是分批落库)。

### 4.3 Mechanism 2:merge 审查 checkpoint

`concept_merge_review.review_merge_candidates` 增可选 `on_chunk: Callable[[List[dict]], None] = None`,在并发 `as_completed` 循环与串行循环里**每块决策就绪即调用一次**(在**主线程**,SQLite 写安全);`on_chunk` 自身异常被吞并 warning(持久化失败绝不能拖垮 fail-open 的审查)。

rebuild 的 merge 审查块(`sqlite_repository.py:6889`)改为:

```
id_to_key = { f"ac{i}": _pair_key(a, b) for i,(a,b,s) in enumerate(autoc) }   # _pair_key = "\x1f".join(sorted((a,b)))
cached = _rebuild_ckpt_load(db, nb, _ver, 'merge_review')          # {pair_key: {decision, confidence, canonical_name}}
todo = [ cand for cand in cand_dicts if id_to_key[cand['id']] not in cached ]

def _persist(chunk_decisions):
    rows = [(id_to_key[d['candidate_id']],
             {'decision': d['decision'], 'confidence': d['confidence'], 'canonical_name': d.get('canonical_name','')})
            for d in chunk_decisions if d['candidate_id'] in id_to_key]
    if rows: _rebuild_ckpt_put(nb, _ver, 'merge_review', rows)

new = review_merge_candidates(self.kg_llm_client, todo,
          batch_size=settings.kg_merge_review_batch_size,
          max_workers=settings.kg_job_concurrency, on_chunk=_persist)

decided = dict(cached)                                              # 合并 缓存 ∪ 新
for d in new: decided[id_to_key[d['candidate_id']]] = {...}
extra = set()
for i,(a,b,s) in enumerate(autoc):
    dec = decided.get(_pair_key(a,b))
    if dec and dec['decision']=='merge' and dec['confidence']>=settings.kg_merge_confirm_threshold:
        extra.add(frozenset((a[2:] if a.startswith('K-') else a, b[2:] if b.startswith('K-') else b)))
```

- 键 = **排序后的 canonical id 对**(`_pair_key`),(X,Y) 与 (Y,X) 归一。
- 被杀在 merge 审查中途 → 重跑重做分钟级聚类 → 首轮 `auto_candidates` 与 `ac{i}` 编号确定性重现 → `todo` 只剩未落库候选 → 只补这些的 LLM。
- 外层 `try/except`(现有那圈)保留:审查+持久化任何异常 → `extra=set()`,rebuild 照常写簇。

### 4.4 Mechanism 3:概念描述 checkpoint

描述阶段(`sqlite_repository.py:6933-7031`)PHASE1 现已有 `old_desc`(从 `concept_clusters` 按 `canonical_id` 载入 `(desc, sig)`)做跨 rebuild 复用。新增**同版本 checkpoint** 作为第一优先复用源:

- PHASE1 载入 `desc_ckpt = _rebuild_ckpt_load(db, nb, _ver, 'concept_desc')`(`{canonical_id: {description, sig}}`)。判定顺序:checkpoint 命中同 sig → 复用并跳过 LLM;否则 `old_desc` 命中同 sig → 复用(现行为);否则入 `work`。
- PHASE2 `as_completed` 循环里,完成的 `(cid, desc, sig)` 除写入 `desc_by_cid` 外,**缓冲 `_DESC_CKPT_FLUSH`(常量 16)个 flush 一次**到 checkpoint(`_rebuild_ckpt_put(nb, _ver, 'concept_desc', [(cid, {'description':desc,'sig':sig})...])`),循环末尾 flush 余量。仅 `desc` 非空才落。被杀最多丢 16 条描述。
- 被杀在描述中途 → 重跑 PHASE1 从 checkpoint 命中已完成 canonical → 只补剩余。

描述最终仍在写簇(stage 6)落 `concept_clusters.canonical_description/_sig` —— checkpoint 只是写簇前的恢复日志。

## 5. 端到端续跑语义

`--rebuild-only` **保持 `force=True`**(仍重算分钟级聚类),不改语义。小时级三段的复用由上述 checkpoint 与 `missing` 过滤承担:

- 杀在 **merge 审查** → 重做聚类(min)+ merge 审查按 `_ver` 命中 checkpoint,只补剩余候选。
- 杀在 **描述** → merge 审查此时已完成(其 checkpoint 全命中,`extra` 一致 → 重聚一致 → 描述 canonical id 键有效)+ 描述 checkpoint 命中,只补剩余。
- 杀在 **节点向量** → 聚类顶层跳过闸命中(`cluster_input_version` 已在 stage 8 落库)→ 直奔 backfill,按 `missing` 续。
- **数据变了**(seq/算法版本变)→ `input_version` 变 → 两个 checkpoint GC 失效 → 全量重裁/重描述(正确)。

新增 CLI:`kg --fresh`(与 `--rebuild-only` 或普通 kg 组合),置 `rebuild_unified_kg(fresh=True)` → 入口清 checkpoint,强制 LLM 两段全量重跑。

## 6. 配置

`config.py` 新增(遵 [[pydantic-env-alias-gotcha]],用 `validation_alias`):

- `embed_commit_batches: int = Field(50, validation_alias="EMBED_COMMIT_BATCHES")` —— 节点向量每多少批 commit 一次(batch=10 时每 500 节点)。
- merge 审查复用现有 `kg_merge_review_batch_size` 作 checkpoint 粒度;描述 checkpoint flush 粒度用模块常量 `_DESC_CKPT_FLUSH = 16`,不新增旋钮(YAGNI)。

checkpoint 无开关,恒开(严格改进)。

## 7. 改动面

- `backend/app/services/sqlite_repository.py`
  - `_migration_10` 建 `kg_rebuild_checkpoint`;`SCHEMA_VERSION = 10`。
  - 四个 checkpoint helper。
  - `_embed_objects_batch`:增量提交 + `progress`/`commit_every` 参数;抽 `_flush_object_vectors`。
  - `_backfill_knowledge_embeddings`:增可选 `progress` 透传。
  - `rebuild_unified_kg`:加 `fresh` 参数;入口 GC/clear checkpoint;merge 审查块与描述块接 checkpoint。
- `backend/app/services/concept_merge_review.py`:`review_merge_candidates` 加 `on_chunk`。
- `backend/app/services/batch_ingest.py`:`backfill_node_embeddings` 传进度打印器;`run_kg` 接 `fresh`;CLI 加 `--fresh`。
- `backend/app/core/config.py`:`embed_commit_batches`。

## 8. 测试

- **节点向量增量/续跑**:构造 N 节点缺向量,`commit_every` 设小;模拟只跑前 K 组后停(monkeypatch `_flush_object_vectors` 在第 K 次后抛)→ 断言已 flush 的向量在库;二次 `backfill_node_embeddings` 只嵌入剩余(计数 embedder 调用);`progress` 回调 done 单调增至 total。
- **merge 审查 checkpoint**:fake LLM 计数;首跑用 `on_chunk` 落库,注入"跑一半抛"→ 二次跑断言 LLM 只收到未缓存候选;`_pair_key` 对 (a,b)/(b,a) 归一;版本 GC 删旧 `input_version` 行;外层异常仍 `extra=set()` 且写簇。
- **描述 checkpoint**:同构 —— 部分完成→resume 只补剩余;checkpoint 优先于 `old_desc`。
- **迁移**:`_migration_10` 在**已部署库(user_version=9)**上补建 `kg_rebuild_checkpoint`(不止全新库 baseline);全新库 baseline 亦含表。
- **集成/不变量**:同输入连跑两次 `rebuild_unified_kg(force=True)`,第二次 merge 审查 + 描述 LLM 调用数 = 0;两次产出的 `concept_clusters`(canonical/desc)一致;既有 `test_rebuild_streaming`/`test_cross_doc_merge`/`test_kg_merge` 全绿(聚类语义不变)。

## 9. 风险与权衡

- **canonical id 稳定性**是续跑正确性根基,已按 `kg_merge.py:367` 验证(`min(grp)` 确定性)。若未来改 canonical 选取逻辑,须 bump `CLUSTER_ALGO_VERSION`(→ `input_version` 变 → checkpoint 自动失效),风险被版本闸兜住。
- **LLM 非确定性**:续跑对"未决候选"可能给出与"若不中断本会得到"的不同决策 —— 无害,每候选只裁决一次,裁决即 checkpoint 固定;已决的稳定。
- **checkpoint 写放大**:merge 审查每块 +1 小事务(47k 候选 ≈ 1574 事务),描述每组 +1,节点向量每 50 批 +1 —— 相对小时级 LLM/embed 可忽略,换来续跑与有界内存。
- **表增长**:仅当前 `input_version` 行存活(GC),上界 = 本次候选数 + canonical 数,数十万行级、`--fresh`/版本变即清,可接受。
- **并发写**:所有 checkpoint 落库在主线程(`as_completed`/backfill 循环均主线程),单写者,无锁竞争。

## 10. 待评审确认点

- 统一 `kg_rebuild_checkpoint` 表(vs 每阶段独立表)—— 本设计取统一,一迁移一 GC 一 helper。
- `--fresh` 只清 LLM checkpoint、不删向量 —— 如需"连向量一起重来"另走删 `knowledge_embeddings` 的运维动作,不在本设计。
