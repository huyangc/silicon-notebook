# SA Schema-Aligned Extraction + A/B Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 KG 抽取对齐到 schema v1.0.0(validity_scope、原子 claim+连边、放宽并显式猎稀疏推理边、base meta 过滤),并用 A/B 小切片标定证明新 prompt 更好。

**Architecture:** 改动集中在抽取侧:`Node` 加 `validity_scope`;`_prompt`/`_KG_SCHEMA_HINT` 重写并加 `base_filter` 参数;`extract_window`/`extract_graph`/生产调用方串 `base_filter` 并解析 validity_scope;`build_records` 把 validity_scope 写进 payload(JSON,无 DB 迁移)。标定是独立脚本(scratch DB、就地读 root .env、old-vs-new 同切片)。

**Tech Stack:** Python 3.13、pydantic v2、pytest(`backend/tests/kg/`,`Fake().chat_json` stub 模式)、sqlite3。检索/答案/前端零改动。

参考:`docs/superpowers/specs/2026-06-10-sa-extraction-alignment-design.md`、`schema/kg-schema.yaml` v1.0.0。

**已核实的事实(实现者无需重新勘探):**
- `Node`(pydantic)定义:`backend/app/services/kg/models.py:20-27`,字段 id/type/name/section_path/evidence/mentions/steps。
- `_prompt` 在 `backend/app/services/kg/extract.py:26-68`;`_KG_SCHEMA_HINT` 在 `:19`;`extract_window` 在 `:198-256`(节点装配 217-230,边装配 246-255,**边只校验 `type in EDGE_TYPES`,不校验端点类型** → 放宽边纯 prompt 事)。`EDGE_TYPES` 已含全 12 类。`safe_json` from `app.services.kg.client`。
- Node→payload 装配:`backend/app/services/kg_ingest.py:build_records` 第 80 行 `payload = {"name": node.name, "section_path": node.section_path}`。
- `extract_graph`:`kg_ingest.py:151-188`,第 172 行 `submit_window(extract_window, client, els, w.section_path, doc_type, idx, refine=..., gleaning_rounds=...)`。
- 生产调用方:`backend/app/services/sqlite_repository.py:1402-1409`(`source.notebook_id` 可查 tier;`store_kg` 已用 `SELECT tier FROM notebooks WHERE id=?` 模式)。
- **canonicalize 只合并 Concept/Procedure,不碰 Claim/Formula**(`kg/canonicalize.py:17,31`)→ validity_scope 天然穿透,**spec 里「canonicalize 保 scope」任务作废**。
- 测试模式:`backend/tests/kg/test_extract.py` —— `Fake` 类带 `chat_json(self, messages, response_schema_hint)` 返回 `json.dumps({...})`;`_se(idx,text,char_start)` 造 `SourceElementQ`;`ELEMENTS` fixture;pytest `def test_*`。

---

### Task 1: `Node.validity_scope` 字段

**Files:**
- Modify: `backend/app/services/kg/models.py:20-27`
- Test: `backend/tests/kg/test_sa_extraction.py`(新建)

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/kg/test_sa_extraction.py`:

```python
import json
from app.services.kg.models import Node


def test_node_validity_scope_defaults_empty():
    n = Node(id="n1", type="Claim", name="x")
    assert n.validity_scope == {}


def test_node_validity_scope_roundtrips():
    vs = {"region": ["saturation"], "approximation": "small-signal"}
    n = Node(id="n2", type="Formula", name="g_m = ...", validity_scope=vs)
    assert n.validity_scope == vs
    assert n.model_dump()["validity_scope"] == vs
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -v`
Expected: FAIL — `TypeError`/`ValidationError`(`Node` 无 `validity_scope` 字段)。

- [ ] **Step 3: 加字段**

`backend/app/services/kg/models.py`,在 `Node` 的 `steps` 行后加(并确保文件顶部 `from typing import Any, Dict, List, Literal` 已含 `Any, Dict` —— 当前已是 `Any, Dict, List, Literal`):

```python
class Node(BaseModel):
    id: str
    type: NodeType
    name: str = ""              # node text: Concept/Procedure name, Claim statement, Formula expression
    section_path: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    mentions: List[Evidence] = Field(default_factory=list)
    steps: List[Step] = Field(default_factory=list)   # ordered steps for a flow Procedure
    validity_scope: Dict[str, Any] = Field(default_factory=dict)  # claim/formula only: {region[],assumptions[],approximation,range}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -v`
