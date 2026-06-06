# 推理模式 Agentic KG 检索 — 后端实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在后端新增独立的「推理模式」(`mode="reasoning"`) KG 检索路径:模型规划子查询 → 检索 → 反思(自由图遍历深挖) → 合成,产出可折叠推理轨迹;不动现有 fast pipeline。

**Architecture:** 手搓 JSON-action 循环(无原生 function calling,复用 `chat_json`)。新增 `ReasoningRetriever`(独立类,持 repo 引用调检索原语)跑结构化骨架 `Plan→Retrieve→Reflect→Answer`,Reflect 阶段由模型自主决定 expand_graph(深挖跳数无硬上限)/add_subquery/search_elements(降级原文)/answer。最终用**原问题统一重打分**裁定 grounding 口径,复用现有 `classify_evidence` 三档分类。护栏只做 circuit breaker(节点去重防环 + 总步数上限)。

**Tech Stack:** Python 3 / FastAPI / SQLite / pydantic v2 / numpy / pytest。LLM 走现有 `OpenAICompatibleClient.chat_json`。

**Scope:** 仅后端。前端折叠展示 `reasoning_trace` 是 follow-up 计划(本计划产出 trace 字段与 API 契约,前端可据此对接)。

---

## 复用接口速查(全部已核实)

| 复用项 | 位置 | 签名 / 返回 |
|---|---|---|
| `_KG_TYPES` | `sqlite_repository.py:117` | `("claim","formula","procedure","concept")` |
| `USABLE_STATUSES` | `sqlite_repository.py:108` | `("approved","reviewed","project_specific","conflict")` |
| `_knowledge_objects(db, nb, type)` | `:563` | `List[{id,payload,evidence:List[Evidence],status,owner,last_reviewed}]` |
| `_embed_query(query)` | `:2696` | `Optional[List[float]]` |
| `_vector_matrix(db, nb, table, id_col)` | `:2742` | `(ids, matrix)` |
| `query_sims(qv, ids, mat)` | `vector_index.py` | `{id: sim}` |
| `score_knowledge(query, objs, type, qv, None, None, element_sims=, knowledge_sims=)` | `retrieval.py:276` | `List[RetrievedKnowledge]` (按 score 降序) |
| `score_elements(query, elements, qv=None, limit=8, element_sims=None)` | `retrieval.py:363` | `List[RetrievedElement]` |
| `relations_for_notebook(nb)` | `:1846` | `List[{id,source_id,source_object_id,target_object_id,edge_type,evidence}]` |
| `node_context(nb, oid)` | `:2230` | `{id,object_type,name,section_path,occurrences,definition,steps}`,缺失 raise `KeyError` |
| `_gather_elements(db, nb, with_vectors=True)` | `:2704` | `List[{element_id,source_id,source_title,location_label,element_type,text,vector}]` |
| `_answer_context(nb, top_hits)` | `:3048` | `(context_block_str, id_map)` |
| `_parse_answer_anchors(answer, id_map)` | `:3136` | `List[AnswerAnchor]` |
| `classify_evidence(top_hits, anchors, llm_grounded, tau_low, tau_high)` | `retrieval.py:123` | `(evidence_level, top_relevance)` |
| `_ensure_conversation(db, nb, conv_id, question)` | `:3183` | `conversation_id` |
| `_conversation_history(db, conv_id, limit=5)` | `:3209` | history str |
| `_save_answer(nb, question, response, conv_id)` | `:3157` | `answer_id` |
| `_knowledge_record(type, obj, schema)` / `effective_schemas()` / `_citations_from(...)` | `:1477` / `:1536` / `:2804` | KnowledgeRecord / registry / `List[Citation]` |
| `answer_prompt(q, ctx, history)` / `ANSWER_SCHEMA_HINT` | `prompts.py:108` / `:105` | str / `'{"answer":"","grounded":true}'` |
| `llm_client.chat_json(messages, schema_hint)` / `.configured` | `core/llm.py:73` | JSON str / bool |
| `RetrievedKnowledge` | `retrieval.py:33` | dataclass: object_id, object_type, payload, evidence, score, relevance, weight, status, owner, last_reviewed |
| `RetrievedElement` | `retrieval.py:50` | dataclass: element_id, source_id, source_title, location_label, element_type, text, score |

**测试基础设施(现成,见 `tests/test_followup_retrieval_grounding.py`):**
- `repo.create_notebook(NotebookCreate(name="nb"))`
- `repo.store_kg(nb_id, source_id_or_None, objects, relations)`
  - `objects=[{"local_id","object_type","payload":{"name",...},"evidence":[]}]`
  - `relations=[{"source_local_id","target_local_id","edge_type","evidence":[]}]`
- fixture 设 `EMBED_*` env + `FakeEmbedder(dim=16)`(`app.services.embedding`) + 自定义 `chat_json` mock

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/models/schemas.py` | 改 | `TraceStep` 模型;`AskRequest.mode`;`AskResponse.reasoning_trace` |
| `app/core/config.py` | 改 | `reasoning_max_steps`、`reasoning_max_subqueries` 旋钮 |
| `app/services/retrieval.py` | 改 | `_fuse`/`score_knowledge` 加 `w_keyword`/`w_semantic` 参数(prefer 用) |
| `app/services/prompts.py` | 改 | `plan_prompt`/`reflect_prompt` + schema hints |
| `app/services/reasoning_retrieval.py` | **新建** | `ReasoningRetriever` + `SubQuery`/`ReflectDecision`/`ReasoningResult` + loop |
| `app/services/sqlite_repository.py` | 改 | `_retrieve_scored`/`_retrieve_neighbors`/`_retrieve_elements`/`_answer_reasoning`/`ask_reasoning` + `ask()` mode 分流 |
| `tests/test_reasoning_retrieval.py` | **新建** | 工具箱 / plan / reflect / loop 单测 |
| `tests/test_reasoning_ask.py` | **新建** | 端到端集成测 |

---

## Task 1: schema + config 脚手架

**Files:**
- Modify: `app/models/schemas.py` (新增 `TraceStep`;改 `AskRequest`、`AskResponse`)
- Modify: `app/core/config.py` (Settings 新增两旋钮)
- Test: `tests/test_reasoning_retrieval.py` (新建)

- [ ] **Step 1: Write the failing test**

新建 `tests/test_reasoning_retrieval.py`:
```python
import json
import pytest


