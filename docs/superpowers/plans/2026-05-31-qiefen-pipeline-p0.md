# qiefen Pipeline P0 (Deterministic Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic half (stages S1–S5 + deterministic `do_not_extract` + emit + harness adapter) of the qiefen parsing→extraction pipeline, emitting the gold YAML schema so `fangan/testcases/harness` can score it.

**Architecture:** A new isolated `backend/app/services/qiefen/` package, one module per stage, reading a raw MinerU `.md` and producing a `QiefenDocument` whose `evidence_atoms` carry char-precise `source_span` (absolute offsets into the raw file). A harness adapter slices each gold chapter by its `source_meta.source_line_range`, runs the pipeline, writes `pred.yaml` mirroring the gold tree, and runs `harness.run_all`. LLM stages (S6–S8) and live cutover are out of scope for P0.

**Tech Stack:** Python 3, pydantic v2, PyYAML, pytest. No LLM/network in P0.

**Spec:** `docs/superpowers/specs/2026-05-31-qiefen-pipeline-design.md`

---

## File Structure

```
backend/app/services/qiefen/
  __init__.py          # package marker + public run() re-export
  models.py            # pydantic types mirroring gold schema + QiefenDocument.to_pred_dict()
  source_elements.py   # S1: raw .md -> SourceElementQ with absolute char spans
  section_tree.py      # S2: headings -> SectionNode breadcrumb paths
  atomizer.py          # S3: elements -> EvidenceAtom (exact span + atom_type)
  chunker.py           # S4: atoms -> SemanticChunk (anchor-based, typed)
  packager.py          # S5: chunks -> ContextPackage
  do_not_extract.py    # deterministic negatives (citations / urls / figure refs)
  emit.py              # QiefenDocument -> ordered dict / yaml string
  pipeline.py          # orchestrator: run(source_text, profile, line_range) -> QiefenDocument
  profiles.py          # P0 stub: profile id + atom/chunk vocab tables (object types come in P1)

backend/tests/qiefen/
  conftest.py          # fixtures: paths to gold tree + raw source files, loaders
  test_models.py
  test_source_elements.py
  test_section_tree.py
  test_atomizer.py
  test_chunker.py
  test_packager.py
  test_do_not_extract.py
  test_pipeline.py
  test_harness_invariant.py   # cross-chapter: source_file[span]==raw_text for every emitted atom

scripts/qiefen_score.py        # build pred.yaml tree for all 14 chapters + run harness.run_all
```

**Source-file resolution** (raw MinerU `.md`, outside the repo — machine-specific, overridable by `QIEFEN_SOURCE_ROOT`):

| `source_meta.source_file` (basename) | absolute path |
| --- | --- |
| `engram_paper_mineru.md` | `/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md` |
| `CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md` | `/Users/hzf/workspace/pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md` |

---

## Task 0: Scaffold package, tests, and dependencies

**Files:**
- Create: `backend/app/services/qiefen/__init__.py`
- Create: `backend/tests/__init__.py`, `backend/tests/qiefen/__init__.py`, `backend/tests/qiefen/conftest.py`
- Modify: `backend/requirements.txt`

- [ ] **Step 1: Add test/yaml deps**

Append to `backend/requirements.txt`:

```
pyyaml>=6.0
pytest>=8.0
```

- [ ] **Step 2: Install**

Run: `cd backend && ${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python} -m pip install -r requirements.txt`
Expected: pyyaml + pytest installed.

- [ ] **Step 3: Create empty package + test markers**

`backend/app/services/qiefen/__init__.py`:

```python
"""qiefen parsing+extraction pipeline (gold-schema output). See
docs/superpowers/specs/2026-05-31-qiefen-pipeline-design.md."""
```

`backend/tests/__init__.py`: empty file.
`backend/tests/qiefen/__init__.py`: empty file.

- [ ] **Step 4: Create conftest fixtures**

`backend/tests/qiefen/conftest.py`:

```python
import os
import pathlib
import pytest
import yaml

# Repo root = .../silicon_notebook (or worktree root). tests live at backend/tests/qiefen.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GOLD_ROOT = REPO_ROOT / "fangan" / "testcases"

SOURCE_ROOT = pathlib.Path(
    os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser")
)
SOURCE_PATHS = {
    "engram_paper_mineru.md": SOURCE_ROOT / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SOURCE_ROOT
    / "notebook_papers_mineru_skill_results"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}


def _require(path: pathlib.Path) -> pathlib.Path:
    if not path.exists():
        pytest.skip(f"raw source not available: {path}")
    return path


@pytest.fixture
def gold_root():
    return GOLD_ROOT


@pytest.fixture
def load_gold():
    def _load(doc, chapter):
        return yaml.safe_load(
            (GOLD_ROOT / doc / chapter / "gold.yaml").read_text(encoding="utf-8")
        )
    return _load


@pytest.fixture
def source_text():
    def _load(basename):
        return _require(SOURCE_PATHS[basename]).read_text(encoding="utf-8")
    return _load
```

- [ ] **Step 5: Verify pytest collects (no tests yet is fine)**

Run: `cd backend && python -m pytest tests/qiefen -q`
Expected: `no tests ran` (exit 5) — collection works, conftest imports cleanly.

- [ ] **Step 6: Commit**

```bash
git add backend/requirements.txt backend/app/services/qiefen/__init__.py backend/tests
git commit -m "chore(qiefen): scaffold package + pytest fixtures"
```

---

## Task 1: Data model (`models.py`)

**Files:**
- Create: `backend/app/services/qiefen/models.py`
- Test: `backend/tests/qiefen/test_models.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_models.py`:

```python
from app.services.qiefen.models import (
    SourceSpan, EvidenceAtom, SemanticChunk, ContextPackage,
    SectionNode, QiefenDocument, SourceMeta,
)


def test_atom_roundtrips_and_to_pred_dict_key_order():
    atom = EvidenceAtom(
        id="A1", section_id="SEC", atom_type="claim_sentence",
        source_element_id="SE1",
        source_span=SourceSpan(file="x.md", line_start=11, line_end=11,
                               char_start=526, char_end=730),
        raw_text="While ...", normalized_text="While ...",
    )
    doc = QiefenDocument(
        source_meta=SourceMeta(source_id="engram", profile="article_research",
                               title="t", source_file="x.md",
                               source_line_range=[9, 11]),
        section_tree=[SectionNode(id="SEC", path="Abstract", title="Abstract")],
        evidence_atoms=[atom],
        semantic_chunks=[SemanticChunk(id="C1", profile="article_research",
                                       chunk_type="article_core_claim_block",
                                       section_path="Abstract", atom_ids=["A1"])],
        context_packages=[ContextPackage(id="P1", profile="article_research",
                                         chunk_id="C1", section_path="Abstract",
                                         document_title="t",
                                         atoms=[{"atom_id": "A1",
                                                 "atom_type": "claim_sentence"}])],
    )
    d = doc.to_pred_dict()
    assert list(d.keys())[:5] == [
        "schema_version", "source_meta", "section_tree",
        "evidence_atoms", "semantic_chunks",
    ]
    assert d["evidence_atoms"][0]["source_span"]["char_start"] == 526
    assert d["evidence_atoms"][0]["raw_text"] == "While ..."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_models.py -q`
Expected: FAIL — `ModuleNotFoundError: app.services.qiefen.models`.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/models.py`:

```python
"""Pydantic types mirroring the gold.yaml schema (v0.3.3). to_pred_dict()
emits keys in the gold top-level order so emit.py is a trivial dump."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceSpan(BaseModel):
    file: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int


class SourceElementQ(BaseModel):
    id: str
    type: str  # heading | paragraph | formula | table | figure_caption | list_item
    file: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int
    text: str  # verbatim slice of source_file[char_start:char_end]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SectionNode(BaseModel):
    id: str
    path: str
    title: str
    parent: Optional[str] = None
    kind: Optional[str] = None


class EvidenceAtom(BaseModel):
    id: str
    section_id: str
    atom_type: str
    source_element_id: str
    source_span: SourceSpan
    raw_text: str
    normalized_text: str
    evidence_strength: str = "direct"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SemanticChunk(BaseModel):
    id: str
    profile: str
    chunk_type: str
    section_path: str
    atom_ids: List[str] = Field(default_factory=list)
    central_atom_ids: List[str] = Field(default_factory=list)
    boundary_reason: str = ""
    extraction_targets: List[str] = Field(default_factory=list)
    gold_must_cover_atoms: List[str] = Field(default_factory=list)


class ContextPackage(BaseModel):
    id: str
    profile: str
    chunk_id: str
    section_path: str
    document_title: str
    atoms: List[Dict[str, str]] = Field(default_factory=list)  # {atom_id, atom_type}
    linked_context: Dict[str, Any] = Field(default_factory=dict)
    extraction_targets: List[str] = Field(default_factory=list)
    expected_objects: List[str] = Field(default_factory=list)


class Mention(BaseModel):
    id: str
    text: str
    type: str
    atom_id: str
    canonical_key: str = ""


class KnowledgeObjectQ(BaseModel):
    id: str
    type: str
    section_path: str = ""
    home_package: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    local_evidence_atom_ids: List[str] = Field(default_factory=list)
    supporting_context_atom_ids: List[str] = Field(default_factory=list)


class RelationQ(BaseModel):
    id: str
    relation_type: str
    source_object_id: str
    target_object_id: str
    evidence_atom_ids: List[str] = Field(default_factory=list)


class SourceMeta(BaseModel):
    source_id: str
    profile: str
    title: str
    source_file: str
    source_line_range: List[int] = Field(default_factory=list)
    scope: str = ""
    extraction_targets: List[str] = Field(default_factory=list)


class QiefenDocument(BaseModel):
    schema_version: str = "0.3.3"
    source_meta: SourceMeta
    section_tree: List[SectionNode] = Field(default_factory=list)
    evidence_atoms: List[EvidenceAtom] = Field(default_factory=list)
    semantic_chunks: List[SemanticChunk] = Field(default_factory=list)
    context_packages: List[ContextPackage] = Field(default_factory=list)
    mentions: List[Mention] = Field(default_factory=list)
    canonicalization: List[Dict[str, Any]] = Field(default_factory=list)
    objects: List[KnowledgeObjectQ] = Field(default_factory=list)
    relations: List[RelationQ] = Field(default_factory=list)
    do_not_extract: List[Dict[str, Any]] = Field(default_factory=list)

    # Gold top-level key order (README/_AGENT_SPEC.md).
    _ORDER = (
        "schema_version", "source_meta", "section_tree", "evidence_atoms",
        "semantic_chunks", "context_packages", "mentions", "canonicalization",
        "objects", "relations", "do_not_extract",
    )

    def to_pred_dict(self) -> Dict[str, Any]:
        dumped = self.model_dump(mode="python", exclude_none=True)
        return {k: dumped[k] for k in self._ORDER if k in dumped}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/models.py backend/tests/qiefen/test_models.py
git commit -m "feat(qiefen): gold-schema pydantic model + ordered to_pred_dict"
```

---

## Task 2: S1 SourceElements with absolute char spans (`source_elements.py`)

**Files:**
- Create: `backend/app/services/qiefen/source_elements.py`
- Test: `backend/tests/qiefen/test_source_elements.py`

The core requirement: every element's `[char_start, char_end]` is an **absolute** offset into the raw file such that `source_text[char_start:char_end] == element.text`. We compute a line→offset index over the FULL file, then optionally restrict to a line range.

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_source_elements.py`:

```python
from app.services.qiefen.source_elements import parse_elements, line_offsets


def test_line_offsets_absolute():
    text = "a\nbb\nccc\n"
    offs = line_offsets(text)
    assert offs[1] == 0   # line 1 starts at 0
    assert offs[2] == 2   # after "a\n"
    assert offs[3] == 5   # after "bb\n"


def test_abstract_paragraph_span_is_verbatim(source_text):
    src = source_text("engram_paper_mineru.md")
    els = parse_elements(src, "engram_paper_mineru.md", line_range=[9, 11])
    # Heading "Abstract" (line 9) + the paragraph (line 11).
    heading = [e for e in els if e.type == "heading"]
    para = [e for e in els if e.type == "paragraph"]
    assert heading and para
    p = para[0]
    assert p.char_start == 526
    assert src[p.char_start:p.char_end] == p.text
    assert p.text.startswith("While Mixture-of-Experts (MoE)")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_source_elements.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/source_elements.py`:

```python
"""S1: raw MinerU markdown -> SourceElementQ with absolute char spans.

