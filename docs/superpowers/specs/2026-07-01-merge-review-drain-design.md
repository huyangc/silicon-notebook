# 设计：合并审阅队列止血(deferred 终态 + 全量后台预审 + 稳定 seed 键)

- 日期：2026-07-01
- 范围：修复「待确认合并」队列**永远显示 ~1000、点「LLM 预审」也不消失**的问题。三处协同:A 给 unsure 一个终态 `deferred`(不再回流)、B 把预审升级为后台分批跑完整队列 + 进度、C 候选去重键改用稳定的 seed 名对(拒绝/deferred 不复活)。
- 不在本 spec:合并算法本身(相似度阈值 hi/lo 不动)、auto_candidates 的 LLM 兜底逻辑不动、viz 索引(独立 PR #135)。

## 背景与根因

「待确认合并」队列 = 概念名向量相似度落在 **[lo=0.82, hi=0.94)** 这一档的候选对(≥hi 自动合并、<lo 丢弃),硬上限 `max_pending=1000`([kg_merge.py](../../../backend/app/services/kg_merge.py) `cluster_seeds`)。它"不消失"由三因叠加:

1. **量 vs 批**:队列封顶 1000(真实候选更多),但「LLM 预审」每次只审 50(前端 [page.tsx:648](../../../frontend/app/page.tsx) 写死 `limit:50`;后端 `review_pending_merges` cap ≤200)。
2. **unsure 永不离队**:`review_pending_merges`([sqlite_repository.py:4415](../../../backend/app/services/sqlite_repository.py))对 merge<confirm / keep_separate<separate / unsure 的判定**保持 `status='pending'`** → 下次预审重审、rebuild 重灌。这些版本变体名(deepseek v2 ↔ deepseek v2 series 等)恰恰多为 unsure。
3. **拒绝记忆键不稳**:`rebuild_unified_kg` 每次 `DELETE ... status='pending'` 再从 `cluster_seeds` 重灌;排除已决定对用 `decided_pairs`(canonical id `K-<簇内最小成员名>`,strip-`K-` 还原 seed)。canonical id 随簇成员变而变 → 键对不上 → **已拒绝的对复活**(多成员簇触发;单名簇不受影响)。

## 架构

三部件都在「概念合并审阅」子系统内,共享一个不变量:**队列只显示 `status='pending'`;rebuild 只删 pending;confirmed/rejected/deferred 三态存活且不再被重新提出**(按稳定 seed 键排除)。

## 组件

### A — `deferred` 终态(unsure 不再回流)
- `concept_merge_candidates.status` 增加取值 `deferred`(表无 CHECK 约束,直接使用;非破坏性)。
- `review_pending_merges`:原 `else: unsure += 1`(status 保持 pending)改为 **`status='deferred'`** 并 `unsure += 1`。confirmed/rejected 分支不变。deferred 行照常写 `confidence/rationale/reviewed_by='llm'`。
- `pending_merges` 查询已是 `WHERE status='pending'` → deferred 立即离队,无需改。
- `rebuild_unified_kg` 的 `DELETE ... status='pending'` 天然不动 deferred;**候选再生成排除集须含 deferred**(见 C 的排除集)。
- 语义:deferred = 已看过、暂不并、别再问。重评估 → 走「完整重抽」(清库重来)。

### C — 稳定 seed 名对身份(拒绝/deferred 不复活)
- `concept_merge_candidates` 加两列 `seed_a TEXT NOT NULL DEFAULT ''`、`seed_b TEXT NOT NULL DEFAULT ''`(归一化 seed 名对,rebuild 间稳定;canonical id 会变、seed 名不变)。
- `cluster_seeds` 的 `pending` 返回项从 `(canon_a, canon_b, sim)` 扩为 **`(seed_a, seed_b, canon_a, canon_b, sim)`**(该函数内已有两个 seed 名 `a`/`b`,顺带带出)。`auto_candidates` 不涉队列、不改。
- rebuild 插入 pending 行时写 `seed_a/seed_b`。
- 新增/改造去重键来源:`decided_seed_pairs(notebook_id) -> {(seed_a, seed_b): status}`,`SELECT seed_a, seed_b, status WHERE status IN ('confirmed','rejected','deferred')`;**seed_a/seed_b 非空取之,为空(存量行)回退现有 strip-`K-`**。
- rebuild 里构造给 `cluster_seeds` 的集合:`confirmed`(union 合并,来自 confirmed 对)+ `rejected`(跳过再提问,来自 **rejected ∪ deferred** 对),全部按 seed 名 frozenset —— `cluster_seeds` 现有 `rejected` 跳过逻辑不变,只是喂进去的集合变大(含 deferred)。
- 迁移:加列 + 对存量 pending/confirmed/rejected 行 best-effort 回填 `seed_a/seed_b = strip-K-(canonical_a/_b)`(旧行即便不回填,`decided_seed_pairs` 的回退分支也覆盖)。

### B — 后台「全部预审」+ 进度(把队列一次跑完)
- 新状态表 `merge_review_jobs(notebook_id TEXT PRIMARY KEY, status TEXT, total INTEGER, done INTEGER, started_at TEXT, updated_at TEXT, error TEXT)`;`status ∈ {running, done, failed}`。
- 新方法 `run_merge_review_job(notebook_id)`:置 `running` + `total = pending 数`;循环 `review_pending_merges(limit=batch)` 直到 pending 清空或达 `max_batches`(`total/batch + 余量` 上限,防 unsure 之外的死循环);每批 `done += reviewed`、更新 `updated_at`;**每批 try/except fail-open**(一批异常记 `error`、continue,不终止);结束置 `done`(异常置 `failed` 但仍标结束)。**同 notebook 已有 running job 时拒绝重入**(返回 already-running)。
- 端点:`POST /notebooks/{id}/unified-kg/merges/review-all`(启动,复用 KG job 同款 `contextvars.copy_context()` + daemon `threading.Thread`,因内部走 LLM);`GET /notebooks/{id}/unified-kg/merges/review-job`(读进度 `{status,total,done}`)。
- 前端:「待确认合并」区加「全部预审」钮;点击启动后按现有 6s 轮询范式轮询 review-job,显示「预审中 done/total」,完成刷新队列 + 图状态。保留原「LLM 预审(50)」做小批量手动。

## 数据流 / 边界

- **生成(rebuild)**:`decided_seed_pairs` → confirmed(union)/ rejected∪deferred(skip)按 seed 键 → `cluster_seeds` → pending 带 seed 对 → 插入带 `seed_a/seed_b`。
- **手动预审(50)** 与 **全部预审(后台)** 共用 `review_pending_merges`;unsure→deferred,confirmed/rejected 改图 → `_mark_unified_kg_dirty` + 缓存失效(旧有,仅当 confirmed/rejected>0);deferred 不改图、不触发。
- **并发**:同 notebook 只允许一个 review job;job 与 rebuild 若并发,fail-open 兜底(某批候选行被 rebuild 删除 → 该批 UPDATE 影响 0 行,无害)。
- **旧行**:`seed_a/seed_b=''` → `decided_seed_pairs` 回退 strip-`K-`;迁移回填后新行精确。
- **max_batches 上限**:防御性封顶(如 `ceil(total/batch)+2`);到顶仍有 pending(全 unsure→已转 deferred 应清空,理论到不了)则 job 正常结束,剩余留待下次。

## 测试

- **A**:`review_pending_merges` 对低置信/unsure 判定 → `status='deferred'`;deferred 不出现在 `pending_merges`;rebuild 后 deferred 行**存活且不再被生成为新 pending**(mock LLM 返回 unsure,预审→rebuild→断言该对不在 pending)。
- **C**:候选行写入 `seed_a/seed_b`;构造"簇最小成员变→canonical id 变"场景,已 rejected 的对经 rebuild **仍被排除**(用旧 canonical 键会漏、用 seed 键不漏);`decided_seed_pairs` 对空 seed 行回退 strip-`K-`;迁移回填断言。
- **B**:`run_merge_review_job` 分批把 pending 跑到 0(mock LLM);`total/done` 推进;一批抛异常 → job 继续、最终 `failed`/`done` 且未崩;同 notebook 二次启动被拒(already-running);端点形状。
- 后端全量回归 + 前端 tsc/现有测试绿。

## 风险

- **cluster_seeds 返回形状变**:pending 由 3 元组→5 元组,所有调用点(rebuild 主 + per-type + CLI 若有)须同步解包;`cluster_objects`/`cluster_concepts` 委托链一并核对。等价测试兜底。
- **deferred 语义偏保守**:deferred 对在「完整重抽」前不再被评估,即使后续新增数据让它更该合并。可接受(用户确认;完整重抽是逃生口)。
- **后台 job × rebuild 并发**:低频;fail-open + 「影响 0 行」使其无害,不加重锁(YAGNI)。
- **迁移回填**:对超大 `concept_merge_candidates`(百万级)一次性 UPDATE 可能慢;分批或 `DEFAULT ''` + 惰性回退即可(本 spec 取 `DEFAULT ''` + 回退,不强制回填存量,新写入精确)。

## 实施分期(可并行处标注)

- **P1 — schema + C 基础**:加 `seed_a/seed_b` 列迁移 + `decided_seed_pairs`(seed 键 + 回退)+ 单测。
- **P2 — cluster_seeds 返回 seed 对**:改返回形状 + 全部调用点解包 + 等价测试。依赖 P1 概念但文件不同,**可与 P1 并行**(约定 5 元组形状)。
- **P3 — rebuild 接线**:rebuild 用 `decided_seed_pairs` 构造 confirmed/(rejected∪deferred),插入写 seed 对;A 的 deferred 排除在此生效。依赖 P1+P2。
- **P4 — A：review_pending_merges unsure→deferred** + 测试。依赖 schema(P1);与 P3 无文件冲突可穿插。
- **P5 — B：后台 job + 表 + 两端点** + 测试。依赖 P4(复用 review_pending_merges)。
- **P6 — 前端**:「全部预审」钮 + 进度轮询 + 保留「LLM 预审(50)」+ tsc/视觉。依赖 P5。
