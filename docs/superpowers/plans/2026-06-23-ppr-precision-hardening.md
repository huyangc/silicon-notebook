# PPR 精度硬化(graph 模式)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 graph 模式 PPR 检索加种子侧 specificity 权重(抑制大众概念霸权)和可选 LLM fact-rerank(过滤无关种子),提升精度。

**Architecture:** 仅改 `_ppr_retrieve`(全仓库唯一调用点是 `ask_graph`,在 `graph_ppr_enabled` 内)。先把 reset 向量构造抽成 `_ppr_reset_vector(notebook_id, question, key_to_idx) -> Dict[int,float]`(行为不变,便于白盒单测),再在其中加 specificity(`÷ 实体出现chunk数`)与 fact-rerank(LLM 后置过滤候选种子,fail-open)。**绝不改共享的 `federated_retrieve`/`_retrieve_chunks`**,故 chunk(通用问答)与 reasoning 模式零影响。

**Tech Stack:** Python 3, SQLite, rustworkx, pydantic Settings, pytest。

**不变量:** ① 隔离:只动 `_ppr_retrieve`/`_ppr_reset_vector` + 新增只读 helper,不碰 `ask_chunk`/`ask_reasoning`/`federated_retrieve`/`_retrieve_chunks`。② specificity 默认开、fact-rerank 默认关。③ fact-rerank fail-open:LLM 未配/报错/非法返回 → 不过滤,沿用全部候选。④ 守 [0,1]/tau(下游 run_ppr 归一不变)。