Unlike the legacy parsers.py, raw text is NEVER whitespace-collapsed: each
element's [char_start, char_end] is a verbatim slice of the source file, so the
downstream atomizer can compute spans that satisfy source[span]==raw_text.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from app.services.qiefen.models import SourceElementQ

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FORMULA_BLOCK = re.compile(r"^\s*\$\$")  # $$ ... $$ display formula
_TABLE_HTML = re.compile(r"^\s*<(table|details)\b", re.IGNORECASE)
_FIGURE = re.compile(r"^\s*(Figure\s+\d+|!\[\]\()", re.IGNORECASE)
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")


def line_offsets(text: str) -> Dict[int, int]:
    """1-based line number -> absolute char offset where that line begins."""
    offsets: Dict[int, int] = {}
    off = 0
    for i, line in enumerate(text.split("\n"), start=1):
        offsets[i] = off
        off += len(line) + 1  # +1 for the '\n'
    return offsets


def _classify_line(line: str) -> str:
    if _HEADING.match(line):
        return "heading"
    if _FORMULA_BLOCK.match(line):
        return "formula"
    if _TABLE_HTML.match(line):
        return "table"
    if _FIGURE.match(line):
        return "figure_caption"
    if _LIST.match(line):
        return "list_item"
    return "paragraph"


def parse_elements(
    text: str, source_file: str, line_range: Optional[List[int]] = None
) -> List[SourceElementQ]:
    lines = text.split("\n")
    offs = line_offsets(text)
    lo, hi = (line_range or [1, len(lines)])
    elements: List[SourceElementQ] = []
    counter = 0

    def emit(kind: str, l_start: int, l_end: int) -> None:
        nonlocal counter
        char_start = offs[l_start]
        # char_end = end of l_end's text (no trailing newline).
        char_end = offs[l_end] + len(lines[l_end - 1])
        raw = text[char_start:char_end]
        if not raw.strip():
            return
        counter += 1
        elements.append(SourceElementQ(
            id=f"SE-{l_start}-{counter}", type=kind, file=source_file,
            line_start=l_start, line_end=l_end,
            char_start=char_start, char_end=char_end, text=raw,
        ))

    i = lo
    while i <= hi:
        line = lines[i - 1]
        if not line.strip():
            i += 1
            continue
        kind = _classify_line(line)
        if kind == "heading":
            emit("heading", i, i)
            i += 1
        elif kind in ("formula", "table", "figure_caption", "list_item"):
            # single-line structural element (MinerU emits these on one line)
            emit(kind, i, i)
            i += 1
        else:
            # paragraph: consume consecutive non-blank, non-structural lines
            j = i
            while (j < hi and lines[j].strip()
                   and _classify_line(lines[j]) == "paragraph"):
                j += 1
            emit("paragraph", i, j)
            i = j + 1
    return elements
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_source_elements.py -q`
Expected: PASS (or SKIP if raw source unavailable — `test_line_offsets_absolute` must still PASS).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/source_elements.py backend/tests/qiefen/test_source_elements.py
git commit -m "feat(qiefen): S1 source elements with absolute char spans"
```

---

## Task 3: S2 Section tree (`section_tree.py`)

**Files:**
- Create: `backend/app/services/qiefen/section_tree.py`
- Test: `backend/tests/qiefen/test_section_tree.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_section_tree.py`:

```python
from app.services.qiefen.models import SourceElementQ
from app.services.qiefen.section_tree import build_section_tree


def _heading(text, level, line):
    return SourceElementQ(id=f"H{line}", type="heading", file="x.md",
                          line_start=line, line_end=line, char_start=0,
                          char_end=1, text=("#" * level) + " " + text,
                          metadata={"level": level})


def test_breadcrumb_paths():
    els = [
        _heading("2. Architecture", 1, 1),
        _heading("2.2 Sparse Retrieval", 2, 2),
        _heading("3. Scaling Laws", 1, 3),
    ]
    nodes = build_section_tree(els)
    paths = [n.path for n in nodes]
    assert "2. Architecture" in paths
    assert "2. Architecture > 2.2 Sparse Retrieval" in paths
    assert "3. Scaling Laws" in paths


def test_single_heading_no_levels():
    els = [_heading("Abstract", 1, 9)]
    nodes = build_section_tree(els)
    assert nodes[0].path == "Abstract"
    assert nodes[0].title == "Abstract"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_section_tree.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/section_tree.py`:

```python
"""S2: heading elements -> SectionNode list with breadcrumb `path` joined by
' > ', matching the gold normalization (section paths are scored as a set)."""
from __future__ import annotations

import re
from typing import List

from app.services.qiefen.models import SectionNode, SourceElementQ

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _level_and_title(el: SourceElementQ) -> tuple[int, str]:
    m = _HEADING.match(el.text)
    if m:
        return len(m.group(1)), m.group(2).strip()
    return int(el.metadata.get("level", 1) or 1), el.text.strip()


def build_section_tree(elements: List[SourceElementQ]) -> List[SectionNode]:
    nodes: List[SectionNode] = []
    stack: List[tuple[int, str, str]] = []  # (level, node_id, title)
    counter = 0
    for el in elements:
        if el.type != "heading":
            continue
        level, title = _level_and_title(el)
        while stack and stack[-1][0] >= level:
            stack.pop()
        counter += 1
        node_id = f"SEC-{counter}"
        parent_id = stack[-1][1] if stack else None
        path = " > ".join([t for _, _, t in stack] + [title])
        nodes.append(SectionNode(id=node_id, path=path, title=title,
                                 parent=parent_id))
        stack.append((level, node_id, title))
    return nodes
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_section_tree.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/section_tree.py backend/tests/qiefen/test_section_tree.py
git commit -m "feat(qiefen): S2 breadcrumb section tree"
```

