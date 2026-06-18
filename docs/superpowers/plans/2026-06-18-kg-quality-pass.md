# KG 质量提升 Pass 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **提交纪律:** 每个 commit 末尾追加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。**直接叠在当前分支 `claude/wonderful-bell-3b27db`(PR #59),只 commit、不 push、不另开 PR**(用户一并合)。

**Goal:** 离线提升已建 KG 的检索质量:检索文本去 `section_path` 噪声(+ re-embed)、检索消费 canonical 簇折叠碎片(非销毁)、about 边可选降权;全部不改对象 id。

**Architecture:** 三个检索/文本层改动 + 两个离线 CLI。① 改 `_payload_text` 排除 `section_path` 再 force re-embed;② 加纯函数 `fold_by_canonical` 并在 `_retrieve_scored` 用 `cluster_map` 折叠(`KG_CANONICAL_FOLD_ENABLED` 默认关);③ `score_relations` 对 about 边加 rank 乘子(`KG_ABOUT_DOWNWEIGHT_ENABLED` 默认关,**因 gold 里大量 about 边,必须可关**);④ recall 指标在 canonical 层比对。守不改 id / [0,1]·tau / 关时等价。

**Tech Stack:** Python3 / SQLite / pytest / FakeEmbedder(哈希向量,检索测试走关键词)。

**Spec:** `docs/superpowers/specs/2026-06-18-kg-quality-pass-design.md`

**测试 repo fixture**(复用,见 `tests/test_relation_embed.py`):monkeypatch `DATABASE_URL`/`SILICON_NOTEBOOK_STORAGE_DIR`/`LLM_LOG_ENABLED=false` + 四个 `EMBED_*` 使 `embedder_configured=True` + `r.embedder=FakeEmbedder(dim=16)`。

---

## Task 1: `_payload_text` 排除 section_path

**Files:**
- Modify: `backend/app/services/retrieval.py:499`(`_payload_text`)
- Test: `backend/tests/test_kg_quality.py`(新建)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kg_quality.py
from app.services.retrieval import _payload_text


def test_payload_text_excludes_section_path():
    # name 干净,section_path 是纯定位元数据,不该进检索文本
    t = _payload_text({"name": "Mixtral", "section_path": "3 > 3.1"})
    assert t == "Mixtral"
    assert ">" not in t


def test_payload_text_keeps_other_fields():
    t = _payload_text({"name": "KV cache", "steps": ["a", "b"]})
    assert "KV cache" in t and "a" in t and "b" in t
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_kg_quality.py -q`
Expected: FAIL(`test_payload_text_excludes_section_path`:`t == "Mixtral 3 > 3.1"`)

- [ ] **Step 3: 实现**(`retrieval.py`,`_payload_text` 当前为:跳过 `_` 前缀键,拼接 str + list/tuple)

在 `_payload_text` 上方加常量,并在循环里跳过该集合:

```python
# 非语义元数据字段:仅用于显示/引用,绝不进检索文本(否则污染 embedding/关键词)。
_PAYLOAD_SKIP_KEYS = frozenset({"section_path"})


def _payload_text(payload: Dict[str, object]) -> str:
    parts: List[str] = []
    for key, value in payload.items():
        if str(key).startswith("_") or key in _PAYLOAD_SKIP_KEYS:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
    return " ".join(parts)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_kg_quality.py tests/test_retrieval.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/retrieval.py backend/tests/test_kg_quality.py
git commit -m "fix(kg): _payload_text 排除 section_path(去检索文本噪声,数据不动)"
```

---

## Task 2: canonical 折叠(非销毁)+ 开关

**Files:**
- Modify: `backend/app/services/retrieval.py`(加 `fold_by_canonical`)、`backend/app/core/config.py`(开关)、`backend/app/services/sqlite_repository.py:4085`(`_retrieve_scored` 接线)
- Test: `backend/tests/test_kg_quality.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kg_quality.py —— 追加
def test_fold_by_canonical_keeps_highest_per_canonical():
    from app.services.retrieval import fold_by_canonical, RetrievedKnowledge
    a = RetrievedKnowledge(object_id="o1", object_type="concept", payload={}, score=0.9, relevance=0.9)
    b = RetrievedKnowledge(object_id="o2", object_type="concept", payload={}, score=0.5, relevance=0.5)
    c = RetrievedKnowledge(object_id="o3", object_type="concept", payload={}, score=0.4, relevance=0.4)
    cmap = {"o1": "K", "o2": "K", "o3": "other"}        # o1,o2 同 canonical
    out = fold_by_canonical([a, b, c], cmap)            # 输入已按 score 降序
    assert [h.object_id for h in out] == ["o1", "o3"]   # o2(同 K 但更低)被折掉


