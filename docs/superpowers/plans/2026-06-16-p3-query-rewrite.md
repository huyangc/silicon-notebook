# P3:查询改写/扩展(共享查询理解层)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** chunk 与 reasoning 共用一层"查询理解"(规整 + 中→英改写 + 对比/泛→具体分解)+ 配额融合,根治多实体对比/跨语言/实体写法偏弱。

**Architecture:** 新增 `query_rewrite.py`(`normalize_terms` 纯函数 + `expand_query` LLM 扩展);把 `_quota_rerank` 的分组+轮转抽成通用 `quota_fuse`;`ask_chunk` 改多子查询召回→融合;reasoning `plan` 改建在 `expand_query`(`want_types=True`)。

**Tech Stack:** Python、SQLite、pytest;复用 `_retrieve_chunks`/`score_chunks`/`chat_json`/`search`。

**对应 spec:** `docs/superpowers/specs/2026-06-16-p3-query-rewrite-design.md`

**Python:** `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试 `PYTHONPATH=backend <py> -m pytest`。worktree:`.claude/worktrees/p3-query-rewrite`(分支 `claude/p3-query-rewrite`,off origin/master 含 #44/#45/#46/#48)。

**基线已知失败(忽略,非本工作):** `test_prompts.py::test_extract_prompt_excludes_enumerated_values_and_meta_claims`。验收=不新增失败。

---

## File Structure
- Create: `backend/app/services/query_rewrite.py` — `normalize_terms`、`ExpandedQuery`/`SubQuerySpec`、`expand_query`
- Modify: `backend/app/services/prompts.py` — `expand_query_prompt` + `EXPAND_SCHEMA_HINT`
- Modify: `backend/app/services/retrieval.py` — 新增通用 `quota_fuse`
- Modify: `backend/app/services/reasoning_retrieval.py` — `_quota_rerank` 改用 `quota_fuse`;`plan` 改用 `expand_query`
- Modify: `backend/app/services/sqlite_repository.py` — `ask_chunk` 多子查询接线
- Modify: `backend/app/core/config.py` — `chunk_max_subqueries`、`query_rewrite_enabled`
- Test: `test_query_rewrite.py`(新)、`test_quota_fuse.py`(新)、扩展 `test_chunk_retrieval.py`

---

## Task 1: `normalize_terms` 纯函数

**Files:** Create `backend/app/services/query_rewrite.py`;Test `backend/tests/test_query_rewrite.py`

- [ ] **Step 1: 失败测试** — `backend/tests/test_query_rewrite.py`:
```python
from app.services.query_rewrite import normalize_terms


def test_splits_letter_digit_boundaries():
    assert normalize_terms("gpt4") == "gpt 4"
    assert normalize_terms("v100 gpu") == "v 100 gpu"
    assert normalize_terms("llama3 vs mistral7b") == "llama 3 vs mistral 7b"


def test_leaves_clean_text_untouched():
    assert normalize_terms("deepseek v2 改进") == "deepseek v2 改进"
    assert normalize_terms("") == ""