---

## Task 4: S3 Atomizer (`atomizer.py`) — the critical stage

**Files:**
- Create: `backend/app/services/qiefen/atomizer.py`
- Test: `backend/tests/qiefen/test_atomizer.py`

Splits paragraph elements into sentence atoms (formula/table/figure handled by type), assigns `atom_type` by profile cue rules, and computes each atom's span by locating its text inside its element (offset arithmetic). The invariant `source[span]==raw_text` must hold.

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_atomizer.py`:

```python
from app.services.qiefen.models import SourceElementQ
from app.services.qiefen.atomizer import atomize


def test_sentence_atoms_have_verbatim_spans_and_types():
    src = ("Heading\n\n"
           "While MoE scales capacity, Transformers lack lookup. "
           "To address this, we introduce conditional memory via Engram.")
    # paragraph element covering the 3rd line region:
    start = src.index("While")
    el = SourceElementQ(id="SE1", type="paragraph", file="x.md",
                        line_start=3, line_end=3, char_start=start,
                        char_end=len(src), text=src[start:])
    atoms = atomize(src, [el], section_id="SEC", profile="article_research")
    assert len(atoms) == 2
    for a in atoms:
        assert src[a.source_span.char_start:a.source_span.char_end] == a.raw_text
    assert atoms[0].atom_type == "claim_sentence"
    assert atoms[1].atom_type == "method_sentence"  # "we introduce"


def test_formula_element_becomes_formula_atom():
    src = "$$ C_j = C_{j0} \\tag{2} $$"
    el = SourceElementQ(id="SE2", type="formula", file="x.md", line_start=1,
                        line_end=1, char_start=0, char_end=len(src), text=src)
    atoms = atomize(src, [el], section_id="SEC", profile="textbook")
    assert len(atoms) == 1
    assert atoms[0].atom_type == "formula_atom"
    assert src[atoms[0].source_span.char_start:atoms[0].source_span.char_end] \
        == atoms[0].raw_text
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_atomizer.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/atomizer.py`:

```python
"""S3: SourceElement -> EvidenceAtom with exact spans + atom_type.

Spans are computed by locating each sentence verbatim inside its element's
text and offsetting by the element's char_start, so source[span]==raw_text by
construction. atom_type is assigned by deterministic, profile-specific cues.
"""
from __future__ import annotations

import re
from typing import List

from app.services.qiefen.models import EvidenceAtom, SourceElementQ, SourceSpan

# Sentence boundary: terminator + following space. Keep the terminator with the
# left sentence. (Sub-sentence splitting for "while ... we observe ..." is a
# later refinement driven by the harness atom report.)
_SENT = re.compile(r"(?<=[.!?。！？])\s+")

_ARTICLE_CUES = [
    ("scaling_law_result_atom", re.compile(r"\bU-shaped|scaling law\b", re.I)),
    ("method_sentence", re.compile(r"\bwe (introduce|propose|instantiate|present)\b", re.I)),
    ("mechanism_sentence", re.compile(r"\bMechanistic|relieves|frees up|delegating\b", re.I)),
    ("result_sentence", re.compile(r"\bwe observe|achieving|\+\d|improv|outperform\b", re.I)),
    ("risk_sentence", re.compile(r"\bcollision|polysemy|risk|degrad\b", re.I)),
]
_TEXTBOOK_CUES = [
    ("definition_atom", re.compile(r"\bis defined as|refers to|means\b", re.I)),
    ("process_step_atom", re.compile(r"\b(step|then|next|first|finally)\b", re.I)),
    ("example_problem_atom", re.compile(r"\bExample\s+\d", re.I)),
    ("given_atom", re.compile(r"\bgiven\b", re.I)),
    ("formula_usage_atom", re.compile(r"\b(find|calculate|using Eq)\b", re.I)),
]


def _sentence_type(sentence: str, profile: str) -> str:
    cues = _ARTICLE_CUES if profile == "article_research" else _TEXTBOOK_CUES
    for atom_type, pat in cues:
        if pat.search(sentence):
            return atom_type
    return "claim_sentence" if profile == "article_research" else "concept_definition_atom"


def _normalize(text: str) -> str:
    out = (text.replace("→", "->").replace("≤", "<=")
           .replace("≥", ">=").replace("×", "x"))
    return re.sub(r"\$([^$]*)\$", r"\1", out)


def atomize(source_text: str, elements: List[SourceElementQ], section_id: str,
            profile: str) -> List[EvidenceAtom]:
    atoms: List[EvidenceAtom] = []
    n = 0

    def add(el: SourceElementQ, local_start: int, raw: str, atom_type: str) -> None:
        nonlocal n
        if not raw.strip():
            return
        cstart = el.char_start + local_start
        cend = cstart + len(raw)
        assert source_text[cstart:cend] == raw, "span/raw mismatch"
        n += 1
        atoms.append(EvidenceAtom(
            id=f"A-{section_id}-{n}", section_id=section_id, atom_type=atom_type,
            source_element_id=el.id,
            source_span=SourceSpan(file=el.file, line_start=el.line_start,
                                   line_end=el.line_end, char_start=cstart,
                                   char_end=cend),
            raw_text=raw, normalized_text=_normalize(raw),
        ))

    for el in elements:
        if el.type == "formula":
            add(el, 0, el.text, "formula_atom")
        elif el.type == "figure_caption":
            add(el, 0, el.text, "figure_caption_atom")
        elif el.type == "table":
            add(el, 0, el.text, "table_caption_atom")
        elif el.type == "heading":
            continue
        else:  # paragraph / list_item -> sentences
            pos = 0
            for piece in _SENT.split(el.text):
                if not piece:
                    continue
                local = el.text.index(piece, pos)
                pos = local + len(piece)
                add(el, local, piece, _sentence_type(piece, profile))
    return atoms
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_atomizer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/atomizer.py backend/tests/qiefen/test_atomizer.py
git commit -m "feat(qiefen): S3 atomizer with verbatim spans + profile atom types"
```

---

## Task 5: S4 Chunker (`chunker.py`)

**Files:**
- Create: `backend/app/services/qiefen/chunker.py`
- Test: `backend/tests/qiefen/test_chunker.py`

P0 chunking rule: group consecutive atoms that share a `section_id` into one chunk; never split a formula/table/figure away from the paragraph atom that immediately precedes it; assign `chunk_type` from a profile default. (Anchor-based refinement comes when the harness chunk report shows over/under-splitting.)

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_chunker.py`:

```python
from app.services.qiefen.models import EvidenceAtom, SourceSpan
from app.services.qiefen.chunker import build_chunks


def _atom(aid, section_id, atom_type="claim_sentence"):
    return EvidenceAtom(id=aid, section_id=section_id, atom_type=atom_type,
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1,
                                               line_end=1, char_start=0, char_end=1),
                        raw_text="t", normalized_text="t")


def test_atoms_grouped_by_section_each_in_one_chunk():
    atoms = [_atom("A1", "SEC1"), _atom("A2", "SEC1"), _atom("A3", "SEC2")]
    chunks = build_chunks(atoms, profile="article_research",
                          section_paths={"SEC1": "Abstract", "SEC2": "1. Intro"})
    assert len(chunks) == 2
    assert set(chunks[0].atom_ids) == {"A1", "A2"}
    assert all(c.chunk_type for c in chunks)  # every chunk is typed
    # every atom appears in exactly one chunk
    seen = [a for c in chunks for a in c.atom_ids]
    assert sorted(seen) == ["A1", "A2", "A3"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_chunker.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/chunker.py`:

```python
"""S4: atoms -> SemanticChunk. P0: one chunk per contiguous run of same-section
atoms, typed by a profile default keyed on the dominant atom_type."""
from __future__ import annotations

from typing import Dict, List

from app.services.qiefen.models import EvidenceAtom, SemanticChunk

_ARTICLE_CHUNK_BY_ATOM = {
    "scaling_law_result_atom": "scaling_law_block",
    "result_sentence": "experiment_result_block",
    "method_sentence": "architecture_component_block",
    "mechanism_sentence": "article_core_claim_block",
}
_TEXTBOOK_CHUNK_BY_ATOM = {
    "formula_atom": "formula_definition_block",
    "definition_atom": "concept_definition_block",
    "process_step_atom": "process_flow_block",
    "example_problem_atom": "example_solution_block",
}


def _chunk_type(profile: str, atom_types: List[str]) -> str:
    table = _ARTICLE_CHUNK_BY_ATOM if profile == "article_research" else _TEXTBOOK_CHUNK_BY_ATOM
    for at in atom_types:
        if at in table:
            return table[at]
    return "article_core_claim_block" if profile == "article_research" else "chapter_overview_block"


def build_chunks(atoms: List[EvidenceAtom], profile: str,
                 section_paths: Dict[str, str]) -> List[SemanticChunk]:
    chunks: List[SemanticChunk] = []
    run: List[EvidenceAtom] = []
    n = 0

    def flush() -> None:
        nonlocal n
        if not run:
            return
        n += 1
        sid = run[0].section_id
        chunks.append(SemanticChunk(
            id=f"C-{sid}-{n}", profile=profile,
            chunk_type=_chunk_type(profile, [a.atom_type for a in run]),
            section_path=section_paths.get(sid, ""),
            atom_ids=[a.id for a in run],
            central_atom_ids=[run[0].id],
        ))

    cur_section = None
    for a in atoms:
        if cur_section is not None and a.section_id != cur_section:
            flush()
            run = []
        cur_section = a.section_id
        run.append(a)
    flush()
    return chunks
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_chunker.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/chunker.py backend/tests/qiefen/test_chunker.py
git commit -m "feat(qiefen): S4 section-contiguous typed chunker"
```

---

## Task 6: S5 Packager (`packager.py`)

**Files:**
- Create: `backend/app/services/qiefen/packager.py`
- Test: `backend/tests/qiefen/test_packager.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_packager.py`:

```python
from app.services.qiefen.models import EvidenceAtom, SemanticChunk, SourceSpan
from app.services.qiefen.packager import build_packages


def _atom(aid, atom_type="claim_sentence"):
    return EvidenceAtom(id=aid, section_id="SEC", atom_type=atom_type,
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1, line_end=1,
                                               char_start=0, char_end=1),
                        raw_text="t", normalized_text="t")


def test_one_package_per_chunk_with_atom_type_pairs():
    atoms = [_atom("A1"), _atom("A2", "method_sentence")]
    chunk = SemanticChunk(id="C1", profile="article_research",
                          chunk_type="article_core_claim_block",
                          section_path="Abstract", atom_ids=["A1", "A2"])
    pkgs = build_packages([chunk], {a.id: a for a in atoms},
                          document_title="Engram", profile="article_research")
    assert len(pkgs) == 1
    assert pkgs[0].chunk_id == "C1"
    assert pkgs[0].atoms == [{"atom_id": "A1", "atom_type": "claim_sentence"},
                             {"atom_id": "A2", "atom_type": "method_sentence"}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_packager.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/packager.py`:

```python
"""S5: one ContextPackage per chunk. expected_objects is left empty in P0
(filled by the LLM object stage in P1)."""
from __future__ import annotations

from typing import Dict, List

from app.services.qiefen.models import (
    ContextPackage, EvidenceAtom, SemanticChunk,
)


def build_packages(chunks: List[SemanticChunk], atoms_by_id: Dict[str, EvidenceAtom],
                   document_title: str, profile: str) -> List[ContextPackage]:
    pkgs: List[ContextPackage] = []
    for i, ch in enumerate(chunks, start=1):
        pairs = []
        for aid in ch.atom_ids:
            a = atoms_by_id.get(aid)
            if a is not None:
                pairs.append({"atom_id": aid, "atom_type": a.atom_type})
        pkgs.append(ContextPackage(
            id=f"PKG-{i}", profile=profile, chunk_id=ch.id,
            section_path=ch.section_path, document_title=document_title,
            atoms=pairs, extraction_targets=ch.extraction_targets,
        ))
    return pkgs
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_packager.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/packager.py backend/tests/qiefen/test_packager.py
git commit -m "feat(qiefen): S5 context packager"
```

---

## Task 7: Deterministic `do_not_extract` (`do_not_extract.py`)

**Files:**
- Create: `backend/app/services/qiefen/do_not_extract.py`
- Test: `backend/tests/qiefen/test_do_not_extract.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_do_not_extract.py`:

```python
from app.services.qiefen.models import EvidenceAtom, SourceSpan
from app.services.qiefen.do_not_extract import detect_negatives


def _atom(aid, raw):
    return EvidenceAtom(id=aid, section_id="SEC", atom_type="claim_sentence",
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1, line_end=1,
                                               char_start=0, char_end=len(raw)),
                        raw_text=raw, normalized_text=raw)


def test_detects_url_and_citation():
    atoms = [
        _atom("A1", "Code available at: https://github.com/deepseek-ai/Engram"),
        _atom("A2", "Transformers (Vaswani et al., 2017) lack lookup."),
        _atom("A3", "A plain sentence with no negatives."),
    ]
    dne = detect_negatives(atoms)
    kinds = {e["kind"] for e in dne}
    assert "out_of_slice_reference" in kinds  # url
    assert "citation_policy" in kinds         # author-year
    texts = " ".join(str(e.get("text", "")) + str(e.get("examples", "")) for e in dne)
    assert "github.com" in texts
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_do_not_extract.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the implementation**

`backend/app/services/qiefen/do_not_extract.py`:

```python
"""Deterministic negative control: surfaces that must NOT become knowledge
objects (URLs, author-year citations, figure/table cross-references)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.services.qiefen.models import EvidenceAtom

_URL = re.compile(r"https?://\S+")
_CITATION = re.compile(r"\([A-Z][A-Za-z]+ et al\.?,?\s*\d{4}\)")
_FIGREF = re.compile(r"\b(?:see |in )?(Figure|Table)\s+\d+\b")


def detect_negatives(atoms: List[EvidenceAtom]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    citation_examples: List[str] = []
    for a in atoms:
        for url in _URL.findall(a.raw_text):
            entries.append({"text": url.rstrip(".,);"), "atom_id": a.id,
                            "reason": "Resource URL, not a knowledge object.",
                            "kind": "out_of_slice_reference"})
        for cit in _CITATION.findall(a.raw_text):
            if cit not in citation_examples:
                citation_examples.append(cit)
    if citation_examples:
        entries.append({"pattern": "inline_author_year_citation",
                        "examples": citation_examples,
                        "reason": "inline author-year citations are not knowledge objects.",
                        "kind": "citation_policy"})
    return entries
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_do_not_extract.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/qiefen/do_not_extract.py backend/tests/qiefen/test_do_not_extract.py
git commit -m "feat(qiefen): deterministic do_not_extract negatives"
```

---

## Task 8: Emit + Pipeline orchestrator (`emit.py`, `pipeline.py`, `profiles.py`)

**Files:**
- Create: `backend/app/services/qiefen/profiles.py`
- Create: `backend/app/services/qiefen/emit.py`
- Create: `backend/app/services/qiefen/pipeline.py`
- Modify: `backend/app/services/qiefen/__init__.py`
- Test: `backend/tests/qiefen/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/qiefen/test_pipeline.py`:

```python
import yaml
from app.services.qiefen.pipeline import run
from app.services.qiefen.emit import to_yaml


def test_pipeline_abstract_atoms_satisfy_span_invariant(source_text):
    src = source_text("engram_paper_mineru.md")
    doc = run(src, source_file="engram_paper_mineru.md",
              profile="article_research", line_range=[9, 11],
              source_id="engram", title="Engram", scope="Abstract only")
    assert doc.evidence_atoms, "expected atoms from the abstract"
    for a in doc.evidence_atoms:
        s = a.source_span
        assert src[s.char_start:s.char_end] == a.raw_text
    # every atom is in exactly one chunk
    in_chunks = [aid for c in doc.semantic_chunks for aid in c.atom_ids]
    assert sorted(in_chunks) == sorted(a.id for a in doc.evidence_atoms)
    # emit is valid YAML with gold key order
    parsed = yaml.safe_load(to_yaml(doc))
    assert list(parsed.keys())[1] == "source_meta"
    assert parsed["evidence_atoms"][0]["raw_text"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/qiefen/test_pipeline.py -q`
Expected: FAIL — modules missing.

- [ ] **Step 3: Write `profiles.py` (P0 stub — atom/chunk vocab only)**

`backend/app/services/qiefen/profiles.py`:

```python
"""P0 profile stub: just the id + extraction targets used by chunks/packages.
The object/relation type vocabularies arrive in P1."""
from __future__ import annotations

ARTICLE_TARGETS = ["ArticleClaim", "ArticleMethod", "ScalingLaw",
                   "ExperimentResult", "MechanisticExplanation"]
TEXTBOOK_TARGETS = ["Concept", "Formula", "Derivation", "ProcessFlow",
                    "DesignPrinciple"]


def extraction_targets(profile: str):
    return ARTICLE_TARGETS if profile == "article_research" else TEXTBOOK_TARGETS
```

- [ ] **Step 4: Write `emit.py`**

`backend/app/services/qiefen/emit.py`:

```python
"""QiefenDocument -> gold-ordered YAML."""
from __future__ import annotations

import yaml

from app.services.qiefen.models import QiefenDocument


def to_yaml(doc: QiefenDocument) -> str:
    return yaml.safe_dump(doc.to_pred_dict(), sort_keys=False, allow_unicode=True,
                          width=1000)
```

- [ ] **Step 5: Write `pipeline.py`**

`backend/app/services/qiefen/pipeline.py`:

```python
"""Deterministic P0 orchestrator: source_text -> QiefenDocument (S1..S5 + DNE)."""
from __future__ import annotations

from typing import List, Optional

from app.services.qiefen import atomizer, chunker, do_not_extract, packager
from app.services.qiefen.models import EvidenceAtom, QiefenDocument, SourceMeta
from app.services.qiefen.profiles import extraction_targets
from app.services.qiefen.section_tree import build_section_tree
from app.services.qiefen.source_elements import parse_elements


def run(source_text: str, source_file: str, profile: str,
        line_range: Optional[List[int]] = None, source_id: str = "",
        title: str = "", scope: str = "") -> QiefenDocument:
    elements = parse_elements(source_text, source_file, line_range)
    sections = build_section_tree(elements)

    # Map each element to its enclosing section (last heading at/above it).
    section_of: dict[str, str] = {}
    section_paths = {s.id: s.path for s in sections}
    cur_section = sections[0].id if sections else "SEC-0"
    # Non-heading elements inherit the last heading at/above their line.
    sec_by_line = sorted(
        [(e.line_start, s.id) for e, s in _pair_headings(elements, sections)],
    )
    for el in elements:
        sid = _section_for_line(el.line_start, sec_by_line)
        section_of[el.id] = sid or cur_section

    atoms: List[EvidenceAtom] = []
    for sid in _ordered_sections(elements, section_of, sections):
        sec_elements = [e for e in elements if section_of[e.id] == sid
                        and e.type != "heading"]
        atoms.extend(atomizer.atomize(source_text, sec_elements, sid, profile))

    chunks = chunker.build_chunks(atoms, profile, section_paths)
    atoms_by_id = {a.id: a for a in atoms}
    packages = packager.build_packages(chunks, atoms_by_id, title, profile)
    for ch in chunks:
        ch.extraction_targets = extraction_targets(profile)
    for pkg in packages:
        pkg.extraction_targets = extraction_targets(profile)
    dne = do_not_extract.detect_negatives(atoms)

    return QiefenDocument(
        source_meta=SourceMeta(source_id=source_id, profile=profile, title=title,
                               source_file=source_file,
                               source_line_range=line_range or [],
                               scope=scope,
                               extraction_targets=extraction_targets(profile)),
        section_tree=sections, evidence_atoms=atoms, semantic_chunks=chunks,
        context_packages=packages, do_not_extract=dne,
    )


def _pair_headings(elements, sections):
    headings = [e for e in elements if e.type == "heading"]
    return list(zip(headings, sections))


def _section_for_line(line, sec_by_line):
    chosen = None
    for hline, sid in sec_by_line:
        if hline <= line:
            chosen = sid
        else:
            break
    return chosen


def _ordered_sections(elements, section_of, sections):
    seen = []
    for e in elements:
        sid = section_of[e.id]
        if sid not in seen:
            seen.append(sid)
    return seen
```

- [ ] **Step 6: Re-export `run` from package init**

Replace `backend/app/services/qiefen/__init__.py`:

```python
"""qiefen parsing+extraction pipeline (gold-schema output). See
docs/superpowers/specs/2026-05-31-qiefen-pipeline-design.md."""
from app.services.qiefen.pipeline import run  # noqa: F401
```

- [ ] **Step 7: Run to verify it passes**

Run: `cd backend && python -m pytest tests/qiefen/test_pipeline.py -q`
Expected: PASS (or SKIP if raw source unavailable).

- [ ] **Step 8: Run the full qiefen suite**

Run: `cd backend && python -m pytest tests/qiefen -q`
Expected: all PASS/skip, no failures.

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/qiefen/profiles.py backend/app/services/qiefen/emit.py \
        backend/app/services/qiefen/pipeline.py backend/app/services/qiefen/__init__.py \
        backend/tests/qiefen/test_pipeline.py
git commit -m "feat(qiefen): deterministic pipeline orchestrator + yaml emit"
```

---

## Task 9: Harness adapter + cross-chapter invariant test

**Files:**
- Create: `scripts/qiefen_score.py`
- Test: `backend/tests/qiefen/test_harness_invariant.py`

- [ ] **Step 1: Write the failing test (the harness's own required invariant, over every chapter)**

`backend/tests/qiefen/test_harness_invariant.py`:

```python
import pathlib
import pytest
import yaml
from app.services.qiefen.pipeline import run

REPO = pathlib.Path(__file__).resolve().parents[3]
GOLD = REPO / "fangan" / "testcases"
CHAPTERS = sorted(str(p.parent.relative_to(GOLD))
                  for p in GOLD.glob("*/ch*/gold.yaml"))


@pytest.mark.parametrize("chapter", CHAPTERS)
def test_every_emitted_atom_span_is_verbatim(chapter, source_text):
    gold = yaml.safe_load((GOLD / chapter / "gold.yaml").read_text(encoding="utf-8"))
    meta = gold["source_meta"]
    src = source_text(meta["source_file"])
    doc = run(src, source_file=meta["source_file"], profile=meta["profile"],
              line_range=meta.get("source_line_range"),
              source_id=meta.get("source_id", ""), title=meta.get("title", ""))
    for a in doc.evidence_atoms:
        s = a.source_span
        assert src[s.char_start:s.char_end] == a.raw_text, f"{chapter}:{a.id}"
```

- [ ] **Step 2: Run to verify it fails or skips meaningfully**

Run: `cd backend && python -m pytest tests/qiefen/test_harness_invariant.py -q`
Expected: FAIL — `scripts/qiefen_score.py` not needed for this test, but the test itself must run; if the pipeline emits a bad span it FAILS here. (SKIP only if sources unavailable.) Fix any span bug before continuing.

- [ ] **Step 3: Write the harness adapter script**

`scripts/qiefen_score.py`:

```python
#!/usr/bin/env python3
"""Run the qiefen pipeline over every gold chapter, write pred.yaml into a
candidate tree mirroring the gold layout, then run the testcase harness.