def test_trace_step_model_shape():
    from app.models.schemas import TraceStep
    t = TraceStep(step_type="plan", summary="规划了 2 个子查询", detail={"n": 2})
    d = t.model_dump()
    assert d["step_type"] == "plan"
    assert d["summary"].startswith("规划")
    assert d["detail"] == {"n": 2}


def test_ask_request_mode_defaults_fast():
    from app.models.schemas import AskRequest
    assert AskRequest(question="x").mode == "fast"
    assert AskRequest(question="x", mode="reasoning").mode == "reasoning"


def test_ask_response_reasoning_trace_defaults_none_and_dumps():
    from app.models.schemas import AskResponse
    r = AskResponse(conclusion="x")
    assert r.reasoning_trace is None
    assert "reasoning_trace" in r.model_dump()


def test_reasoning_settings_knobs():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_max_steps == 50
    assert s.reasoning_max_subqueries == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -v`
Expected: FAIL — `ImportError`/`AttributeError` (TraceStep 不存在 / mode 字段缺失)

- [ ] **Step 3: Write minimal implementation**

`app/models/schemas.py` — 在 `Citation` 类后(约 `:139` 之后)新增:
```python
class TraceStep(BaseModel):
    """推理模式 agent 的一步轨迹(供前端折叠展示)。"""
    step_type: str            # plan | retrieve | reflect | expand | fallback | answer
    summary: str              # 人话摘要
    detail: Dict[str, Any] = Field(default_factory=dict)
```

`AskRequest`(`:141`)新增字段:
```python
class AskRequest(BaseModel):
    question: str
    scenario: Dict[str, str] = Field(default_factory=dict)
    conversation_id: Optional[str] = None
    mode: str = "fast"        # "fast" | "reasoning"
```

`AskResponse`(`:159`)新增字段(放在 `top_relevance` 之后):
```python
    # 推理模式 agent 轨迹;fast 模式恒为 None。
    reasoning_trace: Optional[List["TraceStep"]] = None
```

`app/core/config.py` — 在 `proc_min`(`:78`)之后新增:
```python
    # 推理模式(mode=reasoning)护栏: Reflect 循环总步数 circuit breaker。
    reasoning_max_steps: int = Field(50, env="REASONING_MAX_STEPS")
    # 推理模式 Plan 输出子查询数上限。
    reasoning_max_subqueries: int = Field(5, env="REASONING_MAX_SUBQUERIES")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/schemas.py backend/app/core/config.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): schema+config 脚手架(TraceStep/mode/reasoning_trace/旋钮)"
```

---

## Task 2: prefer 权重参数化 (retrieval.py)

**Files:**
- Modify: `app/services/retrieval.py` (`_fuse` `:266`、`score_knowledge` `:276`)
- Test: `tests/test_retrieval.py` (现有文件追加)

- [ ] **Step 1: Write the failing test**

`tests/test_retrieval.py` 追加:
```python
def test_fuse_custom_weights_shift_balance():
    from app.services.retrieval import _fuse
    # 默认 0.4/0.6: 语义为 0 时融合分 = keyword * 0.4/(0.4+0.6) = 0.4
    assert abs(_fuse(1.0, 0.0, True) - 0.4) < 1e-9
    # keyword-heavy 0.7/0.3: 同输入下关键词权重更高
    assert abs(_fuse(1.0, 0.0, True, w_keyword=0.7, w_semantic=0.3) - 0.7) < 1e-9


def test_score_knowledge_passes_weights_through():
    from app.services.retrieval import score_knowledge
    objs = [{"id": "o1", "payload": {"name": "RTL synthesis flow"}, "evidence": []}]
    # 纯关键词(无向量)下,提高 w_keyword 不应改变 keyword-only 融合分(归一化抵消),
    # 但调用必须接受参数且不报错,返回命中。
    hits = score_knowledge("RTL synthesis", objs, "claim", w_keyword=0.7, w_semantic=0.3)
    assert hits and hits[0].object_id == "o1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_retrieval.py::test_fuse_custom_weights_shift_balance tests/test_retrieval.py::test_score_knowledge_passes_weights_through -v`
Expected: FAIL — `_fuse() got an unexpected keyword argument 'w_keyword'`

- [ ] **Step 3: Write minimal implementation**

`app/services/retrieval.py` 改 `_fuse`(`:266`):
```python
def _fuse(keyword: float, semantic: float, has_vector: bool,
          w_keyword: float = W_KEYWORD, w_semantic: float = W_SEMANTIC) -> float:
    """Weighted-sum fusion, renormalized by active signals so keyword-only
    objects are scored on the same 0..1 scale instead of being capped at the
    keyword weight. Weights default to the module constants; the reasoning
    retriever overrides them per sub-query (prefer=keyword/semantic/balanced)."""
    semantic = max(0.0, semantic)
    denom = w_keyword + (w_semantic if has_vector else 0.0)
    if denom <= 0:
        return 0.0
    return (w_keyword * keyword + (w_semantic * semantic if has_vector else 0.0)) / denom
```

`score_knowledge`(`:276`)签名加两参数,并在 `:329` 的 `_fuse(...)` 调用透传:
```python
def score_knowledge(
    query: str,
    objects: List[dict],
    object_type: str,
    query_vector: Optional[List[float]] = None,
    element_vectors: Optional[Dict[str, List[float]]] = None,
    knowledge_vectors: Optional[Dict[str, List[float]]] = None,
    element_sims: Optional[Dict[str, float]] = None,
    knowledge_sims: Optional[Dict[str, float]] = None,
    w_keyword: float = W_KEYWORD,
    w_semantic: float = W_SEMANTIC,
) -> List[RetrievedKnowledge]:
```
将原 `relevance = _fuse(keyword, semantic, has_vector)` 改为:
```python
        relevance = _fuse(keyword, semantic, has_vector, w_keyword, w_semantic)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_retrieval.py -v`
Expected: PASS (含原有用例,默认权重行为不变)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_retrieval.py
git commit -m "feat(reasoning): score_knowledge 支持自定义融合权重(prefer 基础)"
```

---

## Task 3: plan/reflect prompts (prompts.py)

**Files:**
- Modify: `app/services/prompts.py` (文件末尾追加)
- Test: `tests/test_reasoning_retrieval.py` (追加)

- [ ] **Step 1: Write the failing test**

`tests/test_reasoning_retrieval.py` 追加:
```python
def test_plan_prompt_contains_question_and_schema():
    from app.services.prompts import plan_prompt, PLAN_SCHEMA_HINT
    p = plan_prompt("innovus 的 PR 流程", "User: ...\nAssistant: ...")
    assert "innovus 的 PR 流程" in p
    assert "sub_queries" in PLAN_SCHEMA_HINT
    assert "prefer" in PLAN_SCHEMA_HINT


