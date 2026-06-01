# KG Golden Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the knowledge-graph golden subsystem — KG data model, shared window-based KG extraction (verbatim evidence grounding), Concept canonicalization, a qwen-based golden *generator*, and a graph-matching *evaluator* — so we can produce reviewable gold KGs for engram/cmos and score any predicted KG against them.

**Architecture:** Reuse the existing S1 parse (char offsets + 3-line `$$` formula fix) and S2 section tree. New `backend/app/services/kg/` holds the shared machinery: window a chapter (N/M chars), send each window to an LLM that returns a KG fragment (4 node types + typed edges + verbatim evidence quotes), ground each quote to a char span (drop ungroundable), then merge + canonicalize Concept nodes. The **gold generator** drives this with qwen3.7-max (independent of the product's deepseek). A separate `kg_eval/` scores a predicted KG vs gold by graph matching (type + name-similarity + evidence-overlap), tolerant of exact spans and using only the 4 coarse node types.

**Tech Stack:** Python, pydantic v2, PyYAML, the `openai` SDK (two endpoints: qwen for gold, deepseek for product), pytest. Fake clients for deterministic unit tests.

**Spec:** `docs/superpowers/specs/2026-06-01-kg-redesign-design.md`. The product extraction pipeline is a SEPARATE later plan that reuses `kg/` with the deepseek client.

**Model config:** gold generator reads `GOLDGEN_OPENAI_COMPAT_{BASE_URL,API_KEY,MODEL}` from `.env` (qwen3.7-max, already set, gitignored). Product reads `OPENAI_COMPAT_*` (deepseek).

---

## File Structure

```
backend/app/services/kg/
  __init__.py
  models.py          # Evidence, Node, Edge, KnowledgeGraph (+ to_yaml dict). gold & product shared.
  client.py          # make_client(env_prefix) -> KGClient(chat_json) from env config
  windowing.py       # chapter -> [(win_start_abs, win_end_abs, section_path)] N/M windows
  extract.py         # extract_window(client, source_text, win, section_path, doc_type) -> (nodes, edges)
  canonicalize.py    # merge Concept nodes across fragments; rewire edges
  emit.py            # KnowledgeGraph -> ordered yaml
backend/app/services/kg_eval/
  __init__.py
  match.py           # node/edge graph matching
  score.py           # score_kg(gold, pred) -> node/edge P/R/F1 + evidence grounding
backend/tests/kg/
  __init__.py  conftest.py  test_models.py  test_windowing.py
  test_extract.py  test_canonicalize.py  test_eval.py
scripts/kg_goldgen.py                       # per-chapter goldgen orchestrator (qwen)
docs/superpowers/specs/2026-06-01-kg-generation-contract.md   # operational node/edge definitions
fangan/testcases_kg/<doc>/<chapter>/gold_kg.yaml              # generated DRAFT gold (for human curation)
```

Node types: `Concept | Claim | Formula | Procedure`. Edge types: `defines, part_of, composed_of, contrasts_with, kind_of, about, supports, derived_from, depends_on, prerequisite_of, used_in, precedes`.

---

## Task 0: Scaffold + generation contract + client factory

**Files:**
- Create: `backend/app/services/kg/__init__.py`, `backend/app/services/kg_eval/__init__.py`, `backend/tests/kg/__init__.py`, `backend/tests/kg/conftest.py`, `backend/app/services/kg/client.py`
- Create: `docs/superpowers/specs/2026-06-01-kg-generation-contract.md`

- [ ] **Step 1: Empty package markers** — create the three `__init__.py` (kg, kg_eval, tests/kg) empty, plus `kg/__init__.py` with a one-line docstring `"""Knowledge-graph extraction machinery (shared by gold generator and product)."""`.

- [ ] **Step 2: Write the generation contract** — `docs/superpowers/specs/2026-06-01-kg-generation-contract.md`:

```markdown
# KG Generation Contract — operational definitions

The authoritative, operational definitions of every node and edge type. Both the
gold generator and human curators apply THESE rules (this is the root fix for the
old "fine types with no definitions" problem).

## Node types (exactly 4)
- **Concept** — a named noun-like entity (term, concept, method, component, device,
  system, material). Test: it can be a grammatical subject/object and recurs across
  sentences. Attributes: name, aliases[], kind (free tag), definition (short).
- **Claim** — a truth-evaluable assertion ABOUT one or more Concepts (a claim,
  finding, principle, mechanism, or definitional statement). Test: it has a predicate
  and asserts one fact. Attributes: statement, quantitative_values{}, polarity.
- **Formula** — an equation/expression. Test: contains `=` or a math operator.
  Attributes: expression, variables{symbol: meaning}, role (what it computes/states).
- **Procedure** — an ordered process (fabrication flow, worked-example solution,
  derivation chain). Test: >= 2 ordered steps. Attributes: name, steps[] (ordered).

## Edge types (source_type -> target_type : trigger)
- defines: Claim -> Concept : the claim states what the concept IS.
- part_of / composed_of: Concept -> Concept : structural containment.
- contrasts_with: Concept -> Concept : explicit contrast/vs.
- kind_of: Concept -> Concept : taxonomic is-a.
- about: Claim|Formula -> Concept : the statement/formula is about the concept.
- supports: Claim|Formula -> Claim : evidence/derivation supporting a claim.
- derived_from: Formula -> Formula : derived from a prior formula.
- depends_on / prerequisite_of: Concept|Formula -> Concept : needs the target first.
- used_in: Formula -> Procedure : the formula is used by the procedure/example.
- precedes: within a Procedure's steps, ordering.

## Evidence (hard invariant)
Every node and edge carries evidence: one or more verbatim source spans such that
source_text[char_start:char_end] == quote. Ungroundable items are dropped.

## Canonicalization
Concept nodes with the same normalized name (or listed alias) merge into ONE node
across sections and documents; mentions[] records every source span.
```

- [ ] **Step 3: Client factory** — `backend/app/services/kg/client.py`:

```python
"""Minimal OpenAI-compatible JSON client, configured from an env prefix so the
gold generator (GOLDGEN_) and product ("") can use different endpoints/models."""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional


class KGClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120):
        self.base_url, self.api_key, self.model, self.timeout = base_url, api_key, model, timeout
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=self.timeout)
        return self._client

    def chat_json(self, prompt: str) -> str:
        resp = self._ensure().chat.completions.create(
            model=self.model, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}])
        return resp.choices[0].message.content or "{}"


def make_client(env_prefix: str = "") -> KGClient:
    g = lambda k: os.environ.get(env_prefix + k, "")
    return KGClient(g("OPENAI_COMPAT_BASE_URL"), g("OPENAI_COMPAT_API_KEY"),
                    g("OPENAI_COMPAT_MODEL"))


def safe_json(raw: str) -> dict:
    if not raw:
        return {}
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        out = json.loads(t)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}
```

- [ ] **Step 4: conftest** — `backend/tests/kg/conftest.py`:

```python
import os, pathlib, pytest, yaml
REPO = pathlib.Path(__file__).resolve().parents[3]
GOLD = REPO / "fangan" / "testcases"
SRC = pathlib.Path(os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser"))
SRC_PATHS = {
    "engram_paper_mineru.md": SRC / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SRC
    / "notebook_papers_mineru_skill_results"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}

@pytest.fixture
def source_text():
    def _load(name):
        p = SRC_PATHS[name]
        if not p.exists():
            pytest.skip(f"source missing: {p}")
        return p.read_text(encoding="utf-8")
    return _load
```

- [ ] **Step 5: Verify + commit**

Run: `cd backend && python -m pytest tests/kg -q` → `no tests ran` (collection clean).
Run: `cd backend && python -c "from app.services.kg.client import make_client; c=make_client('GOLDGEN_'); print('goldgen configured:', c.configured, c.model)"` → `goldgen configured: True qwen3.7-max`

```bash
git add backend/app/services/kg backend/app/services/kg_eval backend/tests/kg \
        docs/superpowers/specs/2026-06-01-kg-generation-contract.md
git commit -m "chore(kg): scaffold kg subsystem + generation contract + client factory"
```

---

## Task 1: KG data model (`models.py`, `emit.py`)

**Files:** Create `backend/app/services/kg/models.py`, `backend/app/services/kg/emit.py`; Test `backend/tests/kg/test_models.py`.

- [ ] **Step 1: Failing test** — `test_models.py`:

```python
from app.services.kg.models import Evidence, Node, Edge, KnowledgeGraph
from app.services.kg.emit import to_yaml
import yaml

def test_kg_roundtrips_and_emits_ordered():
    n1 = Node(id="C1", type="Concept", name="depletion region",
              evidence=[Evidence(file="x.md", char_start=0, char_end=16,
                                 line_start=1, line_end=1, quote="depletion region")])
    n2 = Node(id="F1", type="Formula", attrs={"expression": "x_d = x_n - x_p"},
              evidence=[Evidence(file="x.md", char_start=20, char_end=35,
                                 line_start=2, line_end=2, quote="x_d = x_n - x_p")])
    g = KnowledgeGraph(doc_id="cmos", doc_type="textbook", nodes=[n1, n2],
                       edges=[Edge(id="E1", type="about", source_id="F1", target_id="C1")])
    d = yaml.safe_load(to_yaml(g))
    assert list(d.keys()) == ["doc_id", "doc_type", "nodes", "edges"]
    assert d["nodes"][0]["type"] == "Concept"
    assert d["nodes"][0]["evidence"][0]["quote"] == "depletion region"
    assert d["edges"][0]["source_id"] == "F1"
```

- [ ] **Step 2: Run → FAIL** (`cd backend && python -m pytest tests/kg/test_models.py -q`).

- [ ] **Step 3: Implement `models.py`**:

```python
"""KG data model. 4 node types, typed edges, verbatim evidence spans."""
from __future__ import annotations
from typing import Any, Dict, List, Literal
from pydantic import BaseModel, Field

NodeType = Literal["Concept", "Claim", "Formula", "Procedure"]

class Evidence(BaseModel):
    file: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    quote: str

class Node(BaseModel):
    id: str
    type: NodeType
    name: str = ""              # Concept/Procedure name; "" for Claim/Formula
    attrs: Dict[str, Any] = Field(default_factory=dict)
    section_path: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    mentions: List[Evidence] = Field(default_factory=list)

class Edge(BaseModel):
    id: str
    type: str
    source_id: str
    target_id: str
    evidence: List[Evidence] = Field(default_factory=list)

class KnowledgeGraph(BaseModel):
    doc_id: str
    doc_type: str
    nodes: List[Node] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = self.model_dump(mode="python", exclude_none=True)
        return {k: d[k] for k in ("doc_id", "doc_type", "nodes", "edges")}
```

- [ ] **Step 4: Implement `emit.py`**:

```python
"""KnowledgeGraph -> ordered YAML."""
from __future__ import annotations
import yaml
from app.services.kg.models import KnowledgeGraph

def to_yaml(g: KnowledgeGraph) -> str:
    return yaml.safe_dump(g.to_dict(), sort_keys=False, allow_unicode=True, width=1000)
```

- [ ] **Step 5: Run → PASS; commit**

```bash
git add backend/app/services/kg/models.py backend/app/services/kg/emit.py backend/tests/kg/test_models.py
git commit -m "feat(kg): KG data model (4 node types, edges, evidence) + yaml emit"
```

---

## Task 2: Windowing (`windowing.py`)

Reuse the existing parser/section-tree; produce contiguous N/M windows over a chapter's prose span, each tagged with its section_path.

**Files:** Create `backend/app/services/kg/windowing.py`; Test `backend/tests/kg/test_windowing.py`.

- [ ] **Step 1: Failing test** — `test_windowing.py`:

```python
from app.services.kg.windowing import make_windows

def test_windows_cover_with_overlap_and_section():
    # 25-char "sections": one heading + body
    text = "# 1 Intro\n\n" + ("A" * 50) + "\n\n# 2 Body\n\n" + ("B" * 50) + "\n"
    wins = make_windows(text, source_file="x.md", line_range=None, n=30, m=6)
    assert wins, "expected windows"
    # every window is a contiguous source slice with a section path
    for w in wins:
        assert 0 <= w.char_start < w.char_end <= len(text)
        assert w.section_path
    # overlap: consecutive windows in the same section overlap by ~m
    same = [w for w in wins if w.section_path == wins[0].section_path]
    if len(same) >= 2:
        assert same[1].char_start < same[0].char_end
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `windowing.py`:

```python
"""Chapter -> contiguous N-char windows (M overlap) over prose, tagged by section."""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel
from app.services.qiefen.source_elements import parse_elements
from app.services.qiefen.section_tree import build_section_tree

class Window(BaseModel):
    char_start: int
    char_end: int
    section_path: str
    file: str

def _section_of_line(line: int, sec_by_line):
    chosen = ""
    for hline, path in sec_by_line:
        if hline <= line:
            chosen = path
        else:
            break
    return chosen

def make_windows(text: str, source_file: str, line_range: Optional[List[int]],
                 n: int = 9000, m: int = 450) -> List[Window]:
    elements = parse_elements(text, source_file, line_range)
    sections = build_section_tree(elements)
    headings = [e for e in elements if e.type == "heading"]
    sec_by_line = sorted((h.line_start, s.path) for h, s in zip(headings, sections))
    prose = [e for e in elements if e.type in ("paragraph", "list_item", "formula",
                                               "table", "figure_caption")]
    # group prose elements by enclosing section, window each section's span
    windows: List[Window] = []
    by_sec = {}
    for e in prose:
        path = _section_of_line(e.line_start, sec_by_line)
        by_sec.setdefault(path, []).append(e)
    step = max(1, n - m)
    for path, els in by_sec.items():
        start = min(e.char_start for e in els)
        end = max(e.char_end for e in els)
        s = start
        while s < end:
            windows.append(Window(char_start=s, char_end=min(s + n, end),
                                  section_path=path, file=source_file))
            if s + n >= end:
                break
            s += step
    windows.sort(key=lambda w: w.char_start)
    return windows
```

- [ ] **Step 4: Run → PASS; commit**

```bash
git add backend/app/services/kg/windowing.py backend/tests/kg/test_windowing.py
git commit -m "feat(kg): N/M windowing over chapter prose, section-tagged"
```

---

## Task 3: Window KG extraction (`extract.py`)

The core: window text -> LLM -> KG fragment with verbatim evidence -> ground to char spans.

**Files:** Create `backend/app/services/kg/extract.py`; Test `backend/tests/kg/test_extract.py`.

- [ ] **Step 1: Failing test** (FakeClient; the hard invariant + grounding + drop-ungroundable):

```python
import json
from app.services.kg.extract import extract_window

SRC = "An analog signal is defined over a continuous range. C_j = C_j0 here."

class Fake:
    def chat_json(self, prompt):
        return json.dumps({"nodes": [
            {"local_id": "a", "type": "Concept", "name": "analog signal",
             "evidence": "analog signal"},
            {"local_id": "b", "type": "Formula", "attrs": {"expression": "C_j = C_j0"},
             "evidence": "C_j = C_j0"},
            {"local_id": "c", "type": "Claim", "attrs": {"statement": "x"},
             "evidence": "NOT IN SOURCE"}],          # ungroundable -> dropped
            "edges": [
            {"type": "about", "source": "b", "target": "a", "evidence": "C_j = C_j0"},
            {"type": "about", "source": "b", "target": "zzz", "evidence": ""}]})  # bad endpoint

def test_extract_grounds_evidence_and_drops_ungroundable():
    nodes, edges = extract_window(Fake(), SRC, 0, len(SRC), "1 > 1.1", "textbook")
    assert len(nodes) == 2                       # claim 'c' dropped (ungroundable)
    for n in nodes:
        e = n.evidence[0]
        assert SRC[e.char_start:e.char_end] == e.quote   # hard invariant
    assert len(edges) == 1                        # bad-endpoint edge dropped
    assert edges[0].type == "about"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `extract.py`:

```python
"""Window -> LLM KG fragment -> grounded nodes/edges. Local ids are kept so the
caller can wire edges; the LLM's evidence quotes are located verbatim in the
window (drop ungroundable). Node types constrained to the 4; edges to the vocab."""
from __future__ import annotations
import re
from typing import Any, List, Optional, Tuple
from app.services.kg.client import safe_json
from app.services.kg.models import Edge, Evidence, Node

NODE_TYPES = {"Concept", "Claim", "Formula", "Procedure"}
EDGE_TYPES = {"defines", "part_of", "composed_of", "contrasts_with", "kind_of",
              "about", "supports", "derived_from", "depends_on", "prerequisite_of",
              "used_in", "precedes"}

def _locate(window: str, quote: str) -> Optional[Tuple[int, str]]:
    if not quote or len(quote.strip()) < 3:
        return None
    i = window.find(quote)
    if i >= 0:
        return i, quote
    pat = r"\s+".join(re.escape(t) for t in quote.split())
    m = re.search(pat, window)
    return (m.start(), m.group(0)) if m else None

def _prompt(window_text: str, section_path: str, doc_type: str) -> str:
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure (see definitions: Concept=named entity; Claim=truth-evaluable assertion
about concepts; Formula=equation; Procedure=ordered process). Edges (source->target):
defines(Claim->Concept), about(Claim|Formula->Concept), supports(Claim|Formula->Claim),
part_of/composed_of/contrasts_with/kind_of(Concept->Concept), derived_from(Formula->
Formula), depends_on/prerequisite_of, used_in(Formula->Procedure), precedes.

Every node and edge MUST include "evidence": an EXACT verbatim substring copied from
the passage. Give each node a "local_id" you reuse in edges. Skip narrative/filler.

Passage:
\"\"\"{window_text}\"\"\"

Return JSON ONLY:
{{"nodes":[{{"local_id":"..","type":"..","name":"..","attrs":{{}},"evidence":"<verbatim>"}}],
 "edges":[{{"type":"..","source":"<local_id>","target":"<local_id>","evidence":"<verbatim>"}}]}}
"""

def extract_window(client: Any, source_text: str, win_start: int, win_end: int,
                   section_path: str, doc_type: str) -> Tuple[List[Node], List[Edge]]:
    window = source_text[win_start:win_end]
    try:
        data = safe_json(client.chat_json(_prompt(window, section_path, doc_type)))
    except Exception:
        return [], []
    nodes: List[Node] = []
    by_local = {}
    for it in (data.get("nodes") or []):
        if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
            continue
        loc = _locate(window, str(it.get("evidence", "")))
        if loc is None:
            continue
        local, matched = loc
        cstart = win_start + local
        line = source_text.count("\n", 0, cstart) + 1
        nid = f"L{win_start}-{len(nodes)}"
        ev = Evidence(file="", char_start=cstart, char_end=cstart + len(matched),
                      line_start=line, line_end=source_text.count("\n", 0, cstart + len(matched)) + 1,
                      quote=matched)
        nodes.append(Node(id=nid, type=it["type"], name=str(it.get("name", "")),
                          attrs=it.get("attrs") or {}, section_path=section_path, evidence=[ev]))
        if it.get("local_id"):
            by_local[str(it["local_id"])] = nid
    edges: List[Edge] = []
    for it in (data.get("edges") or []):
        if not isinstance(it, dict) or it.get("type") not in EDGE_TYPES:
            continue
        s = by_local.get(str(it.get("source"))); t = by_local.get(str(it.get("target")))
        if not s or not t or s == t:
            continue
        edges.append(Edge(id=f"E{win_start}-{len(edges)}", type=it["type"],
                          source_id=s, target_id=t))
    return nodes, edges
```

- [ ] **Step 4: Run → PASS; commit**

```bash
git add backend/app/services/kg/extract.py backend/tests/kg/test_extract.py
git commit -m "feat(kg): window KG extraction with verbatim evidence grounding"
```

---

## Task 4: Concept canonicalization (`canonicalize.py`)

**Files:** Create `backend/app/services/kg/canonicalize.py`; Test `backend/tests/kg/test_canonicalize.py`.

- [ ] **Step 1: Failing test**:

```python
from app.services.kg.models import Node, Edge, Evidence
from app.services.kg.canonicalize import canonicalize

def _c(nid, name):
    return Node(id=nid, type="Concept", name=name,
                evidence=[Evidence(file="x", char_start=0, char_end=1, line_start=1, line_end=1, quote="z")])

def test_merges_concepts_by_normalized_name_and_rewires_edges():
    nodes = [_c("A", "Depletion Region"), _c("B", "depletion  region"),
             Node(id="F", type="Formula", attrs={"expression": "x=1"},
                  evidence=[Evidence(file="x", char_start=2, char_end=3, line_start=1, line_end=1, quote="x")])]
    edges = [Edge(id="e1", type="about", source_id="F", target_id="B")]
    g_nodes, g_edges = canonicalize(nodes, edges, doc_id="d")
    concepts = [n for n in g_nodes if n.type == "Concept"]
    assert len(concepts) == 1                      # A & B merged
    assert len(concepts[0].mentions) == 2          # both spans recorded
    assert g_edges[0].target_id == concepts[0].id  # edge rewired to canonical id
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `canonicalize.py`:

```python
"""Merge Concept nodes by normalized name/alias across fragments; rewire edges."""
from __future__ import annotations
import re
from typing import List, Tuple
from app.services.kg.models import Edge, Node

def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", name.lower())).strip()

def canonicalize(nodes: List[Node], edges: List[Edge], doc_id: str) -> Tuple[List[Node], List[Edge]]:
    canon: dict = {}          # normalized name -> canonical Concept node
    remap: dict = {}          # every original node id -> final id
    out: List[Node] = []
    cn = 0
    for n in nodes:
        if n.type == "Concept" and _norm(n.name):
            key = _norm(n.name)
            if key in canon:
                c = canon[key]
                c.mentions.extend(n.evidence + n.mentions)
                if n.name != c.name and n.name not in (c.attrs.get("aliases") or []):
                    c.attrs.setdefault("aliases", []).append(n.name)
                remap[n.id] = c.id
            else:
                cn += 1
                new_id = f"{doc_id}:C{cn}"
                remap[n.id] = new_id
                n.id = new_id
                n.mentions = list(n.evidence)
                canon[key] = n
                out.append(n)
        else:
            remap[n.id] = n.id
            out.append(n)
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

- [ ] **Step 4: Run → PASS; commit**

```bash
git add backend/app/services/kg/canonicalize.py backend/tests/kg/test_canonicalize.py
git commit -m "feat(kg): Concept canonicalization + edge rewiring"
```

---

## Task 5: Graph-matching evaluator (`kg_eval/`)

**Files:** Create `backend/app/services/kg_eval/match.py`, `backend/app/services/kg_eval/score.py`; Test `backend/tests/kg/test_eval.py`.

- [ ] **Step 1: Failing test** (gold-vs-gold ~1.0; partial match):

```python
from app.services.kg.models import KnowledgeGraph, Node, Edge, Evidence
from app.services.kg_eval.score import score_kg

def _c(nid, name, cs):
    return Node(id=nid, type="Concept", name=name,
                evidence=[Evidence(file="x", char_start=cs, char_end=cs+3, line_start=1, line_end=1, quote="abc")])

def _g():
    return KnowledgeGraph(doc_id="d", doc_type="textbook",
        nodes=[_c("C1", "analog signal", 0), _c("C2", "digital signal", 10)],
        edges=[Edge(id="e1", type="contrasts_with", source_id="C1", target_id="C2")])

def test_gold_vs_gold_is_perfect():
    r = score_kg(_g(), _g())
    assert r["nodes"]["f1"] == 1.0
    assert r["edges"]["f1"] == 1.0

def test_partial_node_and_missing_edge():
    pred = KnowledgeGraph(doc_id="d", doc_type="textbook",
        nodes=[_c("P1", "analog signal", 0)], edges=[])   # 1/2 nodes, 0 edges
    r = score_kg(_g(), pred)
    assert r["nodes"]["recall"] == 0.5
    assert r["edges"]["recall"] == 0.0
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `match.py`**:

```python
"""Graph matching: align pred nodes to gold by (type + name-sim + evidence overlap)."""
from __future__ import annotations
import re
from difflib import SequenceMatcher

def _norm(s): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (s or "").lower())).strip()