Usage:
  PYTHONPATH=backend python scripts/qiefen_score.py --out /tmp/qiefen_pred
"""
import argparse
import os
import pathlib
import subprocess
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
GOLD = REPO / "fangan" / "testcases"
sys.path.insert(0, str(REPO / "backend"))

from app.services.qiefen.pipeline import run  # noqa: E402
from app.services.qiefen.emit import to_yaml  # noqa: E402

SOURCE_ROOT = pathlib.Path(
    os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser")
)
SOURCE_PATHS = {
    "engram_paper_mineru.md": SOURCE_ROOT / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SOURCE_ROOT
    / "notebook_papers_mineru_skill_results"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "harness_out" / "qiefen_pred"))
    args = ap.parse_args()
    out_root = pathlib.Path(args.out)

    for gp in sorted(GOLD.glob("*/ch*/gold.yaml")):
        chapter_dir = gp.parent
        rel = chapter_dir.relative_to(GOLD)
        meta = yaml.safe_load(gp.read_text(encoding="utf-8"))["source_meta"]
        src_path = SOURCE_PATHS.get(meta["source_file"])
        if not src_path or not src_path.exists():
            print(f"skip {rel}: source missing")
            continue
        src = src_path.read_text(encoding="utf-8")
        doc = run(src, source_file=meta["source_file"], profile=meta["profile"],
                  line_range=meta.get("source_line_range"),
                  source_id=meta.get("source_id", ""), title=meta.get("title", ""),
                  scope=meta.get("scope", ""))
        dst = out_root / rel
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "pred.yaml").write_text(to_yaml(doc), encoding="utf-8")
        print(f"wrote {rel}/pred.yaml ({len(doc.evidence_atoms)} atoms)")

    # Run the harness.
    cmd = [sys.executable, "-m", "harness.run_all", "--gold-root", ".",
           "--pred-root", str(out_root), "--out-dir", str(out_root / "_report")]
    print("running harness:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(GOLD), check=False)
    print(f"leaderboard: {out_root / '_report' / 'leaderboard.md'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the invariant test (must pass before scoring)**

Run: `cd backend && python -m pytest tests/qiefen/test_harness_invariant.py -q`
Expected: PASS for all chapters whose source is available.

- [ ] **Step 5: Commit**

```bash
git add scripts/qiefen_score.py backend/tests/qiefen/test_harness_invariant.py
git commit -m "feat(qiefen): harness adapter script + cross-chapter span invariant test"
```

---

## Task 10: Baseline harness run + record P0 score

**Files:**
- Create: `docs/superpowers/specs/2026-05-31-qiefen-p0-baseline.md`

- [ ] **Step 1: Generate predictions + run harness**

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/recursing-babbage-df3459 && PYTHONPATH=backend python scripts/qiefen_score.py --out harness_out/qiefen_pred`
Expected: 14 `pred.yaml` written (or fewer if a source is missing) + `harness_out/qiefen_pred/_report/leaderboard.md`.

- [ ] **Step 2: Read the leaderboard**

Run: `cat harness_out/qiefen_pred/_report/leaderboard.md`
Expected: a mean weighted score + per-chapter `evidence_atoms / semantic_chunks / objects / relations` percentages. (objects/relations will be low/zero — that is P1.)

- [ ] **Step 3: Record the baseline**

Write `docs/superpowers/specs/2026-05-31-qiefen-p0-baseline.md` capturing: the mean weighted score, the per-stage means for `evidence_atoms`, `semantic_chunks`, `structure`, `context_packages`, `do_not_extract`, and the 3 worst chapters by `evidence_atoms` (the iteration targets for tuning the atomizer/segmenter). Include the date and the exact command used.

- [ ] **Step 4: Set the P0 acceptance line**

Based on the baseline, append to the same file a concrete P0 target for the deterministic buckets (e.g. "mean `evidence_atoms` F1 ≥ X, `semantic_chunks` F1 ≥ Y") and confirm it with the user before further atomizer tuning.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-05-31-qiefen-p0-baseline.md harness_out/qiefen_pred/_report/leaderboard.md
git commit -m "docs(qiefen): record P0 deterministic harness baseline"
```

---

## Self-Review notes

- **Spec coverage:** S1 (Task 2), S2 (Task 3), S3 (Task 4), S4 (Task 5), S5 (Task 6), deterministic do_not_extract (Task 7), data model mirroring gold (Task 1), emit + orchestrator (Task 8), harness adapter + char-span invariant (Task 9), baseline + target-setting (Task 10). LLM S6–S8 and live cutover are explicitly deferred to P1/P2 plans per the spec's phasing.
- **Char-span invariant** is enforced three times: an `assert` in `atomizer.add`, the pipeline test (Task 8), and the cross-chapter parametrized test (Task 9) — matching the harness's own `required` validation.
- **Type consistency:** `run(...)` signature is identical in Task 8, Task 9 test, and Task 10 script; `build_chunks(atoms, profile, section_paths)`, `build_packages(chunks, atoms_by_id, document_title, profile)`, `atomize(source_text, elements, section_id, profile)`, `detect_negatives(atoms)`, `to_yaml(doc)` are used with the same signatures everywhere.
- **Known P0 limitation (documented, not a placeholder):** the atomizer does sentence-level (not sub-sentence) splitting, so chapters where gold splits a "while … we observe …" sentence into sub-atoms will under-recall; Task 10 surfaces exactly these as the iteration targets. Refinement is in-scope for a follow-up once the baseline shows where it hurts.
```
