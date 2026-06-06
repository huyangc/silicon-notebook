# KG 去噪 + nb-012 删除重抽 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 KG 抽取加三层去噪（章节窗口过滤 + 正文概念噪声过滤 + prompt 收紧）和用户可维护的概念白名单，然后删除 nb-012 当前 KG、复用已解析文档在新逻辑下重抽。

**Architecture:** 去噪是通用抽取代码：新 `kg/filters.py` 提供两个纯函数（`should_extract_window` / `is_noise_concept`），在 `extract_graph` 里抽取前过滤窗口、抽取后丢弃噪声 Concept 并连带删悬空边；白名单存 `concept_whitelist` 表、经 `is_noise_concept` 做保护覆盖、开放 CRUD API。删除重抽是仅对 nb-012 的维护编排：P0 备份 → P1 过滤器离线验证 → P2 试抽 1 本 → P3 删全部+逐源重抽+rebuild → P4 评估，两道人工闸门、一步回滚。

**Tech Stack:** Python、FastAPI、SQLite（WAL）、pydantic、numpy、现有 OpenAI 兼容 LLM/embedding adapter、pytest。

**关键约束：** 删除/重抽脚本**必须在后端停掉时运行**（单写者，避免与 uvicorn 双写同一 SQLite）。去噪只影响未来抽取，不动其他 notebook。

---

## File Structure

**新建**
- `backend/app/services/kg/filters.py` — `_norm` / `should_extract_window` / `is_noise_concept`（纯函数）。
- `backend/tests/kg/test_filters.py` — filters 单测。
- `scripts/validate_concept_filter.py` — P1 离线验证（无 LLM、无写库）。
- `scripts/denoise_reextract_nb.py` — P2/P3 编排（doc_type→删除→逐源重抽→rebuild）。

**修改**
- `backend/app/services/kg/models.py` — `KnowledgeGraph` 加 `windows_skipped` / `concepts_dropped`。
- `backend/app/services/kg_ingest.py` — 新增 `drop_noise_concepts`；`extract_graph` 接 `whitelist` 参数、接窗口过滤与概念过滤、回填计数。
- `backend/app/services/kg/extract.py` — `_prompt` 加显式负例。
- `backend/app/services/sqlite_repository.py` — `concept_whitelist` 表 + seed；`concept_whitelist_terms/list/add/remove`；`delete_notebook_kg`；`_run_extraction` 加载白名单并传入、run message 加计数。
- `backend/app/models/schemas.py` — `ConceptWhitelistEntry` / `ConceptWhitelistAdd`。
- `backend/app/api/routes.py` — `GET/POST/DELETE /kg/concept-whitelist`。
- `backend/tests/test_kg_repository.py` — `delete_notebook_kg` + 白名单 repo 测试。
- `backend/tests/test_kg_ingest.py` — `drop_noise_concepts` 测试。

---

## Task 1: `kg/filters.py` —— 窗口过滤 + 概念噪声判定

**Files:**
- Create: `backend/app/services/kg/filters.py`
- Test: `backend/tests/kg/test_filters.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/kg/test_filters.py`:

```python
from app.services.kg.filters import should_extract_window, is_noise_concept
from app.services.kg.parsing import SourceElementQ


def _el(text, typ="paragraph"):
    return SourceElementQ(id="SE-1", type=typ, file="b.md", line_start=1, line_end=1,
                          char_start=0, char_end=len(text), text=text)


# ---- should_extract_window ----

def test_skips_textbook_problem_sections():
    keep, reason = should_extract_window("7 > 7.5 > Problems", [_el("7.1 Calculate the gain.")], "textbook")
    assert keep is False and reason == "textbook_problem_section"


def test_skips_backmatter_index_section():
    keep, reason = should_extract_window("Index", [_el("frequency response, 495")], "textbook")
    assert keep is False and reason == "backmatter_section"


def test_skips_index_like_window():
    els = [_el("frequency response, 495"), _el("input offset voltage, 230"), _el("slew rate, 312")]
    keep, reason = should_extract_window("3 > 3.2 Body", els, "textbook")
    assert keep is False and reason == "index_like_window"


def test_keeps_formula_body_section():
    keep, reason = should_extract_window(
        "9 > 9.6 > Slew Rate",
        [_el("The slew rate is set by the compensation capacitor."), _el("SR = I/C", "formula")],
        "textbook",
    )
    assert keep is True and reason == ""


def test_problem_skip_only_for_textbook():
    keep, _ = should_extract_window("7 > Problems", [_el("Find the gain.")], "academic")
    assert keep is True


# ---- is_noise_concept ----

WL = frozenset()


def test_noise_symbols_dropped():
    for n in ["V_DD", "g_m1", "i_b68", "R_E26", "(W/L)_1", "A_v^+"]:
        assert is_noise_concept(n, WL)[0] is True, n


def test_noise_instance_labels_dropped():
    for n in ["Q12", "M10", "C20"]:
        assert is_noise_concept(n, WL)[0] is True, n


def test_noise_refs_and_sections_dropped():
    assert is_noise_concept("Fig. 5.38", WL)[0] is True
    assert is_noise_concept("Table 2.1", WL)[0] is True
    assert is_noise_concept("8.4.1 Series-Shunt Feedback", WL)[0] is True


def test_noise_too_short_dropped():
    assert is_noise_concept("Q", WL)[0] is True
    assert is_noise_concept("gm", WL)[0] is True


def test_real_concepts_kept():
    for n in ["transconductance", "current mirror", "slew rate",
              "channel length modulation", "Wilson current mirror", "741 op-amp"]:
        assert is_noise_concept(n, WL)[0] is False, n


def test_whitelist_overrides_symbol_rule():
    assert is_noise_concept("VCO", frozenset({"vco"}))[0] is False
    assert is_noise_concept("gm", frozenset({"gm"}))[0] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_filters.py -q`
