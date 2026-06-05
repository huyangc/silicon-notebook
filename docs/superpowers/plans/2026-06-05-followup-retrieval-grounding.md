# 追问检索 + grounding 重定义（一期）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 innovus 这类多轮深入追问时，检索能命中正确内容（指代消解 + 流程类召回 procedure），并把 grounded 从「LLM 自报」改为「相关度感知三档」，前端如实标注 有据/概述/推断。

**Architecture:** 在 `SqliteRepository.ask()` 入口处，先取历史、对「像追问」的问题做轻量 LLM query 改写（门控触发）；检索排序按问题意图条件化类型权重，并为流程类问题保底召回 procedure；回答后按命中相关度把证据分三档写入 `AskResponse.evidence_level`，前端渲染徽章。纯函数（追问识别 / 意图识别 / 类型权重 / 配额 / 证据分档）抽到 `followup.py` 与 `retrieval.py` 便于单测。

**Tech Stack:** Python 3 / FastAPI / SQLite，pydantic-settings；前端 Next.js（`frontend/app/page.tsx` + `globals.css`）。LLM 经 OpenAI 兼容 URL（`llm_client.chat_json`）。

**Spec:** `docs/superpowers/specs/2026-06-05-followup-retrieval-grounding-design.md`

**约定**（每个任务的命令都用这两个）：
- Python 解释器：`PY=/opt/homebrew/Caskroom/miniconda/base/bin/python`（不存在时回退 `python3`）。
- 后端测试：`cd backend && PYTHONPATH=. $PY -m pytest tests/<file> -q`。

> 注：一期**不做效果/质量回归**（按用户要求，效果在一期+二期全部完成后统一看）。本计划的"通过判据"是**正确性**：单测绿 + `scripts/check.sh` 绿 + `cd frontend && npm run build` 通过 + 既有测试不回归。`source_elements` 段落兜底（C）与 flow 重抽取归二期。

---

## 文件结构（创建 / 修改）

- **Create** `backend/app/services/followup.py` — 追问识别纯函数 `looks_like_followup()` + 指代标记集合。单一职责、无重依赖。
- **Modify** `backend/app/services/retrieval.py` — 新增 `is_process_query()`、`_PROCESS_TYPE_WEIGHT`/`type_weight()`、`ensure_procedure_quota()`、`classify_evidence()`。与既有打分常量同处。
- **Modify** `backend/app/services/prompts.py` — 新增 `FOLLOWUP_REWRITE_SCHEMA_HINT` + `followup_rewrite_prompt()`。
- **Modify** `backend/app/core/config.py` — 新增 4 个 env 旋钮。
- **Modify** `backend/app/models/schemas.py` — `AskResponse` 增 `evidence_level`/`retrieval_query`/`top_relevance`。
- **Modify** `backend/app/services/sqlite_repository.py` — `ask()` 接入改写/意图排序/配额/证据分档/落字段；`_answer_kg()` 改为返回 LLM 原始 grounded。
- **Modify** `frontend/app/page.tsx` — `AskResponse` 类型加 `evidence_level`；答案卡片改三档徽章。
- **Modify** `frontend/app/globals.css` — 加 `.answer-grounded` / `.answer-overview` 两个徽章样式。
- **Create** `backend/tests/test_followup_retrieval_grounding.py` — 纯函数单测 + `ask()` 接入集成测。

---

## Task 1: 配置旋钮

**Files:**
- Modify: `backend/app/core/config.py:62`（在 `retrieval_top_n` 之后）
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

新建 `backend/tests/test_followup_retrieval_grounding.py`，内容：

```python
import json, re, pytest


def test_settings_have_followup_and_evidence_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.followup_max_len == 12
    assert s.evidence_tau_low == 0.18
    assert s.evidence_tau_high == 0.35
    assert s.proc_min == 2

    monkeypatch.setenv("EVIDENCE_TAU_HIGH", "0.5")
    assert Settings().evidence_tau_high == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_settings_have_followup_and_evidence_knobs -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'followup_max_len'`.

- [ ] **Step 3: Add the settings**

在 `backend/app/core/config.py` 的 `retrieval_top_n: int = Field(12, env="RETRIEVAL_TOP_N")` 一行之后插入：

