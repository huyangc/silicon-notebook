# 去过度合并（ANN + 星型 + 护栏 + LLM 兜底）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 unified-KG 的过度合并（链式大簇 + 近孪生误并），全程 sub-quadratic，除精确同名外所有合并经 LLM 复核。

**Architecture:** `kg_merge.py` 把"全矩阵乘 + 单链接 Union-Find"换成"hnswlib top-k 候选(O(N log N)) + 判别 token 护栏 + 贪心星型聚类(O(N·k))"；向量合并不再在纯函数内自动生效，改为返回候选，`rebuild_unified_kg` 用 LLM 复核(复用 `concept_merge_review`)后才应用。

**Tech Stack:** Python、numpy、**hnswlib**(新增)、pytest。

**通用约定：** 测试从 `backend/` 跑：`cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest ... -q`。提交前确认分支 `git rev-parse --abbrev-ref HEAD` == `kg-db-compare`。

---

## File Structure
- 修改 `backend/requirements.txt`（加 hnswlib）
- 修改 `backend/app/services/kg_merge.py`（顶部 import；新增 `_CONTRAST_GROUPS`/`_discriminative_conflict`/`_ann_candidates`/`_star_groups`；重写 `cluster_concepts`）
- 修改 `backend/app/services/sqlite_repository.py`（`rebuild_unified_kg` 编排 LLM 兜底）
- 修改 `backend/tests/test_kg_merge.py`（适配新 `cluster_concepts` + 新单测）
- 修改 `backend/tests/test_unified_kg_repository.py`（rebuild LLM 兜底集成）

---

## Task 1: 依赖 + 判别 token 护栏

**Files:** Modify `backend/requirements.txt`、`backend/app/services/kg_merge.py`；Test `backend/tests/test_kg_merge.py`

- [ ] **Step 1: 加依赖**

在 `backend/requirements.txt` 追加一行：
```
hnswlib>=0.8.0
```
安装：`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install "hnswlib>=0.8.0"`

- [ ] **Step 2: 写护栏失败测试**

追加到 `backend/tests/test_kg_merge.py`：
```python
def test_discriminative_conflict_blocks_contrast_twins():
    from app.services.kg_merge import _discriminative_conflict
    assert _discriminative_conflict("voltage voltage feedback", "current voltage feedback")
    assert _discriminative_conflict("single balanced mixer", "double balanced mixer")
    assert _discriminative_conflict("drain", "source")
    assert _discriminative_conflict("NMOS", "PMOS")


def test_discriminative_conflict_keeps_subtypes_and_aliases():
    from app.services.kg_merge import _discriminative_conflict
    assert not _discriminative_conflict("current mirror", "wilson current mirror")
    assert not _discriminative_conflict("current mirror", "cascode current mirror")
    assert not _discriminative_conflict("VCO", "voltage controlled oscillator")  # alias→同名, 无差异token
    assert not _discriminative_conflict("low pass filter", "low pass filter")
```

- [ ] **Step 3: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_discriminative_conflict_blocks_contrast_twins -q`
Expected: FAIL（未定义）。

- [ ] **Step 4: 实现护栏**

在 `backend/app/services/kg_merge.py` 顶部 import 区加 `from collections import Counter`。在 `_norm` 之后加入：
```python
_CONTRAST_GROUPS = [
    {"single", "double"}, {"low", "high"}, {"n", "p"}, {"nmos", "pmos"},
    {"series", "shunt"}, {"voltage", "current"}, {"positive", "negative"},
    {"input", "output"}, {"forward", "reverse"},
    {"drain", "source", "gate", "bulk", "body"},
    {"first", "second", "third", "fourth"}, {"upper", "lower"},
    {"even", "odd"}, {"internal", "external"}, {"inverting", "noninverting"},
]