Expected: FAIL（`ModuleNotFoundError: app.services.kg.filters`）。

- [ ] **Step 3: 实现 filters.py**

Create `backend/app/services/kg/filters.py`:

```python
"""Deterministic KG denoising filters (pure, no IO).

- should_extract_window: skip低价值抽取窗口（习题/索引/参考文献/索引式）。
- is_noise_concept: 判定正文噪声概念（符号/实例号/图号/章节标题/过短），
  白名单命中优先保护。
规则最终取值以现有概念上的离线验证（scripts/validate_concept_filter.py）为准。
"""
from __future__ import annotations

import re
from typing import Sequence, Tuple

from app.services.kg.parsing import SourceElementQ

# --- normalization (must match concept_whitelist 存储/查找口径) ---
_WS_RE = re.compile(r"[\s\-_]+")


def _norm(name: str) -> str:
    return _WS_RE.sub(" ", (name or "").strip().lower())


# --- window filter ---
_PROBLEM_RE = re.compile(r"(^|[>\s])problems?$|(^|[>\s])exercises?$|习题|练习", re.IGNORECASE)
_BACKMATTER_RE = re.compile(r"index|glossary|references|bibliography|索引|参考文献|术语表", re.IGNORECASE)
_INDEX_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /\-(),]+,\s*\d+([,–\-\s\d]+)?$")


def _index_like_ratio(elements: Sequence[SourceElementQ]) -> float:
    texts = [(e.text or "").strip() for e in elements if (e.text or "").strip()]
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if _INDEX_LINE_RE.match(t))
    return hits / len(texts)


def should_extract_window(section_path: str, elements: Sequence[SourceElementQ],
                          doc_type: str) -> Tuple[bool, str]:
    path = section_path or ""
    if (doc_type or "").lower() == "textbook" and _PROBLEM_RE.search(path):
        return False, "textbook_problem_section"
    if _BACKMATTER_RE.search(path):
        return False, "backmatter_section"
    if _index_like_ratio(elements) >= 0.6:
        return False, "index_like_window"
    return True, ""


# --- concept noise filter ---
_REF_RE = re.compile(r"^(fig|figure|table|eq|equation|sec|section|§)\b", re.IGNORECASE)
_SECTION_RE = re.compile(r"^\d+(\.\d+)+")
_INSTANCE_RE = re.compile(r"^[A-Za-z]\d+$")


def is_noise_concept(name: str, whitelist) -> Tuple[bool, str]:
    raw = (name or "").strip()
    if _norm(raw) in whitelist:          # 白名单保护优先
        return False, ""
    if len(raw) <= 2:
        return True, "too_short"
    if raw.isdigit():
        return True, "pure_number"
    if _REF_RE.match(raw):
        return True, "reference"
    if _SECTION_RE.match(raw):
        return True, "section_heading"
    if "_" in raw or "^" in raw:
        return True, "symbol"
    if _INSTANCE_RE.match(raw):
        return True, "instance_label"
    return False, ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_filters.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/filters.py backend/tests/kg/test_filters.py
git commit -m "feat(kg): 去噪过滤器 filters.py(窗口过滤+概念噪声判定)"
```

---

## Task 2: `KnowledgeGraph` 加去噪计数字段

**Files:**
- Modify: `backend/app/services/kg/models.py:34-46`

- [ ] **Step 1: 加字段**

把 `KnowledgeGraph`（现有 `total_windows` / `failed_windows`）改为：

```python
class KnowledgeGraph(BaseModel):
    doc_id: str
    doc_type: str
    nodes: List[Node] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)
    total_windows: int = 0
    failed_windows: int = 0
    windows_skipped: int = 0
    concepts_dropped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = self.model_dump(mode="python", exclude_none=True)
        return {k: d[k] for k in ("doc_id", "doc_type", "nodes", "edges")}
```

