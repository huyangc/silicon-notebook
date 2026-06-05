# flow 章节 → 结构化 procedure（二期）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽取期让 LLM 为 flow 章节直接输出一个带有序 `steps[]` 的 `procedure` 对象，让"展开某 flow"从库里取整条有序流程；并修掉 `node_context` 按 section_path 精确分组的过/误分组。

**Architecture:** 在 `kg/extract.py` 的抽取 schema/prompt 里给 Procedure 增加有序 `steps[]`（每步 name+元素标签 ev）；`extract_window` 解析并绑证据到 `Node.steps`；`build_records` 写入 `payload.steps[{name,element_id,quote}]`；`canonicalize` 新增按 `(name, section_path)` 跨窗口合并 Procedure 并按 char_start 拼接步骤；`node_context` 优先直读 `payload.steps`、旧形态回退现有分组；一个可重入脚本对指定 notebook 全量重抽取（先验证 innovus）。

**Tech Stack:** Python / pydantic（kg models）/ SQLite / OpenAI 兼容 LLM（`client.chat_json`）。

**Spec:** `docs/superpowers/specs/2026-06-05-flow-procedure-extraction-design.md`

**约定**：
- Python：`PY=/opt/homebrew/Caskroom/miniconda/base/bin/python`
- 测试：`cd backend && PYTHONPATH=. $PY -m pytest tests/<path> -q`
- 本期**不做效果回归**；判据=单测绿 + `scripts/check.sh` 绿 + 既有测试不回归。范围 A：不修章节层级、不跨章节合成。

---

## 文件结构

- **Modify** `backend/app/services/kg/models.py` — 新增 `Step`，`Node` 增 `steps`。
- **Modify** `backend/app/services/kg/extract.py` — `_KG_SCHEMA_HINT`/`_prompt` 增 steps；`extract_window` 解析 steps（新 `_parse_steps`）。
- **Modify** `backend/app/services/kg_ingest.py` — `build_records` 写 `payload.steps`。
- **Modify** `backend/app/services/kg/canonicalize.py` — 新增按 `(name,section_path)` 合并 Procedure、拼接步骤。
- **Modify** `backend/app/services/sqlite_repository.py` — `node_context` procedure 分支优先读 `payload.steps`，旧形态回退。
- **Create** `backend/app/services/reextract.py` — `reextract_notebook(repo, notebook_id)`（可单测）。
- **Create** `scripts/reextract_notebook.py` — 薄 CLI 包装。
- **Tests**: `backend/tests/kg/test_models.py`、`test_extract.py`、`test_canonicalize.py`（追加）；新 `backend/tests/kg/test_build_records_steps.py`、`backend/tests/test_node_context_steps.py`、`backend/tests/test_reextract.py`。

---

## Task 1: Step 模型 + Node.steps

**Files:** Modify `backend/app/services/kg/models.py:8-22`; Test `backend/tests/kg/test_models.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/kg/test_models.py`）
```python
def test_node_carries_ordered_steps():
    from app.services.kg.models import Node, Step, Evidence
    ev = Evidence(file="d.md", char_start=0, char_end=5, line_start=1, line_end=1, quote="hello")
    n = Node(id="p1", type="Procedure", name="Foundation Flow",
             steps=[Step(name="import", evidence=[ev]), Step(name="floorplan", evidence=[ev])])
    assert [s.name for s in n.steps] == ["import", "floorplan"]
    assert n.steps[0].evidence[0].quote == "hello"
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_models.py::test_node_carries_ordered_steps -q`
Expected: FAIL — `ImportError: cannot import name 'Step'`.

- [ ] **Step 3: 实现** — 在 `models.py` 的 `Evidence` 类之后、`Node` 类之前插入 `Step`，并给 `Node` 增 `steps`：
```python
class Step(BaseModel):
    name: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
```
并把 `Node` 改为（在 `mentions` 行后增一行 `steps`）：
```python
class Node(BaseModel):
    id: str
    type: NodeType
    name: str = ""              # node text: Concept/Procedure name, Claim statement, Formula expression
    section_path: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    mentions: List[Evidence] = Field(default_factory=list)
    steps: List[Step] = Field(default_factory=list)   # ordered steps for a flow Procedure
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_models.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg/models.py backend/tests/kg/test_models.py
git commit -m "feat(kg): Step 模型 + Node.steps"
```