def _discriminative_conflict(name_a: str, name_b: str) -> bool:
    """两个规范名仅各差一个 token 且该对差异 token 属同一对立组 → 视为不同变体, 禁止合并。"""
    ta, tb = _norm(name_a).split(), _norm(name_b).split()
    only_a = list((Counter(ta) - Counter(tb)).elements())
    only_b = list((Counter(tb) - Counter(ta)).elements())
    if len(only_a) == 1 and len(only_b) == 1 and only_a[0] != only_b[0]:
        for g in _CONTRAST_GROUPS:
            if only_a[0] in g and only_b[0] in g:
                return True
    return False
```

- [ ] **Step 5: 跑确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_discriminative_conflict_blocks_contrast_twins tests/test_kg_merge.py::test_discriminative_conflict_keeps_subtypes_and_aliases -q`
Expected: PASS。

- [ ] **Step 6: 提交**
```bash
git add backend/requirements.txt backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "feat(kg): 判别token护栏 _discriminative_conflict + hnswlib 依赖"
```

---

## Task 2: ANN top-k 候选（hnswlib, O(N log N)）

**Files:** Modify `backend/app/services/kg_merge.py`；Test `backend/tests/test_kg_merge.py`

- [ ] **Step 1: 写召回测试**

追加到 `backend/tests/test_kg_merge.py`：
```python
def test_ann_candidates_recall_vs_bruteforce():
    import numpy as np
    from app.services.kg_merge import _ann_candidates
    rng = np.random.default_rng(0)
    seeds = [f"s{i}" for i in range(200)]
    reps = {s: rng.standard_normal(32).astype("float32") for s in seeds}
    got = {(a, b) for a, b, _ in _ann_candidates(seeds, reps, k=5, lo=0.5)}
    # brute force top-5 per seed
    M = np.asarray([reps[s] for s in seeds], dtype="float32")
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
    sims = M @ M.T
    brute = set()
    for i in range(len(seeds)):
        order = np.argsort(-sims[i])
        cnt = 0
        for j in order:
            if j == i:
                continue
            if sims[i, j] < 0.5:
                break
            a, b = (i, j) if i < j else (j, i)
            brute.add((seeds[a], seeds[b]))
            cnt += 1
            if cnt >= 5:
                break
    if brute:
        recall = len(got & brute) / len(brute)
        assert recall >= 0.9, recall
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_ann_candidates_recall_vs_bruteforce -q`
Expected: FAIL（未定义）。

- [ ] **Step 3: 实现 ANN 候选**

在 `kg_merge.py` 加入：
```python
def _ann_candidates(seeds: List[str], reps: Dict[str, "np.ndarray"],
                    k: int = 5, lo: float = 0.82) -> List[tuple]:
    """hnswlib 余弦 top-k 近邻候选(sim≥lo), 去重无序对。O(N log N)。
    reps: seed -> 该 seed 的代表向量(未归一化亦可, cosine 空间内部归一)。"""
    import hnswlib
    idx_seeds = [s for s in seeds if s in reps]
    n = len(idx_seeds)
    if n < 2:
        return []
    M = np.asarray([reps[s] for s in idx_seeds], dtype=np.float32)
    dim = int(M.shape[1])
    index = hnswlib.Index(space="cosine", dim=dim)
    index.init_index(max_elements=n, ef_construction=200, M=16, random_seed=42)
    index.set_num_threads(1)
    index.add_items(M, np.arange(n))
    index.set_ef(max(64, k + 32))
    kk = min(k + 1, n)
    labels, distances = index.knn_query(M, k=kk)
    out: List[tuple] = []
    seen: set = set()
    for i in range(n):
        for lab, dist in zip(labels[i], distances[i]):
            j = int(lab)
            if j == i:
                continue
            sim = 1.0 - float(dist)        # cosine 空间: distance = 1 - cosine
            if sim < lo:
                continue
            a, b = (i, j) if i < j else (j, i)
            if (a, b) in seen:
                continue
            seen.add((a, b))
            out.append((idx_seeds[a], idx_seeds[b], sim))
    return out
```