- [ ] **Step 2: 验证现有 KG 测试不破**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_models.py tests/test_kg_ingest.py -q`
Expected: PASS（新字段有默认值，向后兼容）。

- [ ] **Step 3: 提交**

```bash
git add backend/app/services/kg/models.py
git commit -m "feat(kg): KnowledgeGraph 增加 windows_skipped/concepts_dropped 计数"
```

---

## Task 3: `extract_graph` 接入窗口过滤 + 概念过滤

**Files:**
- Modify: `backend/app/services/kg_ingest.py:10-14`（import）, `:133-163`（extract_graph）, 新增 `drop_noise_concepts`
- Test: `backend/tests/test_kg_ingest.py`

- [ ] **Step 1: 写 `drop_noise_concepts` 失败测试**

追加到 `backend/tests/test_kg_ingest.py`：

```python
def test_drop_noise_concepts_removes_symbols_and_dangling_edges():
    from app.services.kg.models import Node, Edge
    from app.services.kg_ingest import drop_noise_concepts
    nodes = [
        Node(id="n1", type="Concept", name="current mirror"),
        Node(id="n2", type="Concept", name="V_DD"),
        Node(id="n3", type="Claim", name="The current mirror copies the reference current."),
    ]
    edges = [
        Edge(id="e1", type="about", source_id="n3", target_id="n1"),
        Edge(id="e2", type="about", source_id="n3", target_id="n2"),
    ]
    kept_nodes, kept_edges, dropped = drop_noise_concepts(nodes, edges, frozenset())
    assert dropped == 1
    assert {n.id for n in kept_nodes} == {"n1", "n3"}
    assert {e.id for e in kept_edges} == {"e1"}


def test_drop_noise_concepts_keeps_whitelisted():
    from app.services.kg.models import Node
    from app.services.kg_ingest import drop_noise_concepts
    nodes = [Node(id="n1", type="Concept", name="VCO")]
    kept, _e, dropped = drop_noise_concepts(nodes, [], frozenset({"vco"}))
    assert dropped == 0 and len(kept) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_ingest.py::test_drop_noise_concepts_removes_symbols_and_dangling_edges -q`
Expected: FAIL（`drop_noise_concepts` 未定义）。

- [ ] **Step 3: 改 import + 新增 `drop_noise_concepts`**

把 `backend/app/services/kg_ingest.py` 顶部 import（行 10-14）改为：

```python
from app.services.kg.windowing import windows_with_elements
from app.services.kg.extract import extract_window
from app.services.kg.canonicalize import canonicalize
from app.services.kg.filters import should_extract_window, is_noise_concept
from app.services.kg.models import Edge, KnowledgeGraph, Node
from app.services.kg.scheduler import submit_window
```

在 `plan_window_size` 之后、`extract_graph` 之前新增：

```python
def drop_noise_concepts(nodes: List[Node], edges: List[Edge],
                        whitelist) -> Tuple[List[Node], List[Edge], int]:
    """丢弃噪声 Concept 节点（白名单保护），并移除指向被丢节点的悬空边。
    仅对 Concept 生效；Claim/Formula/Procedure 一律保留。"""
    kept_ids = set()
    kept_nodes: List[Node] = []
    dropped = 0
    for nd in nodes:
        if nd.type == "Concept" and is_noise_concept(nd.name, whitelist)[0]:
            dropped += 1
            continue
        kept_ids.add(nd.id)
        kept_nodes.append(nd)
    kept_edges = [e for e in edges if e.source_id in kept_ids and e.target_id in kept_ids]
    return kept_nodes, kept_edges, dropped
```

- [ ] **Step 4: 改 `extract_graph` 签名与主体**

把 `extract_graph`（行 133-163）整体替换为：

```python
def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, whitelist=frozenset()) -> KnowledgeGraph:
    """Window the text, extract a KG fragment per window concurrently, denoise,
    then canonicalize. 抽取前按 should_extract_window 跳过低价值窗口；抽取后按
    is_noise_concept 丢弃噪声 Concept（连带删悬空边）。Ungroundable nodes are
    dropped inside extract_window."""
    all_pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file,
                                                              None, n, m) if els]
    pairs = []
    windows_skipped = 0
    for w, els in all_pairs:
        keep, _reason = should_extract_window(w.section_path, els, doc_type)
        if keep:
            pairs.append((w, els))
        else:
            windows_skipped += 1
    nodes: List[Node] = []
    edges: List[Edge] = []
    failed = 0
    if pairs:
        futs = [submit_window(extract_window, client, els, w.section_path,
                              doc_type, idx)
                for idx, (w, els) in enumerate(pairs)]
        for fut in futs:
            try:
                ns, es = fut.result()
                nodes += ns
                edges += es
            except Exception:
                failed += 1
    nodes, edges, concepts_dropped = drop_noise_concepts(nodes, edges, whitelist)
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
    return KnowledgeGraph(doc_id=source_file, doc_type=doc_type, nodes=nodes,
                          edges=edges, total_windows=len(pairs),
                          failed_windows=failed, windows_skipped=windows_skipped,
                          concepts_dropped=concepts_dropped)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_ingest.py -q`
Expected: PASS。

> 注：`extract_graph` 的端到端接线（真实 LLM 输出经过滤）由 **P2 试抽**（Task 11）作为集成闸门验证——其核心逻辑已被 `drop_noise_concepts` 与 `should_extract_window` 的纯函数单测覆盖，不再写依赖 LLM/调度池的重型 mock 测试。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/kg_ingest.py backend/tests/test_kg_ingest.py
git commit -m "feat(kg): extract_graph 接入窗口过滤+概念去噪(白名单保护)"
```

