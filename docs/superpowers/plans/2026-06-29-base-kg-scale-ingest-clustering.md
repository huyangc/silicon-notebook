# 基础 KG 规模化摄取/聚类(SP3)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `rebuild_unified_kg` 与离线摄取在 5M+ concept 规模下内存严格有界(随唯一名数而非总数),`batch_ingest` CLI 与之一致。

**Architecture:** 把 unified 聚类拆出纯函数 `cluster_seeds`(seed 级);`rebuild_unified_kg` 改三趟流式(temp 表 id→seed → 流式求 reps → 分块写),内存随唯一 name-seed 数有界;CLI 单一实现共用 + kg→index 自动链 + `--no-rebuild`/`--rebuild-only` + 进度。

**Tech Stack:** Python, numpy, hnswlib, SQLite(TEMP TABLE), scipy(已由 SP2 引入)。

参考 spec：`docs/superpowers/specs/2026-06-29-base-kg-scale-ingest-clustering-design.md`。基于 SP2 分支(`build_scale_index` 已在)。

## 并行分组(用户要求尽量并行)
- **P1(可并发,不同文件)**：Task 1(`kg_merge.py`)、Task 2(`config.py`)、Task 6(README)。
- Task 3 依赖 Task 1;Task 4 依赖 Task 3;Task 5(rep-ANN 有界化,`kg_merge.py`)与 Task 1 同文件→紧随 Task 1 串行;Task 7 末尾。
- 执行建议：并发派 Task 1 + Task 2 + Task 6;Task 1 回来后接 Task 5、再 Task 3、再 Task 4、最后 Task 7。

## File Structure
- `backend/app/services/kg_merge.py` — 新增 `cluster_seeds`;`cluster_objects` 改为「构造 seed 级输入后委托 `cluster_seeds`」;`_star_groups` 改吃 counts;`_ann_candidates` 加 rep 上限分片(Task 5)。
- `backend/app/services/sqlite_repository.py` — `rebuild_unified_kg` 流式化 + `_stream_seed_reps` + `_write_cluster_map_streamed`;`write_clusters` 分块。
- `backend/app/core/config.py` — `kg_cluster_rep_ann_max`。
- `backend/app/services/batch_ingest.py` + `backend/app/scripts/batch_ingest.py` — `--no-rebuild`/`--rebuild-only` + kg→index 链 + 进度。
- `README.md` / `README_zh.md`。
- 测试：`backend/tests/test_kg_merge.py`、`test_unified_kg_repository.py`、`test_scale_index_repo.py`(CLI)、新 `test_rebuild_streaming.py`。

---

## Task 1: 抽出 `cluster_seeds`(seed 级纯函数)+ `cluster_objects` 委托 [P1]

**Files:** Modify `backend/app/services/kg_merge.py`; Test `backend/tests/test_kg_merge.py`.