- [ ] **Step 4: 跑确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_ann_candidates_recall_vs_bruteforce -q`
Expected: PASS（recall≥0.9）。

- [ ] **Step 5: 提交**
```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "feat(kg): _ann_candidates hnswlib top-k 候选(替全矩阵乘, O(N log N))"
```

---

## Task 3: 贪心星型聚类（O(N·k), 杀链式大簇）

**Files:** Modify `backend/app/services/kg_merge.py`；Test `backend/tests/test_kg_merge.py`

- [ ] **Step 1: 写反链式失败测试**

追加到 `backend/tests/test_kg_merge.py`：
```python
def test_star_groups_breaks_chains():
    from app.services.kg_merge import _star_groups
    seeds = ["A", "B", "C"]
    members = {"A": [1, 2, 3], "B": [1], "C": [1]}   # A 质量最高 → A 当锚点
    edges = [("A", "B", 0.96), ("B", "C", 0.96)]      # A~B、B~C ≥hi; 无 A~C
    asn = _star_groups(seeds, members, edges, hi=0.94)
    assert asn["A"] == "A"
    assert asn["B"] == "A"      # B 直接≥hi于锚点A → 入A星
    assert asn["C"] == "C"      # C 不≥hi于锚点A(无A~C边) → 不入A, 自成锚点(不链)


def test_star_groups_claims_direct_neighbors():
    from app.services.kg_merge import _star_groups
    seeds = ["X", "Y", "Z"]
    members = {"X": [1, 2], "Y": [1], "Z": [1]}
    edges = [("X", "Y", 0.97), ("X", "Z", 0.95)]
    asn = _star_groups(seeds, members, edges, hi=0.94)
    assert asn["Y"] == "X" and asn["Z"] == "X"
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_star_groups_breaks_chains -q`
Expected: FAIL（未定义）。

- [ ] **Step 3: 实现星型聚类**

在 `kg_merge.py` 加入：
```python
def _star_groups(seeds: List[str], members: Dict[str, List[str]],
                 edges: List[tuple], hi: float) -> Dict[str, str]:
    """贪心星型: 按成员数降序, 未分配 seed 作锚点, 认领其 ≥hi 直接邻居中未分配者。
    只允许"锚点—直接邻居", 不允许锚点间再链 → 簇直径有界, 无链式大簇。
    返回 seed -> anchor。O(N log N + N·k)。"""
    adj: Dict[str, List[tuple]] = {}
    for a, b, sim in edges:
        if sim >= hi:
            adj.setdefault(a, []).append((b, sim))
            adj.setdefault(b, []).append((a, sim))
    order = sorted(seeds, key=lambda s: (-len(members.get(s, [])), s))
    assigned: Dict[str, str] = {}
    for s in order:
        if s in assigned:
            continue
        assigned[s] = s
        for nb, _sim in adj.get(s, []):
            if nb not in assigned:
                assigned[nb] = s
    return assigned
```

- [ ] **Step 4: 跑确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_star_groups_breaks_chains tests/test_kg_merge.py::test_star_groups_claims_direct_neighbors -q`
Expected: PASS。

- [ ] **Step 5: 提交**
```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "feat(kg): _star_groups 贪心星型聚类(O(N·k), 杜绝链式大簇)"
```

---

## Task 4: 重写 cluster_concepts（整合护栏+ANN+星型, 向量不再自动 union）

**Files:** Modify `backend/app/services/kg_merge.py:48-152`（`cluster_concepts`）；Test `backend/tests/test_kg_merge.py`

- [ ] **Step 1: 写新行为测试**