def test_reflect_prompt_contains_summary_and_schema():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    p = reflect_prompt("问题X", "- [claim] A (id=k1)")
    assert "问题X" in p
    assert "id=k1" in p
    assert "next_action" in REFLECT_SCHEMA_HINT
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k "plan_prompt or reflect_prompt" -v`
Expected: FAIL — `ImportError: cannot import name 'plan_prompt'`

- [ ] **Step 3: Write minimal implementation**

`app/services/prompts.py` 末尾追加:
```python
PLAN_SCHEMA_HINT = (
    '{"sub_queries":[{"query":"","types":["concept|claim|formula|procedure"],'
    '"prefer":"keyword|semantic|balanced","reason":""}]}'
)


def plan_prompt(question: str, history_block: str = "") -> str:
    history_section = (
        "Prior conversation (resolve pronouns/ellipsis against it):\n"
        f"{history_block}\n\n" if history_block else ""
    )
    return (
        "You plan how to retrieve a knowledge graph (KG) to answer an "
        "engineer's question. The KG has 4 node types: concept (definitions), "
        "claim (conclusions), formula (math/models), procedure (step flows).\n"
        "Decompose the question into 1-N standalone sub-queries. For EACH:\n"
        "- query: a self-contained search string (resolve any references using "
        "the prior conversation).\n"
        "- types: which node types to search (subset of the 4; omit/empty = all).\n"
        "- prefer: keyword (exact terms/codes), semantic (paraphrase/concept), "
        "or balanced.\n"
        "- reason: one line on why this sub-query.\n"
        "Keep sub-queries focused and non-redundant. Return JSON only.\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        'Return JSON only: {"sub_queries":[{"query":"","types":[],'
        '"prefer":"balanced","reason":""}]}'
    )


REFLECT_SCHEMA_HINT = (
    '{"sufficient":false,"next_action":"answer|expand_graph|add_subquery|'
    'search_elements","expand":{"object_id":"","edge_type":null,'
    '"direction":"out|in|both"},"new_sub_query":{"query":"","types":[],'
    '"prefer":"balanced","reason":""},"elements_query":"","reason":""}'
)


def reflect_prompt(question: str, candidates_summary: str) -> str:
    return (
        "You decide the NEXT retrieval step for answering a question from a "
        "knowledge graph. Below are the candidates gathered so far.\n"
        "Choose next_action:\n"
        "- answer: candidates suffice — stop and answer.\n"
        "- expand_graph: a candidate looks central; follow its relations one "
        "more hop (set expand.object_id, optional edge_type/direction). You may "
        "expand repeatedly across turns — go as deep as the question needs.\n"
        "- add_subquery: an aspect of the question is uncovered; add one "
        "sub-query (set new_sub_query).\n"
        "- search_elements: the KG is too thin; fall back to raw document "
        "passages (set elements_query).\n"
        "Set sufficient=true only when you can answer well. reason: one line.\n"
        "Return JSON only.\n\n"
        f"Question: {question}\n\n"
        f"Candidates so far:\n{candidates_summary}\n\n"
        'Return JSON only matching the schema (omit unused branch fields).'
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k "plan_prompt or reflect_prompt" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prompts.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): plan/reflect prompt 模板 + schema hint"
```

---

## Task 4: repo `_retrieve_scored` 检索原语

**Files:**
- Modify: `app/services/sqlite_repository.py` (新增方法,建议紧邻 `_vector_matrix` 之后 `:2818` 前)
- Test: `tests/test_reasoning_retrieval.py` (追加)

- [ ] **Step 1: Write the failing test**

`tests/test_reasoning_retrieval.py` 顶部追加 fixture(与 `test_followup_retrieval_grounding.py` 同款),再加用例:
```python
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def rrepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_retrieve_scored_returns_sorted_hits(rrepo):
    nb = _seed_two_nodes(rrepo)
    hits = rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
    assert hits and hits[0].relevance >= (hits[-1].relevance if len(hits) > 1 else 0)
    assert any(h.object_type == "claim" for h in hits)


def test_retrieve_scored_filters_types(rrepo):
    nb = _seed_two_nodes(rrepo)
    hits = rrepo._retrieve_scored(nb.id, "布局布线", types=["procedure"])
    assert all(h.object_type == "procedure" for h in hits)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k retrieve_scored -v`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute '_retrieve_scored'`

- [ ] **Step 3: Write minimal implementation**

`app/services/sqlite_repository.py` — 在 `def ask(`(`:2818`)之前新增。确保文件顶部已 `from app.services.retrieval import (... score_knowledge, W_KEYWORD, W_SEMANTIC ...)`(若 `W_KEYWORD/W_SEMANTIC` 未导入则补到现有 retrieval 导入行):
```python
    def _retrieve_scored(self, notebook_id: str, query: str,
                         types: Optional[Iterable[str]] = None,
                         w_keyword: float = W_KEYWORD,
                         w_semantic: float = W_SEMANTIC) -> List[RetrievedKnowledge]:
        """Score KG objects of `types` (default all 4 _KG_TYPES) for `query`,
        returning RetrievedKnowledge sorted by fused relevance desc. Shared by
        the reasoning retriever's tools; `w_keyword`/`w_semantic` carry the
        per-sub-query `prefer` bias."""
        type_list = [t for t in (list(types) if types else list(_KG_TYPES)) if t in _KG_TYPES]
        with self._connect() as db:
            kg_objs = {t: self._knowledge_objects(db, notebook_id, t) for t in type_list}
            query_vector = self._embed_query(query)
            elem_ids, elem_mat = self._vector_matrix(db, notebook_id, "element_embeddings", "element_id")
            kn_ids, kn_mat = self._vector_matrix(db, notebook_id, "knowledge_embeddings", "object_id")
        from app.services.vector_index import query_sims
        element_sims = query_sims(query_vector, elem_ids, elem_mat) if query_vector else None
        knowledge_sims = query_sims(query_vector, kn_ids, kn_mat) if query_vector else None
        scored: List[RetrievedKnowledge] = []
        for t in type_list:
            objs = kg_objs.get(t) or []
            if not objs:
                continue
            scored.extend(score_knowledge(
                query, objs, t, query_vector, None, None,
                element_sims=element_sims, knowledge_sims=knowledge_sims,
                w_keyword=w_keyword, w_semantic=w_semantic,
            ))
        scored.sort(key=lambda it: it.score, reverse=True)
        return scored
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k retrieve_scored -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): repo._retrieve_scored 检索原语(types+prefer 权重)"
```