```python
    # 追问改写：问题长度 ≤ 此值（或含指代标记）才触发轻量 LLM 改写。
    followup_max_len: int = Field(12, env="FOLLOWUP_MAX_LEN")
    # grounded 三档阈值（作用于融合相关度 .relevance ∈[0,1]）。
    # 注意：现有 grounded 测试要求 tau_high ≤ 0.4（纯关键词命中融合分=0.4）。
    evidence_tau_low: float = Field(0.18, env="EVIDENCE_TAU_LOW")
    evidence_tau_high: float = Field(0.35, env="EVIDENCE_TAU_HIGH")
    # 流程类问题 top-N 至少保底召回的 procedure 条数。
    proc_min: int = Field(2, env="PROC_MIN")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_settings_have_followup_and_evidence_knobs -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(config): 追问改写/证据分档旋钮"
```

---

## Task 2: 追问识别 `looks_like_followup`

**Files:**
- Create: `backend/app/services/followup.py`
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

追加到 `backend/tests/test_followup_retrieval_grounding.py`：

```python
def test_looks_like_followup():
    from app.services.followup import looks_like_followup
    # 含指代标记 → 追问
    assert looks_like_followup("把这个流程按阶段画成流程图", 12) is True
    assert looks_like_followup("展开讲讲这个流程", 12) is True
    assert looks_like_followup("draw that flow as stages please now", 12) is True
    # 短问题 → 追问（marker-less 兜底）
    assert looks_like_followup("那 ECO 呢", 12) is True
    # 长且无指代的独立问题 → 非追问
    assert looks_like_followup("innovus中有哪些常见flow", 12) is False
    assert looks_like_followup("innovus是什么工具", 12) is False
    # 空 → False
    assert looks_like_followup("", 12) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_looks_like_followup -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.followup'`.

- [ ] **Step 3: Create the module**

`backend/app/services/followup.py`：

```python
"""Follow-up question detection for multi-turn ask().

A pure heuristic gate: only when a question "looks like a follow-up" do we pay
for an LLM query rewrite (coreference resolution). Kept dependency-free and
side-effect-free so it is trivially unit-testable.
"""

from __future__ import annotations

import re

# CJK anaphora / continuation markers. Substring match is fine for CJK.
_ANAPHORA_MARKERS = (
    "这个", "那个", "这些", "那些", "这一", "那一", "这种", "这样",
    "这块", "这部分", "这章", "这节", "上面", "上述", "前面", "刚才",
    "继续", "接着", "它", "该", "此",
)
# English anaphora markers, matched on word tokens (not substrings).
_EN_MARKERS = {"it", "this", "that", "these", "those", "above", "former", "latter"}


def looks_like_followup(question: str, max_len: int) -> bool:
    """True when `question` is likely an elliptical follow-up that needs the
    conversation history to be understood (short, or carrying an anaphor)."""
    q = (question or "").strip()
    if not q:
        return False
    if len(q) <= max_len:
        return True
    if any(m in q for m in _ANAPHORA_MARKERS):
        return True
    tokens = set(re.findall(r"[a-z]+", q.lower()))
    return bool(tokens & _EN_MARKERS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_looks_like_followup -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/followup.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(followup): 追问识别启发式 looks_like_followup"
```

---

## Task 3: 意图识别 + 意图条件化类型权重

**Files:**
- Modify: `backend/app/services/retrieval.py:85`（紧接 `_TYPE_WEIGHT` 定义之后）
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

追加：

```python
def test_is_process_query_and_type_weight():
    from app.services.retrieval import is_process_query, type_weight
    assert is_process_query("展开讲讲RTL到GDSII的流程") is True
    assert is_process_query("把这个流程按阶段画成流程图") is True
    assert is_process_query("what are the place and route steps") is True
    assert is_process_query("innovus是什么工具") is False
    # 非流程意图：保持现有权重（procedure 被压低）
    assert type_weight("procedure", False) == 0.7
    assert type_weight("claim", False) == 1.0
    # 流程意图：procedure 不再被惩罚、略占优
    assert type_weight("procedure", True) == 1.0
    assert type_weight("claim", True) == 0.9
    assert type_weight("concept", True) == 0.6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_is_process_query_and_type_weight -q`
