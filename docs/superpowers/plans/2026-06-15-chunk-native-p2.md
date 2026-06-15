# chunk-native P2:chunk-native 检索(大召回 + MMR + 长上下文综合)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **前置:** 依赖 P1([`2026-06-15-chunk-native-p1.md`](2026-06-15-chunk-native-p1.md))已落地 —— `chunks`/`chunk_embeddings` 表、`_build_chunks_for_source`/`_embed_chunks_for_source`、`_vector_matrix(db, nb, "chunk_embeddings", "chunk_id")`。

**Goal:** 通用问答从 KG-claim 检索切到 chunk-native:大召回(top-150)候选 → MMR 多样性精选(λ=0.5,打破稠密主题通吃)→ 长上下文综合 → 引用绑回 chunk;`mode="chunk"` 成为默认路由。

**Architecture:** 新增纯函数 `mmr_rerank`(MMR 选择)与 `score_chunks`/`RetrievedChunk`(检索打分,复用 `_fuse`/`keyword_score`);repo 侧 `_retrieve_chunks`(大召回,复用 `_vector_matrix`+`query_sims`)→ `_mmr_select_chunks`(用归一化矩阵点积做两两相似度)→ `_chunk_answer_context`(产出与 KG 同形的 `id_map`,故 `_parse_answer_anchors`/`answer_prompt` 原样复用)→ `_answer_chunks`(LLM 合成)→ `ask_chunk`(装配 `AskResponse`)。`ask()` 加 `mode=="chunk"` 分支并设为 `AskRequest` 默认。

**Tech Stack:** Python/FastAPI、SQLite、numpy、pytest;复用 `retrieval.py`、`vector_index.query_sims`、`prompts.answer_prompt`、`_parse_answer_anchors`、`classify_evidence`。

**对应 spec:** `docs/superpowers/specs/2026-06-15-chunk-native-retrieval-design.md`(P2 节)

---

## File Structure

- Create: `backend/app/services/mmr.py` — `mmr_rerank` 纯函数(MMR 选择)
- Modify: `backend/app/services/retrieval.py` — `RetrievedChunk` dataclass + `score_chunks`
- Modify: `backend/app/services/sqlite_repository.py` — `_gather_chunks`、`_retrieve_chunks`、`_mmr_select_chunks`、`_chunk_answer_context`、`_answer_chunks`、`ask_chunk`、`ask()` 路由分支
- Modify: `backend/app/core/config.py` — chunk 检索旋钮
- Modify: `backend/app/models/schemas.py` — `AskRequest.mode` 默认 → `"chunk"`,注释补 `"chunk"`
- Test: `backend/tests/test_mmr.py`(新)、`backend/tests/test_chunk_retrieval.py`(新)

**复用而非新增的关键事实:**
- `_vector_matrix(db, nb, "chunk_embeddings", "chunk_id")` 已表名参数化(P1/既有),返回 `(ids, L2归一化 float32 矩阵)`,行点积=余弦 → MMR 两两相似度免额外计算。
- `_parse_answer_anchors(answer, id_map)` 只读 `id_map[key]` 的 dict 键(`object_id/object_type/name/definition/snippet/source_title/location_label/tier`)。chunk 的 `id_map` 用相同形状(`object_id=chunk_id`、`object_type="chunk"`)即可**原样复用**,`AnswerAnchor` 不改 schema。
- `classify_evidence(top_hits, anchors, llm_grounded, tau_low, tau_high)` 只读 `h.relevance`/`h.object_id` 与 `a.object_id`。`RetrievedChunk` 暴露 `relevance` 字段与 `object_id`(=chunk_id)属性 → 原样复用。
- `answer_prompt`/`ANSWER_SCHEMA_HINT` 的 `[k_i]` 标注协议与对象类型无关 → 原样复用。

---

## Task 5: MMR 多样性选择(纯函数)

**Files:**
- Create: `backend/app/services/mmr.py`
- Test: `backend/tests/test_mmr.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_mmr.py`:

```python
from app.services.mmr import mmr_rerank


def test_mmr_drops_redundant_for_diverse():
    # a,b 近似重复(两两相似 0.95); c 与众不同(相似 ~0)。
    # 相关度 a>b>c, 但 λ=0.5 下选完 a 后应优先多样的 c, 而非冗余的 b。
    rel = {"a": 0.90, "b": 0.88, "c": 0.70}
    sim = {("a", "b"): 0.95, ("a", "c"): 0.05, ("b", "c"): 0.05}
    def pair(x, y):
        return sim.get((x, y)) or sim.get((y, x)) or (1.0 if x == y else 0.0)
    out = mmr_rerank(["a", "b", "c"], rel, pair, k=2, lambda_=0.5)
    assert out == ["a", "c"]


def test_mmr_pure_relevance_when_lambda_one():
    rel = {"a": 0.5, "b": 0.9, "c": 0.7}
    out = mmr_rerank(["a", "b", "c"], rel, lambda x, y: 0.0, k=3, lambda_=1.0)
    assert out == ["b", "c", "a"]          # 纯按相关度降序


def test_mmr_respects_k_and_handles_short_input():
    rel = {"a": 0.5, "b": 0.9}
    out = mmr_rerank(["a", "b"], rel, lambda x, y: 0.0, k=5, lambda_=0.5)
    assert set(out) == {"a", "b"} and len(out) == 2
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/chunk-native
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_mmr.py -x -q
```
Expected: FAIL(ModuleNotFoundError: mmr)

- [ ] **Step 3: 实现**

`backend/app/services/mmr.py`:

```python
"""Maximal Marginal Relevance 选择。检索召回里同一稠密主题会霸占前排,稀释
答案覆盖面;MMR 在"相关度"和"与已选集的新颖度"间折中,逐个挑选,打破通吃。"""
from __future__ import annotations
from typing import Callable, Dict, List


def mmr_rerank(cand_ids: List[str], relevance: Dict[str, float],
               pair_sim: Callable[[str, str], float],
               k: int, lambda_: float = 0.5) -> List[str]:
    """从 cand_ids 选出至多 k 个,平衡相关度与多样性。
    每步选 argmax( λ*rel(c) - (1-λ)*max_{s∈selected} sim(c,s) )。
    λ=1 退化为纯相关度排序;λ=0 为纯多样性。pair_sim(a,b) 返回 [0,1] 余弦。"""
    selected: List[str] = []
    remaining = list(cand_ids)
    while remaining and len(selected) < k:
        best, best_score = None, float("-inf")
        for c in remaining:
            rel = relevance.get(c, 0.0)
            div = max((pair_sim(c, s) for s in selected), default=0.0)
            score = lambda_ * rel - (1.0 - lambda_) * div
            if score > best_score:
                best, best_score = c, score
        selected.append(best)
        remaining.remove(best)
    return selected
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_mmr.py -q
```
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/mmr.py backend/tests/test_mmr.py
git commit -m "feat(chunk): mmr_rerank 多样性选择纯函数

检索召回里稠密主题霸占前排稀释覆盖面; MMR 在相关度与新颖度间折中
逐个挑选, 打破通吃。λ=0.5 默认。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: chunk 检索打分(RetrievedChunk + score_chunks)

**Files:**
- Modify: `backend/app/services/retrieval.py`(在 `score_elements` 之后追加)
- Test: `backend/tests/test_chunk_retrieval.py`(新)

- [ ] **Step 1: 写失败测试**

`backend/tests/test_chunk_retrieval.py`:

```python
from app.services.retrieval import score_chunks, RetrievedChunk


def _ck(cid, text):
    return {"chunk_id": cid, "source_id": "s1", "source_title": "Doc",
            "section_path": "1", "text": text, "element_ids": ["e1"]}


def test_score_chunks_keyword_only_filters_floor():
    chunks = [_ck("c1", "deepseek mixture of experts routing"),
              _ck("c2", "unrelated cooking recipe tomato")]
    out = score_chunks("deepseek experts routing", chunks, query_vector=None, chunk_sims=None, limit=10)
    ids = [c.chunk_id for c in out]
    assert "c1" in ids and "c2" not in ids      # c2 低于 RELEVANCE_FLOOR 被丢
    assert all(isinstance(c, RetrievedChunk) for c in out)
    assert out[0].relevance > 0 and out[0].object_id == out[0].chunk_id


def test_score_chunks_caps_to_limit_sorted():
    chunks = [_ck(f"c{i}", f"shared term token{i}") for i in range(20)]
    out = score_chunks("shared term", chunks, query_vector=None, chunk_sims=None, limit=5)
    assert len(out) == 5
    assert all(out[i].score >= out[i+1].score for i in range(len(out)-1))


def test_score_chunks_uses_semantic_sims():
    chunks = [_ck("c1", "no keyword overlap here")]
    # 仅语义信号(关键词 0): chunk_sims 给高余弦 → 仍能过 floor。
    out = score_chunks("totally different words", chunks,
                       query_vector=[0.1]*4, chunk_sims={"c1": 0.9}, limit=10)
    assert [c.chunk_id for c in out] == ["c1"]
    assert out[0].relevance >= 0.5
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_retrieval.py -x -q
```
Expected: FAIL(ImportError: cannot import name score_chunks)

- [ ] **Step 3: 实现**

`backend/app/services/retrieval.py`,在 `score_elements`(文件末尾)之后追加:

```python
@dataclass
class RetrievedChunk:
    chunk_id: str
    source_id: str
    source_title: str
    section_path: str
    text: str
    element_ids: List[str] = field(default_factory=list)
    score: float = 0.0
    relevance: float = 0.0

    @property
    def object_id(self) -> str:
        # classify_evidence 读 .object_id;anchors 的 object_id 也=chunk_id,
        # 两边对齐才能算出 anchored_rel。
        return self.chunk_id


def score_chunks(
    query: str,
    chunks: List[dict],
    query_vector: Optional[List[float]] = None,
    chunk_sims: Optional[Dict[str, float]] = None,
    limit: int = 150,
) -> List[RetrievedChunk]:
    """Keyword + 可选语义(预算好的 chunk_sims)融合打分 chunk;大召回(默认
    top-150)。与 score_elements 同构,但作用于合并后的检索 chunk。"""
    scored: List[RetrievedChunk] = []
    for c in chunks:
        keyword = keyword_score(query, c["text"])
        semantic = 0.0
        has_vector = bool(query_vector and chunk_sims is not None)
        if has_vector:
            semantic = chunk_sims.get(c["chunk_id"], 0.0)
        score = _fuse(keyword, semantic, has_vector)
        if score < RELEVANCE_FLOOR:
            continue
        scored.append(RetrievedChunk(
            chunk_id=c["chunk_id"], source_id=c["source_id"],
            source_title=c.get("source_title", ""), section_path=c.get("section_path", ""),
            text=c["text"], element_ids=c.get("element_ids", []),
            score=score, relevance=score,
        ))
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:limit]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_retrieval.py -q
```
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_chunk_retrieval.py
git commit -m "feat(chunk): RetrievedChunk + score_chunks 大召回打分

复用 _fuse/keyword_score/RELEVANCE_FLOOR; object_id 属性=chunk_id 让
classify_evidence/anchors 对齐免改。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: repo 大召回 + MMR 精选(_gather_chunks / _retrieve_chunks / _mmr_select_chunks)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_gather_elements` ~3530 附近加 `_gather_chunks`;`_retrieve_elements` ~3947 附近加 `_retrieve_chunks`/`_mmr_select_chunks`)
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/test_chunk_retrieval.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_chunk_retrieval.py`(文件头补 import + 复用 P1 风格 hermetic fixture):

```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_chunks(repo, texts):
    """建 notebook+source+elements, 走 P1 的 build+embed 真路径产出 chunks。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid
    sid = f"src-{uuid.uuid4().hex[:8]}"; now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, nb.id, "Doc", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    repo._chunk_and_embed_source(sid)
    return nb, sid


def test_retrieve_chunks_returns_scored_with_matrix(repo):
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts " * 20,
                                "tomato soup cooking recipe " * 20])
    scored, ids, mat = repo._retrieve_chunks(nb.id, "deepseek experts")
    assert scored and scored[0].relevance > 0
    assert len(ids) >= 1 and mat.shape[0] == len(ids)


def test_mmr_select_caps_and_subsets(repo):
    nb, _ = _seed_chunks(repo, [f"shared topic alpha detail {i} " * 20 for i in range(8)])
    scored, ids, mat = repo._retrieve_chunks(nb.id, "shared topic alpha")
    picked = repo._mmr_select_chunks(scored, ids, mat, k=3, lambda_=0.5)
    assert len(picked) <= 3
    assert {p.chunk_id for p in picked} <= {c.chunk_id for c in scored}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_retrieval.py -x -q -k "retrieve_chunks or mmr_select"
```
Expected: FAIL(no attribute _retrieve_chunks)