---

## Task 5: repo `_retrieve_neighbors` + `_retrieve_elements`

**Files:**
- Modify: `app/services/sqlite_repository.py` (紧接 `_retrieve_scored` 之后)
- Test: `tests/test_reasoning_retrieval.py` (追加)

- [ ] **Step 1: Write the failing test**

```python
def test_retrieve_neighbors_follows_edges(rrepo):
    nb = _seed_two_nodes(rrepo)
    # 找到 C1 的 DB id (claim 命中)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    neigh = rrepo._retrieve_neighbors(nb.id, claim.object_id)
    assert any(n.object_type == "procedure" for n in neigh)


def test_retrieve_neighbors_edge_type_filter(rrepo):
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    assert rrepo._retrieve_neighbors(nb.id, claim.object_id, edge_type="nonexistent") == []


def test_retrieve_elements_degrades_gracefully(rrepo):
    nb = _seed_two_nodes(rrepo)
    # 无 source_elements 时返回空列表,不报错
    assert rrepo._retrieve_elements(nb.id, "任意查询") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k "retrieve_neighbors or retrieve_elements" -v`
Expected: FAIL — `AttributeError: ... '_retrieve_neighbors'`

- [ ] **Step 3: Write minimal implementation**

`app/services/sqlite_repository.py` — 紧接 `_retrieve_scored` 之后(确保顶部已 `from app.services.retrieval import (... score_elements ...)`):
```python
    def _retrieve_neighbors(self, notebook_id: str, object_id: str,
                            edge_type: Optional[str] = None,
                            direction: str = "both") -> List[RetrievedKnowledge]:
        """1-hop graph neighbours of `object_id` as RetrievedKnowledge with
        placeholder relevance=0 (final relevance unified by run() via the
        original question). Honours edge_type filter; direction out=object as
        source, in=as target, both=either."""
        neighbour_ids: set = set()
        for rel in self.relations_for_notebook(notebook_id):
            if edge_type and rel["edge_type"] != edge_type:
                continue
            src, tgt = rel["source_object_id"], rel["target_object_id"]
            if object_id == src and direction in ("out", "both"):
                neighbour_ids.add(tgt)
            elif object_id == tgt and direction in ("in", "both"):
                neighbour_ids.add(src)
        if not neighbour_ids:
            return []
        with self._connect() as db:
            placeholders = ",".join("?" for _ in neighbour_ids)
            status_ph = ",".join("?" for _ in USABLE_STATUSES)
            rows = db.execute(
                f"SELECT * FROM knowledge_objects WHERE id IN ({placeholders}) "
                f"AND status IN ({status_ph})",
                [*neighbour_ids, *USABLE_STATUSES],
            ).fetchall()
        out: List[RetrievedKnowledge] = []
        for row in rows:
            keys = row.keys()
            out.append(RetrievedKnowledge(
                object_id=row["id"], object_type=row["object_type"],
                payload=json.loads(row["payload"] or "{}"),
                evidence=[Evidence(**e) for e in json.loads(row["evidence"] or "[]")],
                score=0.0, relevance=0.0, status=row["status"], owner=row["owner"],
                last_reviewed=row["last_reviewed"] if "last_reviewed" in keys else "",
            ))
        return out

    def _retrieve_elements(self, notebook_id: str, query: str,
                           limit: int = 8) -> List[RetrievedElement]:
        """Keyword+semantic search over raw source_elements (fallback layer 2)."""
        query_vector = self._embed_query(query)
        with self._connect() as db:
            elements = self._gather_elements(db, notebook_id, with_vectors=True)
        return score_elements(query, elements, query_vector, limit=limit)
```