def test_fold_by_canonical_unmapped_passthrough():
    from app.services.retrieval import fold_by_canonical, RetrievedKnowledge
    a = RetrievedKnowledge(object_id="o1", object_type="concept", payload={}, score=0.9, relevance=0.9)
    b = RetrievedKnowledge(object_id="o2", object_type="concept", payload={}, score=0.5, relevance=0.5)
    out = fold_by_canonical([a, b], {})                 # 无映射 → 按自身 id,不折
    assert [h.object_id for h in out] == ["o1", "o2"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_kg_quality.py -q`
Expected: FAIL(`ImportError: cannot import name 'fold_by_canonical'`)

- [ ] **Step 3: 实现**

3a. `retrieval.py` 加纯函数(`score_relations` 之后):

```python
def fold_by_canonical(hits, cluster_map):
    """非销毁折叠:同一 canonical_id 只保留打分最高的成员(输入须已按 score 降序),
    其余 drop。无映射的 hit 按自身 object_id(不折)。不改 hit 内容,只去重候选。"""
    seen, out = set(), []
    for h in hits:
        c = cluster_map.get(h.object_id, h.object_id)
        if c in seen:
            continue
        seen.add(c)
        out.append(h)
    return out
```

3b. `config.py`(`relation_retrieval_enabled` 附近):

```python
    kg_canonical_fold_enabled: bool = Field(False, env="KG_CANONICAL_FOLD_ENABLED")
```

3c. `sqlite_repository.py` `_retrieve_scored` 尾部 —— 当前为:

```python
        scored.sort(key=lambda it: it.score, reverse=True)
        return scored
```

改为:

```python
        scored.sort(key=lambda it: it.score, reverse=True)
        if self.settings.kg_canonical_fold_enabled:
            from app.services.retrieval import fold_by_canonical
            scored = fold_by_canonical(scored, self.cluster_map(notebook_id))
        return scored
```

(`self.cluster_map(notebook_id)` 已存在,返回 `{member_object_id: canonical_id}`。)

- [ ] **Step 4: 写接线等价/折叠测试 + 跑**

```python
# backend/tests/test_kg_quality.py —— 追加(顶部加 repo fixture,见计划开头范式 + import NotebookCreate)
def test_retrieve_scored_fold_flag(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 两次 store_kg 各写一个同名概念 "KV cache" → 不在同一抽取批,canonicalize 不合 → 2 对象
    for _ in range(2):
        repo.store_kg(nb.id, None,
            [{"local_id": "k", "object_type": "concept", "payload": {"name": "KV cache"}, "evidence": []}], [])
    with repo._connect() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchall()]
    assert len(ids) == 2
    monkeypatch.setattr(repo, "cluster_map", lambda n: {ids[0]: "K", ids[1]: "K"})
    monkeypatch.setattr(repo.settings, "kg_canonical_fold_enabled", False)
    off = repo._retrieve_scored(nb.id, "KV cache")
    monkeypatch.setattr(repo.settings, "kg_canonical_fold_enabled", True)
    on = repo._retrieve_scored(nb.id, "KV cache")
    assert len([h for h in off if h.object_id in ids]) == 2     # 关:两碎节点都在
    assert len([h for h in on if h.object_id in ids]) == 1      # 开:折成一个
```

Run: `cd backend && python -m pytest tests/test_kg_quality.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/retrieval.py backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_kg_quality.py
git commit -m "feat(kg): 检索消费 canonical 簇折叠碎片(KG_CANONICAL_FOLD_ENABLED 默认关,非销毁/等价回退)"
```

---

## Task 3: about 边可选降权

**Files:**
- Modify: `backend/app/services/retrieval.py`(`score_relations` + `EDGE_TYPE_RANK_WEIGHT`)、`backend/app/core/config.py`、`backend/app/services/sqlite_repository.py`(`_retrieve_relations_scored` 传开关)
- Test: `backend/tests/test_kg_quality.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kg_quality.py —— 追加
def test_score_relations_about_downweight_rank_only():
    from app.services.retrieval import score_relations
    rels = [
        {"id": "r1", "source_object_id": "s", "target_object_id": "t", "edge_type": "about", "text": "cascode output resistance"},
        {"id": "r2", "source_object_id": "s", "target_object_id": "t", "edge_type": "supports", "text": "cascode output resistance"},
    ]
    # 不降权:两者关键词相同 → relevance 相同
    base = {h.relation_id: h for h in score_relations("cascode output resistance", rels)}
    assert abs(base["r1"].relevance - base["r2"].relevance) < 1e-9
    # 降权:about 的 score(排序用)被压低,但 relevance(tau 用)不变
    dw = {h.relation_id: h for h in score_relations("cascode output resistance", rels, downweight_edges=True)}
    assert abs(dw["r1"].relevance - base["r1"].relevance) < 1e-9    # relevance 不动
    assert dw["r1"].score < dw["r2"].score                         # about 排序被压
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_kg_quality.py::test_score_relations_about_downweight_rank_only -q`
Expected: FAIL(`score_relations() got an unexpected keyword 'downweight_edges'`)

- [ ] **Step 3: 实现**

3a. `retrieval.py`(`score_relations` 之前加常量 + 函数):

```python
# 边类型 rank 乘子:about 是弱结构边(本语料占 ~57%),降权仅作用于排序(score),
# 绝不进 relevance(守 [0,1]/tau)。推理边保持 1.0。
_EDGE_TYPE_RANK_WEIGHT = {"about": 0.5}


def edge_type_rank_weight(edge_type: str) -> float:
    return _EDGE_TYPE_RANK_WEIGHT.get(edge_type, 1.0)
```

3b. `score_relations` 加形参 `downweight_edges: bool = False`,并把 `score` 改成乘 rank 乘子(`relevance` 不变)。当前 append 处为 `score=relevance, relevance=relevance`,改为:

```python
def score_relations(
    query: str,
    relations: List[dict],
    query_vector: Optional[List[float]] = None,
    relation_sims: Optional[Dict[str, float]] = None,
    w_keyword: float = W_KEYWORD,
    w_semantic: float = W_SEMANTIC,
    downweight_edges: bool = False,
) -> List[RetrievedRelation]:
    ...
        relevance = _fuse(keyword, semantic, has_vector, w_keyword, w_semantic)
        if relevance < RELEVANCE_FLOOR:
            continue
        rank_mult = edge_type_rank_weight(rel["edge_type"]) if downweight_edges else 1.0
        scored.append(RetrievedRelation(
            relation_id=rid,
            source_object_id=rel["source_object_id"],
            target_object_id=rel["target_object_id"],
            edge_type=rel["edge_type"],
            text=text,
            evidence=rel.get("evidence", []),
            score=relevance * rank_mult,
            relevance=relevance,
        ))
    scored.sort(key=lambda it: it.score, reverse=True)
    return scored
```

3c. `config.py`:

```python
    kg_about_downweight_enabled: bool = Field(False, env="KG_ABOUT_DOWNWEIGHT_ENABLED")
```

3d. `sqlite_repository.py` `_retrieve_relations_scored` 调 `score_relations` 处加 `downweight_edges=self.settings.kg_about_downweight_enabled`:

```python
        return score_relations(query, relations, query_vector=query_vector,
                               relation_sims=relation_sims,
                               downweight_edges=self.settings.kg_about_downweight_enabled)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_kg_quality.py tests/test_relation_retrieval.py -q`
Expected: PASS(降权 + 既有关系检索不回归)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/retrieval.py backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_kg_quality.py
git commit -m "feat(kg): about 边可选 rank 降权(KG_ABOUT_DOWNWEIGHT_ENABLED 默认关,不进 _fuse 守 tau)"
```

---

## Task 4: recall 指标 canonical 层比对

**Files:**
- Modify: `backend/app/eval/retrieval_metrics.py`(`run_recall`)
- Test: `backend/tests/test_recall_relations.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_recall_relations.py —— 追加
class _FakeRepo:
    def __init__(self, hits, cmap):
        self._hits, self._cmap = hits, cmap
    def _retrieve_scored(self, nb, q):
        class H:  # 轻量 hit
            def __init__(s, oid): s.object_id = oid
        return [H(o) for o in self._hits]
    def _retrieve_relations_scored(self, nb, q): return []
    def cluster_map(self, nb): return self._cmap


def test_run_recall_maps_object_ids_to_canonical():
    from app.eval.retrieval_metrics import run_recall
    # 检索到代表 oA;gold 是被折掉的同簇成员 oB。canonical 映射后应判命中。
    repo = _FakeRepo(hits=["oA", "x", "y"], cmap={"oA": "K", "oB": "K"})
    q = [{"id": "g1", "question": "?", "gold_object_ids": ["oB"]}]
    rows = run_recall(repo, "nb", q, k=12)
    assert rows[0]["recall_at_k"] == 1.0   # oB→K,oA→K,canonical 层命中
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_recall_relations.py::test_run_recall_maps_object_ids_to_canonical -q`
Expected: FAIL(`recall_at_k == 0.0`,未做 canonical 映射)

- [ ] **Step 3: 实现**(`retrieval_metrics.py` `run_recall`,对象侧加 canonical 映射;关系侧不变——relations 不聚类)

在 `run_recall` 顶部取一次 cluster_map,对象 gold/retrieved 都映射:

```python
def run_recall(repo: Any, notebook_id: str, questions: List[Dict[str, Any]],
               k: int = 12) -> List[Dict[str, Any]]:
    """对带 gold_object_ids 或 gold_relation_ids 的题分别跑节点/关系检索,各算
    recall@k + MRR。对象侧在 canonical 层比对(检索/gold 都映射到 canonical_id),
    使 canonical 折叠不致 gold 假性 miss;关系侧按 relation_id(关系不聚类)。"""
    cmap = repo.cluster_map(notebook_id) if hasattr(repo, "cluster_map") else {}
    def canon(i): return cmap.get(i, i)
    rows: List[Dict[str, Any]] = []
    for q in questions:
        gold_obj = q.get("gold_object_ids")
        gold_rel = q.get("gold_relation_ids")
        if not gold_obj and not gold_rel:
            continue
        row: Dict[str, Any] = {"id": q.get("id", ""),
                               "track": q.get("track", ""), "bucket": q.get("bucket", "")}
        if gold_obj:
            ids = [canon(h.object_id) for h in repo._retrieve_scored(notebook_id, q["question"])]
            g = [canon(x) for x in gold_obj]
            row["recall_at_k"] = recall_at_k(ids, g, k)
            row["mrr"] = mrr(ids, g)
            row["n_gold"] = len(gold_obj)
        if gold_rel:
            rids = [h.relation_id for h in repo._retrieve_relations_scored(notebook_id, q["question"])]
            row["relation_recall_at_k"] = recall_at_k(rids, gold_rel, k)
            row["relation_mrr"] = mrr(rids, gold_rel)
            row["n_gold_rel"] = len(gold_rel)
        rows.append(row)
    return rows
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_recall_relations.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/eval/retrieval_metrics.py backend/tests/test_recall_relations.py
git commit -m "feat(eval): recall 对象侧 canonical 层比对(防 canonical 折叠致 gold 假性 miss)"
```

---

## Task 5: 离线 CLI(force re-embed + re-cluster)

**Files:**
- Create: `backend/app/scripts/reembed_kg.py`、`backend/app/scripts/recluster_kg.py`
- Test: `backend/tests/test_kg_quality.py`(追加 syntax 检查)

- [ ] **Step 1: 写 force re-embed CLI**

```python
# backend/app/scripts/reembed_kg.py
"""在干净 _payload_text(已去 section_path)上强制重嵌一个 notebook 的知识/关系向量。
用法: PYTHONPATH=backend python -m app.scripts.reembed_kg <notebook_id>
先清空该 nb 的 knowledge_embeddings/relation_embeddings,再全量重嵌(故用 _payload_text 新文本)。"""
import json, sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: reembed_kg <notebook_id>"); return 2
    nb = sys.argv[1]
    repo = SQLiteRepository(get_settings())
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_embeddings WHERE notebook_id=?", (nb,))
        db.execute("DELETE FROM relation_embeddings WHERE notebook_id=?", (nb,))
    with repo._connect() as db:
        rows = db.execute("SELECT id, payload FROM knowledge_objects WHERE notebook_id=?", (nb,)).fetchall()
    items = [{"_oid": r["id"], "payload": json.loads(r["payload"] or "{}")} for r in rows]
    repo._embed_objects_batch(nb, items)        # 干净文本重嵌对象
    repo._backfill_relation_embeddings(nb)      # 关系已清空 → 全量重嵌(名取自干净 _payload_text)
    print(f"[reembed] {nb}: re-embedded {len(items)} objects + relations"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 写 re-cluster CLI**

```python
# backend/app/scripts/recluster_kg.py
"""重建一个 notebook 的 canonical 簇(concept_clusters),让检索折叠覆盖全部当前对象。
用法: PYTHONPATH=backend python -m app.scripts.recluster_kg <notebook_id>"""
import sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: recluster_kg <notebook_id>"); return 2
    nb = sys.argv[1]
    repo = SQLiteRepository(get_settings())
    n = repo.rebuild_unified_kg(nb)
    print(f"[recluster] {nb}: rebuilt unified KG (clusters={n})"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: 写 syntax 检查测试**

```python
# backend/tests/test_kg_quality.py —— 追加
import ast, pathlib


def test_offline_clis_parse():
    for f in ("reembed_kg.py", "recluster_kg.py"):
        p = pathlib.Path("app/scripts") / f
        ast.parse(p.read_text(encoding="utf-8"))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_kg_quality.py -q`
Expected: PASS（CLI 靠真机离线跑,不进单测;此处仅 syntax）

- [ ] **Step 5: 提交**

```bash
git add backend/app/scripts/reembed_kg.py backend/app/scripts/recluster_kg.py backend/tests/test_kg_quality.py
git commit -m "feat(kg): 离线 CLI — force re-embed(干净文本)+ re-cluster(rebuild_unified_kg)"
```

---

## 收尾:全量验证 + 真机复测(用户跑)

- [ ] **全量**:`cd backend && python -m pytest -q` 全绿;`bash scripts/check.sh` EXIT=0。
- [ ] **env 文档**:`.env.example` + README 补 `KG_CANONICAL_FOLD_ENABLED=false`、`KG_ABOUT_DOWNWEIGHT_ENABLED=false`。
- [ ] **真机离线提质(prod 副本/实验库,nb-b37185f4ae):**
  1. `python -m app.scripts.recluster_kg <nb>`(刷新 canonical 覆盖全量)
  2. `python -m app.scripts.reembed_kg <nb>`(干净文本重嵌)
  3. 复测 recall:`run_recall`(canonical 层)baseline 对照 `KG_CANONICAL_FOLD_ENABLED=true`(+ 可选 `KG_ABOUT_DOWNWEIGHT_ENABLED=true`),看 recall 是否↑。
  4. 复跑 graph A/B(`.local/run_graph_ab.py`),看 grounding 回升 / 伪引用回落 / correctness 是否被抬起来。
- [ ] 数字落 PR #59 评论;按结果决定各开关是否默认开。

---

## Self-Review

- **Spec 覆盖**:① `_payload_text` 去 section_path(T1)✓ + re-embed(T5)✓;② canonical 折叠非销毁 + 开关(T2)✓ + re-cluster(T5)✓;③ about 降权 rank 层 + 开关(T3)✓;④ recall canonical 层(T4)✓ + 复测(收尾)✓。**out-of-scope**(销毁合并/重抽/prompt/模糊自动合并)无任务 ✓。
- **占位符**:无 TBD;每 code step 给完整代码 + 命令。CLI 靠真机(syntax 检查),已注明非计划缺口。
- **类型/命名一致**:`_PAYLOAD_SKIP_KEYS`、`fold_by_canonical(hits, cluster_map)`、`KG_CANONICAL_FOLD_ENABLED`/`kg_canonical_fold_enabled`、`edge_type_rank_weight`/`_EDGE_TYPE_RANK_WEIGHT`、`KG_ABOUT_DOWNWEIGHT_ENABLED`/`kg_about_downweight_enabled`、`score_relations(..., downweight_edges=)`、`cluster_map`/`rebuild_unified_kg`/`_embed_objects_batch`/`_backfill_relation_embeddings` —— 跨任务一致,均与现有签名对齐。
- **风险**:③ about 降权会压低 gold 中的 about 边 → 默认关、可单独开/关分别测(已在收尾步骤分开评估)。② 折叠改变候选集 → 默认关 + canonical 层 recall(T4)保证度量公平。
