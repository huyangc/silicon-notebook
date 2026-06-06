# KG 抽取 + 问答评测套件 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 `backend/eval/` 评测套件,在真实 KG 上量化抽取速度、抽取质量(探针)、问答推断三场景,高性价比、可回归。

**Architecture:** 一套数据三场景复用。质量探针纯 SQL 扫现有 KG(0 token);速度用截片段真实抽取计时 + 公式外推;推断用现有 notebook `repo.ask` + LLM-judge。纯逻辑(探针/外推/解析/渲染)走 TDD pytest 合成数据;真实 LLM 部分靠运行验证。

**Tech Stack:** Python 3(miniconda)、sqlite3、pytest、PyYAML、产品 `app.services.sqlite_repository.SQLiteRepository` + `app.core.llm` + `app.services.kg.parsing.parse_elements`。

**对应 spec:** `docs/superpowers/specs/2026-06-06-kg-eval-suite-design.md`

**评测对象:** notebook `nb-012fb94249`("Analog CMOS IC Design");Razavi 本 source_id=`src-9c312953d7`,storage 路径 `.local/storage/notebooks/nb-012fb94249/src-9c312953d7_Design_of_Analog_CMOS_IC_2nd_Ed_Razavi_mineru.md`。

**通用约定:**
- Python:`/opt/homebrew/Caskroom/miniconda/base/bin/python`(下文记为 `$PY`;不可用则 `python3`)。
- 测试命令前缀:`PYTHONPATH=backend $PY -m pytest`。
- 所有 commit 在分支 `kg-eval-suite`,message 末尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 产物目录 `.local/eval_runs/<ts>/`(已被 `.local` gitignore 覆盖)。

---

## File Structure

```
backend/eval/
  __init__.py        # 包标记(空)
  db.py              # source_of() 纯函数 + EvalDB 只读取数(objects/relations/按书归属/关系度)
  probes.py          # 探针纯函数(concept/claim/formula/procedure) + aggregate_quality() 聚合 + run_quality()
  speed.py           # estimate_extract_seconds()/parse_llm_log() 纯函数 + measure_speed() 真实抽取计时
  inference.py       # load_questions()/judge_prompt()/parse_judge() 纯函数 + run_inference() 真实问答
  report.py          # render_quality_report()/render_speed_report()/render_inference_report() 纯函数
  questions.yaml     # 30 题(spec §6.1)
  run_all.py         # CLI 编排,落 .local/eval_runs/<ts>/
backend/tests/eval/
  __init__.py
  test_db.py
  test_probes.py
  test_speed.py
  test_inference.py
  test_report.py
```

测试策略:`db/probes/speed(纯)/inference(纯)/report` 全部 TDD;`speed.measure_speed`、`inference.run_inference`、`run_all` 靠运行验证(需真实库/LLM,不写 pytest)。

---

## Task 1: 包脚手架 + db.py(只读取数 + 按书归属)

**Files:**
- Create: `backend/eval/__init__.py`, `backend/tests/eval/__init__.py`
- Create: `backend/eval/db.py`
- Test: `backend/tests/eval/test_db.py`

- [ ] **Step 1: 建包标记文件**

```bash
mkdir -p backend/eval backend/tests/eval
: > backend/eval/__init__.py
: > backend/tests/eval/__init__.py
```

- [ ] **Step 2: 写失败测试 `test_db.py`(source_of 纯函数 + EvalDB 临时库)**

```python
import json, sqlite3, pathlib
from app.eval.db import source_of, EvalDB


def test_source_of_takes_first_evidence():
    ev = json.dumps([{"source_id": "src-A", "element_id": "e1"},
                     {"source_id": "src-B"}])
    assert source_of(ev) == "src-A"


def test_source_of_handles_empty_and_bad():
    assert source_of("[]") is None
    assert source_of("") is None
    assert source_of("not json") is None
    assert source_of(None) is None


def _mk_db(tmp_path):
    p = tmp_path / "t.db"
    db = sqlite3.connect(p)
    db.executescript(
        """
        CREATE TABLE knowledge_objects(id TEXT, notebook_id TEXT, object_type TEXT,
          status TEXT, payload TEXT, evidence TEXT);
        CREATE TABLE knowledge_relations(id TEXT, notebook_id TEXT, source_id TEXT,
          source_object_id TEXT, target_object_id TEXT, edge_type TEXT, evidence TEXT);
        """)
    db.execute("INSERT INTO knowledge_objects VALUES(?,?,?,?,?,?)",
               ("ko1", "nb", "concept", "approved",
                json.dumps({"name": "cascode", "section_path": "5"}),
                json.dumps([{"source_id": "src-A", "element_id": "e1"}])))
    db.execute("INSERT INTO knowledge_objects VALUES(?,?,?,?,?,?)",
               ("ko2", "nb", "concept", "approved",
                json.dumps({"name": "Vb1", "section_path": "5"}),
                json.dumps([{"source_id": "src-B", "element_id": "e2"}])))
    db.execute("INSERT INTO knowledge_relations VALUES(?,?,?,?,?,?,?)",
               ("r1", "nb", "src-A", "ko1", "ko2", "about", "[]"))
    db.commit(); db.close()
    return p


def test_evaldb_objects_and_degree(tmp_path):
    p = _mk_db(tmp_path)
    ed = EvalDB(str(p))
    objs = ed.objects("nb", "concept")
    assert len(objs) == 2
    by_name = {o["name"]: o for o in objs}
    assert by_name["cascode"]["source_id"] == "src-A"
    assert by_name["cascode"]["payload"]["section_path"] == "5"
    deg = ed.relation_degree("nb")
    assert deg["ko1"] == 1 and deg["ko2"] == 1
```

- [ ] **Step 3: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_db.py -v`
Expected: FAIL(`ModuleNotFoundError: app.eval.db`)

- [ ] **Step 4: 实现 `backend/eval/db.py`**

```python
"""只读评测取数:不修改任何产品数据。"""
from __future__ import annotations
import json, sqlite3
from typing import Any, Dict, List, Optional


def source_of(evidence_json: Optional[str]) -> Optional[str]:
    """按书归属 = evidence 数组首元素的 source_id。"""
    try:
        ev = json.loads(evidence_json or "[]")
    except (ValueError, TypeError):
        return None
    if isinstance(ev, list) and ev and isinstance(ev[0], dict):
        return ev[0].get("source_id")
    return None


class EvalDB:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def objects(self, notebook_id: str, object_type: str) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, payload, evidence FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=?",
                (notebook_id, object_type)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            out.append({
                "id": r["id"],
                "name": payload.get("name", ""),
                "payload": payload,
                "evidence": json.loads(r["evidence"] or "[]"),
                "evidence_count": len(json.loads(r["evidence"] or "[]")),
                "source_id": source_of(r["evidence"]),
            })
        return out

    def relation_degree(self, notebook_id: str) -> Dict[str, int]:
        deg: Dict[str, int] = {}
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_object_id, target_object_id FROM knowledge_relations "
                "WHERE notebook_id=?", (notebook_id,)).fetchall()
        for r in rows:
            for k in (r["source_object_id"], r["target_object_id"]):
                if k:
                    deg[k] = deg.get(k, 0) + 1
        return deg

    def source_titles(self, notebook_id: str) -> Dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, file_name FROM sources WHERE notebook_id=?",
                (notebook_id,)).fetchall()
        return {r["id"]: r["file_name"] for r in rows}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_db.py -v`
Expected: PASS(4 passed)

- [ ] **Step 6: 真实库冒烟(确认能读主库)**

Run:
```bash
PYTHONPATH=backend $PY -c "from app.eval.db import EvalDB; e=EvalDB('.local/silicon_notebook.db'); c=e.objects('nb-012fb94249','concept'); print('concepts:',len(c)); print('razavi:',sum(1 for o in c if o['source_id']=='src-9c312953d7'))"
```
Expected: 打印 `concepts: 7955` 量级、`razavi: 1500` 量级(数值可不同,>0 即可)。

- [ ] **Step 7: Commit**

```bash
git add backend/eval/__init__.py backend/tests/eval/__init__.py backend/eval/db.py backend/tests/eval/test_db.py
git commit -m "feat(eval): db.py 只读取数 + 按书归属(source_of/EvalDB)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: probes.py — concept 探针纯函数