确保顶部 retrieval 导入含 `RetrievedElement`、`score_elements`(若缺则补)。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k "retrieve_neighbors or retrieve_elements" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): repo._retrieve_neighbors/_retrieve_elements(图遍历+原文降级)"
```

---

## Task 6: ReasoningRetriever 数据类 + 工具箱

**Files:**
- Create: `app/services/reasoning_retrieval.py`
- Test: `tests/test_reasoning_retrieval.py` (追加)

- [ ] **Step 1: Write the failing test**

```python
def test_toolbox_delegates_to_repo(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rr = ReasoningRetriever(rrepo, rrepo.settings)
    hits = rr.search(nb.id, "RTL到GDSII流程", types=["claim"], prefer="keyword")
    assert all(h.object_type == "claim" for h in hits)
    claim = hits[0]
    neigh = rr.neighbors(nb.id, claim.object_id)
    assert any(n.object_type == "procedure" for n in neigh)
    ctx = rr.get(nb.id, claim.object_id)
    assert ctx.get("object_type") == "claim"
    assert rr.get(nb.id, "no-such-id") == {}     # KeyError 吞掉
    assert rr.search_elements(nb.id, "x") == []   # 无原文不报错
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k toolbox -v`
Expected: FAIL — `ModuleNotFoundError: app.services.reasoning_retrieval`

- [ ] **Step 3: Write minimal implementation**

新建 `app/services/reasoning_retrieval.py`:
```python
"""推理模式 (mode=reasoning) 的 agentic KG 检索。

结构化骨架 Plan→Retrieve→Reflect→Answer + Reflect 阶段自由图遍历深挖。
手搓 JSON-action 循环(无原生 tool calling),复用 SQLiteRepository 的检索原语。
ReasoningRetriever 持 repo 引用,运行时注入,避免与 sqlite_repository 循环导入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.models.schemas import TraceStep
from app.services.prompts import (
    PLAN_SCHEMA_HINT, REFLECT_SCHEMA_HINT, plan_prompt, reflect_prompt,
)
from app.services.retrieval import (
    RetrievedElement, RetrievedKnowledge, W_KEYWORD, W_SEMANTIC,
)

KG_TYPES = ("claim", "formula", "procedure", "concept")
PREFER_WEIGHTS = {
    "keyword": (0.7, 0.3),
    "semantic": (0.2, 0.8),
    "balanced": (W_KEYWORD, W_SEMANTIC),
}
_PER_QUERY_LIMIT = 8


@dataclass
class SubQuery:
    query: str
    types: List[str] = field(default_factory=list)   # 空 = 全部 4 类
    prefer: str = "balanced"
    reason: str = ""


@dataclass
class ReflectDecision:
    sufficient: bool = False
    next_action: str = "answer"   # answer|expand_graph|add_subquery|search_elements
    expand_object_id: str = ""
    expand_edge_type: Optional[str] = None
    expand_direction: str = "both"
    new_sub_query: Optional[SubQuery] = None
    elements_query: str = ""
    reason: str = ""


@dataclass
class ReasoningResult:
    top_hits: List[RetrievedKnowledge] = field(default_factory=list)
    elements: List[RetrievedElement] = field(default_factory=list)
    trace: List[TraceStep] = field(default_factory=list)


class ReasoningRetriever:
    def __init__(self, repo, settings):
        self.repo = repo
        self.settings = settings

    # --- KG 工具箱(薄封装 repo 原语) ---
    def search(self, notebook_id, query, types=None, prefer="balanced"):
        wk, ws = PREFER_WEIGHTS.get(prefer, PREFER_WEIGHTS["balanced"])
        return self.repo._retrieve_scored(notebook_id, query, types=types,
                                          w_keyword=wk, w_semantic=ws)

    def neighbors(self, notebook_id, object_id, edge_type=None, direction="both"):
        return self.repo._retrieve_neighbors(notebook_id, object_id, edge_type, direction)

    def get(self, notebook_id, object_id):
        try:
            return self.repo.node_context(notebook_id, object_id)
        except KeyError:
            return {}

    def search_elements(self, notebook_id, query):
        return self.repo._retrieve_elements(notebook_id, query)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k toolbox -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): ReasoningRetriever 数据类 + KG 工具箱"
```

---

## Task 7: plan + reflect 阶段(含 JSON 容错)

**Files:**
- Modify: `app/services/reasoning_retrieval.py` (`ReasoningRetriever` 加 `plan`/`reflect`)
- Test: `tests/test_reasoning_retrieval.py` (追加)

- [ ] **Step 1: Write the failing test**

```python
class _StubLLM:
    """按 schema_hint 返回预置 JSON;configured 可控。"""
    def __init__(self, plan=None, reflect=None, configured=True):
        self._plan = plan
        self._reflect = reflect
        self.configured = configured
    def chat_json(self, messages, schema_hint):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        return json.dumps(self._reflect)


def _rr_with_llm(repo, **llm):
    from app.services.reasoning_retrieval import ReasoningRetriever
    repo.llm_client = _StubLLM(**llm)
    return ReasoningRetriever(repo, repo.settings)


def test_plan_parses_subqueries(rrepo):
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "RTL综合", "types": ["claim"], "prefer": "keyword", "reason": "r"},
        {"query": "布线", "types": ["bogus"], "prefer": "weird"},
    ]})
    subs = rr.plan("问题", "")
    assert [s.query for s in subs] == ["RTL综合", "布线"]
    assert subs[0].types == ["claim"] and subs[0].prefer == "keyword"
    assert subs[1].types == [] and subs[1].prefer == "balanced"  # 非法值被清洗


def test_plan_truncates_to_max_subqueries(rrepo):
    rrepo.settings.reasoning_max_subqueries = 2
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "a"}, {"query": "b"}, {"query": "c"}]})
    assert len(rr.plan("q", "")) == 2


def test_plan_falls_back_on_bad_json(rrepo):
    rr = _rr_with_llm(rrepo, plan={"garbage": 1})
    subs = rr.plan("原问题X", "")
    assert len(subs) == 1 and subs[0].query == "原问题X"


def test_plan_falls_back_when_llm_unconfigured(rrepo):
    rr = _rr_with_llm(rrepo, configured=False)
    subs = rr.plan("原问题Y", "")
    assert len(subs) == 1 and subs[0].query == "原问题Y"


def test_reflect_parses_expand(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "sufficient": False, "next_action": "expand_graph",
        "expand": {"object_id": "ko-1", "edge_type": "relates", "direction": "out"},
        "reason": "深挖"})
    d = rr.reflect("q", "summary")
    assert d.next_action == "expand_graph" and d.expand_object_id == "ko-1"
    assert d.expand_edge_type == "relates" and d.expand_direction == "out"


def test_reflect_bad_json_becomes_answer(rrepo):
    rr = _rr_with_llm(rrepo, reflect=["not", "a", "dict"])
    d = rr.reflect("q", "s")
    assert d.next_action == "answer" and d.sufficient is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k "plan_parses or plan_truncates or plan_falls or reflect_parses or reflect_bad" -v`
Expected: FAIL — `AttributeError: 'ReasoningRetriever' object has no attribute 'plan'`

- [ ] **Step 3: Write minimal implementation**

`app/services/reasoning_retrieval.py` — `ReasoningRetriever` 内追加:
```python
    # --- LLM 决策点 ---
    def plan(self, question, history=""):
        fallback = [SubQuery(query=question)]
        if not getattr(self.repo.llm_client, "configured", False):
            return fallback
        try:
            raw = self.repo.llm_client.chat_json(
                [{"role": "user", "content": plan_prompt(question, history)}],
                PLAN_SCHEMA_HINT)
            data = json.loads(raw)
            subs = data.get("sub_queries") if isinstance(data, dict) else None
            if not isinstance(subs, list) or not subs:
                return fallback
            out: List[SubQuery] = []
            for s in subs[: self.settings.reasoning_max_subqueries]:
                if not isinstance(s, dict):
                    continue
                q = str(s.get("query", "")).strip()
                if not q:
                    continue
                types = [t for t in (s.get("types") or []) if t in KG_TYPES]
                prefer = s.get("prefer") if s.get("prefer") in PREFER_WEIGHTS else "balanced"
                out.append(SubQuery(query=q, types=types, prefer=prefer,
                                    reason=str(s.get("reason", ""))))
            return out or fallback
        except Exception:
            return fallback

    def reflect(self, question, candidates_summary):
        answer_decision = ReflectDecision(sufficient=True, next_action="answer")
        try:
            raw = self.repo.llm_client.chat_json(
                [{"role": "user", "content": reflect_prompt(question, candidates_summary)}],
                REFLECT_SCHEMA_HINT)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return answer_decision
            action = str(data.get("next_action", "answer"))
            if action not in ("answer", "expand_graph", "add_subquery", "search_elements"):
                action = "answer"
            d = ReflectDecision(
                sufficient=bool(data.get("sufficient", False)),
                next_action=action, reason=str(data.get("reason", "")))
            exp = data.get("expand")
            if isinstance(exp, dict):
                d.expand_object_id = str(exp.get("object_id", ""))
                et = exp.get("edge_type")
                d.expand_edge_type = str(et) if et else None
                dr = exp.get("direction")
                d.expand_direction = dr if dr in ("out", "in", "both") else "both"
            nsq = data.get("new_sub_query")
            if isinstance(nsq, dict) and str(nsq.get("query", "")).strip():
                types = [t for t in (nsq.get("types") or []) if t in KG_TYPES]
                prefer = nsq.get("prefer") if nsq.get("prefer") in PREFER_WEIGHTS else "balanced"
                d.new_sub_query = SubQuery(query=str(nsq["query"]).strip(),
                                           types=types, prefer=prefer,
                                           reason=str(nsq.get("reason", "")))
            d.elements_query = str(data.get("elements_query", "")).strip()
            return d
        except Exception:
            return answer_decision
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k "plan or reflect" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): plan/reflect 阶段(JSON 解析+容错降级)"
```

---

## Task 8: loop `run` 编排 + trace + 护栏

**Files:**
- Modify: `app/services/reasoning_retrieval.py` (`ReasoningRetriever` 加 `_summarize`/`run`)
- Test: `tests/test_reasoning_retrieval.py` (追加)

- [ ] **Step 1: Write the failing test**

```python
class _SeqLLM:
    """plan 固定;reflect 按序列返回(耗尽后默认 answer)。"""
    configured = True
    def __init__(self, plan, reflects):
        self._plan = plan
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def test_run_plan_then_answer(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True, "reason": "够了"}])
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert res.top_hits  # 召回到候选
    kinds = [t.step_type for t in res.trace]
    assert kinds[0] == "plan" and "retrieve" in kinds and kinds[-1] == "answer"


def test_run_expand_graph_records_trace(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id},
             "reason": "深挖关系"},
            {"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert any(t.step_type == "expand" for t in res.trace)
    assert any(h.object_type == "procedure" for h in res.top_hits)  # 邻居被纳入


def test_run_dedups_expand_and_respects_step_cap(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    rrepo.settings.reasoning_max_steps = 3
    # 始终要求 expand 同一节点 → 去重后无新增,且步数撞上限强制收尾
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "expand_graph",
                   "expand": {"object_id": claim.object_id}}] * 10)
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) <= 3                 # circuit breaker 生效
    assert res.trace[-1].step_type == "answer"     # 仍正常收尾
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -k run_ -v`
Expected: FAIL — `AttributeError: ... 'run'`

- [ ] **Step 3: Write minimal implementation**

`app/services/reasoning_retrieval.py` — `ReasoningRetriever` 内追加:
```python
    # --- 编排 ---
    def _summarize(self, collected, elements):
        lines = []
        for rk in list(collected.values())[:30]:
            name = str(rk.payload.get("name", "")).strip() or rk.object_id
            lines.append(f"- [{rk.object_type}] {name} (id={rk.object_id})")
        for el in elements[:10]:
            lines.append(f"- [element] {el.source_title} · {el.location_label}: {el.text[:80]}")
        return "\n".join(lines) if lines else "(no candidates yet)"

    def run(self, notebook_id, question, history=""):
        trace: List[TraceStep] = []
        collected: Dict[str, RetrievedKnowledge] = {}
        elements: List[RetrievedElement] = []
        visited: set = set()

        subqueries = self.plan(question, history)
        trace.append(TraceStep(
            step_type="plan", summary=f"规划了 {len(subqueries)} 个子查询",
            detail={"sub_queries": [{"query": s.query, "types": s.types,
                                     "prefer": s.prefer, "reason": s.reason}
                                    for s in subqueries]}))

        for sq in subqueries:
            for h in self.search(notebook_id, sq.query, sq.types, sq.prefer)[:_PER_QUERY_LIMIT]:
                collected.setdefault(h.object_id, h)
        trace.append(TraceStep(step_type="retrieve",
                               summary=f"初检索得到 {len(collected)} 个候选节点",
                               detail={"count": len(collected)}))

        steps = 0
        while steps < self.settings.reasoning_max_steps:
            steps += 1
            decision = self.reflect(question, self._summarize(collected, elements))
            trace.append(TraceStep(step_type="reflect",
                                   summary=decision.reason or decision.next_action,
                                   detail={"next_action": decision.next_action,
                                           "sufficient": decision.sufficient}))
            if decision.next_action == "answer" or decision.sufficient:
                break
            if decision.next_action == "expand_graph":
                oid = decision.expand_object_id
                if not oid or oid in visited:
                    continue
                visited.add(oid)
                neigh = self.neighbors(notebook_id, oid,
                                       decision.expand_edge_type, decision.expand_direction)
                for h in neigh:
                    collected.setdefault(h.object_id, h)
                trace.append(TraceStep(step_type="expand",
                                       summary=f"顺关系深挖 {oid},得到 {len(neigh)} 个邻居",
                                       detail={"object_id": oid,
                                               "edge_type": decision.expand_edge_type,
                                               "found": len(neigh)}))
            elif decision.next_action == "add_subquery" and decision.new_sub_query:
                sq = decision.new_sub_query
                for h in self.search(notebook_id, sq.query, sq.types, sq.prefer)[:_PER_QUERY_LIMIT]:
                    collected.setdefault(h.object_id, h)
                trace.append(TraceStep(step_type="retrieve",
                                       summary=f"补充子查询: {sq.query}",
                                       detail={"query": sq.query}))
            elif decision.next_action == "search_elements":
                eq = decision.elements_query or question
                els = self.search_elements(notebook_id, eq)
                elements.extend(els)
                trace.append(TraceStep(step_type="fallback",
                                       summary=f"降级查原文: {eq},命中 {len(els)} 段",
                                       detail={"query": eq, "found": len(els)}))
            else:
                break

        # 统一口径: 用原问题对全库重打分,agent 召回的候选优先用此版本(带原问题 relevance)
        scored_map = {h.object_id: h for h in self.repo._retrieve_scored(notebook_id, question)}
        top_hits = [scored_map.get(oid, rk) for oid, rk in collected.items()]
        top_hits.sort(key=lambda h: h.relevance, reverse=True)
        top_hits = top_hits[: self.settings.retrieval_top_n]
        trace.append(TraceStep(step_type="answer",
                               summary=f"合成: 采用 {len(top_hits)} 个KG候选 + {len(elements)} 段原文",
                               detail={"kg": len(top_hits), "elements": len(elements)}))
        return ReasoningResult(top_hits=top_hits, elements=elements, trace=trace)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -v`
Expected: PASS (全文件)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): loop run 编排 + trace + 护栏(去重/步数上限/统一重打分)"
```

---

## Task 9: repo `ask_reasoning` + 答案合成 + mode 分流 + 降级

**Files:**
- Modify: `app/services/sqlite_repository.py` (`ask()` 头部分流;新增 `_answer_reasoning`、`ask_reasoning`)
- Test: `tests/test_reasoning_ask.py` (新建)

- [ ] **Step 1: Write the failing test**

新建 `tests/test_reasoning_ask.py`:
```python
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


class _SeqLLM:
    configured = True
    def __init__(self, plan, reflects, answer):
        self._plan, self._reflects, self._answer = plan, list(reflects), answer
    def chat_json(self, messages, schema_hint):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps(self._answer)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    return nb


def test_reasoning_ask_returns_trace_and_evidence_level(arepo):
    nb = _seed(arepo)
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "RTL到GDSII是标准流程 [k1].", "grounded": True})
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.reasoning_trace and resp.reasoning_trace[0].step_type == "plan"
    assert resp.evidence_level in {"grounded", "overview", "inferred"}
    assert resp.conversation_id


def test_fast_mode_unaffected_and_no_trace(arepo):
    nb = _seed(arepo)
    arepo.llm_client = _SeqLLM(plan={}, reflects=[],
                               answer={"answer": "x", "grounded": False})
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程"))  # 默认 fast
    assert resp.reasoning_trace is None


def test_reasoning_falls_back_to_fast_on_llm_error(arepo):
    nb = _seed(arepo)
    class _BoomLLM:
        configured = True
        def chat_json(self, messages, schema_hint):
            raise RuntimeError("boom")
    arepo.llm_client = _BoomLLM()
    # 整体不抛错: ReasoningRetriever.run 内 plan/reflect 各自容错;answer 合成失败被吞;
    # 即便如此也应返回一个合法 AskResponse。
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.evidence_level in {"grounded", "overview", "inferred"}
    assert resp.conversation_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reasoning_ask.py -v`
Expected: FAIL — `test_reasoning_ask_returns_trace...` 失败(reasoning_trace 为 None / mode 未分流)

- [ ] **Step 3: Write minimal implementation**

`app/services/sqlite_repository.py` — `ask()`(`:2818`)函数体最前(在 `import time` 之前或之后均可,放在 docstring 之后第一行逻辑)插入分流:
```python
    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """KG-native ask: ... (原 docstring 保留)"""
        if getattr(payload, "mode", "fast") == "reasoning":
            return self.ask_reasoning(notebook_id, payload)
        import time
        ask_started = time.perf_counter()
        # ... 原有逻辑不变 ...
```

在 `_answer_kg`(`:3111`)之后新增 `_answer_reasoning`:
```python
    def _answer_reasoning(self, notebook_id, question, top_hits, elements, history=""):
        """Synthesise the reasoning-mode answer: reuse _answer_context for KG
        hits (so [k] anchors/citations stay identical to fast mode), then append
        fallback document passages as reference-only context (no [k] id, so they
        never become anchors). Returns (answer, llm_grounded, anchors)."""
        context_block, id_map = self._answer_context(notebook_id, top_hits)
        if elements:
            extra = "\n".join(
                f"(原文 {i+1}) {el.source_title} · {el.location_label}: {el.text[:200]}"
                for i, el in enumerate(elements[:6])
            )
            context_block = f"{context_block}\n\n补充原文段落(供参考,无引用编号):\n{extra}"
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
```

紧接其后新增 `ask_reasoning`(复用 fast 路径的 related_knowledge / citations / conclusion 构造,差异仅检索来自 ReasoningRetriever 且响应带 trace):
```python
    def ask_reasoning(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Reasoning-mode ask: agentic plan→retrieve→reflect(自由深挖)→answer。
        检索委托 ReasoningRetriever;答案/证据分档复用 fast 路径口径;响应携带
        reasoning_trace。任何阶段异常不向用户抛出(逐层容错 + 兜底空候选)。"""
        from app.services.reasoning_retrieval import ReasoningRetriever
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        with self._connect() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)

        try:
            result = ReasoningRetriever(self, self.settings).run(notebook_id, question, history)
            top_hits, elements, trace = result.top_hits, result.elements, result.trace
        except Exception:
            top_hits, elements, trace = [], [], []

        # related_knowledge from top_hits (hits only; reasoning 不做额外 1-hop,
        # 深挖已在 loop 内并入 top_hits)。
        registry = self.effective_schemas()
        seen_ids: set = set()
        related_knowledge: List[KnowledgeRecord] = []
        for item in top_hits:
            if item.object_id in seen_ids:
                continue
            seen_ids.add(item.object_id)
            related_knowledge.append(self._knowledge_record(
                item.object_type,
                {"id": item.object_id, "payload": item.payload, "status": item.status,
                 "owner": getattr(item, "owner", ""),
                 "last_reviewed": getattr(item, "last_reviewed", ""),
                 "evidence": item.evidence},
                registry.get(item.object_type)))
        related_knowledge = related_knowledge[:12]

        cited_element_ids = {ev.element_id for item in top_hits
                             for ev in item.evidence if ev.element_id}
        citations = self._citations_from(top_hits, cited_element_ids, "KG evidence")

        answer, llm_grounded, anchors = "", False, []
        if self.llm_client.configured and (top_hits or elements):
            try:
                answer, llm_grounded, anchors = self._answer_reasoning(
                    notebook_id, question, top_hits, elements, history)
            except Exception:
                answer, llm_grounded, anchors = "", False, []

        evidence_level, top_relevance = classify_evidence(
            top_hits, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
        grounded = evidence_level == "grounded"

        if answer:
            conclusion = _MARKER_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        else:
            llm_mode = "deterministic"
            conclusion = (
                f"Found {len(top_hits)} relevant KG object(s) for this question."
                if top_hits else
                "The notebook does not yet contain approved knowledge that matches "
                "this question. Upload and review sources to build coverage.")

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors,
            related_knowledge=related_knowledge, citations=citations,
            llm_mode=llm_mode, conversation_id=conversation_id,
            retrieval_query=question, top_relevance=top_relevance,
            reasoning_trace=trace or None,
        )
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        return response
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_reasoning_ask.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_reasoning_ask.py
git commit -m "feat(reasoning): ask_reasoning + _answer_reasoning + mode 分流 + 逐层容错"
```

---

## Task 10: 端到端集成测试(各路径全覆盖)

**Files:**
- Test: `tests/test_reasoning_ask.py` (追加)

- [ ] **Step 1: Write the failing test**

```python
def _seed_graph(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_reasoning_expand_then_answer_end_to_end(arepo):
    nb = _seed_graph(arepo)
    claim = next(h for h in arepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
            {"next_action": "answer", "sufficient": True}],
        answer={"answer": "流程概述 [k1],其布线步骤见 [k2].", "grounded": True})
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    step_kinds = [t.step_type for t in resp.reasoning_trace]
    assert "expand" in step_kinds
    # 深挖到的 procedure 进入了 related_knowledge
    assert any(r.object_type == "procedure" for r in resp.related_knowledge)