Expected: FAIL — `ImportError: cannot import name 'is_process_query'`.

- [ ] **Step 3: Add the helpers**

在 `backend/app/services/retrieval.py` 的 `_TYPE_WEIGHT = { ... }` 块之后（约第 85 行后）插入：

```python
# Process/flow-intent overrides: a "what are the steps / 展开流程" question wants
# procedures surfaced, not buried. Used INSTEAD of _TYPE_WEIGHT for such queries.
_PROCESS_TYPE_WEIGHT = {
    "procedure": 1.0,
    "claim": 0.9,
    "formula": 0.9,
    "concept": 0.6,
}

# Substring markers signalling the user wants a process/flow/steps answer.
_PROCESS_MARKERS = (
    "流程", "步骤", "怎么", "如何", "展开", "阶段", "画成", "过程", "顺序", "先后",
    "flow", "step", "procedure", "process", "pipeline", "stage", "walkthrough",
)


def is_process_query(text: str) -> bool:
    """True when the question is about a process/flow/steps (intent signal)."""
    t = (text or "").lower()
    return any(m in t for m in _PROCESS_MARKERS)


def type_weight(object_type: str, process_intent: bool) -> float:
    """Cross-type authority weight; process-intent questions stop penalising
    procedures (and slightly favour them)."""
    table = _PROCESS_TYPE_WEIGHT if process_intent else _TYPE_WEIGHT
    return table.get(object_type, 0.5)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_is_process_query_and_type_weight -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(retrieval): 流程意图识别+意图条件化类型权重"
```

---

## Task 4: 流程类问题的 procedure 配额 `ensure_procedure_quota`

**Files:**
- Modify: `backend/app/services/retrieval.py`（接 Task 3 之后）
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

追加：

```python
def _rk(oid, otype, score):
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=oid, object_type=otype, payload={},
                              score=score, relevance=score)


def test_ensure_procedure_quota_backfills_and_preserves_order():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    # top-N=3 全是 claim；后面有两条 procedure；min_proc=2
    scored = [
        _rk("c1", "claim", 0.9), _rk("c2", "claim", 0.8), _rk("c3", "claim", 0.7),
        _rk("p1", "procedure", 0.6), _rk("p2", "procedure", 0.5), _rk("c4", "claim", 0.1),
    ]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    types = [h.object_type for h in out]
    assert types.count("procedure") == 2          # 配额满足
    assert len(out) == 3                            # 不超 top_n
    assert out[0].object_id == "c1"                # 未驱逐最强命中
    assert [key(h) for h in out] == sorted((key(h) for h in out), reverse=True)  # 降序

def test_ensure_procedure_quota_noop_when_enough():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    scored = [_rk("p1", "procedure", 0.9), _rk("p2", "procedure", 0.8), _rk("c1", "claim", 0.7)]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    assert [h.object_id for h in out] == ["p1", "p2", "c1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py -k ensure_procedure_quota -q`
Expected: FAIL — `ImportError: cannot import name 'ensure_procedure_quota'`.

- [ ] **Step 3: Add the function**

在 `retrieval.py` 的 `type_weight()` 之后插入：

```python
def ensure_procedure_quota(scored_all, top_n, min_proc, key):
    """Take the top_n of an already-sorted `scored_all`, but guarantee at least
    `min_proc` procedures when the pool has them — back-fill from the remainder
    and evict the weakest non-procedure items. Never evicts a procedure; result
    is re-sorted by `key` descending and capped at top_n."""
    top = scored_all[:top_n]
    procs = [h for h in top if h.object_type == "procedure"]
    if len(procs) >= min_proc:
        return top
    have_ids = {h.object_id for h in top}
    extra = [h for h in scored_all[top_n:]
             if h.object_type == "procedure" and h.object_id not in have_ids]
    extra = extra[: min_proc - len(procs)]
    if not extra:
        return top
    non_proc = [h for h in top if h.object_type != "procedure"]
    # weakest non-procedures sit at the tail (top is sorted desc); drop len(extra) of them
    drop_ids = {h.object_id for h in non_proc[len(non_proc) - len(extra):]}
    kept = [h for h in top if h.object_id not in drop_ids]
    return sorted(kept + extra, key=key, reverse=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py -k ensure_procedure_quota -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(retrieval): 流程类问题 procedure 配额回填"
```