---

## Task 2: 抽取 schema hint + prompt

**Files:** Modify `backend/app/services/kg/extract.py:18-55`; Test `backend/tests/kg/test_extract.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/kg/test_extract.py`）
```python
def test_prompt_and_schema_mention_steps():
    from app.services.kg.extract import _prompt, _KG_SCHEMA_HINT
    assert '"steps"' in _KG_SCHEMA_HINT
    p = _prompt("[0] x", "1 > Flow", "manual")
    assert "steps" in p
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_extract.py::test_prompt_and_schema_mention_steps -q`
Expected: FAIL（schema/prompt 无 "steps"）。

- [ ] **Step 3a: 改 `_KG_SCHEMA_HINT`** — 把（:18-22）替换为：
```python
_KG_SCHEMA_HINT = (
    '{"nodes":[{"local_id":"","type":"Concept|Claim|Formula|Procedure",'
    '"name":"","ev":0,"steps":[{"name":"","ev":0}]}],'
    '"edges":[{"type":"about|supports|...","source":"","target":"","ev":0}]}'
)
```

- [ ] **Step 3b: 改 `_prompt`** — 在 prompt 里把这句：
```
For an ordered Procedure, connect its consecutive steps with `precedes` edges (step_i -> step_{{i+1}}).
```
替换为：
```
For a Procedure that is an ordered multi-step process/flow, emit it as ONE Procedure node (named after the flow — use the section heading if it names the flow) and list its ordered steps in a `steps` array, each {{"name":..,"ev":..}} where ev is the element label containing that step; prefer this over many separate Procedure nodes. `steps` is the source of truth for order (you may still add `precedes` edges).
```
（保留其后的 "Skip narrative/filler." 不动。）

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_extract.py -q`
Expected: PASS（含既有 test_marker_anchoring）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg/extract.py backend/tests/kg/test_extract.py
git commit -m "feat(kg): 抽取 schema/prompt 增有序 steps[]"
```

---

## Task 3: extract_window 解析 steps

**Files:** Modify `backend/app/services/kg/extract.py:10,96-108`; Test `backend/tests/kg/test_extract.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/kg/test_extract.py`，复用其中的 `ELEMENTS`）
```python
def test_extract_window_parses_procedure_steps():
    import json
    class FakeProc:
        def chat_json(self, messages, hint):
            return json.dumps({"nodes": [
                {"local_id": "p", "type": "Procedure", "name": "Foundation Flow", "ev": 0,
                 "steps": [{"name": "import design", "ev": 0},
                           {"name": "floorplan", "ev": 1},
                           {"name": "unbindable step", "ev": 99}]}],
                "edges": []})
    nodes, _ = extract_window(FakeProc(), ELEMENTS, "1 > Flow", "manual", win_idx=0)
    proc = [n for n in nodes if n.type == "Procedure"][0]
    # ev=0 binds el0, ev=1 binds el1; the third has bad ev + no name match -> dropped
    assert [s.name for s in proc.steps] == ["import design", "floorplan"]
    assert proc.steps[0].evidence[0].quote == ELEMENTS[0].text
    assert proc.steps[1].evidence[0].quote == ELEMENTS[1].text
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_extract.py::test_extract_window_parses_procedure_steps -q`
Expected: FAIL（`proc.steps` 为空）。

- [ ] **Step 3a: import Step** — 把 `extract.py` 第 10 行
```python
from app.services.kg.models import Edge, Evidence, Node
```
改为
```python
from app.services.kg.models import Edge, Evidence, Node, Step
```

- [ ] **Step 3b: 新增 `_parse_steps`**（放在 `extract_window` 之前，比如 `_ev` 之后）：
```python
def _parse_steps(elements: List[SourceElementQ], raw_steps: Any) -> List[Step]:
    """Resolve an LLM `steps` array into grounded Step objects (drop unbindable)."""
    steps: List[Step] = []
    if not isinstance(raw_steps, list):
        return steps
    for st in raw_steps:
        if not isinstance(st, dict):
            continue
        nm = str(st.get("name", "")).strip()
        if not nm:
            continue
        el = _resolve(elements, st.get("ev"), nm)
        if el is None:
            continue
        steps.append(Step(name=nm, evidence=[_ev(el)]))
    return steps
```