---

## Task 4: 抽取 prompt 加显式负例

**Files:**
- Modify: `backend/app/services/kg/extract.py:36-40`
- Test: `backend/tests/kg/test_extract.py`

- [ ] **Step 1: 写 prompt 断言测试**

追加到 `backend/tests/kg/test_extract.py`：

```python
def test_prompt_forbids_symbol_and_label_concepts():
    from app.services.kg.extract import _prompt
    p = _prompt("[0] sample", "1 > intro", "textbook")
    assert "Do NOT emit Concepts" in p
    assert "symbol" in p.lower()
    assert "figure" in p.lower()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_extract.py::test_prompt_forbids_symbol_and_label_concepts -q`
Expected: FAIL（当前 prompt 无 "symbol"/"figure" 负例）。

- [ ] **Step 3: 扩写 `_prompt` 的 selective 段**

把 `extract.py` 行 36-40 的那段（`Be SELECTIVE with Concepts: ...` 到 `... do not skip those.`）替换为：

```python
Be SELECTIVE with Concepts: emit a Concept only for a distinctive named entity. Do
NOT emit Concepts for generic/common terms (e.g. training, inference, buffer,
latency, forward pass, backward pass, hidden state, input sequence, host memory) or
for trivial sub-parts of another concept. Also do NOT emit Concepts for: bare
symbols/variables (e.g. V_DD, g_m1, i_b68, R_E26, (W/L)_1, A_v^+); instance labels
of a specific device/node/pole (e.g. Q1, M5, Pole p8); figure/table/equation/section
references (e.g. Fig. 5.38, Table 2.1, Eq. 9.4); or section headings/numbers (e.g.
"8.4.1 Series-Shunt Feedback"). In contrast, capture EVERY Formula (equation) and
EVERY Procedure (process/phase) present — do not skip those.
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg/test_extract.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/extract.py backend/tests/kg/test_extract.py
git commit -m "feat(kg): 抽取 prompt 显式禁止符号/实例号/图号/章节标题当概念"
```

---

## Task 5: `concept_whitelist` 表 + seed + repo 方法

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（schema 块 ~349-376；`_seed()` ~460-493；新增 4 个方法）
- Test: `backend/tests/test_kg_repository.py`

- [ ] **Step 1: 写白名单 repo 失败测试**

追加到 `backend/tests/test_kg_repository.py`：

```python
def test_builtin_whitelist_seeded(repo):
    terms = repo.concept_whitelist_terms()
    assert "vco" in terms
    assert "mosfet" in terms


def test_whitelist_add_list_remove(repo):
    repo.concept_whitelist_add("Gm Cell", note="custom")
    assert "gm cell" in repo.concept_whitelist_terms()
    listed = repo.concept_whitelist_list()
    assert any(e["term"] == "gm cell" and e["note"] == "custom" for e in listed)
    repo.concept_whitelist_remove("gm cell")
    assert "gm cell" not in repo.concept_whitelist_terms()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_repository.py::test_builtin_whitelist_seeded -q`
Expected: FAIL（`concept_whitelist_terms` 未定义 / 表不存在）。

- [ ] **Step 3: 建表**

在 schema 初始化 SQL 串里、`unified_kg_state` 那个 `CREATE TABLE IF NOT EXISTS unified_kg_state (...)` 块之后，紧接着加入：

```sql
CREATE TABLE IF NOT EXISTS concept_whitelist (
  term TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
```

- [ ] **Step 4: seed 内置术语**

在 `_seed()` 方法（`with self._connect() as db:` 块内，user/user_profiles 两个 INSERT 之后）加入：

