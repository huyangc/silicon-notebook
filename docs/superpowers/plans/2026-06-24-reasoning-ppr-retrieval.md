# PPR 跨文档检索接入深挖推理(reasoning)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让深挖推理(reasoning)模式获得 HippoRAG 式跨文档检索能力——agent 可主动调用、且每次推理无条件先跑一次兜底——把跨文档原文 chunk 升为可 `[k]` 引用的一等证据,确定性修复对比/跨文档坍缩。

**Architecture:** 复用已落地的与模式无关原语 `_ppr_retrieve`(graph 模式在用,`GRAPH_PPR_ENABLED` 默认开)。在 `ReasoningRetriever` 里加薄封装 + 新 reflect 动作 `ppr_retrieve` + 初检索后一次无条件 seed pass;chunk 累积去重后透传到答案侧,`_answer_reasoning` 按 `_answer_mix` 约定(chunk 段 `k1..N` + KG 推理链段 `k1001+`)组成可引用上下文,仍走 reasoning 客户端;chunk 同步纳入 `classify_evidence` 证据池以正确分档。只动 reasoning 路径,chunk/graph 模式零改。

**Tech Stack:** Python / FastAPI / SQLite;`rustworkx` PPR(已是依赖);pytest。

**Spec:** [docs/superpowers/specs/2026-06-24-reasoning-ppr-retrieval-design.md](../specs/2026-06-24-reasoning-ppr-retrieval-design.md)

**关键约束(来自项目记忆):** 交互用中文;模型仅经 URL 端点;不新增对外 env 开关(复用 `GRAPH_PPR_ENABLED`);守 `[0,1]`/tau;特性分支线性、收尾提 PR(rebase→push→`gh pr create --base master`);commit 末尾署名 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。当前分支已是 `claude/reasoning-ppr-retrieval`(off origin/master)。

---

## 文件结构

**修改:**
- `backend/app/services/reasoning_retrieval.py` — 常量 `_MAX_PPR_RETRIEVES`;`ReflectDecision.ppr_query`;`ReasoningResult.chunks`;`ReasoningRetriever.ppr_retrieve()` 薄封装;`reflect()` 动作白名单 + 解析;`run()` seed pass + 动作分发 + 累积去重 + 熔断;`_summarize()` 含 chunks。
- `backend/app/services/prompts.py` — `REFLECT_SCHEMA_HINT` 加 `ppr_retrieve`/`ppr_query`;`reflect_prompt` 加引导句。
- `backend/app/services/sqlite_repository.py` — `_answer_context` 加 `id_offset`;`_answer_reasoning` 加 `chunks` 参数 + mix 组装;`ask_reasoning` 透传 chunks + 证据池纳入 chunks;模块级 `_ChunkEvHit` namedtuple。

**新建:**
- `backend/tests/test_reasoning_ppr.py` — 本特性全部测试(含复制的 `repo` fixture 与 `_seed_two_doc_moe` 播种助手,沿用各测试文件自带 seed 的房风格)。

---

## Task 1: 数据脚手架(字段 + 薄封装 + 常量)

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(imports `:19-21`;常量近 `:29`;`ReflectDecision` `:49-58`;`ReasoningResult` `:61-65`;新方法近 `:90`)
- Test: `backend/tests/test_reasoning_ppr.py`(新建)

- [ ] **Step 1: 写失败测试(新建测试文件含 fixture + 播种助手)**

创建 `backend/tests/test_reasoning_ppr.py`:

```python
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo):
    """两个源,各一个 MoE 概念节点,经 concept_clusters(canonical_id=K-moe)桥接;
    每节点 evidence 指向本源的 chunk。复刻 test_ppr_retrieve.py 同名助手。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        for sid, title in [("src-A", "DeepSeek paper"), ("src-B", "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (sid, nb.id, title, "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cA", nb.id, "src-A", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps(["elA"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", nb.id, "src-B", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps(["elB"]), now))
        for oid, sid, el in [("e1", "src-A", "elA"), ("e2", "src-B", "elB")]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        for oid in ("e1", "e2"):
            db.execute("INSERT INTO concept_clusters "
                       "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{oid}", nb.id, "K-moe", oid, "Mixture-of-Experts (MoE)", "concept", now))
    return nb


def test_reflect_decision_has_ppr_query():
    from app.services.reasoning_retrieval import ReflectDecision
    assert ReflectDecision().ppr_query == ""


def test_reasoning_result_has_chunks():
    from app.services.reasoning_retrieval import ReasoningResult
    assert ReasoningResult().chunks == []


def test_ppr_retrieve_wrapper_delegates_cross_doc(repo):
    """薄封装委托 repo._ppr_retrieve:问 DeepSeek 的 MoE,经概念簇桥接到 GLM 那篇的 cB。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    rr = ReasoningRetriever(repo, repo.settings)
    chunks = rr.ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts architecture")
    ids = {c.chunk_id for c in chunks}
    assert "cA" in ids and "cB" in ids
    assert all(0.0 <= c.relevance <= 1.0 for c in chunks)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q`
Expected: FAIL —`test_reflect_decision_has_ppr_query` 报 `AttributeError: 'ReflectDecision' object has no attribute 'ppr_query'`;`test_reasoning_result_has_chunks` 报 `chunks`;`test_ppr_retrieve_wrapper_delegates_cross_doc` 报 `AttributeError: 'ReasoningRetriever' object has no attribute 'ppr_retrieve'`。

- [ ] **Step 3: 实现字段 + 常量 + 薄封装**

在 `reasoning_retrieval.py` 顶部 import 加 `RetrievedChunk`(`:19-21`):

```python
from app.services.retrieval import (
    RetrievedChunk, RetrievedElement, RetrievedKnowledge, W_KEYWORD, W_SEMANTIC,
)
```

在 `_PER_QUERY_LIMIT = 8`(`:29`)下方加常量:

```python
# agent 主动 ppr_retrieve 的累计次数上限。写死常量(非 env 开关):reasoning_max_steps=50
# 且每次 ppr_retrieve 都拉到新 chunk=算"有进展"→ stale 熔断不跳,无此上限一次推理可触发
# 多达 50 次全图 PageRank。镜像 search_elements 的 reasoning_max_element_searches。
# 注:run() 初检索后的 seed pass 不计入此上限(它是保证基线、非 agent 动作)。
_MAX_PPR_RETRIEVES = 3
```

`ReflectDecision`(`:57` `elements_query` 下方)加字段:

```python
    elements_query: str = ""
    ppr_query: str = ""
```

`ReasoningResult`(`:65`)加字段:

```python
@dataclass
class ReasoningResult:
    top_hits: List[RetrievedKnowledge] = field(default_factory=list)
    elements: List[RetrievedElement] = field(default_factory=list)
    trace: List[TraceStep] = field(default_factory=list)
    chunks: List[RetrievedChunk] = field(default_factory=list)
```

`search_elements` 薄封装(`:89-90`)下方加:

```python
    def ppr_retrieve(self, notebook_id, query):
        return self.repo._ppr_retrieve(notebook_id, query)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_ppr.py
git commit -m "$(cat <<'EOF'
feat(reasoning): scaffold PPR retrieval (fields, wrapper, cap constant)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: reflect 解析 `ppr_retrieve` 动作 + prompt 引导

**Files:**
- Modify: `backend/app/services/prompts.py`(`REFLECT_SCHEMA_HINT` `:229-234`;`reflect_prompt` `:237-254`)
- Modify: `backend/app/services/reasoning_retrieval.py`(`reflect()` 白名单 `:123`;解析 `:143`)
- Test: `backend/tests/test_reasoning_ppr.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_reasoning_ppr.py`:

```python
def test_reflect_prompt_and_schema_expose_ppr():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    assert "ppr_retrieve" in REFLECT_SCHEMA_HINT
    assert "ppr_query" in REFLECT_SCHEMA_HINT
    p = reflect_prompt("对比 DeepSeek 与 GLM", "- [concept] MoE (id=k1)")
    assert "ppr_retrieve" in p
    # 既有 4 动作不丢
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT


def test_reflect_parses_ppr_retrieve_decision():
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.core.config import Settings

    class _LLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"next_action": "ppr_retrieve",
                               "ppr_query": "DeepSeek vs GLM MoE", "reason": "需跨文档对比"})

    class _Repo:
        def __init__(self): self.reasoning_llm_client = _LLM()

    rr = ReasoningRetriever(_Repo(), Settings(_env_file=None))
    d = rr.reflect("对比题", "候选摘要")
    assert d.next_action == "ppr_retrieve"
    assert d.ppr_query == "DeepSeek vs GLM MoE"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q -k "ppr_retrieve_decision or expose_ppr"`
Expected: FAIL —`expose_ppr` 报 `assert 'ppr_retrieve' in REFLECT_SCHEMA_HINT`;`ppr_retrieve_decision` 因白名单把未知动作降级为 `answer`,断言 `next_action == 'ppr_retrieve'` 失败。

- [ ] **Step 3: 实现 prompt + schema + reflect 解析**

`prompts.py` 的 `REFLECT_SCHEMA_HINT`(`:229-234`)整体替换为:

```python
REFLECT_SCHEMA_HINT = (
    '{"sufficient":false,"next_action":"answer|expand_graph|add_subquery|'
    'search_elements|ppr_retrieve","expand":{"object_id":"","edge_type":null,'
    '"direction":"out|in|both"},"new_sub_query":{"query":"","types":[],'
    '"prefer":"balanced","reason":""},"elements_query":"","ppr_query":"","reason":""}'
)
```

`reflect_prompt`(`:248-249` 的 `search_elements` 描述行后)插入一条动作说明:

```python
        "- search_elements: the KG is too thin; fall back to raw document "
        "passages (set elements_query).\n"
        "- ppr_retrieve: the question compares across models/sources or needs "
        "breadth across documents; pull cross-document source passages via PPR "
        "(set ppr_query). Prefer this for comparison / cross-paper questions where "
        "single-document evidence isn't enough.\n"
```

`reasoning_retrieval.py` 的 `reflect()` 白名单(`:123`)加 `ppr_retrieve`:

```python
            if action not in ("answer", "expand_graph", "add_subquery",
                               "search_elements", "ppr_retrieve"):
                action = "answer"