def test_reasoning_search_elements_fallback_path(arepo):
    nb = _seed_graph(arepo)
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "完全不相关的冷门问题zzz"}]},
        reflects=[
            {"next_action": "search_elements", "elements_query": "zzz"},
            {"next_action": "answer", "sufficient": True}],
        answer={"answer": "知识库未覆盖,以下为推断。", "grounded": False})
    resp = arepo.ask(nb.id, AskRequest(question="冷门zzz", mode="reasoning"))
    assert any(t.step_type == "fallback" for t in resp.reasoning_trace)
    assert resp.evidence_level == "inferred"  # 无强相关 KG 命中 → 推断档


def test_reasoning_conversation_persists(arepo):
    nb = _seed_graph(arepo)
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "答案 [k1].", "grounded": True})
    t1 = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    detail = arepo.get_conversation(t1.conversation_id)
    assert detail.turn_count == 1
    # 存回的轮次能反序列化(reasoning_trace 不破坏 AskResponse 往返)
    assert detail.turns[0].response.evidence_level == t1.evidence_level
```

- [ ] **Step 2: Run test to verify it fails (or passes if behavior already correct)**

Run: `cd backend && python -m pytest tests/test_reasoning_ask.py -k "expand_then or search_elements_fallback or conversation_persists" -v`
Expected: 若 Task 9 实现完整,这些断言应已可过;若 `test_reasoning_conversation_persists` 因 `AskResponse(**payload)` 往返失败(TraceStep 反序列化),则在 `get_conversation` 已用 `AskResponse(**payload)`——pydantic 会自动从 dict 重建 `reasoning_trace: List[TraceStep]`,应通过。任何失败按红-绿修复。

- [ ] **Step 3: Fix if needed**

若 `test_reasoning_search_elements_fallback_path` 的 `evidence_level` 非 `inferred`:确认 `_seed_graph` 的节点与"冷门zzz"无关键词重叠(融合分 < `RELEVANCE_FLOOR=0.12`),从而 `top_hits` 为空或弱、anchors 空 → `classify_evidence` 返回 `inferred`。若 KG 节点意外命中,调整种子 payload 名称使其与冷门 query 无 token 重叠。

- [ ] **Step 4: Run full suite**

Run: `cd backend && python -m pytest tests/test_reasoning_ask.py tests/test_reasoning_retrieval.py tests/test_retrieval.py tests/test_followup_retrieval_grounding.py -v`
Expected: ALL PASS (新功能 + 既有回归)

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_reasoning_ask.py
git commit -m "test(reasoning): 端到端覆盖 expand/原文降级/会话持久化"
```