- [ ] **Step 1: 写等价测试(cluster_objects 行为不变)** — 追加到 `test_kg_merge.py`：
```python
from app.services.kg_merge import cluster_objects, cluster_seeds, _norm
import numpy as np


def test_cluster_seeds_matches_cluster_objects_smallcase():
    objs = [{"object_id": f"o{i}", "name": n} for i, n in enumerate(
        ["MOSFET", "mosfet", "current mirror", "current-mirror", "slew rate"])]
    vecs = {"o0": [1.0, 0.0], "o1": [1.0, 0.0], "o2": [0.0, 1.0], "o3": [0.0, 1.0], "o4": [0.5, 0.5]}
    full = cluster_objects(objs, vecs, set(), set(), seed_fn=lambda c: _norm(c["name"]))
    # 经由 seed 级路径手工组装等价输入
    seed_of = {o["object_id"]: _norm(o["name"]) for o in objs}
    seeds = sorted(set(seed_of.values()))
    members_count = {}
    seed_first_name = {}
    reps = {}
    acc = {}
    for o in objs:
        s = seed_of[o["object_id"]]
        members_count[s] = members_count.get(s, 0) + 1
        seed_first_name.setdefault(s, o["name"])
        acc.setdefault(s, []).append(vecs[o["object_id"]])
    for s, vs in acc.items():
        reps[s] = np.mean(np.asarray(vs, dtype=np.float32), axis=0)
    sd = cluster_seeds(seeds, reps, members_count, seed_first_name, set(), set(),
                       conflict_fn=None, id_prefix="K-")
    # 展开到 object 级，应与 cluster_objects 完全一致
    cmap = {o["object_id"]: sd["seed_to_canonical"][seed_of[o["object_id"]]] for o in objs}
    assert cmap == full["cluster_map"]
```
Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_cluster_seeds_matches_cluster_objects_smallcase -q` → FAIL(`cluster_seeds` 未定义)。

- [ ] **Step 2: 实现 `cluster_seeds` + 重构 `cluster_objects` 委托。**
  在 `kg_merge.py`：
  1. 改 `_star_groups` 签名吃计数:`def _star_groups(seeds, members_count, edges, hi)`，把 `len(members.get(s, []))` 换成 `members_count.get(s, 0)`。
  2. 新增 `cluster_seeds`:把现 `cluster_objects` 第 302–335 行(`_ann_candidates`→护栏→`_star_groups`→groups→canon_id/canon_name→auto/pending)整体移入,签名：
```python
def cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                  *, conflict_fn=None, id_prefix="K-", hi=0.94, lo=0.82,
                  top_k=5, max_pending=1000) -> dict:
    """seed 级聚类核心(随 #seeds 有界)。confirmed/rejected 为 seed 对(frozenset)。
    返回 {seed_to_canonical:{seed:cid}, canonical_names:{cid:name},
          auto_candidates, pending, capped}。"""
    uf = _UF(seeds)
    for pair in confirmed:
        if len(pair) != 2: continue
        a, b = tuple(pair)
        if a in uf.p and b in uf.p: uf.union(a, b)
    rej = {frozenset(p) for p in rejected}
    raw = _ann_candidates(seeds, reps, k=top_k, lo=lo)
    cand = []
    for a, b, sim in raw:
        if rej and frozenset((a, b)) in rej: continue
        if conflict_fn and conflict_fn(seed_first_name.get(a, a), seed_first_name.get(b, b)): continue
        cand.append((a, b, sim))
    star = _star_groups(seeds, members_count, cand, hi)
    auto_set = {frozenset((nb, anc)) for nb, anc in star.items() if nb != anc}
    groups = {}
    for s in seeds: groups.setdefault(uf.find(s), []).append(s)
    canon_id, canon_name = {}, {}
    for root, grp in groups.items():
        best = max(grp, key=lambda s: members_count.get(s, 0))
        cid = id_prefix + min(grp)
        for s in grp: canon_id[s] = cid
        canon_name[cid] = seed_first_name[best]
    auto_candidates = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
                       if sim >= hi and frozenset((a, b)) in auto_set and canon_id[a] != canon_id[b]]
    pending = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
               if sim < hi and canon_id[a] != canon_id[b]]
    pending.sort(key=lambda t: t[2], reverse=True)
    was_capped = len(pending) > max_pending
    return {"seed_to_canonical": canon_id, "canonical_names": canon_name,
            "auto_candidates": auto_candidates, "pending": pending[:max_pending], "capped": was_capped}
```
  3. 重构 `cluster_objects`:保留 alias/seed_of/seeds/uf-confirmed/seed_first_name/members/reps 构造,然后：
```python
    members_count = {s: len(lst) for s, lst in members.items()}
    sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                       conflict_fn=conflict_fn, id_prefix=id_prefix, hi=hi, lo=lo,
                       top_k=top_k, max_pending=max_pending)
    canon_id = sd["seed_to_canonical"]
    cluster_map = {c["object_id"]: canon_id[seed_of[c["object_id"]]] for c in objects}
    names = {c["object_id"]: sd["canonical_names"][cluster_map[c["object_id"]]] for c in objects}
    return {"cluster_map": cluster_map, "canonical_names": names,
            "auto_candidates": sd["auto_candidates"], "pending": sd["pending"], "capped": sd["capped"]}