```

解析 `elements_query` 那行(`:143`)后追加 `ppr_query` 解析:

```python
            d.elements_query = str(data.get("elements_query", "")).strip()
            d.ppr_query = str(data.get("ppr_query", "")).strip()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py tests/test_reasoning_retrieval.py -q`
Expected: PASS（含既有 `test_reflect_prompt_contains_summary_and_schema` 仍绿——它只断言 4 个旧动作存在,不禁止新增）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/prompts.py backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_ppr.py
git commit -m "$(cat <<'EOF'
feat(reasoning): reflect parses ppr_retrieve action + prompt guidance

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `run()` seed pass + chunk 累积 + `_summarize`/no_progress 纳入

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(`_summarize` `:165-172`;`run()` 累积器 `:176-179`、seed pass 近 `:219`、循环 `:232/236/254/320`、返回 `:348`)
- Test: `backend/tests/test_reasoning_ppr.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_reasoning_ppr.py`:

```python
class _AnswerOnlyLLM:
    """plan 出单子查询;reflect 永远 answer(不选 ppr_retrieve)→ 只靠 seed pass。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "都用 MoE [k1].", "grounded": True})


def test_run_seed_pass_populates_cross_doc_chunks_when_flag_on(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    assert repo.settings.graph_ppr_enabled is True   # 默认开
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    ids = {c.chunk_id for c in result.chunks}
    assert "cA" in ids and "cB" in ids               # seed pass 拉到跨文档 chunk
    assert any(s.step_type == "ppr" for s in result.trace)


def test_run_no_seed_when_flag_off(repo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    assert result.chunks == []
    assert not any(s.step_type == "ppr" for s in result.trace)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q -k "seed"`
Expected: FAIL —`test_run_seed_pass_...` 报 `result.chunks` 为空(seed pass 未实现)且无 `ppr` trace 步。

- [ ] **Step 3: 实现 seed pass + 累积 + `_summarize`**

`_summarize`(`:165-172`)整体替换为含 chunks:

```python
    def _summarize(self, collected, elements, chunks):
        lines = []
        for rk in list(collected.values())[:30]:
            name = str(rk.payload.get("name", "")).strip() or rk.object_id
            lines.append(f"- [{rk.object_type}] {name} (id={rk.object_id})")
        for el in elements[:10]:
            lines.append(f"- [element] {el.source_title} · {el.location_label}: {el.text[:80]}")
        for c in chunks[:10]:
            lines.append(f"- [chunk] {c.source_title} · {c.section_path}: {c.text[:80]}")
        return "\n".join(lines) if lines else "(no candidates yet)"
```

`run()` 累积器:在 `elements: List[RetrievedElement] = []`(`:178`)下方加:

```python
        elements: List[RetrievedElement] = []
        chunks: List[RetrievedChunk] = []
        seen_chunks: set = set()
        visited: set = set()
```

seed pass:在初检索 record(`:217-219` 的 `record(TraceStep(step_type="retrieve", ...))`)之后、`used_queries = ...`(`:222`)之前插入:

```python
        # PPR seed pass(确定性兜底):flag 开时无条件先跑一次跨文档 PPR,保证对比/跨文档题
        # 至少有一组跨文档 chunk,不赌 agent 是否选 ppr_retrieve。纯图传播、无 LLM、图已缓存。
        if self.settings.graph_ppr_enabled:
            raise_if_cancelled(self.cancel_event)
            seeded = [c for c in self.ppr_retrieve(notebook_id, question)
                      if c.chunk_id not in seen_chunks]
            for c in seeded:
                seen_chunks.add(c.chunk_id)
            chunks.extend(seeded)
            record(TraceStep(step_type="ppr",
                             summary=f"PPR 跨文档兜底检索,得到 {len(seeded)} 段原文",
                             detail={"found": len(seeded), "phase": "seed"}))
```

`_summarize` 调用点(`:236`)传 chunks:

```python
            summary = self._summarize(collected, elements, chunks)
```

`before`(`:254`)与 `no_progress`(`:320`)纳入 chunks:

```python
            before = len(collected) + len(elements) + len(chunks)
```

```python
            no_progress = (len(collected) + len(elements) + len(chunks)) == before
```

返回(`:348`)带 chunks:

```python
        return ReasoningResult(top_hits=top_hits, elements=elements,
                               trace=trace, chunks=chunks)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q`
Expected: PASS（含 flag-off 用例）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_ppr.py
git commit -m "$(cat <<'EOF'
feat(reasoning): unconditional PPR seed pass + chunk accumulation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `run()` 中 `ppr_retrieve` 动作分发 + 熔断上限

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(计数器近 `:232`;动作分支在 `search_elements` 分支 `:300-316` 之后、`else: break` `:317` 之前)
- Test: `backend/tests/test_reasoning_ppr.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_reasoning_ppr.py`:

```python
class _ScriptedReflectLLM:
    """plan 单子查询;reflect 按 reflects 列表逐轮弹出;其余=answer。"""
    configured = True
    def __init__(self, reflects):
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "ok [k1].", "grounded": True})


def test_ppr_retrieve_action_caps_at_max(repo, monkeypatch):
    """连发 4 次 ppr_retrieve 决策 → 仅前 3 次执行(phase=action),第 4 次写 skip
    (ppr_retrieve_cap)。flag 保持默认开(seed 跑,但 seed 是 phase=seed,按 phase 过滤
    不污染动作计数)。抬高 stale_limit 防『动作 0 新增→stale 早熔断』掩盖 cap。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "reasoning_stale_limit", 99)
    repo._reasoning_llm_client = _ScriptedReflectLLM(
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": f"q{i}"} for i in range(4)]
        + [{"next_action": "answer", "sufficient": True}])
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "对比题")
    ppr_actions = [s for s in result.trace
                   if s.step_type == "ppr" and s.detail.get("phase") == "action"]
    caps = [s for s in result.trace
            if s.step_type == "skip" and s.detail.get("reason") == "ppr_retrieve_cap"]
    assert len(ppr_actions) == 3       # 上限 _MAX_PPR_RETRIEVES=3
    assert len(caps) >= 1              # 第 4 次被 cap


