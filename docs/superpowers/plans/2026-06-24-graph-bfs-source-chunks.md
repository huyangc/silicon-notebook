# graph 模式 PPR 默认开 + BFS 兜底叠原文 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** graph 模式默认走 PPR(原文+跨文档),PPR 召回空/关时回退的 BFS 也把子图 KG 节点的源 chunk 整段喂模型并出 chunk 引用。

**Architecture:** ① `graph_ppr_enabled` 代码默认 False→True。② 在 `ask_graph` 的 BFS 分支(verify 之后)插一段自包含块:子图节点 → `_kg_source_chunks` 取源 chunk → 按 token 预算截(镜像 chunk overlay)→ `_answer_mix`(KG 块 k1001+ / chunk 块 k1..N)→ chunk 引用 → 组 `AskResponse` 直接 return;无源 chunk 落到下方现状 KG-only。复用 `_kg_source_chunks`/`_answer_mix`/`render_subgraph_context`/`truncate_by_tokens`/`_truncate_kg_block`,零新拼装逻辑。

**Tech Stack:** Python 3, SQLite, pydantic Settings, pytest。

**不变量:** 只动 `ask_graph` BFS 分支 + config 默认值;不碰 `ask_chunk`/`ask_reasoning`/`federated_retrieve`/`_answer_mix`/`_kg_source_chunks` 本体;BFS 叠原文永久开、无新 flag;无源 chunk 自动回退现状;chunk 按 token 预算(无数量魔法数字)。

**前置:** 分支 `claude/graph-bfs-source-chunks`(off origin/master 7ef54b3,已含 P2+codex cancel)。测试加到 `backend/tests/test_graph_src_chunks.py`(新建)。cwd=`backend/`,`python3 -m pytest <path> -v`。spec:`docs/superpowers/specs/2026-06-24-graph-bfs-source-chunks-design.md`。

---

## File Structure

- **Modify** `app/core/config.py` — `graph_ppr_enabled` 默认 True。
- **Modify** `app/services/sqlite_repository.py` — `ask_graph` BFS 分支插原文增强块(约 6167 之后)。
- **Create** `tests/test_graph_src_chunks.py` — 新功能测试。
- **Modify** `tests/test_ask_redesign.py` / `tests/test_ask_modes.py` 等 — 默认翻 True 的连带适配(Task 3,按实际失败修)。

---

## Task 1: `graph_ppr_enabled` 默认 True

**Files:** Modify `app/core/config.py`;Test `tests/test_graph_src_chunks.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_graph_src_chunks.py
from app.core.config import Settings


def test_graph_ppr_default_on():
    assert Settings(_env_file=None).graph_ppr_enabled is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_graph_src_chunks.py::test_graph_ppr_default_on -v`
Expected: FAIL(默认仍 False)

- [ ] **Step 3: 改默认值**

`app/core/config.py` 找到 `graph_ppr_enabled: bool = Field(False, env="GRAPH_PPR_ENABLED")`,改为:
```python
    graph_ppr_enabled: bool = Field(True, env="GRAPH_PPR_ENABLED")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_graph_src_chunks.py::test_graph_ppr_default_on -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/config.py tests/test_graph_src_chunks.py
git commit -m "feat(graph): default graph_ppr_enabled to True (PPR primary)"
```

---

## Task 2: BFS 兜底叠源 chunk(自包含块)

**Files:** Modify `app/services/sqlite_repository.py`(`ask_graph` BFS 分支,verify 之后);Test `tests/test_graph_src_chunks.py`

BFS 分支结构(master):`_federated_rx_graph` → `multihop_subgraph` → `render_subgraph_context(id_offset=0)` → `verify_chain_edges`(降权后重渲染,id_offset=0)→ `_refine_context` → `answer_prompt`+`chat_json`。在 **verify 块结束、`_refine_context` 之前**插入下面的块;命中即 return,不命中落到现状 KG-only。

- [ ] **Step 1: 写失败测试(带原文 + 回退)**