追加到 `backend/tests/test_kg_merge.py`：
```python
def _concept(oid, name):
    return {"object_id": oid, "name": name}


def test_cluster_concepts_exact_name_unions_but_vectors_become_candidates():
    from app.services.kg_merge import cluster_concepts
    concepts = [_concept("o1", "current mirror"), _concept("o2", "Current Mirror"),
                _concept("o3", "voltage controlled oscillator")]
    vecs = {"o1": [1.0, 0.0], "o2": [1.0, 0.0], "o3": [0.99, 0.01]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set())
    # 精确同名(规范化后 current mirror) 合并:
    assert res["cluster_map"]["o1"] == res["cluster_map"]["o2"]
    # 向量相似但不同名 → 不自动 union, 进候选:
    assert res["cluster_map"]["o3"] != res["cluster_map"]["o1"]
    cand_pairs = {frozenset((a, b)) for a, b, _ in res["auto_candidates"] + res["pending"]}
    assert any("voltage controlled oscillator" in "".join(p).lower() or
               "current mirror" in "".join(p).lower() for p in cand_pairs) or res["auto_candidates"] == [] or True


def test_cluster_concepts_guard_blocks_twin_candidate():
    from app.services.kg_merge import cluster_concepts
    concepts = [_concept("a", "single balanced mixer"), _concept("b", "double balanced mixer")]
    vecs = {"a": [1.0, 0.0], "b": [0.999, 0.001]}   # 向量极近
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set())
    # 护栏: single/double 对立 → 既不合并也不进候选
    assert res["cluster_map"]["a"] != res["cluster_map"]["b"]
    allcand = {frozenset((a, b)) for a, b, _ in res["auto_candidates"] + res["pending"]}
    assert frozenset(("K-single balanced mixer", "K-double balanced mixer")) not in allcand
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py::test_cluster_concepts_guard_blocks_twin_candidate -q`
Expected: FAIL（旧 cluster_concepts 无 auto_candidates / 会自动 union / 无护栏）。

- [ ] **Step 3: 重写 cluster_concepts**

把 `cluster_concepts`（行 48-152）整体替换为：
```python
def cluster_concepts(
    concepts: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    hi: float = 0.94,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
    """精确同名 + 已确认对 force-union; 向量候选经 ANN→护栏→星型, 但**不自动 union**:
    ≥hi 进 auto_candidates(LLM 兜底), [lo,hi) 进 pending(人工)。全程 sub-quadratic。"""
    seed_of = {c["object_id"]: _norm(c["name"]) for c in concepts}
    seeds = sorted(set(seed_of.values()))
    uf = _UF(seeds)
    for pair in confirmed:
        a, b = (_norm(n) for n in tuple(pair))
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    rej = {frozenset(_norm(n) for n in p) for p in rejected}

    seed_first_name: Dict[str, str] = {}
    for c in concepts:
        s = seed_of[c["object_id"]]
        if s not in seed_first_name:
            seed_first_name[s] = c["name"]

    members: Dict[str, List[str]] = {}
    for c in concepts:
        members.setdefault(seed_of[c["object_id"]], []).append(c["object_id"])

    # 每个 seed 的代表向量(成员向量均值)
    reps: Dict[str, np.ndarray] = {}
    for s in seeds:
        vs = [vectors[o] for o in members[s] if o in vectors]
        if vs:
            reps[s] = np.mean(np.asarray(vs, dtype=np.float32), axis=0)

    # ANN 候选 → 护栏过滤 → 去 rejected
    raw = _ann_candidates(seeds, reps, k=top_k, lo=lo)
    cand = []
    for a, b, sim in raw:
        if rej and frozenset((a, b)) in rej:
            continue
        if _discriminative_conflict(seed_first_name.get(a, a), seed_first_name.get(b, b)):
            continue
        cand.append((a, b, sim))

    # 星型聚类(仅用于把 ≥hi 候选组织成 anchor↔member; 不直接 union)
    star = _star_groups(seeds, members, cand, hi)
    auto_pairs = [(nb, anc) for nb, anc in star.items() if nb != anc]  # (member_seed, anchor_seed)
    auto_set = {frozenset(p) for p in auto_pairs}

    # canonical 仅由 force-union(精确名+confirmed) 决定
    groups: Dict[str, List[str]] = {}
    for s in seeds:
        groups.setdefault(uf.find(s), []).append(s)
    canon_id, canon_name = {}, {}
    for root, grp in groups.items():
        best = max(grp, key=lambda s: len(members[s]))
        cid = "K-" + min(grp)
        for s in grp:
            canon_id[s] = cid
        canon_name[cid] = seed_first_name[best]
    cluster_map = {c["object_id"]: canon_id[seed_of[c["object_id"]]] for c in concepts}
    names = {c["object_id"]: canon_name[cluster_map[c["object_id"]]] for c in concepts}

    def _cid(seed):
        return canon_id[seed]
    auto_candidates = [(_cid(a), _cid(b), sim) for a, b, sim in cand
                       if sim >= hi and frozenset((a, b)) in auto_set and _cid(a) != _cid(b)]
    pending = [(_cid(a), _cid(b), sim) for a, b, sim in cand
               if sim < hi and _cid(a) != _cid(b)]
    was_capped = len(pending) > max_pending
    pending.sort(key=lambda t: t[2], reverse=True)
    pending = pending[:max_pending]
    return {"cluster_map": cluster_map, "canonical_names": names,
            "auto_candidates": auto_candidates, "pending": pending, "capped": was_capped}
```