- [ ] **Step 3c: 在节点解析处挂上 steps** — 把（:104-106）
```python
            nid = f"W{win_idx}-{len(nodes)}"
            nodes.append(Node(id=nid, type=it["type"], name=str(it.get("name", "")),
                              section_path=section_path, evidence=[_ev(el)]))
```
替换为：
```python
            nid = f"W{win_idx}-{len(nodes)}"
            node = Node(id=nid, type=it["type"], name=str(it.get("name", "")),
                        section_path=section_path, evidence=[_ev(el)])
            if it["type"] == "Procedure":
                node.steps = _parse_steps(elements, it.get("steps"))
            nodes.append(node)
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_extract.py -q`
Expected: PASS（含既有用例）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg/extract.py backend/tests/kg/test_extract.py
git commit -m "feat(kg): extract_window 解析 procedure steps 并绑证据"
```

---

## Task 4: build_records 写 payload.steps

**Files:** Modify `backend/app/services/kg_ingest.py:62-94`; Test `backend/tests/kg/test_build_records_steps.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/kg/test_build_records_steps.py`）
```python
from types import SimpleNamespace
from app.services.kg_ingest import build_records
from app.services.kg.models import KnowledgeGraph, Node, Step, Evidence


def _el(eid, text):
    return SimpleNamespace(id=eid, element_type="paragraph", location_label="1", text=text)


def _kev(text):
    return Evidence(file="d.md", char_start=0, char_end=len(text), line_start=1, line_end=1, quote=text)


def test_build_records_binds_procedure_steps():
    el0 = _el("E0", "import the design netlist")
    el1 = _el("E1", "run floorplanning now")
    node = Node(id="p1", type="Procedure", name="Foundation Flow", section_path="1 > Flow",
                evidence=[_kev("import the design netlist")],
                steps=[Step(name="import", evidence=[_kev("import the design netlist")]),
                       Step(name="floorplan", evidence=[_kev("run floorplanning now")])])
    g = KnowledgeGraph(doc_id="d", doc_type="manual", nodes=[node], edges=[])
    objects, _ = build_records(g, "src-1", "Doc", [el0, el1])
    proc = [o for o in objects if o["object_type"] == "procedure"][0]
    steps = proc["payload"]["steps"]
    assert [s["name"] for s in steps] == ["import", "floorplan"]
    assert steps[0]["element_id"] == "E0" and steps[1]["element_id"] == "E1"
    assert steps[0]["quote"]


def test_build_records_procedure_without_steps_unchanged():
    el0 = _el("E0", "a single action happens here")
    node = Node(id="p1", type="Procedure", name="lone action", section_path="1 > X",
                evidence=[_kev("a single action happens here")])
    g = KnowledgeGraph(doc_id="d", doc_type="manual", nodes=[node], edges=[])
    objects, _ = build_records(g, "src-1", "Doc", [el0])
    proc = [o for o in objects if o["object_type"] == "procedure"][0]
    assert "steps" not in proc["payload"]   # no steps -> payload shape unchanged
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_build_records_steps.py -q`
Expected: FAIL（`payload` 无 `steps`）。

- [ ] **Step 3: 实现** — 把 `build_records`（:79-84）的对象构造段：
```python
        kept.add(node.id)
        objects.append({
            "local_id": node.id,
            "object_type": node.type.lower(),
            "payload": {"name": node.name, "section_path": node.section_path},
            "evidence": bound,
        })