---

## Self-Review

**1. Spec coverage(逐条对照 spec):**
- D1 独立 mode → Task 1(字段) + Task 9(分流) ✓
- D2 混合骨架 + Reflect 自由深挖 → Task 8 `run` ✓
- D3 手搓 JSON-action → Task 7 plan/reflect 走 `chat_json` ✓
- D4 跳数模型自定无上限 → Task 8 expand 无跳数限制,仅 `visited` 去重 ✓
- D5 护栏 circuit breaker → Task 8 `reasoning_max_steps` + `visited` ✓(测试 `test_run_dedups_expand_and_respects_step_cap`)
- D6 follow-up/意图吸收进 Plan → Task 3 plan_prompt 含指代消解 + types 选择;reasoning 路径不调 `_rewrite_followup_query`/`is_process_query` ✓
- D7 兜底链 KG→原文→inferred → Task 8 search_elements + Task 9 classify_evidence(`test_reasoning_search_elements_fallback_path`) ✓
- D8 LLM 失败降级 → Task 7 plan/reflect 容错 + Task 9 `_answer_reasoning` 失败吞掉 + run 整体 try/except(`test_reasoning_falls_back_to_fast_on_llm_error`) ✓
- D9 合成复用三档 → Task 9 复用 `_answer_context`/`_parse_answer_anchors`/`classify_evidence` ✓
- §5.2 relevance 口径统一 → Task 8 末尾原问题重打分 ✓
- §5.6 TraceStep → Task 1 ✓;轨迹随响应返回 → Task 9 ✓
- §9 配置项 → Task 1 ✓