---

## Task 5: 证据分档 `classify_evidence`

**Files:**
- Modify: `backend/app/services/retrieval.py`（接 Task 4 之后）
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

追加（复用 Task 4 的 `_rk`）：

```python
def _anchor(oid):
    from app.models.schemas import AnswerAnchor
    return AnswerAnchor(key="k1", object_id=oid, object_type="claim", label="x")


def test_classify_evidence_three_levels():
    from app.services.retrieval import classify_evidence
    strong = [_rk("a", "claim", 0.6), _rk("b", "claim", 0.2)]
    # 强命中 + 锚定 + LLM 自报 grounded → grounded
    lvl, top = classify_evidence(strong, [_anchor("a")], True, 0.18, 0.35)
    assert lvl == "grounded" and top == 0.6
    # 有相关但只是弱/概述（top 在 [low, high)）→ overview
    weak = [_rk("a", "claim", 0.25)]
    lvl, _ = classify_evidence(weak, [_anchor("a")], True, 0.18, 0.35)
    assert lvl == "overview"
    # LLM 自报 grounded 但锚定的命中很弱 → 不许冒充 grounded
    lvl, _ = classify_evidence(weak, [_anchor("a")], True, 0.18, 0.35)
    assert lvl != "grounded"
    # 无锚点 → inferred（哪怕 top 高）
    lvl, _ = classify_evidence(strong, [], True, 0.18, 0.35)
    assert lvl == "inferred"
    # 无命中 → inferred
    lvl, top = classify_evidence([], [], False, 0.18, 0.35)
    assert lvl == "inferred" and top == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_classify_evidence_three_levels -q`
Expected: FAIL — `ImportError: cannot import name 'classify_evidence'`.

- [ ] **Step 3: Add the function**

在 `retrieval.py` 的 `ensure_procedure_quota()` 之后插入：

```python
def classify_evidence(top_hits, anchors, llm_grounded, tau_low, tau_high):
    """Relevance-aware grounding. Returns (evidence_level, top_relevance).

    - grounded : an answer-CITED hit is strongly relevant (>= tau_high) AND the
                 LLM self-reported grounded. (Can't fake grounding on junk.)
    - overview : some relevant hit exists (top relevance >= tau_low) but the
                 answer is largely extrapolated from thin evidence.
    - inferred : no relevant hit / nothing cited — general-knowledge answer.
    """
    top_rel = max((h.relevance for h in top_hits), default=0.0)
    if anchors:
        ids = {a.object_id for a in anchors}
        anchored_rel = max((h.relevance for h in top_hits if h.object_id in ids), default=0.0)
    else:
        anchored_rel = 0.0
    if top_hits and llm_grounded and anchors and anchored_rel >= tau_high:
        level = "grounded"
    elif top_hits and top_rel >= tau_low:
        level = "overview"
    else:
        level = "inferred"
    return level, top_rel
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_classify_evidence_three_levels -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(retrieval): 相关度感知证据分档 classify_evidence"
```

---

## Task 6: 追问改写 prompt

**Files:**
- Modify: `backend/app/services/prompts.py:71`（`ANSWER_SCHEMA_HINT` 附近）
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

追加：

```python
def test_followup_rewrite_prompt():
    from app.services.prompts import followup_rewrite_prompt, FOLLOWUP_REWRITE_SCHEMA_HINT
    p = followup_rewrite_prompt("User: innovus中有哪些常见flow\nAssistant: ...RTL到GDSII...",
                                "展开讲讲这个流程")
    assert "展开讲讲这个流程" in p          # 当前问题在
    assert "RTL到GDSII" in p                 # 历史在
    assert "query" in FOLLOWUP_REWRITE_SCHEMA_HINT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_followup_rewrite_prompt -q`
Expected: FAIL — `ImportError: cannot import name 'followup_rewrite_prompt'`.

- [ ] **Step 3: Add the prompt**

在 `backend/app/services/prompts.py` 的 `ANSWER_SCHEMA_HINT = '{"answer":"","grounded":true}'` 一行之前插入：