def _span_overlap(a, b):
    inter = max(0, min(a.char_end, b.char_end) - max(a.char_start, b.char_start))
    return inter > 0

def _node_key(n):
    return _norm(n.name) or _norm(str(n.attrs.get("expression") or n.attrs.get("statement") or ""))

def node_sim(g, p):
    if g.type != p.type:
        return 0.0
    name = SequenceMatcher(None, _node_key(g), _node_key(p)).ratio()
    ev = 1.0 if any(_span_overlap(ge, pe) for ge in g.evidence for pe in p.evidence) else 0.0
    return max(name, 0.5 * name + 0.5 * ev)

def match_nodes(gold, pred, thresh=0.6):
    pairs = []
    used = set()
    for g in gold:
        best, bi = 0.0, None
        for i, p in enumerate(pred):
            if i in used:
                continue
            s = node_sim(g, p)
            if s > best:
                best, bi = s, i
        if bi is not None and best >= thresh:
            used.add(bi)
            pairs.append((g.id, pred[bi].id))
    return dict(pairs)   # gold_id -> pred_id
```

- [ ] **Step 4: Implement `score.py`**:

```python
"""score_kg(gold, pred) -> node/edge P/R/F1 + evidence grounding."""
from __future__ import annotations
from app.services.kg_eval.match import match_nodes