**前置:** P1(PR #63)已在 master。基分支 `claude/ppr-precision-p2`(off origin/master)。测试均加到现有 `backend/tests/test_ppr_retrieve.py`(已有 `repo` fixture、`_seed_two_doc_moe`、`NotebookCreate`/`json`/`pytest` 导入)。测试命令 `python3 -m pytest <path> -v`,cwd=`backend/`。

---

## File Structure

- **Modify** `app/core/config.py` — 2 个 flag。
- **Modify** `app/services/sqlite_repository.py` — 抽 `_ppr_reset_vector`;加 specificity;加 `_ppr_fact_rerank` + 常量。
- **Modify** `tests/test_ppr_retrieve.py` — 新增单测。

---

## Task 1: 配置开关

**Files:** Modify `app/core/config.py`(紧邻 P1 的 `ppr_chunk_seed_top_n` 之后);Test `tests/test_ppr_retrieve.py`

- [ ] **Step 1: 写失败测试**

```python
def test_ppr_precision_flag_defaults():
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.ppr_specificity_enabled is True       # 默认开(把 PPR 做对)
    assert s.ppr_fact_rerank_enabled is False       # 默认关(每查一次 LLM,opt-in)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_ppr_retrieve.py::test_ppr_precision_flag_defaults -v`
Expected: FAIL — `AttributeError: ... 'ppr_specificity_enabled'`

- [ ] **Step 3: 加字段**

在 `app/core/config.py` 的 `ppr_chunk_seed_top_n: int = Field(30, env="PPR_CHUNK_SEED_TOP_N")` 之后插入:
```python
ppr_specificity_enabled: bool = Field(True, env="PPR_SPECIFICITY_ENABLED")   # 种子 ÷ 实体出现chunk数
ppr_fact_rerank_enabled: bool = Field(False, env="PPR_FACT_RERANK_ENABLED")  # LLM 过滤候选种子(每查一次 LLM)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_ppr_retrieve.py::test_ppr_precision_flag_defaults -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/config.py tests/test_ppr_retrieve.py
git commit -m "feat(ppr): add specificity + fact-rerank config flags"
```

---

## Task 2: 抽取 `_ppr_reset_vector`(行为不变重构)

**Files:** Modify `app/services/sqlite_repository.py`(`_ppr_retrieve`,约 4723-4766);Test `tests/test_ppr_retrieve.py`

把 reset 向量构造从 `_ppr_retrieve` 抽到独立方法,行为完全不变(现有 PPR 用例须仍绿),便于后续单测 specificity / fact-rerank。

- [ ] **Step 1: 写失败测试**

```python
def test_ppr_reset_vector_seeds_entities_and_chunks(repo):
    nb = _seed_two_doc_moe(repo)
    G, key_to_idx, chunk_idx_to_id = repo._ppr_graph(nb.id)
    reset = repo._ppr_reset_vector(nb.id, "Mixture-of-Experts (MoE)", key_to_idx)
    assert isinstance(reset, dict) and reset
    assert all(w > 0 for w in reset.values())
    # 至少命中一个实体种子(e1/e2)与一个 chunk 种子(chunk:cA / chunk:cB)
    ent_idxs = {key_to_idx["e1"], key_to_idx["e2"]}
    chunk_idxs = {key_to_idx["chunk:cA"], key_to_idx["chunk:cB"]}
    assert ent_idxs & set(reset)
    assert chunk_idxs & set(reset)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_ppr_retrieve.py::test_ppr_reset_vector_seeds_entities_and_chunks -v`
Expected: FAIL — `AttributeError: ... '_ppr_reset_vector'`

- [ ] **Step 3: 抽取方法**

在 `app/services/sqlite_repository.py` 中,把 `_ppr_retrieve` 里**从 `reset: Dict[int, float] = {}` 到 `if not reset: return []` 之前**这段(即 4732-4743 三段:初始化 + KG 种子循环 + chunk 种子循环)替换为一次调用:
```python
        reset = self._ppr_reset_vector(notebook_id, question, key_to_idx)
        if not reset:
            return []
```
并在 `_ppr_retrieve` 方法**之后**新增:
```python
    def _ppr_reset_vector(self, notebook_id: str, question: str,
                          key_to_idx: Dict[str, int]) -> Dict[int, float]:
        """构造 PPR 的 reset/personalization 向量:KG 实体种子(federated_retrieve)
        + chunk 种子(dense)。返回 {vertex_idx: weight}。仅 graph 模式 PPR 路径调用。"""
        reset: Dict[int, float] = {}
        kg_hits = self.federated_retrieve(notebook_id, question)[: self.settings.ppr_kg_seed_top_n]
        for h in kg_hits:
            idx = key_to_idx.get(h.object_id)
            if idx is not None and h.relevance > 0:
                reset[idx] = reset.get(idx, 0.0) + float(h.relevance)
        scored, _ids, _mat = self._retrieve_chunks(notebook_id, question)
        pw = self.settings.ppr_passage_node_weight
        for c in scored[: self.settings.ppr_chunk_seed_top_n]:
            idx = key_to_idx.get(f"chunk:{c.chunk_id}")
            if idx is not None and c.relevance > 0:
                reset[idx] = reset.get(idx, 0.0) + float(c.relevance) * pw
        return reset
```
确认 `_ppr_retrieve` 现在长这样(reset 段已被替换):
```python
        G, key_to_idx, chunk_idx_to_id = self._ppr_graph(notebook_id)
        if G.num_nodes() == 0 or not chunk_idx_to_id:
            return []

        reset = self._ppr_reset_vector(notebook_id, question, key_to_idx)
        if not reset:
            return []

        ranked = run_ppr(G, chunk_idx_to_id, reset, damping=self.settings.ppr_damping)
        ...（其余不变）
```

- [ ] **Step 4: 跑测试确认通过(新测 + 旧 PPR 回归)**

Run: `python3 -m pytest tests/test_ppr_retrieve.py -v`
Expected: PASS（新 `test_ppr_reset_vector_*` + 所有 P1 PPR 用例,行为不变）

- [ ] **Step 5: 提交**

```bash
git add app/services/sqlite_repository.py tests/test_ppr_retrieve.py
git commit -m "refactor(ppr): extract _ppr_reset_vector (behavior-preserving)"
```

---

## Task 3: Specificity 权重

**Files:** Modify `app/services/sqlite_repository.py`(`_ppr_reset_vector` 的 KG 种子段);Test `tests/test_ppr_retrieve.py`

- [ ] **Step 1: 写失败测试(白盒:比较开/关时的权重比)**

```python
def _seed_hub_vs_rare(repo):
    """eH 出现在 3 个 chunk,e1 出现在 1 个;二者都含 query 关键词 'Attention'。"""
    nb = repo.create_notebook(NotebookCreate(name="hub"))
    with repo._write() as db:
        now = "2026-06-23T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("src-H", nb.id, "paper", "md", "ready", now, now))
        for cid, el in [("h1", "eh1"), ("h2", "eh2"), ("h3", "eh3"), ("r1", "er1")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (cid, nb.id, "src-H", "Attention mechanism.", "S", json.dumps([el]), now))
        def _ev(els):
            return json.dumps([{"source_id": "src-H", "source_title": "", "element_id": e,
                                "element_type": "paragraph", "location_label": "p",
                                "quoted_span": "Attention", "confidence": 1.0} for e in els])
        # eH 横跨 3 chunk;e1 仅 1 chunk
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eH", nb.id, "concept", "approved", "", json.dumps({"name": "Attention"}),
                    _ev(["eh1", "eh2", "eh3"]), "src-H", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("e1", nb.id, "concept", "approved", "", json.dumps({"name": "Attention"}),
                    _ev(["er1"]), "src-H", now, now))
        # 给两实体各建单簇,确保进入 _ppr_graph
        for oid in ("eH", "e1"):
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"cl-{oid}", nb.id, f"K-{oid}", oid, "Attention", "concept", now))
    return nb


def test_specificity_divides_hub_entity_by_chunk_count(repo, monkeypatch):
    nb = _seed_hub_vs_rare(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    q = "Attention"
    monkeypatch.setattr(repo.settings, "ppr_specificity_enabled", False)
    off = repo._ppr_reset_vector(nb.id, q, key_to_idx)
    monkeypatch.setattr(repo.settings, "ppr_specificity_enabled", True)
    on = repo._ppr_reset_vector(nb.id, q, key_to_idx)
    iH, i1 = key_to_idx["eH"], key_to_idx["e1"]
    # eH 出现在 3 chunk → on 权重 = off / 3;e1 出现在 1 chunk → 不变
    assert on[iH] == off[iH] / 3
    assert on[i1] == off[i1]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_ppr_retrieve.py::test_specificity_divides_hub_entity_by_chunk_count -v`
Expected: FAIL — `on[iH]` 仍等于 `off[iH]`(specificity 未实现)

- [ ] **Step 3: 在 `_ppr_reset_vector` 加 specificity**

把 `_ppr_reset_vector` 的 KG 种子段改为(顶部按 flag 预取 ent_chunk_map,循环里除以 chunk 数):
```python
        reset: Dict[int, float] = {}
        ent_chunk_map = (self._ent_chunk_map(notebook_id)
                         if self.settings.ppr_specificity_enabled else {})
        kg_hits = self.federated_retrieve(notebook_id, question)[: self.settings.ppr_kg_seed_top_n]
        for h in kg_hits:
            idx = key_to_idx.get(h.object_id)
            if idx is not None and h.relevance > 0:
                w = float(h.relevance)
                if self.settings.ppr_specificity_enabled:
                    # 大众概念(出现在很多 chunk)降权,避免 Transformer/KV cache 灌满 PPR。
                    w /= max(1, len(ent_chunk_map.get(h.object_id) or ()))
                reset[idx] = reset.get(idx, 0.0) + w
```
(chunk 种子段不变。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_ppr_retrieve.py -k "specificity or reset_vector or ppr_retrieve" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/sqlite_repository.py tests/test_ppr_retrieve.py
git commit -m "feat(ppr): specificity weighting — down-weight hub-concept seeds by chunk count"
```

---

## Task 4: LLM fact-rerank(recognition memory,fail-open)

**Files:** Modify `app/services/sqlite_repository.py`(新增 `_ppr_fact_rerank` + 常量;在 `_ppr_reset_vector` 接入);Test `tests/test_ppr_retrieve.py`

- [ ] **Step 1: 写失败测试(过滤 + fail-open)**

```python
class _FilterLLM:
    """recognition-memory stub:只保留名字含 'keep' 的候选。"""
    configured = True
    def __init__(self): self.calls = 0
    def chat_json(self, messages, schema_hint, **kw):
        self.calls += 1
        import re as _re
        text = messages[0]["content"]
        keep = _re.findall(r'(\w+)\s+—\s+[^\n]*keep', text)  # id — name(含 keep)
        return '{"relevant_ids": ' + json.dumps(keep) + '}'


def _seed_relevant_irrelevant(repo):
    nb = repo.create_notebook(NotebookCreate(name="rr"))
    with repo._write() as db:
        now = "2026-06-23T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("src-R", nb.id, "p", "md", "ready", now, now))
        for cid, el in [("ck", "elk"), ("cd", "eld")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "src-R", "topic", "S", json.dumps([el]), now))
        def _ev(e): return json.dumps([{"source_id": "src-R", "source_title": "", "element_id": e,
                                        "element_type": "paragraph", "location_label": "p",
                                        "quoted_span": "topic", "confidence": 1.0}])
        # ekeep 名字含 'keep';edrop 名字含 'drop'。两者都让 federated_retrieve 命中(同含 'topic')
        for oid, nm, el in [("ekeep", "topic keep", "elk"), ("edrop", "topic drop", "eld")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": nm}), _ev(el), "src-R", now, now))
        for oid in ("ekeep", "edrop"):
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"cl-{oid}", nb.id, f"K-{oid}", oid, "topic", "concept", now))
    return nb