- [ ] **Step 3: 实现**

config 增(`backend/app/core/config.py`,P1 的 `chunk_target_chars` 附近):

```python
    # chunk-native 检索: 大召回候选数 / MMR 精选数 / MMR λ / 答案上下文预算(长上下文综合)。
    chunk_recall: int = Field(150, env="CHUNK_RECALL")
    chunk_mmr_k: int = Field(16, env="CHUNK_MMR_K")
    chunk_mmr_lambda: float = Field(0.5, env="CHUNK_MMR_LAMBDA")
    chunk_answer_budget_chars: int = Field(30000, env="CHUNK_ANSWER_BUDGET_CHARS")
```

`sqlite_repository.py` —— 顶部已 `import json`;`_gather_chunks` 放在 `_gather_elements`(~3530)附近:

```python
    def _gather_chunks(self, db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = db.execute(
            """
            SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids,
                   s.title AS source_title
            FROM chunks c JOIN sources s ON s.id = c.source_id
            WHERE c.notebook_id = ?
            """,
            (notebook_id,),
        ).fetchall()
        return [{
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]
```

`_retrieve_chunks` / `_mmr_select_chunks` 放在 `_retrieve_elements`(~3947)附近:

```python
    def _retrieve_chunks(self, notebook_id: str, query: str, recall: int = 0):
        """大召回 chunk 候选。返回 (scored, ids, matrix);后两者供 MMR 取两两余弦
        (matrix 行已 L2 归一化, 点积即余弦)。"""
        from app.services.retrieval import score_chunks
        from app.services.vector_index import query_sims
        recall = recall or self.settings.chunk_recall
        query_vector = self._embed_query(query)
        with self._connect() as db:
            chunks = self._gather_chunks(db, notebook_id)
            ids, mat = self._vector_matrix(db, notebook_id, "chunk_embeddings", "chunk_id")
        chunk_sims = query_sims(query_vector, ids, mat) if query_vector else None
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        return scored, ids, mat

    def _mmr_select_chunks(self, scored, ids, mat, k: int, lambda_: float):
        """对大召回结果做 MMR 多样性精选。沿用归一化矩阵, pair_sim=行点积。"""
        from app.services.mmr import mmr_rerank
        if len(scored) <= k:
            return list(scored)
        id_to_row = {cid: i for i, cid in enumerate(ids)}
        relevance = {c.chunk_id: c.relevance for c in scored}

        def pair_sim(a: str, b: str) -> float:
            ia, ib = id_to_row.get(a), id_to_row.get(b)
            if ia is None or ib is None:
                return 0.0
            return float(mat[ia] @ mat[ib])

        chosen = mmr_rerank([c.chunk_id for c in scored], relevance, pair_sim, k, lambda_)
        by_id = {c.chunk_id: c for c in scored}
        return [by_id[cid] for cid in chosen]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_retrieval.py -q
```
Expected: 全 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_chunk_retrieval.py
git commit -m "feat(chunk): 大召回 _retrieve_chunks + MMR 精选 _mmr_select_chunks

复用 _vector_matrix(chunk_embeddings)+query_sims 大召回 top-150;MMR
用归一化矩阵行点积做两两余弦精选 top-16。config 加 chunk_recall/
mmr_k/mmr_lambda/answer_budget。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: chunk 答题装配 + mode=chunk 默认路由

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_chunk_answer_context`/`_answer_chunks`/`ask_chunk`;`ask()` 路由 ~3959)
- Modify: `backend/app/models/schemas.py`(`AskRequest.mode` 默认)
- Test: `backend/tests/test_chunk_retrieval.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_chunk_retrieval.py`:

```python
import json
from app.models.schemas import AskRequest