```

- [ ] **Step 2: 跑测试确认失败** — `PYTHONPATH=backend <py> -m pytest backend/tests/test_query_rewrite.py -x -q` → FAIL(ModuleNotFoundError)

- [ ] **Step 3: 实现** — `backend/app/services/query_rewrite.py`:
```python
"""查询理解层(chunk 与 reasoning 共用):规整 + LLM 改写/分解。"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

# 在字母↔数字边界插空格,让 "gpt4" 这类连写匹配上语料 "GPT-4"→tokens "gpt","4"。
# 注意:无法把 "deepseekv2" 拆成 "deepseek v2"(中间无边界)——那类靠 expand_query 的
# LLM 改写写出规范名(DeepSeek-V2)。此处只做边界明确的廉价补充(也惠及无 LLM 回退)。
_LD = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def normalize_terms(q: str) -> str:
    return _LD.sub(" ", q or "")
```

- [ ] **Step 4: 跑测试确认通过** — `... -m pytest backend/tests/test_query_rewrite.py -q` → 2 passed

- [ ] **Step 5: Commit** — `git add backend/app/services/query_rewrite.py backend/tests/test_query_rewrite.py && git commit -m "feat(p3): normalize_terms 字母↔数字边界规整(纯函数)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

## Task 2: `expand_query` + prompt(LLM 查询扩展)

**Files:** Modify `query_rewrite.py`、`prompts.py`;Test `test_query_rewrite.py`

- [ ] **Step 1: 失败测试** — 追加到 `backend/tests/test_query_rewrite.py`:
```python
from app.services.query_rewrite import expand_query, ExpandedQuery
import json as _json


class _FakeLLM:
    def __init__(self, payload, configured=True, raise_exc=False):
        self.configured = configured; self._p = payload; self._raise = raise_exc
    def chat_json(self, messages, schema_hint, **kw):
        if self._raise: raise RuntimeError("boom")
        return _json.dumps(self._p)


def test_expand_parses_subqueries_and_english():
    llm = _FakeLLM({"query_en": "diff between DeepSeek V3 and V2",
                    "sub_queries": [{"query": "DeepSeek V3 improvements"},
                                    {"query": "DeepSeek V2 architecture"}]})
    ex = expand_query(llm, "deepseekv3相比deepseekv2有什么改进")
    assert isinstance(ex, ExpandedQuery)
    assert ex.query_en == "diff between DeepSeek V3 and V2"
    assert [s.query for s in ex.sub_queries] == ["DeepSeek V3 improvements", "DeepSeek V2 architecture"]


def test_expand_caps_and_dedups_and_drops_empty():
    subs = [{"query": f"q{i}"} for i in range(8)] + [{"query": "q0"}, {"query": "  "}]
    ex = expand_query(_FakeLLM({"query_en": "x", "sub_queries": subs}), "q", max_subqueries=4)
    assert len(ex.sub_queries) == 4 and len({s.query for s in ex.sub_queries}) == 4


def test_expand_want_types_keeps_kg_types():
    llm = _FakeLLM({"query_en": "x", "sub_queries": [
        {"query": "what is MLA", "types": ["concept", "bogus"], "prefer": "semantic"}]})
    ex = expand_query(llm, "MLA 是什么", want_types=True)
    s = ex.sub_queries[0]
    assert s.types == ["concept"] and s.prefer == "semantic"   # 过滤非法 type


def test_expand_fallback_on_unconfigured_exception_empty():
    for llm in (_FakeLLM({}, configured=False), _FakeLLM({}, raise_exc=True),
                _FakeLLM({"sub_queries": []}), _FakeLLM({"query_en": "x", "sub_queries": "nope"})):
        ex = expand_query(llm, "gpt4 对比")
        assert [s.query for s in ex.sub_queries] == ["gpt 4 对比"]   # 回退=normalize_terms(原问)
```

- [ ] **Step 2: 跑测试确认失败** — `... -k expand -x -q` → FAIL(ImportError expand_query)

- [ ] **Step 3: 实现** — 追加到 `query_rewrite.py`(`KG_TYPES`/`PREFER` 与 reasoning 一致):
```python
_KG_TYPES = ("concept", "claim", "formula", "procedure")
_PREFER = ("keyword", "semantic", "balanced")


@dataclass
class SubQuerySpec:
    query: str
    types: List[str] = field(default_factory=list)
    prefer: str = "balanced"
    reason: str = ""


@dataclass
class ExpandedQuery:
    query_en: str
    sub_queries: List[SubQuerySpec]


def expand_query(client, question: str, history: str = "", *,
                 timeout: Optional[float] = None, max_retries: Optional[int] = None,
                 max_subqueries: int = 4, want_types: bool = False) -> ExpandedQuery:
    """一次 LLM 调用:问题(任意语言)→ 英文改写 + 1..max_subqueries 个具体英文子查询。
    want_types=True 时每个子查询附 KG types/prefer(供 reasoning)。
    任何失败/未配置/空 → 回退 [normalize_terms(question)] 单子查询。始终 >=1。"""
    from app.services.prompts import expand_query_prompt, EXPAND_SCHEMA_HINT
    fallback = ExpandedQuery(query_en=question,
                             sub_queries=[SubQuerySpec(query=normalize_terms(question))])
    if not getattr(client, "configured", False):
        return fallback
    kw = {}
    if timeout is not None: kw["timeout"] = timeout
    if max_retries is not None: kw["max_retries"] = max_retries
    try:
        raw = client.chat_json(
            [{"role": "user", "content": expand_query_prompt(question, history, want_types)}],
            EXPAND_SCHEMA_HINT, **kw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return fallback
        subs_raw = data.get("sub_queries")
        if not isinstance(subs_raw, list) or not subs_raw:
            return fallback
        out: List[SubQuerySpec] = []
        seen = set()
        for s in subs_raw:
            if not isinstance(s, dict):
                continue
            q = normalize_terms(str(s.get("query", "")).strip())
            if not q or q in seen:
                continue
            seen.add(q)
            types, prefer = [], "balanced"
            if want_types:
                tr = s.get("types")
                types = [t for t in (tr if isinstance(tr, list) else []) if t in _KG_TYPES]
                prefer = s.get("prefer") if s.get("prefer") in _PREFER else "balanced"
            out.append(SubQuerySpec(query=q, types=types, prefer=prefer,
                                    reason=str(s.get("reason", ""))))
            if len(out) >= max_subqueries:
                break
        if not out:
            return fallback
        query_en = str(data.get("query_en", "")).strip() or question
        return ExpandedQuery(query_en=query_en, sub_queries=out)
    except Exception:
        return fallback
```
`backend/app/services/prompts.py` 末尾追加:
```python
EXPAND_SCHEMA_HINT = '{"query_en":"","sub_queries":[{"query":"","types":[],"prefer":"balanced","reason":""}]}'


def expand_query_prompt(question: str, history_block: str = "", want_types: bool = False) -> str:
    history_section = (
        "Prior conversation (resolve pronouns/ellipsis against it):\n"
        f"{history_block}\n\n" if history_block else "")
    types_line = (
        "- types: which KG node types to search (subset of concept/claim/formula/"
        "procedure; omit/empty = all). prefer: keyword|semantic|balanced.\n"
        if want_types else "")
    types_schema = ',"types":[],"prefer":"balanced"' if want_types else ""
    return (
        "You prepare an engineer's question for retrieval over an ENGLISH document "
        "corpus. Produce:\n"
        "1. query_en: the question rewritten in clear English (translate if needed; "
        "spell entity/version names canonically, e.g. 'deepseekv2' -> 'DeepSeek-V2').\n"
        "2. sub_queries: 1-4 focused, standalone ENGLISH search queries that together "
        "cover the question. For a COMPARISON, emit ONE sub-query per entity (e.g. "
        "'DeepSeek-V2 architecture and features', 'DeepSeek-V3 improvements'). For a "
        "BROAD/overview question, emit one per distinct dimension. For a simple "
        "single-topic question, ONE sub-query is fine. Use canonical entity names.\n"
        f"{types_line}"
        "Keep sub-queries non-redundant.\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        'Return JSON only: {"query_en":"","sub_queries":[{"query":""' + types_schema + "}]}"
    )
```

- [ ] **Step 4: 跑测试确认通过** — `... -m pytest backend/tests/test_query_rewrite.py -q` → 6 passed

- [ ] **Step 5: Commit** — `feat(p3): expand_query + expand_query_prompt(中→英改写+对比/泛→具体分解)`

---

## Task 3: 抽通用 `quota_fuse`,reasoning `_quota_rerank` 改薄封装

**Files:** Modify `retrieval.py`、`reasoning_retrieval.py`;Test `backend/tests/test_quota_fuse.py`

- [ ] **Step 1: 失败测试** — `backend/tests/test_quota_fuse.py`:
```python
from app.services.retrieval import quota_fuse
from dataclasses import dataclass


@dataclass
class _H:
    object_id: str
    relevance: float


def test_round_robin_balances_across_subqueries():
    a1, a2, b1 = _H("a1", .9), _H("a2", .8), _H("b1", .7)
    collected = {h.object_id: h for h in (a1, a2, b1)}
    per_q = [{"a1": a1, "a2": a2}, {"b1": b1}]      # 子查询A 命中 a1/a2;子查询B 命中 b1
    res, counts = quota_fuse(collected, per_q, top_n=2)
    assert {h.object_id for h in res} == {"a1", "b1"}   # 各组轮流取队首,B 的 b1 不被 A 通吃挤掉
    assert counts == [1, 1, 0]                          # [A, B, 兜底]


def test_fallback_group_when_unscored():
    x = _H("x", 0.0)
    res, counts = quota_fuse({"x": x}, [{}, {}], top_n=5)
    assert [h.object_id for h in res] == ["x"] and counts == [0, 0, 1]
```

- [ ] **Step 2: 跑测试确认失败** — `... -m pytest backend/tests/test_quota_fuse.py -x -q` → FAIL(ImportError quota_fuse)

- [ ] **Step 3: 实现** — `backend/app/services/retrieval.py` 末尾追加(把 reasoning `_quota_rerank` 步骤 2-4 泛化;`relevance` 可配,默认读 `.relevance`):
```python
def quota_fuse(collected, per_query, top_n, relevance=lambda h: h.relevance):
    """复合查询配额 round-robin。collected: {id: item}; per_query: List[{id: scored_item}]
    (第 i 个=第 i 个子查询的命中,scored_item 须有 relevance)。每个候选归到 relevance
    最高的子查询组,组内降序,跨组轮流取队首;全未命中归兜底组最后轮转。
    返回 (result, counts): counts[i]=第 i 子查询贡献数, counts[-1]=兜底组。"""
    groups = [[] for _ in per_query]
    fallback = []
    for oid, item in collected.items():
        best_i, best_h = -1, None
        for i, scored in enumerate(per_query):
            h = scored.get(oid)
            if h is not None and (best_h is None or relevance(h) > relevance(best_h)):
                best_i, best_h = i, h
        if best_i >= 0:
            groups[best_i].append(best_h)
        else:
            fallback.append(item)
    for g in groups:
        g.sort(key=relevance, reverse=True)
    queues = groups + [fallback]
    idx = [0] * len(queues)
    result, seen, sources = [], set(), []
    while len(result) < top_n:
        progressed = False
        for qi in range(len(queues)):
            if len(result) >= top_n:
                break
            while idx[qi] < len(queues[qi]):
                h = queues[qi][idx[qi]]; idx[qi] += 1
                oid = getattr(h, "object_id", None) or getattr(h, "chunk_id", None) or id(h)
                if oid not in seen:
                    seen.add(oid); result.append(h); sources.append(qi); progressed = True
                    break
        if not progressed:
            break
    counts = [sources.count(i) for i in range(len(queues))]
    return result, counts
```
然后 `reasoning_retrieval.py` 的 `_quota_rerank` 步骤 2-4 替换为调用 `quota_fuse`(步骤 1 的 per-query 重打分保留):
```python
    def _quota_rerank(self, notebook_id, collected, used_queries, top_n):
        from app.services.retrieval import quota_fuse
        per_q = []
        for q in used_queries:
            try:
                per_q.append({h.object_id: h for h in self.search(notebook_id, q)})
            except Exception:
                per_q.append({})
        return quota_fuse(collected, per_q, top_n)
```

- [ ] **Step 4: 跑测试确认通过** — `... -m pytest backend/tests/test_quota_fuse.py backend/tests/test_reasoning_retrieval.py -q` → quota_fuse 2 passed **且 reasoning 测试全过**(行为等价,counts 语义不变)

- [ ] **Step 5: Commit** — `refactor(p3): 抽通用 quota_fuse,reasoning _quota_rerank 改薄封装`

---

## Task 4: `ask_chunk` 多子查询接线 + config

**Files:** Modify `sqlite_repository.py`(`ask_chunk` ~4155、新增 `_retrieve_chunks_multi`)、`config.py`;Test 扩展 `test_chunk_retrieval.py`

- [ ] **Step 1: 失败测试** — 追加到 `backend/tests/test_chunk_retrieval.py`(复用其 `repo` fixture、`_seed_chunks`、`_FakeLLM`):
```python
def test_ask_chunk_comparison_balances_both_entities(repo, monkeypatch):
    # 种两实体 chunk;假 expand 出 2 子查询;断言两实体都进 selected
    nb, _ = _seed_chunks(repo, ["DeepSeek-V2 uses MLA attention " * 20,
                                "DeepSeek-V2 dense baseline " * 20,
                                "DeepSeek-V3 MoE 671B improvements " * 20,
                                "DeepSeek-V3 MTP training " * 20])
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query_en="V3 vs V2", sub_queries=[qr.SubQuerySpec("DeepSeek-V3 improvements"),
                                          qr.SubQuerySpec("DeepSeek-V2 features")]))
    repo.llm_client = _FakeLLM("V3 improves on V2 [k1][k2].")
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseekv3相比deepseekv2有什么改进"))
    srcs = " ".join((a.snippet or "") + (a.name or "") for a in resp.anchors).lower()
    cites = " ".join(c.quoted_span.lower() for c in resp.citations)
    assert "v2" in (srcs + cites) and "v3" in (srcs + cites)   # 两实体都被代表


def test_ask_chunk_single_subquery_still_works(repo, monkeypatch):
    nb, _ = _seed_chunks(repo, ["alpha topic " * 30, "beta topic " * 30])
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query_en="alpha", sub_queries=[qr.SubQuerySpec("alpha topic")]))
    resp = repo.ask_chunk(nb.id, AskRequest(question="alpha"))
    assert resp.citations   # 单子查询走 MMR,正常返回
```

- [ ] **Step 2: 跑测试确认失败** — `... -k "comparison_balances or single_subquery" -x -q` → FAIL(expand 未接线 / 单查询行为)

- [ ] **Step 3: 实现** —
`config.py` 加:
```python
    chunk_max_subqueries: int = Field(4, env="CHUNK_MAX_SUBQUERIES")
    query_rewrite_enabled: bool = Field(True, env="QUERY_REWRITE_ENABLED")
```
`sqlite_repository.py` 新增多子查询召回 + 改 `ask_chunk` 的检索段。新增方法(放 `_retrieve_chunks` ~4084 附近):
```python
    def _retrieve_chunks_multi(self, notebook_id, sub_queries):
        """对每个子查询并发跑 _retrieve_chunks;返回 (collected{chunk_id:best}, per_query, ids, mat)。
        ids/mat 取首个非空子查询的矩阵(同 notebook 矩阵一致,用于后续 MMR 兜底)。"""
        from concurrent.futures import ThreadPoolExecutor
        def _one(q):
            try: return self._retrieve_chunks(notebook_id, q)
            except Exception: return ([], [], None)
        results = []
        if sub_queries:
            with ThreadPoolExecutor(max_workers=min(len(sub_queries), 8)) as ex:
                results = list(ex.map(_one, sub_queries))
        per_query, collected, ids, mat = [], {}, [], None
        for scored, qids, qmat in results:
            per_query.append({c.chunk_id: c for c in scored})
            for c in scored:
                cur = collected.get(c.chunk_id)
                if cur is None or c.relevance > cur.relevance:
                    collected[c.chunk_id] = c
            if mat is None and len(qids):
                ids, mat = qids, qmat
        return collected, per_query, ids, mat
```
`ask_chunk` 把 `retrieval_query` → `_retrieve_chunks` → `_mmr_select_chunks` 那段(~4174-4183)替换为:
```python
        _t = time.perf_counter()
        from app.services.query_rewrite import expand_query
        from app.services.retrieval import quota_fuse
        if self.settings.query_rewrite_enabled:
            ex = expand_query(self.llm_client, retrieval_query,
                              max_subqueries=self.settings.chunk_max_subqueries)
            sub_queries = [s.query for s in ex.sub_queries]
        else:
            sub_queries = [retrieval_query]
        ask_stage("expand_query", _t, n=len(sub_queries))

        _t = time.perf_counter()
        if len(sub_queries) >= 2:
            collected, per_query, _ids, _mat = self._retrieve_chunks_multi(notebook_id, sub_queries)
            selected, _counts = quota_fuse(collected, per_query, self.settings.chunk_mmr_k,
                                           relevance=lambda c: c.relevance)
            ask_stage("retrieve_fuse", _t, recall=len(collected), selected=len(selected))
        else:
            scored, ids, mat = self._retrieve_chunks(notebook_id, sub_queries[0])
            selected = self._mmr_select_chunks(scored, ids, mat,
                                               self.settings.chunk_mmr_k, self.settings.chunk_mmr_lambda)
            ask_stage("retrieve_mmr", _t, recall=len(scored), selected=len(selected))
```
(其余 ask_chunk 不变:citations / `_answer_chunks` / classify / AskResponse。)

- [ ] **Step 4: 跑测试确认通过** — `... -m pytest backend/tests/test_chunk_retrieval.py -q` → 全 passed

- [ ] **Step 5: Commit** — `feat(p3): ask_chunk 多子查询召回 + quota_fuse 平衡(默认 expand)`

---

## Task 5: reasoning `plan` 改建在 `expand_query` 上 + 384K 检查

**Files:** Modify `reasoning_retrieval.py`(`plan` ~91);Test `test_reasoning_retrieval.py`

- [ ] **Step 1: 失败测试** — 追加到 `backend/tests/test_reasoning_retrieval.py`:
```python
def test_plan_uses_expand_query(rrepo, monkeypatch):
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query_en="x", sub_queries=[qr.SubQuerySpec("sub A", types=["concept"]),
                                   qr.SubQuerySpec("sub B")]))
    from app.services.reasoning_retrieval import ReasoningRetriever
    r = ReasoningRetriever(rrepo, rrepo.settings)
    subs = r.plan("中文复合问题")
    assert [s.query for s in subs] == ["sub A", "sub B"] and subs[0].types == ["concept"]
```

- [ ] **Step 2: 跑测试确认失败** — `... -k test_plan_uses_expand_query -x -q` → FAIL(plan 仍用 plan_prompt 直出)

- [ ] **Step 3: 实现** — `plan` 改为(用 `expand_query(want_types=True)` + 各模式自己的 reasoning_llm_client;映射 `SubQuerySpec`→`SubQuery`):
```python
    def plan(self, question, history=""):
        from app.services.query_rewrite import expand_query
        fallback = [SubQuery(query=question)]
        ex = expand_query(self.repo.reasoning_llm_client, question, history,
                          timeout=self.settings.reasoning_timeout_seconds,
                          max_retries=self.settings.reasoning_max_retries,
                          max_subqueries=self.settings.reasoning_max_subqueries,
                          want_types=True)
        out = [SubQuery(query=s.query, types=s.types, prefer=s.prefer, reason=s.reason)
               for s in ex.sub_queries]
        return out or fallback
```
(`expand_query` 内部已含未配置/异常回退;`SubQuery`/`KG_TYPES`/`PREFER_WEIGHTS` 校验已在 `expand_query(want_types=True)` 完成。删除 `plan` 原 plan_prompt 解析体;`plan_prompt`/`PLAN_SCHEMA_HINT` 暂留不删以防他用,后续清理。)
**384K 检查(顺带):** 在本 task 确认 reasoning 路径未把上下文 budget 硬编码到远小于模型窗口;如有,提为 `reasoning_context_max_chars` 配置(默认足够大)。若无硬上限则只在 spec/commit 注明"已确认无小上限",不改代码。

- [ ] **Step 4: 跑测试确认通过** — `... -m pytest backend/tests/test_reasoning_retrieval.py -q` → 全 passed(含新测;现有推理测试不破)

- [ ] **Step 5: Commit** — `feat(p3): reasoning plan 改建在共享 expand_query(中→英/分解惠及推理)`

---

## Task 6: 全量验证 + 真机对照 + PR

- [ ] **Step 1: 全量** — `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh 2>&1 | tail -6`;`... -m pytest backend/tests/ -q`。验收:相对基线无新增失败(仅既有 `test_prompts` 一条)。

- [ ] **Step 2: PR** — `git push -u origin claude/p3-query-rewrite`;`gh pr create --base master --title "feat(retrieval): P3 查询改写/扩展(共享查询理解层)" --body "..."`。(gh pr merge 可能打印 "master 已被工作区使用" 的本地无害报错,远端仍成功。)

- [ ] **Step 3: 真机对照运行手册(交用户)** — 需用户重启后端(`scripts/backend.sh restart`,先 `git pull` 拿 P3 + #48)。三基准 + 跨语言:
```bash
for Q in "deepseekv3相比deepseekv2有什么改进" "review一下当前材料里llm架构的演进" "<一个中文问题,材料是英文>"; do
  curl -s -X POST localhost:8000/api/notebooks/<nb>/ask -H 'content-type: application/json' -d "{\"question\":\"$Q\"}" | python -m json.tool; done
```
判据:对比题 **V2、V3 都被引用**且稳定 grounded;中文问英文材料召回明显改善;综述覆盖更全。

---

## 自检验证
- 纯函数(normalize_terms/quota_fuse/expand_query 解析)离线单测;ask_chunk/reasoning 用 hermetic fixture。
- 关键不变量:reasoning 现有测试在 Task3/Task5 后仍全过(quota_fuse 等价、plan 输出 shape 不变)。
- 真机对照需用户重启(我不启停服务)。