```python
        from app.services.kg.filters import _norm as _wl_norm
        builtin_whitelist = [
            "VCO", "PLL", "LNA", "BJT", "MOS", "MOSFET", "CMOS", "FET",
            "NMOS", "PMOS", "BiCMOS", "JFET", "op amp", "ADC", "DAC",
            "CMRR", "PSRR", "ESD", "PVT",
        ]
        for term in builtin_whitelist:
            db.execute(
                "INSERT OR IGNORE INTO concept_whitelist (term, note, created_at) VALUES (?, 'builtin', ?)",
                (_wl_norm(term), now),
            )
```

- [ ] **Step 5: 新增 4 个 repo 方法**

在 `decided_pairs` 方法（行 ~1901-1904）之后加入：

```python
    def concept_whitelist_terms(self) -> set:
        with self._connect() as db:
            return {r["term"] for r in db.execute("SELECT term FROM concept_whitelist").fetchall()}

    def concept_whitelist_list(self) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT term, note, created_at FROM concept_whitelist ORDER BY term").fetchall()
        return [{"term": r["term"], "note": r["note"], "created_at": r["created_at"]} for r in rows]

    def concept_whitelist_add(self, term: str, note: str = "") -> dict:
        from app.services.kg.filters import _norm
        t = _norm(term)
        if not t:
            raise ValueError("empty term")
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO concept_whitelist (term, note, created_at) VALUES (?, ?, ?)",
                (t, note, now),
            )
        return {"term": t, "note": note, "created_at": now}

    def concept_whitelist_remove(self, term: str) -> None:
        from app.services.kg.filters import _norm
        with self._connect() as db:
            db.execute("DELETE FROM concept_whitelist WHERE term = ?", (_norm(term),))
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_repository.py::test_builtin_whitelist_seeded tests/test_kg_repository.py::test_whitelist_add_list_remove -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): concept_whitelist 表+内置seed+CRUD repo 方法"
```

---

## Task 6: `delete_notebook_kg` repo 方法（保留 sources/elements）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（在 `delete_notebook` 之后新增）
- Test: `backend/tests/test_kg_repository.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_kg_repository.py`：

```python
def test_delete_notebook_kg_clears_kg_but_keeps_elements(repo):
    from app.services.sqlite_repository import _now
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._connect() as db:
        db.execute(
            "INSERT INTO source_elements (id, source_id, element_type, location_label, text, metadata, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("el-x", "src-x", "paragraph", "p1", "hello", "{}", now),
        )
        db.execute(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("ko-x", nb.id, "concept", "approved", "", "{}", "[]", None, "src-x", now, now),
        )
        db.execute(
            "INSERT INTO knowledge_relations (id, notebook_id, source_id, source_object_id, target_object_id, "
            "edge_type, evidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("rel-x", nb.id, "src-x", "ko-x", "ko-x", "about", "[]", now),
        )
    counts = repo.delete_notebook_kg(nb.id)
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id='src-x'").fetchone()["c"] == 1
    assert counts["knowledge_objects"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_repository.py::test_delete_notebook_kg_clears_kg_but_keeps_elements -q`
Expected: FAIL（`delete_notebook_kg` 未定义）。

- [ ] **Step 3: 实现方法**

在 `delete_notebook`（行 ~688-697）之后加入：

```python
    def delete_notebook_kg(self, notebook_id: str) -> dict:
        """Delete all KG artifacts for a notebook (objects, relations, clusters,
        merge candidates, embeddings, extraction runs, unified state) while KEEPING
        sources and source_elements so it can be re-extracted from already-parsed
        elements. Returns {table: rows_deleted}."""
        self.get_notebook(notebook_id)
        counts: dict = {}
        with self._connect() as db:
            for table in ("knowledge_objects", "knowledge_relations", "concept_clusters",
                          "concept_merge_candidates", "knowledge_embeddings",
                          "extraction_runs", "unified_kg_state"):
                cur = db.execute(f"DELETE FROM {table} WHERE notebook_id = ?", (notebook_id,))
                counts[table] = cur.rowcount
        self._invalidate_unified_cache(notebook_id)
        return counts
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_repository.py::test_delete_notebook_kg_clears_kg_but_keeps_elements -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): delete_notebook_kg(删 KG 保留 sources/elements 供重抽)"
```

---

## Task 7: `_run_extraction` 加载白名单并传入 + run message 计数

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:1167-1183`（`_run_extraction` 内）

- [ ] **Step 1: 改 extract_graph 调用，注入白名单**

把 `_run_extraction` 里的 `graph = kg_ingest.extract_graph(...)` 调用（行 1167-1171）替换为：

```python
            whitelist = self.concept_whitelist_terms()
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
                whitelist=whitelist,
            )
```

- [ ] **Step 2: run message 追加去噪计数**

把 run message 那行（行 1182-1183，`f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type} windows_failed={fw}/{tw}"`）替换为：