```
  注意:`confirmed`/`rejected` 在 `cluster_objects` 入口已是 seed 串(caller 对齐),直接透传给 `cluster_seeds`。

- [ ] **Step 3: 跑等价 + 现有 kg_merge 全部测试** — `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py -q` → all pass(含原有 cluster_concepts/cluster_objects 测试,证明重构等价)。
- [ ] **Step 4: 提交** — `git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py && git commit -m "refactor(kg): 抽 cluster_seeds(seed级纯函数)+ cluster_objects 委托,行为等价"`

---

## Task 2: config `kg_cluster_rep_ann_max` [P1]

**Files:** Modify `backend/app/core/config.py`; Test `backend/tests/test_kg_merge.py` 或现有 config 测试。

- [ ] **Step 1:** 在 `config.py` 的 Settings 加(紧邻其它 `kg_*` 字段,沿用其 Field 写法)：
```python
    kg_cluster_rep_ann_max: int = Field(2_000_000, env="KG_CLUSTER_REP_ANN_MAX")
```
（默认 2M:唯一 seed 超此值时 Task 5 的 ANN 走分片 + WARNING。）
- [ ] **Step 2:** 验证可读取 — `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -c "from app.core.config import Settings; print(Settings().kg_cluster_rep_ann_max)"` → `2000000`。
- [ ] **Step 3: 提交** — `git add backend/app/core/config.py && git commit -m "feat(kg-scale): config kg_cluster_rep_ann_max(rep-ANN 上限)"`

---

## Task 5: rep-ANN 有界化(`_ann_candidates` 分片 + WARNING)

**依赖 Task 1(同文件)。** Files: Modify `backend/app/services/kg_merge.py`; Test `test_kg_merge.py`.

- [ ] **Step 1: 测试** — 追加:当 `len(seeds) > max_reps` 时 `_ann_candidates` 不抛错、仍返回候选(分片),并触发一次 WARNING 日志(用 `caplog`)：
```python
def test_ann_candidates_shards_above_cap(caplog):
    import numpy as np
    from app.services.kg_merge import _ann_candidates
    seeds = [f"s{i}" for i in range(50)]
    reps = {s: np.random.default_rng(i).random(8).astype("float32") for i, s in enumerate(seeds)}
    with caplog.at_level("WARNING"):
        out = _ann_candidates(seeds, reps, k=5, lo=0.0, max_reps=10)
    assert isinstance(out, list)
    assert any("rep" in r.message.lower() for r in caplog.records)