```python
# tests/test_graph_src_chunks.py(追加)
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


class _GraphLLM:
    """verify(返回 valid) + _answer_mix(引用首个 chunk k1)两用 stub。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        text = messages[0]["content"]
        if "valid" in (schema_hint or ""):          # verify_chain_edges 的 schema
            return '{"valid": true, "reason": "ok"}'
        return '{"answer": "Mamba 是选择性状态空间模型 [k1].", "grounded": true}'


def _seed_one_node_with_chunk(repo):
    """一个 concept(名含 query 关键词)+ 它 evidence 指向的 chunk。"""
    nb = repo.create_notebook(NotebookCreate(name="g"))
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("src-M", nb.id, "Mamba paper", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cM", nb.id, "src-M", "[2.1 MAMBA] Mamba uses a selective state space mechanism.",
                    "Mamba", json.dumps(["elM"]), now))
        ev = json.dumps([{"source_id": "src-M", "source_title": "", "element_id": "elM",
                          "element_type": "paragraph", "location_label": "p",
                          "quoted_span": "selective state space", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eM", nb.id, "concept", "approved", "", json.dumps({"name": "Mamba"}), ev, "src-M", now, now))
    return nb


def test_bfs_brings_source_chunks(repo, monkeypatch):
    nb = _seed_one_node_with_chunk(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)   # 强制走 BFS
    repo.llm_client = _GraphLLM()
    repo._reasoning_llm_client = _GraphLLM()
    resp = repo.ask_graph(nb.id, AskRequest(question="Mamba 的原理", mode="graph"))
    assert resp.mode == "graph"
    src_ids = {c.source_id for c in resp.citations}
    assert "src-M" in src_ids                       # BFS 答案带上了 chunk 原文引用


def test_bfs_falls_back_when_no_source_chunk(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="g2"))
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        # 节点 evidence 指向不存在的 element(无 chunk 命中)→ _kg_source_chunks 空
        ev = json.dumps([{"source_id": "src-X", "source_title": "", "element_id": "ghost",
                          "element_type": "paragraph", "location_label": "p",
                          "quoted_span": "x", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eX", nb.id, "concept", "approved", "", json.dumps({"name": "Mamba"}), ev, "src-X", now, now))
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    repo.llm_client = _GraphLLM()
    repo._reasoning_llm_client = _GraphLLM()
    resp = repo.ask_graph(nb.id, AskRequest(question="Mamba", mode="graph"))
    assert resp.mode == "graph"
    assert not any(c.source_id for c in resp.citations)   # 回退 KG-only,无 chunk 引用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_graph_src_chunks.py -k bfs -v`
Expected: FAIL — `test_bfs_brings_source_chunks`(无 chunk 引用,增强未接)

- [ ] **Step 3: 插入原文增强块**

在 `ask_graph` 的 BFS 分支里,找到 verify 块结尾这两行:
```python
                    context_block, id_map = render_subgraph_context(subgraph, id_offset=0)

            # Synthesise the answer through the existing LLM + grounding path.
```
在它们之间(即 verify 块之后、`# Synthesise the answer...` 之前)插入:
```python
            # 原文增强:子图 KG 节点的源 chunk 整段也喂模型(复用 chunk overlay 的 mix)。
            # 有源 chunk → 走 _answer_mix(KG 段 k1001+ / chunk 段 k1..N)、出 chunk 引用、直接 return;
            # 无源 chunk → 落到下方现状 KG-only 答案,行为不变。
            from app.services.retrieval import est_tokens, truncate_by_tokens
            src_chunks = self._kg_source_chunks(
                notebook_id, [n["object_id"] for n, _e, _s in subgraph])
            if src_chunks:
                mix_kg_block, mix_id_map = render_subgraph_context(
                    subgraph, id_offset=self._MIX_KG_KEY_BASE)
                mix_kg_block = self._truncate_kg_block(
                    mix_kg_block,
                    self.settings.max_entity_tokens + self.settings.max_relation_tokens)
                chunk_budget = max(0, self.settings.max_total_tokens
                                   - est_tokens(mix_kg_block) - self._MIX_PROMPT_BUFFER_TOKENS)
                src_chunks = truncate_by_tokens(src_chunks, lambda c: c.text, chunk_budget)
                # 源 chunk 的 source_title 补全(供引用标签;_kg_source_chunks 留空)
                with self._connect() as _db:
                    _sids = list({c.source_id for c in src_chunks})
                    _titles = {r["id"]: r["title"] for r in _db.execute(
                        f"SELECT id, title FROM sources WHERE id IN ({','.join('?' for _ in _sids)})",
                        _sids).fetchall()} if _sids else {}
                for c in src_chunks:
                    c.source_title = _titles.get(c.source_id, "")
                answer, llm_grounded, anchors = "", False, []
                if getattr(self.llm_client, "configured", False):
                    try:
                        answer, llm_grounded, anchors = self._answer_mix(
                            question, src_chunks, mix_kg_block, mix_id_map, history,
                            cancel_event=cancel_event)
                    except AskCancelled:
                        raise
                    except Exception as exc:
                        self._note_model_error("answer", self.settings.openai_compat_model, exc)
                        answer, llm_grounded, anchors = "", False, []
                citations: List[Citation] = []
                by_id = {c.chunk_id: c for c in src_chunks}
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        citations.append(Citation(
                            label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200]))
                evidence_level, top_relevance = classify_evidence(
                    src_chunks, anchors, llm_grounded,
                    self.settings.evidence_tau_low, self.settings.evidence_tau_high)
                grounded = evidence_level == "grounded"
                if answer:
                    conclusion = _MARKER_RE.sub("", answer).strip()
                    llm_mode = "grounded" if grounded else "ungrounded"
                else:
                    conclusion = f"Graph retrieved {len(src_chunks)} source passage(s) for this question."
                    llm_mode = "deterministic"
                from app.models.schemas import TraceStep
                resp = AskResponse(
                    answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                    evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                    citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                    retrieval_query=question, top_relevance=top_relevance,
                    reasoning_trace=[TraceStep(step_type="graph_src_chunks",
                        summary=f"BFS 子图 + {len(src_chunks)} 段源原文",
                        detail={"chunks": len(src_chunks),
                                "sources": len({c.source_id for c in src_chunks})})])
                resp.mode = "graph"
                resp.model_errors = [ModelError(**e) for e in _err_sink]
                resp.answer_id = self._save_answer(notebook_id, question, resp, conversation_id)
                return resp
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_graph_src_chunks.py -v`
Expected: PASS（default_on + bfs_brings_source_chunks + bfs_falls_back）