**2. Placeholder scan:** 无 TBD/TODO;每步含真实测试与实现代码、确切命令与期望。✓

**3. Type consistency:**
- `SubQuery(query, types, prefer, reason)` / `ReflectDecision(...)` / `ReasoningResult(top_hits, elements, trace)` 在 Task 6 定义,Task 7/8 一致使用 ✓
- `TraceStep(step_type, summary, detail)` Task 1 定义,Task 8 构造一致 ✓
- repo helper 命名一致:`_retrieve_scored`/`_retrieve_neighbors`/`_retrieve_elements`(Task 4/5)→ Task 6 工具箱调用一致 ✓
- `score_knowledge(..., w_keyword, w_semantic)` Task 2 加 → Task 4 透传一致 ✓
- `_answer_reasoning` / `ask_reasoning` Task 9 内自洽,复用 `_answer_context`/`classify_evidence`/`_save_answer` 签名与速查表一致 ✓

**注意事项(执行时):** `sqlite_repository.py` 顶部需确保从 `app.services.retrieval` 导入了 `RetrievedKnowledge, RetrievedElement, W_KEYWORD, W_SEMANTIC, score_elements`(部分已有,缺则补);`_KG_TYPES`、`USABLE_STATUSES`、`_MARKER_RE`、`Evidence`、`AskResponse`、`KnowledgeRecord`、`Citation`、`answer_prompt`、`ANSWER_SCHEMA_HINT`、`classify_evidence` 均已在该文件可用(fast 路径已用)。

---

## Follow-up(不在本计划)
- 前端:折叠展示 `reasoning_trace`(消费 Task 1 的 `AskResponse.reasoning_trace`)
- 前端:fast/reasoning 模式切换开关(发送 `AskRequest.mode`)
- 候选并行检索加速;Plan 前 `overview` 勘探;轻量分类器反哺 fast 的 `is_process_query`