```
替换为：
```python
        kept.add(node.id)
        payload = {"name": node.name, "section_path": node.section_path}
        if node.steps:
            bound_steps = []
            for st in node.steps:
                quote = st.evidence[0].quote if st.evidence else ""
                fields = _bind_quote(quote, elements, source_id, source_title)
                if fields:
                    bound_steps.append({
                        "name": st.name,
                        "element_id": fields["element_id"],
                        "quote": fields["quoted_span"],
                    })
            if bound_steps:
                payload["steps"] = bound_steps
        objects.append({
            "local_id": node.id,
            "object_type": node.type.lower(),
            "payload": payload,
            "evidence": bound,
        })
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_build_records_steps.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg_ingest.py backend/tests/kg/test_build_records_steps.py
git commit -m "feat(kg): build_records 把 steps 绑成 payload.steps"
```

---

## Task 5: canonicalize 跨窗口合并 Procedure

**Files:** Modify `backend/app/services/kg/canonicalize.py:10-49`; Test `backend/tests/kg/test_canonicalize.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/kg/test_canonicalize.py`）
```python
def test_canonicalize_merges_procedure_steps_across_windows():
    from app.services.kg.canonicalize import canonicalize
    from app.services.kg.models import Node, Step, Evidence
    def ev(cs):
        return Evidence(file="d", char_start=cs, char_end=cs + 5, line_start=1, line_end=1, quote="q")
    n1 = Node(id="W0-0", type="Procedure", name="Foundation Flow", section_path="1 > Flow",
              steps=[Step(name="b", evidence=[ev(100)])])
    n2 = Node(id="W1-0", type="Procedure", name="foundation  flow", section_path="1 > Flow",
              steps=[Step(name="a", evidence=[ev(10)])])
    other = Node(id="W2-0", type="Procedure", name="Foundation Flow", section_path="2 > Other",
                 steps=[Step(name="z", evidence=[ev(5)])])
    out, _ = canonicalize([n1, n2, other], [], "doc")
    procs = [n for n in out if n.type == "Procedure"]
    merged = [n for n in procs if n.section_path == "1 > Flow"]
    assert len(merged) == 1
    assert [s.name for s in merged[0].steps] == ["a", "b"]      # ordered by char_start (10,100)
    assert any(n.section_path == "2 > Other" for n in procs)    # different section kept separate
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_canonicalize.py::test_canonicalize_merges_procedure_steps_across_windows -q`
Expected: FAIL（两个 "1 > Flow" 节点未合并）。

- [ ] **Step 3: 实现** — 把整个 `canonicalize` 函数（:10-49）替换为（在 Concept 合并基础上加 Procedure 合并 + 步骤拼接）：
```python
def canonicalize(nodes: List[Node], edges: List[Edge], doc_id: str) -> Tuple[List[Node], List[Edge]]:
    canon: dict = {}          # normalized name -> canonical Concept node
    proc_canon: dict = {}     # (normalized name, section_path) -> canonical Procedure node
    remap: dict = {}          # every original node id -> final id
    out: List[Node] = []
    cn = 0
    for n in nodes:
        if n.type == "Concept" and _norm(n.name):
            key = _norm(n.name)
            if key in canon:
                c = canon[key]
                c.mentions.extend(n.evidence + n.mentions)
                remap[n.id] = c.id
            else:
                cn += 1
                new_id = f"{doc_id}:C{cn}"
                remap[n.id] = new_id
                n.id = new_id
                n.mentions = list(n.evidence)
                canon[key] = n
                out.append(n)
        elif n.type == "Procedure" and _norm(n.name):
            key = (_norm(n.name), n.section_path)
            if key in proc_canon:
                c = proc_canon[key]
                c.steps.extend(n.steps)        # concatenate; ordered + deduped below
                remap[n.id] = c.id
            else:
                proc_canon[key] = n
                remap[n.id] = n.id
                out.append(n)
        else:
            remap[n.id] = n.id
            out.append(n)
    # order each merged flow's steps by evidence position and drop name-duplicates
    for n in out:
        if n.type == "Procedure" and n.steps:
            n.steps.sort(key=lambda s: (s.evidence[0].char_start if s.evidence else 1_000_000))
            seen, deduped = set(), []
            for s in n.steps:
                k = _norm(s.name)
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(s)
            n.steps = deduped
    final_edges: List[Edge] = []
    seen = set()
    for e in edges:
        s = remap.get(e.source_id, e.source_id)
        t = remap.get(e.target_id, e.target_id)
        if s == t:
            continue
        key = (e.type, s, t)
        if key in seen:
            continue
        seen.add(key)
        final_edges.append(Edge(id=e.id, type=e.type, source_id=s, target_id=t, evidence=e.evidence))
    return out, final_edges
```

- [ ] **Step 4: 跑测试，确认 PASS**（含既有 Concept 合并用例）
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_canonicalize.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg/canonicalize.py backend/tests/kg/test_canonicalize.py
git commit -m "feat(kg): canonicalize 按(name,section)合并Procedure并拼接steps"
```

---

## Task 6: node_context 直读 payload.steps + 旧形态回退