def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else (1.0 if fn == 0 else 0.0)
    r = tp / (tp + fn) if tp + fn else (1.0 if fp == 0 else 0.0)
    f = 2 * p * r / (p + r) if p + r else (1.0 if tp == fp == fn == 0 else 0.0)
    return {"precision": p, "recall": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}

def score_kg(gold, pred):
    g2p = match_nodes(gold.nodes, pred.nodes)
    n_tp = len(g2p)
    nodes = _prf(n_tp, len(pred.nodes) - n_tp, len(gold.nodes) - n_tp)
    # edges: map gold endpoints to pred via g2p; matched if same pred endpoints+type exist
    pred_edges = {(e.type, e.source_id, e.target_id) for e in pred.edges}
    e_tp = 0
    for e in gold.edges:
        ps, pt = g2p.get(e.source_id), g2p.get(e.target_id)
        if ps and pt and (e.type, ps, pt) in pred_edges:
            e_tp += 1
    edges = _prf(e_tp, len(pred.edges) - e_tp, len(gold.edges) - e_tp)
    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 5: Run → PASS; commit**

```bash
git add backend/app/services/kg_eval backend/tests/kg/test_eval.py
git commit -m "feat(kg-eval): graph-matching scorer (node/edge P/R/F1)"
```

---

## Task 6: Gold generator orchestrator (`scripts/kg_goldgen.py`)