```python
FOLLOWUP_REWRITE_SCHEMA_HINT = '{"query":""}'


def followup_rewrite_prompt(history_block: str, question: str) -> str:
    return (
        "You rewrite a possibly-elliptical follow-up question into ONE standalone "
        "search query for a knowledge base, using the prior conversation to "
        "resolve pronouns and omissions (e.g. '这个流程' -> the concrete flow named "
        "earlier).\n"
        "Rules:\n"
        "- Output ONE concise query in the SAME language as the question.\n"
        "- Resolve references to concrete entities mentioned in the conversation.\n"
        "- Keep it search-friendly (keywords + the resolved entity); do NOT answer.\n"
        "- If the question is already standalone, return it essentially unchanged.\n\n"
        f"Prior conversation:\n{history_block}\n\n"
        f"Follow-up question: {question}\n\n"
        'Return JSON only: {"query":"<standalone search query>"}'
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_followup_rewrite_prompt -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prompts.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(prompts): 追问改写 prompt"
```

---

## Task 7: `AskResponse` 新增字段

**Files:**
- Modify: `backend/app/models/schemas.py:169-178`
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing test**

追加：

```python
def test_askresponse_new_fields_default_and_dump():
    from app.models.schemas import AskResponse
    r = AskResponse(conclusion="x")
    assert r.evidence_level == "inferred"
    assert r.retrieval_query == ""
    assert r.top_relevance == 0.0
    d = r.model_dump()
    assert {"evidence_level", "retrieval_query", "top_relevance"} <= set(d)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_askresponse_new_fields_default_and_dump -q`
Expected: FAIL — `AttributeError: 'AskResponse' object has no attribute 'evidence_level'`.

- [ ] **Step 3: Add the fields**

把 `backend/app/models/schemas.py` 的 `AskResponse`（第 169-178 行）改为：

```python
class AskResponse(BaseModel):
    answer_id: str = ""
    conclusion: str
    answer: str = ""
    grounded: bool = False
    # 相关度感知证据分档：grounded(有据) | overview(概述) | inferred(推断)
    evidence_level: str = "inferred"
    anchors: List[AnswerAnchor] = Field(default_factory=list)
    related_knowledge: List["KnowledgeRecord"] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    llm_mode: str = ""
    conversation_id: str = ""
    # 实际用于检索的 query（原问或改写后）+ 最高命中相关度，供排错/二期标定。
    retrieval_query: str = ""
    top_relevance: float = 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py::test_askresponse_new_fields_default_and_dump -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/schemas.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(schema): AskResponse 增 evidence_level/retrieval_query/top_relevance"
```

---

## Task 8: 接入 `ask()` —— 改写 + 意图排序 + 配额 + 证据分档 + 落字段

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`ask()` 2716-2770、2851-2898；`_answer_kg` 2952-2974；新增导入与 `_rewrite_followup_query`）
- Test: `backend/tests/test_followup_retrieval_grounding.py`

- [ ] **Step 1: Write the failing tests**

追加（自带最小 repo fixture，仿 `test_ask_redesign.py`；FakeLLM 按 schema_hint 分流改写/回答）：