**Files:**
- Create: `backend/eval/probes.py`
- Test: `backend/tests/eval/test_probes.py`

- [ ] **Step 1: 写失败测试(用 §2.3 真实样例固化期望)**

```python
from app.eval.probes import (classify_concept, enumerated_groups,
                             near_duplicate_groups)


def test_symbol_variables():
    assert "symbol" in classify_concept("Vb1")
    assert "symbol" in classify_concept("R_0")
    assert "symbol" in classify_concept("Z_out,0")
    assert "symbol" in classify_concept("place_opt_design")  # innovus 小写命令(无空格含_)


def test_reference_like():
    assert "reference" in classify_concept("Circuit of Fig. 12.3(a)")
    assert "reference" in classify_concept("CMFB using error amplifier (Fig. 9.51)")
    assert "reference" in classify_concept("Table 2-1")


def test_quantity_like():
    assert "quantity" in classify_concept("7nm")
    assert "quantity" in classify_concept("0.18 um process")
    assert "quantity" in classify_concept("3.5 GHz")


def test_code_identifier_not_misfiring_on_terms():
    assert "code" in classify_concept("SET_DB")
    assert "code" in classify_concept("getValue()")
    assert "code" not in classify_concept("FinFET")   # 驼峰术语不算代码
    assert "code" not in classify_concept("MOSFET")


def test_clean_concepts_have_no_tags():
    assert classify_concept("cascode connection") == set()
    assert classify_concept("current mirror") == set()
    assert classify_concept("bandgap reference") == set()


def test_enumerated_groups_catches_level_models():
    names = ["Level 1 Model", "Level 2 Model", "Level 3 Model", "current mirror"]
    groups = enumerated_groups(names)
    assert "level # model" in groups
    assert set(groups["level # model"]) == {"Level 1 Model", "Level 2 Model", "Level 3 Model"}


def test_near_duplicate_groups():
    names = ["S_A-B = (rate A) / (rate B)", "S_A-B = rate A / rate B", "noise"]
    groups = near_duplicate_groups(names)
    assert any(len(v) >= 2 for v in groups.values())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_probes.py -v`
Expected: FAIL(`ImportError`)

- [ ] **Step 3: 实现 concept 探针(写入 `backend/eval/probes.py`)**

```python
"""KG 质量探针:返回'疑似信号',非定论(见 spec §4.4 精度校准)。"""
from __future__ import annotations
import re
from collections import defaultdict
from typing import Dict, List, Set

_REF_RE = re.compile(r"\b(fig|figure|table|eq|equation|section|chapter)\b\.?\s*[\d-]", re.I)
_UNIT_RE = re.compile(
    r"\d+\.?\d*\s?(nm|um|µm|mm|kv|mv|v|ma|ua|a|khz|mhz|ghz|hz|db|ohm|ff|pf|nf|f|mw|w)\b",
    re.I)
_CODE_CALL_RE = re.compile(r"\w\(")
_CODE_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_SYM_AN_RE = re.compile(r"^[A-Za-z]{1,4}\d+$")


def classify_concept(name: str) -> Set[str]:
    """返回命中的探针标签:symbol/reference/quantity/code/short。"""
    tags: Set[str] = set()
    n = (name or "").strip()
    if not n:
        return tags
    low = n.lower()
    if len(n) <= 2:
        tags.add("short")
    if _REF_RE.search(low) or low.startswith("circuit of"):
        tags.add("reference")
    if _UNIT_RE.search(low):
        tags.add("quantity")
    if _CODE_CALL_RE.search(n) or (_CODE_CONST_RE.match(n) and "_" in n):
        tags.add("code")
    if " " not in n and ("_" in n or "," in n or _SYM_AN_RE.match(n)):
        tags.add("symbol")
    return tags


def _mask(name: str) -> str:
    """数字/罗马数字掩码为 #,小写归一。'Level 1 Model' -> 'level # model'。"""
    s = re.sub(r"\b[IVXLC]+\b", "#", name, flags=re.I)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def enumerated_groups(names: List[str]) -> Dict[str, List[str]]:
    """P3 取值枚举:同掩码下有 >=2 个不同原名的组。"""
    buckets: Dict[str, Set[str]] = defaultdict(set)
    for nm in names:
        m = _mask(nm)
        if "#" in m:
            buckets[m].add(nm)
    return {k: sorted(v) for k, v in buckets.items() if len(v) >= 2}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()


def near_duplicate_groups(names: List[str]) -> Dict[str, List[str]]:
    """P8 近重复:归一化后同名 >=2 的组。"""
    buckets: Dict[str, List[str]] = defaultdict(list)
    for nm in names:
        buckets[_norm(nm)].append(nm)
    return {k: v for k, v in buckets.items() if len(v) >= 2 and k}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_probes.py -v`