```
Run → FAIL(`max_reps` 参数不存在)。
- [ ] **Step 2: 实现** — 给 `_ann_candidates` 加 `max_reps: int = None`;当 `n > max_reps`:`_log.warning("rep-ANN sharding: %d seeds > cap %d", n, max_reps)`,把 reps 分成 `ceil(n/max_reps)` 片,每片各自建 hnswlib 索引求片内候选(跨片候选可省略——文档化为「超大库时跨片同义对可能漏」)。`n <= max_reps`(含 None)走原路径。`cluster_seeds` 调用处传 `max_reps=settings.kg_cluster_rep_ann_max`——但 `cluster_seeds` 是纯函数无 settings;改为 `cluster_seeds(..., rep_ann_max=None)` 形参,`_ann_candidates(..., max_reps=rep_ann_max)`;由 repo 调用时传入。
- [ ] **Step 3:** `cd backend && ... pytest tests/test_kg_merge.py -q` → all pass。
- [ ] **Step 4: 提交** — `git commit -m "feat(kg-scale): _ann_candidates rep 上限分片 + WARNING(不静默截断)"`

---

## Task 3: `rebuild_unified_kg` 流式化(crux)

**依赖 Task 1、5。** Files: Modify `backend/app/services/sqlite_repository.py`; Test `backend/tests/test_rebuild_streaming.py`(新)+ `test_unified_kg_repository.py`(回归).

**先读**:当前 `rebuild_unified_kg`(~3964–4110)、`write_clusters`(~3123)、`_connect`/`_write` 连接语义、`build_acronym_alias_map`/`_seed_with_alias`/`_norm`(kg_merge)。

- [ ] **Step 1: 等价测试(流式 == 现实现)** — 新建 `test_rebuild_streaming.py`(fixture 仿 `test_unified_kg_repository.py`:FakeEmbedder dim=16)。存多概念(含同名跨源 + 近义),`rebuild_unified_kg`,断言 cluster_map 与「旧实现」一致。由于这是原地重写,等价靠现有 `test_unified_kg_repository.py` 的既有断言守护(同名合一、idempotent、confirmed force-union 跨 rebuild)。新增一条:rebuild 后 `concept_clusters` 行数 == #concept,且 cluster 数稳定。
```python
def test_rebuild_streaming_clusters_same_name(repo):  # repo fixture 同 test_unified_kg_repository
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"mosfet","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert len(set(cmap.values())) == 1 and len(cmap) == 2
```
- [ ] **Step 2: 加 `_stream_seed_reps`** — 在 repo 加:
```python
def _stream_seed_reps(self, db, notebook_id, object_type, seed_fn):
    """流式构建:临时表 tmp_obj_seed(object_id,seed) + reps{seed:mean_vec} +
    members_count{seed:int} + seed_first_name{seed:name}。内存随 #唯一seed 有界。"""
    import numpy as np
    db.execute("DROP TABLE IF EXISTS tmp_obj_seed")
    db.execute("CREATE TEMP TABLE tmp_obj_seed (object_id TEXT PRIMARY KEY, seed TEXT)")
    # A1: alias_map（流式过名字一遍）
    from app.services.kg_merge import build_acronym_alias_map, _seed_with_alias
    names_iter = (json.loads(r["payload"] or "{}").get("name","") for r in db.execute(
        "SELECT payload FROM knowledge_objects WHERE notebook_id=? AND object_type=? AND status!='deprecated'",
        (notebook_id, object_type)))
    alias_map = build_acronym_alias_map(names_iter)
    # A2: seed_of -> temp 表 + members_count + seed_first_name
    members_count, seed_first_name = {}, {}
    buf = []
    for r in db.execute("SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND object_type=? AND status!='deprecated'",
                        (notebook_id, object_type)):
        name = json.loads(r["payload"] or "{}").get("name","")
        s = _seed_with_alias({"name": name}, seed_fn, alias_map)
        members_count[s] = members_count.get(s, 0) + 1
        seed_first_name.setdefault(s, name)
        buf.append((r["id"], s))
        if len(buf) >= 1000:
            db.executemany("INSERT OR REPLACE INTO tmp_obj_seed VALUES (?,?)", buf); buf=[]
    if buf: db.executemany("INSERT OR REPLACE INTO tmp_obj_seed VALUES (?,?)", buf)
    # B: 流式求 reps（join 向量）
    dim = self.settings.embed_dim
    rep_sum, rep_cnt = {}, {}
    for r in db.execute("SELECT t.seed AS seed, e.vector AS vector FROM knowledge_embeddings e "
                        "JOIN tmp_obj_seed t ON t.object_id=e.object_id WHERE e.notebook_id=?", (notebook_id,)):
        v = json.loads(r["vector"])
        if len(v) != dim: continue
        a = np.asarray(v, dtype=np.float32)
        s = r["seed"]
        rep_sum[s] = a if s not in rep_sum else rep_sum[s] + a
        rep_cnt[s] = rep_cnt.get(s, 0) + 1
    reps = {s: rep_sum[s] / rep_cnt[s] for s in rep_sum}
    return reps, members_count, seed_first_name