```python
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


class RecordingLLM:
    """按 schema_hint 区分『改写调用』与『回答调用』。"""
    configured = True
    def __init__(self):
        self.rewrite_calls = []
        self.answer_calls = []
    def chat_json(self, messages, schema_hint):
        content = messages[0]["content"]
        if schema_hint == '{"query":""}':
            self.rewrite_calls.append(content)
            return json.dumps({"query": "RTL到GDSII流程 步骤"})
        self.answer_calls.append(content)
        return json.dumps({"answer": "答案 [k1].", "grounded": True})


@pytest.fixture
def repo2(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    r.llm_client = RecordingLLM()
    return r


def _seed_flow(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    return nb


def test_first_turn_not_rewritten(repo2):
    nb = _seed_flow(repo2)
    resp = repo2.ask(nb.id, AskRequest(question="innovus中有哪些常见flow"))
    assert repo2.llm_client.rewrite_calls == []        # 首轮 history 空，不改写
    assert resp.retrieval_query == "innovus中有哪些常见flow"
    assert resp.evidence_level in {"grounded", "overview", "inferred"}


def test_followup_triggers_rewrite_and_uses_rewritten_query(repo2):
    nb = _seed_flow(repo2)
    t1 = repo2.ask(nb.id, AskRequest(question="innovus中有哪些常见flow"))
    repo2.ask(nb.id, AskRequest(question="展开讲讲这个流程",
                                conversation_id=t1.conversation_id))
    # 第二轮含指代 + 有 history → 触发一次改写
    assert len(repo2.llm_client.rewrite_calls) == 1
    # 末轮答案落库的 retrieval_query 是改写后的串
    last = repo2.ask(nb.id, AskRequest(question="再展开这个流程",
                                       conversation_id=t1.conversation_id))
    assert last.retrieval_query == "RTL到GDSII流程 步骤"


def test_ask_sets_evidence_level_field(repo2):
    nb = _seed_flow(repo2)
    resp = repo2.ask(nb.id, AskRequest(question="RTL到GDSII流程"))
    assert hasattr(resp, "evidence_level") and resp.evidence_level
    assert resp.top_relevance >= 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py -k "rewritten or first_turn or evidence_level_field" -q`
Expected: FAIL — `retrieval_query`/改写计数不符（`ask()` 尚未接入）。

- [ ] **Step 3a: Add imports**

在 `backend/app/services/sqlite_repository.py` 顶部 import 区追加两行（与既有 `from app.services.retrieval import (...)` / `from app.services.prompts import (...)` 并列即可，名称不冲突）：

```python
from app.services.retrieval import (
    type_weight, is_process_query, ensure_procedure_quota, classify_evidence,
)
from app.services.prompts import followup_rewrite_prompt, FOLLOWUP_REWRITE_SCHEMA_HINT
from app.services.followup import looks_like_followup
```

- [ ] **Step 3b: Rewrite the head of `ask()`（2716-2770）**

把现有 2716-2770 段替换为：

```python
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        # Legacy `scenario` is accepted for frontend back-compat but no longer
        # woven into retrieval or the answer prompt.

        # Resolve (create-or-append) the conversation and load prior turns FIRST,
        # so an elliptical follow-up can be rewritten against it before retrieval.
        with self._connect() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question
            )
            history = self._conversation_history(db, conversation_id)

        # Coreference-resolve follow-ups into a standalone retrieval query
        # (gated; falls back to the raw question). Retrieval uses this; the
        # answer prompt still gets the user's original wording.
        retrieval_query = self._rewrite_followup_query(history, question)
        query = retrieval_query
        process_intent = is_process_query(question)

        with self._connect() as db:
            kg_objs: Dict[str, List[dict]] = {
                t: self._knowledge_objects(db, notebook_id, t) for t in _KG_TYPES
            }
            elements = self._gather_elements(db, notebook_id, with_vectors=False)
            query_vector = self._embed_query(query)
            all_kg = [o for objs in kg_objs.values() for o in objs]
            self._backfill_knowledge_embeddings(db, notebook_id, all_kg)
            elem_ids, elem_mat = self._vector_matrix(
                db, notebook_id, "element_embeddings", "element_id")
            kn_ids, kn_mat = self._vector_matrix(
                db, notebook_id, "knowledge_embeddings", "object_id")

        from app.services.vector_index import query_sims
        element_sims = query_sims(query_vector, elem_ids, elem_mat) if query_vector else None
        knowledge_sims = query_sims(query_vector, kn_ids, kn_mat) if query_vector else None

        # Score each KG type, pool, then rank by relevance * intent-aware type
        # weight (process/flow questions stop burying procedures).
        scored_all: List[RetrievedKnowledge] = []
        for t in _KG_TYPES:
            objs = kg_objs[t]
            if not objs:
                continue
            scored_all.extend(
                score_knowledge(
                    query, objs, t, query_vector, None, None, None,
                    element_sims=element_sims, knowledge_sims=knowledge_sims,
                )
            )
        rank_key = lambda it: it.score * type_weight(it.object_type, process_intent)
        scored_all.sort(key=rank_key, reverse=True)
        top_n = self.settings.retrieval_top_n
        if process_intent:
            top_hits: List[RetrievedKnowledge] = ensure_procedure_quota(
                scored_all, top_n, self.settings.proc_min, rank_key)
        else:
            top_hits = scored_all[:top_n]
```