Expected: PASS(2 passed)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/models.py backend/tests/kg/test_sa_extraction.py
git commit -m "feat(kg): Node 加 validity_scope 字段(claim/formula)"
```

---

### Task 2: 重写 `_prompt` + `_KG_SCHEMA_HINT`(加 base_filter + 4 条 schema 指令)

**Files:**
- Modify: `backend/app/services/kg/extract.py:19`(`_KG_SCHEMA_HINT`)、`:26-68`(`_prompt`)
- Test: `backend/tests/kg/test_sa_extraction.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/kg/test_sa_extraction.py`:

```python
from app.services.kg.extract import _prompt, _KG_SCHEMA_HINT


def test_schema_hint_includes_validity_scope():
    assert "validity_scope" in _KG_SCHEMA_HINT


def test_prompt_has_sa_directives():
    p = _prompt("[0] foo", "1 > 1.1", "textbook")
    assert "[0] foo" in p and "ev" in p          # 既有契约不破
    assert "validity_scope" in p
    assert "ATOMIC" in p or "atomic" in p
    for e in ("depends_on", "contrasts_with", "prerequisite_of"):
        assert e in p


def test_prompt_base_filter_toggles_meta_rule():
    on = _prompt("[0] x", "1", "textbook", base_filter=True)
    off = _prompt("[0] x", "1", "textbook", base_filter=False)
    assert "QUALITY FILTER" in on
    assert "QUALITY FILTER" not in off
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -k "schema_hint or sa_directives or base_filter" -v`
Expected: FAIL —— `validity_scope` 不在 hint/prompt;`_prompt` 不接受 `base_filter`(TypeError)。

- [ ] **Step 3: 重写 `_KG_SCHEMA_HINT` 与 `_prompt`**

`backend/app/services/kg/extract.py`,替换 `_KG_SCHEMA_HINT`(:19)为:

```python
_KG_SCHEMA_HINT = (
    '{"nodes":[{"local_id":"","type":"Concept|Claim|Formula|Procedure","name":"",'
    '"ev":0,"validity_scope":{"region":[],"assumptions":[],"approximation":"","range":""},'
    '"steps":[{"name":"","ev":0}]}],'
    '"edges":[{"type":"about|supports|derived_from|depends_on|contrasts_with|'
    'prerequisite_of|defines|part_of|composed_of|kind_of|used_in|precedes",'
    '"source":"<local_id>","target":"<local_id>","ev":0}]}'
)
```

替换整个 `_prompt`(:26-68)为:

```python
def _prompt(labeled_text: str, section_path: str, doc_type: str,
            base_filter: bool = False) -> str:
    base_rule = (
        "\nBASE-TIER QUALITY FILTER: drop non-knowledge meta-text — pedagogical "
        "asides (\"In a semester system…\"), exercise/homework hints, tool/UI "
        "trivia, document navigation. Keep only durable technical knowledge.\n"
        if base_filter else ""
    )
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure (Concept=a NAMED reusable technical entity — method, mechanism,
component, named model/structure/distribution; Claim=truth-evaluable assertion;
Formula=equation; Procedure=ordered process).

Be SELECTIVE with Concepts: emit a Concept only for a distinctive NAMED entity.
Do NOT emit Concepts for generic/common terms or trivial sub-parts; nor for bare
symbols/variables (V_DD, g_m1, (W/L)_1); instance labels (Q1, M5, Pole p8);
figure/table/equation/section references (Fig. 5.38, Eq. 9.4); section headings;
or enumerated settings (Level 1/2/3 Model, Type I/II). Capture EVERY Formula and
EVERY Procedure present.

CLAIMS — ATOMIC (one proposition per node). SPLIT compound statements:
- "A and B" / "A; B"  ->  two separate Claims.
- "B because/therefore A", "A, which causes B"  ->  TWO atomic Claims (A, B) PLUS
  a reasoning edge between them (supports / derived_from / depends_on) by local_id.
  Connecting the atoms with an edge is HOW reasoning edges get built — do it.