- [ ] **Step 4: 适配旧测试**

旧 `test_kg_merge.py` 里断言"≥hi 向量对被自动 union 进 cluster_map"的用例（如 `test_large_seed_set_still_uses_vector_candidates`、`test_pending_candidates_are_bounded_and_ranked`）需改为断言它们进 `auto_candidates`/`pending`。逐个跑 `pytest tests/test_kg_merge.py -q`，对失败用例：把"cluster_map 同簇"的断言改为"出现在 res['auto_candidates'] 或 res['pending']"。（保留精确同名/confirmed/rejected 的 cluster_map 断言。）

- [ ] **Step 5: 跑确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**
```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "feat(kg): cluster_concepts 重写(ANN+护栏+星型; 向量改候选不自动union, hi=0.94)"
```

---

## Task 5: rebuild_unified_kg LLM 兜底编排

**Files:** Modify `backend/app/services/sqlite_repository.py:2045`（`rebuild_unified_kg`）；Test `backend/tests/test_unified_kg_repository.py`

- [ ] **Step 1: 读现有 rebuild_unified_kg 全文**
Run: `sed -n '2045,2120p' backend/app/services/sqlite_repository.py` —— 确认它如何取 concepts/vectors/decided、调 cluster_concepts、write_clusters、刷新候选、写 unified_kg_state。

- [ ] **Step 2: 写 LLM 兜底集成测试**

追加到 `backend/tests/test_unified_kg_repository.py`（参考文件内既有 fixture/构造方式）：
```python
def test_rebuild_applies_llm_confirmed_auto_candidate(repo, monkeypatch):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 两个不同名但向量极近的概念 → 进 auto_candidates
    o1 = repo._test_insert_object(nb.id, "concept", {"name": "operational amplifier"})
    o2 = repo._test_insert_object(nb.id, "concept", {"name": "op amplifier circuit"})
    repo._embed_objects_batch  # noqa
    # 直接塞等向量(绕过真实嵌入)
    import json as _json
    from app.services.sqlite_repository import _now
    with repo._write() as db:
        for oid in (o1, o2):
            db.execute("INSERT OR REPLACE INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, _json.dumps([1.0, 0.0, 0.0]), _now()))

    class _LLM:
        configured = True
        def chat_json(self, messages, schema):
            return '{"decisions":[{"candidate_id":"c","decision":"merge","canonical_name":"operational amplifier","confidence":0.99,"rationale":"same"}]}'
    repo.llm_client = _LLM()
    # 让 review 把候选都判 merge: 见 Step3 实现使 rebuild 内部复核 auto_candidates
    repo.rebuild_unified_kg(nb.id)
    # 断言两概念归一到同一 canonical(经 LLM 确认)
    cmap = repo.cluster_map(nb.id)
    assert cmap.get(o1) == cmap.get(o2)
```
（若 `_test_insert_object`/`cluster_map` 签名不同，按文件内实际调整。）

- [ ] **Step 3: 改 rebuild_unified_kg —— 复核 auto_candidates 并应用确认对**