> 注意：删去了原 2766-2767 处对 `_TYPE_WEIGHT` 的直接引用（已被 `type_weight()` 取代）。`_TYPE_WEIGHT` 的既有 import 可保留不动（其它处可能仍用）。

- [ ] **Step 3c: Rewrite the answer/grounding block（原 2851-2898）**

把原 `has_knowledge = bool(top_hits)` 到 `response = AskResponse(...)` 之间（2851-2898）替换为：

```python
        has_knowledge = bool(top_hits)
        llm_mode = "deterministic"
        conclusion = ""
        answer = ""
        llm_grounded = False
        anchors: List[AnswerAnchor] = []

        if self.llm_client.configured:
            try:
                answer, llm_grounded, anchors = self._answer_kg(
                    notebook_id, question, top_hits, history
                )
            except Exception:
                answer, llm_grounded, anchors = "", False, []

        # Relevance-aware grounding (no longer LLM self-report alone).
        evidence_level, top_relevance = classify_evidence(
            top_hits, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high,
        )
        grounded = evidence_level == "grounded"

        if answer:
            conclusion = _MARKER_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        else:
            llm_mode = "deterministic"
            if has_knowledge:
                n = len(top_hits)
                conclusion = (
                    f"Found {n} relevant KG knowledge object(s) for this question."
                    if n
                    else "Relevant notebook knowledge was retrieved for this question."
                )
            else:
                conclusion = (
                    "The notebook does not yet contain approved knowledge that "
                    "matches this question. Upload and review sources to build coverage."
                )

        response = AskResponse(
            answer_id="",
            conclusion=conclusion,
            answer=answer,
            grounded=grounded,
            evidence_level=evidence_level,
            anchors=anchors,
            related_knowledge=related_knowledge,
            citations=citations,
            llm_mode=llm_mode,
            conversation_id=conversation_id,
            retrieval_query=retrieval_query,
            top_relevance=top_relevance,
        )
```

- [ ] **Step 3d: Refactor `_answer_kg` return（2971-2974）**

把 `_answer_kg` 末尾的：

```python
        answer = str(data.get("answer", "")).strip()
        grounded = bool(data.get("grounded", False)) and bool(top_hits)
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, grounded, ("grounded" if grounded else "ungrounded"), anchors
```

改为（返回 LLM 原始自报，分档交给 `ask()`）：

```python
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors
```

并把其 docstring 里"grounded requires both the LLM's self-report and at least one retrieved hit"一句更新为"returns the LLM's raw self-report; the relevance-aware grounding is decided by the caller via classify_evidence."

- [ ] **Step 3e: Add `_rewrite_followup_query`**

在 `_answer_kg` 之前（约 2951 行）新增方法：

```python
    def _rewrite_followup_query(self, history: str, question: str) -> str:
        """Resolve an elliptical follow-up into a standalone retrieval query
        using prior turns. Gated (only when it looks like a follow-up and we
        have history + a configured LLM); always falls back to the raw question."""
        if not history.strip():
            return question
        if not looks_like_followup(question, self.settings.followup_max_len):
            return question
        if not self.llm_client.configured:
            return question
        try:
            raw = self.llm_client.chat_json(
                [{"role": "user", "content": followup_rewrite_prompt(history, question)}],
                FOLLOWUP_REWRITE_SCHEMA_HINT,
            )
            data = json.loads(raw)
            rewritten = str(data.get("query", "")).strip()
            return rewritten or question
        except Exception:
            return question
```

- [ ] **Step 4: Run the new + existing ask tests**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_followup_retrieval_grounding.py tests/test_ask_redesign.py tests/test_ask_vector_matrix.py tests/test_conversations.py -q`
Expected: PASS（全部）。若 `test_ask_grounded_answer_has_anchors` 失败，确认 `evidence_tau_high ≤ 0.4`（纯关键词命中融合分=0.4）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_followup_retrieval_grounding.py
git commit -m "feat(ask): 追问改写+意图排序+配额+证据分档接入"
```

---