def test_ppr_retrieve_action_skipped_when_flag_off(repo, monkeypatch):
    """flag 关 → 即便 agent 显式选 ppr_retrieve 也被 skip(ppr_disabled),不跑 PPR。
    honors GRAPH_PPR_ENABLED 作为 reasoning 的 PPR 总开关(off=零 PageRank)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    repo._reasoning_llm_client = _ScriptedReflectLLM(
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": "q"},
                  {"next_action": "answer", "sufficient": True}])
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "对比题")
    assert any(s.step_type == "skip" and s.detail.get("reason") == "ppr_disabled"
               for s in result.trace)
    assert result.chunks == []
```

注:`ppr_retrieve` **动作**受 `graph_ppr_enabled` 门控(关→skip `ppr_disabled`),与 seed pass 一致地把总开关当作 reasoning 的 PPR 总开关(off=零 PageRank)。reflect_prompt 仍始终列出该动作(不随 flag 改写——避免把 flag 串进 prompt 签名);off 时 agent 偶尔选到它只是一次 no-op skip,无 PPR 计算。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q -k "action_caps"`
Expected: FAIL —当前 `ppr_retrieve` 动作未实现,reflect 分支落入 `else: break`,无 `phase==action` 的 ppr 步,`len(ppr_actions) == 3` 断言失败。

- [ ] **Step 3: 实现动作分发 + 计数器**

`run()` 中 `elements_searches = 0`(`:232`)下方加计数器:

```python
        elements_searches = 0
        ppr_searches = 0
```

在 `search_elements` 分支末尾(`:316` 的 `record(TraceStep(step_type="fallback", ...))` 之后)、`else:`(`:317`)之前插入新分支:

```python
            elif decision.next_action == "ppr_retrieve":
                if not self.settings.graph_ppr_enabled:
                    record(TraceStep(step_type="skip",
                                     summary="跳过 ppr_retrieve(PPR 未启用)",
                                     detail={"reason": "ppr_disabled"}))
                elif ppr_searches >= _MAX_PPR_RETRIEVES:
                    record(TraceStep(step_type="skip",
                                     summary=f"跳过 ppr_retrieve(已达次数上限 {_MAX_PPR_RETRIEVES})",
                                     detail={"reason": "ppr_retrieve_cap"}))
                else:
                    ppr_searches += 1
                    pq = decision.ppr_query or question
                    new = [c for c in self.ppr_retrieve(notebook_id, pq)
                           if c.chunk_id not in seen_chunks]
                    for c in new:
                        seen_chunks.add(c.chunk_id)
                    chunks.extend(new)
                    record(TraceStep(step_type="ppr",
                                     summary=f"PPR 跨文档检索: {pq},新增 {len(new)} 段",
                                     detail={"query": pq, "found": len(new), "phase": "action"}))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_ppr.py
git commit -m "$(cat <<'EOF'
feat(reasoning): ppr_retrieve reflect action with _MAX_PPR_RETRIEVES cap

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 答案侧 mix —— `_answer_context` offset + `_answer_reasoning` chunks + `ask_reasoning` 透传 + 证据池

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(模块级 namedtuple;`_answer_context` `:5681/5714`;`_answer_reasoning` `:5839-5876`;`ask_reasoning` `:5923/5927/5952/5954/5966`)
- Test: `backend/tests/test_reasoning_ppr.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_reasoning_ppr.py`:

```python
def test_answer_context_id_offset_shifts_keys(repo):
    """加 id_offset 后 KG 键从 k1001 起(与 chunk 段 k1..N 不撞)。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    assert hits
    block, id_map = repo._answer_context(nb.id, hits, id_offset=repo._MIX_KG_KEY_BASE)
    assert all(int(k[1:]) > repo._MIX_KG_KEY_BASE for k in id_map)   # k1001+
    # 默认 offset=0 保持旧行为
    _b0, id0 = repo._answer_context(nb.id, hits)
    assert "k1" in id0