```python
            db.execute("UPDATE extraction_runs SET status='completed', error_message=?, updated_at=? WHERE id=?",
                       (f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type} "
                        f"windows_failed={fw}/{tw} windows_skipped={graph.windows_skipped} "
                        f"concepts_dropped={graph.concepts_dropped}", _now(), run_id))
```

- [ ] **Step 3: 全量 KG 测试回归**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_repository.py tests/test_kg_ingest.py tests/test_unified_kg_repository.py -q`
Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add backend/app/services/sqlite_repository.py
git commit -m "feat(kg): _run_extraction 注入概念白名单 + run message 记去噪计数"
```

---

## Task 8: 概念白名单 API（schemas + routes）

**Files:**
- Modify: `backend/app/models/schemas.py`（文件末尾追加）
- Modify: `backend/app/api/routes.py`（import 段加模型；unified-kg 端点段之后加 3 个端点）

- [ ] **Step 1: 加 schemas 模型**

在 `backend/app/models/schemas.py` 末尾追加：

```python
class ConceptWhitelistEntry(BaseModel):
    term: str
    note: str = ""
    created_at: str = ""


class ConceptWhitelistAdd(BaseModel):
    term: str
    note: str = ""
```

- [ ] **Step 2: routes import 这两个模型**

在 `backend/app/api/routes.py` 顶部 `from app.models.schemas import (` 列表里，按字母位置加入：

```python
    ConceptWhitelistAdd,
    ConceptWhitelistEntry,
```

- [ ] **Step 3: 加 3 个端点**

在 reject_merge 端点（行 ~543-549）之后加入：

```python
@router.get("/kg/concept-whitelist", response_model=List[ConceptWhitelistEntry])
def list_concept_whitelist() -> List[ConceptWhitelistEntry]:
    return [ConceptWhitelistEntry(**e) for e in repository().concept_whitelist_list()]


@router.post("/kg/concept-whitelist", response_model=ConceptWhitelistEntry)
def add_concept_whitelist(payload: ConceptWhitelistAdd) -> ConceptWhitelistEntry:
    try:
        return ConceptWhitelistEntry(**repository().concept_whitelist_add(payload.term, payload.note))
    except ValueError:
        raise HTTPException(status_code=400, detail="term must be non-empty")


@router.delete("/kg/concept-whitelist/{term}", status_code=204)
def delete_concept_whitelist(term: str) -> None:
    repository().concept_whitelist_remove(term)
```

- [ ] **Step 4: 后端 import 冒烟**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -c "import app.main"`
Expected: 无报错（路由/模型导入正常）。

> 端点是对已测 repo 方法（Task 5）的薄包装，遵循文件内既有 unified-kg 端点写法，与同类端点一致不再单测；上线后由 Task 12 的 curl 冒烟覆盖。

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/api/routes.py
git commit -m "feat(api): 概念白名单 CRUD 端点 /kg/concept-whitelist"
```

---

## Task 9: 全仓库检查通过

- [ ] **Step 1: 跑 check.sh + 全 KG 测试**

Run:
```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```
Expected: PASS。若失败，按输出修复后重跑，不要跳过。

---

## Task 10: P1 离线验证脚本（无 LLM、无写库）

**Files:**
- Create: `scripts/validate_concept_filter.py`

- [ ] **Step 1: 写脚本**

Create `scripts/validate_concept_filter.py`:

```python
"""P1: 在现有 notebook 的 concept 上离线试跑 is_noise_concept（无 LLM、无写库）。
用法：
  cd /Users/hzf/workspace/silicon_notebook
  PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/validate_concept_filter.py
"""
import json
import sqlite3

from app.services.kg.filters import is_noise_concept

DB = ".local/silicon_notebook.db"
NB = "nb-012fb94249"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    wl = {r["term"] for r in con.execute("SELECT term FROM concept_whitelist")}
    rows = con.execute(
        "SELECT payload FROM knowledge_objects WHERE notebook_id=? AND object_type='concept'",
        (NB,),
    ).fetchall()
    dropped, kept, reasons = [], [], {}
    for r in rows:
        name = json.loads(r["payload"] or "{}").get("name", "")
        noise, reason = is_noise_concept(name, wl)
        if noise:
            dropped.append((name, reason))
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            kept.append(name)
    print(f"whitelist_terms={len(wl)} concepts={len(rows)} dropped={len(dropped)} kept={len(kept)}")
    print("by_reason:", reasons)
    print("\n--- DROPPED 抽样 (前 50) ---")
    for n, why in dropped[:50]:
        print(f"  [{why}] {n}")
    print("\n--- KEPT 抽样 (每 150 个取 1，查误伤) ---")
    for n in kept[::150][:50]:
        print(f"  {n}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行并人工判读（P1 闸门）**

先确保白名单已 seed 进真实库（首次连库即 seed；如未，启动一次后端或跑任一 repo 即可）。然后：
```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/validate_concept_filter.py
```
**人工闸门**：检查 DROPPED 抽样是否确为噪声、KEPT 抽样有无被误判的真概念。若发现规律性误伤（如某真概念族被符号规则命中）→ 调 `is_noise_concept` 规则或 `concept_whitelist_add` 补白名单，重跑，直到满意。**满意后才进 Task 11。**

- [ ] **Step 3: 提交脚本（含本轮规则/白名单微调）**

```bash
git add scripts/validate_concept_filter.py
git commit -m "tooling(kg): 概念过滤器离线验证脚本(P1)"
```

---

## Task 11: 编排脚本 + P0 备份 + P2 试抽（人工闸门）

**Files:**
- Create: `scripts/denoise_reextract_nb.py`

- [ ] **Step 1: 写编排脚本**

Create `scripts/denoise_reextract_nb.py`:

```python
"""去噪重抽一个 notebook。RUN WITH BACKEND STOPPED（单写者）。
用法：
  cd /Users/hzf/workspace/silicon_notebook
  # 试抽 1 本（不删全部，仅替换该 source 的 KG）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --pilot src-8286a380ae
  # 全量（删 nb-012 全部 KG，再 5 本逐源重抽 + rebuild）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --full
"""
import sys
import time

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