**Files:** Create `scripts/kg_goldgen.py`.

- [ ] **Step 1: Implement** — `scripts/kg_goldgen.py`:

```python
#!/usr/bin/env python3
"""Generate DRAFT gold KGs for the testcase chapters using the qwen gold model.
Output: fangan/testcases_kg/<doc>/<chapter>/gold_kg.yaml  (for human curation).

Usage: PYTHONPATH=backend python scripts/kg_goldgen.py --chapters engram/ch00_abstract
"""
import argparse, os, pathlib, sys, concurrent.futures as cf
import yaml
from dotenv import load_dotenv

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
load_dotenv(REPO / ".env")

from app.services.kg.client import make_client
from app.services.kg.windowing import make_windows
from app.services.kg.extract import extract_window
from app.services.kg.canonicalize import canonicalize
from app.services.kg.models import KnowledgeGraph
from app.services.kg.emit import to_yaml

GOLD = REPO / "fangan" / "testcases"
OUT = REPO / "fangan" / "testcases_kg"
SRC = pathlib.Path(os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser"))
SRC_PATHS = {
    "engram_paper_mineru.md": SRC / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SRC
    / "notebook_papers_mineru_skill_results" / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}
DOC_TYPE = {"article_research": "academic", "textbook": "textbook"}

def gen_chapter(rel, client, n, m):
    meta = yaml.safe_load((GOLD / rel / "gold.yaml").read_text())["source_meta"]
    src = SRC_PATHS[meta["source_file"]].read_text(encoding="utf-8")
    dt = DOC_TYPE.get(meta["profile"], "academic")
    wins = make_windows(src, meta["source_file"], meta.get("source_line_range"), n, m)
    nodes, edges = [], []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for ns, es in pool.map(lambda w: extract_window(client, src, w.char_start,
                                                         w.char_end, w.section_path, dt), wins):
            nodes += ns; edges += es
    for e in nodes + edges:
        for ev in e.evidence:
            ev.file = meta["source_file"]
    nodes, edges = canonicalize(nodes, edges, doc_id=meta.get("source_id", "doc"))
    g = KnowledgeGraph(doc_id=meta.get("source_id", "doc"), doc_type=dt, nodes=nodes, edges=edges)
    dst = OUT / rel; dst.mkdir(parents=True, exist_ok=True)
    (dst / "gold_kg.yaml").write_text(to_yaml(g), encoding="utf-8")
    return rel, len(nodes), len(edges)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", default="")
    ap.add_argument("--n", type=int, default=9000); ap.add_argument("--m", type=int, default=450)
    a = ap.parse_args()
    client = make_client("GOLDGEN_")
    assert client.configured, "GOLDGEN_OPENAI_COMPAT_* not set in .env"
    only = {c.strip() for c in a.chapters.split(",") if c.strip()}
    chapters = [str(p.parent.relative_to(GOLD)) for p in sorted(GOLD.glob("*/ch*/gold.yaml"))]
    for rel in chapters:
        if only and rel not in only:
            continue
        r, nn, ne = gen_chapter(rel, client, a.n, a.m)
        print(f"{r}: {nn} nodes, {ne} edges -> fangan/testcases_kg/{r}/gold_kg.yaml")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add `python-dotenv` if missing**

Run: `cd backend && python -c "import dotenv" 2>/dev/null || pip install python-dotenv` (dotenv loads `.env` for the script).

- [ ] **Step 3: Smoke on ONE small chapter (validation-allowed: engram/ch00_abstract)**

Run: `cd <repo> && PYTHONPATH=backend python scripts/kg_goldgen.py --chapters engram/ch00_abstract`
Expected: prints node/edge counts; writes `fangan/testcases_kg/engram/ch00_abstract/gold_kg.yaml`.
Verify the span invariant on the output:
```bash
PYTHONPATH=backend python -c "
import yaml; from scripts.kg_goldgen import SRC_PATHS
g=yaml.safe_load(open('fangan/testcases_kg/engram/ch00_abstract/gold_kg.yaml'))
src=SRC_PATHS['engram_paper_mineru.md'].read_text()
bad=[ev for it in g['nodes']+g['edges'] for ev in it.get('evidence',[]) if src[ev['char_start']:ev['char_end']]!=ev['quote']]
print('span violations:', len(bad))"
```
Expected: `span violations: 0`.

- [ ] **Step 4: Commit (script only; NOT the generated gold yet — it needs curation)**

```bash
echo "fangan/testcases_kg/" >> .gitignore   # draft gold is curated before committing
git add scripts/kg_goldgen.py .gitignore
git commit -m "feat(kg): qwen gold-generator orchestrator (draft KG per chapter)"
```

---

## Task 7: Gold-vs-gold self-test + curation guide

**Files:** Create `backend/tests/kg/test_eval_selftest.py`, `docs/superpowers/specs/2026-06-01-kg-curation-guide.md`.

- [ ] **Step 1: Self-test** — scoring a generated draft against itself must be perfect (sanity for the evaluator on real shapes). `test_eval_selftest.py`:

```python
import pathlib, yaml, pytest
from app.services.kg.models import KnowledgeGraph
from app.services.kg_eval.score import score_kg