def test_answer_reasoning_mixes_chunks_as_citable(repo):
    """chunks 非空 → 上下文含 chunk 段、id_map 含 chunk 锚(object_type=chunk)、
    答案 [k1] 解析为 chunk 锚。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts")
    assert chunks

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            # 回显含 [k1];k1 落在 chunk 段
            return json.dumps({"answer": "跨文档证据 [k1].", "grounded": True})
    repo._reasoning_llm_client = _Echo()

    answer, grounded, anchors = repo._answer_reasoning(
        nb.id, "对比 MoE", hits, [], chunks=chunks)
    assert anchors and anchors[0].object_type == "chunk"   # k1 = chunk 段一等引用


def test_answer_reasoning_empty_chunks_unchanged(repo):
    """chunks 为空 → 走旧 KG-only 路径,k1 是 KG 锚。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "KG 证据 [k1].", "grounded": True})
    repo._reasoning_llm_client = _Echo()

    answer, grounded, anchors = repo._answer_reasoning(nb.id, "MoE", hits, [], chunks=None)
    assert anchors and anchors[0].object_type == "concept"  # k1 = KG 锚(旧行为)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q -k "id_offset or mixes_chunks or empty_chunks_unchanged"`
Expected: FAIL —`_answer_context()` 不接受 `id_offset`(TypeError);`_answer_reasoning()` 不接受 `chunks`(TypeError)。

- [ ] **Step 3a: `_answer_context` 加 `id_offset`**

签名(`:5681`)与键构造(`:5714`):

```python
    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge],
                        id_offset: int = 0) -> tuple:
```

```python
            i += 1
            key = f"k{i + id_offset}"
```

(其余不变;`:5742` 的 `oid_to_key` 由 `id_map` 反推,自动用 offset 后的键,relations 行一致。)

- [ ] **Step 3b: `_answer_reasoning` 加 `chunks` + mix 组装**

整体替换 `_answer_reasoning`(`:5839-5876`)上下文组装段(签名 + 到 `_refine_context` 之前):

```python
    def _answer_reasoning(
        self,
        notebook_id,
        question,
        top_hits,
        elements,
        history="",
        cancel_event: CancelEvent = None,
        chunks=None,
    ):
        """Synthesise the reasoning-mode answer. When PPR chunks are present they
        become first-class [k]-citable evidence: chunk segment k1..N + KG reasoning
        chain segment k1001+ (mirrors _answer_mix's keying), still via the reasoning
        client. Otherwise KG-only (legacy). search_elements passages stay
        reference-only (no [k] id). Returns (answer, llm_grounded, anchors)."""
        raise_if_cancelled(cancel_event)
        chunks = chunks or []
        if chunks:
            # 按相关度降序(_chunk_answer_context 自带 char 预算,保留最相关);
            # chunk 段 k1..N + KG 段 k1001+,合并 id_map,两段都可 [k] 引用。
            ordered = sorted(chunks, key=lambda c: (-c.relevance, c.chunk_id))
            chunk_block, chunk_id_map = self._chunk_answer_context(ordered)
            kg_block, kg_id_map = self._answer_context(
                notebook_id, top_hits, id_offset=self._MIX_KG_KEY_BASE)
            if kg_block and kg_block != "(none)":
                context_block = f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
            else:
                context_block = chunk_block
            id_map = {**chunk_id_map, **kg_id_map}
        else:
            context_block, id_map = self._answer_context(notebook_id, top_hits)
        if elements:
            extra = "\n".join(
                f"(原文 {i+1}) {el.source_title} · {el.location_label}: {el.text[:200]}"
                for i, el in enumerate(elements[:6])
            )
            context_block = f"{context_block}\n\n补充原文段落(供参考,无引用编号):\n{extra}"
        context_block = self._refine_context(
            question, context_block, self.reasoning_llm_client, cancel_event)
```