class _FakeLLM:
    """配置好的假 LLM:chat_json 回定长 JSON, 内含 [k1] 标记。"""
    configured = True
    def __init__(self, answer): self._answer = answer
    def chat_json(self, messages, schema_hint, **kw):
        return json.dumps({"answer": self._answer, "grounded": True})


def test_ask_chunk_deterministic_without_llm(repo):
    # fixture 清了 LLM key → llm_client.configured False → 走确定性兜底。
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts routing " * 20,
                                "deepseek v2 dense baseline architecture " * 20])
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts routing"))
    assert resp.answer == "" and "passage" in resp.conclusion.lower()
    assert resp.anchors == [] and resp.citations          # 有引用, 无 anchor
    assert resp.citations[0].source_id and resp.evidence_level == "inferred"


def test_ask_chunk_binds_anchor_to_chunk_with_llm(repo, monkeypatch):
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts routing " * 20])
    repo.llm_client = _FakeLLM("DeepSeek V3 uses MoE routing [k1].")
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts"))
    assert resp.answer and resp.anchors
    a = resp.anchors[0]
    assert a.object_type == "chunk" and a.object_id.startswith("ck-")
    assert resp.conclusion and "[k1]" not in resp.conclusion   # 标记已剥离


def test_ask_routes_default_mode_to_chunk(repo, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(repo, "ask_chunk", lambda nb, p: sentinel)
    # AskRequest() 默认 mode 应为 "chunk" → ask() 分发到 ask_chunk
    assert AskRequest(question="x").mode == "chunk"
    assert repo.ask("nb-irrelevant", AskRequest(question="x")) is sentinel
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_retrieval.py -x -q -k "ask_chunk or routes_default"
```
Expected: FAIL(no attribute ask_chunk / mode!="chunk")

- [ ] **Step 3: 实现**

(a) `schemas.py`(~156):`AskRequest.mode` 默认 `"fast"` → `"chunk"`,注释补 `"chunk"`:

```python
    mode: str = "chunk"       # "chunk"(默认,通用问答) | "fast"(旧KG) | "reasoning" | "graph" | "global"
```

(b) `sqlite_repository.py` `ask()`(~3959),在 `_ask_global` 分支**之后**加 chunk 分支(在 `import time` / fast 主体之前):

```python
        if getattr(payload, "mode", "chunk") == "chunk":
            return self.ask_chunk(notebook_id, payload)
```

(c) `sqlite_repository.py` 新增三方法(放在 `_answer_kg`/`ask_reasoning` 附近,与既有问答方法同区):

```python
    def _chunk_answer_context(self, chunks) -> tuple:
        """产出长上下文综合用的 id 标注块 + id_map。chunk.text 已含 [section] 前缀
        (P1 build_chunks),故每行直接 `k_i: <text>`。id_map 形状与 KG 版一致,
        使 _parse_answer_anchors 原样复用(object_id=chunk_id, object_type=chunk)。"""
        budget = self.settings.chunk_answer_budget_chars
        lines, id_map = [], {}
        used = 0
        for i, c in enumerate(chunks, 1):
            if used >= budget and len(lines) >= 1:
                break
            key = f"k{i}"
            line = f"{key}: {c.text}"
            lines.append(line)
            used += len(line)
            id_map[key] = {
                "object_id": c.chunk_id, "object_type": "chunk",
                "name": c.section_path or c.source_title, "definition": None,
                "snippet": c.text[:300], "source_title": c.source_title,
                "location_label": c.section_path, "tier": "personal",
            }
        return ("\n".join(lines) if lines else "(none)"), id_map

    def _answer_chunks(self, notebook_id, question, chunks, history="") -> tuple:
        """长上下文综合:把 MMR 精选的 chunk 原文喂给答案 LLM。返回
        (answer, llm_grounded, anchors)。复用 answer_prompt 的 [k] 标注协议。"""
        from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
        context_block, id_map = self._chunk_answer_context(chunks)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def ask_chunk(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """chunk-native 通用问答:大召回 → MMR 多样性精选 → 长上下文综合 →
        引用绑回 chunk。KG 不参与(严格推理走 ask_reasoning)。"""
        import time
        from app.services.retrieval import classify_evidence
        ask_started = time.perf_counter()

        def ask_stage(name: str, started: float, **extra) -> None:
            self.event_log.emit({
                "kind": "ask_stage", "notebook_id": notebook_id, "stage": name,
                "latency_ms": round((time.perf_counter() - started) * 1000), **extra,
            })

        self.get_notebook(notebook_id)
        question = payload.question.strip()
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)
        retrieval_query = self._rewrite_followup_query(history, question)

        _t = time.perf_counter()
        scored, ids, mat = self._retrieve_chunks(notebook_id, retrieval_query)
        ask_stage("retrieve_chunks", _t, recall=len(scored))

        _t = time.perf_counter()
        selected = self._mmr_select_chunks(
            scored, ids, mat, self.settings.chunk_mmr_k, self.settings.chunk_mmr_lambda)
        ask_stage("mmr", _t, selected=len(selected))

        # 引用绑回 chunk:element_id 取 chunk 首个 element(前端既有 element 引用可解析)。
        citations: List[Citation] = []
        for c in selected:
            eid = c.element_ids[0] if c.element_ids else ""
            citations.append(Citation(
                label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                source_id=c.source_id, element_id=eid,
                location_label=c.section_path, quoted_span=c.text[:200]))

        answer, llm_grounded, anchors = "", False, []
        _t = time.perf_counter()
        if self.llm_client.configured and selected:
            try:
                answer, llm_grounded, anchors = self._answer_chunks(
                    notebook_id, question, selected, history)
            except Exception:
                answer, llm_grounded, anchors = "", False, []
        ask_stage("answer_llm", _t)

        evidence_level, top_relevance = classify_evidence(
            selected, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
        grounded = evidence_level == "grounded"

        if answer:
            conclusion = _MARKER_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        else:
            llm_mode = "deterministic"
            conclusion = (
                f"Retrieved {len(selected)} relevant passage(s) for this question."
                if selected else
                "No indexed content matches this question yet. Upload sources or build chunks.")

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
            citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
            retrieval_query=retrieval_query, top_relevance=top_relevance)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        ask_stage("total", ask_started)
        return response
```

注:`_MARKER_RE`、`Citation`、`AskResponse`、`_parse_answer_anchors`、`classify_evidence`、`_ensure_conversation`、`_conversation_history`、`_rewrite_followup_query`、`_save_answer` 均为既有(已在文件内/已 import)。chunk 路径**单 notebook**;两层 KB(base tier)联邦检索是 P3+,本 plan 不接。

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_retrieval.py -q
```
Expected: 全 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_chunk_retrieval.py
git commit -m "feat(chunk): ask_chunk 长上下文综合 + mode=chunk 默认路由

_chunk_answer_context 产出与 KG 同形 id_map(复用 _parse_answer_anchors/
answer_prompt 免改); ask_chunk 装配大召回→MMR→综合→引用绑 chunk;
AskRequest.mode 默认 chunk, ask() 加 chunk 分支(旧 KG 走 mode=fast)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: 全量验证 + 真机三基准对照 NotebookLM + PR

**Files:**
- 无新代码;验证 + 文档化对照运行手册 + 提 PR。

- [ ] **Step 1: 全量 check.sh**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/chunk-native
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh 2>&1 | tail -6
```
验收:相对基线**不新增失败**。基线已知唯一失败 `test_prompts.py::test_extract_prompt_excludes_enumerated_values_and_meta_claims`(master 即失败,见 P1 plan 说明,独立任务修);P1+P2 新增测试(test_chunking / test_chunk_embed / test_mmr / test_chunk_retrieval)必须全 PASS。

- [ ] **Step 2: 提交 PR(P1+P2 合并一个 PR)**

```bash
git push -u origin claude/chunk-native-retrieval 2>&1 | tail -2
gh pr create --base master --head claude/chunk-native-retrieval \
  --title "feat(retrieval): chunk-native 检索(P1+P2 基础设施+大召回/MMR/长上下文综合)" \
  --body "$(cat <<'EOF'
## 背景
KG-claim 检索碎/空洞/丢语境,通用问答召回弱(对照 NotebookLM 明显落后)。本 PR 落地 chunk-native 检索的 P1+P2(spec: docs/superpowers/specs/2026-06-15-chunk-native-retrieval-design.md)。

## P1 基础设施
- `chunking.build_chunks`:碎 element(47%<150字)贪心合并成 ~600 字检索 chunk(heading 作 section、跳 image、记 element_ids)。
- `chunks`/`chunk_embeddings` 两表;`_build_chunks_for_source`(摄取 inline,query 立即可用)+ `_embed_chunks_for_source`(后台补向量,复用 429 退避并发,不阻塞流水线)。
- `scripts/build_chunks.py` 回填现有 notebook。

## P2 检索
- 大召回 `_retrieve_chunks`(top-150,复用 `_vector_matrix`/`query_sims`)→ `mmr_rerank` 多样性精选(λ=0.5,打破稠密主题通吃)→ `_answer_chunks` 长上下文综合 → 引用绑回 chunk。
- `mode="chunk"` 成为 `AskRequest` 默认;旧 KG fast 路径保留在 `mode="fast"`;严格推理 `mode="reasoning"` 不变。

## KG 边界
KG 退出通用问答检索,保留:严格推理(graph 多跳)+ 两层 KB 治理资产。图谱可视化按需构建。

## 验证
- 新增单测:test_chunking / test_chunk_embed / test_mmr / test_chunk_retrieval 全绿。
- check.sh 相对基线无新增失败(基线 test_prompts 一条 stale 失败为既有,独立修复)。
- 真机三基准(综述 / DeepSeek V3vsV2 差别 / 具体问题)对照 NotebookLM —— 见下方运行手册。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -3
```

- [ ] **Step 3: 真机三基准对照运行手册(交用户执行 —— 需重启 + 回填)**

> ⚠️ 新代码需**重启后端**才生效(改了 config/检索代码,跑着的进程内存仍是旧值)。按用户既定流程,**我不启停服务**;以下步骤由用户执行(或在用户已重启且回填后,我对已在跑的服务 `curl` 取数对照)。

1. 用户合并/切到本分支后重启后端(用户自有流程)。
2. 为目标 notebook 回填 chunk(一次性):
   ```bash
   PYTHONPATH=backend python scripts/build_chunks.py <notebook_id>
   ```
3. 跑三基准问题(默认即 chunk 模式),对照 NotebookLM 看覆盖面/具体度/跨语言:
   ```bash
   for Q in "review一下当前材料,看看当前llm架构的演进" \
            "deepseek v3 和 v2 的差别是什么" \
            "<一个具体细节问题>"; do
     curl -s -X POST localhost:8000/api/notebooks/<notebook_id>/ask \
       -H 'content-type: application/json' \
       -d "{\"question\":\"$Q\"}" | python -m json.tool
   done
   ```
4. 对照判据(P2 是否达标):
   - **覆盖面**:综述类问题能否覆盖多个模型结构/演进阶段(此前只出单一结构)?
   - **具体度**:V3vsV2 能否给出多点实质差异(此前空洞)?
   - **跨语言**:中文 query 能否召回英文原文 chunk?
   - **引用**:`anchors` 是否绑到 chunk(`object_type:"chunk"`)且 `citations` 指向原文段?

- [ ] **Step 4: 记录对照结论**

把三问的 our-vs-NotebookLM 对照结论(达标/差距)回填到本 plan 或新 issue;决定是否进 P3(query 改写/跨语言扩展)、P4(KG 收缩)、P5(路由/前端 mode 开关)。

---

## 自检验证

- 每 task 跑对应 `pytest`;Task 9 跑全量 `check.sh`。
- 纯函数(mmr / score_chunks)单测离线即覆盖核心逻辑;repo 路径用 hermetic fixture(FakeEmbedder + 清 LLM key)。
- 真机对照需用户重启 + 回填(我不启停服务);我可对**已在跑**的服务 curl 取数协助对照。
- worktree:`.claude/worktrees/chunk-native`(分支 claude/chunk-native-retrieval)。

## P2 完成后

依据真机对照结论排期 P3-P5。P3(query 泛→具体扩展 + 中→英跨语言改写)对"覆盖面/跨语言"判据增益最大,优先级最高。