```
  注意:整个 rebuild 必须在**同一个 `db` 连接**内(TEMP TABLE 是连接级)。把 `rebuild_unified_kg` 主体包进一个 `with self._write() as db:`(或 `_connect`),所有读写走同一 `db`。
- [ ] **Step 3: 加 `_write_cluster_map_streamed`** —
```python
def _write_cluster_map_streamed(self, db, notebook_id, object_type, seed_to_canonical, canonical_names, desc_by_cid=None):
    """流式扫 tmp_obj_seed 写 concept_clusters,1000 行/批。清旧行后插。"""
    prefix_clear = {"concept": "K-", "claim":"KL-","formula":"KF-","procedure":"KP-"}[object_type]
    db.execute("DELETE FROM concept_clusters WHERE notebook_id=? AND canonical_id LIKE ?", (notebook_id, prefix_clear+"%"))
    now = _now(); buf=[]
    for r in db.execute("SELECT object_id, seed FROM tmp_obj_seed"):
        cid = seed_to_canonical.get(r["seed"])
        if cid is None: continue
        name = canonical_names.get(cid, "")
        desc = (desc_by_cid or {}).get(cid, "")
        buf.append((... cluster_clusters 列, 含 canonical_description=desc ...))
        if len(buf) >= 1000:
            db.executemany("INSERT INTO concept_clusters (...) VALUES (...)", buf); buf=[]
    if buf: db.executemany(...)
```
  （列与现 `write_clusters` 持久化一致;实现前读 `write_clusters` 确认 schema/列。）
- [ ] **Step 4: 重写 `rebuild_unified_kg` 主体** 用上述三步替换「全量 concepts/vectors/cluster_concepts/rows」:
  - 同一 `db` 连接内:`reps, members_count, seed_first_name = self._stream_seed_reps(db, nb, "concept", lambda o:_norm(o["name"]))`;`seeds=sorted(reps_or_all_seeds)`(注意:无向量的 seed 也要在 seeds 里——用 `sorted(members_count)`)。
  - confirmed/rejected 同现(decided_pairs → _seed 去 K- 前缀)。
  - `sd = cluster_seeds(sorted(members_count), reps, members_count, seed_first_name, confirmed, rejected, conflict_fn=_discriminative_conflict, id_prefix="K-", rep_ann_max=self.settings.kg_cluster_rep_ann_max)`。
  - LLM 复审:用 `sd["auto_candidates"]`;若 extra → 再 `cluster_seeds(..., confirmed|extra, ...)`(reps 已在 RAM,廉价)。
  - concept-desc(gated):多成员 canonical 从 `seed_to_canonical`+`members_count` 求(`canonical→Σcount`);对 ≥2 成员的 canonical,经 SQL 取其成员 evidence(`SELECT evidence FROM knowledge_objects JOIN tmp_obj_seed ... WHERE seed IN (该 canonical 的 seeds)`)。bounded。得 `desc_by_cid`。
  - `self._write_cluster_map_streamed(db, nb, "concept", sd["seed_to_canonical"], sd["canonical_names"], desc_by_cid)`。
  - per-type(claim/formula/procedure):`reps={}`(空)→ 仍可走 `_stream_seed_reps`(向量 join 命中空→reps 空)或直接流式 seed 分组;`cluster_seeds(sorted(members_count), {}, members_count, seed_first_name, set(), set(), conflict_fn=None, id_prefix=PREFIX)`;`_write_cluster_map_streamed(..., type)`。
  - pending 刷新、cache 失效、状态写:维持现状(pending 已 cap 1000,bounded)。`res["pending"]` → 用 `sd["pending"]`。
- [ ] **Step 5: 回归** — `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_unified_kg_repository.py tests/test_rebuild_streaming.py tests/test_cross_doc_merge.py tests/test_kg_merge.py -q` → all pass(这些覆盖同名合一/idempotent/confirmed-union/跨文档,是等价的事实守护)。
- [ ] **Step 6: 提交** — `git commit -m "feat(kg-scale): rebuild_unified_kg 流式化(temp表id→seed+流式reps+分块写),内存随唯一名数有界"`

---

## Task 4: CLI 一致性(`--no-rebuild`/`--rebuild-only` + kg→index + 进度)

**依赖 Task 3。** Files: Modify `backend/app/services/batch_ingest.py`、`backend/app/scripts/batch_ingest.py`;Test `backend/tests/test_scale_index_repo.py`。

**先读**:`run_kg`(~144–181)、CLI argparse、SP2 `run_index`/`build_scale_index`。

- [ ] **Step 1: 测试**(append to `test_scale_index_repo.py`):
```python
def test_run_kg_rebuilds_scale_index_for_base(repo, monkeypatch):
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="base")); repo.mark_notebook_base(nb.id)
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"X","section_path":""},"evidence":[]}], [])
    # 无 LLM:extraction 跳过;run_kg 应仍执行 rebuild + 因 base tier 重建 scale 索引
    batch_ingest.run_kg(repo, nb.id, rebuild_only=True)
    assert repo._scale_index(nb.id) is not None