(`_refine_context` 之后到 `return` 的部分——chat_json 调用、json 解析、`_parse_answer_anchors(answer, id_map)`——保持 `:5862-5876` 原样不动。)

- [ ] **Step 4a: 跑 `_answer_*` 单元测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q -k "id_offset or mixes_chunks or empty_chunks_unchanged"`
Expected: PASS。

- [ ] **Step 3c: `ask_reasoning` 透传 chunks + 证据池纳入 chunks**

`sqlite_repository.py` 模块顶部(与其它 import/常量同区)加 namedtuple:

```python
from collections import namedtuple
# classify_evidence 只读 .object_id/.relevance;PPR chunk 用 chunk_id 充当 object_id,
# 使跨文档 chunk 引用也能进证据分档(否则纯 chunk 引用的答案会被误判 inferred)。
_ChunkEvHit = namedtuple("_ChunkEvHit", "object_id relevance")
```

(若文件已 import `namedtuple` 则不重复;否则加到现有 `from collections import` 或单列。)

`ask_reasoning` 解包(`:5923`)与异常兜底(`:5927`):

```python
                top_hits, elements, trace, chunks = (
                    result.top_hits, result.elements, result.trace, result.chunks)
```

```python
            except Exception:
                top_hits, elements, trace, chunks = [], [], [], []
```

答案门控(`:5952`)纳入 chunks,调用(`:5954-5956`)传 chunks:

```python
            if self.reasoning_llm_client.configured and (top_hits or elements or chunks):
                try:
                    answer, llm_grounded, anchors = self._answer_reasoning(
                        notebook_id, question, top_hits, elements, history,
                        cancel_event=cancel_event, chunks=chunks)
```

`classify_evidence`(`:5966-5968`)证据池纳入 chunks:

```python
            evidence_pool = list(top_hits) + [
                _ChunkEvHit(c.chunk_id, c.relevance) for c in chunks]
            evidence_level, top_relevance = classify_evidence(
                evidence_pool, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
```

- [ ] **Step 4b: 跑全套 reasoning 测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py tests/test_reasoning_ask.py tests/test_reasoning_retrieval.py tests/test_cross_tier_reasoning.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_reasoning_ppr.py
git commit -m "$(cat <<'EOF'
feat(reasoning): mix PPR chunks as first-class citable evidence in answer

_answer_context gains id_offset; _answer_reasoning mixes chunk segment
(k1..N) with KG chain (k1001+) via the reasoning client; ask_reasoning
threads chunks through and folds them into the classify_evidence pool so
cross-doc chunk citations are graded honestly. Empty chunks => unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 端到端集成 + 隔离 + 不变量 + 全量

**Files:**
- Test: `backend/tests/test_reasoning_ppr.py`

- [ ] **Step 1: 写集成 + 隔离 + 不变量测试**

追加到 `tests/test_reasoning_ppr.py`:

```python
def test_reasoning_ask_seed_cites_cross_doc_end_to_end(repo):
    """端到端:flag 开 + reflect 只 answer(纯靠 seed pass)→ 答案锚点含跨文档 chunk,
    引用覆盖 src-A 与 src-B。"""
    nb = _seed_two_doc_moe(repo)
    repo.llm_client = _AnswerOnlyLLM()
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    resp = repo.ask(nb.id, AskRequest(question="DeepSeek-V3 MoE 相比其他模型", mode="reasoning"))
    assert resp.mode == "reasoning"
    assert any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))
    chunk_anchor_sources = {a.source_title for a in resp.anchors if a.object_type == "chunk"}
    # 答案引用了 [k1] → 至少一个 chunk 锚;跨文档体现在 citations 覆盖两源
    cit_sources = {c.source_id for c in resp.citations}
    assert "src-A" in cit_sources and "src-B" in cit_sources


