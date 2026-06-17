# KG-answer 增强接进 reasoning/graph 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 逐任务实施。每任务 TDD(红→绿→commit)。

**Goal:** 把退役 ask_fast 后成生产死代码的三个 KG-answer 增强——query-refine(答案侧)、RRF、rerank(检索侧)——接进 reasoning/graph,恢复其作用。query-refine 默认开;RRF/rerank 默认关 opt-in。

**Architecture:** 抽共享 `_refine_context(question, context_block, client)` 接进 `_answer_reasoning`+`ask_graph` 答案;`_retrieve_scored` 加 `retrieval_rrf_enabled` 分支调现有 `_rrf_scored`(reasoning/graph 经 `federated_retrieve` 全享);最终候选阶段调 `_rerank_hits`(reasoning `run()` 末 top_hits + graph 种子)。删与 `_answer_reasoning` 同构的死方法 `_answer_kg`。

**Tech Stack:** Python/FastAPI/SQLite、pytest。基于已批准设计(本会话)。前提:#55 已并 master(`94989ab`)。

> 行号取自 worktree `worktree-kg-answer-enhance`(off 94989ab)快照,实施时复核。守不变量:`_rrf_scored` 已是修过版(score=RRF 排序、relevance 保 [0,1] 守 tau、dual-index best-of);refine 只前置参考文本不碰 id_map/`[k]`;rerank 只重排不改 relevance。

## 文件结构
- **改** `backend/app/services/sqlite_repository.py` — `_refine_context`(新);`_answer_reasoning`/`ask_graph` 接 refine;`_retrieve_scored` 加 RRF 分支;`ask_graph` 种子接 rerank;删 `_answer_kg`。
- **改** `backend/app/services/reasoning_retrieval.py` — `run()` 末 top_hits 接 rerank。
- **改** `backend/tests/test_query_refine.py`、`backend/tests/test_reasoning_retrieval.py` — `_answer_kg` 删后改测活路径。
- **建/改** 测试覆盖 RRF 分支、rerank 接线。

---

## Task 1:query-refine → reasoning/graph(+ 删死方法 _answer_kg)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(新 `_refine_context`;`_answer_reasoning` ~L4640、`ask_graph` 答案 ~L4846 接入;删 `_answer_kg` ~L4591-4638)
- Modify: `backend/tests/test_query_refine.py`、`backend/tests/test_reasoning_retrieval.py`(retarget `_answer_kg`→活路径)

- [ ] **Step 1: 写失败测试** — 在 `backend/tests/test_query_refine.py` 加(或改)一条直接测共享 helper 的:

```python
def test_refine_context_prepends_focused_evidence(repo):
    # 假 LLM 返回 relevant 列表 → 前置 "Focused relevant evidence";不动原 context
    class _FakeRefineLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            import json as _j
            return _j.dumps({"relevant": ["Engram separates storage from computation"]})
    out = repo._refine_context("What is Engram?", "k1: [concept][personal] Engram",
                               _FakeRefineLLM())
    assert out.startswith("Focused relevant evidence")
    assert "k1: [concept][personal] Engram" in out          # 原 context 保留在后

def test_refine_context_noop_when_disabled(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_query_refine_enabled", False)
    cb = "k1: [concept][personal] Engram"
    class _C: configured = True
    assert repo._refine_context("q", cb, _C()) == cb         # 关 → 原样返回

def test_refine_context_noop_when_client_unconfigured(repo):
    cb = "k1: x"
    class _C: configured = False
    assert repo._refine_context("q", cb, _C()) == cb
```

(`repo` fixture 见现有 test_query_refine.py 顶部;若它依赖 `_answer_kg` 的旧用例,本任务一并 retarget——见 Step 5。)

- [ ] **Step 2: 跑红** — `cd backend && python -m pytest tests/test_query_refine.py -q -k refine_context` → FAIL(`_refine_context` 不存在)。

- [ ] **Step 3: 实现 helper**(放 `_answer_reasoning` 上方):

```python
    def _refine_context(self, question: str, context_block: str, client) -> str:
        """问题感知证据精炼:把 context_block 喂给 evidence_refine LLM,抽"相关要点"
        前置成聚焦上下文(参考性,不产生 [k] 锚点)。默认开(kg_query_refine_enabled);
        client 未配/失败/无内容 → 原样返回。reasoning 传 reasoning_llm_client、graph
        传 llm_client。"""
        if not (self.settings.kg_query_refine_enabled
                and getattr(client, "configured", False)
                and context_block.strip() and context_block.strip() != "(none)"):
            return context_block
        from app.services.prompts import evidence_refine_prompt, EVIDENCE_REFINE_SCHEMA_HINT
        ev_block = context_block[: self.settings.query_refine_max_chars]
        try:
            raw = client.chat_json(
                [{"role": "user", "content": evidence_refine_prompt(question, ev_block)}],
                EVIDENCE_REFINE_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries)
            rel = json.loads(raw).get("relevant")
            if not isinstance(rel, list):
                rel = []
            rel = [str(x).strip() for x in rel if str(x).strip()]
        except Exception:
            rel = []
        if rel:
            context_block = ("Focused relevant evidence (for this question):\n"
                             + "\n".join(f"- {x}" for x in rel[:12])
                             + "\n\n" + context_block)
        return context_block
```