def test_fact_rerank_filters_irrelevant_seed(repo, monkeypatch):
    nb = _seed_relevant_irrelevant(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    monkeypatch.setattr(repo.settings, "ppr_fact_rerank_enabled", True)
    repo._reasoning_llm_client = _FilterLLM()
    reset = repo._ppr_reset_vector(nb.id, "topic", key_to_idx)
    # edrop 被 LLM 过滤 → 其实体种子不进 reset;ekeep 保留
    assert key_to_idx["ekeep"] in reset
    assert key_to_idx["edrop"] not in reset


def test_fact_rerank_fail_open_when_no_llm(repo, monkeypatch):
    nb = _seed_relevant_irrelevant(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    monkeypatch.setattr(repo.settings, "ppr_fact_rerank_enabled", True)
    class _Down: configured = False
    repo._reasoning_llm_client = _Down()
    reset = repo._ppr_reset_vector(nb.id, "topic", key_to_idx)
    # LLM 未配 → fail-open,两个实体种子都在
    assert key_to_idx["ekeep"] in reset
    assert key_to_idx["edrop"] in reset
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_ppr_retrieve.py -k fact_rerank -v`
Expected: FAIL — `test_fact_rerank_filters_irrelevant_seed`(edrop 仍在 reset,rerank 未接)

- [ ] **Step 3: 加 `_ppr_fact_rerank` 并接入**

在 `_ppr_reset_vector` **之后**新增方法 + 在文件里 `_ppr_reset_vector` 上方(类作用域)定义常量。先在 `_ppr_reset_vector` 内接入(在 `kg_hits = ...` 之后、循环之前):
```python
        kg_hits = self.federated_retrieve(notebook_id, question)[: self.settings.ppr_kg_seed_top_n]
        if self.settings.ppr_fact_rerank_enabled:
            kg_hits = self._ppr_fact_rerank(question, kg_hits)
```
新增方法:
```python
    _PPR_RERANK_SCHEMA = '{"relevant_ids": ["..."]}'

    def _ppr_fact_rerank(self, question: str, kg_hits: list) -> list:
        """Recognition memory:LLM 过滤候选 KG 种子,只留与 question 相关的。
        fail-open:LLM 未配/报错/非法返回/过滤后为空 → 原样返回 kg_hits(绝不因
        rerank 失败而清空种子)。复用 reasoning_llm_client。"""
        client = self.reasoning_llm_client
        if not kg_hits or not getattr(client, "configured", False):
            return kg_hits
        lines = []
        for h in kg_hits:
            name = str(h.payload.get("name", "")).strip()
            snippet = h.evidence[0].quoted_span[:80] if h.evidence else ""
            lines.append(f"{h.object_id} — {name} — {snippet}")
        prompt = (
            "You are filtering knowledge-graph entries for relevance to a user question "
            "(recognition memory). Keep an entry only if it could help answer the question; "
            "when unsure, KEEP it.\n\n"
            f"Question: {question}\n\nCandidates (id — name — snippet):\n"
            + "\n".join(lines)
            + '\n\nReturn JSON only: {"relevant_ids": [ids to keep]}.'
        )
        try:
            raw = client.chat_json(
                [{"role": "user", "content": prompt}], self._PPR_RERANK_SCHEMA,
                timeout=self.settings.reasoning_timeout_seconds, max_retries=1)
            data = json.loads(raw)
            ids = data.get("relevant_ids") if isinstance(data, dict) else None
            if not isinstance(ids, list):
                return kg_hits
            keep = {str(i) for i in ids}
            kept = [h for h in kg_hits if h.object_id in keep]
            return kept or kg_hits   # 过滤后为空 → fail-open(LLM 过度过滤)
        except Exception as exc:
            self._note_model_error(
                "ppr_fact_rerank",
                self.settings.reasoning_llm_model or self.settings.openai_compat_model, exc)
            return kg_hits
```
(`reasoning_llm_client` 是属性,会回退到 `llm_client`;`reasoning_timeout_seconds`/`reasoning_llm_model` 已存在于 settings,见 ask_reasoning 用法。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_ppr_retrieve.py -k fact_rerank -v`
Expected: PASS（filters + fail_open 两测）

- [ ] **Step 5: 提交**

```bash
git add app/services/sqlite_repository.py tests/test_ppr_retrieve.py
git commit -m "feat(ppr): LLM fact-rerank (recognition memory) seed filter, fail-open"
```

---

## Task 5: 隔离回归 + 全量验证

**Files:** Test `tests/test_ppr_retrieve.py`

- [ ] **Step 1: 写隔离测试**

```python
def test_precision_changes_do_not_touch_chunk_or_reasoning(repo, monkeypatch):
    """specificity/fact-rerank 默认值下,chunk(通用问答)与 reasoning 不调 PPR。
    这里仅断言开关默认值 + ask_chunk 不引用 PPR 路径(防回归护栏)。"""
    s = repo.settings
    assert s.ppr_specificity_enabled is True and s.ppr_fact_rerank_enabled is False
    import inspect
    from app.services.sqlite_repository import SQLiteRepository
    src = inspect.getsource(SQLiteRepository.ask_chunk)
    assert "_ppr_retrieve" not in src and "_ppr_reset_vector" not in src
    rsrc = inspect.getsource(SQLiteRepository.ask_reasoning)
    assert "_ppr_retrieve" not in rsrc and "_ppr_reset_vector" not in rsrc
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python3 -m pytest tests/test_ppr_retrieve.py::test_precision_changes_do_not_touch_chunk_or_reasoning -v`
Expected: PASS

- [ ] **Step 3: 全量回归**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿,无新失败(对照 P1 后的基线;两开关默认 specificity=True/fact_rerank=False)。

- [ ] **Step 4: 提交**

```bash
git add tests/test_ppr_retrieve.py
git commit -m "test(ppr): isolation guard — precision changes don't touch chunk/reasoning"
```

---

## 收尾

- [ ] 全量 `python3 -m pytest tests/ -q` 绿。
- [ ] rebase 到 origin/master 保持线性 → push → `gh pr create --base master`(PR 写明:graph 模式 only;specificity 默认开/fact-rerank 默认关;真机对照待 PPR 整体开后验证)。
- [ ] 真机(由用户决定重启):`PPR_FACT_RERANK_ENABLED=true` 时观察 events 里 `ppr_fact_rerank` model_error 是否为空、对比答案精度。

## Self-Review

- **Spec 覆盖:** specificity(Task 3)✓;fact-rerank 节点级+fail-open+reasoning_llm_client(Task 4)✓;隔离不变量(Task 5 + 全程不碰共享方法)✓;两 flag 默认值(Task 1)✓;Q1「不加大簇降权」= 本计划不含,符合 spec ✓。
- **占位符:** 无 TBD/TODO,每步含完整代码与命令。
- **类型一致:** `_ppr_reset_vector(notebook_id, question, key_to_idx) -> Dict[int,float]` 在 Task 2 定义,Task 3/4 一致修改;`_ppr_fact_rerank(question, kg_hits) -> list` Task 4 定义并在 `_ppr_reset_vector` 调用,签名一致。