- [ ] **Step 5: 提交**

```bash
git add app/services/sqlite_repository.py tests/test_graph_src_chunks.py
git commit -m "feat(graph): BFS fallback feeds subgraph source chunks to model (token-budgeted, chunk citations)"
```

---

## Task 3: 默认翻 True 的连带适配 + 全量回归

**Files:** Modify 失败的既有 graph 用例(`tests/test_ask_redesign.py` / `tests/test_ask_modes.py` 等);Test 全量

`graph_ppr_enabled` 默认 True 后,seed 了 chunk 的既有 graph 用例会改走 PPR 分支 → 断言失效。逐个修:**要测 BFS/KG 行为的,在该用例里显式 `monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)`**(或 fixture 级);要测新默认 PPR 行为的,更新断言。

- [ ] **Step 1: 跑全量,定位受影响用例**

Run: `python3 -m pytest tests/ -q`
Expected: 记录所有 FAIL/ERROR。忽略 `test_innovus_characterization.py` 的 `~/Downloads` 沙箱权限错(环境无关)。预期受影响:`test_ask_redesign.py`、`test_ask_modes.py` 里走 graph 且 seed 了 chunk 的用例。

- [ ] **Step 2: 逐个修复**

对每个因「默认走 PPR」而失败、但本意是测 BFS/KG-anchor 行为的用例:在其 `ask_graph(..., mode="graph")` 调用前加:
```python
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
```
(若该测试无 `monkeypatch` 参数,加到签名;若用共享 fixture 起 repo,在 fixture 里 setenv `GRAPH_PPR_ENABLED=false` 或 setattr。)
对本意是测「graph 默认行为」的用例:按新 PPR/原文增强行为更新断言(参考 Task 2 的引用/trace 形态)。**不得为了过测把 graph_ppr 关掉来掩盖真实回归**——先判断该用例测的是哪条路径。

- [ ] **Step 3: 全量回归绿**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿(除 innovus 的环境错)。

- [ ] **Step 4: 提交**

```bash
git add tests/
git commit -m "test(graph): adapt existing graph-mode tests to PPR-default (force BFS where BFS is under test)"
```

---

## 收尾

- [ ] 全量 `python3 -m pytest tests/ -q` 绿。
- [ ] rebase 到 origin/master 保持线性 → push → `gh pr create --base master`(PR 写明:graph 默认走 PPR、BFS 兜底叠原文、token 预算无魔法数字、可 `GRAPH_PPR_ENABLED=false` 关 PPR)。
- [ ] 真机(由用户重启)graph 模式问对比题,确认 PPR 与回退 BFS 都带原文。

## Self-Review

- **Spec 覆盖:** ① graph_ppr 默认 True(Task 1)✓;② BFS 叠原文 via `_answer_mix`+token 预算+chunk 引用+无源回退(Task 2)✓;③ 默认翻 True 连带适配(Task 3)✓;键位 k1001+(Task 2 render id_offset=_MIX_KG_KEY_BASE)✓;隔离(只动 BFS 分支/config,不碰共享)✓。
- **占位符:** 无 TBD;每步含完整代码/命令(Task 3 Step 2 依赖实际失败列表,已给确定的修法规则 + 命令)。
- **类型一致:** `_kg_source_chunks(notebook_id, oids)->List[RetrievedChunk]`、`_answer_mix(question, chunks, kg_block, kg_id_map, history, cancel_event)->(answer,grounded,anchors)`、`render_subgraph_context(subgraph, id_offset)->(block,id_map)`、`truncate_by_tokens(items,key,budget)`、`est_tokens`、`_truncate_kg_block`、`_MIX_KG_KEY_BASE=1000`、`_MIX_PROMPT_BUFFER_TOKENS` —— 均与现有签名一致。