- [ ] **Step 4: 接入两条活路径**
  - `_answer_reasoning`:在 `context_block = f"{context_block}\n\n补充原文段落..."`(elements 追加,~L4651)**之后**、`raw = self.reasoning_llm_client.chat_json(...)` **之前**插:
    ```python
        context_block = self._refine_context(question, context_block, self.reasoning_llm_client)
    ```
  - `ask_graph`:在最终 `context_block, id_map = render_subgraph_context(...)`(含 verify 后重渲,~L4840)**之后**、`raw = self.llm_client.chat_json([{... answer_prompt(question, context_block, history)}])`(~L4846)**之前**插:
    ```python
            context_block = self._refine_context(question, context_block, self.llm_client)
    ```

- [ ] **Step 5: 删死方法 `_answer_kg` + retarget 其测试**
  - 先 `grep -rn "_answer_kg" backend/` 确认仅测试调用(无生产调用——`ask_reasoning` 用 `_answer_reasoning`、`ask_graph` 内联)。删 `_answer_kg`(~L4591-4638)。
  - `test_query_refine.py` / `test_reasoning_retrieval.py` 里调用 `_answer_kg` 的用例:改为测 `_answer_reasoning`(活路径,签名 `(notebook_id, question, top_hits, elements, history)`,内部已含 refine)或测 `_refine_context`/`_answer_context`。**保住原断言意图**(精炼生效、grounded、[k] 锚点);把这些用例迁过去,别只删。

- [ ] **Step 6: 跑绿 + 回归** — `cd backend && python -m pytest tests/test_query_refine.py tests/test_reasoning_retrieval.py -q`,再 `python -m pytest -q`(全量,确认删 `_answer_kg` 无连带破)。

- [ ] **Step 7: commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_query_refine.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(kg-answer): query-refine 接进 reasoning/graph 答案(默认开)+ 删死方法 _answer_kg"
```

---

## Task 2:RRF 接进 reasoning/graph 检索(`_retrieve_scored` 分支)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_retrieve_scored` ~L4024)
- Test: `backend/tests/test_bm25_rrf.py`(加分支测试)

- [ ] **Step 1: 写失败测试**(append 到 test_bm25_rrf.py;复用其 repo fixture + 种 KG):

```python
def test_retrieve_scored_uses_rrf_when_enabled(repo, monkeypatch):
    # 种几个对象 + 向量(见本文件现有 setup);开 RRF → _retrieve_scored 走 _rrf_scored
    monkeypatch.setattr(repo.settings, "retrieval_rrf_enabled", True)
    called = {}
    orig = repo._rrf_scored
    def _spy(query, kg_objs, knowledge_sims, element_sims=None):
        called["hit"] = True
        return orig(query, kg_objs, knowledge_sims, element_sims)
    monkeypatch.setattr(repo, "_rrf_scored", _spy)
    hits = repo._retrieve_scored(<notebook_id>, "<query>")
    assert called.get("hit") is True
    assert all(0.0 <= h.relevance <= 1.0 for h in hits)   # 守 [0,1] 不变量
```
> `<notebook_id>`/`<query>`/种数据用本文件现有模式填(实现期按 test_bm25_rrf 现有 fixture 写实)。

- [ ] **Step 2: 跑红** — RRF 关时 `_rrf_scored` 不被调 → FAIL。

- [ ] **Step 3: 实现** — 在 `_retrieve_scored` 算完 `element_sims`/`knowledge_sims`(~L4042)**之后**、`score_knowledge` 循环**之前**插分支:
```python
        if self.settings.retrieval_rrf_enabled:
            return self._rrf_scored(query, kg_objs, knowledge_sims, element_sims)
```
(`_rrf_scored` 自带排序与 [0,1] relevance;`kg_objs` 已按 `type_list` 限定 → 类型过滤保留;RRF 不用 w_keyword/w_semantic 的 prefer 偏置,opt-in 可接受。)