DRAFT = pathlib.Path(__file__).resolve().parents[3] / "fangan" / "testcases_kg" / "engram" / "ch00_abstract" / "gold_kg.yaml"

def test_draft_vs_itself_is_perfect():
    if not DRAFT.exists():
        pytest.skip("draft gold not generated yet")
    g = KnowledgeGraph(**yaml.safe_load(DRAFT.read_text()))
    r = score_kg(g, g)
    assert r["nodes"]["f1"] == 1.0 and r["edges"]["f1"] == 1.0
```

- [ ] **Step 2: Curation guide** — `docs/superpowers/specs/2026-06-01-kg-curation-guide.md`: short doc telling a human curator how to review a `gold_kg.yaml` against the generation contract (merge over-split Concepts, drop noise nodes, fix edge endpoints/types, confirm evidence quotes), and that curated chapters get committed under `fangan/testcases_kg/` (remove from .gitignore once curated).

- [ ] **Step 3: Run self-test + commit**

```bash
cd backend && python -m pytest tests/kg -q
git add backend/tests/kg/test_eval_selftest.py docs/superpowers/specs/2026-06-01-kg-curation-guide.md
git commit -m "test(kg): draft-vs-self eval sanity + curation guide"
```

---

## Self-Review notes
- **Spec coverage:** §1 doc-type (DOC_TYPE map in goldgen; full registry is the product plan), §2 KG schema (Task 1 models), §3 windowing+extraction+canonicalize (Tasks 2-4), §4.1 contract (Task 0 doc), §4.2 generator (Task 6, qwen), §4.3 graph-matching eval (Task 5). The product extraction pipeline (deepseek) is the explicitly-deferred next plan that reuses `kg/`.
- **Evidence invariant** enforced by construction in `extract.py` (takes the real source substring) and verified in Task 6 Step 3.
- **Type consistency:** `extract_window(client, source_text, win_start, win_end, section_path, doc_type)`, `make_windows(text, source_file, line_range, n, m)`, `canonicalize(nodes, edges, doc_id)`, `score_kg(gold, pred)`, `make_client(env_prefix)` are used identically across tasks and the script.
- **Known follow-ups (documented):** Procedure `steps` and node `mentions` cross-document merge are modeled but lightly exercised by gold generation; the curation step (Task 7) is where humans finalize. `canonicalize.py` remap bookkeeping must be cleaned by the implementer (Task 4 note).