def test_reasoning_ask_flag_off_no_ppr(repo, monkeypatch):
    """flag 关 → 无 ppr 轨迹,回到今天行为(KG-only)。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    repo.llm_client = _AnswerOnlyLLM()
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    resp = repo.ask(nb.id, AskRequest(question="MoE", mode="reasoning"))
    assert not any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))


def test_chunk_relevance_within_unit_interval(repo):
    """守 [0,1]:reasoning 累积的 chunk relevance 全在单位区间。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "MoE 对比")
    assert all(0.0 <= c.relevance <= 1.0 for c in result.chunks)
```

注:`resp.anchors` 的 chunk 锚 `source_title` 在 `_seed_two_doc_moe` 里 sources.title 为 "DeepSeek paper"/"GLM paper",但 chunk 的 `source_title` 取自 RetrievedChunk(可能为空)——故集成断言以 `resp.citations` 的 `source_id` 覆盖两源为准(`chunk_anchor_sources` 仅作存在性观测,不强断言其内容)。

- [ ] **Step 2: 跑新测试确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py -q`
Expected: PASS（全文件)。若 `test_..._end_to_end` 的 `cit_sources` 不含两源,排查:seed pass 是否在 `ask`(非直接 `run`)路径生效、citations 是否由 chunk 锚回填——必要时改以 `resp.related_knowledge`/`anchors` 的 chunk 锚存在性断言,核心不变量是「答案出现了跨文档 chunk 引用」。

- [ ] **Step 3: 隔离验证(chunk/graph 模式零改)**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py tests/test_graph_src_chunks.py tests/test_ask_redesign.py tests/test_ask_modes.py -q`
Expected: PASS（全绿——本特性未碰 `ask_chunk`/`ask_graph`/共享 `_ppr_retrieve`)。

- [ ] **Step 4: 全量回归**

Run: `cd backend && python -m pytest -q`
Expected: PASS（除环境性 innovus `~/Downloads` 沙箱权限偶发错外,0 failed)。记录通过数(基线约 911 passed + 本特性新增用例)。

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_reasoning_ppr.py
git commit -m "$(cat <<'EOF'
test(reasoning): e2e cross-doc grounding + isolation + [0,1] invariant

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## 收尾:提 PR

全量绿后(记忆 `dev-flow-finish-with-pr` / `pr-merge-is-rebase`):

```bash
cd backend && python -m pytest -q                          # 最终确认
git -C .. fetch origin && git -C .. rebase origin/master   # 保持线性
git -C .. push -u origin claude/reasoning-ppr-retrieval
gh pr create --base master --head claude/reasoning-ppr-retrieval \
  --title "feat(reasoning): HippoRAG PPR cross-doc retrieval in deep-dive mode" \
  --body "见 spec/plan:reasoning 模式接入 PPR(seed 兜底 + agent 动作),跨文档 chunk 升一等引用,守 [0,1]/tau,复用 GRAPH_PPR_ENABLED,chunk/graph 零改。"
```

待用户决定真机:`GRAPH_PPR_ENABLED=true` 重启后端(由用户操作,记忆 `service-restart-prefs`),在 nb-b37185f4ae 对 DeepSeek-V3 对比题验证 reasoning 答案跨多篇引用、对照 NotebookLM。

---

## 自审清单(写计划后已核)

- **Spec 覆盖:** A(动作)→T2;B(seed)→T3;C(累积/去重/熔断)→T3+T4;D(透传)→T5;E(答案 mix)→T5;F(开关复用 + tau 不变量)→T5(证据池)+T6(flag-off/区间)。✓
- **类型一致:** `ppr_query`/`chunks`/`_MAX_PPR_RETRIEVES`/`id_offset`/`_ChunkEvHit` 跨任务命名一致;`RetrievedChunk` 经 `retrieval.py:604` import。✓
- **无占位:** 每步含真实代码 + 确切命令 + 预期。✓
- **风险点:** 集成测试 citations 断言已给排查回退(以 chunk 锚存在性为核心不变量)。