- [ ] **Step 4: 跑绿 + 回归** — `python -m pytest tests/test_bm25_rrf.py tests/test_trackA_rrf_relevance.py -q`,再全量 `-q`(默认关,行为不变)。

- [ ] **Step 5: commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_bm25_rrf.py
git commit -m "feat(kg-answer): _retrieve_scored 加 RRF 分支(retrieval_rrf_enabled,默认关)→ reasoning/graph 经 federated_retrieve 共享"
```

---

## Task 3:rerank 接进 reasoning/graph 最终候选

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(`run()` ~L307-318 末 top_hits)
- Modify: `backend/app/services/sqlite_repository.py`(`ask_graph` 种子 top_hits)
- Test: `backend/tests/test_rerank.py`(加接线测试)

- [ ] **Step 1: 写失败测试**(append test_rerank.py;开 rerank + 假 LLM 给逆序打分,断言 reasoning 最终 top_hits 顺序随 rerank 变):

```python
def test_reasoning_run_applies_rerank_when_enabled(repo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    monkeypatch.setattr(repo.settings, "rerank_enabled", True)
    # 种 ≥2 个会命中的对象;假 llm_client 让 _rerank_hits 把顺序反过来
    # (见 test_rerank.py 现有假 LLM 模式)。
    rr = ReasoningRetriever(repo, repo.settings)
    result = rr.run(<notebook_id>, "<query>")
    # 断言:top_hits 的顺序 == rerank 指定的顺序(而非纯 relevance 序)
    assert [h.object_id for h in result.top_hits] == <expected_reranked_order>
```
> 用 test_rerank.py 现有假 LLM/种数据模式填实;若整 run() 太重,退而测 `ask_graph` 种子或直接断言 `run()` 调了 `_rerank_hits`(spy)。契约:rerank 开时最终候选经 `_rerank_hits`;关时不变。

- [ ] **Step 2: 跑红**

- [ ] **Step 3: 实现**
  - `reasoning_retrieval.py` `run()`:在算出最终 `top_hits`(quota 与 global 两分支汇合后,~L318)、`record(...answer...)` 之前插:
    ```python
        top_hits = self.repo._rerank_hits(question, top_hits)   # no-op when rerank_enabled off
    ```
  - `sqlite_repository.py` `ask_graph`:在种子 `top_hits = self.federated_retrieve(notebook_id, question)[:...]` **之后**、`use_seeds = ...` **之前**插:
    ```python
        top_hits = self._rerank_hits(question, top_hits)
    ```

- [ ] **Step 4: 跑绿 + 回归** — `python -m pytest tests/test_rerank.py tests/test_reasoning_retrieval.py -q`,再全量 `-q`(默认关,行为不变)。

- [ ] **Step 5: commit**
```bash
git add backend/app/services/reasoning_retrieval.py backend/app/services/sqlite_repository.py backend/tests/test_rerank.py
git commit -m "feat(kg-answer): rerank 接进 reasoning 最终候选 + graph 种子(rerank_enabled,默认关)"
```

---

## Task 4:全量验证 + PR

- [ ] **Step 1: 全量** — `cd backend && python -m pytest -q` 全绿;`cd .. && bash scripts/check.sh`,EXIT=0。
- [ ] **Step 2: 死代码复核** — `grep -rn "_answer_kg" backend/` 应为空(已删);`_rrf_scored`/`_rerank_hits` 现有生产调用者(`_retrieve_scored`/reasoning·graph)。
- [ ] **Step 3: PR**(按 [[dev-flow-finish-with-pr]]):
```bash
git fetch origin && git merge --no-edit origin/master
cd backend && python -m pytest -q
git push -u origin worktree-kg-answer-enhance
gh pr create --base master --title "feat(kg-answer): query-refine/RRF/rerank 接进 reasoning/graph" \
  --body "退役 ask_fast 后这三个 KG-answer 增强成生产死代码;本 PR 接进活路径。query-refine 默认开(恢复生效),RRF/rerank 默认关 opt-in。删同构死方法 _answer_kg。守 [0,1]/tau 与 dual-index best-of 不变量。"
```
- [ ] **Step 4:** 更新 memory(chunk-native-retrieval-state:三增强已接 reasoning/graph,query-refine 恢复生效)。

## 备注
- `_answer_kg` 删除前必 grep 确认无生产调用;其测试迁到 `_answer_reasoning`/`_refine_context`,保住断言意图。
- RRF/rerank 默认关:本 PR 不改默认行为,只恢复"可开"。开启增益由用户后续 eval 定(ref-kg memory:RRF 曾伤 L2 多跳、query-refine 单开最优)。
- 测试占位 `<...>` 实现期按对应测试文件现有 fixture 写实。