**Files:** Modify `backend/app/services/sqlite_repository.py:2104-2131`; Test `backend/tests/test_node_context_steps.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_node_context_steps.py`）
```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_node_context_reads_payload_steps(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure", {
        "name": "Foundation Flow", "section_path": "1 > Flow",
        "steps": [{"name": "import", "element_id": "E0", "quote": "import"},
                  {"name": "floorplan", "element_id": "E1", "quote": "floorplan"}]})
    ctx = repo.node_context(nb.id, pid)
    assert [s["name"] for s in ctx["steps"]] == ["import", "floorplan"]   # order preserved


def test_node_context_legacy_procedure_fallback(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure",
                                   {"name": "old step", "section_path": "1 > X"})  # no steps[]
    ctx = repo.node_context(nb.id, pid)
    assert isinstance(ctx["steps"], list)                 # fallback ran, no crash
    assert any(s["name"] == "old step" for s in ctx["steps"])
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_node_context_steps.py -q`
Expected: FAIL（新形态 `test_node_context_reads_payload_steps` 失败——当前按 section_path 分组、读不到 payload.steps）。

- [ ] **Step 3: 实现** — 把 `node_context` 的 procedure 分支（:2104-2131）替换为：
```python
            if obj_type == "procedure":
                steps_payload = payload.get("steps")
                if isinstance(steps_payload, list) and steps_payload:
                    # New self-contained shape: ordered steps live in the object's payload.
                    eids = [s.get("element_id") for s in steps_payload if s.get("element_id")]
                    texts, _ord = self._element_texts(db, eids) if eids else ({}, {})
                    result["steps"] = [
                        {"name": s.get("name", ""),
                         "element_text": texts.get(s.get("element_id", ""), s.get("quote", "")),
                         "section_path": section}
                        for s in steps_payload
                    ]
                else:
                    # Legacy fallback: group sibling procedure nodes by exact section_path
                    # (precedes edges are sparse). Two distinct procedures sharing a heading
                    # would merge — acceptable for inspection.
                    prows = db.execute(
                        "SELECT id, payload, evidence FROM knowledge_objects WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated'", (notebook_id,)).fetchall()
                    candidate_steps = []
                    for pr in prows:
                        ppay = json.loads(pr["payload"] or "{}")
                        if ppay.get("section_path", "") != section:
                            continue
                        ev = json.loads(pr["evidence"] or "[]")
                        first_eid = ev[0].get("element_id") if ev else ""
                        candidate_steps.append((ppay.get("name", ""), first_eid))
                    all_step_first_eids = [eid for _, eid in candidate_steps if eid]
                    if all_step_first_eids:
                        texts, ordinal = self._element_texts(db, all_step_first_eids)
                    else:
                        texts, ordinal = {}, {}
                    steps = []
                    for step_name, first_eid in candidate_steps:
                        steps.append({"name": step_name, "element_text": texts.get(first_eid, ""),
                                      "section_path": section, "_ord": ordinal.get(first_eid, 1_000_000)})
                    steps.sort(key=lambda s: s["_ord"])
                    for s in steps:
                        s.pop("_ord", None)
                    result["steps"] = steps
            return result
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_node_context_steps.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_node_context_steps.py
git commit -m "feat(kg): node_context 直读 payload.steps + 旧形态回退"
```

---

## Task 7: 重抽取脚本（可单测的函数 + 薄 CLI）

**Files:** Create `backend/app/services/reextract.py`, `scripts/reextract_notebook.py`; Test `backend/tests/test_reextract.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_reextract.py`）
```python
def test_reextract_notebook_loops_all_sources_in_order():
    from app.services.reextract import reextract_notebook

    class _Summary:
        def __init__(self, sid): self.id = sid

    class FakeRepo:
        def __init__(self): self.extracted = []
        def list_sources(self, notebook_id): return [_Summary("s1"), _Summary("s2")]
        def extract_source(self, source_id): self.extracted.append(source_id)

    repo = FakeRepo()
    done = reextract_notebook(repo, "nb-1")
    assert done == ["s1", "s2"]
    assert repo.extracted == ["s1", "s2"]


def test_reextract_notebook_continues_on_source_error():
    from app.services.reextract import reextract_notebook

    class _Summary:
        def __init__(self, sid): self.id = sid

    class FakeRepo:
        def __init__(self): self.extracted = []
        def list_sources(self, notebook_id): return [_Summary("s1"), _Summary("s2")]
        def extract_source(self, source_id):
            if source_id == "s1":
                raise RuntimeError("boom")
            self.extracted.append(source_id)

    repo = FakeRepo()
    done = reextract_notebook(repo, "nb-1")
    assert done == ["s2"]                 # s1 failed, s2 still processed
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_reextract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.reextract'`.