Expected: PASS(7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/eval/probes.py backend/tests/eval/test_probes.py
git commit -m "feat(eval): concept 探针(符号/引用/数量/代码/取值枚举/近重复)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: probes.py — claim/formula/procedure 退化 + aggregate_quality 聚合

**Files:**
- Modify: `backend/eval/probes.py`(追加函数)
- Test: `backend/tests/eval/test_probes.py`(追加)

- [ ] **Step 1: 追加失败测试**

```python
from app.eval.probes import (claim_degraded, formula_degraded,
                             procedure_degraded, aggregate_quality)


def test_claim_degraded():
    assert not claim_degraded("The cascode connection increases output resistance.")
    assert claim_degraded("cascode")                  # 太短/无动词
    assert claim_degraded("output resistance of the")  # 截断结尾


def test_formula_degraded():
    assert not formula_degraded("f_write = (M/(2N)) * f_ref")
    assert not formula_degraded("$C/g_m$")
    assert formula_degraded("the gain")               # 无运算符/等号/数学符号


def test_procedure_degraded():
    assert procedure_degraded({"name": "Analysis process"})         # 无 steps
    assert procedure_degraded({"name": "x", "steps": []})           # 空 steps
    assert not procedure_degraded({"name": "x", "steps": [{"name": "a"}, {"name": "b"}]})


def test_aggregate_quality_counts_and_rate():
    concepts = [
        {"id": "1", "name": "cascode connection", "evidence_count": 3},
        {"id": "2", "name": "Vb1", "evidence_count": 1},
        {"id": "3", "name": "Circuit of Fig. 9.1", "evidence_count": 1},
        {"id": "4", "name": "Level 1 Model", "evidence_count": 1},
        {"id": "5", "name": "Level 2 Model", "evidence_count": 1},
    ]
    degree = {"1": 2}  # 只有 cascode 有关系;其余度=0
    m = aggregate_quality(concepts, degree)
    assert m["total"] == 5
    assert m["probe_counts"]["symbol"] >= 1
    assert m["probe_counts"]["reference"] >= 1
    assert m["enumerated_groups"] >= 1          # Level 1/2 Model
    assert m["orphans"] >= 1                     # 度=0 且 evidence=1
    assert 0.0 < m["suspect_non_atomic_rate"] <= 1.0
    assert len(m["samples"]["symbol"]) >= 1     # 样例清单
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_probes.py::test_aggregate_quality_counts_and_rate -v`
Expected: FAIL(`ImportError: cannot import name 'aggregate_quality'`)

- [ ] **Step 3: 追加实现到 `backend/eval/probes.py`**

```python
_VERB_RE = re.compile(
    r"\b(is|are|was|were|be|has|have|had|can|cannot|will|provides?|requires?|"
    r"causes?|increases?|reduces?|decreases?|achieves?|applies|operates?|"
    r"depends?|uses?|results?|produces?|equals?|yields?|improves?|limits?)\b", re.I)


def claim_degraded(name: str) -> bool:
    n = (name or "").strip()
    words = n.split()
    if len(words) < 4:
        return True
    if not _VERB_RE.search(n):
        return True
    if re.search(r"\b(the|a|an|of|to|for|and|or|with|by|in|on)$", n, re.I):
        return True
    return False


def formula_degraded(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    return not re.search(r"[=+\-*/^$\\<>]", n)


def procedure_degraded(payload: dict) -> bool:
    steps = (payload or {}).get("steps") or []
    return len(steps) == 0


_NON_ATOMIC = ("symbol", "reference", "quantity", "code", "short")


def aggregate_quality(concepts: List[dict], degree: Dict[str, int]) -> dict:
    counts: Dict[str, int] = defaultdict(int)
    samples: Dict[str, List[str]] = defaultdict(list)
    suspect_ids: Set[str] = set()
    orphans = 0
    for c in concepts:
        tags = classify_concept(c["name"])
        for t in tags:
            counts[t] += 1
            if len(samples[t]) < 20:
                samples[t].append(c["name"])
        if tags & set(_NON_ATOMIC):
            suspect_ids.add(c["id"])
        if c.get("evidence_count", 1) <= 1 and degree.get(c["id"], 0) == 0:
            orphans += 1
    names = [c["name"] for c in concepts]
    enum = enumerated_groups(names)
    dups = near_duplicate_groups(names)
    total = len(concepts) or 1
    return {
        "total": len(concepts),
        "probe_counts": dict(counts),
        "orphans": orphans,
        "enumerated_groups": len(enum),
        "enumerated_samples": dict(list(enum.items())[:20]),
        "near_duplicate_groups": len(dups),
        "suspect_non_atomic": len(suspect_ids),
        "suspect_non_atomic_rate": round(len(suspect_ids) / total, 4),
        "samples": {k: v for k, v in samples.items()},
    }
```

- [ ] **Step 4: 跑全部 probes 测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_probes.py -v`
Expected: PASS(全部 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/eval/probes.py backend/tests/eval/test_probes.py
git commit -m "feat(eval): claim/formula/procedure 退化探针 + aggregate_quality 聚合" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: report.py 质量报告 + run_quality 真实库产出

**Files:**
- Create: `backend/eval/report.py`
- Modify: `backend/eval/probes.py`(追加 run_quality)
- Test: `backend/tests/eval/test_report.py`

- [ ] **Step 1: 写失败测试(报告渲染纯函数)**

```python
from app.eval.report import render_quality_report


def test_render_quality_report_has_sections():
    per_book = {
        "Razavi": {"concept": {"total": 1500, "suspect_non_atomic": 200,
                               "suspect_non_atomic_rate": 0.13,
                               "probe_counts": {"symbol": 78, "reference": 159},
                               "orphans": 50, "enumerated_groups": 5,
                               "near_duplicate_groups": 3,
                               "enumerated_samples": {"level # model": ["Level 1 Model", "Level 2 Model"]},
                               "samples": {"symbol": ["Vb1", "R_0"]}},
                   "claim": {"total": 2000, "degraded": 120, "degraded_rate": 0.06,
                             "samples": ["cascode"]}},
    }
    md = render_quality_report(per_book)
    assert "# KG 抽取质量报告" in md
    assert "Razavi" in md
    assert "疑似非原子" in md
    assert "Vb1" in md          # 样例出现
    assert "Level 1 Model" in md
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_report.py -v`
Expected: FAIL(`ModuleNotFoundError: app.eval.report`)

- [ ] **Step 3: 实现 `backend/eval/report.py`(本任务先实现质量部分)**

```python
"""把指标 dict 渲染为 markdown 报告(纯函数)。"""
from __future__ import annotations
from typing import Dict, List


def _h(s: str) -> str:
    return f"\n## {s}\n"


def render_quality_report(per_book: Dict[str, dict]) -> str:
    out: List[str] = ["# KG 抽取质量报告", "",
                      "> 探针给的是**疑似信号**,非定论;含合理变体误报,真噪声率需抽样校准(见 spec §4.4)。"]
    for book, by_type in per_book.items():
        out.append(_h(f"书:{book}"))
        cm = by_type.get("concept", {})
        out.append(f"- concept 总数:{cm.get('total', 0)}")
        out.append(f"- **疑似非原子率:{cm.get('suspect_non_atomic_rate', 0):.1%}** "
                   f"({cm.get('suspect_non_atomic', 0)} 个)")
        out.append(f"- 孤儿节点:{cm.get('orphans', 0)};取值枚举组:{cm.get('enumerated_groups', 0)};"
                   f"近重复组:{cm.get('near_duplicate_groups', 0)}")
        pc = cm.get("probe_counts", {})
        if pc:
            out.append("- 各探针命中:" + ", ".join(f"{k}={v}" for k, v in sorted(pc.items())))
        for other in ("claim", "formula", "procedure"):
            om = by_type.get(other)
            if om:
                out.append(f"- {other}:总数 {om.get('total', 0)},"
                           f"退化 {om.get('degraded', 0)}({om.get('degraded_rate', 0):.1%})")
        samples = cm.get("samples", {})
        for tag, items in sorted(samples.items()):
            if items:
                out.append(f"  - 样例[{tag}]:" + "; ".join(items[:20]))
        for mask, variants in list(cm.get("enumerated_samples", {}).items())[:20]:
            out.append(f"  - 枚举组「{mask}」:" + "; ".join(variants[:8]))
    return "\n".join(out) + "\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_report.py -v`
Expected: PASS(1 passed)

- [ ] **Step 5: 追加 `run_quality` 到 `backend/eval/probes.py`**

```python
def run_quality(db_path: str, notebook_id: str) -> Dict[str, dict]:
    """扫现有 KG,按书 × 类型 聚合质量指标。返回 {book_label: {type: metrics}}。"""
    from app.eval.db import EvalDB
    ed = EvalDB(db_path)
    titles = ed.source_titles(notebook_id)

    def book_label(src_id):
        name = titles.get(src_id, src_id or "unknown")
        return (name or "unknown")[:36]

    per_book: Dict[str, dict] = defaultdict(dict)
    degree = ed.relation_degree(notebook_id)

    concepts_by_book: Dict[str, List[dict]] = defaultdict(list)
    for c in ed.objects(notebook_id, "concept"):
        concepts_by_book[book_label(c["source_id"])].append(c)
    for book, items in concepts_by_book.items():
        per_book[book]["concept"] = aggregate_quality(items, degree)

    degraders = {"claim": claim_degraded, "formula": formula_degraded}
    for otype, fn in degraders.items():
        by_book: Dict[str, List[dict]] = defaultdict(list)
        for o in ed.objects(notebook_id, otype):
            by_book[book_label(o["source_id"])].append(o)
        for book, items in by_book.items():
            bad = sum(1 for o in items if fn(o["name"]))
            total = len(items) or 1
            per_book[book][otype] = {
                "total": len(items), "degraded": bad,
                "degraded_rate": round(bad / total, 4),
                "samples": [o["name"] for o in items if fn(o["name"])][:20],
            }
    proc_by_book: Dict[str, List[dict]] = defaultdict(list)
    for o in ed.objects(notebook_id, "procedure"):
        proc_by_book[book_label(o["source_id"])].append(o)
    for book, items in proc_by_book.items():
        bad = sum(1 for o in items if procedure_degraded(o["payload"]))
        total = len(items) or 1
        per_book[book]["procedure"] = {
            "total": len(items), "degraded": bad,
            "degraded_rate": round(bad / total, 4),
            "samples": [o["name"] for o in items if procedure_degraded(o["payload"])][:20],
        }
    return dict(per_book)
```

- [ ] **Step 6: 真实库冒烟产出质量报告**

Run:
```bash
PYTHONPATH=backend $PY -c "
from app.eval.probes import run_quality
from app.eval.report import render_quality_report
pb = run_quality('.local/silicon_notebook.db','nb-012fb94249')
print(render_quality_report(pb))" | tee /tmp/quality_preview.md | head -40
```
Expected: 输出 5 本书分节,每本含「疑似非原子率」(Razavi 约 10–15%),并列出 `Vb1`/`Circuit of Fig.`/`Level # Model` 等样例。人工扫一眼确认数字合理。

- [ ] **Step 7: Commit**

```bash
git add backend/eval/report.py backend/eval/probes.py backend/tests/eval/test_report.py
git commit -m "feat(eval): 质量报告渲染 + run_quality 按书×类型聚合" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: speed.py — 外推公式 + 日志解析(纯函数)

**Files:**
- Create: `backend/eval/speed.py`
- Test: `backend/tests/eval/test_speed.py`

- [ ] **Step 1: 写失败测试**

```python
import json
from app.eval.speed import estimate_extract_seconds, parse_llm_log, plan_windows


def test_plan_windows_matches_product():
    # 生产配置 workers=1000:level=clamp(1M/1000=1000,4000,8000)=4000 -> 250 窗口,均分回 4000
    size, n = plan_windows(1_000_000, 1000, 4000, 8000)
    assert size == 4000 and n == 250
    # workers=100 时 level=clamp(10000,4000,8000)=8000 -> 125 窗口(对齐现有 test_adaptive_windows)
    assert plan_windows(1_000_000, 100, 4000, 8000) == (8000, 125)


def test_estimate_monotonic_and_formula():
    # 25 窗口、有效并发 16、单窗口 2s、固定开销 3s -> ceil(25/16)*2 + 3 = 7
    assert estimate_extract_seconds(n_windows=25, effective_concurrency=16,
                                    per_window_p50_s=2.0, fixed_overhead_s=3.0) == 7.0
    a = estimate_extract_seconds(10, 16, 2.0, 3.0)
    b = estimate_extract_seconds(100, 16, 2.0, 3.0)
    assert b > a


def test_parse_llm_log_filters_by_ts(tmp_path):
    p = tmp_path / "llm.jsonl"
    lines = [
        {"ts": "2026-06-06T10:00:00", "kind": "chat", "status": "ok",
         "latency_ms": 1000, "usage": {"total_tokens": 700}},
        {"ts": "2026-06-06T12:00:00", "kind": "chat", "status": "ok",
         "latency_ms": 2000, "usage": {"total_tokens": 500}},
        {"ts": "2026-06-06T12:00:01", "kind": "chat", "status": "retry",
         "latency_ms": 50},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines))
    stats = parse_llm_log(str(p), since_ts="2026-06-06T11:00:00")
    assert stats["calls"] == 1            # 只统计 ok,且 ts 在 since 之后
    assert stats["retries"] == 1
    assert stats["latency_p50_s"] == 2.0
    assert stats["total_tokens"] == 500
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_speed.py -v`
Expected: FAIL(`ModuleNotFoundError`)

- [ ] **Step 3: 实现 `backend/eval/speed.py`(本任务只放纯函数)**

```python
"""抽取速度:截片段真实抽取计时 + 公式外推。"""
from __future__ import annotations
import json, math
from statistics import median
from typing import Dict, List, Optional, Tuple