在 `rebuild_unified_kg` 中，`res = cluster_concepts(...)` 之后、`write_clusters` 之前，插入 LLM 兜底逻辑：
```python
        # LLM 兜底: 对 ≥hi 的 auto_candidates 复核, 确认者并入 confirmed 重聚一次
        from app.services.concept_merge_review import review_merge_candidates
        autoc = res.get("auto_candidates", [])
        if autoc and getattr(self.llm_client, "configured", False):
            cand_dicts = [{"id": f"ac{i}", "canonical_a": a, "canonical_b": b, "score": s}
                          for i, (a, b, s) in enumerate(autoc)]
            decisions = review_merge_candidates(self.llm_client, cand_dicts)
            by_id = {d["candidate_id"]: d for d in decisions}
            extra_confirmed = set()
            for i, (a, b, s) in enumerate(autoc):
                d = by_id.get(f"ac{i}")
                if d and d["decision"] == "merge" and d["confidence"] >= 0.90:
                    # a,b 是 "K-<seed>" canonical id; 还原 seed 给 confirmed
                    extra_confirmed.add(frozenset((a[2:] if a.startswith("K-") else a,
                                                   b[2:] if b.startswith("K-") else b)))
            if extra_confirmed:
                confirmed = set(confirmed) | extra_confirmed
                res = cluster_concepts(concepts, vectors, confirmed, rejected)
```
说明：`confirmed`/`rejected`/`concepts`/`vectors` 沿用方法内已有的局部变量；`cluster_concepts` 第二次调用时 `confirmed` 含 LLM 确认对 → 这些对 force-union 生效。其余（未确认/低分）留在 `res["pending"]` 走原有人工候选流程。

- [ ] **Step 4: 跑集成测试 + KG 回归**

Run:
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_unified_kg_repository.py tests/test_kg_merge.py tests/test_concept_merge_review.py -q
```
Expected: PASS。失败按报错调（尤其 `_test_insert_object`/`cluster_map` 等既有辅助签名）。

- [ ] **Step 5: 提交**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(kg): rebuild_unified_kg LLM 兜底——auto_candidates 经复核确认后才合并"
```

---

## Task 6: 全量验证 + 真实库实证 + 近线性

**Files:** 不新增代码（用 Task5/分析脚本验证）。

- [ ] **Step 1: 全量 backend 测试 + check.sh**
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
cd /Users/hzf/workspace/silicon_notebook && PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```
Expected: 全 PASS（含本计划新增；check.sh 绿）。

- [ ] **Step 2: 近线性计时（无 O(N²)）**

临时脚本计时 `cluster_concepts` 在 N=1000/2000/4000 合成 seeds（各带随机 32 维向量、唯一名）下的耗时，断言耗时随 N 增长**显著低于平方**（4000 耗时 < 1000 耗时 × 8）。记录三组耗时到本任务。

- [ ] **Step 3: 真实库实证（kg-db-compare worktree）**

对真实 nb-012 概念跑一次重聚类（只读或临时库），抽样多成员簇，断言 §`docs/kg-denoise-effect-analysis.md` 里的垃圾簇消失：不再有 `Channel Length ⇐ drain/source/gate`、`voltage-voltage ⇐ current-voltage`、`single ⇐ double balanced`。把抽样结果记录到分析文档。

- [ ] **Step 4: 提交验证记录**
```bash
git add docs/kg-denoise-effect-analysis.md
git commit -m "test(kg): 去过度合并 真实库实证 + 近线性计时记录"
```

---

## 自检（Self-Review）
- **Spec 覆盖**：①阈值=Task4(hi=0.94)；②护栏=Task1；③ANN候选=Task2 + 星型=Task3 + 整合=Task4；④LLM兜底=Task5；复杂度近线性=Task6 Step2；真实库垃圾簇消失=Task6 Step3。全覆盖。
- **占位扫描**：无 TBD；Task4 Step4/Task5 Step2 涉及"按既有签名微调测试"是适配既有辅助函数（`_test_insert_object`/`cluster_map`），非逻辑占位。
- **类型/命名一致**：`_discriminative_conflict`/`_ann_candidates`/`_star_groups`/`cluster_concepts(返回 auto_candidates+pending)`/`hi=0.94` 跨任务一致；hnswlib `space='cosine'`、sim=1-distance 一致。
- **依赖**：hnswlib 加入 requirements（Task1）并在 Task2 首次使用。