```
（按 `run_kg` 实际签名调整;若 `run_kg` 不接受 `rebuild_only`,本测试驱动你加该参数。）Run → FAIL。
- [ ] **Step 2: 实现** — `run_kg(repo, notebook_id, *, limit=None, no_rebuild=False, rebuild_only=False, ...)`:
  - `rebuild_only=True`:跳过抽取段。
  - `no_rebuild=True`:跑抽取、跳过末尾 `rebuild_unified_kg` 与 index。
  - 否则:抽取 → `rebuild_unified_kg` → **若 notebook 是 base tier 或已有 scale 索引** → `build_scale_index(nb)`。
  - 进度日志:抽取 `i/N`、`rebuilt clusters=X`、`scale index built`。
  - CLI `scripts/batch_ingest.py`:加 `--no-rebuild`/`--rebuild-only` flags;`--help` 写明「`--limit` 只限本轮抽取;聚类始终覆盖全量;分批工作流=多次 `kg --limit N --no-rebuild` → `kg --rebuild-only`」。
- [ ] **Step 3:** `cd backend && ... pytest tests/test_scale_index_repo.py -q` → all pass。
- [ ] **Step 4: 提交** — `git commit -m "feat(kg-scale): CLI run_kg 加 --no-rebuild/--rebuild-only + kg→index 链 + 进度"`

---

## Task 6: README 中英 [P1,内容据 spec,最后合]

**Files:** Modify `README.md`、`README_zh.md`。

- [ ] **Step 1:** 在 batch-ingest 段补:大库分批工作流与 `--no-rebuild`/`--rebuild-only` 语义、`--limit` 只限抽取、kg 后自动重建 scale 索引;`KG_CLUSTER_REP_ANN_MAX` 环境变量。中英一致,通用口径(无机器特定路径)。
- [ ] **Step 2: 提交** — `git commit -m "docs(kg-scale): README(中英)补大库分批摄取工作流 + rep-ANN 上限"`

---

## Task 7: gated 规模慢测 + 全量回归

**Files:** `backend/tests/test_rebuild_streaming.py`。

- [ ] **Step 1:** 加 `@pytest.mark.slow` 测试:合成 N=200_000 concept(多唯一名),`store_kg` 分批写 + FakeEmbedder,`rebuild_unified_kg`,断言完成且 `concept_clusters` 行数==N;记录耗时。(内存有界靠流式设计 + 不物化全量 dict 的断言;可选 `tracemalloc` 峰值记录。)
- [ ] **Step 2:** 跑 slow:`cd backend && ... pytest tests/test_rebuild_streaming.py -q -m slow -s` → PASS,记录耗时/峰值。
- [ ] **Step 3:** 全量(非 slow):`cd backend && ... pytest -q -m "not slow" -p no:cacheprovider | tail -3` → all pass。
- [ ] **Step 4: 提交** — `git commit -m "test(kg-scale): rebuild 流式 2e5 规模 gated 慢测 + 全量回归"`

---

## 收尾
- [ ] rebase 到 SP2 分支最新(或 SP2 合并后 rebase 到 origin/master)→ push → `gh pr create --base master`(SP2 合并前可标注 stacked-on-#111)。PR 附 Task 7 实测耗时/峰值、等价测试结论、CLI 一致性说明。