from app.services.kg_ingest import plan_window_size


def plan_windows(chars: int, workers: int, w_min: int, w_max: int) -> Tuple[int, int]:
    size = plan_window_size(chars, workers, w_min, w_max)
    n = math.ceil(chars / size) if size else 0
    return size, n


def estimate_extract_seconds(n_windows: int, effective_concurrency: int,
                             per_window_p50_s: float, fixed_overhead_s: float) -> float:
    conc = max(1, effective_concurrency)
    batches = math.ceil(n_windows / conc) if n_windows else 0
    return round(batches * per_window_p50_s + fixed_overhead_s, 2)


def parse_llm_log(path: str, since_ts: str) -> Dict[str, float]:
    lats: List[float] = []
    tokens = 0
    retries = 0
    try:
        raw = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return {"calls": 0, "retries": 0, "latency_p50_s": 0.0,
                "latency_p95_s": 0.0, "total_tokens": 0}
    for line in raw:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "chat" or rec.get("ts", "") < since_ts:
            continue
        if rec.get("status") == "retry":
            retries += 1
            continue
        if rec.get("status") != "ok":
            continue
        lats.append(rec.get("latency_ms", 0) / 1000.0)
        tokens += (rec.get("usage") or {}).get("total_tokens", 0)
    lats.sort()

    def pct(p):
        if not lats:
            return 0.0
        return round(lats[min(len(lats) - 1, int(p * len(lats)))], 3)
    return {
        "calls": len(lats),
        "retries": retries,
        "latency_p50_s": round(median(lats), 3) if lats else 0.0,
        "latency_p95_s": pct(0.95),
        "total_tokens": tokens,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_speed.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/eval/speed.py backend/tests/eval/test_speed.py
git commit -m "feat(eval): 速度外推公式 + llm.jsonl 解析(纯函数)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: speed.py — 真实抽取计时 + 速度报告

**Files:**
- Modify: `backend/eval/speed.py`(追加 measure_speed)
- Modify: `backend/eval/report.py`(追加 render_speed_report)
- Test: `backend/tests/eval/test_report.py`(追加 render_speed_report 测试)

- [ ] **Step 1: 写 render_speed_report 失败测试**

```python
from app.eval.report import render_speed_report


def test_render_speed_report():
    measured = [
        {"chars": 5000, "n_windows": 1, "wall_s": 6.0,
         "latency_p50_s": 3.0, "latency_p95_s": 5.0, "total_tokens": 1500,
         "retries": 0, "effective_concurrency": 1},
        {"chars": 100000, "n_windows": 13, "wall_s": 9.0,
         "latency_p50_s": 4.0, "latency_p95_s": 8.0, "total_tokens": 30000,
         "retries": 2, "effective_concurrency": 13},
    ]
    extrapolated = [{"chars": 500000, "n_windows": 63, "est_s": 21.0}]
    md = render_speed_report(measured, extrapolated, recommended_max_chars=250000,
                             target_seconds=120)
    assert "# KG 抽取速度报告" in md
    assert "100000" in md or "100,000" in md
    assert "推荐文档上限" in md
    assert "250" in md
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_report.py::test_render_speed_report -v`
Expected: FAIL(`cannot import name 'render_speed_report'`)

- [ ] **Step 3: 追加 `render_speed_report` 到 `backend/eval/report.py`**

```python
def render_speed_report(measured: list, extrapolated: list,
                        recommended_max_chars: int, target_seconds: int) -> str:
    out = ["# KG 抽取速度报告", "",
           f"目标:单文档抽取 ≤ {target_seconds}s。瓶颈在 deepseek 限流/承载(WORKERS=1000)。", "",
           "## 实测", "",
           "| 字数 | 窗口数 | 墙钟(s) | 单窗口p50(s) | p95(s) | tokens | 重试 | 有效并发 |",
           "|---|---|---|---|---|---|---|---|"]
    for r in measured:
        out.append(f"| {r['chars']} | {r['n_windows']} | {r['wall_s']:.1f} | "
                   f"{r['latency_p50_s']:.1f} | {r['latency_p95_s']:.1f} | "
                   f"{r['total_tokens']} | {r['retries']} | {r['effective_concurrency']} |")
    out += ["", "## 外推", "", "| 字数 | 窗口数 | 预估耗时(s) |", "|---|---|---|"]
    for r in extrapolated:
        out.append(f"| {r['chars']} | {r['n_windows']} | {r['est_s']:.1f} |")
    out += ["", f"## 推荐文档上限",
            f"满足 ≤ {target_seconds}s 的最大文档约 **{recommended_max_chars} 字符**;"
            f"超出建议拆分上传或下调 KG_EXTRACT_WORKERS 以减少限流重试。"]
    return "\n".join(out) + "\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_report.py -v`
Expected: PASS(全部 passed)

- [ ] **Step 5: 追加 `measure_speed` 到 `backend/eval/speed.py`(真实抽取,复用 smoke insert_source 模式)**

```python
import pathlib, tempfile, time, uuid
from datetime import datetime


def _truncate_on_paragraph(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    return text[:cut] if cut > 1000 else text[:limit]


def _insert_source(repo, nb_id, name, text, tmpdir):
    from app.services.kg.parsing import parse_elements
    from app.services.sqlite_repository import _now
    f = pathlib.Path(tmpdir) / f"{name}.md"
    f.write_text(text, encoding="utf-8")
    sid = f"src-{uuid.uuid4().hex[:10]}"
    now = _now()
    els = parse_elements(text, source_file=str(f))
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed', ?, ?, 0, '', '', ?, ?, ?)""",
            (sid, nb_id, name, f"{name}.md", str(f), "textbook", now, now))
        for el in els:
            db.execute(
                """INSERT INTO source_elements
                   (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, '{}', ?)""",
                (f"el-{uuid.uuid4().hex[:10]}", sid, el.type,
                 f"L{el.line_start}-{el.line_end}", el.text, now))
    return sid


def _cleanup(repo, nb_id):
    with repo._connect() as db:
        sids = [r[0] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (nb_id,)).fetchall()]
        for sid in sids:
            db.execute("DELETE FROM source_elements WHERE source_id=?", (sid,))
        db.execute("DELETE FROM knowledge_relations WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM knowledge_objects WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM sources WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM notebooks WHERE id=?", (nb_id,))


def measure_speed(source_md_path: str, char_steps: Optional[List[int]] = None,
                  llm_log_path: str = ".local/logs/llm.jsonl") -> List[dict]:
    """对一份源 md 按 char_steps 各截一段、真实抽取计时。用临时 notebook,跑完清理。"""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import SQLiteRepository
    char_steps = char_steps or [5000, 20000, 50000, 100000, 200000]
    settings = Settings()
    repo = SQLiteRepository(settings)
    assert repo.llm_client.configured, "LLM 未配置(.env)"
    full = pathlib.Path(source_md_path).read_text(encoding="utf-8")
    tmpdir = tempfile.mkdtemp()
    results: List[dict] = []
    for limit in char_steps:
        text = _truncate_on_paragraph(full, limit)
        nb = repo.create_notebook(NotebookCreate(name=f"eval-speed-{limit}-{uuid.uuid4().hex[:6]}"))
        try:
            sid = _insert_source(repo, nb.id, f"seg{limit}", text, tmpdir)
            since = datetime.now().isoformat()
            t0 = time.perf_counter()
            repo._run_extraction(sid)
            wall = time.perf_counter() - t0
            size, n = plan_windows(len(text), settings.kg_extract_workers,
                                   settings.kg_window_min_chars, settings.kg_window_max_chars)
            stats = parse_llm_log(llm_log_path, since)
            eff = max(1, min(n, stats["calls"]) if stats["calls"] else n)
            results.append({
                "chars": len(text), "n_windows": n, "window_size": size,
                "wall_s": round(wall, 2),
                "latency_p50_s": stats["latency_p50_s"], "latency_p95_s": stats["latency_p95_s"],
                "total_tokens": stats["total_tokens"], "retries": stats["retries"],
                "effective_concurrency": eff,
            })
            print(f"[speed] {len(text)} chars -> {n} win, wall={wall:.1f}s, "
                  f"p50={stats['latency_p50_s']}s, retries={stats['retries']}", flush=True)
        finally:
            _cleanup(repo, nb.id)
    return results


def extrapolate(measured: List[dict], target_chars: List[int],
                workers: int, w_min: int, w_max: int) -> List[dict]:
    p50 = median([m["latency_p50_s"] for m in measured if m["latency_p50_s"]] or [2.0])
    eff = max((m["effective_concurrency"] for m in measured), default=16)
    overhead = min((m["wall_s"] for m in measured), default=3.0)
    out = []
    for c in target_chars:
        size, n = plan_windows(c, workers, w_min, w_max)
        out.append({"chars": c, "n_windows": n,
                    "est_s": estimate_extract_seconds(n, eff, p50, overhead)})
    return out
```

- [ ] **Step 6: 真实运行验证(消耗少量 token)**

Run:
```bash
PYTHONPATH=backend $PY -c "
from app.eval.speed import measure_speed
r = measure_speed('.local/storage/notebooks/nb-012fb94249/src-9c312953d7_Design_of_Analog_CMOS_IC_2nd_Ed_Razavi_mineru.md', char_steps=[5000, 20000])
print('OK', [ (x['chars'], x['wall_s']) for x in r ])"
```
Expected: 打印两档(5000/20000)的墙钟耗时,且**主库不残留 eval-speed 临时 notebook**(脚本内已 `_cleanup`)。验证后再用全 5 档跑(在 run_all 中)。

- [ ] **Step 7: 确认无临时 notebook 残留**

Run:
```bash
sqlite3 .local/silicon_notebook.db "SELECT count(*) FROM notebooks WHERE name LIKE 'eval-speed-%';"
```
Expected: `0`

- [ ] **Step 8: Commit**

```bash
git add backend/eval/speed.py backend/eval/report.py backend/tests/eval/test_report.py
git commit -m "feat(eval): 真实抽取计时 measure_speed + 速度报告渲染" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: questions.yaml + inference.py 纯函数(加载/judge prompt/解析)

**Files:**
- Create: `backend/eval/questions.yaml`
- Create: `backend/eval/inference.py`
- Test: `backend/tests/eval/test_inference.py`

- [ ] **Step 1: 写 `backend/eval/questions.yaml`(spec §6.1 的 30 题,逐题落盘)**

```yaml
# 分层问答评测集。level: L1 直接 / L2 单跳 / L3 多跳综合 / L4 应拒答。
# expected_behavior: grounded | use_neighbor | synthesize | refuse_or_infer
- {id: q01, level: L1, question: "什么是 cascode connection?它的主要作用是什么?", expected_points: ["级联/串叠晶体管", "提高输出电阻"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q02, level: L1, question: "MOSFET 的 square-law characteristic 指什么?", expected_points: ["漏电流与过驱动电压平方成正比"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q03, level: L1, question: "什么是 current mirror?", expected_points: ["复制/镜像电流"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q04, level: L1, question: "bandgap reference 的目标是什么?", expected_points: ["与温度无关的基准电压"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q05, level: L1, question: "flicker noise(1/f noise)是什么?", expected_points: ["低频噪声", "与频率成反比"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q06, level: L1, question: "什么是 switched-capacitor circuit?", expected_points: ["开关电容", "等效电阻"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q07, level: L1, question: "differential amplifier 的基本概念是什么?", expected_points: ["放大差分输入", "抑制共模"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q08, level: L1, question: "MOS 晶体管进入 saturation 的条件是什么?", expected_points: ["v_DS >= v_GS - v_TH"], expected_evidence_level: grounded, expected_behavior: grounded}
- {id: q09, level: L2, question: "current mirror 有哪些典型实现或变体?", expected_points: ["简单镜像", "cascode/regulated/Wilson 等变体"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q10, level: L2, question: "cascode 技术能把输出电阻提高多少(给出量级或因子)?", expected_points: ["约 g_m·r_ds 倍"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q11, level: L2, question: "哪些电路用到了 cascode connection?", expected_points: ["cascode 电流源/镜像", "folded-cascode 运放"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q12, level: L2, question: "与 bandgap reference 相关的关键公式有哪些?", expected_points: ["PTAT/CTAT 组合", "温度系数抵消"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q13, level: L2, question: "regulated cascode 的输出电阻能达到什么量级?", expected_points: ["g_m^2·r^3 量级"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q14, level: L2, question: "CMFB(共模反馈)用在什么放大器里?", expected_points: ["全差分运放", "folded-cascode"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q15, level: L2, question: "影响 current mirror 精度(ratio error)的因素有哪些?", expected_points: ["输出/输入电压差", "失配", "沟道长度调制"], expected_evidence_level: grounded, expected_behavior: use_neighbor}
- {id: q16, level: L3, question: "为什么 cascode 既能提高输出阻抗、又会限制输出摆幅?", expected_points: ["输出电阻 ↑ 约 g_m·r_ds", "堆叠增加阈值+过驱动电压需求,吃掉 headroom"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q17, level: L3, question: "为什么 regulated cascode 比 standard cascode 输出电阻更高,代价是什么?", expected_points: ["增益级提升 g_m·r 因子", "更复杂/更多电压裕度或稳定性代价"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q18, level: L3, question: "高增益与输出摆幅在 cascode 结构里如何权衡?", expected_points: ["级数↑增益↑", "可用摆幅↓"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q19, level: L3, question: "mismatch 如何影响 current mirror 与 differential pair,机理上有何共性?", expected_points: ["阈值/尺寸失配", "电流不对称/失调电压", "共性=器件参数失配"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q20, level: L3, question: "为什么 CMOS 适合做模拟/混合信号 VLSI,但模拟设计仍需 hands-on?", expected_points: ["密度/功耗优势", "good mix of components", "模拟仍需经验/手工设计"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q21, level: L3, question: "增大 cascode 级数对增益和电压裕度分别有什么影响?", expected_points: ["增益随级数提升", "每级多吃一个阈值+过驱动"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q22, level: L3, question: "反馈如何同时影响放大器的带宽与稳定性?", expected_points: ["带宽扩展", "相位裕度/稳定性约束"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q23, level: L3, question: "从器件 square-law 出发,如何解释 current mirror 的电流复制原理?", expected_points: ["相同 v_GS", "相同尺寸 -> 相同电流"], expected_evidence_level: grounded, expected_behavior: synthesize}
- {id: q24, level: L4, question: "用 3nm FinFET 工艺实现这个 bandgap,失调会有什么具体变化?", expected_points: ["书未覆盖先进工艺节点"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
- {id: q25, level: L4, question: "在 Innovus 里用什么命令做 place_opt_design?", expected_points: ["EDA 工具命令,本 notebook 无"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
- {id: q26, level: L4, question: "这个 op amp 在 -40C 到 125C 车规下的具体失调电压是多少 mV?", expected_points: ["无具体数值"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
- {id: q27, level: L4, question: "台积电 N5 工艺的具体 SPICE 模型参数是多少?", expected_points: ["无厂商工艺参数"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
- {id: q28, level: L4, question: "把这套电路改成 GaN 工艺需要改哪些版图规则?", expected_points: ["跨工艺,书未覆盖"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
- {id: q29, level: L4, question: "这本书第 20 章讲了什么?", expected_points: ["超出实际章节,不应臆造"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
- {id: q30, level: L4, question: "ChatGPT 的注意力机制和这里的 feedback 有关系吗?", expected_points: ["完全跨域,不应强行关联"], expected_evidence_level: inferred, expected_behavior: refuse_or_infer}
```

- [ ] **Step 2: 写失败测试 `test_inference.py`**

```python
import json
from app.eval.inference import load_questions, judge_prompt, parse_judge


def test_load_questions():
    qs = load_questions("backend/eval/questions.yaml")
    assert len(qs) == 30
    assert {q["level"] for q in qs} == {"L1", "L2", "L3", "L4"}
    assert all(q.get("question") and q.get("expected_points") for q in qs)


def test_judge_prompt_includes_answer_and_points():
    msgs = judge_prompt(question="什么是 cascode?", expected_points=["提高输出电阻"],
                        answer="cascode 提高输出电阻[k1]", evidence_level="grounded",
                        expected_behavior="grounded")
    assert isinstance(msgs, list) and msgs[-1]["role"] == "user"
    blob = msgs[-1]["content"]
    assert "提高输出电阻" in blob and "cascode 提高输出电阻" in blob


def test_parse_judge_ok_and_garbage():
    j = parse_judge(json.dumps({"correctness": 2, "inference_quality": 1,
                                "grounding_consistency": True,
                                "fabricated_citation": False, "reason": "好"}))
    assert j["correctness"] == 2 and j["fabricated_citation"] is False
    bad = parse_judge("not json")
    assert bad["correctness"] == 0 and "parse_error" in bad["reason"]
```

- [ ] **Step 3: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_inference.py -v`
Expected: FAIL(`ModuleNotFoundError`)

- [ ] **Step 4: 实现 `backend/eval/inference.py`(本任务只放纯函数)**

```python
"""推断问答评测:跑 repo.ask + LLM-judge。"""
from __future__ import annotations
import json
from typing import Any, Dict, List

import yaml

_JUDGE_SCHEMA = ('{"correctness":0,"inference_quality":0,'
                 '"grounding_consistency":true,"fabricated_citation":false,"reason":""}')


def load_questions(path: str) -> List[Dict[str, Any]]:
    data = yaml.safe_load(open(path, encoding="utf-8"))
    assert isinstance(data, list) and data, "questions.yaml 应为非空列表"
    return data


def judge_prompt(question: str, expected_points: List[str], answer: str,
                 evidence_level: str, expected_behavior: str) -> List[Dict[str, str]]:
    points = "; ".join(expected_points)
    user = (
        "你是严格的问答评委。根据【期望要点】评判【系统答案】,只看是否正确与推断是否恰当。\n"
        f"问题:{question}\n"
        f"期望要点:{points}\n"
        f"期望行为:{expected_behavior}(grounded=应有据引用 / use_neighbor=应用到邻居 / "
        "synthesize=应综合多个事实 / refuse_or_infer=KG 无据应说明或标(推断),不得伪造引用)\n"
        f"系统答案:{answer}\n"
        f"系统自报 evidence_level:{evidence_level}\n\n"
        "评分:correctness 0/1/2(覆盖要点且无事实错误);inference_quality 0/1/2"
        "(该综合时综合、该标推断时标);grounding_consistency(evidence_level 是否相符);"
        "fabricated_citation(是否给推断句/无关项强加 [k] 引用,L4 尤其关注)。"
        "reason 一句话。"
    )
    return [{"role": "user", "content": user}]


def parse_judge(raw: str) -> Dict[str, Any]:
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {"correctness": 0, "inference_quality": 0,
                "grounding_consistency": False, "fabricated_citation": False,
                "reason": "parse_error"}
    def _int(k):
        v = d.get(k, 0)
        return v if isinstance(v, int) and 0 <= v <= 2 else 0
    return {
        "correctness": _int("correctness"),
        "inference_quality": _int("inference_quality"),
        "grounding_consistency": bool(d.get("grounding_consistency", False)),
        "fabricated_citation": bool(d.get("fabricated_citation", False)),
        "reason": str(d.get("reason", ""))[:200],
    }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_inference.py -v`
Expected: PASS(3 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/eval/questions.yaml backend/eval/inference.py backend/tests/eval/test_inference.py
git commit -m "feat(eval): 30 题分层问题集 + inference 纯函数(加载/judge/解析)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: inference.py 真实问答 run + 推断报告

**Files:**
- Modify: `backend/eval/inference.py`(追加 run_inference)
- Modify: `backend/eval/report.py`(追加 render_inference_report)
- Test: `backend/tests/eval/test_report.py`(追加)

- [ ] **Step 1: 写 render_inference_report 失败测试**

```python
from app.eval.report import render_inference_report


def test_render_inference_report():
    rows = [
        {"id": "q01", "level": "L1", "question": "什么是 cascode?",
         "answer": "...[k1]", "evidence_level": "grounded",
         "judge": {"correctness": 2, "inference_quality": 2,
                   "grounding_consistency": True, "fabricated_citation": False,
                   "reason": "准确"}},
        {"id": "q16", "level": "L3", "question": "为何...摆幅?",
         "answer": "...", "evidence_level": "overview",
         "judge": {"correctness": 1, "inference_quality": 1,
                   "grounding_consistency": True, "fabricated_citation": False,
                   "reason": "部分综合"}},
    ]
    md = render_inference_report(rows)
    assert "# 推断问答评测报告" in md
    assert "L1" in md and "L3" in md
    assert "落差" in md      # L3 vs L1 落差
    assert "q16" in md
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_report.py::test_render_inference_report -v`
Expected: FAIL(`cannot import name 'render_inference_report'`)

- [ ] **Step 3: 追加 `render_inference_report` 到 `backend/eval/report.py`**

```python
def render_inference_report(rows: list) -> str:
    from collections import defaultdict
    by_level = defaultdict(list)
    for r in rows:
        by_level[r["level"]].append(r)
    out = ["# 推断问答评测报告", "",
           "judge=deepseek。correctness/inference_quality 0–2;越高越好。", "",
           "## 分层得分", "",
           "| 层 | 题数 | 平均正确性 | 平均推断质量 | grounding一致率 | 伪引用率 |",
           "|---|---|---|---|---|---|"]
    avg = {}
    for lvl in ("L1", "L2", "L3", "L4"):
        rs = by_level.get(lvl, [])
        if not rs:
            continue
        c = sum(r["judge"]["correctness"] for r in rs) / len(rs)
        iq = sum(r["judge"]["inference_quality"] for r in rs) / len(rs)
        gc = sum(1 for r in rs if r["judge"]["grounding_consistency"]) / len(rs)
        fc = sum(1 for r in rs if r["judge"]["fabricated_citation"]) / len(rs)
        avg[lvl] = c
        out.append(f"| {lvl} | {len(rs)} | {c:.2f} | {iq:.2f} | {gc:.0%} | {fc:.0%} |")
    if "L1" in avg and "L3" in avg:
        out += ["", f"**推断能力信号:L3 多跳综合均分 {avg['L3']:.2f} − L1 直接均分 "
                f"{avg['L1']:.2f} = 落差 {avg['L1'] - avg['L3']:+.2f}**(落差越小越好)。"]
    out += ["", "## 逐题明细", "",
            "| id | 层 | 正确 | 推断 | 伪引用 | evidence_level | 理由 |",
            "|---|---|---|---|---|---|---|"]
    for r in rows:
        j = r["judge"]
        out.append(f"| {r['id']} | {r['level']} | {j['correctness']} | "
                   f"{j['inference_quality']} | {'是' if j['fabricated_citation'] else '否'} | "
                   f"{r['evidence_level']} | {j['reason']} |")
    return "\n".join(out) + "\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/test_report.py -v`
Expected: PASS(全部 passed)

- [ ] **Step 5: 追加 `run_inference` 到 `backend/eval/inference.py`**

```python
def run_inference(notebook_id: str, questions_path: str = "backend/eval/questions.yaml"
                  ) -> List[Dict[str, Any]]:
    """对每题:repo.ask -> LLM-judge。返回逐题结果(含 judge)。"""
    from app.core.config import Settings
    from app.models.schemas import AskRequest
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(Settings())
    assert repo.llm_client.configured, "LLM 未配置(.env)"
    questions = load_questions(questions_path)
    rows: List[Dict[str, Any]] = []
    for q in questions:
        resp = repo.ask(notebook_id, AskRequest(question=q["question"]))
        msgs = judge_prompt(q["question"], q["expected_points"], resp.answer or resp.conclusion,
                            resp.evidence_level, q["expected_behavior"])
        try:
            judged = parse_judge(repo.llm_client.chat_json(msgs, _JUDGE_SCHEMA))
        except Exception as exc:  # judge 调用失败不应中断整轮
            judged = {"correctness": 0, "inference_quality": 0,
                      "grounding_consistency": False, "fabricated_citation": False,
                      "reason": f"judge_error: {type(exc).__name__}"}
        rows.append({
            "id": q["id"], "level": q["level"], "question": q["question"],
            "answer": resp.answer or resp.conclusion,
            "evidence_level": resp.evidence_level,
            "anchors": len(resp.anchors), "top_relevance": resp.top_relevance,
            "judge": judged,
        })
        print(f"[infer] {q['id']} {q['level']} -> correctness={judged['correctness']} "
              f"evidence={resp.evidence_level}", flush=True)
    return rows
```

- [ ] **Step 6: 真实运行验证(2 题冒烟,消耗少量 token)**

Run:
```bash
PYTHONPATH=backend $PY -c "
from app.eval.inference import run_inference, load_questions
import app.eval.inference as I
qs = load_questions('backend/eval/questions.yaml')[:2]
import tempfile, yaml, pathlib
p = pathlib.Path(tempfile.mktemp(suffix='.yaml')); p.write_text(yaml.safe_dump(qs, allow_unicode=True))
rows = run_inference('nb-012fb94249', str(p))
print('OK', [(r['id'], r['judge']['correctness'], r['evidence_level']) for r in rows])"
```
Expected: 打印 2 题的 correctness(0–2)与 evidence_level;无异常。

- [ ] **Step 7: Commit**

```bash
git add backend/eval/inference.py backend/eval/report.py backend/tests/eval/test_report.py
git commit -m "feat(eval): 真实问答 run_inference + 推断报告(L3-L1 落差)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: run_all.py CLI 编排 + 文档

**Files:**
- Create: `backend/eval/run_all.py`
- Modify: `docs/superpowers/specs/2026-06-06-kg-eval-suite-design.md`(末尾补"如何运行")

- [ ] **Step 1: 实现 `backend/eval/run_all.py`**

```python
"""一键评测。用法:
PYTHONPATH=backend python -m app.eval.run_all --notebook nb-012fb94249 --only quality,speed,inference
"""
from __future__ import annotations
import argparse, pathlib
from datetime import datetime

DEFAULT_DB = ".local/silicon_notebook.db"
RAZAVI = (".local/storage/notebooks/nb-012fb94249/"
          "src-9c312953d7_Design_of_Analog_CMOS_IC_2nd_Ed_Razavi_mineru.md")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook", default="nb-012fb94249")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--only", default="quality,speed,inference")
    ap.add_argument("--source-md", default=RAZAVI)
    ap.add_argument("--target-seconds", type=int, default=120)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = pathlib.Path(a.out or f".local/eval_runs/{ts}")
    out.mkdir(parents=True, exist_ok=True)
    print(f"[eval] -> {out}")

    if "quality" in only:
        from app.eval.probes import run_quality
        from app.eval.report import render_quality_report
        pb = run_quality(a.db, a.notebook)
        (out / "quality_report.md").write_text(render_quality_report(pb), encoding="utf-8")
        print("[eval] quality_report.md done")

    if "speed" in only:
        from app.core.config import Settings
        from app.eval.speed import measure_speed, extrapolate
        from app.eval.report import render_speed_report
        s = Settings()
        measured = measure_speed(a.source_md)
        extra = extrapolate(measured, [100000, 200000, 500000, 1000000],
                            s.kg_extract_workers, s.kg_window_min_chars, s.kg_window_max_chars)
        within = [m["chars"] for m in measured if m["wall_s"] <= a.target_seconds]
        rec = max(within) if within else (measured[0]["chars"] if measured else 0)
        (out / "speed_report.md").write_text(
            render_speed_report(measured, extra, rec, a.target_seconds), encoding="utf-8")
        print("[eval] speed_report.md done")

    if "inference" in only:
        from app.eval.inference import run_inference
        from app.eval.report import render_inference_report
        rows = run_inference(a.notebook)
        (out / "inference_report.md").write_text(render_inference_report(rows), encoding="utf-8")
        print("[eval] inference_report.md done")

    print(f"[eval] all done -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 只跑质量(0 token)验证编排可用**

Run: `PYTHONPATH=backend $PY -m app.eval.run_all --only quality`
Expected: 生成 `.local/eval_runs/<ts>/quality_report.md`;打印 `quality_report.md done`。

- [ ] **Step 3: 跑全部测试确认无回归**

Run: `PYTHONPATH=backend $PY -m pytest backend/tests/eval/ -v`
Expected: 全部 PASS。

- [ ] **Step 4: 补文档"如何运行"到 spec 末尾**

在 `docs/superpowers/specs/2026-06-06-kg-eval-suite-design.md` 末尾追加:

```markdown
## 12. 如何运行
- 质量(0 token):`PYTHONPATH=backend python -m app.eval.run_all --only quality`
- 速度(少量 token):`PYTHONPATH=backend python -m app.eval.run_all --only speed`
- 推断(低 token):`PYTHONPATH=backend python -m app.eval.run_all --only inference`
- 全部:`PYTHONPATH=backend python -m app.eval.run_all`
- 产物:`.local/eval_runs/<时间戳>/{quality,speed,inference}_report.md`
```

- [ ] **Step 5: Commit**

```bash
git add backend/eval/run_all.py docs/superpowers/specs/2026-06-06-kg-eval-suite-design.md
git commit -m "feat(eval): run_all CLI 编排 + 运行文档" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: 完整端到端跑一遍 + 基线归档

**Files:**
- Create: `.local/eval_runs/<ts>/`(产物,gitignore;不提交)

- [ ] **Step 1: 全量运行(消耗 token:速度~几万 + 推断~二三十万)**

Run: `PYTHONPATH=backend $PY -m app.eval.run_all --notebook nb-012fb94249`
Expected: 三份报告生成;无异常;主库无 `eval-speed-%` 残留。

- [ ] **Step 2: 人工审阅三份报告**

Run: `ls -la .local/eval_runs/ && cat .local/eval_runs/*/quality_report.md | head -60`
检查:① 质量报告 5 本书疑似非原子率合理(Razavi 10–15%);② 速度报告给出推荐文档上限;③ 推断报告 L3−L1 落差与 L4 伪引用率。

- [ ] **Step 3: 确认临时数据已清理**

Run: `sqlite3 .local/silicon_notebook.db "SELECT count(*) FROM notebooks WHERE name LIKE 'eval-speed-%';"`
Expected: `0`

- [ ] **Step 4: 最终 commit(代码已提交,此步仅在有遗漏改动时)**

```bash
git status
# 若有评测代码改动:git add backend/eval backend/tests/eval && git commit -m "fix(eval): 端到端跑通后的修正" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review(写计划者自检结论)

**Spec 覆盖:** §4 质量→Task 1–4;§5 速度→Task 5–6;§6 推断→Task 7–8;§7 目录→全程;§10 回归→run_all 支持 `--only`/`--notebook`。全部有对应任务。

**Placeholder:** 无 TODO/TBD;每个代码步给完整代码;每个真实 LLM 步给运行命令与预期。

**类型一致:** `EvalDB.objects` 返回的 dict 键(`id/name/payload/evidence/evidence_count/source_id`)在 probes/report 中一致使用;`aggregate_quality` 输出键与 `render_quality_report` 读取键一致;`measure_speed` 行键与 `render_speed_report` 列一致;`run_inference` 行键(`id/level/question/answer/evidence_level/judge`)与 `render_inference_report` 一致;`parse_judge` 输出键与报告读取键一致。

**已知风险(执行时注意):**
- 探针对 `FinFET`/cascode 变体等会有误报——这是设计内的"疑似信号",报告已声明需抽样校准。
- `measure_speed` 在主库建临时 notebook 并清理;若中途异常,`finally` 仍会清理。Task 6 Step 7 / Task 10 Step 3 校验无残留。
- 速度/推断步消耗真实 token;先用 Task 6 Step 6、Task 8 Step 6 的小冒烟验证再全量。