- [ ] **Step 3a: 实现函数** — 新建 `backend/app/services/reextract.py`：
```python
"""Re-extract every source of a notebook (offline job).

`extract_source` deletes the source's old KG objects/relations and rebuilds them
from the current extractor, so re-running is idempotent per source. A failing
source is logged and skipped — the rest still run."""

from __future__ import annotations

from typing import List


def reextract_notebook(repo, notebook_id: str) -> List[str]:
    """Re-extract all sources of `notebook_id`. Returns the source ids that
    completed successfully (in order)."""
    done: List[str] = []
    for summary in repo.list_sources(notebook_id):
        try:
            repo.extract_source(summary.id)
            done.append(summary.id)
        except Exception as exc:  # noqa: BLE001 — one bad source must not abort the run
            print(f"[reextract] source {summary.id} failed: {exc}")
    return done
```

- [ ] **Step 3b: 实现薄 CLI** — 新建 `scripts/reextract_notebook.py`：
```python
#!/usr/bin/env python
"""CLI: re-extract all sources of a notebook.

Usage (from repo root):
  PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
      scripts/reextract_notebook.py <notebook_id>
"""
import sys

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.reextract import reextract_notebook


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: reextract_notebook.py <notebook_id>")
        return 2
    notebook_id = sys.argv[1]
    repo = SQLiteRepository(Settings())
    done = reextract_notebook(repo, notebook_id)
    print(f"[reextract] re-extracted {len(done)} source(s): {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_reextract.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/reextract.py scripts/reextract_notebook.py backend/tests/test_reextract.py
git commit -m "feat(reextract): notebook 全量重抽取函数 + CLI"
```

---

## Task 8: 全量校验（正确性，不含效果）

**Files:** 无（仅运行校验）

- [ ] **Step 1: kg + repo 相关测试**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg tests/test_node_context_steps.py tests/test_reextract.py -q`
Expected: 全绿。若既有 `tests/kg/test_canonicalize.py` 因新增 Procedure 合并而某断言变化，按"同名同 section 才合并、且仅对 Procedure"的语义核对并更新该断言。

- [ ] **Step 2: 后端全量**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -q`
Expected: 全绿（含一期的 ask/conversation 等）。

- [ ] **Step 3: check.sh**
Run: `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`
Expected: EXIT 0（py_compile 含 kg_ingest/extract/canonicalize/models/sqlite_repository + smoke + 前端 lint）。注意 `scripts/check.sh` 的 py_compile 清单未含 `app/services/kg/*` 与 `app/services/reextract.py`——可选地把这两条加入 check.sh 的 py_compile 列表（与既有风格一致）。

- [ ] **Step 4: Commit（如对齐了 check.sh / 既有断言）**
```bash
git add -A
git commit -m "test: 对齐二期(canonicalize断言/check.sh py_compile清单)"
```

---

## 自检：spec 覆盖

- 抽取 schema/prompt 增 steps → Task 2。✓
- Node.steps + 解析绑证据 → Task 1 + Task 3。✓
- build_records payload.steps → Task 4。✓
- canonicalize 跨窗口按(name,section)合并拼接 → Task 5。✓
- node_context 直读 steps[] + 旧形态回退 → Task 6。✓
- 重抽取脚本(先验证 innovus) → Task 7（函数+CLI；实际对 innovus 跑由你在合并后用 CLI 触发，属离线作业不在自动化测试内）。✓
- 非目标(章节层级/跨章节合成/效果回归) → 不在计划。✓

## 风险与回归保护
- steps 全程可选：无 steps 的 procedure payload 不变、extract_window/ build_records/ node_context 旧路径不破（Task 3/4/6 各含"无 steps"或"旧形态"用例）。
- Procedure 误合并：合并键含 section_path 且仅 Procedure，Task 5 用例覆盖"同名不同章不并"。
- 既有 kg 测试：Task 8 Step 1/2 显式回归 `tests/kg` 全量。
- 重抽取对 innovus 的实际运行属离线、可重入（按 source 删旧建新），不在 CI；先单库验证。