GUARDRAIL: split ONLY when each part stands alone as truth-evaluable; never
fragment a single proposition just because it is long. Do NOT emit Claims for
section headings, narrative/meta sentences about the document ("This chapter
covers…"), or navigation.

VALIDITY SCOPE: when a Claim or Formula holds only under a stated condition, put
that condition in a structured `validity_scope` object ON that node — NOT as prose,
NOT as a separate dangling Claim (never emit "This holds for DC…" as its own Claim).
Fields (ALL optional; include only what the text explicitly states):
  region: [..]        e.g. ["saturation"] | ["weak-inversion"]
  assumptions: [..]   e.g. ["perfect matching", "R_in << R_C"]
  approximation: ".." e.g. "small-signal" | "neglecting body effect"
  range: ".."         e.g. "low-frequency" | "f << f_T"
NEVER invent a scope the text does not state; OMIT validity_scope when none.

EDGES (source->target by local_id). REASONING-BEARING edges are the PRIORITY and
connect Claims/Formulas/Concepts (not only Concept->Concept):
- supports (claim/formula/concept -> claim): evidence/argument backing a claim.
- derived_from (claim/formula -> claim/formula): result follows from another.
- depends_on (claim/formula/concept -> ...): validity/value depends on target.
- contrasts_with (claim/formula/concept <-> ...): trade-off / disagreement /
  contradiction.
- prerequisite_of (concept/claim -> concept/claim): must hold/be understood first.
EXPLICITLY HUNT depends_on, contrasts_with, prerequisite_of — rare and high-value;
look for "requires", "assuming", "unlike", "trade-off", "valid when", "before".
Structural edges (secondary): about(claim/formula->concept), defines(claim->
concept), part_of/composed_of/kind_of(concept->concept), used_in(formula/concept->
procedure), precedes.
{base_rule}
The passage is numbered elements, one per line, prefixed like [3]. Every node and
edge MUST include "ev": the INTEGER label of the element that best contains it.
Give each node a "local_id" reused in edges. "name" carries the node's text
(Concept/Procedure name, Claim proposition, Formula expression). For an ordered
multi-step Procedure emit ONE Procedure node with an ordered `steps` array, each
{{"name":..,"ev":..}}. Skip narrative/filler.

Passage:
\"\"\"{labeled_text}\"\"\"

Return JSON ONLY:
{_KG_SCHEMA_HINT}
"""
```

- [ ] **Step 4: 跑测试确认通过(并回归既有 prompt 测试)**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py tests/kg/test_extract.py -v`
Expected: PASS(新 3 个 + 既有 `test_prompt_template_valid`、`test_prompt_and_schema_mention_steps` 仍过——`_prompt` 仍含 `"[0] foo"`/`"ev"`/`"steps"`,`_KG_SCHEMA_HINT` 仍含 `"steps"`)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/extract.py backend/tests/kg/test_sa_extraction.py
git commit -m "feat(kg): 重写抽取 prompt——原子claim+连边/validity_scope/猎稀疏边/base_filter"
```

---

### Task 3: `extract_window` 解析 validity_scope + 串 base_filter

**Files:**
- Modify: `backend/app/services/kg/extract.py`(新增 `_parse_validity_scope`;`extract_window` 签名 :198-200、`_prompt` 调用 :207、节点装配 :224-228)
- Test: `backend/tests/kg/test_sa_extraction.py`

- [ ] **Step 1: 写失败测试**

追加:

```python
from app.services.kg.extract import extract_window
from app.services.kg.parsing import SourceElementQ


def _el(idx, text, cs):
    return SourceElementQ(id=f"SE-{idx}", type="paragraph", file="d.md",
                          line_start=idx + 1, line_end=idx + 1,
                          char_start=cs, char_end=cs + len(text), text=text)


_ELS = [_el(0, "In saturation, I_D depends on V_GS.", 0),
        _el(1, "Threshold voltage definition.", 100)]


class _VSFake:
    def chat_json(self, messages, hint):
        return json.dumps({"nodes": [
            {"local_id": "c1", "type": "Claim",
             "name": "I_D depends on V_GS", "ev": 0,
             "validity_scope": {"region": ["saturation"], "assumptions": [],
                                "approximation": "", "range": ""}},
            {"local_id": "k1", "type": "Concept", "name": "threshold voltage",
             "ev": 1, "validity_scope": {"region": ["bogus"]}}],
            "edges": []})


def test_extract_window_parses_validity_scope_claim_only():
    nodes, _ = extract_window(_VSFake(), _ELS, "1", "textbook", win_idx=0)
    by = {n.name: n for n in nodes}
    # claim keeps normalized scope (empty subfields dropped)
    assert by["I_D depends on V_GS"].validity_scope == {"region": ["saturation"]}
    # concept never carries validity_scope (schema: claim/formula only)
    assert by["threshold voltage"].validity_scope == {}


def test_extract_window_backward_compat_no_scope():
    # node JSON without validity_scope still parses -> {}
    class _Old:
        def chat_json(self, m, h):
            return json.dumps({"nodes": [
                {"local_id": "c", "type": "Claim", "name": "I_D depends on V_GS",
                 "ev": 0}], "edges": []})
    nodes, _ = extract_window(_Old(), _ELS, "1", "textbook", win_idx=0)
    assert nodes[0].validity_scope == {}


def test_extract_window_accepts_base_filter():
    nodes, _ = extract_window(_VSFake(), _ELS, "1", "textbook", win_idx=0,
                              base_filter=True)
    assert any(n.type == "Claim" for n in nodes)   # 不崩,正常抽取
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -k "validity_scope_claim or backward_compat or accepts_base_filter" -v`
Expected: FAIL —— `extract_window` 不接受 `base_filter`(TypeError);claim 的 `validity_scope` 为 `{}`(未解析)。

- [ ] **Step 3: 实现解析 + base_filter 串接**

`backend/app/services/kg/extract.py`:在 `_resolve`(:71)前新增 helper:

```python
def _parse_validity_scope(raw: Any) -> Dict[str, Any]:
    """Normalize an LLM validity_scope object -> {} unless real content.
    Keeps only known keys; drops empty lists/strings. claim/formula only."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("region", "assumptions"):
        v = raw.get(key)
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            if items:
                out[key] = items
    for key in ("approximation", "range"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    return out
```

确保文件顶部 imports 含 `Dict`、`Any`(当前 `from typing import Any, Dict, List, Optional, Sequence, Tuple` —— 若缺则补)。

改 `extract_window` 签名(:198-200)加 `base_filter`:

```python
def extract_window(client: Any, elements: List[SourceElementQ], section_path: str,
                   doc_type: str, win_idx: int = 0, refine: bool = False,
                   gleaning_rounds: int = 0, base_filter: bool = False
                   ) -> Tuple[List[Node], List[Edge]]:
```

改 `_prompt` 调用(:207)传 base_filter:

```python
            [{"role": "user",
              "content": _prompt(labeled, section_path, doc_type, base_filter=base_filter)}],
```

在节点装配处(:224-228 `node = Node(...)` 之后、`nodes.append(node)` 之前)加:

```python
            if it["type"] in ("Claim", "Formula"):
                node.validity_scope = _parse_validity_scope(it.get("validity_scope"))
```

- [ ] **Step 4: 跑测试确认通过(+回归)**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py tests/kg/test_extract.py tests/kg/test_gleaning.py tests/kg/test_refine.py -v`
Expected: PASS(新测试过;既有 extract/gleaning/refine 全过——`base_filter` 默认 False,行为不变)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/extract.py backend/tests/kg/test_sa_extraction.py
git commit -m "feat(kg): extract_window 解析 validity_scope(claim/formula)+串 base_filter"
```

---

### Task 4: `build_records` 把 validity_scope 写进 payload

**Files:**
- Modify: `backend/app/services/kg_ingest.py:80`
- Test: `backend/tests/kg/test_sa_extraction.py`

- [ ] **Step 1: 写失败测试**

追加(`build_records` 需要 evidence 能 bind 到 element —— 用与 element 文本完全一致的 quote):

```python
from app.services.kg_ingest import build_records
from app.services.kg.models import Node, Edge, Evidence, KnowledgeGraph


def _ev_for(el):
    return Evidence(file=el.file, char_start=el.char_start, char_end=el.char_end,
                    line_start=el.line_start, line_end=el.line_end, quote=el.text)


def test_build_records_threads_validity_scope_into_payload():
    el = _ELS[0]
    claim = Node(id="c1", type="Claim", name="I_D depends on V_GS",
                 section_path="1", evidence=[_ev_for(el)],
                 validity_scope={"region": ["saturation"]})
    plain = Node(id="c2", type="Claim", name="Threshold voltage definition.",
                 section_path="1", evidence=[_ev_for(_ELS[1])])
    g = KnowledgeGraph(doc_id="d.md", doc_type="textbook",
                       nodes=[claim, plain], edges=[])
    objects, _ = build_records(g, "src1", "Doc", _ELS)
    by = {o["payload"]["name"]: o["payload"] for o in objects}
    assert by["I_D depends on V_GS"]["validity_scope"] == {"region": ["saturation"]}
    assert "validity_scope" not in by["Threshold voltage definition."]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -k threads_validity_scope -v`
Expected: FAIL —— payload 无 `validity_scope` 键(`KeyError`)。

- [ ] **Step 3: 实现**

`backend/app/services/kg_ingest.py`,第 80 行后插入:

```python
        payload = {"name": node.name, "section_path": node.section_path}
        if node.validity_scope:
            payload["validity_scope"] = node.validity_scope
```

(即在现有 `payload = {...}` 与 `if node.steps:` 之间加这 2 行;非空才写,保持 payload 干净。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -k threads_validity_scope -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg_ingest.py backend/tests/kg/test_sa_extraction.py
git commit -m "feat(kg): build_records 把 validity_scope 写进 payload"
```

---

### Task 5: `extract_graph` + 生产调用方串 base_filter

**Files:**
- Modify: `backend/app/services/kg_ingest.py:151-153`(签名)、`:172-174`(submit_window)
- Modify: `backend/app/services/sqlite_repository.py:1401-1409`
- Test: `backend/tests/kg/test_sa_extraction.py`

- [ ] **Step 1: 写失败测试(用 monkeypatch 捕获 base_filter 透传)**

追加:

```python
def test_extract_graph_forwards_base_filter(monkeypatch):
    import app.services.kg_ingest as ingest
    captured = {}

    def fake_extract_window(client, els, section_path, doc_type, idx,
                            refine=False, gleaning_rounds=0, base_filter=False):
        captured["base_filter"] = base_filter
        return [], []

    # submit_window 直接同步调用 fn 并返回一个有 .result() 的 future-like
    class _Now:
        def __init__(self, v): self._v = v
        def result(self): return self._v

    monkeypatch.setattr(ingest, "extract_window", fake_extract_window)
    monkeypatch.setattr(ingest, "submit_window",
                        lambda fn, *a, **k: _Now(fn(*a, **k)))
    text = "Para one has enough content to make a window.\n\nPara two as well."
    ingest.extract_graph(object(), text, "d.md", "textbook", base_filter=True)
    assert captured.get("base_filter") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -k forwards_base_filter -v`
Expected: FAIL —— `extract_graph` 不接受 `base_filter`(TypeError)。

- [ ] **Step 3: 实现 —— extract_graph 签名 + submit_window 透传**

`backend/app/services/kg_ingest.py`,改 `extract_graph` 签名(:151-153):

```python
def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, whitelist=frozenset(),
                  refine: bool = False, gleaning_rounds: int = 0,
                  base_filter: bool = False) -> KnowledgeGraph:
```

改 submit_window 调用(:172-174)加 `base_filter=base_filter`:

```python
        futs = [submit_window(extract_window, client, els, w.section_path,
                              doc_type, idx, refine=refine,
                              gleaning_rounds=gleaning_rounds,
                              base_filter=base_filter)
                for idx, (w, els) in enumerate(pairs)]
```

- [ ] **Step 4: 实现 —— 生产调用方按 tier 传 base_filter**

`backend/app/services/sqlite_repository.py`,在 `whitelist = self.concept_whitelist_terms()`(:1401)后、`graph = kg_ingest.extract_graph(`(:1402)前插入 tier 查询,并在调用里加一行 `base_filter=...`:

```python
            whitelist = self.concept_whitelist_terms()
            with self._connect() as db:
                _nb = db.execute(
                    "SELECT tier FROM notebooks WHERE id=?", (source.notebook_id,)
                ).fetchone()
            base_filter = bool(_nb and _nb["tier"] == "base")
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
                whitelist=whitelist,
                refine=self.settings.kg_refine_enabled,
                gleaning_rounds=(self.settings.kg_gleaning_rounds if self.settings.kg_gleaning_enabled else 0),
                base_filter=base_filter,
            )
```

- [ ] **Step 5: 跑测试确认通过 + py_compile 生产文件**

Run: `cd backend && python -m pytest tests/kg/test_sa_extraction.py -k forwards_base_filter -v && python -m py_compile app/services/sqlite_repository.py app/services/kg_ingest.py`
Expected: PASS + 无编译错误。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/kg_ingest.py backend/app/services/sqlite_repository.py backend/tests/kg/test_sa_extraction.py
git commit -m "feat(kg): extract_graph 与生产调用方按 tier 串 base_filter"
```

---

### Task 6: SA-2 A/B 标定 harness(脚本 + 纯指标函数单测)

**Files:**
- Create: `backend/app/eval/sa_calibration.py`
- Test: `backend/tests/kg/test_sa_calibration_metrics.py`(新建,只测纯函数)

- [ ] **Step 1: 写失败测试(纯指标函数,确定性、无 LLM)**

新建 `backend/tests/kg/test_sa_calibration_metrics.py`:

```python
from app.services.kg.models import Node, Edge
from app.eval.sa_calibration import (
    compound_claim_rate, sparse_edge_count, validity_scope_fill_rate,
)


def _claim(name, vs=None):
    return Node(id=name, type="Claim", name=name, validity_scope=vs or {})


def test_compound_claim_rate():
    claims = [_claim("A and B holds"), _claim("single fact"),
              _claim("x; y"), _claim("plain")]
    # 2 of 4 look compound ("and", ";")
    assert compound_claim_rate(claims) == 0.5


def test_sparse_edge_count():
    edges = [Edge(id="1", type="depends_on", source_id="a", target_id="b"),
             Edge(id="2", type="contrasts_with", source_id="a", target_id="b"),
             Edge(id="3", type="about", source_id="a", target_id="b")]
    # depends_on + contrasts_with = 2 sparse; about not counted
    assert sparse_edge_count(edges) == 2


def test_validity_scope_fill_rate():
    nodes = [_claim("a", {"region": ["sat"]}), _claim("b"),
             Node(id="f", type="Formula", name="f", validity_scope={"range": "DC"}),
             Node(id="c", type="Concept", name="c")]
    # claim+formula = 3 eligible; 2 filled -> 2/3
    assert round(validity_scope_fill_rate(nodes), 3) == round(2 / 3, 3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_sa_calibration_metrics.py -v`
Expected: FAIL —— `app.eval.sa_calibration` 不存在(ImportError)。

- [ ] **Step 3: 创建 harness(纯函数 + main A/B 运行器)**

新建 `backend/app/eval/sa_calibration.py`:

```python
"""SA-2 A/B 标定:同一批 window 上 old vs new 抽取 prompt,逐指标对比。
纯指标函数(下)单测覆盖;main() 是手动运行的测量工具(不入 CI)。

运行(就地读 root .env,不拷文件;scratch 临时库;真 LLM):
    cd backend && PYTHONPATH=. python -m app.eval.sa_calibration \\
        --source-md /abs/path/to/a/chapter.md --doc-type academic_paper
若不给 --source-md,从 prod storage 里 nb-012fb94249 的首个 source md 只读取一章。
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Sequence
from app.services.kg.models import Node, Edge

_SPARSE = {"depends_on", "contrasts_with", "prerequisite_of"}
_COMPOUND = re.compile(r"[；;]| and | as well as |, and ")


def compound_claim_rate(claims: Sequence[Node]) -> float:
    """疑似复合 claim 占比(沿用勘探用启发式:连接词或多句)。"""
    names = [c.name for c in claims if c.name]
    if not names:
        return 0.0
    comp = sum(1 for n in names if _COMPOUND.search(n) or n.count(".") >= 2)
    return comp / len(names)


def sparse_edge_count(edges: Sequence[Edge]) -> int:
    """depends_on + contrasts_with + prerequisite_of 计数。"""
    return sum(1 for e in edges if e.type in _SPARSE)


def validity_scope_fill_rate(nodes: Sequence[Node]) -> float:
    """claim/formula 中 validity_scope 非空的占比。"""
    eligible = [n for n in nodes if n.type in ("Claim", "Formula")]
    if not eligible:
        return 0.0
    return sum(1 for n in eligible if n.validity_scope) / len(eligible)


def summarize(nodes: List[Node], edges: List[Edge],
              input_tokens: int) -> Dict[str, Any]:
    claims = [n for n in nodes if n.type == "Claim"]
    per_rel: Dict[str, int] = {}
    for e in edges:
        per_rel[e.type] = per_rel.get(e.type, 0) + 1
    sparse = sparse_edge_count(edges)
    return {
        "nodes": len(nodes), "edges": len(edges),
        "claims": len(claims),
        "compound_claim_rate": round(compound_claim_rate(claims), 3),
        "validity_scope_fill_rate": round(validity_scope_fill_rate(nodes), 3),
        "sparse_edges": sparse,
        "sparse_per_1k_tok": round(1000 * sparse / max(1, input_tokens), 3),
        "per_relation": per_rel,
        "input_tokens_est": input_tokens,
    }


# --- main A/B 运行器(手动;真 LLM;scratch;就地读 root .env)---

def _load_root_env() -> None:
    import os
    path = "/Users/hzf/workspace/silicon_notebook/.env"
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# 改动前 `_prompt` 的内联副本(A/B 的 OLD 臂)。复制自 git 基线 0852314 的
# extract.py:_prompt。NEW 臂 import 改后的 extract._prompt。
def _old_prompt(labeled_text: str, section_path: str, doc_type: str,
                base_filter: bool = False) -> str:
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure ... [完整粘贴 git show 0852314:backend/app/services/kg/extract.py 的 _prompt 函数体;
base_filter 形参仅为签名对齐,旧体不使用]."""


def _run_arm(prompt_fn, windows, doc_type: str) -> Dict[str, Any]:
    import os
    from app.core.config import Settings
    from app.services.kg.client import safe_json
    from app.services.kg import extract as E
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(Settings())
    client = repo.llm_client
    all_nodes: List[Node] = []
    all_edges: List[Edge] = []
    tok = 0
    # monkeypatch extract._prompt 为本臂的 prompt,复用 extract_window 的解析
    orig = E._prompt
    E._prompt = prompt_fn
    try:
        for els, section_path in windows:
            tok += sum(len(e.text) for e in els) // 4  # 粗略 token 估计
            ns, es = E.extract_window(client, els, section_path, doc_type,
                                      win_idx=0, base_filter=True)
            all_nodes += ns
            all_edges += es
    finally:
        E._prompt = orig
    return summarize(all_nodes, all_edges, tok)


def main() -> None:
    import argparse, json, tempfile, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-md", default="")
    ap.add_argument("--doc-type", default="academic_paper")
    ap.add_argument("--max-windows", type=int, default=12)
    args = ap.parse_args()

    _load_root_env()
    d = tempfile.mkdtemp(prefix="sacal_")
    os.environ["DATABASE_URL"] = f"sqlite:///{d}/cal.db"
    os.environ["SILICON_NOTEBOOK_STORAGE_DIR"] = f"{d}/storage"
    os.environ["LLM_LOG_ENABLED"] = "false"
    os.environ["EVENT_LOG_ENABLED"] = "false"

    from app.services.kg.parsing import windows_with_elements
    if args.source_md:
        raw = open(args.source_md, encoding="utf-8").read()
    else:
        raise SystemExit("请用 --source-md 指定一章 markdown(只读;勿用 prod 库)")
    pairs = [(els, w.section_path)
             for w, els in windows_with_elements(raw, "cal.md", None, 9000, 450)
             if els][: args.max_windows]
    print(f"[cal] {len(pairs)} windows")
    old = _run_arm(_old_prompt, pairs, args.doc_type)
    new = _run_arm(__import__("app.services.kg.extract", fromlist=["_prompt"])._prompt,
                   pairs, args.doc_type)
    print(json.dumps({"OLD": old, "NEW": new}, ensure_ascii=False, indent=2))
    # 门槛判定
    gate = {
        "sparse_density_up_1.5x":
            new["sparse_per_1k_tok"] >= 1.5 * max(1e-9, old["sparse_per_1k_tok"]),
        "compound_below_15pct": new["compound_claim_rate"] < 0.15,
        "validity_scope_filled": new["validity_scope_fill_rate"] > 0,
    }
    print(json.dumps({"GATE": gate, "PASS": all(gate.values())},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

> 注:`_old_prompt` 函数体在实现时用 `git show 0852314:backend/app/services/kg/extract.py` 取改动前 `_prompt` 全文粘贴(本计划不重复 200 行旧 prompt)。token 估计用 `chars//4` 粗略代理(A/B 同口径即可比较)。

- [ ] **Step 4: 跑纯函数测试确认通过**

Run: `cd backend && python -m pytest tests/kg/test_sa_calibration_metrics.py -v`
Expected: PASS(3 passed)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/eval/sa_calibration.py backend/tests/kg/test_sa_calibration_metrics.py
git commit -m "feat(eval): SA-2 A/B 标定 harness(纯指标函数 + 手动运行器)"
```

---

### Task 7: 全门禁 + 收尾

**Files:** 无新增(验证 + 文档)

- [ ] **Step 1: 跑全 KG 测试 + 项目门禁**

Run: `cd backend && python -m pytest tests/kg/ -v`
Expected: 全 PASS(新测试 + 既有 extract/gleaning/refine/merge/ingest 无回归)。

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/sa-extraction && bash scripts/check.sh`
Expected: EXIT 0(py_compile + backend smoke + node + tsc;FE 不受影响)。

- [ ] **Step 2: 手动跑一次 A/B 标定(真 LLM,小切片)**

准备一章分析电路 markdown(从 prod storage 只读复制,或自备),然后:

Run: `cd backend && PYTHONPATH=. python -m app.eval.sa_calibration --source-md /abs/chapter.md --doc-type academic_paper`
Expected: 打印 OLD/NEW 指标对比 + GATE 判定。记录到下方「标定结果」。

- [ ] **Step 3: 把标定结果回填本计划 + 更新 storage-decouple-roadmap memory**

在本文件「标定结果」节填入 OLD/NEW 数字与 PASS/FAIL;若 PASS,记「new prompt 为底库重抽(SA-3)候选」;若 FAIL,记不达标项,决定迭代 prompt 或启用后备 C(边 gleaning)。

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "docs(sa): 回填 A/B 标定结果"
```

---

## 标定结果(Task 7 回填)

| 指标 | OLD | NEW | 门槛 | 判定 |
|---|---|---|---|---|
| sparse_per_1k_tok | — | — | NEW ≥ 1.5× OLD | — |
| compound_claim_rate | — | — | NEW < 0.15 | — |
| validity_scope_fill_rate | 0(必然) | — | NEW > 0 | — |
| token/window(est) | — | — | NEW ≤ 1.8× OLD | — |

**结论:** _(待回填)_

---

## 自检(against spec)

- **Spec 覆盖**:validity_scope(Task1/3/4)、原子claim+连边(Task2 prompt)、放宽并猎稀疏边(Task2 prompt;边端点零代码改已核实)、base meta 过滤(Task2/3/5)、A/B 标定+指标+门槛(Task6/7)。**spec「canonicalize 保 scope」作废**(canonicalize 不碰 claim/formula,已核实)——非缺口,是事实修正。
- **占位扫描**:唯一「待粘贴」是 Task6 的 `_old_prompt` 旧体(明确用 `git show 0852314:.../extract.py` 取),属可执行指令而非占位;其余代码全量给出。
- **类型一致**:`validity_scope: Dict[str,Any]`(Node)贯穿 `_parse_validity_scope`→payload→指标函数;`base_filter: bool` 贯穿 `_prompt`/`extract_window`/`extract_graph`/调用方;指标函数名 `compound_claim_rate`/`sparse_edge_count`/`validity_scope_fill_rate`/`summarize` 前后一致。
- **不变量**:不碰检索/答案/前端;标定用 scratch DB 不污染 prod;不拷 .env;schema 不改。