## Task 9: 前端三档徽章

**Files:**
- Modify: `frontend/app/page.tsx:115-125`（`AskResponse` 类型）、`frontend/app/page.tsx:3590`（徽章）
- Modify: `frontend/app/globals.css`（在 `.answer-ungrounded` 规则旁加两条）

- [ ] **Step 1: 类型加字段**

把 `frontend/app/page.tsx` 第 124 行 `llm_mode: string;` 之后、`};`（125 行）之前插入：

```typescript
  evidence_level?: "grounded" | "overview" | "inferred";
  retrieval_query?: string;
  top_relevance?: number;
```

- [ ] **Step 2: 换徽章**

把第 3590 行：

```tsx
      {!answer.grounded && <span className="tag answer-ungrounded">未基于笔记本来源</span>}
```

替换为（始终显示一档徽章，旧记录无 evidence_level 时按 grounded 兜底）：

```tsx
      {(() => {
        const lvl = answer.evidence_level ?? (answer.grounded ? "grounded" : "inferred");
        const meta =
          lvl === "grounded"
            ? { cls: "answer-grounded", label: "有据" }
            : lvl === "overview"
            ? { cls: "answer-overview", label: "概述（仅薄证据，余为推断）" }
            : { cls: "answer-ungrounded", label: "推断（未命中笔记本依据）" };
        return <span className={`tag ${meta.cls}`}>{meta.label}</span>;
      })()}
```

- [ ] **Step 3: 加样式**

先定位现有规则：`rg -n "answer-ungrounded" frontend/app/globals.css`。在该规则同处追加：

```css
.answer-grounded {
  background: #e6f4ea;
  color: #1e7e34;
}
.answer-overview {
  background: #fff4e5;
  color: #b26a00;
}
```

- [ ] **Step 4: Typecheck / build**

Run: `cd frontend && npm run build`
Expected: 编译通过（无 TS 报错）。若仅想快速校验类型：`cd frontend && npx tsc --noEmit`。

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(ui): 答案三档证据徽章(有据/概述/推断)"
```

---

## Task 10: 全量校验（正确性，不含效果）

**Files:** 无（仅运行校验）

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -q`
Expected: 全绿。若 `scripts/smoke_backend.py` 相关或某 smoke 因新增字段做了严格 shape 断言而红，按"新增字段、向后兼容"的原则对齐该断言（仅加字段、不改既有字段语义）。

- [ ] **Step 2: check.sh**

Run: `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`
Expected: py_compile + smoke_backend + 前端 lint 全过。

- [ ] **Step 3: 前端 build**

Run: `cd frontend && npm run build`
Expected: 通过。

- [ ] **Step 4: Commit（如有 smoke/lint 对齐改动）**

```bash
git add -A
git commit -m "test: 对齐 smoke/lint（一期新增字段）"
```

---

## 自检：spec 覆盖

- A 追问改写 → Task 2（识别）+ Task 6（prompt）+ Task 8（`_rewrite_followup_query` 接入、检索用改写 query / 回答用原话、落 `retrieval_query`）。✓
- B 流程召回排序 → Task 3（意图+权重）+ Task 4（配额）+ Task 8（排序 key + 配额接入）。✓
- D grounded 三档 + 前端徽章 → Task 5（分档）+ Task 7（字段）+ Task 8（接入、`grounded`/`llm_mode` 重算）+ Task 9（徽章）。✓
- E 可观测字段 → Task 7（字段）+ Task 8（落 `retrieval_query`/`evidence_level`/`top_relevance`；`_save_answer` 已 `model_dump()` 自动持久化）。✓
- 非目标（C 段落兜底 / flow 重抽取 / 效果回归 / 逐句标注）→ 不在本计划，符合 spec。✓

## 风险与回归保护

- 既有 `test_ask_redesign.py` 依赖：`evidence_tau_high ≤ 0.4`（否则纯关键词命中的 grounded 测试会掉档）；Task 8 Step 4 显式回归这些用例。
- 改写仅在 history 非空 + 像追问 + LLM 配置时触发，且任何异常回退原问 → 首轮与无 LLM 路径行为不变。
- 配额只在流程意图触发、不驱逐 procedure、有单测护栏（Task 4）。