NB = "nb-012fb94249"


def main():
    args = sys.argv[1:]
    pilot = args[args.index("--pilot") + 1] if "--pilot" in args else None
    full = "--full" in args
    if not pilot and not full:
        print("need --pilot SRC_ID or --full")
        sys.exit(2)

    repo = SQLiteRepository(Settings())
    with repo._connect() as db:
        srcs = db.execute("SELECT id, title FROM sources WHERE notebook_id=? ORDER BY id", (NB,)).fetchall()
        db.execute("UPDATE sources SET doc_type='textbook' WHERE notebook_id=?", (NB,))
    print("sources:", [(r["id"], r["title"][:30]) for r in srcs])

    if pilot:
        targets = [pilot]
        print(f"PILOT: 仅重抽 {pilot}（其余 KG 不动）")
    else:
        print("FULL: 删除 nb-012 全部 KG")
        print("deleted:", repo.delete_notebook_kg(NB))
        targets = [r["id"] for r in srcs]

    for sid in targets:
        t = time.perf_counter()
        repo._run_extraction(sid)
        # 读回该 source 的去噪计数
        with repo._connect() as db:
            run = db.execute(
                "SELECT error_message FROM extraction_runs WHERE source_id=? ORDER BY created_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
        print(f"[{sid}] {time.perf_counter()-t:.1f}s :: {run['error_message'] if run else 'n/a'}")

    if full:
        print("rebuild clusters:", repo.rebuild_unified_kg(NB))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 停后端**

```bash
pkill -f "uvicorn app.main:app" || true
sleep 2
lsof -nP -iTCP:8000 -sTCP:LISTEN || echo "8000 已空闲"
```
Expected: 8000 端口空闲（后端已停）。

- [ ] **Step 3: P0 备份（硬闸门，必须成功）**

```bash
cd /Users/hzf/workspace/silicon_notebook
TS=$(date +%Y%m%d_%H%M%S); mkdir -p .local/backups/nb012_$TS
for t in knowledge_objects knowledge_relations concept_clusters concept_merge_candidates knowledge_embeddings extraction_runs unified_kg_state; do
  sqlite3 .local/silicon_notebook.db ".mode insert $t" "SELECT * FROM $t WHERE notebook_id='nb-012fb94249';" > .local/backups/nb012_$TS/$t.sql
done
echo "backup dir: .local/backups/nb012_$TS"; ls -lh .local/backups/nb012_$TS
```
Expected: 7 个 .sql 文件生成（knowledge_objects.sql 应有数万行）。**备份不成功不得进入 Step 4。**

- [ ] **Step 4: P2 试抽最小的一本（CMOS Analog，src 由 Step 1 输出确认）**

> 从 Step 1 打印的 sources 列表里挑 "CMOS_Analog_Circuit_Design" 对应的 src id（约 3866 元素那本，预期是 `src-0eaa98f531`，以实际输出为准）。

```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/denoise_reextract_nb.py --pilot src-0eaa98f531
```
Expected: 打印该源 `windows_skipped=.. concepts_dropped=..`，耗时数分钟。

- [ ] **Step 5: P2 人工闸门——核对噪声确实降了**

```bash
sqlite3 -header -column .local/silicon_notebook.db "
SELECT object_type, COUNT(*) n FROM knowledge_objects
WHERE notebook_id='nb-012fb94249' AND source_id='src-0eaa98f531' GROUP BY object_type;"
sqlite3 .local/silicon_notebook.db "
SELECT json_extract(payload,'\$.name') FROM knowledge_objects
WHERE notebook_id='nb-012fb94249' AND source_id='src-0eaa98f531' AND object_type='concept'
ORDER BY RANDOM() LIMIT 40;"
```
**闸门**：抽样概念里几乎不应再有 `V_DD`/`Q1`/`Fig.x`/`8.4.1 …` 这类。达标 → 进 Task 12；不达标 → 调规则（回 Task 1/10）后重跑本任务。

- [ ] **Step 6: 提交脚本**

```bash
git add scripts/denoise_reextract_nb.py
git commit -m "tooling(kg): nb-012 去噪重抽编排脚本(P0备份/P2试抽/P3全量)"
```

---

## Task 12: P3 全量重抽 + P4 评估（人工闸门后执行）

> 前提：Task 11 的 P2 闸门已通过、后端仍处于停止状态、P0 备份已在手。

- [ ] **Step 1: 抓 BEFORE 基线**

```bash
sqlite3 -header -column .local/silicon_notebook.db "
SELECT object_type, COUNT(*) n FROM knowledge_objects WHERE notebook_id='nb-012fb94249' GROUP BY object_type;"
```
记下当前 concept/claim/formula/procedure 数（重抽前）。

- [ ] **Step 2: 全量删除 + 重抽 + rebuild**

```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/denoise_reextract_nb.py --full
```
Expected: 打印 `deleted: {...}`、5 本各自 `windows_skipped/concepts_dropped`、最后 `rebuild clusters: <n>`。耗时约 30 分钟+（含嵌入）。

- [ ] **Step 3: P4 评估**

```bash
sqlite3 -header -column .local/silicon_notebook.db "
SELECT object_type, COUNT(*) n FROM knowledge_objects WHERE notebook_id='nb-012fb94249' GROUP BY object_type;"
sqlite3 -header -column .local/silicon_notebook.db "
SELECT COUNT(*) members, COUNT(DISTINCT canonical_id) canon FROM concept_clusters WHERE notebook_id='nb-012fb94249';"
# 抽查 rebuild 后的语义合并簇是否还有'大杂烩'
sqlite3 .local/silicon_notebook.db "
WITH m AS (SELECT cc.canonical_id cid, cc.canonical_name cn, json_extract(ko.payload,'\$.name') nm
           FROM concept_clusters cc JOIN knowledge_objects ko ON ko.id=cc.member_object_id
           WHERE cc.notebook_id='nb-012fb94249')
SELECT '['||cn||'] <= '||GROUP_CONCAT(DISTINCT nm) FROM m GROUP BY cid
HAVING COUNT(DISTINCT lower(nm))>1 ORDER BY COUNT(DISTINCT lower(nm)) DESC LIMIT 20;"
```
**评估**：① concept 总数较 BEFORE 明显下降；② 合并簇不再出现符号大杂烩/拓扑混并。记录结论；若合并仍偏松，作为后续“收紧合并阈值”的独立动作（不在本计划内）。

- [ ] **Step 4: 重启后端**

```bash
cd /Users/hzf/workspace/silicon_notebook
nohup /opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir backend > .local/logs/backend_restart.log 2>&1 &
sleep 3
curl -s http://localhost:8000/api/notebooks/nb-012fb94249/unified-kg/status
```
Expected: 返回 status JSON（dirty=false、clusters 为新值）。

> **回滚（仅当 P4 判定变差）**：停后端 → `repo.delete_notebook_kg('nb-012fb94249')` → 回灌 `.local/backups/nb012_<TS>/*.sql`（用 `sqlite3 .local/silicon_notebook.db < <file>`，逐表）→ 重启后端。

---

## 自检（Self-Review）

- **Spec 覆盖**：①窗口过滤=Task1+Task3；②概念噪声过滤=Task1+Task3；③prompt 收紧=Task4；④白名单表+API=Task5+Task8；⑤doc_type=Task11 脚本 UPDATE；⑥删除保留 elements=Task6；⑦P0 备份=Task11 Step3；⑧P1 离线验证=Task10；⑨P2 试抽=Task11；⑩P3 全量+P4 评估=Task12；⑪回滚=Task12 末。全覆盖。
- **占位扫描**：无 TBD/TODO；每个代码步给出完整代码与命令。
- **类型/命名一致**：`is_noise_concept(name, whitelist)`、`should_extract_window(section_path, elements, doc_type)`、`drop_noise_concepts(nodes, edges, whitelist)`、`delete_notebook_kg`、`concept_whitelist_terms/list/add/remove`、`KnowledgeGraph.windows_skipped/concepts_dropped` 在各 Task 间一致。
- **已知调参点**：`Pole p8` 这类“词+编号”不在初版规则内，留待 P1 验证按数据决定是否补规则。
